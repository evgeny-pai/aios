# The `aios` overlay

Where `forge` writes ebuilds and patches that Gentoo's tree cannot express.

Portage's USE flags only expose the feature switches an ebuild author chose to
expose. When a probe shows a feature is unused but no USE flag turns it off, the
lever has to be created — an `EXTRA_ECONF` addition, a `--disable-*` configure
flag, or a patch. Those land here, in a package's `.ebuild`, and the minimizer's
journal records which of them survived.

Layout is a standard Gentoo overlay (`masters = gentoo`), so `emerge` needs no
special handling — only the `repos.conf` entry that `forge render --overlay`
writes.

```
overlay/
  metadata/layout.conf
  profiles/
    repo_name
    categories              # only for categories not in the gentoo tree
  <category>/<package>/
    <package>-<version>.ebuild
    files/*.patch
```

Nothing here yet — phase 1 populates it with the first forged ebuild
(`app-editors/vim`, with configure-level levers the tree's USE flags don't
provide).
