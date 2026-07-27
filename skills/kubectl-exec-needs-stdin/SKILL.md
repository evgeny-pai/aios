---
name: kubectl-exec-needs-stdin
description: Use when a heredoc or piped input to `kubectl exec` produces no output and no error, or when a script works locally but silently does nothing in a pod. exec does not forward stdin without -i.
---

# kubectl exec discards stdin unless you pass -i

**Failed:** a verification block that printed absolutely nothing — no output, no error, exit 0:

```sh
kubectl exec -n aios aios -- python3 - <<'PY'
print("this never runs")
PY
```

The heredoc was consumed by the local shell, `kubectl exec` was invoked with no `-i`, so the remote `python3 -` read an immediately-closed stdin and exited cleanly having executed nothing. Silence read as "the checks passed".

**Why:** `-i` (`--stdin`) is what attaches the local stdin to the remote process. Without it there is no pipe at all — not an empty one — and any program reading stdin (`python3 -`, `sh -s`, `cat`, `bash`) sees instant EOF and succeeds trivially. Nothing warns you, because doing nothing successfully is not an error.

**Fix:** pass `-i`, or don't use stdin.

```sh
kubectl exec -i -n aios aios -- python3 - <<'PY'
print("runs")
PY

kubectl exec -n aios aios -- sh -c 'python3 -c "print(\"runs\")"'   # no stdin needed
```

Add `-t` only for a real interactive terminal; `-it` on a non-tty caller produces its own warning and mangles output. For scripted checks prefer the `sh -c` form — it has no stdin dependency and survives being run from any harness.

**Verify:** make the payload print something unmistakable and assert you saw it. A block that "passes" with zero output has not run — treat empty output from a check as failure, never as success.
