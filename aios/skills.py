"""Push this node's skills/*/SKILL.md to the ConductorAI mesh.

skills/ is this project's own compound-engineering practice: each `SKILL.md` is
one distilled lesson, the same frontmatter-plus-body shape a Claude Code skill
uses (`name`, `description`, then the write-up). It has always been a convention
a human edited by hand; `aios.agent.SYSTEM` now tells the machine's own agent to
add to it too, via the `write_file` tool it already has (skills/ is not in that
tool's package-code blocklist — nothing there needed to change).

This module is the other half: getting a skill written on ONE node in front of
every other agent on the mesh, autonomously, with no human relaying it. Modelled
directly on `aios.mesh`'s posture — a share daemon reachable at AIOS_MESH_URL is
the normal case for "up", not "down" is the exception this degrades to:

  - one-way and best-effort. This node PUSHES; it has no use for what comes back,
    so `since` is set to "now" specifically to make the daemon's reply empty
    rather than the account's entire memory/skill history (see ConductorAI's own
    `src/share.ts`: POST /api/sync returns everything updated since `since`).
  - never raises. A mesh push must never cost the agent its turn or fail a boot;
    every failure returns as a one-line string, the same contract as `mesh.look`.
  - deliberately omits the `x-conductorai-port` request header that
    `aios.mesh`'s OWN requests never send either. ConductorAI's sync handler only
    registers the caller as a mesh PEER (`recordPeer`, in `src/share.ts`) when
    that header is present and parses as a port > 0 — this node is a pod with
    nothing listening on any port a peer could dial back, so sending it would
    register a dead endpoint the daemon polls forever. The `x-conductorai-host`
    header alone is sent for provenance (it becomes `origin_host` on every
    inserted row) without tripping that branch.
  - skill ids are deterministic (`uuid5` of `repo/slug`), not fresh per push.
    ConductorAI's merge is an upsert keyed on `id`, keeping the newer
    `updated_at` — a fresh `uuid4` every sweep would duplicate all 46-plus
    skills once per pod generation instead of updating them in place.

`pull()` is the other direction, and deliberately NOT symmetric with push(): it
sends `since` as the watermark from its own last successful pull (0 on first
run) instead of "now", specifically so the daemon's reply is NOT empty. What
comes back lands at .aios/skills-mesh/<slug>/SKILL.md rather than skills/ —
skills/ is a PAYLOAD entry aios.update.swap_in() replaces wholesale on every
self-update, so anything written there that isn't git-tracked would be deleted
on the next tick (skills/payload-list-is-a-deletion-list). A slug already
authored locally under skills/ is left untouched by design: that copy is
already where the agent reads first.

    python3 -m aios.skills push
    python3 -m aios.skills pull
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Callable

from . import mesh

SYNC_PATH = "/api/sync"

#: The daemon's own peer poll budget is 2s (src/share.ts); a push is one more
#: request of the same shape, so it inherits mesh.TIMEOUT_S's reasoning rather
#: than picking a new number.
TIMEOUT_S = mesh.TIMEOUT_S

#: A sync response is discarded unread, but still capped — the same "not a share
#: daemon" defence mesh.look() applies to a body that turns out to be huge.
MAX_BYTES = mesh.MAX_BYTES

#: Fixed, not generated: uuid5 needs a stable namespace so the SAME (repo, slug)
#: produces the SAME id on every node and every push, forever. Regenerating this
#: constant would silently duplicate every skill already in the mesh.
NAMESPACE = uuid.UUID("f2da3c91-d5bf-4f49-bd6a-249bbae7fe13")

#: (url, headers, body, timeout) -> response body, discarded by every caller here.
#: Injected by tests; raises on failure, exactly like mesh.Transport.
Transport = Callable[[str, dict, bytes, float], bytes]

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)

#: Where a pull lands what other nodes have shared. Deliberately outside skills/
#: — see the module docstring's `pull()` paragraph for why.
PULL_DIR = ".aios/skills-mesh"
PULL_STATE_PATH = ".aios/skills-pull.json"

#: A pulled row's `name` becomes a directory name (skills-mesh/<name>/SKILL.md),
#: and that row is network input from a peer this node does not authenticate
#: beyond the shared mesh token. Reject anything that is not a plain slug before
#: it ever reaches a path.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class Skill:
    slug: str
    name: str
    when_to_use: str
    body: str
    updated_at: int


def _field(frontmatter: str, key: str) -> str:
    for line in frontmatter.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return ""


def _parse(path: Path) -> Skill | None:
    """One `skills/<slug>/SKILL.md`, or None if it is not well-formed enough to push.

    Malformed is silent-skip, not an error: a skill mid-edit or missing a `name`
    is the agent's own draft, and a sweep that dies on one bad file would stop
    pushing every other one behind it.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = int(path.stat().st_mtime * 1000)
    except OSError:
        return None
    match = _FRONTMATTER.match(text)
    if not match:
        return None
    name = _field(match.group(1), "name")
    if not name:
        return None
    return Skill(
        slug=path.parent.name,
        name=name,
        when_to_use=_field(match.group(1), "description"),
        body=match.group(2).strip(),
        updated_at=mtime,
    )


