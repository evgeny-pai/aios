---
name: vacuous-probe-checks
description: Use when writing a capability check, smoke test, or probe that is meant to prove a binary works — or when a test suite is green for something that is not installed. A check that passes with the subject absent is worse than a failing one.
---

# A check that passes when the thing is missing is not a check

**Failed:** an agent authored a probe for `ranger` on a machine where neither ranger nor vim was installed. `forge probe` reported:

```
ranger: FAIL 2/3
  FAIL ranger binary exists   (which: no ranger in (/usr/local/sbin:...))
  ok  ranger lists directory contents
  ok  ranger opens vim for editing
```

Two of three checks passed against a machine with nothing installed. Only the `which` check was honest.

**Why:** the checks exercised the *shell plumbing* around the binary rather than the binary — a pipeline whose exit status came from `echo`/`ls`/a `|| true`, so the missing command never decided the result. In this project the danger is specific and severe: `forge minimize` drops a feature and keeps it dropped whenever the probes stay green. A vacuously-green check licenses deleting the very thing it claims to protect.

**Fix:** make the subject decide the exit status, and prove the probe fails without it.

```sh
# Vacuous: `ls` sets the status; ranger's absence is invisible
ranger --list "$T" 2>/dev/null | ls "$T" && echo ok

# Honest: the binary runs, its status is the result, and output is asserted
command -v ranger
printf 'alpha\n' > "$T/f.txt"
ranger --cmd=quit "$T"            # non-zero if ranger is missing or broken
grep -qx alpha "$T/f.txt"
```

Rules that keep checks honest: no `|| true`, no `2>/dev/null` on the command under test, never end a pipeline with a command that can't fail, and assert on an observable *effect* (a file changed, a string in stdout) rather than on the invocation not crashing.

**Verify — the step that actually matters:** run the probe on a machine where the package is *absent* and confirm every check fails. A probe suite you have only ever seen pass is a probe suite you have not tested. Same discipline as [negative-assertions](../negative-assertions/SKILL.md) and [host-dependent-assertions](../host-dependent-assertions/SKILL.md): a guard that has never fired may not work.
