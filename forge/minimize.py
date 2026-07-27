"""Feature minimization as a search problem.

    levers     enabled USE flags, then EXTRA_ECONF flags, then patches
    objective  installed size of the package's own files
    constraint every probe bound to the package still passes

Greedy, one lever at a time, in sorted order. Greedy is the right starting point:
O(n) rebuilds, resumable, and each step is independently explainable in the
journal. Delta-debugging over lever subsets is a later optimization and only pays
for itself once binary packages are cached.

Every attempt is a real build. That cost is the reason FEATURES=buildpkg and a
local binhost are a phase-1 requirement rather than a nicety.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import lock as lock_mod
from . import portage as portage_mod
from . import probe as probe_mod

JOURNAL = "forge.journal.jsonl"


class MinimizeError(Exception):
    pass


@dataclass
class BuildResult:
    ok: bool
    duration_s: float
    log: str = ""


@dataclass
class Attempt:
    lever: str
    verdict: str  # accepted | rejected-build | rejected-probe | rejected-size
    size_before: int
    size_after: int | None
    duration_s: float
    detail: str = ""

    @property
    def delta(self) -> int | None:
        return None if self.size_after is None else self.size_after - self.size_before


@dataclass
class MinimizeResult:
    atom: str
    baseline_size: int
    final_size: int
    dropped: list[str] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    runner: str = ""
    simulated: bool = False

    @property
    def saved(self) -> int:
        return self.baseline_size - self.final_size

    def summary(self) -> str:
        note = "  [simulated]" if self.simulated else ""
        if not self.dropped:
            return f"{self.atom}: nothing droppable ({len(self.attempts)} attempts){note}"
        return (
            f"{self.atom}: dropped {len(self.dropped)} of {len(self.attempts)} levers, "
            f"{_human(self.baseline_size)} -> {_human(self.final_size)} "
            f"(-{_human(self.saved)}){note}"
        )


class Runner(Protocol):
    """Build and measure. The seam between the search and the actual machine."""

    name: str
    simulated: bool

    def build(self, atom: str, disabled: list[str]) -> BuildResult: ...
    def installed_size(self, atom: str) -> int: ...


class DryRunner:
    """No builds, no probes. Exercises the search's control flow only.

    Sizes are a linear function of the levers dropped, so every candidate looks
    like a win. That makes the loop testable while being obviously fake — it must
    never be mistaken for a measurement.
    """

    def __init__(self, base_size: int = 4_194_304, per_lever: int = 65_536) -> None:
        self.name = "dry"
        self.simulated = True
        self.base_size = base_size
        self.per_lever = per_lever
        self._disabled: list[str] = []

    def build(self, atom: str, disabled: list[str]) -> BuildResult:
        self._disabled = list(disabled)
        return BuildResult(ok=True, duration_s=0.0, log="dry run: no build performed")

    def installed_size(self, atom: str) -> int:
        return max(0, self.base_size - self.per_lever * len(self._disabled))


class LocalRunner:
    """Real builds via emerge into `root`, sizes from portage's own bookkeeping."""

    TRIAL_FILE = "forge-trial"

    def __init__(self, lock: dict, root: str | Path = "/", *, log_dir: str | Path = ".forge/logs") -> None:
        self.name = f"local(root={root})"
        self.simulated = False
        self.lock = lock
        self.root = Path(root)
        self.log_dir = Path(log_dir)
        if shutil.which("emerge") is None:
            raise MinimizeError(
                "emerge is not on PATH — LocalRunner needs a Gentoo host or chroot. "
                "Use --dry-run to exercise the loop without building."
            )

    def build(self, atom: str, disabled: list[str]) -> BuildResult:
        self._write_trial(atom, disabled)
        argv = portage_mod.emerge_argv(self.lock, root=self.root, atoms=[atom])
        started = time.monotonic()
        completed = subprocess.run(argv, capture_output=True, text=True)
        duration = time.monotonic() - started
        self._save_log(atom, disabled, completed.stdout + completed.stderr)
        return BuildResult(
            ok=completed.returncode == 0,
            duration_s=duration,
            log=(completed.stdout + completed.stderr)[-4000:],
        )

    def installed_size(self, atom: str) -> int:
        """Sum the package's own installed files, from its vdb CONTENTS."""
        category, name = atom.split("/", 1)
        vdb = self.root / "var" / "db" / "pkg" / category
        if not vdb.is_dir():
            raise MinimizeError(f"{atom} is not installed under {self.root} (no vdb entry)")
        total = 0
        counted = False
        for entry in sorted(vdb.iterdir()):
            if not (entry.name == name or entry.name.startswith(f"{name}-")):
                continue
            contents = entry / "CONTENTS"
            if not contents.is_file():
                continue
            counted = True
            for line in contents.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "obj":
                    target = self.root / parts[1].lstrip("/")
                    try:
                        total += target.stat().st_size
                    except OSError:
                        pass
        if not counted:
            raise MinimizeError(f"{atom} has no CONTENTS under {vdb}")
        return total

    def _write_trial(self, atom: str, disabled: list[str]) -> None:
        path = self.root / "etc" / "portage" / "package.use" / self.TRIAL_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        if not disabled:
            path.write_text("# forge: no trial overrides\n", encoding="utf-8")
            return
        flags = " ".join(f"-{flag}" for flag in disabled)
        path.write_text(
            "# forge minimize trial — transient, overwritten every attempt\n"
            f"{atom} {flags}\n",
            encoding="utf-8",
        )

    def clear_trial(self) -> None:
        path = self.root / "etc" / "portage" / "package.use" / self.TRIAL_FILE
        if path.exists():
            path.unlink()

    def _save_log(self, atom: str, disabled: list[str], text: str) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        slug = atom.replace("/", "_") + ("--" + "-".join(disabled) if disabled else "--baseline")
        (self.log_dir / f"{slug}.log").write_text(text, encoding="utf-8")


