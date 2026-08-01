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

import grp
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
            # A node that already has a handle is exactly the common case — every
            # multi-generation node had one minted before this function ever grew
            # `create_account` — and it must not be the one path that skips
            # `ensure_account`. Without this, no amount of self-update or reboot
            # ever creates the account on an existing node: this same function,
            # called from both aios-init and aios-login, always takes this branch.
            if create_account:
                ensure_account(existing)
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


#: The group that carries administrative privilege, and the sudoers drop-in that
#: grants it. The drop-in names the GROUP, not the handle, so it is one constant
#: file on every node — the per-machine part (which handle is in wheel) lives in
#: /etc/group where account state belongs.
WHEEL = "wheel"
SUDOERS = Path("/etc/sudoers.d/aios-operator")
SUDOERS_TEXT = "%wheel ALL=(ALL:ALL) NOPASSWD: ALL\n"
SUDOERS_MODE = 0o440


def ensure_account(handle: str) -> bool:
    """Make the handle real in /etc/passwd. Best-effort and never fatal.

    Real means usable: the account is in `wheel` and the sudoers drop-in for that
    group exists, so the operator can administer the machine without being root.
    NOPASSWD because the account has no password to give — nothing on this machine
    ever set one, and a prompt that can only be answered by ^C is worse than none.

    All of it lives in the ephemeral layer (/etc/passwd, /home, /etc/sudoers.d), so
    this runs at every boot and every login and converges rather than creates: an
    account that already exists is joined to wheel if it is not there, and the
    drop-in is rewritten only when its content or mode is wrong.

    The agent session itself stays uid 0 — see the module docstring — this is for
    the human in the cockpit's shell pane.
    """
    try:
        pwd.getpwnam(handle)
    except KeyError:
        try:
            subprocess.run(
                ["useradd", "--create-home", "--shell", "/bin/bash", handle],
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return False
    _join_wheel(handle)
    _ensure_sudoers()
    return True


def _join_wheel(handle: str) -> bool:
    """Membership of wheel, added exactly once. Best-effort."""
    try:
        if handle in grp.getgrnam(WHEEL).gr_mem:
            return True
    except KeyError:
        return False  # a userland with no wheel group has no privilege to grant
    try:
        subprocess.run(
            ["usermod", "-aG", WHEEL, handle],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _ensure_sudoers() -> bool:
    """The drop-in that makes wheel mean something. Best-effort.

    Written whether or not app-admin/sudo is installed yet: at boot the binary
    comes back from the binpkg cache moments before this runs, and on a machine
    that never installs sudo the file is inert. Mode 0440 because sudo refuses a
    sudoers file that is writable or executable, and refusing loudly at every
    `sudo` is the failure mode this avoids. The temp name contains a dot, which
    sudoers.d skips by rule, so a crash mid-write can never leave a half-parsed
    grant behind.
    """
    try:
        st = SUDOERS.stat()
        if (
            (st.st_mode & 0o777) == SUDOERS_MODE
            and SUDOERS.read_text(encoding="utf-8") == SUDOERS_TEXT
        ):
            return True
    except OSError:
        pass
    try:
        SUDOERS.parent.mkdir(parents=True, exist_ok=True)
        tmp = SUDOERS.with_suffix(".new")
        tmp.write_text(SUDOERS_TEXT, encoding="utf-8")
        tmp.chmod(SUDOERS_MODE)
        tmp.replace(SUDOERS)
    except OSError:
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


NAME_FILE = "node-name"


def node_name(root: Path | None = None) -> str:
    """The name this machine picks for itself, for a human to read.

    Distinct from `node_id()`, which is the stable identifier the mesh matches on and
    must never be pretty at the cost of being unique. This is the other half: three
    pods built from one spec are all called `aios`, all report the same spec name, and
    are told apart today only by a hex node id — which is exactly the kind of thing a
    person reads twice and still gets wrong. A foundry word is read once.

    Minted from the same word list as the operator handle and persisted beside it on
    the state volume, so the machine keeps the name across recreates. It chooses its
    own rather than taking one from the manifest, because a name in the manifest is
    the same name on every node that applies that manifest.
    """
    path = Path(root or os.environ.get("AIOS_ROOT", "/aios")) / STATE / NAME_FILE
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass

    minted = _mint()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".new")
        tmp.write_text(minted + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # an unwritable volume costs persistence, not identity
    return minted


def hostname(root: Path | None = None, generation: int | None = None) -> str:
    """`aios-<generation>-<name>` — what this incarnation of this machine is called.

    The generation is in the name on purpose. Two of the three facts that make a node
    itself are already stable (the id and the name); the third is which boot you are
    looking at, and that is the one that has actually caused confusion — a node
    reported running code it was not running, and a pod recreate that reverted the
    ephemeral layer was indistinguishable from one that did not. A prompt that reads
    `aios-9-anvil-3f` cannot be mistaken for `aios-8-anvil-3f`.

    Composed rather than stored, so it cannot go stale against the counter.
    """
    from . import generation as generation_mod

    # generation.current() takes its root from AIOS_ROOT rather than an argument, so
    # callers passing an explicit root (the tests) must pass the generation too.
    gen = generation_mod.current() if generation is None else generation
    return f"aios-{gen}-{node_name(root)}"


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
