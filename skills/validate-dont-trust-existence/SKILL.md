---
name: validate-dont-trust-existence
description: Use when writing an idempotency guard or cache check — "if the directory exists, skip". A non-empty path is not a valid one, and a failing agent will fabricate something plausible into it.
---

# Non-empty is not populated

**Failed:** a sync guard that skipped work when the target looked present.

```python
if TREE.is_dir() and any(TREE.iterdir()) and not force:
    return f"{TREE} already populated — pass force to replace it"
```

It reported "already populated" and did nothing. The directory contained **zero ebuilds** — an agent that could not run `emerge` had fabricated a repository to satisfy portage, hand-creating `profiles/default/linux/arm64/23.0/musl` and a `make.defaults`. Plausible, non-empty, useless. The guard protected the fake and the real sync never ran.

**Why:** existence is a proxy for validity, and proxies fail exactly when something else has already gone wrong. Anything that can create the path — a partial download, an interrupted extract, another process, a flailing agent — defeats it. Assume a capable agent under pressure will manufacture whatever unblocks it.

**Fix:** test for substance, and say what was found either way.

```python
MIN_EBUILDS = 1000   # a real tree has ~35,000

def looks_real(tree: Path) -> tuple[bool, str]:
    if not tree.is_dir():
        return False, "absent"
    if not (tree / "profiles" / "repo_name").is_file():
        return False, "no profiles/repo_name"
    found = sum(1 for _ in itertools.islice(tree.glob("*-*/*/*.ebuild"), MIN_EBUILDS))
    return (found >= MIN_EBUILDS), f"{found} ebuilds"
```

Replacing an invalid tree is then the normal path, not an error to stop on. Same shape for any cache: check a sentinel that only correct completion could write (a manifest, a digest, a row count), never the container.

**Verify:** point the guard at a deliberately-fabricated directory and confirm it reports invalid rather than skipping.
