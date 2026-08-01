"""Detached emerge jobs. A build has no timeout — not a long one, none.

A Gentoo package build takes tens of minutes and sometimes hours. Every mechanism
this machine had for running one was a *call* with a deadline: `run_shell` clamps to
`tools.MAX_TIMEOUT`, and raising that number only moves the cliff. The repo already
paid for that lesson — `skills/tool-budget-shorter-than-task` records an agent cut
off at 120s that escalated through backgrounding, self-killing, and finally
fabricating a fake portage tree, because a plausible artifact was the only thing that
fitted the budget. The prescribed fix in that skill is a start/poll/read pattern, so
a three-hour build is many short calls and progress is visible. This is that.

Four things make it more than a `Popen` with the output redirected.

**A job outlives everything that could kill it.** `os.setsid` puts it in a new
session with no controlling terminal, so closing the terminal, detaching tmux,
restarting the REPL or exiting the agent sends it no SIGHUP. stdin is /dev/null —
`skills/subprocess-stdin-hang` is the same fact from the other side, a build that
inherits a terminal blocks on it forever. stdout and stderr go to a file under the
state volume, so the log outlives the container that produced it.

**Exit status is recorded by the job, not by its parent.** The parent is normally
gone before emerge finishes, so nothing is left to `waitpid` and the kernel's status
is lost with the reaper. So emerge runs under a two-command `sh` wrapper that writes
the status to a file and only then exits: the status is durable, and `status()`
prefers it over anything it could infer. This is the whole point of the module. A job
runner that cannot tell success from failure after a restart is worse than no job
runner, because it reports a failed build as finished.

**Liveness does not depend on a process being alive.** Four outcomes, not two:
`running`, `exited 0`, `exited <n>`, and `vanished` — the process is gone and left no
status at all. That last one is real (OOM-killed, pod recreated, SIGKILLed) and it is
reported as an unknown exit status, never as success. Reading the process group is
the *fallback*, because a pgid can be recycled; the recorded status cannot.

**A zombie is not a running build.** Nothing waits for these jobs, so a job started by
a process that is *still alive* leaves a zombie when it dies — and a zombie answers
`kill(pgid, 0)` as though it were running (on some systems by succeeding, on others with
EPERM, which reads the same). Believed, that turns the one case liveness has to get
right — killed, with no status recorded — into "running", permanently. So a job this
process started is reaped through its own `Popen` before the kernel is asked about it.
Jobs started by some *other* process are unaffected: theirs reparent to init when their
starter exits, and init reaps them.

**Stopping means the process GROUP.** emerge spawns make, which spawns compilers.
Killing the pid leaves the tree running and the machine still loaded, so `stop`
signals `-pgid`.

Stdlib only, and it must stay that way: this runs on a node whose only interpreter
is portage's own python3. `forge.portage.emerge_argv` is the single definition of
what an emerge invocation looks like and is imported rather than restated.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

#: Where a job's registry record, log and exit status live. Under `.aios` because
#: that is the mounted state volume: a log has to survive the pod that wrote it, and
#: `status` has to keep answering after the agent process has been replaced.
STATE = ".aios"
BUILDS = "builds"

#: The audit journal, and the writer name the dashboard counts as the machine doing
#: work rather than commenting on it. Restated here rather than imported from
#: `dashboard` for one reason: the dashboard *reads this module*, so importing it back
#: would be a cycle. `aios.tools` restates the same path for the same kind of reason.
#: `test_cockpit` asserts the three agree.
JOURNAL = f"{STATE}/agent.jsonl"
AUTHOR = "agent"
KIND_STARTED = "build_started"
KIND_EXITED = "build_exited"

#: The four outcomes. `VANISHED` is not a synonym for failure and not a synonym for
#: success: it is "no status was recorded and the process is gone", which is what an
#: OOM kill or a recreated pod looks like from here. `UNKNOWN` is about the *job* —
#: this node has never heard of that id.
RUNNING = "running"
EXITED = "exited"
VANISHED = "vanished"
UNKNOWN = "unknown"

#: Small on purpose. A build log is executed package code's stdout and it is
#: megabytes long; the interesting part is the end, and a default that ships the
#: whole thing into a model's context is the output-cap half of the same mistake the
#: timeout half of this module exists to fix.
DEFAULT_TAIL_LINES = 40
MAX_TAIL_LINES = 400

#: How much of the log's tail is read off disk to find those lines. Bounded because
#: the file is appended to while it is read and can be arbitrarily large.
TAIL_BYTES = 256 * 1024

#: SIGTERM, then this long, then SIGKILL. emerge cleans up on SIGTERM — unmerging
#: half-installed files, releasing the portage lock — so it is worth waiting for, but
#: not worth blocking a tool call on indefinitely.
STOP_GRACE_S = 3.0
STOP_POLL_S = 0.05

#: A job id, as this module writes it: sortable, and strict enough to be pasted into
#: a path. The model supplies this string, so it is matched rather than trusted —
#: `../../etc/passwd` is a perfectly good filename fragment.
JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")

#: What may be handed to emerge as a PACKAGE. The first character is the load-bearing
#: part: emerge's own options start with `-`, and `-C` is *unmerge*, so an "atom"
#: composed out of a build log could uninstall the toolchain. Everything a real atom
#: needs is allowed (`@aios`, `app-editors/vim`, `>=dev-vcs/git-2.4`,
#: `sys-devel/gcc:13`, `app-editors/vim[python]`); nothing that reads as a flag is.
ATOM_RE = re.compile(r"^[@A-Za-z0-9<>=~][A-Za-z0-9@/._+:<>=~*\[\],-]*$")
MAX_ATOM = 200

#: A binhost is a URL portage will hand to wget. Validated rather than trusted for
#: the same reason `forge.portage.distcc_host` validates a peer name: it can arrive
#: from a place this machine does not control.
BINHOST_RE = re.compile(r"^https?://[A-Za-z0-9._~%:@\[\]/-]+$")

#: What actually runs. Two commands rather than one, deliberately: an `exec` here
#: would replace the shell with emerge and there would be nothing left to record the
#: status — which is the defect this module exists to fix. The write is
#: temp-then-rename inside one directory, so a reader never sees half a status.
_WRAPPER = (
    "exitfile=$1; shift\n"
    '"$@"\n'
    "status=$?\n"
    'printf %s "$status" > "$exitfile.part" 2>/dev/null'
    ' && mv -f "$exitfile.part" "$exitfile" 2>/dev/null\n'
    'exit "$status"\n'
)


class BuildError(Exception):
    """Message is user-facing, and reaches the model verbatim."""


class _Detached(subprocess.Popen):
    """A child that is MEANT to outlive the object representing it.

    `Popen.__del__` warns when it is collected while its child still runs, because the
    usual cause is a forgotten `wait()`. Here it is the entire feature — the job is in
    its own session, it reparents to init when this process exits, and its exit status is
    on disk precisely because no `waitpid` is left to make. So the warning would fire on
    every single build to describe correct behaviour, and a warning that cries during
    ordinary operation is one nobody reads. Overridden here rather than filtered
    globally, so it is these objects and nothing else that stay quiet.
    """

    def __del__(self, *args: object, **kwargs: object) -> None:
        return


#: Jobs *this* process started, by pid. The reference keeps the `Popen` from being
#: collected, and the `Popen` is the only thing that can reap our own child — see
#: `_alive`. One small entry per build, so a session's worth is a handful.
_OWN: dict[int, subprocess.Popen] = {}


# --- the job ------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """One detached build, exactly as its registry record spells it."""

    id: str
    atoms: tuple[str, ...]
    argv: tuple[str, ...]
    pid: int
    pgid: int
    started: float
    log: str
    exit_file: str
    #: When `stop` signalled it. A stopped job records no exit status — the wrapper
    #: is inside the group being killed — so this is the only evidence of why it
    #: vanished, and without it a deliberate stop reads like an OOM kill.
    stopped: float = 0.0

    @property
    def what(self) -> str:
        """What this build is *of*, in the words emerge was given."""
        return " ".join(self.atoms) or "@aios"

    def record(self) -> dict:
        return {
            "id": self.id,
            "atoms": list(self.atoms),
            "argv": list(self.argv),
            "pid": self.pid,
            "pgid": self.pgid,
            "started": self.started,
            "log": self.log,
            "exit_file": self.exit_file,
            "stopped": self.stopped,
        }


def _job(record: object) -> Job | None:
    """A registry record as a Job, or None if it is not one.

    Every field is coerced rather than trusted: this file is on a mounted volume that
    outlives the process that wrote it, so a truncated or hand-edited record is a
    normal input and must cost one job rather than the whole listing.
    """
    if not isinstance(record, dict):
        return None
    job_id = str(record.get("id") or "")
    if not JOB_ID_RE.match(job_id):
        return None
    return Job(
        id=job_id,
        atoms=tuple(str(a) for a in _list(record.get("atoms"))),
        argv=tuple(str(a) for a in _list(record.get("argv"))),
        pid=_int(record.get("pid")),
        pgid=_int(record.get("pgid")),
        started=_float(record.get("started")),
        log=str(record.get("log") or ""),
        exit_file=str(record.get("exit_file") or ""),
        stopped=_float(record.get("stopped")),
    )


def _list(value: object) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class Status:
    """What is true about a job right now, and how confident we are of it."""

    state: str
    job: Job | None = None
    code: int | None = None
    #: Wall time so far while running; the whole run once it has ended. Measured from
    #: the exit file's mtime rather than from `now`, so a finished build still reports
    #: how long it took after the agent that started it has been replaced.
    elapsed: float = 0.0
    finished: float = 0.0
    detail: str = ""
    #: The last line of the log, for a display. Filled only where it was asked for.
    last: str = ""

    @property
    def running(self) -> bool:
        return self.state == RUNNING

    @property
    def ok(self) -> bool | None:
        """True, False, or None for "nobody knows" — never False-y for unknown.

        `if status.ok:` and `if not status.ok:` are both wrong about a vanished job,
        which is exactly why this is tri-state: the failure mode being designed
        against is a missing status being read as a green build.
        """
        return None if self.state != EXITED else self.code == 0

    @property
    def exit_text(self) -> str:
        return "unknown" if self.code is None else str(self.code)

    def line(self) -> str:
        """One or two lines a model can act on without reading anything else."""
        if self.job is None:
            return f"{self.state}: {self.detail}"
        head = f"{self.job.id}  {self.job.what}"
        if self.state == RUNNING:
            return (
                f"{head}\n  running for {duration(self.elapsed)} — there is no time "
                f"limit; poll build_status again later, or read build_tail"
            )
        if self.state == EXITED:
            verdict = "succeeded" if self.code == 0 else "FAILED"
            return f"{head}\n  exited {self.code} ({verdict}) after {duration(self.elapsed)}"
        why = self.detail or (
            "the process is gone and recorded no status — OOM-killed, or the node "
            "was replaced under it"
        )
        return (
            f"{head}\n  vanished after {duration(self.elapsed)}, exit status unknown: "
            f"{why}. This is NOT a success — nothing proved the build finished"
        )


def duration(seconds: float) -> str:
    """The same rendering as `dashboard.ago`, restated because that module reads this
    one and the import would be a cycle. `test_build` asserts the two agree."""
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {total % 3600 // 60:02d}m"


# --- where it all lives -------------------------------------------------------


def _base(root: Path | str | None = None) -> Path:
    return Path(root if root is not None else os.environ.get("AIOS_ROOT", "."))


def registry(root: Path | str | None = None) -> Path:
    return _base(root) / STATE / BUILDS


def log_path(job_id: str, root: Path | str | None = None) -> Path:
    return registry(root) / f"{_check_id(job_id)}.log"


def _exit_path(job_id: str, root: Path | str | None = None) -> Path:
    return registry(root) / f"{_check_id(job_id)}.exit"


def _record_path(job_id: str, root: Path | str | None = None) -> Path:
    return registry(root) / f"{_check_id(job_id)}.json"


def _check_id(job_id: str) -> str:
    if not JOB_ID_RE.match(str(job_id)):
        raise BuildError(f"{job_id!r} is not a build id (they look like 20260801-141233-9f2a)")
    return str(job_id)


def _write_atomic(path: Path, text: str) -> None:
    """Write-temp-then-rename, in the target's own directory.

    The dashboard reads these records every two seconds while a build writes them, so
    a reader must never see a half-written record. Same directory on purpose:
    `skills/rename-fails-cross-device` is what happens when a rename crosses one.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _save(job: Job, root: Path | str | None = None) -> None:
    _write_atomic(_record_path(job.id, root), json.dumps(job.record(), sort_keys=True) + "\n")


