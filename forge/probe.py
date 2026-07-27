"""Probes: executable capability checks derived from intent.

A probe answers one question — "does the thing I said I do still work?" — and it
is the only guardrail the minimizer has. Upstream test suites are the wrong tool
here: they assert that *every* feature works, which is the opposite of the goal.

Probes run under `bash -e` in a scratch directory exported as $T.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DIR = "probes"
DEFAULT_TIMEOUT = 120


class ProbeError(Exception):
    """A probe definition is malformed or missing."""


@dataclass(frozen=True)
class Check:
    name: str
    script: str
    expect_exit: int = 0
    expect_stdout_contains: str = ""
    timeout: int = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class Probe:
    name: str
    description: str
    atoms: tuple[str, ...]
    checks: tuple[Check, ...]
    path: Path | None = field(default=None, compare=False)


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    skipped: bool = False


@dataclass
class ProbeResult:
    name: str
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]

    def summary(self) -> str:
        if not self.checks:
            return f"{self.name}: no checks"
        skipped = sum(1 for check in self.checks if check.skipped)
        passed = sum(1 for check in self.checks if check.passed)
        tail = f" ({skipped} skipped)" if skipped else ""
        verdict = "ok" if self.passed else "FAIL"
        return f"{self.name}: {verdict} {passed}/{len(self.checks)}{tail}"


def load(name: str, directory: str | Path = DEFAULT_DIR) -> Probe:
    path = Path(directory) / f"{name}.toml"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProbeError(f"no probe {name!r} at {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ProbeError(f"{path}: {exc}") from None

    checks_raw = raw.get("check", [])
    if not isinstance(checks_raw, list) or not checks_raw:
        raise ProbeError(f"{path}: at least one [[check]] is required")

    checks: list[Check] = []
    for index, item in enumerate(checks_raw):
        if not isinstance(item, dict):
            raise ProbeError(f"{path}: check[{index}] must be a table")
        script = item.get("script")
        if not isinstance(script, str) or not script.strip():
            raise ProbeError(f"{path}: check[{index}].script is required")
        checks.append(
            Check(
                name=str(item.get("name", f"check[{index}]")),
                script=script,
                expect_exit=int(item.get("expect_exit", 0)),
                expect_stdout_contains=str(item.get("expect_stdout_contains", "")),
                timeout=int(item.get("timeout", DEFAULT_TIMEOUT)),
            )
        )

    return Probe(
        name=str(raw.get("name", name)),
        description=str(raw.get("description", "")),
        atoms=tuple(raw.get("atoms", [])),
        checks=tuple(checks),
        path=path,
    )


def run(probe: Probe, *, root: str | Path = "/", dry_run: bool = False) -> ProbeResult:
    """Execute every check. `root` other than / runs them inside a chroot."""
    results: list[CheckResult] = []
    for check in probe.checks:
        if dry_run:
            results.append(
                CheckResult(name=check.name, passed=True, skipped=True, reason="dry run")
            )
            continue
        results.append(_run_check(check, root))
    return ProbeResult(name=probe.name, checks=results)


def run_all(names, *, directory: str | Path = DEFAULT_DIR, root: str | Path = "/",
            dry_run: bool = False) -> list[ProbeResult]:
    return [run(load(name, directory), root=root, dry_run=dry_run) for name in names]


def _run_check(check: Check, root: str | Path) -> CheckResult:
    in_chroot = str(root) not in ("/", "")
    base = Path(root) / "tmp" if in_chroot else None
    if base is not None:
        base.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="forge-probe-", dir=base) as scratch:
        if in_chroot:
            inner = "/tmp/" + Path(scratch).name
            argv = ["chroot", str(root), "/bin/bash", "-e", "-c", check.script]
            env = {"T": inner, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/root"}
            cwd = None
        else:
            argv = ["bash", "-e", "-c", check.script]
            env = {"T": scratch, "PATH": _path(), "HOME": scratch}
            cwd = scratch

        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=check.timeout,
                # No stdin, ever. A probe that reads is a probe that hangs: `vim -es`
                # inherits the caller's terminal and waits forever for input that is
                # never coming. The timeout would eventually fire, but a minimizer
                # doing one build per lever cannot afford to discover that per attempt.
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                name=check.name,
                passed=False,
                reason=f"timed out after {check.timeout}s",
                duration_s=time.monotonic() - started,
            )
        except FileNotFoundError as exc:
            return CheckResult(
                name=check.name, passed=False, reason=f"cannot execute: {exc}",
                duration_s=time.monotonic() - started,
            )

    duration = time.monotonic() - started
    reason = ""
    passed = True
    if completed.returncode != check.expect_exit:
        passed = False
        reason = f"exit {completed.returncode}, expected {check.expect_exit}"
    elif check.expect_stdout_contains and check.expect_stdout_contains not in completed.stdout:
        passed = False
        reason = f"stdout does not contain {check.expect_stdout_contains!r}"

    return CheckResult(
        name=check.name,
        passed=passed,
        reason=reason,
        exit_code=completed.returncode,
        stdout=completed.stdout[-4000:],
        stderr=completed.stderr[-4000:],
        duration_s=duration,
    )


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")
