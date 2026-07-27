---
name: subprocess-stdin-hang
description: Use when a subprocess hangs forever, a test suite stops producing output, or a tool that "works by hand" times out in CI — especially vim, ssh, git, gpg, apt, sudo. capture_output does not close stdin.
---

# capture_output does not close stdin

**Failed:** a 1.7s test suite printed its banner and then hung until killed at 120s. The call was ordinary:

```python
subprocess.run([sys.executable, "-m", "forge", "probe"],
               cwd=repo, capture_output=True, text=True, timeout=300)
```

`forge probe` runs `vim -es`, which reads stdin. It sat waiting for a human. The same call had worked minutes earlier from a caller whose stdin happened to be `/dev/null` — which is what makes this class invisible until something runs it from a terminal.

The same omission was in shipped code, in the verifier an autonomous agent must pass before declaring done — and its deployment gives PID 1 a tty.

**Why:** `capture_output=True` is exactly `stdout=PIPE, stderr=PIPE`. There is no `capture_input`; stdin stays inherited. Terminal → blocks forever. A pipe *with* data → the child eats input the parent needed, silently.

**Fix:**

```python
subprocess.run(argv, capture_output=True, text=True, timeout=60,
               stdin=subprocess.DEVNULL)
```

Belt and braces for known offenders: `ssh -o BatchMode=yes`, `git` with `GIT_TERMINAL_PROMPT=0`, `gpg --batch`, `sudo -n`, `DEBIAN_FRONTEND=noninteractive`. `timeout=` is not a fix — it converts an infinite hang into a slow one, which in a per-iteration build loop is its own outage.

**Verify:** `grep -rn "subprocess.run\|Popen" --include=*.py . | grep -v "stdin="` — and run the suite from a real terminal at least once.
