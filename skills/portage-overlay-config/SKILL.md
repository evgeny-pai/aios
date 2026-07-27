---
name: portage-overlay-config
description: Use when configuring a Gentoo overlay, or on "Deprecated syntax found in make.conf: PORTDIR_OVERLAY". repos.conf is the supported mechanism.
---

# Configure overlays with repos.conf

**Failed:** `PORTDIR_OVERLAY="/var/db/repos/aios"` in make.conf → works, but warns on *every* emerge. In a loop that builds once per candidate, that trains you to ignore emerge's stderr.

**Why:** `PORTDIR_OVERLAY` predates multi-repo support and carries only a path — no priority, no sync policy.

**Fix:** one file per repo in `/etc/portage/repos.conf/`.

```ini
[aios]
location = /var/db/repos/aios
masters = gentoo
auto-sync = no
priority = 100
```

`masters = gentoo` is required or eclass inherits fail. The overlay also needs `profiles/repo_name` and `metadata/layout.conf`. Leave a comment where `PORTDIR_OVERLAY` was so nobody re-adds it.

**Verify:** `emerge -p app-editors/vim 2>&1 | grep -i deprecat` prints nothing.
