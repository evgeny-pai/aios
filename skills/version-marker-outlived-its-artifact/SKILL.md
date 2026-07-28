---
name: version-marker-outlived-its-artifact
description: Use when deciding where a version file, build stamp, migration marker, or cache key lives — especially in a container with volumes. A marker on persistent storage describing ephemeral content will survive a rollback it never describes and then lie about what is running.
---

# A version file on a volume describes code that is no longer there

**Failed:** the updater recorded what it had installed in `/aios/.aios/code-version`, on the state volume, next to the generation counter and the audit journal. The code it described lived in `/aios`, which is in the container's ephemeral layer.

Recreating the pod reverted the code to the image and kept the marker. The node then reported, confidently and wrongly:

```
code-version: 42ca31b78136          # the newest release
pod packages: app-editors/vim, net-misc/openssh, sys-devel/make   # a 3-package lock from weeks earlier
forge --help | grep binpkg          # (nothing — the subcommand did not exist yet)
```

`aios.update check` therefore said "up to date" and refused to install anything. The node was pinned to old code by a file claiming it was new, and the symptom looked like a broken updater.

**Why:** persistence is per-directory, not per-meaning. The marker was put with the *other state* because that is where state goes, but a version marker is not state about the machine — it is **metadata about an artifact**, and it is only true while that artifact is there. Split them and the pair can desynchronise in the one direction that matters: the claim outliving the thing claimed.

The same project had already reasoned this out correctly elsewhere and not generalised it — its manifest deliberately does **not** persist `/var/db/pkg`, because a package database on a volume would outlive the `/usr` binaries it describes and insist tmux was installed with no `/usr/bin/tmux`. Identical shape, one level up.

**Fix:** put the marker with the artifact, and let it disappear when the artifact does.

```python
VERSION_FILE = ROOT / ".code-version"      # ephemeral, beside the code
# not STATE / "code-version"               # persistent, outlives what it describes
```

A recreated node now reports `unknown` and takes an update. "Unknown" is true; "up to date" was not.

If the marker genuinely must persist, make it self-checking instead: store a fingerprint of the artifact alongside the version and treat a mismatch as unknown — never as the recorded version.

**Verify:** the test is a destroy-and-recreate, not a restart. Recreate the container so the ephemeral layer resets, then ask what version it thinks it is running and check that answer against the artifact itself:

```sh
python3 -m aios.update check          # must say unknown, not "up to date"
python3 -m forge --help | grep -c binpkg   # does the code match the claim?
```
