"""How many times this machine has come up clean.

Generations are the unit this project already thinks in — DESIGN.md §5 has each
one as a commit in /aios, promoted or rolled back whole. So the number belongs on
the welcome screen: it says which incarnation you are talking to, and it is how a
machine that rebuilds itself gets an identity across reboots.

Only a *clean* boot counts. A boot that came up with a broken userland is not a
new generation of anything, and inflating the number for it would make the one
fact this file carries a lie.

Lives on the state volume, so it survives the container it was written by.
"""

from __future__ import annotations

import os
from pathlib import Path

FILE = "generation"


def _path() -> Path:
    root = Path(os.environ.get("AIOS_ROOT", "/aios"))
    return root / ".aios" / FILE


def current() -> int:
    """The generation now running, or 0 if this machine has never booted clean."""
    try:
        return int(_path().read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def bump() -> int:
    """Record one more clean boot. Returns the new generation.

    Write-then-rename so an interrupted boot cannot leave a half-written number
    that `current()` would read as 0 and silently restart the count.
    """
    path = _path()
    nxt = current() + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".new")
        tmp.write_text(f"{nxt}\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # An unwritable state volume must not stop the machine from booting.
        return nxt
    return nxt


def main() -> int:
    import sys

    print(bump() if "bump" in sys.argv[1:] else current())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
