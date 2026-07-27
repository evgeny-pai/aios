---
name: updater-cannot-update-itself
description: Use when shipping a fix to the code that performs updates — an updater, migration runner, deploy script, or bootstrap installer. The broken version is the one that has to deliver its own replacement, so it cannot.
---

# The updater is the one component it cannot update

**Failed:** `aios/update.py` had a fatal bug — it renamed the tree it was replacing, which fails on any node with a mounted state volume. The fix was written, tested, published as a release, and then:

```
$ python3 -m aios.update apply
  File "/aios/aios/update.py", line 202, in apply
    ROOT.rename(PREVIOUS)
OSError: [Errno 18] Cross-device link: '/aios' -> '/aios.prev'
```

The target was still running the *old* updater, so the fix could not arrive by the mechanism the fix was for. Twice in a row, because the second attempt tripped a second bug in the same file.

**Why:** every other component is data to the updater; the updater is data to nothing. Its replacement has to be delivered by something outside itself, and the release that contains the fix is inert until the fix is already in place.

**Fix:** accept a deliberate out-of-band bootstrap step, and make it one step.

```sh
kubectl cp aios/update.py pod:/aios/aios/update.py    # once, by hand
python3 -m aios.update apply                          # everything else rides the mechanism
```

What makes this cheap rather than painful:

- **Keep the updater small and dependency-free.** It is the file you will hand-copy; every import is another thing that must already be correct on the target.
- **Make it idempotent and version-agnostic**, so hand-copying a newer one over an older one is always safe.
- **Do not let the updater be the only path in.** Whatever you used to bootstrap the node is the fallback — keep it working.
- **Version it separately** if you can: the target reports which updater it runs, so "this node cannot take updates until it is bootstrapped" is visible rather than inferred from a traceback.

The same shape applies to a database migration runner, a self-updating CLI, and a package manager updating itself — all of them need an external hand for their own upgrade.

**Verify:** test an updater fix by copying it to the target by hand *first*, then applying a release with it. A test where the updater under test is the fixed one already in place proves nothing about delivery.
