"""Run each mirrored repository's own test gate, on the node that serves them.

The node already hosts the git mirrors and the source releases peers update from. What
it could not say is whether what it serves actually passes — and "the mesh has a copy"
is a weaker claim than "the mesh has a copy that builds". This runs each repository's
OWN gate rather than a definition of correctness kept here, because a CI that decides
for itself what passing means stops tracking the project it is testing.

Stdlib only, like the rest of the on-target code: the target has the interpreter portage
requires and no pip.

A gate whose tool is absent is SKIPPED, loudly and with the reason recorded. That
distinction is the whole value of the report: `conductorai` needs node to run `npm test`,
the node has no node, and reporting that as a pass would be a lie while reporting it as a
failure would make the report useless — nothing is broken, it is simply not covered here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GIT_ROOT = Path(os.environ.get("AIOS_GIT_ROOT", "/srv/aios/git"))
REPORT = Path(os.environ.get("AIOS_CI_REPORT", "/srv/aios/ci/latest.json"))

#: repository name -> the command that repository uses to check itself, run at its root.
GATES: dict[str, tuple[str, ...]] = {
    "aios": ("python3", "-m", "aios.update", "gate", "."),
    "conductorai": ("npm", "test"),
}

#: How long any single gate may run before it is a failure rather than a hang. A CI that
#: blocks forever on one repository reports nothing about the others.
TIMEOUT_S = int(os.environ.get("AIOS_CI_TIMEOUT", "1800"))


class CIError(Exception):
    """Message is user-facing."""


def gate_for(repo: str) -> tuple[str, ...] | None:
    return GATES.get(repo)


def mirrors(git_root: Path | None = None) -> list[str]:
    """Repository names this node hosts, from the bare mirrors it serves."""
    root = Path(git_root) if git_root is not None else GIT_ROOT
    try:
        return sorted(p.name[:-4] for p in root.iterdir() if p.name.endswith(".git"))
    except OSError:
        return []


def _clone(bare: Path, into: Path) -> None:
    result = subprocess.run(
        ("git", "clone", "--quiet", "--depth", "1", str(bare), str(into)),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        raise CIError(f"clone of {bare.name} failed: {result.stderr.strip()[:300]}")


def run_one(repo: str, bare: Path, *, timeout_s: int = TIMEOUT_S) -> dict:
    """Clone one mirror and run its gate. Never raises for a failing gate."""
    gate = gate_for(repo)
    if gate is None:
        return {"repo": repo, "status": "skipped", "reason": "no gate defined for this repository"}
    if shutil.which(gate[0]) is None:
        return {
            "repo": repo,
            "status": "skipped",
            "reason": f"{gate[0]} is not installed on this node, so `{' '.join(gate)}` cannot run",
            "gate": " ".join(gate),
        }

    started = time.time()
    with tempfile.TemporaryDirectory() as work:
        tree = Path(work) / repo
        try:
            _clone(bare, tree)
        except CIError as exc:
            return {"repo": repo, "status": "error", "reason": str(exc)}
        # stdin closed on purpose: a gate that waits on input would otherwise hang the
        # whole report, and there is nobody to type into it.
        try:
            result = subprocess.run(
                gate,
                cwd=tree,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout_s,
                env={**os.environ, "PYTHONPATH": str(tree), "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return {
                "repo": repo,
                "status": "error",
                "reason": f"gate exceeded {timeout_s}s",
                "gate": " ".join(gate),
                "seconds": round(time.time() - started, 1),
            }
        tail = (result.stdout + result.stderr).strip().splitlines()
        return {
            "repo": repo,
            "status": "pass" if result.returncode == 0 else "fail",
            "gate": " ".join(gate),
            "exit_code": result.returncode,
            "commit": _head(bare),
            "seconds": round(time.time() - started, 1),
            # The tail, not the whole log: this is served over HTTP to peers and a full
            # emerge log would be megabytes of something nobody reads.
            "output_tail": tail[-12:],
        }


def _head(bare: Path) -> str:
    result = subprocess.run(
        ("git", "--git-dir", str(bare), "rev-parse", "--short", "HEAD"),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return result.stdout.strip() or "unknown"


def run_all(git_root: Path | None = None, report: Path | None = None) -> dict:
    root = Path(git_root) if git_root is not None else GIT_ROOT
    names = mirrors(root)
    if not names:
        raise CIError(f"no bare mirrors under {root} — nothing to test")
    results = [run_one(name, root / f"{name}.git") for name in names]
    summary = {
        "repos": results,
        # `ok` counts a skip as not-a-failure but records it separately, so a report
        # that is all skips cannot read as a green build.
        "ok": all(r["status"] in ("pass", "skipped") for r in results),
        "passed": sum(r["status"] == "pass" for r in results),
        "skipped": sum(r["status"] == "skipped" for r in results),
        "failed": sum(r["status"] in ("fail", "error") for r in results),
    }
    out = Path(report) if report is not None else REPORT
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".partial")
        tmp.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        tmp.replace(out)
    except OSError as exc:
        print(f"aios.ci: could not write {out}: {exc}", file=sys.stderr)
    return summary


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "help"
    try:
        if command == "run":
            summary = run_all()
            for item in summary["repos"]:
                line = f"  {item['status']:<8} {item['repo']}"
                if item.get("commit"):
                    line += f" @ {item['commit']}"
                if item.get("reason"):
                    line += f" — {item['reason']}"
                print(line)
            print(
                f"{'ok' if summary['ok'] else 'FAILED'}: "
                f"{summary['passed']} passed, {summary['skipped']} skipped, "
                f"{summary['failed']} failed"
            )
            return 0 if summary["ok"] else 1
        if command == "mirrors":
            print(" ".join(mirrors()))
            return 0
        print(__doc__)
        print("usage: python3 -m aios.ci {run|mirrors}")
        return 2
    except CIError as exc:
        print(f"aios.ci: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