def load(job_id: str, root: Path | str | None = None) -> Job | None:
    """One job's record, freshly read. There is no cache, deliberately: the process
    that answers `status` is usually not the process that started the build."""
    try:
        raw = _record_path(job_id, root).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return _job(json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        return None


def jobs(root: Path | str | None = None) -> list[Job]:
    """Every job this node knows about, newest first."""
    try:
        paths = sorted(registry(root).glob("*.json"))
    except OSError:
        return []
    found = []
    for path in paths:
        try:
            job = _job(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if job is not None:
            found.append(job)
    return sorted(found, key=lambda job: (-job.started, job.id))


# --- starting -----------------------------------------------------------------


def check_atom(raw: object) -> str:
    atom = str(raw).strip()
    if not atom:
        raise BuildError("an empty package atom")
    if len(atom) > MAX_ATOM:
        raise BuildError(f"package atom is {len(atom)} characters long — refused")
    if not ATOM_RE.match(atom):
        raise BuildError(
            f"{atom!r} is not a package atom. emerge's own options begin with `-` "
            "(and `-C` is unmerge), so anything shaped like a flag is refused here — "
            "pass packages such as app-editors/vim, >=dev-vcs/git-2.4 or @aios"
        )
    return atom


def check_binhost(raw: object) -> str:
    url = str(raw).strip()
    if url and not BINHOST_RE.match(url):
        raise BuildError(
            f"{url!r} is not a binhost URL — expected http://host:port/binpkgs"
        )
    return url


def start(
    atoms: Sequence[str] = (),
    *,
    binhost: str = "",
    distcc: bool = False,
    oneshot: bool = False,
    buildpkg: bool = True,
    root: Path | str | None = None,
    emerge_root: str = "/",
    lock: dict | None = None,
    lock_path: str = "aios.lock.json",
    argv: Sequence[str] | None = None,
    now: float | None = None,
) -> Job:
    """Spawn a build detached and return immediately.

    Returns as soon as the process exists — it does not wait, and there is no timeout
    to wait *for*. The job survives this process exiting, the tmux client detaching
    and the terminal closing; `status`, `tail` and `stop` are how you find out what
    happened to it, in as many short calls as it takes.

    `argv` runs a command of the caller's choosing instead of composing an emerge.
    That is what makes the runner testable without a Gentoo — and the flags for a real
    build are `forge.portage.emerge_argv`'s to decide, never this module's.
    """
    now = time.time() if now is None else now
    names = tuple(check_atom(atom) for atom in atoms if str(atom).strip())
    binhost = check_binhost(binhost)
    command = tuple(str(part) for part in argv) if argv is not None else _emerge_argv(
        names, emerge_root=emerge_root, binhost=bool(binhost), oneshot=oneshot,
        lock=lock, lock_path=lock_path, root=root,
    )
    if not command:
        raise BuildError("nothing to run")
    if shutil.which(command[0]) is None:
        raise BuildError(
            f"{command[0]} is not on PATH — this node cannot build yet. Would have run:"
            f"\n  {' '.join(command)}"
        )

    directory = registry(root)
    directory.mkdir(parents=True, exist_ok=True)
    job_id = _new_id(now)
    log, exit_file = log_path(job_id, root), _exit_path(job_id, root)

    # Opened append-binary and handed to the child, so both the header below and
    # everything the build prints land in one file in order, and an appending writer
    # never truncates what a reader is part-way through.
    with open(os.devnull, "rb") as null, log.open("ab") as out:
        header = (
            f"# aios build {job_id}  started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}\n"
            f"# {' '.join(command)}\n"
        )
        out.write(header.encode("utf-8"))
        out.flush()
        process = _Detached(
            ["sh", "-c", _WRAPPER, "sh", str(exit_file), *command],
            cwd=str(_base(root)),
            env=_env(binhost=binhost, distcc=distcc, buildpkg=buildpkg),
            stdin=null,
            stdout=out,
            stderr=subprocess.STDOUT,
            # A new session: no controlling terminal, so no SIGHUP when the terminal
            # that started it goes away, and its own process group to kill.
            start_new_session=True,
            close_fds=True,
        )

    _OWN[process.pid] = process
    job = Job(
        id=job_id,
        atoms=names,
        argv=command,
        pid=process.pid,
        pgid=_pgid(process.pid),
        started=now,
        log=str(log),
        exit_file=str(exit_file),
    )
    _save(job, root)
    _journal(
        root,
        KIND_STARTED,
        {"job": job.id, "atoms": list(job.atoms), "log": job.log, "pid": job.pid},
    )
    return job


def _new_id(now: float) -> str:
    """Sortable, and unique enough that two builds started in the same second do not
    collide — the registry is one flat directory and a reused id would overwrite a log."""
    return f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{os.urandom(2).hex()}"


def _pgid(pid: int) -> int:
    """`start_new_session` makes this equal to the pid; asked rather than assumed
    because it is what `stop` signals and guessing it wrong kills the wrong tree."""
    try:
        return os.getpgid(pid)
    except OSError:
        return pid


def _emerge_argv(
    atoms: Sequence[str],
    *,
    emerge_root: str,
    binhost: bool,
    oneshot: bool,
    lock: dict | None,
    lock_path: str,
    root: Path | str | None,
) -> tuple[str, ...]:
    """The emerge invocation, from the one place that defines what those flags are.

    Imported here rather than at module scope so `aios.dashboard` — which reads this
    module on every status redraw — does not pay for the whole forge package.
    """
    from forge import lock as lock_mod
    from forge import portage as portage_mod

    if lock is None:
        path = Path(lock_path)
        if not path.is_absolute():
            path = _base(root) / path
        try:
            lock = lock_mod.load(path)
        except (lock_mod.LockError, OSError) as exc:
            raise BuildError(f"cannot read the lockfile: {exc}") from None
    return tuple(
        portage_mod.emerge_argv(
            lock,
            root=emerge_root,
            # Never. A detached job exists to do work; a plan is a thing you read now.
            pretend=False,
            atoms=list(atoms) or None,
            binhost=binhost,
            # `deep=False, oneshot=True` is the minimizer's shape and portage.py says
            # why; the two travel together, so neither is restated as a separate knob.
            deep=not oneshot,
            oneshot=oneshot,
        )
    )


def _env(*, binhost: str, distcc: bool, buildpkg: bool) -> dict[str, str]:
    """The environment a build runs in — accelerators only, never policy.

    Which peers happen to be up is a fact about this moment rather than about the
    machine being built, which is why neither PORTAGE_BINHOST nor DISTCC_HOSTS may
    enter the lockfile and both arrive here instead.

    FEATURES is one of portage's *incremental* variables: tokens set in the
    environment stack onto make.conf's value rather than replacing it, which is how a
    negation from `binhost_env` and the additions below can share one string.
    """
    from forge import portage as portage_mod

    overrides: dict[str, str] = {}
    features: list[str] = []
    if binhost:
        extra = dict(portage_mod.binhost_env(binhost))
        features.append(extra.pop("FEATURES", ""))
        overrides.update(extra)
    if distcc:
        overrides.update(portage_mod.distcc_env(_distcc_hosts()))
    if buildpkg:
        # Load-bearing rather than tuning: `forge minimize` rebuilds one package once
        # per lever, and without a binary package cache every attempt pays full price.
        features += ["buildpkg", "binpkg-multi-instance"]
    if features:
        overrides["FEATURES"] = " ".join(token for token in features if token)
    return {**os.environ, **overrides}


def _distcc_hosts() -> list[str]:
    """The mesh's compile hosts, or none.

    Broad on purpose: no mesh, no token, no network is a *local* build rather than a
    failed one, and a build that refused to start because the accelerator was absent
    would be strictly worse than a slower build.
    """
    try:
        from . import mesh as mesh_mod

        return list(mesh_mod.distcc_hosts(mesh_mod.look()))
    except Exception:
        return []


# --- polling ------------------------------------------------------------------


def status(
    job_id: str,
    *,
    root: Path | str | None = None,
    now: float | None = None,
    record: bool = True,
) -> Status:
    """What became of one job. Cheap enough to call in a loop.

    The recorded exit status wins over everything else, because it is the only signal
    that survives the process. Only when there is none does this ask whether the
    process group still exists — and a group that is gone with no status recorded is
    reported as `vanished`, never as a build that finished.

    `record=False` for a display. `status` journals the exit transition the first time
    it observes one, and a monitor that appends to the journal it reads certifies its
    own subject alive — `skills/monitor-writes-what-it-reads` is that bug.
    """
    job = load(job_id, root)
    if job is None:
        return Status(
            UNKNOWN,
            detail=f"no build {str(job_id)!r} on this node — build_status with no id lists them",
        )
    now = time.time() if now is None else now
    code, when, note = _recorded_exit(job)
    if code is not None:
        if record:
            _record_exit(job, code, when, root)
        return Status(
            EXITED, job=job, code=code,
            elapsed=max(0.0, when - job.started), finished=when, detail=note,
        )
    if _alive(job.pgid, job.pid):
        return Status(RUNNING, job=job, elapsed=max(0.0, now - job.started), detail=note)

    ended = when or job.stopped or now
    detail = note
    if job.stopped:
        detail = detail or (
            f"stopped on request {duration(max(0.0, now - job.stopped))} ago, so the "
            "wrapper that records the status was killed with it"
        )
    return Status(
        VANISHED, job=job, elapsed=max(0.0, ended - job.started), finished=when, detail=detail,
    )


def report(
    root: Path | str | None = None,
    *,
    now: float | None = None,
    record: bool = False,
    last_line: bool = False,
) -> list[Status]:
    """Every job's status, newest first. `record=False` because the dashboard calls it."""
    now = time.time() if now is None else now
    out = []
    for job in jobs(root):
        state = status(job.id, root=root, now=now, record=record)
        if last_line and state.running:
            state = replace(state, last=_last_line(Path(job.log)))
        out.append(state)
    return out


def _recorded_exit(job: Job) -> tuple[int | None, float, str]:
    """The status the job wrote for itself: (code, when, complaint)."""
    path = Path(job.exit_file)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        when = path.stat().st_mtime
    except OSError:
        return None, 0.0, ""
    try:
        return int(raw), when, ""
    except ValueError:
        # The file exists and says something that is not a status. Do not invent one:
        # fall through to liveness, and say why the answer is weaker than it looks.
        return None, when, f"the recorded exit status reads {raw[:40]!r}, which is not a number"


def _alive(pgid: int, pid: int = 0) -> bool:
    """Does this job's process group still exist?

    The weaker of the two signals and used only as a fallback: a pgid is recycled
    eventually, so a long-dead job whose number came round again would read as
    running. The recorded status has no such failure mode, which is why it is
    consulted first.

    Our own children are reaped before the kernel is asked, because an unreaped child
    is a zombie and a zombie answers this question wrongly in both directions available
    to it: `killpg` either succeeds or returns EPERM, and EPERM has to be read as "alive
    but not ours" for the case where it genuinely is. Nothing waits for these jobs, so
    every build started by a still-running process would otherwise read as running for
    as long as that process lives — including the one case that must not be got wrong, a
    build killed with no status recorded.
    """
    if pgid <= 1:
        return False
    if _reaped(pid):
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it is simply not ours to signal
    except OSError:
        return False
    return True


def _reaped(pid: int) -> bool:
    """True if `pid` is a child of ours that has ended and has now been waited for.

    False for anything else, which covers every job started by another process: theirs
    is not ours to wait for, it reparented to init when its starter exited, and init has
    already reaped it — so the kernel's answer about it is the truthful one.
    """
    own = _OWN.get(pid)
    if own is None:
        return False
    if own.poll() is None:
        return False
    _OWN.pop(pid, None)
    return True


def tail(
    job_id: str, lines: int = DEFAULT_TAIL_LINES, *, root: Path | str | None = None
) -> str:
    """The end of a job's log. Reads the file's tail, not the file."""
    job = load(job_id, root)
    if job is None:
        raise BuildError(f"no build {str(job_id)!r} on this node")
    want = max(1, min(int(lines or DEFAULT_TAIL_LINES), MAX_TAIL_LINES))
    return read_tail(Path(job.log), want)


def read_tail(path: Path, lines: int, limit: int = TAIL_BYTES) -> str:
    """The last `lines` whole lines of a file that is being appended to.

    Bounded read, and the first line of the window is dropped when the seek landed
    inside a record — the same shape `dashboard._tail` uses on the journal, for the
    same reason: this file grows without limit and is written while it is read.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - limit)
            handle.seek(start)
            blob = handle.read()
    except FileNotFoundError:
        return "(no log yet)"
    except OSError as exc:
        return f"(log unreadable: {type(exc).__name__})"
    if not blob:
        return "(log is empty)"
    found = blob.decode("utf-8", "replace").splitlines()
    if start and found:
        found.pop(0)
    return "\n".join(found[-lines:]) or "(log is empty)"


def _last_line(path: Path) -> str:
    """The newest line of a log, for a one-line display. Never the header if there
    is anything after it — a build that has printed nothing has nothing to show."""
    text = read_tail(path, 2, limit=8192)
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


# --- stopping -----------------------------------------------------------------


def stop(job_id: str, *, root: Path | str | None = None) -> str:
    """Terminate the process GROUP: emerge, the make it spawned, and the compilers.

    Killing the pid alone leaves that tree running — still holding the portage lock,
    still loading the machine — while every reader reports the build as gone.
    """
    state = status(job_id, root=root)
    job = state.job
    if job is None:
        raise BuildError(state.detail)
    if state.state == EXITED:
        return f"{job.id} had already exited {job and state.code} — nothing to stop"

    pgid = job.pgid
    if pgid <= 1 or pgid == os.getpgid(0):
        # Our own group means the recorded pgid is wrong or has been recycled onto
        # this very process. Signalling it would kill the agent asking the question.
        raise BuildError(
            f"refusing to signal process group {pgid}: it is not a group this build owns"
        )
    if not _alive(pgid, job.pid):
        _save(replace(job, stopped=job.stopped or time.time()), root)
        return f"{job.id} was already gone; no exit status was ever recorded"

    os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_GRACE_S
    while _alive(pgid, job.pid) and time.monotonic() < deadline:
        time.sleep(STOP_POLL_S)
    hard = _alive(pgid, job.pid)
    if hard:
        os.killpg(pgid, signal.SIGKILL)
        # Same short wait again: SIGKILL is not refusable, but the reap is not instant
        # and a caller that immediately polls should not be told the job is running.
        deadline = time.monotonic() + STOP_GRACE_S
        while _alive(pgid, job.pid) and time.monotonic() < deadline:
            time.sleep(STOP_POLL_S)

    when = time.time()
    _save(replace(job, stopped=when), root)
    if _claim_exit(job, root):
        _journal(
            root,
            KIND_EXITED,
            {
                "job": job.id, "atoms": list(job.atoms), "code": None, "stopped": True,
                "elapsed_s": round(max(0.0, when - job.started), 3),
            },
        )
    how = "SIGTERM, then SIGKILL" if hard else "SIGTERM"
    return (
        f"stopped {job.id} ({job.what}) with {how} to process group {pgid} after "
        f"{duration(max(0.0, when - job.started))}. No exit status was recorded — a "
        "stopped build is not a failed one and not a finished one"
    )


# --- the audit trail ----------------------------------------------------------


def _claim_exit(job: Job, root: Path | str | None) -> bool:
    """Take the right to journal this job's ending, once, across processes.

    O_EXCL rather than a flag inside the registry record: several pollers can observe
    the same exit at the same moment, and read-modify-write would let all of them
    think they were first.
    """
    marker = registry(root) / f"{job.id}.logged"
    try:
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        return False
    except OSError:
        return False
    return True


def _record_exit(job: Job, code: int, when: float, root: Path | str | None) -> None:
    if not _claim_exit(job, root):
        return
    _journal(
        root,
        KIND_EXITED,
        {
            "job": job.id,
            "atoms": list(job.atoms),
            "code": code,
            "elapsed_s": round(max(0.0, when - job.started), 3),
        },
    )


def _journal(root: Path | str | None, kind: str, payload: dict) -> None:
    """Append one transition to the audit journal, the way `agent._record` does.

    `author` last, after the payload, for the same reason it is last there: it is what
    the dashboard counts as the machine doing work, so no field of a record may rename
    the writer of that record.
    """
    record = {"ts": time.time(), "kind": kind, **payload, "author": AUTHOR}
    path = _base(root) / JOURNAL
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except OSError:
        # Losing an audit line must not fail a build that is already running, or take
        # down the poll that noticed it finished.
        pass


# --- entry point --------------------------------------------------------------

USAGE = "usage: python3 -m aios.build list|status <id>|tail <id> [lines]|stop <id>"


def main(argv: list[str] | None = None) -> int:
    """Enough of a surface to inspect the registry by hand. Starting a build is
    `forge build --detach --execute`, so there is one way to do it and not two.

    These polls DO record the exit transition, unlike `aios.dashboard`'s. The
    distinction is not who is asking but how often: this is a person or a script running
    one command on purpose, and the ending has to reach the audit journal the first time
    anybody deliberately looks — otherwise a build started from a shell and polled from a
    shell finishes with nothing written down. The display is the one caller that must not
    write, because it reads the same file every two seconds and would be certifying its
    own subject alive (`skills/monitor-writes-what-it-reads`).
    """
    import sys

    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "list"
    try:
        if command == "list":
            found = report(record=True)
            for state in found:
                print(state.line())
            if not found:
                print("no builds on this node")
        elif command == "status" and len(argv) > 1:
            print(status(argv[1]).line())
        elif command == "tail" and len(argv) > 1:
            print(tail(argv[1], int(argv[2]) if len(argv) > 2 else DEFAULT_TAIL_LINES))
        elif command == "stop" and len(argv) > 1:
            print(stop(argv[1]))
        else:
            print(__doc__)
            print(USAGE)
            return 2
    except BuildError as exc:
        print(f"aios.build: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
