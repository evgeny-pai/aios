---
name: zsh-no-word-splitting
description: Use on "command not found" that names a whole command line including its arguments, when a variable holding a command prefix stops working, or when writing shell for both zsh and bash.
---

# zsh does not word-split unquoted variables

**Failed:** `K="kubectl --context kind-aios -n aios exec aios --"` then `$K sh -c 'id'` →

```
(eval):3: command not found: kubectl --context kind-aios -n aios exec aios --
```

The error naming the *entire string* is the tell. Correct under bash, which is where the habit comes from.

**Why:** bash word-splits unquoted parameter expansions; **zsh does not** — `$K` is one word, spaces and all. zsh is macOS's login shell, so anything without a shebang gets these semantics.

Same family: `$array` is the first element in bash but *all* elements in zsh; an unmatched glob is literal text in bash but an **error** in zsh (the command doesn't run).

**Fix:** a function — correct in both, and reads better.

```sh
kexec() { kubectl --context kind-aios -n aios exec aios -- "$@"; }
kexec python3 -V
```

If it must be data, an array with explicit indexing: `K=(kubectl ... --); "${K[@]}" sh -c 'id'`. For scripts, set a shebang and stop guessing.

**Verify:** `zsh -c 'K="ls -la"; $K' ` fails where `bash -c` succeeds. `-n` syntax checks won't catch it — splitting is runtime.
