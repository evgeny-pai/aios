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

    python3 -m aios.skills push
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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["push"]:
        print("usage: python3 -m aios.skills push", file=sys.stderr)
        return 2
    root = Path(os.environ.get("AIOS_ROOT", "/aios"))
    try:
        from . import identity

        # node_label(), not hostname(): its own docstring names exactly this
        # situation — "everything from this machine currently reaches the mesh
        # through the host's share daemon" — which is this module's daemon too.
        by = identity.node_label(root)
    except Exception:  # noqa: BLE001 — a naming failure must not block the push
        by = ""
    print(push(discover(root), by=by))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
