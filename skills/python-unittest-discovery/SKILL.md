---
name: python-unittest-discovery
description: Use on "ImportError: Start directory is not importable" from python3 -m unittest discover, or when setting up a stdlib-only test layout whose tests import the project package.
---

# unittest discover needs the start directory to be a package

**Failed:** `python3 -m unittest discover -s tests -t .` → `ImportError: Start directory is not importable: '.../tests'`. Eight frames of `unittest/loader.py`, so it reads like a bad `-s` path. The path was right.

**Why:** when `-t` differs from `-s`, discovery imports the start dir *as a package* — that needs `tests/__init__.py`. And `-t .` is not optional: it's what puts the repo root on `sys.path` so `from forge import lock` resolves. The two flags imply the `__init__.py`.

**Fix:**

```bash
touch tests/__init__.py
python3 -m unittest discover -s tests -t .
```

`PYTHONPATH=.` also works but pushes the requirement onto every caller, so the bare command in the README breaks.

**Verify:** expect `Ran N tests` / `OK`. `Ran 0 tests` means nothing matched — files must be named `test_*.py`.
