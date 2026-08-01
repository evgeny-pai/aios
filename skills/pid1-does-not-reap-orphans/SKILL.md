---
name: pid1-does-not-reap-orphans
description: Use when a test that starts a detached/orphaned process and waits for it to "not be alive" hangs until its own patience timeout, deterministically, no matter how generous the timeout — or when a real node accumulates zombie processes over time. Applies to any bare PID 1 (a `docker build` RUN step, a container with no init) that never calls wait() on a reparented child.
---

# A zombie answers `kill(pid, 0)` forever — bumping the timeout never fixes it

**Failed:** `aios.test_build`'s `Outliving`/`Stopping` tests deliberately orphan a
process (starting a job from a subprocess that then exits, or killing a job's
whole process group) and poll `until(lambda: not alive(pid))`. Inside `docker
build`'s RUN step this failed identically every run:

```
FAIL: test_a_fresh_interpreter_finds_it_and_reads_its_exit_status
AssertionError: False is not true
FAIL: test_stop_kills_the_whole_group
AssertionError: False is not true : the child outlived the group
```

Raising `PATIENCE_S` from 10s to 30s to 60s changed nothing — the suite got
slower, the failures stayed exactly the same four tests at exactly the same
assertions.

**Why:** the process really did exit. Its problem is nobody ever called
`wait()`/`waitpid()` on it, so the kernel keeps it as a zombie — a process-table
entry with no resources, but still a valid PID. `os.kill(pid, 0)` (or
`os.killpg`, or `ps`) reports a zombie as existing, because it does; only
reaping removes the entry. Linux reparents an orphan to the nearest subreaper
or, failing that, to PID 1, and it is PID 1's job to reap everything reparented
to it — that is what makes something an "init" rather than just the first
process. A bare shell or `python3 -m something` running as PID 1 (a
`docker build` RUN step; this project's own `aios-init` before it grows a
reaper) never does. The orphan dies, becomes a zombie, and stays one forever —
which is also how a long-lived pod accumulates zombies over uptime with no
single command ever visibly failing.

A patience window cannot fix a wait that will never happen. It only makes the
suite spend longer finding that out.

**Fix:** treat "no reaping init" as an environment limitation, not a timing
problem, and skip the tests that require one — the same shape as
`skills/host-dependent-assertions`:

```python
#: pgid 1 means PID 1 has no session of its own, which in practice means no
#: reaper either — a real init always gives itself one.
needs_reaping_init = unittest.skipIf(
    os.getpgid(0) <= 1,
    "needs an init that reaps orphaned children; PID 1 here does not",
)

@needs_reaping_init
def test_a_fresh_interpreter_finds_it_and_reads_its_exit_status(self):
    ...
```

The actual fix — giving the process that will run as PID 1 a reaper (`tini`,
`dumb-init`, a `while wait; do :; done` trap on `SIGCHLD`) — is a real,
separate piece of work, not a one-line test change; this skill is about not
mistaking the symptom (a hung poll) for a timing bug and inflating a constant
that cannot help.

**Verify:**

```sh
python3 -c "import os; print(os.getpgid(0))"   # 1 inside a bare docker build RUN step
```

If that prints `1`, no wait-for-death assertion in this environment can pass,
regardless of how long it waits.
