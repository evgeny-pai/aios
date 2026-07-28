---
name: iuse-defaults-decide-reuse
description: Use when a tool predicts whether portage will reuse a binary package, or when emerge says "use flag configuration mismatch" / rebuilds a package a checker called `exact`. Portage compares the package against the USE it computes from the ebuild's IUSE defaults, not against the flags a lockfile names.
---

# Portage compares against the USE it computes, not the flags you wrote down

**Failed:** on a live node, `forge binpkg` called the peer's vim `exact` while the
same emerge refused it:

```
verdict  package                   build  size
exact    app-editors/vim-9.1.0866  #1     4.0MiB
1/1 reusable on this machine: 1 exact
```

```
 * The following binary packages have been ignored due to use flag configuration mismatch:
 *     app-editors/vim-9.1.0866-1
```

**Why:** `fit()` compared only the flags the lockfile *names*. vim's ebuild is
`+crypt`, the package was built without crypt, and the lockfile never mentioned
crypt — so forge saw no disagreement while portage, which starts its USE from the
ebuild's own IUSE defaults, saw one. The command whose only job is answering "why
did this rebuild instead of reusing" gave the most confidently wrong answer available.

**Fix:** keep IUSE's `+`/`-` markers (they *are* the defaults), and treat a
defaulted-on flag missing from USE as a rebuild the lockfile never asked for.

```python
iuse_tokens = _tokens(files.get("IUSE", ""))         # "+crypt debug -X"
iuse = frozenset(f.lstrip("+-") for f in iuse_tokens)
defaults = _defaults(iuse_tokens)                    # {"crypt"}
if not _marked(iuse_tokens):                         # markers stripped by the producer
    defaults = _defaults(_tokens(files.get("IUSE_EFFECTIVE", "")))
...
clash = [f for f in meta.default_off() if f not in decided and not _profile_set(f)]
```

An unmarked IUSE is ambiguous (most ebuilds have no defaults), so it is never a gap
— only a reason to read IUSE_EFFECTIVE. A flag the lockfile *does* name overrides the
ebuild default and is already judged; say what is unconstrained instead of implying
disagreement: "the lockfile does not constrain crypt; this package has it off and the
ebuild defaults it on".

**Verify:** `python3 -m unittest tests.test_binpkg.TestEbuildDefaults -v` — and check
the failing verdict is reachable at all by stripping the `+` markers from the fixture's
IUSE: if that still reports `rebuild`, the test is not measuring the defaults.
