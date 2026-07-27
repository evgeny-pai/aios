"""This node's relationship to the ebuild repository — as consumer, or as source.

A stage3 ships no ebuild repository, so a fresh AIos node cannot emerge anything at
all. It needs a tree and a profile. There are two ways to get them, and this module
is both:

    sync    fetch a Gentoo snapshot and lay down /var/db/repos/gentoo + a profile
    serve   publish this node's tree and binary packages to other AIos nodes

`serve` is the interesting half. Baking a 1.4 GB tree into every image is waste when
one node can hold it and the rest can mount it over HTTP — and once a node is
serving, its binary package cache is shared too, which is what makes feature
minimization affordable at all: `forge minimize` rebuilds a package once per lever,
and without a shared binhost every node pays that cost from scratch.

So the first node to sync becomes the seed of the network. Everything after it is
cheap.

Stdlib only — urllib to fetch, tarfile to unpack, http.server to serve. Nothing here
may add a dependency to a machine whose only interpreter is portage's own python3.
"""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: Ordered by measured throughput, not by preference. distfiles.gentoo.org has been
#: seen at both 5 MB/s and 57 KB/s within an hour, so a single hardcoded mirror is a
#: coin flip — try the next one rather than waiting out a slow one.
MIRRORS = (
    "https://mirror.leaseweb.com/gentoo/snapshots/gentoo-latest.tar.xz",
    "https://distfiles.gentoo.org/snapshots/gentoo-latest.tar.xz",
    "https://ftp.fau.de/gentoo/snapshots/gentoo-latest.tar.xz",
)

REPOS = Path("/var/db/repos")
TREE = REPOS / "gentoo"
BINPKGS = Path("/var/cache/binpkgs")
PROFILE_LINK = Path("/etc/portage/make.profile")
SERVE_PORT = 8080


class RepoError(Exception):
    """Message is user-facing."""


@dataclass(frozen=True)
class Profile:
    """Which profile a spec's arch/libc implies.

    Portage will not resolve a single package without make.profile pointing at a
    real directory, and the failure ("profile is broken") names nothing useful.
    """

    arch: str = "arm64"
    libc: str = "musl"
    version: str = "23.0"

    def candidates(self) -> tuple[Path, ...]:
        base = TREE / "profiles" / "default" / "linux" / self.arch / self.version
        # Newer trees nest musl under the version; older ones suffix it. Try both
        # rather than guessing, since the layout has changed across 17.0/23.0.
        return (
            base / self.libc if self.libc != "glibc" else base,
            Path(f"{base}-{self.libc}") if self.libc != "glibc" else base,
            base,
        )

    def resolve(self) -> Path:
        for path in self.candidates():
            if path.is_dir():
                return path
        raise RepoError(
            f"no profile for {self.arch}/{self.libc} {self.version} under {TREE}/profiles — "
            f"tried: {', '.join(str(p) for p in self.candidates())}"
        )


def _fetch(url: str, dest: Path) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": "aios-repo/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as out:
        return sum(out.write(chunk) for chunk in iter(lambda: response.read(1 << 20), b""))


#: A real tree has this many ebuilds at minimum; the actual figure is ~35,000. The
#: check exists because an agent that cannot emerge will try to *fabricate* a repo to
#: satisfy portage — one did, hand-writing profiles/default/linux/arm64/23.0/musl and
#: a make.defaults, producing a directory that is non-empty, plausible, and contains
#: zero ebuilds. Trusting "the directory exists" made this sync a no-op and left the
#: fake in place, so emptiness is not the test. Substance is.
MIN_EBUILDS = 1000


def looks_real(tree: Path = TREE) -> tuple[bool, str]:
    """Is this an actual Gentoo repository, or something shaped like one?"""
    if not tree.is_dir():
        return False, "absent"
    if not (tree / "profiles" / "repo_name").is_file():
        return False, "no profiles/repo_name"
    found = 0
    for _ in tree.glob("*-*/*/*.ebuild"):
        found += 1
        if found >= MIN_EBUILDS:
            return True, f"{found}+ ebuilds"
    return False, f"only {found} ebuilds — fabricated or truncated"


