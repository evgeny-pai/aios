---
name: zombie-reads-as-alive
description: Use when a finished background job still reports "running", or `os.killpg(pgid, 0)` raises `PermissionError: [Errno 1] Operation not permitted` for a process you know exited. An unreaped child is a zombie, and a zombie answers liveness checks as alive.
---

# A zombie answers "is it alive?" with yes

**Failed:** a detached build job, spawned with `Popen(..., start_new_session=True)` and
deliberately never waited for, kept reporting `running` after it had exited. The liveness
check was the usual one:

```python
os.killpg(pgid, 0)     # ProcessLookupError -> dead, otherwise alive
```

```
PermissionError: [Errno 1] Operation not permitted
```

The pid was a **zombie** — the child had exited, but the process that started it was
still alive and had never called `waitpid`, so the entry stayed in the table. `kill(0)`
on a zombie does not raise `ProcessLookupError`: it succeeds, or returns `EPERM` (macOS),
and `EPERM` has to be read as "alive but not ours to signal" for the case where it truly
is. Either way the answer is "alive", forever.

**Why:** the process only disappears when someone reaps it. It is reaped by init — which
reparents and reaps it promptly — *only once its original parent is gone*. So the bug is
invisible in the case the design is about (the starter exited long ago) and permanent in
the case that is easiest to test (the starter is still running).

**Fix:** reap your own children before asking the kernel. Keep the `Popen` and poll it;
a pid you did not start is not yours to wait for, and the kernel's answer about it is the
truthful one.

```python
def alive(pgid: int, pid: int = 0) -> bool:
    own = _OWN.get(pid)                 # {pid: Popen} for jobs THIS process started
    if own is not None and own.poll() is not None:
        return False                    # ours, ended, and now reaped
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                     # exists; simply not ours to signal
    return True
```

Keeping the `Popen` also silences `ResourceWarning: subprocess N is still running`, which
`Popen.__del__` emits on every deliberately-detached spawn — override `__del__` on a
subclass rather than filtering warnings globally, so only these objects stay quiet.

**Verify:** start a job that exits immediately, delete whatever durable status it wrote,
and assert the state is `vanished` and not `running` — from the same process that started
it, which is the case that fails.
