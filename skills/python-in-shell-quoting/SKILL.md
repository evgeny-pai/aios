---
name: python-in-shell-quoting
description: Use when embedding Python in shell via python3 -c, or on "SyntaxError: unexpected character after line continuation character". Inside single shell quotes, quotes are already literal — escaping them breaks Python.
---

# Do not escape quotes that are already literal

**Failed:** `python3 -c '... print(f"{l[\"digest\"]}") ...'` → `SyntaxError: unexpected character after line continuation character`. Because the helper had `2>/dev/null`, it silently returned empty and the boot banner reported a missing lockfile that was fine.

**Why:** in `'single quotes'` the shell interprets nothing — `\"` reaches Python as backslash-quote. The escaping habit comes from `"double quotes"`, where `"` genuinely needs it.

**Fix:** single-quote the Python, use double quotes freely inside, and avoid nested quotes in f-strings (only legal on 3.12+).

```sh
lock=$(python3 -c '
from forge import lock
l = lock.load("aios.lock.json")
print(l["digest"][7:19] + " " + str(len(l["packages"])))
')
```

Over ~3 lines use a quoted heredoc — `<<'PY'` passes the body through untouched; unquoted `<<PY` lets the shell expand `$` inside your Python.

Don't add `2>/dev/null` until the command is known to work.

**Verify:** `sh script.sh` with stderr visible — `sh -n` won't catch it.
