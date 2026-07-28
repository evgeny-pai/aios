"""This node's client for the ConductorAI mesh: ask before you compile.

`forge minimize` does one full build per lever — that is the honest cost of
measuring what a feature is worth, and it is also the cost this module exists to
stop paying twice. Another machine on the LAN may already have compiled this exact
package for this exact CHOST. If it has, portage fetches a binary and the lever
costs a download instead of forty minutes of compiler.

A peer can only ever save time; it cannot change the result. The lockfile still
decides what gets installed: `emerge --binpkg-respect-use=y`
(`forge.portage.emerge_argv`) makes portage REJECT a prebuilt package whose USE
flags disagree with the lock and build from source instead, and `forge binpkg`
answers the same question before anything is fetched. So a peer is a cache, never
an authority — the worst a stale or hostile one manages is a wasted download.

The mesh's shape here is read from ConductorAI's own source, not assumed:

  - one share daemon per HOST, bound 0.0.0.0:4748, `Authorization: Bearer <token>`
    on every route (`src/share.ts`, `src/config.ts`);
  - `GET /api/peer-state` answers with the state of the daemon you asked — ONE
    machine's export, not a directory of machines. The mesh's peer table is not
    published over HTTP; each daemon polls its peers itself and merges what it
    learns into the agents/leases it exports. So from inside a pod this node sees
    the daemon it can route to, and the build capacity that daemon advertises;
  - `/api/build/binpkg/` is the data plane, and the only path build output takes
    between machines;
  - from a pod, `host.docker.internal` is the only route to any of it.

Two granularities of help, and only one of them has a server on the other end:

  - a whole PACKAGE, via a peer's binpkg cache — `build_env`, `mesh env`. This is
    what ConductorAI actually implements, and `/api/build/binpkg/` is its data plane;
  - a single COMPILE JOB, via distcc — `distcc_hosts`, `mesh distcc`. ConductorAI now
    runs a distccd inside each machine's builder container and advertises its port,
    liveness and gcc version, so a slot here is a real offer rather than an inference
    from spare cores. Discovery is `/api/build/hosts`, NOT peer-state, because the
    only question that matters is whether a machine OTHER than this one has capacity:
    a slot on this node's own host is the same cores behind a network hop, and
    `distcc_hosts` skips it. With no remote slot the list is empty, distcc falls back
    to the local compiler, and the build still works.

    "A peer cannot do worse than waste a download" needs one extra thing to stay
    true here, because DISTCC_HOSTS is a language and not a list of names: an entry
    names a machine to send *source* to, and `@host:COMMAND` names a command to run
    there. So a host entry is never remote text — `distcc_hosts` builds it from the
    URL this node dialled, and `portage.distcc_host` refuses anything that is not a
    hostname.

Being off the mesh is the normal case, not an error: nothing here raises, and every
function degrades to "no peers".

The credential is the delicate part. Portage fetches a binhost with wget/curl, which
cannot add an Authorization header, so the token has to ride in the URL — and a URL
with a token in it is precisely the string that ends up in a journal, a prompt, or a
screenshot. Two rules keep that safe:

  - `build_env` is the ONLY thing that puts the real token into a URL, and it hands
    the result straight to portage as a subprocess environment;
  - everything a human or a log can see goes through `redacted` or `scrub`, and the
    CLI prints `${CONDUCTORAI_SHARE_TOKEN}` — the reference, not the value — so its
    output is both shell-eval-able and safe to paste.

The token is read from the environment only. This module never opens
~/.conductorai/config.json and never writes the value anywhere.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from forge import lock as lock_mod
from forge import portage

#: Base URL of the share daemon. The manifest sets this (container/k8s/aios.yaml);
#: the default is the route a pod has to its host, so a node with no env still tries
#: the right place.
URL_ENV = "AIOS_MESH_URL"
DEFAULT_URL = "http://host.docker.internal:4748"
DEFAULT_PORT = 4748

PEER_STATE = "/api/peer-state"
BINPKG_PATH = "/api/build/binpkg/"

#: Machines the local daemon knows can build a CHOST — including OTHER machines,
#: which `PEER_STATE` by design never reports. `compile_slots` says why that
#: distinction is the difference between distcc working and distcc being theatre.
BUILD_HOSTS_PATH = "/api/build/hosts"

TOKEN_ENV = "CONDUCTORAI_SHARE_TOKEN"

#: What replaces a credential in anything human-visible. `forge.portage` owns the
#: value for the same reason it owns `binhost_env`: the code that puts a token into
#: a URL and the code that takes it back out must not be able to disagree.
REDACTION = portage.REDACTION

#: The daemon polls its own peers with a 2s budget (src/share.ts), so 2s is the
#: mesh's own idea of "answering". A build must never wait longer than that to find
#: out whether it has help.
TIMEOUT_S = 2.0

#: A peer-state export is tens of KB. Anything vastly larger is not a share daemon,
#: and reading it into memory to find that out would be the mistake.
MAX_BYTES = 1 << 20

#: (url, headers, timeout) -> response body. Injected by tests; raises on failure,
#: which is why every call site treats an exception as "no mesh".
Transport = Callable[[str, dict, float], bytes]


# --- what the mesh says -------------------------------------------------------


@dataclass(frozen=True)
class Distcc:
    """A machine's offer to run single compile jobs — `DistccState` in src/build.ts.

    `gcc` is the field that decides whether the offer is usable. distcc sends
    preprocessed source and runs the SERVER's compiler over it, and checks nothing:
    a peer one gcc release ahead will happily compile this node's translation units
    into objects a different compiler produced. Since the mesh's builders all run
    the same date-pinned stage3 the versions normally agree, and the point of
    carrying it is to notice the day they do not.
    """

    port: int = 0
    running: bool = False
    gcc: str = ""
    jobs: int = 0
    #: False when that machine's distccd is bound to ITS loopback: up, but closed to
    #: every other machine. Read as False when absent, because a daemon too old to
    #: send it is also too old to run distccd at all.
    peer_reachable: bool = False


@dataclass(frozen=True)
class Slot:
    """One machine's compile capacity, as `/api/build/hosts` describes it.

    Not a `Peer`: this comes from a route that reports the machines the local daemon
    KNOWS ABOUT, not the one machine `/api/peer-state` describes. That distinction is
    the reason the route exists — see `compile_slots`.

    `local` is the load-bearing field. See `distcc_hosts` for why a local slot is
    worth nothing, which is not obvious and cost a measured afternoon to establish.
    """

    host: str = ""
    address: str = ""
    local: bool = True
    cores: int = 0
    distcc: Distcc | None = None


@dataclass(frozen=True)
class Builder:
    """One machine's advertised ability to build — `BuilderState` in src/build.ts.

    `lock_digest` is the interesting field: a peer whose cache was built from this
    node's lockfile digest has packages that are directly installable here, whereas
    a mismatched digest usually means every package gets rebuilt anyway.
    """

    chost: str = ""
    cores: int = 0
    binpkgs: int = 0
    image: str = ""
    running: bool = False
    lock_digest: str = ""   # "" where the response carries null
    distcc: Distcc | None = None    # None where the peer offers no compile slots


@dataclass(frozen=True)
class Peer:
    """A machine this node can reach, as its share daemon describes itself.

    Fields are the ones `exportPeerState` actually sends, minus `macs`: MAC
    addresses identify a machine's owner and buy this node nothing.

    `url` is the exception — it is where THIS node reached the peer, not something
    the peer said about itself.
    """

    host: str = ""
    url: str = ""
    machine_id: str = ""
    exported_at: int = 0     # ms since epoch, on the peer's clock
    ollama: bool = False
    builder: Builder | None = None
    agents: tuple[str, ...] = ()    # agent names (PeerInfo.name)
    leases: tuple[str, ...] = ()    # leased resources (LeaseInfo.resource)

    def serves(self, chost: str) -> bool:
        """Can this peer produce packages this machine can install at all?"""
        return bool(chost) and self.builder is not None and self.builder.chost == chost

    @property
    def building(self) -> tuple[str, ...]:
        """What the mesh says is being compiled right now.

        `build:<chost>:<atom>` is ConductorAI's build lease name (build.ts
        `buildLeaseName`), so these are the atoms another machine has claimed —
        worth knowing before starting the same compile.
        """
        return tuple(name for name in self.leases if name.startswith("build:"))


@dataclass(frozen=True)
class Mesh:
    """What one look at the mesh found. `detail` is why, in words a human can act on."""

    endpoint: str = ""
    reachable: bool = False
    detail: str = ""
    peers: tuple[Peer, ...] = ()

    def serving(self, chost: str) -> tuple[Peer, ...]:
        return tuple(peer for peer in self.peers if peer.serves(chost))


# --- configuration ------------------------------------------------------------


def endpoint() -> str:
    """Base URL of the share daemon this node talks to.

    Read from the environment on every call rather than cached at import: a status
    command that reports a module constant cannot see the env that overrode it.
    """
    raw = (os.environ.get(URL_ENV) or "").strip() or DEFAULT_URL
    if "://" not in raw:
        raw = f"http://{raw}"
    return raw.rstrip("/")


def _token() -> str:
    """The share token, from the environment only. Never logged, never returned up."""
    return (os.environ.get(TOKEN_ENV) or "").strip()


def scrub(text: str) -> str:
    """Text made safe to print, journal, or paste into a prompt.

    Two passes, because a credential can arrive two ways: inside a URL's userinfo
    (the shape portage needs) and as the bare token (an error message that quoted a
    header, or an AIOS_MESH_URL someone helpfully put a password in). Anything
    human-visible in this module goes through here, including error text this
    module did not write.
    """
    out = portage.redact_url(text)
    token = _token()
    return out.replace(token, REDACTION) if token else out


def chost(path: str | Path | None = None) -> str:
    """The CHOST this machine wants packages for, per the lockfile. "" if unknown.

    Read with `check=False`: a digest mismatch is a real problem but it is not this
    module's to report, and refusing to name the CHOST would only make the briefing
    less useful about it.
    """
    candidates = [Path(path)] if path else [
        Path(os.environ.get("AIOS_ROOT", "/aios")) / lock_mod.DEFAULT_PATH,
        Path(lock_mod.DEFAULT_PATH),
    ]
    for candidate in candidates:
        try:
            lock = lock_mod.load(candidate, check=False)
        except (lock_mod.LockError, OSError, ValueError):
            continue
        found = str(lock.get("system", {}).get("chost", "") or "")
        if found:
            return found
    return ""


# --- reading the mesh ---------------------------------------------------------


def _urlopen(url: str, headers: dict, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_BYTES)


def _snippet(raw: bytes) -> str:
    """A few printable characters of a body that was not JSON.

    Sanitized hard: this lands on a terminal and in a system prompt, and a body from
    the network is the last thing that should get to emit escape sequences to either.
    """
    text = raw[:200].decode("utf-8", "replace")
    printable = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text)
    return scrub(" ".join(printable.split())[:60])


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _dicts(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _distcc(payload: object) -> Distcc | None:
    if not isinstance(payload, dict):
        return None
    return Distcc(
        port=_as_int(payload.get("port")),
        running=bool(payload.get("running")),
        gcc=str(payload.get("gcc") or ""),
        jobs=_as_int(payload.get("jobs")),
        peer_reachable=bool(payload.get("peer_reachable")),
    )


def _builder(raw: object) -> Builder | None:
    if not isinstance(raw, dict):
        return None
    return Builder(
        chost=str(raw.get("chost") or ""),
        cores=_as_int(raw.get("cores")),
        binpkgs=_as_int(raw.get("binpkgs")),
        image=str(raw.get("image") or ""),
        running=bool(raw.get("running")),
        lock_digest=str(raw.get("lock_digest") or ""),
        distcc=_distcc(raw.get("distcc")),
    )


def _peer(payload: dict, url: str) -> Peer:
    builder = _builder(payload.get("builder"))
    return Peer(
        host=str(payload.get("host") or ""),
        url=url,
        machine_id=str(payload.get("machine_id") or ""),
        exported_at=_as_int(payload.get("exported_at")),
        ollama=bool(payload.get("ollama")),
        builder=builder,
        agents=tuple(str(a.get("name") or "?") for a in _dicts(payload.get("agents"))),
        leases=tuple(str(l.get("resource") or "?") for l in _dicts(payload.get("leases"))),
    )


def look(transport: Transport | None = None, *, timeout: float = TIMEOUT_S) -> Mesh:
    """One request to the share daemon. Never raises, never waits long.

    Every failure is a `Mesh` with `reachable=False` and a `detail` that says which
    failure it was, because "no mesh" and "wrong token" want different responses
    from whoever reads it — and neither is a reason to fail a build.
    """
    base = endpoint()
    token = _token()
    headers = {"Accept": "application/json", "User-Agent": "aios-mesh/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        raw = (transport or _urlopen)(base + PEER_STATE, headers, timeout)
    except urllib.error.HTTPError as exc:      # a subclass of URLError; catch first
        hint = (
            f" — {TOKEN_ENV} is "
            + ("set but not accepted" if token else "not set")
            + ", so the daemon refused the request"
        ) if exc.code == 401 else ""
        return Mesh(base, False, scrub(f"answered HTTP {exc.code}{hint}"))
    except Exception as exc:  # noqa: BLE001 — deliberate, see below
        # Deliberately every ordinary exception: a transport is arbitrary code
        # (urllib alone raises URLError, TimeoutError, socket.gaierror and ssl
        # errors), and this module's contract is that no build dies because the
        # network misbehaved. Ctrl-C and SystemExit are BaseException and so still
        # propagate — an operator interrupting is not a mesh failure.
        #
        # `reason` unwraps urllib's "<urlopen error ...>" packaging, and the cap keeps
        # a chatty exception from swamping a prompt it is one line of.
        reason = getattr(exc, "reason", None) or exc
        return Mesh(base, False, scrub(f"unreachable: {type(exc).__name__}: {reason}")[:160])

    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return Mesh(base, False, f"answered {len(raw or b'')} bytes that are not JSON "
                                 f"({_snippet(raw or b'')!r}) — not a share daemon")
    if not isinstance(payload, dict) or not str(payload.get("host") or ""):
        return Mesh(base, False, "answered JSON with no `host` field — not a peer-state")

    return Mesh(base, True, "", (_peer(payload, base),))


def available(transport: Transport | None = None, *, timeout: float = TIMEOUT_S) -> bool:
    """Is the mesh both reachable and usable from here? Never raises."""
    return look(transport, timeout=timeout).reachable


def peers(transport: Transport | None = None, *, timeout: float = TIMEOUT_S) -> list[Peer]:
    """The machines this node can see. Empty is the normal case, not an error."""
    return list(look(transport, timeout=timeout).peers)


# --- handing a peer to portage ------------------------------------------------


def _base_for(host: Peer | str = "") -> str:
    """Base URL for a target named as a Peer, a hostname, or a full URL.

    A bare hostname is resolved against this node's own endpoint port, since every
    share daemon in the mesh listens on the same one. Whether this node can actually
    route to that name is a separate question: inside a pod, only
    host.docker.internal is reachable.
    """
    if isinstance(host, Peer):
        return (host.url or endpoint()).rstrip("/")
    name = str(host).strip()
    if not name:
        return endpoint()
    if "://" in name:
        return name.rstrip("/")
    if ":" in name:                       # host:port given explicitly
        return f"http://{name}"
    port = urllib.parse.urlsplit(endpoint()).port or DEFAULT_PORT
    return f"http://{name}:{port}"


def _binpkg_base(host: Peer | str = "") -> str:
    return _base_for(host) + BINPKG_PATH


def build_env(host: Peer | str = "") -> dict[str, str]:
    """Environment for an emerge that consumes a peer's binary packages.

    Composed with `forge.portage.binhost_env`, not restated: the credential form and
    the FEATURES line that waives the binpkg signature requirement are one policy
    with one definition.
    Two copies would drift, and the copy that drifted would be the one that quietly
    stopped rejecting mismatched packages.

    Pair with `portage.emerge_argv(lock, binhost=True)` for the flags. Contains the
    real token — hand it to a subprocess, never to a log. `redacted()` is the form
    for anything a human or a journal will see.
    """
    return portage.binhost_env(_binpkg_base(host), _token() or None)


def binhost_url(host: Peer | str = "") -> str:
    """`PORTAGE_BINHOST` for a peer, credential included. UNREDACTED.

    Returned only to a caller that is about to hand it to portage. It is derived
    from `build_env` so that the one place a token enters a URL stays one place.
    """
    return build_env(host)["PORTAGE_BINHOST"]


def redacted(host: Peer | str = "") -> str:
    """The same URL as a human may see it.

    Built with a placeholder instead of stripping a real credential out afterwards:
    this function never touches the token, so no bug in a regex can make it leak
    one. With no token set it shows the plain URL, which is the truth of what
    portage would be given.
    """
    return portage.binhost_env(_binpkg_base(host), REDACTION if _token() else None)[
        "PORTAGE_BINHOST"
    ]


def _exports(env: dict[str, str], *, expand: str = "") -> str:
    """An environment as shell lines. Sorted, so the output of a verb is stable.

    Every value is `shlex.quote`d, because MANUAL.md tells the operator to run
    `eval "$(python3 -m aios.mesh distcc)"` and some of these values are built from
    another machine's JSON. `export KEY="VALUE"` is not quoting — one `"` in a peer's
    name ends the assignment and the rest of the line is a command.

    `expand` is the single substring that is deliberately shell syntax rather than
    data — `${CONDUCTORAI_SHARE_TOKEN}` in `shell_env` — so it is spliced in around
    the quoting instead of being quoted along with it, and still expands at eval
    time while everything beside it stays literal.
    """
    lines = []
    for key, value in sorted(env.items()):
        if expand and expand in value:
            before, _, after = value.partition(expand)
            rendered = f'{shlex.quote(before)}"{expand}"{shlex.quote(after)}'
        else:
            rendered = shlex.quote(value)
        lines.append(f"export {key}={rendered}")
    return "\n".join(lines)


def shell_env(host: Peer | str = "") -> str:
    """The same environment as shell lines that are safe to print.

    The token appears as `${CONDUCTORAI_SHARE_TOKEN}` — a reference, not a value —
    so the output is eval-able by a shell that already has the secret, and harmless
    in a journal, a screenshot, or an agent's transcript:

        eval "$(python3 -m aios.mesh env <host>)"
    """
    reference = "${%s}" % TOKEN_ENV if _token() else None
    return _exports(
        portage.binhost_env(_binpkg_base(host), reference), expand=reference or ""
    )


# --- handing the mesh's compile capacity to distcc ----------------------------


def local_gcc() -> str:
    """This node's compiler version, e.g. "15.3.0". "" when it cannot be read.

    `-dumpfullversion`, never `-dumpversion`: since gcc 7 the latter prints the major
    alone ("15"), which would make 15.1.0 and 15.3.0 look like the same compiler —
    the exact comparison `distcc_hosts` needs to get right.
    """
    try:
        out = subprocess.run(
            ["gcc", "-dumpfullversion"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def compile_slots(target: str = "", transport: Transport | None = None,
                  *, timeout: float = TIMEOUT_S) -> tuple[Slot, ...]:
    """Machines the local daemon knows can build `target`. Empty is normal.

    A SECOND route, not `look()`, and the reason is structural. `/api/peer-state`
    reports one machine — the daemon you asked. From inside a pod there is exactly
    one daemon reachable, its own host's, so peer-state can only ever describe this
    machine. That is fine for binary packages, because the local daemon can serve a
    copy of any peer's. It is useless for compile slots, where the whole question is
    "is there a machine OTHER than this one" — see `distcc_hosts`.

    Never raises, like everything else here: an old daemon with no such route, an
    unreachable one and a garbled body all mean "no slots".
    """
    want = target or chost()
    headers = {"Accept": "application/json", "User-Agent": "aios-mesh/0.1"}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{endpoint()}{BUILD_HOSTS_PATH}?chost={urllib.parse.quote(want)}"
    try:
        raw = (transport or _urlopen)(url, headers, timeout)
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — same contract as `look`: no build dies here
        return ()
    if not isinstance(payload, dict):
        return ()
    slots: list[Slot] = []
    for entry in _dicts(payload.get("hosts")):
        builder = _builder(entry.get("builder"))
        slots.append(Slot(
            host=str(entry.get("host") or ""),
            address=str(entry.get("address") or ""),
            # Absent `local` is read as True — the conservative direction. An older
            # daemon that does not send the field would otherwise have all of its
            # entries treated as remote capacity, which is the failure this whole
            # function exists to prevent.
            local=bool(entry.get("local", True)),
            cores=builder.cores if builder else 0,
            distcc=builder.distcc if builder else None,
        ))
    return tuple(slots)


def distcc_hosts(view: Mesh | None = None, target: str = "",
                 slots: tuple[Slot, ...] | None = None) -> list[str]:
    """Machines worth sending a compile job to, as DISTCC_HOSTS entries.

    Five things disqualify a machine, and the first is the one that is not obvious:

    1. **It is this machine.** A compile slot on the host this node is already
       running on is not capacity — it is the same cores with a network round trip
       in front of them. Measured on the machine this was written for: an M1 Max has
       8 performance cores, colima is given all 8, the node's own pod is capped at 8
       and the builder container at 8. Sending a job "away" moves it between two
       containers competing for one 8-core budget and adds preprocessing plus four
       forwarding hops. distcc pays only across MACHINES, so a local slot is skipped
       rather than ranked last.
    2. No distccd is actually listening there (`distcc.running`). ConductorAI starts
       one with the builder now, but a peer whose container is up while its daemon is
       not would otherwise be advertised as a slot and refuse every connection.
    3. The compiler disagrees. distcc runs the SERVER's gcc over source this node
       preprocessed and validates nothing, so a version mismatch does not fail — it
       silently produces objects from two different compilers. Skipped when both
       versions are known and differ; an unknown version on either side is not
       treated as a mismatch, since the mesh's builders share a pinned stage3.
    4. Its distccd is bound to its own loopback (`peer_reachable`). Up, and closed
       to this machine — sending jobs there fails every one of them.
    5. This node cannot name it. `address` comes from the URL the local daemon polls
       the peer on, never from the name the peer chose for itself — a peer that could
       name itself could choose where this machine's source goes, and
       `forge.portage.distcc_host` rejects anything that is not a hostname.

    `view` is accepted for signature compatibility with the rest of the module and
    for the empty-mesh detail line; the slots come from `compile_slots`.
    """
    want = target or chost()
    found = slots if slots is not None else compile_slots(want)
    mine = local_gcc()

    entries: list[str] = []
    for slot in found:
        if slot.local or slot.distcc is None or not slot.distcc.running:
            continue
        if not slot.distcc.peer_reachable:
            continue
        if mine and slot.distcc.gcc and slot.distcc.gcc != mine:
            continue
        try:
            entries.append(portage.distcc_host(
                slot.address,
                slot.distcc.jobs or slot.cores,
                slot.distcc.port or None,
            ))
        except ValueError:
            # Dropped, not raised: this module's contract is that no build dies
            # because the network misbehaved. One malformed entry makes distcc
            # reject the whole list, so a peer this node cannot name costs nothing
            # rather than costing every other peer.
            continue
    return entries


def distcc_env(view: Mesh | None = None, target: str = "") -> dict[str, str]:
    """Environment for an emerge that spreads its compiles across the mesh.

    Composed with `forge.portage.distcc_env` rather than restated, for the reason
    `build_env` gives: "topology lives in the environment" is one policy and gets
    one definition, next to the binhost policy it is a variant of.

    Carries no credential — distcc is not authenticated and this node hands it no
    token — so unlike `build_env` this is safe to print. `mesh distcc` prints it.
    """
    return portage.distcc_env(distcc_hosts(view, target))


def distcc_note(hosts: list[str], view: Mesh | None = None, target: str = "",
                slots: tuple[Slot, ...] | None = None) -> str:
    """What a host list means, and — when it is empty — which machine was skipped why.

    One definition, shared by the `distcc` verb and `forge build --distcc`. An empty
    list is the normal case and it has several very different causes, so it names the
    one that applies: no mesh, no peer, a peer with no distccd up, a peer whose gcc
    disagrees, or the case that surprises everybody — the only slot on offer is this
    machine's own, which is not capacity. Saying which costs a few lines; not saying
    it costs somebody an afternoon wondering why a "distributed" build ran at
    single-machine speed.
    """
    want = target or chost() or "this CHOST"
    found = slots if slots is not None else ()

    if hosts:
        return "\n".join([
            f"{len(hosts)} mesh peer(s) offer a compile slot for {want}: {' '.join(hosts)}",
            "Each is a machine whose distccd was listening and whose gcc matches this",
            "node's, as advertised over the mesh a moment ago. Before a long emerge:",
            "  distcc --show-hosts     what distcc will actually try",
            "  forge probe distcc      whether a compile through it works at all",
        ])

    lines = [f"No mesh peer offers a compile slot for {want}."]
    if view is not None and view.detail:
        lines.append(f"The mesh itself did not answer: {scrub(view.detail)}")

    local_only = [s for s in found if s.local]
    remote = [s for s in found if not s.local]
    mine = local_gcc()
    if local_only and not remote:
        lines += [
            "The only build capacity advertised is THIS machine's, which distcc cannot",
            "make faster: it is the same cores with a network round trip in front of",
            "them. A compile leaves this node only when another machine runs a builder.",
        ]
    for slot in remote:
        if slot.distcc is None or not slot.distcc.running:
            lines.append(f"{slot.host}: builder up, but no distccd listening on it.")
        elif not slot.distcc.peer_reachable:
            lines.append(
                f"{slot.host}: distccd is running but bound to that machine's own "
                "loopback, so it is closed to this one. Widening it exposes an "
                "unauthenticated compiler service to the LAN, so it is deliberate: "
                "set builder.distcc.bind on THAT machine."
            )
        elif mine and slot.distcc.gcc and slot.distcc.gcc != mine:
            lines.append(
                f"{slot.host}: skipped — its gcc is {slot.distcc.gcc}, this node's is "
                f"{mine}. distcc does not check compilers, so mixing them silently "
                "produces objects from two different ones."
            )
    lines += [
        "distcc finds nobody to send a job to, falls back to the local compiler, and",
        "the build runs here at this machine's own speed. Whole packages can still",
        "come from a peer's cache instead (python3 -m aios.mesh env).",
    ]
    return "\n".join(lines)


# --- what a human, or the agent, is told -------------------------------------


def _use_command(peer: Peer) -> str:
    """The exact command that points this build at `peer`.

    Deliberately NOT `env <peer.host>`: the name a machine calls itself is a fact
    about that machine's own LAN, and from inside a pod the only routable name is
    host.docker.internal. The URL this node actually reached the peer on is the one
    that works — and when that is already this node's endpoint, the command needs no
    argument at all. Telling the agent to use a hostname it cannot resolve would send
    it debugging DNS instead of building.
    """
    argument = "" if peer.url.rstrip("/") == endpoint() else f" {peer.url}"
    return f'eval "$(python3 -m aios.mesh env{argument})"'


def _builder_line(peer: Peer) -> str:
    if peer.builder is None:
        return "no build capacity advertised (no container runtime up, or builder off)"
    b = peer.builder
    cache = f"cache {b.lock_digest[:19]}…" if b.lock_digest else "cold cache"
    return (
        f"{b.chost}, {b.cores} core(s), {b.binpkgs} binpkg(s), "
        f"builder {'up' if b.running else 'down (startable on demand)'}, {cache}"
    )


def status(mesh: Mesh | None = None, target: str = "") -> str:
    """Human-readable state of this node's relationship to the mesh.

    Safe to run anywhere: with no daemon, no token and no lockfile it still prints
    four lines that say so.
    """
    view = mesh if mesh is not None else look()
    want = target or chost()
    lines = [
        f"endpoint  {scrub(view.endpoint)}",
        f"status    {'reachable' if view.reachable else view.detail or 'unreachable'}",
        f"token     {TOKEN_ENV} " + (
            "is set (value never printed)" if _token()
            else "is NOT set — every route answers 401"
        ),
        f"chost     {want or 'unknown (no lockfile found)'}",
    ]

    if not view.peers:
        lines += [
            "peers     none",
            "",
            "No machine is offering this node anything, so every package it needs it",
            "compiles itself. That is the normal state of a single-machine mesh, not a",
            "fault: a peer appears once its share daemon is up and it advertises a",
            "builder for this CHOST.",
        ]
        return "\n".join(lines)

    lines.append(f"peers     {len(view.peers)}")
    for peer in view.peers:
        lines.append(f"  {peer.host}  {_builder_line(peer)}")
        if peer.agents:
            lines.append(f"    agents  {', '.join(peer.agents)}")
        if peer.building:
            lines.append(f"    building now  {', '.join(peer.building)}")

    helpful = view.serving(want)
    if helpful:
        names = ", ".join(peer.host for peer in helpful)
        lines += [
            "",
            f"{names} can build {want}. Reuse it:",
            f"  {_use_command(helpful[0])}",
            "  emerge --getbinpkg --usepkg --binpkg-respect-use=y <atom>",
            f"  binhost {redacted(helpful[0])}",
        ]
    else:
        lines += [
            "",
            f"Nothing on the mesh advertises {want or 'this CHOST'} — reachable is not the",
            "same as useful. This node compiles its own packages.",
        ]
    return "\n".join(lines)


def briefing(mesh: Mesh | None = None, target: str = "") -> str:
    """The mesh paragraph appended to the agent's system prompt, via aios.node.

    Present tense, no hedging, and no credential: this text goes into a prompt and
    into transcripts. `mesh=None` means nothing was measured, which reads the same
    way as an absent mesh — the briefing must work on a machine that has none.
    """
    view = mesh or Mesh(endpoint(), False, "not measured")
    want = target or chost()
    helpful = view.serving(want)

    if not view.reachable:
        return "\n".join([
            f"  BUILD MESH — none. {scrub(view.endpoint)} is not answering:",
            f"    {view.detail or 'no reason recorded'}",
            "  Every package this machine needs, it compiles itself. That is normal, not",
            "  broken, and not something to debug unless a peer is supposed to be there.",
        ])

    if not helpful:
        others = ", ".join(peer.host for peer in view.peers) or "nothing"
        return "\n".join([
            f"  BUILD MESH — reachable at {scrub(view.endpoint)} ({others}), but no machine",
            f"  advertises {want or 'this CHOST'}. Nobody can hand you a package you could",
            "  install, so you compile your own. Check again before a long emerge:",
            "    python3 -m aios.mesh status",
        ])

    peer = helpful[0]
    b = peer.builder
    assert b is not None  # serves() is only true with a builder
    fresh = b.lock_digest and b.lock_digest == _lock_digest()
    return "\n".join([
        f"  BUILD MESH — {peer.host} can build {b.chost} for you: {b.cores} core(s), "
        f"{b.binpkgs} binpkg(s)",
        f"  cached, builder {'up' if b.running else 'down but startable'}"
        + (", and its cache was built from THIS lockfile" if fresh else "") + ".",
        "  Before a long compile, take its binaries instead:",
        f"    {_use_command(peer)}",
        "    emerge --getbinpkg --usepkg --binpkg-respect-use=y <atom>",
        "  The lockfile still decides what gets installed — portage rejects a peer's",
        "  package whose USE flags disagree with it and builds from source instead. A",
        "  peer saves time; it cannot change the result.",
    ])


def _lock_digest() -> str:
    for candidate in (
        Path(os.environ.get("AIOS_ROOT", "/aios")) / lock_mod.DEFAULT_PATH,
        Path(lock_mod.DEFAULT_PATH),
    ):
        try:
            return str(lock_mod.load(candidate, check=False).get("digest", "") or "")
        except (lock_mod.LockError, OSError, ValueError):
            continue
    return ""


# --- CLI ----------------------------------------------------------------------


USAGE = "usage: python3 -m aios.mesh {status|peers|env [host]|distcc [chost]}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "status"
    target = argv[1] if len(argv) > 1 else ""

    if command == "status":
        print(status())
        return 0

    if command == "peers":
        view = look()   # once: a second call would be a second round trip
        if not view.peers:
            why = "mesh reachable, nothing advertised" if view.reachable else view.detail
            print(f"no peers — {why}")
            return 0
        for peer in view.peers:
            print(f"{peer.host}  {_builder_line(peer)}")
        return 0

    if command == "env":
        # stdout stays exactly the shell lines, so `eval "$(...)"` is safe; anything
        # a human needs to read goes to stderr.
        if not _token():
            print(
                f"# {TOKEN_ENV} is not set: this binhost will answer 401. "
                "Inject the mesh secret first.",
                file=sys.stderr,
            )
        print(f"# binhost {redacted(target)}", file=sys.stderr)
        print(shell_env(target))
        return 0

    if command == "distcc":
        # Same split as `env`: stdout is exactly the shell lines, so
        # `eval "$(python3 -m aios.mesh distcc)"` is safe, and everything a human
        # has to read — above all what is NOT running on the other end — goes to
        # stderr, where a terminal shows it and `eval` never sees it.
        # `target` here is a CHOST, not a host: the question is who can compile FOR
        # this machine, and cross-compiling for another one is a fair thing to ask.
        # Both reads happen once and are threaded through, so the verb costs two
        # round trips rather than the five its callees would each make: `look` for
        # the mesh's own reachability detail, `compile_slots` for who can compile.
        view = look()
        slots = compile_slots(target)
        hosts = distcc_hosts(view, target, slots)
        for line in distcc_note(hosts, view, target, slots).splitlines():
            print(f"# {line}", file=sys.stderr)
        print(_exports(portage.distcc_env(hosts)))
        return 0

    print(__doc__)
    print(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
