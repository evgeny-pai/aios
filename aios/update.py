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
VERSION_FILE = STATE / "code-version"
MANIFEST = "latest.json"

#: What a node's code actually consists of. Deliberately not the whole directory:
#: .aios is per-node state on a mounted volume and must never travel in an update.
PAYLOAD = ("aios", "forge", "probes", "tests", "overlay", "skills",
           "aios.toml", "README.md", "DESIGN.md")

#: Run inside a candidate before it may be promoted. Both suites, because an update
#: that breaks the build tool is as bad as one that breaks the agent.
GATE = (
    ("forge", ("-m", "unittest", "discover", "-s", "tests", "-t", ".")),
    ("agent", ("-m", "unittest", "aios.test_agent")),
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

    # Carry per-node state across: it belongs to the machine, not to the code.
    if STATE.exists() and not (STAGE / ".aios").exists():
        (STAGE / ".aios").symlink_to(STATE)

    if PREVIOUS.exists():
        shutil.rmtree(PREVIOUS)
    ROOT.rename(PREVIOUS)
    STAGE.rename(ROOT)

    STATE.mkdir(parents=True, exist_ok=True)
    VERSION_FILE.write_text(release.sha256 + "\n", encoding="utf-8")
    return (
        f"promoted {release.version} ({release.sha256[:12]}); previous tree kept at "
        f"{PREVIOUS}\nrestart the session to run it: exec aios"
    )


def gate(candidate: Path) -> list[str]:
    """Run the candidate's own suites inside the candidate. Empty list means green."""
    failures = []
    env = {**os.environ, "PYTHONPATH": str(candidate), "PYTHONDONTWRITEBYTECODE": "1"}
    for name, argv in GATE:
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
    if not PREVIOUS.is_dir():
        raise UpdateError(f"nothing to roll back to — {PREVIOUS} does not exist")
    scratch = Path(f"{ROOT}.rollback")
    if scratch.exists():
        shutil.rmtree(scratch)
    ROOT.rename(scratch)
    PREVIOUS.rename(ROOT)
    shutil.rmtree(scratch)
    VERSION_FILE.unlink(missing_ok=True)
    return f"rolled back to the tree in {PREVIOUS}; restart the session to run it"


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
        else:
            print(__doc__)
            print("usage: python3 -m aios.update {check|apply [--force]|rollback|"
                  "publish <version>|gate [dir]}")
            return 2
    except UpdateError as exc:
        print(f"aios.update: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
