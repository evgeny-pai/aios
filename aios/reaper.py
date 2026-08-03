"""What PID 1 does after the boot sequence: reap, and stay reapable.

`aios-init` used to finish with `exec tail -f /dev/null`, which is a PID 1 that never
calls wait(). Linux reparents every orphan to PID 1, and reaping what has been
reparented to you is the thing that makes a process an init rather than merely the first
one. Without it an orphan dies, becomes a zombie — a process-table entry with no
resources but a still-valid PID — and stays one for the life of the pod. That is why
`kill(pid, 0)` reports an exited process as alive, and why a long-uptime node
accumulates zombies with no single command ever visibly failing. See
skills/pid1-does-not-reap-orphans, which describes the symptom and says the reaper
itself is separate work; this is that work.

Python rather than tini or dumb-init deliberately: python3 is already the one
interpreter this userland is guaranteed to have (portage requires it), so this adds no
package, no spec change and no lockfile churn to fix a defect in the boot path. The
whole job is one blocking syscall in a loop.

It also installs signal handlers, which is not incidental. PID 1 is exempt from the
default action of every signal — the kernel will not kill it for a signal it has no
handler for. `tail -f /dev/null` as PID 1 therefore ignored SIGTERM outright, so every
`kubectl delete pod` waited out terminationGracePeriodSeconds and then needed SIGKILL.
Handling TERM and INT is what lets the machine shut down when it is asked to.
"""

from __future__ import annotations

import os
import signal
import sys
import time

#: How long to wait before asking again, when there is nothing to reap. `waitpid` blocks
#: while children exist, so this only paces the idle case — the common one on a healthy
#: node, where nothing has been orphaned in hours.
IDLE_SLEEP_S = float(os.environ.get("AIOS_REAPER_IDLE_S", "1"))


class Terminated(Exception):
    """A signal asked this process to stop. Carries the signal number."""


def _raise_terminated(signum, _frame):
    # Raising rather than setting a flag on purpose: since PEP 475 a syscall interrupted
    # by a handled signal is RETRIED once the handler returns, so a flag checked at the
    # top of the loop would never be reached while `waitpid` is blocking on a child that
    # is not going to exit. An exception unwinds out of the syscall.
    raise Terminated(signum)


def install_handlers(handler=_raise_terminated) -> list[int]:
    """Make this process killable. Returns the signals it now handles."""
    installed = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):  # not the main thread, or not supported here
            continue
        installed.append(signum)
    return installed


def reap_forever(
    *,
    waitpid=os.waitpid,
    sleep=time.sleep,
    should_stop=None,
    idle_sleep_s: float = IDLE_SLEEP_S,
) -> int:
    """Reap reparented children until told to stop. Returns how many were reaped.

    Every error path continues rather than exits: an init that dies because one
    waitpid() call surprised it takes the whole machine down with it, and a pod whose
    PID 1 returns is a CrashLoopBackOff.
    """
    reaped = 0
    while should_stop is None or not should_stop():
        try:
            pid, _status = waitpid(-1, 0)
        except ChildProcessError:
            # Nothing left to wait for. Normal and quiet — most of a node's uptime.
            sleep(idle_sleep_s)
            continue
        except InterruptedError:
            # A handled signal that did not raise. Ask again immediately.
            continue
        except OSError:
            sleep(idle_sleep_s)
            continue
        if pid > 0:
            reaped += 1
    return reaped


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    install_handlers()
    print(f"aios.reaper: pid {os.getpid()} reaping orphans", flush=True)
    try:
        reap_forever()
    except Terminated as exc:
        name = signal.Signals(exc.args[0]).name if exc.args else "signal"
        print(f"aios.reaper: {name} — stopping", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