def minimize(
    lock: dict,
    atom: str,
    runner: Runner,
    *,
    probe_dir: str | Path = probe_mod.DEFAULT_DIR,
    root: str | Path = "/",
    journal: str | Path = JOURNAL,
    levers: list[str] | None = None,
) -> MinimizeResult:
    """Drop every lever that keeps the probes green without growing the package."""
    package = lock_mod.package(lock, atom)
    probe_names = package["probes"]
    if not probe_names and not runner.simulated:
        raise MinimizeError(
            f"{atom} has no probes bound to it — there is nothing to hold constant, so "
            "minimizing it would only be guessing. Add a probe and name it from the "
            "intent that needs this package."
        )

    candidates = sorted(levers) if levers is not None else sorted(lock_mod.enabled_use(lock, atom))
    journal_path = Path(journal)

    disabled: list[str] = []
    baseline = _measure(
        runner, atom, disabled, probe_names, probe_dir, root, journal_path, "baseline"
    )
    if baseline is None:
        raise MinimizeError(
            f"{atom} fails its own probes before minimization starts — fix the baseline "
            "(or the probe) first; a red baseline makes every later result meaningless."
        )

    result = MinimizeResult(
        atom=atom,
        baseline_size=baseline,
        final_size=baseline,
        runner=runner.name,
        simulated=runner.simulated,
    )

    for lever in candidates:
        trial = sorted(disabled + [lever])
        started = time.monotonic()

        build = runner.build(atom, trial)
        if not build.ok:
            result.attempts.append(
                Attempt(lever, "rejected-build", result.final_size, None,
                        time.monotonic() - started, "build failed")
            )
            _journal(journal_path, atom, result.attempts[-1], trial)
            continue

        probes = probe_mod.run_all(
            probe_names, directory=probe_dir, root=root, dry_run=runner.simulated
        )
        failed = [f"{p.name}/{c.name}" for p in probes for c in p.failures]
        if failed:
            result.attempts.append(
                Attempt(lever, "rejected-probe", result.final_size, None,
                        time.monotonic() - started, "probes failed: " + ", ".join(failed))
            )
            _journal(journal_path, atom, result.attempts[-1], trial)
            continue

        size = runner.installed_size(atom)
        if size > result.final_size:
            result.attempts.append(
                Attempt(lever, "rejected-size", result.final_size, size,
                        time.monotonic() - started, "package grew")
            )
            _journal(journal_path, atom, result.attempts[-1], trial)
            continue

        disabled = trial
        result.dropped = list(trial)
        result.attempts.append(
            Attempt(lever, "accepted", result.final_size, size, time.monotonic() - started)
        )
        _journal(journal_path, atom, result.attempts[-1], trial)
        result.final_size = size

    # Leave the machine on the winning configuration, not the last trial.
    if result.dropped != disabled:
        runner.build(atom, result.dropped)
    return result


def apply(lock: dict, result: MinimizeResult) -> dict:
    """Fold a minimization result back into the lockfile.

    The lock stays the single source of truth: dropped levers become ordinary
    disabled USE flags whose `why` records the measurement that justified them.
    """
    package = lock_mod.package(lock, result.atom)
    flags = {flag["flag"]: flag for flag in package["use"]}
    for lever in result.dropped:
        why = (
            f"minimize: dropped, probes green, -{_human(result.saved)}"
            + (" (simulated)" if result.simulated else "")
        )
        flags[lever] = {"flag": lever, "enabled": False, "why": why}
    package["use"] = sorted(flags.values(), key=lambda f: f["flag"])

    lock.setdefault("minimized", {})[result.atom] = {
        "dropped": list(result.dropped),
        "size_before": result.baseline_size,
        "size_after": result.final_size,
        "probes": list(package["probes"]),
        "runner": result.runner,
        "simulated": result.simulated,
    }
    return lock_mod.stamp(lock)


def _measure(runner, atom, disabled, probe_names, probe_dir, root, journal_path, label):
    started = time.monotonic()
    build = runner.build(atom, disabled)
    if not build.ok:
        _journal(
            journal_path, atom,
            Attempt(label, "rejected-build", 0, None, time.monotonic() - started, "build failed"),
            disabled,
        )
        return None
    probes = probe_mod.run_all(
        probe_names, directory=probe_dir, root=root, dry_run=runner.simulated
    )
    if any(not p.passed for p in probes):
        failed = [f"{p.name}/{c.name}" for p in probes for c in p.failures]
        _journal(
            journal_path, atom,
            Attempt(label, "rejected-probe", 0, None, time.monotonic() - started,
                    "probes failed: " + ", ".join(failed)),
            disabled,
        )
        return None
    size = runner.installed_size(atom)
    _journal(
        journal_path, atom,
        Attempt(label, "accepted", size, size, time.monotonic() - started), disabled,
    )
    return size


def _journal(path: Path, atom: str, attempt: Attempt, disabled: list[str]) -> None:
    record = {
        "atom": atom,
        "lever": attempt.lever,
        "verdict": attempt.verdict,
        "disabled": list(disabled),
        "size_before": attempt.size_before,
        "size_after": attempt.size_after,
        "delta": attempt.delta,
        "duration_s": round(attempt.duration_s, 3),
        "detail": attempt.detail,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024 or unit == "GiB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GiB"
