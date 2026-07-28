"""The name this machine gives you.

An AIos node is not a shared server with a user directory — it is one machine with
one operator, and it is the machine that decides what to call them. That is a small
thing that changes the register: you are not "root@a3f9c2b1", you are the operator
this machine has been working with, and it remembers you across reboots because the
handle lives on the state volume next to the generation counter.

The words come from a foundry, which is what `forge` is. They are picked once, never
regenerated, and they are safe as a POSIX login name: lowercase, leading letter, no
punctuation beyond a hyphen.

A real /etc/passwd entry is created to match, best-effort. An identity that exists
only in a greeting is decoration; one that `id` can resolve is part of the machine.
The agent session itself still runs as uid 0 — it has to emerge packages — so the
handle names *you*, not the privilege the session holds.
"""

from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path

STATE = ".aios"
FILE = "operator"

#: Foundry words. Concrete objects and operations, nothing whimsical — the handle
#: appears in a boot screen next to a kernel version, and it has to sit there without
#: embarrassing the rest of the line.
WORDS = (
    "anvil", "billet", "bellows", "crucible", "flux", "forge", "ingot", "lathe",
    "mandrel", "mason", "quench", "swage", "temper", "tongs", "trammel", "vise",
)


def _path(root: Path | None = None) -> Path:
    base = Path(root or os.environ.get("AIOS_ROOT", "/aios"))
    return base / STATE / FILE


def _mint() -> str:
    """A handle the machine has not used before, from real entropy.

    `os.urandom` rather than `random`, because two nodes minting the same handle in
    the same second would defeat the one thing the name is for.
    """
    raw = os.urandom(3)
    word = WORDS[raw[0] % len(WORDS)]
    return f"{word}-{raw[1]:02x}{raw[2]:02x}"


def operator(root: Path | None = None, *, create_account: bool = False) -> str:
    """This machine's name for you. Minted on first call, stable afterwards."""
    path = _path(root)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass

    handle = _mint()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".new")
        tmp.write_text(handle + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # An unwritable state volume costs the handle its persistence, not the
        # session its identity.
        return handle

    if create_account:
        ensure_account(handle)
    return handle


def ensure_account(handle: str) -> bool:
    """Make the handle real in /etc/passwd. Best-effort and never fatal."""
    try:
        pwd.getpwnam(handle)
        return True
    except KeyError:
        pass
    try:
        subprocess.run(
            ["useradd", "--create-home", "--shell", "/bin/bash", handle],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


# --- who the MACHINE is, as distinct from who you are ------------------------

NODE_FILE = "node-id"


def node_id(root: Path | None = None) -> str:
    """This node's own name on the network. Minted once, then never changes.

    Separate from `operator()` on purpose: that handle names the *human*, and a
    machine that introduces itself by its operator's name is not identifying itself.

    It has to be self-minted because nothing else here is unique. The pod's hostname
    is `aios` for every AIos pod ever created; the spec name `aios-dev` is shared by
    every node built from this spec; the pod IP changes on every recreate; and the
    generation counts boots rather than machines. All four are facts about a
    *category*, and the mesh needs to tell one node from another.

    Stable across reboots because it lives on the state volume next to the operator
    handle and the generation counter — the three things that make a node itself.
    """
    path = (root or Path(os.environ.get("AIOS_ROOT", "/aios"))) / STATE / NODE_FILE
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass

    minted = "node-" + os.urandom(3).hex()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".new")
        tmp.write_text(minted + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # an unwritable volume costs persistence, not identity
    return minted


def node_label(root: Path | None = None) -> str:
    """How this node should introduce itself: what it is, which incarnation, and who.

    Everything from this machine currently reaches the mesh through the host's share
    daemon, so a peer sees the HOST and cannot tell two pods apart. Until the mesh
    gains per-node registration, this is what a node puts in its requests, its served
    index and its boot screen so the distinction is at least visible.
    """
    from . import generation as generation_mod

    base = Path(root or os.environ.get("AIOS_ROOT", "/aios"))
    spec_name = "aios"
    try:
        import json

        spec_name = json.loads(
            (base / "aios.lock.json").read_text(encoding="utf-8")
        )["system"]["name"]
    except Exception:
        pass
    gen = generation_mod.current()
    suffix = f"#{gen}" if gen else ""
    return f"{spec_name}{suffix} ({node_id(root)})"


def greeting(handle: str, generation: int = 0) -> str:
    """One line, addressed to the operator. Shown once, at login."""
    where = f" generation {generation}" if generation else ""
    return f"you are {handle} on this machine{where}"


def main() -> int:
    import sys

    handle = operator(create_account="--account" in sys.argv[1:])
    print(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
