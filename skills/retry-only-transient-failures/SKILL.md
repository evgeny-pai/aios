---
name: retry-only-transient-failures
description: Use when wrapping a command in a retry loop — kubectl, curl, any flaky RPC. A loop that retries on any non-zero exit re-runs legitimate failures, multiplying cost and burying the real error in duplicates.
---

# A retry wrapper that cannot tell "flaky" from "wrong"

**Failed:** a wrapper added to survive genuine ephemeral-port exhaustion:

```sh
k() { for i in 1 2 3 4 5 6; do kubectl -n "$NS" "$@" && return 0; sleep 5; done; return 1; }
```

Then it wrapped an update that legitimately failed its test gate. The gate is *supposed* to return non-zero. So the whole thing ran six times — six downloads, six full test-suite runs inside the container, and six identical paragraphs of output before the real message was readable:

```
aios.update: candidate gen2-cockpit failed its own tests — not promoted:
  agent: Ran 112 tests in 9.179s / FAILED (failures=1, skipped=1)
[... the same, five more times ...]
```

Roughly a minute of compute and a screen of noise to learn one thing once.

**Why:** `&& return 0` treats *every* non-zero as "try again". But the failures worth retrying are transport-level and self-describing (`can't assign requested address`, connection reset, 429, 503), while a non-zero exit from the command's own logic is an answer — and re-asking cannot change it.

**Fix:** retry on the signal, not on the exit code.

```sh
k() {                      # retry only what looks transient
    for i in 1 2 3; do
        out=$(kubectl -n "$NS" "$@" 2>&1); rc=$?
        [ $rc -eq 0 ] && { printf '%s\n' "$out"; return 0; }
        case $out in
            *"can't assign requested address"*|*"connection refused"*|*"i/o timeout"*)
                sleep 5; continue ;;
        esac
        printf '%s\n' "$out" >&2; return $rc      # a real answer: report it once
    done
    return 1
}
```

Cheaper still for expensive commands: do not wrap them at all. Retry the *cheap* connectivity check, then run the expensive thing once.

**Verify:** point the wrapper at a command that fails deterministically (`false`, or a gate you know is red) and confirm it runs **once**. If the error appears more than once, the wrapper is retrying answers.
