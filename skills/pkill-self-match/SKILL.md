---
name: pkill-self-match
description: Use when pkill/pgrep -f kills the shell that ran it, or a remote exec returns exit 143 (SIGTERM) instead of output. The pattern matches the killer's own command line.
---

# pkill -f matches its own command line

**Failed:** cleaning up stray build processes inside a container:

```sh
kubectl exec pod -- sh -c 'pkill -f "emerge"; echo "left: $(pgrep -c -f emerge)"'
```

```
command terminated with exit code 143
```

No output. `pkill -f` matches against full command lines, and the `sh -c` string *contains* the word `emerge` — so it matched itself and sent SIGTERM to its own shell. 143 = 128 + 15.

**Why:** `-f` matches the whole argv, and a shell invoked with `-c` has the script text in its argv. Any pattern naming the process you want to kill is also present in the command doing the killing.

**Fix:** break the literal with a character class — the regex still matches the target, but the pattern text itself no longer does.

```sh
pkill -f "emerg[e]-webrsync"      # matches "emerge-webrsync", not "emerg[e]-webrsync"
pkill -f "^emerg[e]"              # anchor too, so substrings elsewhere don't match
```

Safer still, avoid the class of bug: resolve PIDs first and check them, or exclude yourself — `pgrep -f pattern | grep -v $$`. Best of all, read before you kill: `ps -eo pid,args | grep '[e]merge'` shows what you are about to hit, and here it revealed the "stray" process was the machine successfully repairing itself.

**Verify:** `ps -eo pid,args` after — and confirm the exec itself returned 0, not 143.
