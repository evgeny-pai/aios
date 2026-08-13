"""Report this node's real, live status to ConductorAI, autonomously.

Prompted by a real, observed problem: ConductorAI's OWN peer-state "builder"
field — the thing that would answer "is there an aios node here" — is computed
by probing docker for a container ConductorAI manages itself
(`conductorai-builder-<chost>`, see its own src/build.ts). It has no code path
to a Kubernetes pod at all, so it reports no builder regardless of what is
actually running. That is a gap in ConductorAI's own source, not reachable from
this repo, and this module does not attempt to fix it.

What it CAN do is push a repo_status row for this project so that
`repo_context`/`recall` stop serving stale snapshots as if they were live: this
node's own view, at push time, was a set of memory notes 9-11 days old about a
DIFFERENT machine (MabrukV.local) — accurate when written, presented by a
summarizing tool as current fact. A live push from the node itself is the fix
for that gap, not a correction of the old notes (repo_status is one row per
`repo`, last-write-wins on `updated_at`, so a fresh push supersedes them without
needing to find and edit anything old).

repo_status alone is not enough for "show every aios node that is alive" —
it is ONE row per repo, so a second pod pushing its own status just overwrites
the first, and only the last pusher would ever be visible. Multiple pods
staying simultaneously visible needs a row EACH node owns without colliding,
which is what the memory table is for: `id` is per-row, not per-repo, so this
also pushes a heartbeat memory entry keyed to this node's own stable identity
(identity.node_id — minted once, persisted on the state volume, survives
reboots) rather than anything that changes across a push, node_label's
generation number included. Tagged `aios-node`/`liveness` so a query for "which
nodes are up" is one `recall(tags=["aios-node"])`, not a repo_context guess.

Given an `expires_at`, not left open-ended: a node that stops pushing —
crashed, pod deleted, network gone — should stop CLAIMING to be alive, and
ConductorAI already sweeps expired memory rows (db.ts: `DELETE FROM memory
WHERE expires_at < ?`) and filters them from reads before that. Set generously
past the push interval so one or two missed ticks are not mistaken for dead.

Every fact reported here is MEASURED, not declared — the lockfile's package
list is what forge decided to install, not what actually landed on disk, so the
count comes from /var/db/pkg the same way aios-init's own boot report counts it
(skills/status-must-report-effective-values: a status report that prints the
config instead of the measured value cannot diagnose the gap between them).

Same posture as aios.mesh and aios.skills: one-way, best-effort, never raises.

    python3 -m aios.status push
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from time import time
from typing import Callable

from . import mesh

SYNC_PATH = "/api/sync"
TIMEOUT_S = mesh.TIMEOUT_S
MAX_BYTES = mesh.MAX_BYTES

#: The canonical form, matching what a human would type — not this checkout's
#: own remote (which may carry an SSH host alias like `github-dazl`, a string
#: ConductorAI keys repo_status on verbatim rather than normalizing).
REPO = "github.com/evgeny-pai/aios"

#: Fixed, not generated — same reasoning as aios.skills.NAMESPACE: uuid5 needs a
#: stable namespace so the SAME node_id always produces the SAME memory/agent
#: id, on every push, forever. A distinct constant from aios.skills's, since a
#: node's liveness identity and a skill's identity are different things that
#: happening to collide would be an accident, not a feature.
NAMESPACE = uuid.UUID("3ad40b5e-d691-4d96-b8ac-0cdec50513e5")

#: How long a heartbeat claims to be true past the moment it was pushed. Set
#: generously past AIOS_STATUS_PUSH_INTERVAL's default (300s) so one or two
#: missed ticks — a slow boot, a network blip — are not mistaken for a dead
#: node; a node that is actually gone still ages out well within a session.
LIVENESS_TTL_MS = 900_000

Transport = Callable[[str, dict, bytes, float], bytes]


#: Not root-relative — it is a real system path regardless of AIOS_ROOT — so it
#: is a parameter with this default rather than derived from `root`, which
#: keeps a test's redirection explicit instead of monkey-patching Path itself.
PKG_DB = Path("/var/db/pkg")


def _installed_count(pkg_db: Path = PKG_DB) -> int:
    try:
        return sum(1 for cat in pkg_db.iterdir() if cat.is_dir() for _ in cat.iterdir())
    except OSError:
        return 0


def facts(root: Path, *, pkg_db: Path = PKG_DB) -> str:
    """One line: what this node measurably is, right now."""
    from forge import lock as lock_mod

    from . import generation as generation_mod
    from . import identity

    gen = generation_mod.current()
    try:
        digest = lock_mod.load(root / "aios.lock.json")["digest"][7:19]
    except Exception:  # noqa: BLE001 — a status report must not itself fail to report
        digest = "unknown"
    installed = _installed_count(pkg_db)
    opencode = "yes" if shutil.which("opencode") else "no"
    label = identity.node_label(root)
    return (
        f"{label}: generation {gen}, lock {digest}, {installed} packages installed, "
        f"opencode {opencode}. Reachable: kubectl exec -it -n aios aios -- aios."
    )


def _post(url: str, headers: dict, body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_BYTES)


def _node_uuid(node_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, node_id))


def push(
    focus: str,
    *,
    node_id: str = "",
    repo: str = REPO,
    by: str = "",
    transport: Transport | None = None,
    timeout: float = TIMEOUT_S,
) -> str:
    """Best-effort push of this node's status. Never raises.

    Two different rows for two different questions. `repo_status` answers "what
    is the aios PROJECT's current focus" — one row, whoever pushed last wins,
    same as a human's `set_focus`. `memory`, keyed to `node_id`, answers "which
    aios NODES are alive right now" — one row per node, all visible at once,
    aged out by `expires_at` if this node stops pushing. `node_id` is omitted
    only when the caller could not determine one (identity.node_id failed);
    the project-level push still goes out, the per-node heartbeat does not,
    since a heartbeat with no stable identity would just accumulate as a new
    row on every restart instead of updating in place.
    """
    if not focus:
        return "no status to push"
    base = mesh.endpoint()
    token = os.environ.get(mesh.TOKEN_ENV, "").strip()
    now = int(time() * 1000)
    memory_rows = []
    if node_id:
        agent_id = _node_uuid(f"agent:{node_id}")
        memory_rows.append(
            {
                "id": _node_uuid(f"liveness:{node_id}"),
                "agent_id": agent_id,
                "by": by or repo,
                "content": focus,
                "tags": json.dumps(["aios-node", "liveness"]),
                "repo": repo,
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
                "expires_at": now + LIVENESS_TTL_MS,
                "origin_host": by or None,
            }
        )
    payload = {
        "since": now,
        "push": {
            "now": now,
            "memory": memory_rows,
            "repo_status": [
                {
                    "repo": repo,
                    "focus": focus,
                    "updated_by": by or None,
                    "origin_host": by or None,
                    "updated_at": now,
                }
            ],
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "aios-status/0.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if by:
        headers["x-conductorai-host"] = by
    body = json.dumps(payload).encode("utf-8")
    try:
        (transport or _post)(base + SYNC_PATH, headers, body, timeout)
    except Exception as exc:  # noqa: BLE001 — never fail the caller over the network
        reason = getattr(exc, "reason", None) or exc
        return mesh.scrub(f"push failed: {type(exc).__name__}: {reason}")[:160]
    heartbeat = " + liveness heartbeat" if memory_rows else " (no node_id — heartbeat skipped)"
    return f"pushed status to {mesh.scrub(base)}{heartbeat}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["push"]:
        print("usage: python3 -m aios.status push", file=sys.stderr)
        return 2
    root = Path(os.environ.get("AIOS_ROOT", "/aios"))
    by, node_id = "", ""
    try:
        from . import identity

        by = identity.node_label(root)
        node_id = identity.node_id(root)
    except Exception:  # noqa: BLE001 — a naming failure must not block the push
        pass
    print(push(facts(root), node_id=node_id, by=by))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
