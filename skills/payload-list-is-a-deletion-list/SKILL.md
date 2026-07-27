---
name: payload-list-is-a-deletion-list
description: Use when defining what an update, sync, deploy, or rsync ships. Whatever the list omits is what gets destroyed or stranded on the target — audit it against a real target's directory listing, never against a clean checkout.
---

# What the payload omits is what you delete

**Failed:** an update payload, written from the repo's point of view:

```python
PAYLOAD = ("aios", "forge", "probes", "tests", "overlay", "skills",
           "aios.toml", "README.md", "DESIGN.md")
```

Reasonable-looking, and wrong in both directions. `ls -A` on a live node showed what the list had never been checked against:

```
.aios  AESTHETIC.md  CLI.md  DESIGN.md  MANUAL.md  QUICKSTART.md
aios  aios.lock.json  aios.toml  bin  forge  overlay  probes  showcase.sh
skills  tests  wget-log  .swp
```

Two failures:

- **`aios.lock.json` and `MANUAL.md` were missing from the payload.** The design replaced the tree wholesale, so applying an update would have **deleted the lockfile** — the one artifact everything downstream reads. A node without it cannot render, cannot build, and fails its own consistency check.
- **The node had written four files of its own** — `AESTHETIC.md`, `CLI.md`, `QUICKSTART.md`, `showcase.sh`. A wholesale replacement destroys work the target produced, which is exactly what a long-lived machine accumulates and exactly what you cannot get back.

**Why:** the list is written while looking at a clean checkout, where the repo contents and the target contents are the same set. On a live target they diverge immediately: generated artifacts, state, logs, and whatever the machine made for itself. "What do I ship" and "what may I destroy" are different questions and the same list answers both.

**Fix:** replace only the entries you own, and never the containing directory.

```python
for item in PAYLOAD:              # anything not named here is neither moved nor removed
    incoming = stage / item
    if not incoming.exists():
        continue
    _remove(previous / item)
    if (root / item).exists():
        _move(root / item, previous / item)
    _move(incoming, root / item)
```

Then audit deliberately: `ls -A` the real target, and for every entry answer *shipped*, *state*, or *the target's own*. Generated artifacts are the trap — they belong to the target but must still travel when the generator ran elsewhere.

**Verify:** apply to a target seeded with state and a file the payload does not mention; assert the state, the foreign file, *and* the generated artifact all survive. A test on an empty target cannot catch any of this.