def sync(*, profile: Profile | None = None, force: bool = False) -> str:
    """Lay down an ebuild repository and a profile. Idempotent unless `force`."""
    profile = profile or Profile()
    real, why = looks_real()
    if real and not force:
        return f"{TREE} already populated ({why}) — pass force to replace it"
    if not real and TREE.is_dir():
        # Replacing a fake is the normal case, not an exception worth stopping for.
        print(f"replacing unusable tree at {TREE} ({why})", file=sys.stderr)

    REPOS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(REPOS)) as scratch:
        archive = Path(scratch) / "snapshot.tar.xz"
        errors = []
        for url in MIRRORS:
            try:
                size = _fetch(url, archive)
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                errors.append(f"{url.split('/')[2]}: {exc}")
        else:
            raise RepoError("every mirror failed:\n  " + "\n  ".join(errors))

        # The snapshot's top-level directory is `portage`, not `gentoo`.
        with tarfile.open(archive) as tar:
            tar.extractall(scratch, filter="tar")
        unpacked = next(p for p in Path(scratch).iterdir() if p.is_dir() and p.name != "gentoo")

        staged = REPOS / "gentoo.incoming"
        if staged.exists():
            shutil.rmtree(staged)
        unpacked.rename(staged)
        if TREE.exists():
            shutil.rmtree(TREE)
        staged.rename(TREE)

    target = profile.resolve()
    PROFILE_LINK.parent.mkdir(parents=True, exist_ok=True)
    if PROFILE_LINK.is_symlink() or PROFILE_LINK.exists():
        PROFILE_LINK.unlink()
    PROFILE_LINK.symlink_to(target)

    ebuilds = sum(1 for _ in TREE.glob("*/*/*.ebuild"))
    return (
        f"synced {size / 1e6:.0f} MB snapshot -> {TREE} ({ebuilds} ebuilds)\n"
        f"profile {PROFILE_LINK} -> {target}"
    )


def serve(port: int = SERVE_PORT) -> None:
    """Publish this node's tree and binary packages to the rest of the network.

    Read-only by construction: SimpleHTTPRequestHandler serves GET/HEAD and nothing
    else, so a peer can consume the tree but never alter it. That asymmetry is the
    point — one node is the source of truth and the others are clients.
    """
    import functools
    import http.server
    import socketserver

    root = Path("/srv/aios")
    root.mkdir(parents=True, exist_ok=True)
    for name, target in (("gentoo", TREE), ("binpkgs", BINPKGS)):
        link = root / name
        target.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(target)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("0.0.0.0", port), handler) as httpd:
        print(f"aios repo serving {root} on :{port}", flush=True)
        print(f"  tree     http://<this-node>:{port}/gentoo/", flush=True)
        print(f"  binpkgs  http://<this-node>:{port}/binpkgs/", flush=True)
        httpd.serve_forever()


def client_config(host: str, port: int = SERVE_PORT) -> str:
    """The portage config a peer needs to consume another node's repo.

    Rendered rather than hand-written so every node on the network points at the same
    place in the same way.
    """
    return (
        f"# Generated by aios.repo — this node consumes {host}:{port}\n"
        f'PORTAGE_BINHOST="http://{host}:{port}/binpkgs"\n'
        'FEATURES="${FEATURES} getbinpkg buildpkg binpkg-multi-instance"\n'
        'EMERGE_DEFAULT_OPTS="${EMERGE_DEFAULT_OPTS} --binpkg-respect-use=y"\n'
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "help"
    try:
        if command == "sync":
            print(sync(
                profile=Profile(
                    arch=os.environ.get("AIOS_PROFILE_ARCH", "arm64"),
                    libc=os.environ.get("AIOS_PROFILE_LIBC", "musl"),
                ),
                force="--force" in argv,
            ))
        elif command == "serve":
            serve(int(os.environ.get("AIOS_REPO_PORT", SERVE_PORT)))
        elif command == "client":
            print(client_config(argv[1] if len(argv) > 1 else "aios-repo"))
        else:
            print(__doc__)
            print("usage: python3 -m aios.repo {sync [--force]|serve|client [host]}")
            return 2
    except RepoError as exc:
        print(f"aios.repo: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
