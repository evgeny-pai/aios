"""Updating a node's own code without rebooting it.

An AIos node is a Python userland on top of a Gentoo rootfs, so changing its code
does not require a new image or a new container — only new files and a fresh process.
That matters once nodes are long-lived: a machine with a generation number and an
audit log should not lose both to a `kubectl delete pod` because a prompt changed.

The shape is deliberately the same one DESIGN.md §5 uses for root filesystems, one
level down: never mutate the running tree. Stage the new code beside it, prove it,
swap by rename, and keep the old tree so the swap is reversible.

    /aios          the running code
    /aios.next     staged candidate, being verified
    /aios.prev     the last known-good tree, kept for rollback

The gate is the thing that makes this safe to automate: a candidate must pass its own
test suites before it is allowed to become /aios. Code that cannot verify itself does
not get promoted, which is the same rule the machine already applies to its packages
via probes.

Stdlib only. The publisher and the consumer are the same file so a node can serve
updates to its peers as easily as it takes them — see aios.repo for why the first
node to sync becomes the seed of the network.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(os.environ.get("AIOS_ROOT", "/aios"))
STAGE = Path(f"{ROOT}.next")
PREVIOUS = Path(f"{ROOT}.prev")
STATE = ROOT / ".aios"

#: The version marker lives WITH the code, not with the per-node state.
#:
#: It was in .aios — a mounted volume — and that made it lie. Recreating the pod
#: reverts /aios to the image (only .aios is persistent), so the marker survived a
#: code rollback it did not describe: a node reported running `42ca31b78136` while
#: its lockfile had three packages and `forge` had no `binpkg` subcommand. A version
#: file that outlives its artifact is worse than no version file, and it is the same
#: mistake the manifest already documents for /var/db/pkg, which is deliberately not
#: persisted because it would outlive the /usr binaries it describes.
#:
#: Here it is ephemeral on purpose: a recreated node reports "unknown" and takes an
#: update, which is true, instead of claiming a version it has lost.
VERSION_FILE = ROOT / ".code-version"
MANIFEST = "latest.json"

#: What a node's code actually consists of. Deliberately not the whole directory,
#: and the exclusions matter in both directions:
#:
#: - `.aios` is per-node state on a MOUNTED volume — the generation counter, the
#:   operator handle, the agent journal. It must never travel in an update.
#: - `aios.lock.json` and MANUAL.md were missing from this list, which would have
#:   made an update DELETE the lockfile from the target. A node without its lock
#:   cannot render, cannot build, and fails `forge diff`. Found by inspecting a real
#:   node before pointing this at it, not by a test.
#: - Anything a node created for itself (notes, scripts, logs) is not listed and is
#:   therefore left alone. #2 had written AESTHETIC.md, CLI.md, QUICKSTART.md and
#:   showcase.sh; an update is not entitled to destroy a machine's own work.
PAYLOAD = ("aios", "forge", "probes", "tests", "overlay", "skills", "container",
           "aios.toml", "aios.lock.json", "README.md", "DESIGN.md", "MANUAL.md")

#: Files that must land OUTSIDE /aios to have any effect, copied from the payload's
#: `container/` after a successful swap: source -> destination, mode.
#:
#: Without this the update was structurally incapable of delivering the login path,
#: the boot sequence, or the tmux layout — they live at /sbin and /etc, and PAYLOAD
#: only reaches /aios. A node took an update that "promoted" every module and still
#: logged you into the previous cockpit, which is the most confusing possible result:
#: the new code is present, and nothing you touch runs it.
INSTALL = (
    ("container/aios-init", "/sbin/aios-init", 0o755),
    ("container/aios-login", "/sbin/aios-login", 0o755),
    ("container/manual", "/usr/local/bin/manual", 0o755),
    ("container/tmux.conf", "/etc/aios/tmux.conf", 0o644),
)

#: Run inside a candidate before it may be promoted. Both suites, because an update
#: that breaks the build tool is as bad as one that breaks the agent.
GATE = (
    ("forge", ("-m", "unittest", "discover", "-s", "tests", "-t", ".")),
    ("agent", ("-m", "unittest", "aios.test_agent")),
    # Named individually rather than discovered, so a candidate that *deletes* a
    # suite fails the gate instead of quietly passing a smaller one. Suites absent
    # from a candidate are skipped, because an older release predates them and must
    # still be installable — a rollback target that cannot be installed is not one.
    ("cockpit", ("-m", "unittest", "aios.test_cockpit")),
    ("mesh", ("-m", "unittest", "aios.test_mesh")),
)


class UpdateError(Exception):
    """Message is user-facing."""


@dataclass(frozen=True)
class Release:
    version: str
    sha256: str
    path: str

    @classmethod
    def parse(cls, raw: dict) -> Release:
        try:
            return cls(str(raw["version"]), str(raw["sha256"]), str(raw["path"]))
        except KeyError as exc:
            raise UpdateError(f"malformed release manifest, missing {exc}") from None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def installed() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _get(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "aios-update/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"{url}: {exc}") from None


def available(base: str) -> Release:
    return Release.parse(json.loads(_get(f"{base.rstrip('/')}/{MANIFEST}").decode("utf-8")))


def check(base: str) -> str:
    release = available(base)
    here = installed()
    if release.sha256 == here:
        return f"up to date ({release.version})"
    return f"update available: {release.version} ({release.sha256[:12]})\n  running: {here[:12] or 'unknown'}"


# --- publisher ---------------------------------------------------------------


def publish(dest: Path, *, version: str, source: Path | None = None) -> str:
    """Package this node's code and write it where peers can fetch it.

    This is the whole of the "CI" — build an artifact, digest it, and advertise it.
    The verification half deliberately lives on the *consumer*, because the consumer
    is the only party that can tell whether the code works on the machine it has to
    run on.
    """
    source = source or ROOT
    dest.mkdir(parents=True, exist_ok=True)
    name = f"aios-{version}.tar.gz"
    archive = dest / name

    missing = [item for item in PAYLOAD if not (source / item).exists()]
    if missing:
        raise UpdateError(f"cannot publish, missing from {source}: {', '.join(missing)}")

    tmp = archive.with_suffix(".partial")
    with tarfile.open(tmp, "w:gz") as tar:
        for item in PAYLOAD:
            tar.add(source / item, arcname=item, filter=_exclude_noise)
    tmp.replace(archive)

    digest = _sha256(archive)
    manifest = {"version": version, "sha256": digest, "path": name,
                "bytes": archive.stat().st_size}
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
    return f"published {name} ({archive.stat().st_size / 1e6:.1f} MB) sha256 {digest[:12]}"


def _exclude_noise(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    if "__pycache__" in parts or info.name.endswith(".pyc"):
        return None
    if ".aios" in parts:  # per-node state never travels
        return None
    return info


# --- consumer ----------------------------------------------------------------


def apply(base: str, *, force: bool = False) -> str:
    """Fetch, verify, gate, and promote. Returns what happened.

    Nothing is destroyed: the outgoing tree becomes /aios.prev, so a bad promotion
    that somehow passes the gate is still one rename from being undone.
    """
    release = available(base)
    if release.sha256 == installed() and not force:
        return f"already running {release.version}"

    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / release.path
        archive.write_bytes(_get(f"{base.rstrip('/')}/{release.path}", timeout=300))
        actual = _sha256(archive)
        if actual != release.sha256:
            shutil.rmtree(STAGE)
            raise UpdateError(
                "digest mismatch — refusing to install\n"
                f"  advertised: {release.sha256}\n  actual:     {actual}"
            )
        with tarfile.open(archive) as tar:
            tar.extractall(STAGE, filter="tar")

    failures = gate(STAGE)
    if failures:
        shutil.rmtree(STAGE)
        raise UpdateError(
            f"candidate {release.version} failed its own tests — not promoted:\n  "
            + "\n  ".join(failures)
        )

    moved = swap_in(STAGE)
    shutil.rmtree(STAGE, ignore_errors=True)
    installed_paths, install_errors = install_outside()

    STATE.mkdir(parents=True, exist_ok=True)
    VERSION_FILE.write_text(release.sha256 + "\n", encoding="utf-8")
    lines = [
        f"promoted {release.version} ({release.sha256[:12]}): {', '.join(moved)}",
        f"installed: {', '.join(installed_paths) or 'nothing outside /aios'}",
    ]
    if install_errors:
        # Loud, because the machine now runs new modules behind an old login path.
        lines.append("COULD NOT INSTALL (the login path may still be the old one):")
        lines += [f"  {problem}" for problem in install_errors]
    lines += [
        f"previous copies kept at {PREVIOUS} — `aios.update rollback` restores them",
        "restart the session to run it: exec aios",
    ]
    return "\n".join(lines)


def install_outside(root: Path | None = None) -> tuple[list[str], list[str]]:
    """Put the boot, login and layout files where the system actually reads them.

    Copied rather than moved, so /aios/container stays a faithful record of what this
    node was given — and copied per file, so a read-only /sbin costs you that one file
    and a clear message, not the whole update.
    """
    root = root or ROOT
    done: list[str] = []
    problems: list[str] = []
    for relative, destination, mode in INSTALL:
        source = root / relative
        if not source.is_file():
            problems.append(f"{relative} missing from the payload")
            continue
        target = Path(destination)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write via a sibling temp then replace: the login script may be the very
            # file a session is about to exec, and a half-written one is unrunnable.
            staged = target.with_name(target.name + ".incoming")
            shutil.copyfile(source, staged)
            staged.chmod(mode)
            staged.replace(target)
            done.append(destination)
        except OSError as exc:
            problems.append(f"{destination}: {exc}")
    return done, problems


def swap_in(stage: Path) -> list[str]:
    """Move each payload entry into place, one rename at a time.

    ROOT is never renamed, and that is not a stylistic choice. On a real node
    `/aios/.aios` is a mount point (`/dev/vdb1 … type ext4`), and renaming a
    directory that contains a mount fails with EBUSY — so the obvious
    "rename the tree aside, move the new one in" would have failed on every machine
    that actually has persistent state, which is all of them.

    Swapping per entry has a second and better property: files this node created for
    itself are not in PAYLOAD, so they are neither moved nor deleted. An update
    replaces the code it shipped and nothing else.

    The gate has already passed by the time this runs, so a partial swap means the
    filesystem failed under us rather than the code being bad; the displaced copies
    in PREVIOUS are what `rollback()` puts back.
    """
    PREVIOUS.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for item in PAYLOAD:
        incoming = stage / item
        if not incoming.exists():
            continue
        target = ROOT / item
        kept = PREVIOUS / item
        _remove(kept)
        if target.exists():
            _move(target, kept)
        _move(incoming, target)
        moved.append(item)
    return moved


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink(missing_ok=True)


def _move(src: Path, dst: Path) -> None:
    """Rename when the filesystem allows it; copy when it does not.

    Two real failures on the same machine taught this, both reported as
    `OSError: [Errno 18] Cross-device link` despite both paths being on one mount:

      /aios       -> /aios.prev        because /aios contains a mounted volume
      /aios/aios  -> /aios.prev/aios   because overlayfs cannot rename a directory
                                       that still lives in the image's lower layer

    So a directory rename is not a primitive you can rely on inside a container.
    `shutil.move` falls back to copy-then-delete on EXDEV: slower, and correct. The
    destination is always removed first, or move would nest inside it.
    """
    try:
        src.rename(dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(dst))


def gate(candidate: Path) -> list[str]:
    """Run the candidate's own suites inside the candidate. Empty list means green."""
    failures = []
    env = {**os.environ, "PYTHONPATH": str(candidate), "PYTHONDONTWRITEBYTECODE": "1"}
    for name, argv in GATE:
        # A suite the candidate does not carry is skipped, not failed: an older
        # release predates it, and a rollback target you cannot install is not one.
        # Checked by looking for the file rather than by treating "no module named"
        # as absence — that string is equally what a broken import prints.
        module = argv[-1] if argv[-2] == "unittest" else ""
        if module.startswith("aios.") and not (
            candidate / "aios" / f"{module.split('.', 1)[1]}.py"
        ).is_file():
            continue
        try:
            done = subprocess.run(
                [sys.executable, *argv], cwd=candidate, env=env,
                capture_output=True, text=True, timeout=900,
                stdin=subprocess.DEVNULL,  # a gate that can block on a tty is not a gate
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            failures.append(f"{name}: could not run ({exc})")
            continue
        if done.returncode != 0:
            tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
            failures.append(f"{name}: {' / '.join(tail) or 'failed'}")
    return failures


def rollback() -> str:
    """Put back whatever the last apply displaced, entry by entry.

    Mirrors swap_in for the same reason: ROOT cannot be renamed on a node with a
    mounted state volume.
    """
    if not PREVIOUS.is_dir():
        raise UpdateError(f"nothing to roll back to — {PREVIOUS} does not exist")

    restored: list[str] = []
    for item in PAYLOAD:
        kept = PREVIOUS / item
        if not kept.exists():
            continue
        target = ROOT / item
        _remove(target)
        _move(kept, target)
        restored.append(item)

    if not restored:
        raise UpdateError(f"{PREVIOUS} holds none of the payload — nothing to restore")
    VERSION_FILE.unlink(missing_ok=True)
    return (
        f"rolled back: {', '.join(restored)}\nrestart the session to run it: exec aios"
    )


# --- a second consumer: polling this project's own git remote directly -------
#
# The peer path above (`check`/`apply`) is a manifest another AIos node published.
# That has no meaning for a lone node with no peer — there is nothing to publish to
# it. What such a node CAN always reach is the git remote its own code came from, so
# this is the same gate-then-swap machinery with git standing in for the manifest:
# `git ls-remote` in place of fetching `latest.json`, a shallow clone in place of
# fetching a tarball, and the branch's commit sha in place of a sha256 as the version
# identifier VERSION_FILE carries. gate()/swap_in()/install_outside() are unchanged —
# a git checkout of this repository already has every PAYLOAD entry at the paths
# swap_in expects, because that is simply this repository's own layout.
#
# Deliberately a separate pair of verbs (`git-check`/`git-apply`) rather than
# overloading `check`/`apply`: the two sources have incompatible version-identifier
# formats (git sha vs tarball sha256) and mixing them on one node would make
# `installed()` ambiguous about which scheme produced it.


def git_head(remote: str, branch: str) -> str:
    """The commit `branch` points at on `remote`, without cloning anything."""
    try:
        done = subprocess.run(
            ["git", "ls-remote", remote, f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise UpdateError(f"{remote}: {exc}") from None
    if done.returncode != 0:
        raise UpdateError(f"{remote}: {done.stderr.strip() or 'git ls-remote failed'}")
    if not done.stdout.strip():
        raise UpdateError(f"{remote}: no branch {branch!r}")
    return done.stdout.split()[0]


def check_git(remote: str, branch: str) -> str:
    head = git_head(remote, branch)
    here = installed()
    if head == here:
        return f"up to date ({head[:12]})"
    return f"update available: {head[:12]}\n  running: {here[:12] or 'unknown'}"


def apply_git(remote: str, branch: str, *, force: bool = False) -> str:
    """Same gate-then-swap as `apply()`, sourced from a git branch instead of a peer.

    A shallow clone rather than a fetch into an existing checkout: ROOT is not a git
    repository (it is what a tarball or, here, a clone was extracted into), so there
    is no history to fetch against — each run starts a fresh STAGE, exactly as the
    tarball path does.
    """
    head = git_head(remote, branch)
    if head == installed() and not force:
        return f"already running {head[:12]}"

    if STAGE.exists():
        shutil.rmtree(STAGE)

    try:
        done = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", "--branch", branch,
             remote, str(STAGE)],
            capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise UpdateError(f"{remote}: {exc}") from None
    if done.returncode != 0:
        shutil.rmtree(STAGE, ignore_errors=True)
        raise UpdateError(f"git clone failed: {done.stderr.strip()}")
    shutil.rmtree(STAGE / ".git", ignore_errors=True)  # not part of the payload

    missing = [item for item in PAYLOAD if not (STAGE / item).exists()]
    if missing:
        shutil.rmtree(STAGE)
        raise UpdateError(f"candidate {head[:12]} is missing: {', '.join(missing)}")

    failures = gate(STAGE)
    if failures:
        shutil.rmtree(STAGE)
        raise UpdateError(
            f"candidate {head[:12]} failed its own tests — not promoted:\n  "
            + "\n  ".join(failures)
        )

    moved = swap_in(STAGE)
    shutil.rmtree(STAGE, ignore_errors=True)
    installed_paths, install_errors = install_outside()

    STATE.mkdir(parents=True, exist_ok=True)
    VERSION_FILE.write_text(head + "\n", encoding="utf-8")
    lines = [
        f"promoted {head[:12]} ({branch}@{remote}): {', '.join(moved)}",
        f"installed: {', '.join(installed_paths) or 'nothing outside /aios'}",
    ]
    if install_errors:
        lines.append("COULD NOT INSTALL (the login path may still be the old one):")
        lines += [f"  {problem}" for problem in install_errors]
    lines.append(f"previous copies kept at {PREVIOUS} — `aios.update rollback` restores them")

    # Re-serve what was just adopted from upstream. A node that only pulls from git
    # is a dead end for every other node on this cluster: each of them would have to
    # reach GitHub itself, for code this one already fetched and already gated. This
    # is the existing peer half of the mechanism (`aios.repo serve` already publishes
    # AIOS_SRC_DIR at :8080/src) — a node that keeps itself current from upstream
    # becomes, for free, a peer other nodes can point AIOS_UPDATE_URL at instead.
    #
    # Best-effort: a publish failure must not turn a successful, already-gated
    # promotion into a reported failure. AIOS_SRC_DIR is a volume in the shipped
    # manifest (`aios-srv`); off that manifest it is a plain directory and still
    # works, just without surviving a pod recreate.
    try:
        lines.append(publish(Path(os.environ.get("AIOS_SRC_DIR", "/srv/aios/src")),
                              version=head[:12], source=ROOT))
    except (UpdateError, OSError) as exc:
        lines.append(f"NOT republished for peers: {exc}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "help"
    base = os.environ.get("AIOS_UPDATE_URL", "http://aios-repo:8080/src")
    try:
        if command == "check":
            print(check(base))
        elif command == "apply":
            print(apply(base, force="--force" in argv))
        elif command == "rollback":
            print(rollback())
        elif command == "publish":
            version = argv[1] if len(argv) > 1 else "dev"
            print(publish(Path(os.environ.get("AIOS_SRC_DIR", "/srv/aios/src")),
                          version=version))
        elif command == "gate":
            failures = gate(Path(argv[1]) if len(argv) > 1 else ROOT)
            print("green" if not failures else "RED:\n  " + "\n  ".join(failures))
            return 0 if not failures else 1
        elif command in ("git-check", "git-apply"):
            remote = os.environ.get("AIOS_GIT_REMOTE", "")
            if not remote:
                print("aios.update: AIOS_GIT_REMOTE not set", file=sys.stderr)
                return 1
            branch = os.environ.get("AIOS_GIT_BRANCH", "main")
            if command == "git-check":
                print(check_git(remote, branch))
            else:
                print(apply_git(remote, branch, force="--force" in argv))
        else:
            print(__doc__)
            print("usage: python3 -m aios.update {check|apply [--force]|rollback|"
                  "publish <version>|gate [dir]|git-check|git-apply [--force]}")
            return 2
    except UpdateError as exc:
        print(f"aios.update: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