def discover(root: Path) -> list[Skill]:
    """Every well-formed skill under `root/skills/*/SKILL.md`, in path order."""
    out = []
    for md in sorted((root / "skills").glob("*/SKILL.md")):
        skill = _parse(md)
        if skill is not None:
            out.append(skill)
    return out


def _skill_id(repo: str, slug: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{repo}/{slug}"))


def _row(skill: Skill, repo: str, by: str) -> dict:
    return {
        "id": _skill_id(repo, skill.slug),
        "name": skill.name,
        "when_to_use": skill.when_to_use or None,
        "body": skill.body,
        "tags": json.dumps([repo, "aios-authored"]),
        "repo": repo,
        "agent_id": None,
        "by": by or repo,
        "machine_id": None,
        "origin_host": None,
        "uses": 0,
        "created_at": skill.updated_at,
        "updated_at": skill.updated_at,
        "deleted_at": None,
    }


def _post(url: str, headers: dict, body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_BYTES)


def push(
    skills: list[Skill],
    *,
    repo: str = "aios",
    by: str = "",
    transport: Transport | None = None,
    timeout: float = TIMEOUT_S,
) -> str:
    """Best-effort push of `skills` to the mesh. Never raises.

    An empty `skills` list is not an error — a fresh pod with no skills yet, or a
    sweep that ran between two boots, both look like this — so it returns a
    one-line status without making a request.
    """
    if not skills:
        return "no skills to push"
    base = mesh.endpoint()
    token = os.environ.get(mesh.TOKEN_ENV, "").strip()
    now = int(time() * 1000)
    payload = {
        # Push-only: this node has no use for the mesh's own history, and asking
        # for it anyway (since=0) would make the daemon serialize everything it
        # has ever stored — every memory row, every doc — just to be discarded.
        "since": now,
        "push": {
            "now": now,
            "memory": [],
            "repo_status": [],
            "skills": [_row(skill, repo, by) for skill in skills],
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "aios-skills/0.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if by:
        # Provenance only. Deliberately no x-conductorai-port: see module
        # docstring — that header is what makes the daemon register this pod as
        # a peer with a callback address nothing is listening on.
        headers["x-conductorai-host"] = by
    body = json.dumps(payload).encode("utf-8")
    try:
        (transport or _post)(base + SYNC_PATH, headers, body, timeout)
    except Exception as exc:  # noqa: BLE001 — same posture as mesh.look(): never
        # fail the caller over a network that is not this machine's problem.
        reason = getattr(exc, "reason", None) or exc
        return mesh.scrub(f"push failed: {type(exc).__name__}: {reason}")[:160]
    return f"pushed {len(skills)} skill(s) to {mesh.scrub(base)}"


@dataclass(frozen=True)
class Pulled:
    """One pull's outcome. Never raised — same posture as push()'s return value."""

    ok: bool = False
    detail: str = ""
    written: tuple[str, ...] = ()
    skipped: int = 0


def _state_path(root: Path) -> Path:
    return root / PULL_STATE_PATH


def _load_state(root: Path) -> dict:
    """The last pull's watermark and per-slug cache. Missing or corrupt reads as fresh.

    No state file is what a node that has never pulled before looks like, not an
    error — same posture as push()'s empty-skills-list case.
    """
    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"since": 0, "skills": {}}
    if not isinstance(data, dict):
        return {"since": 0, "skills": {}}
    cache = data.get("skills")
    return {
        "since": _as_int(data.get("since")),
        "skills": cache if isinstance(cache, dict) else {},
    }


def _save_state(root: Path, state: dict) -> None:
    """Write .new then replace: same directory as the target, so the rename can
    never cross a device (skills/rename-fails-cross-device), and a reader never
    observes a half-written file.
    """
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".new")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _render(row: dict) -> str:
    """The inverse of `_parse`: a mesh row rendered back into a SKILL.md's own shape."""
    name = str(row.get("name") or "")
    description = str(row.get("when_to_use") or "")
    body = str(row.get("body") or "").strip()
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def pull(
    root: Path,
    *,
    repo: str = "aios",
    by: str = "",
    transport: Transport | None = None,
    timeout: float = TIMEOUT_S,
) -> Pulled:
    """Best-effort pull of skills the mesh has that this node's cache doesn't.

    Mirrors push() structurally but not in intent: push sets `since` to "now"
    specifically to keep the reply empty; pull sets it to the watermark from its
    OWN last successful pull (0 on first run) specifically so the reply is not.
    Same /api/sync endpoint — there is no separate read route — with an empty
    `push` block, since the push loop already covers sharing this node's own
    skills on its own schedule. Never raises, and never touches skills/: see the
    module docstring for why pulled content lands at .aios/skills-mesh/ instead.
    """
    state = _load_state(root)
    since = state["since"]
    cached: dict = dict(state["skills"])
    now = int(time() * 1000)
    payload = {
        "since": since,
        "push": {"now": now, "memory": [], "repo_status": [], "skills": []},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "aios-skills/0.1",
    }
    token = os.environ.get(mesh.TOKEN_ENV, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if by:
        # Provenance only, same as push() — deliberately no x-conductorai-port.
        headers["x-conductorai-host"] = by
    base = mesh.endpoint()
    body = json.dumps(payload).encode("utf-8")
    try:
        raw = (transport or _post)(base + SYNC_PATH, headers, body, timeout)
    except Exception as exc:  # noqa: BLE001 — same posture as push(): never fail
        # the caller over a network that is not this machine's problem.
        reason = getattr(exc, "reason", None) or exc
        return Pulled(False, mesh.scrub(f"pull failed: {type(exc).__name__}: {reason}")[:160])

    try:
        response = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return Pulled(False, "pull failed: mesh answered with a body that is not JSON")
    if not isinstance(response, dict):
        return Pulled(False, "pull failed: mesh answered JSON that is not an object")

    # The exact shape of `pull` is inferred, not verified against ConductorAI's own
    # source (none is checked out on this machine) — accept a dict with a `skills`
    # list, or a bare list, and say plainly when neither is what came back, so a
    # shape mismatch reads differently in a log line than a genuinely idle mesh.
    pull_payload = response.get("pull")
    if isinstance(pull_payload, dict):
        rows = pull_payload.get("skills")
    elif isinstance(pull_payload, list):
        rows = pull_payload
    else:
        rows = None
    if not isinstance(rows, list):
        return Pulled(
            True,
            "mesh answered but had no pull.skills — response shape may not match "
            "what aios.skills expects",
        )

    written: list[str] = []
    skipped = 0
    any_write_failed = False
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        slug = str(row.get("name") or "")
        if row.get("repo") != repo or row.get("deleted_at") or not _SLUG_RE.match(slug):
            skipped += 1
            continue
        if (root / "skills" / slug / "SKILL.md").exists():
            # Already authored locally — that copy is what the agent reads first,
            # so a mesh cache of the same slug adds nothing but drift risk.
            skipped += 1
            continue
        updated_at = _as_int(row.get("updated_at"))
        if updated_at <= cached.get(slug, 0):
            skipped += 1
            continue
        target = root / PULL_DIR / slug / "SKILL.md"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_render(row), encoding="utf-8")
        except OSError:
            # A row that failed to write must hold the watermark back so the next
            # tick retries it — unlike a row skipped for a deliberate reason, which
            # must not.
            any_write_failed = True
            continue
        cached[slug] = updated_at
        written.append(slug)

    next_since = since if any_write_failed else (_as_int(response.get("now")) or now)
    _save_state(root, {"since": next_since, "skills": cached})
    return Pulled(True, f"pulled {len(written)} skill(s), {skipped} skipped", tuple(written), skipped)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv not in (["push"], ["pull"]):
        print("usage: python3 -m aios.skills {push|pull}", file=sys.stderr)
        return 2
    root = Path(os.environ.get("AIOS_ROOT", "/aios"))
    try:
        from . import identity

        # node_label(), not hostname(): its own docstring names exactly this
        # situation — "everything from this machine currently reaches the mesh
        # through the host's share daemon" — which is this module's daemon too.
        by = identity.node_label(root)
    except Exception:  # noqa: BLE001 — a naming failure must not block push/pull
        by = ""
    if argv == ["push"]:
        print(push(discover(root), by=by))
    else:
        print(pull(root, by=by).detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
