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


def push(
    focus: str,
    *,
    repo: str = REPO,
    by: str = "",
    transport: Transport | None = None,
    timeout: float = TIMEOUT_S,
) -> str:
    """Best-effort push of one repo_status row. Never raises."""
    if not focus:
        return "no status to push"
    base = mesh.endpoint()
    token = os.environ.get(mesh.TOKEN_ENV, "").strip()
    now = int(time() * 1000)
    payload = {
        "since": now,
        "push": {
            "now": now,
            "memory": [],
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
    return f"pushed status to {mesh.scrub(base)}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["push"]:
        print("usage: python3 -m aios.status push", file=sys.stderr)
        return 2
    root = Path(os.environ.get("AIOS_ROOT", "/aios"))
    try:
        from . import identity

        by = identity.node_label(root)
    except Exception:  # noqa: BLE001 — a naming failure must not block the push
        by = ""
    print(push(facts(root), by=by))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
