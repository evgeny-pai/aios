---
name: tests-must-own-their-stdin
description: Use when a test suite hangs with no failing test named, passes in CI but hangs in a terminal (or vice versa), or when a test exercises code that calls input(). A test that inherits the caller's stdin depends on the caller, not on the code.
---

# A suite that inherits stdin hangs for reasons no test names

**Failed:** the agent suite "hung" three separate times, twice burning a 10-minute timeout, with no failing test and no traceback. Diagnosed as a race with a concurrent process. It was not.

```
PYTHONPATH=. python3 -m unittest aios.test_agent 2>&1 | tail -5   # hangs forever
PYTHONPATH=. python3 -m unittest aios.test_agent > log 2>&1 &     # 112 tests, 4.3s, OK
```

The difference is the ampersand. zsh gives a backgrounded job `/dev/null` on stdin; a foreground pipeline hands it mine. One test called `agent.main([])`, which calls `input()`, with this comment:

```python
code = agent.main([])  # stdin is not a tty under unittest -> immediate EOF
```

That comment is a claim about the *caller*. Backgrounded: instant EOF, pass. Foreground: `input()` waits for a human who is not typing, forever.

**Why:** it hangs rather than fails, so `unittest -v` never prints a verdict for the offending test and the last line looks like the test *before* it. Worse, whether it reproduces depends on how you launched it — which is exactly the shape of evidence that makes you blame concurrency.

**Fix:** supply stdin; never inherit it.

```python
@contextmanager
def _stdin(text: str):
    saved, sys.stdin = sys.stdin, io.StringIO(text)
    try:
        yield
    finally:
        sys.stdin = saved

with redirect_stdout(buffer), _stdin(""):
    code = agent.main([])
```

Same rule for subprocesses a test spawns: `stdin=subprocess.DEVNULL` (see [subprocess-stdin-hang](../subprocess-stdin-hang/SKILL.md)).

**Verify:** run the suite in the foreground, from a real terminal, with stdin open — the case that hangs. If it passes there and backgrounded, it owns its stdin. A comment asserting what the caller's stdin will be is the bug.
