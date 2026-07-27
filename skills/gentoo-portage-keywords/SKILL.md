---
name: gentoo-portage-keywords
description: Use when writing ACCEPT_KEYWORDS or CHOST for a Gentoo target, or when emerge says "there are no ebuilds to satisfy" for packages that clearly exist. Portage's arch keyword is not the uname string.
---

# Portage arch keywords are not uname strings

**Failed:** `ACCEPT_KEYWORDS="aarch64"` rendered from `uname -m` → every package invisible: `emerge: there are no ebuilds to satisfy "app-editors/vim".`

**Why:** portage keywords are Gentoo's own names, and no rule derives them from the kernel's. `aarch64`→`arm64`, `x86_64`→`amd64`. CHOST *does* use the uname spelling, so both appear in one build.

**Fix:** one lookup, next to the CHOST derivation. `KeyError` on an unmapped arch beats a silent empty package set.

```python
@property
def keyword(self) -> str:
    return {"aarch64": "arm64", "x86_64": "amd64"}[self.arch]
```

Others: `armv7l`→`arm`, `riscv64`→`riscv`, `ppc64le`→`ppc64`.

**Verify:** `grep ACCEPT_KEYWORDS make.conf` is `arm64`; on a real host `emerge -p app-editors/vim` resolves.
