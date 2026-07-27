---
name: negative-assertions
description: Use when asserting something is absent — assertNotIn, "must not appear in the output", grep -v checks. A bare name also matches the comment explaining why it is banned.
---

# Assert on the syntax, not on the name

**Failed:** `self.assertNotIn("PORTDIR_OVERLAY", make_conf)` → `AssertionError: 'PORTDIR_OVERLAY' unexpectedly found in '...# PORTDIR_OVERLAY is deprecated and warns on every emerge.'` The setting *was* gone; the test matched the comment added to stop anyone re-adding it.

**Why:** a bare identifier is a substring search over comments, docs and log lines too. The silent direction is worse: `assertNotIn("password", out)` passes while `passwd=hunter2` leaks.

**Fix:** match syntax only a real occurrence produces — or parse and assert on data.

```python
self.assertNotIn("PORTDIR_OVERLAY=", make_conf)          # the delimiter makes it real

settings = dict(l.split("=", 1) for l in make_conf.splitlines()
                if "=" in l and not l.lstrip().startswith("#"))
self.assertNotIn("PORTDIR_OVERLAY", settings)            # immune by construction
```

**Verify:** reintroduce the banned output once and confirm the test *fails*. An absence test you've never seen fail is untested.
