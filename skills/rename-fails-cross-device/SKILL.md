---
name: rename-fails-cross-device
description: Use on "OSError: [Errno 18] Cross-device link" or EXDEV from os.rename/Path.rename when both paths look like they are on the same filesystem — especially inside a container, or when the directory being moved contains a mount point.
---

# os.rename is not a primitive you can rely on

**Failed:** an atomic "move the old tree aside, move the new one in" swap, twice, on one mount.

```
OSError: [Errno 18] Cross-device link: '/aios' -> '/aios.prev'
OSError: [Errno 18] Cross-device link: '/aios/aios' -> '/aios.prev/aios'
```

`df` said one filesystem. `mount` showed why anyway — two different causes wearing the same errno:

1. **`/aios` contains a mount.** `/dev/vdb1 on /aios/.aios type ext4` — a persistent volume for state. Renaming a directory that contains a mount point cannot work, so this design fails on every node that has state, which is all of them.
2. **overlayfs cannot rename a lower-layer directory.** `/aios/aios` came from the container image's read-only layer and had never been copied up. Both paths were on the overlay; the rename is still refused.

**Why:** `rename(2)` requires src and dst on the same *filesystem instance*, and both a bind/volume mount and an overlay lower layer break that even when the paths sit under one apparent mount. `EXDEV` from a path you were sure was local means "check `mount`, not `df`".

**Fix:** try rename, fall back to copy — and remove the destination first, or `shutil.move` nests inside it.

```python
def _move(src: Path, dst: Path) -> None:
    try:
        src.rename(dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(dst))   # copy+delete: slower, correct
```

Better still, avoid moving directories that contain state: swap the *entries you own* inside the directory rather than the directory itself. That sidesteps mount points entirely and leaves everything you did not ship untouched.

Note what you lose: copy-then-delete is not atomic. Gate before swapping so a partial swap means the filesystem failed, not the code.

**Verify:** run it on the real target, not a clean tmpdir. `mount | grep <path>` before designing any rename; a local test on one filesystem cannot reproduce either cause.
