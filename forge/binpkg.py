"""Does a prebuilt package actually fit this machine? Answered before the build.

Portage already refuses a binary package whose USE flags disagree with what the
lockfile asked for — that is what `--binpkg-respect-use=y` does in
`portage.emerge_argv`. But it refuses *silently and late*, mid-emerge, and says
nothing about which flag was wrong or how close the peer came. On a network where
one node serves binaries to the rest, "why did this node rebuild instead of
reusing" is a question somebody asks every day. This module answers it from
metadata alone: no image is ever extracted and no compiler is involved.

The subtlety that makes a naive comparison useless: a binary package's `USE`
lists only the flags that were ENABLED, and it includes implicit ones (`arm64`,
`elibc_musl`, `kernel_linux`) that are not features of the package at all.
`IUSE` lists what the package OFFERS. So a package feature is disabled iff it
appears in IUSE and not in USE. tmux's lockfile entry names debug, systemd,
utempter and vim-syntax as disabled and none of them appear in USE — compare raw
USE against the lock and all four read as missing rather than as agreeing.

The second thing this does is cheaper than it looks. The lockfile records
*negative* intent ("-X  intent[0]: no X11"), and `NEEDED.ELF.2` records what the
installed binaries actually link. A package built with -X that still links
libX11 is the same class of defect as a vacuously-green probe
(skills/vacuous-probe-checks), caught from the other end: the flag says the
feature is absent, the ELF says otherwise. Believe the ELF.

What this trusts, stated plainly, because a green verdict here is what gates
`emerge --getbinpkg`: the package is a peer's file, fetched unauthenticated (see
`portage.binhost_env`), so every field below is that peer's claim. Two rules keep
a claim from becoming a certification. First, forge must read the same bytes
portage will: the container is identified by its tar header and never by a
trailer that can be appended to a valid archive, and the metadata member is
checked against the `Manifest` portage itself verifies. Second, a gap is never
silence: a variable that could not be read makes the verdict worse, and a USE
that could not be read makes it unfittable — an empty USE agrees with a lockfile
of negative decisions by accident, which would certify anything.
"""

from __future__ import annotations

import bz2
import hashlib
import io
import lzma
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from . import lock as lock_mod

DEFAULT_PKGDIR = "/var/cache/binpkgs"

#: Filenames a portage binary package cache uses. `.xpak` appears on caches
#: produced with `quickpkg --format=xpak`.
SUFFIXES = (".gpkg.tar", ".tbz2", ".xpak")

# --- bounds -----------------------------------------------------------------
# A binary package comes from a peer, which makes it untrusted input. Every read
# below is capped so a corrupt or hostile archive fails with a message instead of
# eating the machine. Real metadata for a large package is tens of KiB, so these
# limits are three orders of magnitude of headroom, not a tuning knob.
MAX_METADATA_ARCHIVE = 16 << 20  # the metadata.tar[.zst] member as stored
MAX_METADATA_BYTES = 64 << 20    # after decompression — the zip-bomb bound
MAX_FILE_BYTES = 4 << 20         # one metadata variable
MAX_FILES = 4096
MAX_TRAILING = 1 << 20           # padding scanned past a tar's end-of-archive marker
PEER_PROBE_BYTES = 4 << 20       # first slice of a peer's package we ask for
PEER_MAX_BYTES = 64 << 20
PEER_TIMEOUT = 20.0

#: Container formats understood. GLEP 78 numbers the layout in a marker member,
#: and a number this does not know is a container to report rather than to guess at.
GPKG_FORMATS = frozenset({"gpkg-1"})

#: Manifest hash names, strongest first, restricted to what `hashlib` computes.
#: Gentoo's BLAKE2B is blake2b-512, which is `hashlib.blake2b`'s default.
MANIFEST_HASHES = ("SHA512", "BLAKE2B", "SHA256")

EXACT, USABLE, MISMATCH, FOREIGN, ERROR = "exact", "usable", "mismatch", "foreign", "error"

#: Ordered worst-last so a verdict can only be made worse as evidence arrives.
_SEVERITY = {EXACT: 0, USABLE: 1, MISMATCH: 2, FOREIGN: 3, ERROR: 4}

#: Flags portage sets from the profile, not from an ebuild's IUSE. Never treated
#: as package features — the IUSE intersection already excludes them, and this
#: set exists only to recognise a *foreign* arch or libc.
ARCH_FLAGS = frozenset(
    "alpha amd64 amd64-fbsd arm arm64 hppa ia64 loong m68k mips ppc ppc64 riscv "
    "s390 sparc x86 x86-fbsd".split()
)
LIBC_FLAGS = frozenset(
    "elibc_glibc elibc_musl elibc_uclibc elibc_bionic elibc_Darwin elibc_SunOS "
    "elibc_FreeBSD elibc_mingw".split()
)

#: Flags a profile can force into USE without any ebuild offering them, beyond the
#: arch/libc/USE_EXPAND populations. Short by design: everything here is a flag whose
#: presence in USE must NOT be read as a package feature.
IMPLICIT_FLAGS = frozenset("prefix prefix-guest prefix-stack split-usr".split())

#: make.conf variables portage expands into ordinary USE flags, `KEY=value` ->
#: `key_value`. They are build policy exactly like USE — `lower.py` lets the agent
#: author them, `portage._make_conf` writes them verbatim, and portage rejects a
#: binpkg built for the wrong member the same way it rejects a wrong feature flag.
#: Reading only the literal `USE` entry made a package built against the wrong
#: python or the wrong -march read as merely "undecided".
USE_EXPAND = frozenset(
    "ABI_MIPS ABI_PPC ABI_RISCV ABI_S390 ABI_X86 ADA_TARGET ALSA_CARDS APACHE2_MODULES "
    "APACHE2_MPMS BINDIST_CARDS COLLECTD_PLUGINS CPU_FLAGS_ARM CPU_FLAGS_PPC "
    "CPU_FLAGS_X86 CURL_QUIC CURL_SSL ELIBC FFTOOLS GPSD_PROTOCOLS GRUB_PLATFORMS "
    "INPUT_DEVICES KERNEL L10N LCD_DEVICES LLVM_SLOT LLVM_TARGETS LUA_SINGLE_TARGET "
    "LUA_TARGETS NGINX_MODULES_HTTP NGINX_MODULES_MAIL NGINX_MODULES_STREAM "
    "OFFICE_IMPLEMENTATION OPENMPI_FABRICS PHP_TARGETS POSTGRES_TARGETS "
    "PYTHON_SINGLE_TARGET PYTHON_TARGETS QEMU_SOFTMMU_TARGETS QEMU_USER_TARGETS "
    "RUBY_TARGETS SANE_BACKENDS USERLAND UWSGI_PLUGINS VIDEO_CARDS "
    "XTABLES_ADDONS".split()
)

#: Group prefixes as they appear inside USE, longest first so `python_targets_`
#: cannot be mistaken for a member of a shorter group.
_EXPAND_PREFIXES = tuple(sorted((key.lower() for key in USE_EXPAND), key=len, reverse=True))

#: USE flag -> library sonames whose presence that flag is the reason for. Used
#: only in one direction: the flag is OFF and the library is linked anyway. Kept
#: deliberately small and high-confidence — a table full of guesses would drown
#: the finding it exists to surface. Prefixes, matched against the soname.
LIBRARY_EVIDENCE: dict[str, tuple[str, ...]] = {
    "X": ("libX11", "libXext", "libXft", "libXt.", "libXpm", "libXaw", "libSM", "libICE"),
    "wayland": ("libwayland-",),
    "gtk": ("libgtk-",),
    "gtk3": ("libgtk-3",),
    "qt5": ("libQt5",),
    "qt6": ("libQt6",),
    "alsa": ("libasound",),
    "pulseaudio": ("libpulse",),
    "jack": ("libjack",),
    "cups": ("libcups",),
    "bluetooth": ("libbluetooth",),
    "systemd": ("libsystemd",),
    "pam": ("libpam",),
    "selinux": ("libselinux",),
    "acl": ("libacl",),
    "caps": ("libcap.",),
    "gpm": ("libgpm",),
    "lua": ("liblua",),
    "perl": ("libperl",),
    "python": ("libpython",),
    "ruby": ("libruby",),
    "tcl": ("libtcl",),
    "jemalloc": ("libjemalloc",),
    "sixel": ("libsixel",),
    "utempter": ("libutempter",),
    "ssl": ("libssl", "libcrypto"),
    "gnutls": ("libgnutls",),
    "kerberos": ("libkrb5", "libgssapi"),
    "ldap": ("libldap",),
    "sqlite": ("libsqlite3",),
    "curl": ("libcurl",),
    "readline": ("libreadline",),
    "dbus": ("libdbus-",),
    "nls": ("libintl",),
    "zstd": ("libzstd",),
    "lz4": ("liblz4",),
    "bzip2": ("libbz2",),
    "lzma": ("liblzma",),
    "png": ("libpng",),
    "jpeg": ("libjpeg",),
}

#: Gentoo version suffix: everything from the first component that looks like a
#: version onwards. Enough to split `tmux-3.5a` and `vim-9.1.0866-r1`.
_PV = re.compile(
    r"^(?P<name>.+?)-(?P<version>\d+(?:\.\d+)*[a-z]?"
    r"(?:_(?:alpha|beta|pre|rc|p)\d*)*(?:-r\d+)?)$"
)
_FLAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_@-]*$")


class BinpkgError(Exception):
    """A binary package could not be read: wrong format, corrupt, or oversized."""


class UnsupportedFormat(BinpkgError):
    """The file is not a format this reads — say so rather than guessing."""


class MetadataNotFound(BinpkgError):
    """A well-formed archive that has no metadata member (or not yet: see `fetch`)."""


# --- what a package says about itself ---------------------------------------


@dataclass(frozen=True)
class Elf:
    """One entry from NEEDED.ELF.2: an installed binary and what it links."""

    path: str
    soname: str = ""
    rpath: str = ""
    needed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Metadata:
    """A binary package's build record, read without touching its image."""

    atom: str
    version: str = ""
    format: str = ""
    source: str = ""
    build_id: str = ""
    build_time: int = 0
    slot: str = ""
    use: frozenset[str] = frozenset()
    iuse: frozenset[str] = frozenset()
    cflags: str = ""
    cxxflags: str = ""
    chost: str = ""
    features: frozenset[str] = frozenset()
    needed: tuple[Elf, ...] = ()
    requires: tuple[str, ...] = ()
    size: int = 0
    archive_size: int = 0
    #: Everything this read could NOT determine. Printed, never silently dropped,
    #: and consumed by `fit`: a gap has to make the verdict worse than "exact".
    unread: tuple[str, ...] = ()
    #: Metadata variables that were absent, or present but unreadable (a symlink,
    #: a hardlink, or over the per-file cap). "Absent USE" and "USE I could not
    #: read" must not both arrive as an empty set of enabled flags.
    undetermined: frozenset[str] = frozenset()
    #: Signature members found in the container. Named, never verified here.
    signatures: tuple[str, ...] = ()

    @property
    def cpv(self) -> str:
        return f"{self.atom}-{self.version}" if self.version else self.atom

    @property
    def implicit(self) -> frozenset[str]:
        """Enabled flags that are not package features — arch, libc, USE_EXPAND."""
        return self.use - self.iuse

    def feature(self, flag: str) -> bool | None:
        """True/False if the package offers `flag`, None if it does not offer it.

        The whole IUSE intersection rule lives here: a flag absent from USE means
        "disabled" only when IUSE proves the package had the choice.
        """
        if flag not in self.iuse:
            return None
        return flag in self.use

    def sonames(self) -> frozenset[str]:
        """Every library any installed binary links, from whichever source exists."""
        linked = {lib for elf in self.needed for lib in elf.needed}
        return frozenset(linked | set(self.requires))

    def linkers_of(self, prefixes: tuple[str, ...]) -> list[tuple[str, str]]:
        """(binary, soname) for each ELF linking a library matching `prefixes`."""
        hits: list[tuple[str, str]] = []
        for elf in self.needed:
            for lib in elf.needed:
                if lib.startswith(prefixes):
                    hits.append((elf.path, lib))
        if not hits:
            # No NEEDED.ELF.2 (older or stripped metadata) but REQUIRES survives,
            # which names the sonames without naming which binary wants them.
            hits += [("(REQUIRES)", lib) for lib in self.requires if lib.startswith(prefixes)]
        return hits


# --- reading ----------------------------------------------------------------


@dataclass
class _Raw:
    """Metadata as name -> text, plus what the container itself told us."""

    files: dict[str, str] = field(default_factory=dict)
    oversize: list[str] = field(default_factory=list)
    #: Present in the metadata tar but not readable as a variable — a symlink or a
    #: hardlink. Kept apart from "absent": the value exists, this just cannot see it.
    unreadable: list[str] = field(default_factory=list)
    #: Sentences about the container itself: Manifest checked or not, signatures seen.
    notes: list[str] = field(default_factory=list)
    signatures: list[str] = field(default_factory=list)
    manifest: str = ""
    fmt: str = ""
    top: str = ""


def read(path: str | os.PathLike) -> Metadata:
    """Parse a gpkg or xpak binary package. Metadata only; the image is never read."""
    path = Path(path)
    try:
        size = path.stat().st_size
        handle = path.open("rb")
    except OSError as exc:
        raise BinpkgError(f"{path}: {exc.strerror or exc}") from None
    with handle:
        return read_stream(handle, source=str(path), archive_size=size)


def read_stream(fh, *, source: str, archive_size: int | None = None,
                head_only: bool = False) -> Metadata:
    """Parse from a seekable file object. `fetch` uses this on a downloaded slice.

    `head_only` says these bytes are a PREFIX whose length the fetcher chose, not
    a whole object. Everything that lives at the end of a package — the xpak
    trailer, the Manifest — is then absent by construction, and anything that
    looks like one at the cut is a coincidence at best: a peer can plant an xpak
    trailer at exactly the probe boundary and serve one file that reads clean to a
    ranged fetch and dirty to everything else. So a head is only ever read as a tar.
    """
    if archive_size is None:
        fh.seek(0, io.SEEK_END)
        archive_size = fh.tell()
    fh.seek(0)
    head = fh.read(512)
    fh.seek(0)

    # The tar header FIRST, always. A gpkg *is* a tar, and every tar reader stops
    # at the end-of-archive blocks and ignores what follows — so an xpak trailer
    # can be appended to a valid gpkg. Sniffing the trailer first meant ~120
    # appended bytes decided which metadata forge read while portage went on
    # reading the real metadata.tar, and a package whose USE contradicts the lock
    # reported `exact`. The reverse cannot happen: a genuine xpak .tbz2 begins
    # with bzip2/gzip/zstd magic, never with a tar header.
    if head[257:262] == b"ustar":
        return _assemble(_read_gpkg(fh, source, head_only=head_only), source, archive_size)

    if head_only:
        raise UnsupportedFormat(
            f"{source}: the first {archive_size} bytes are not a tar, and an xpak "
            "trailer cannot be read from a slice — it lives at the far end of the "
            "file. Fetching the whole object is the only honest way to read this one."
        )

    if archive_size >= 16:
        fh.seek(archive_size - 16)
        trailer = fh.read(16)
        fh.seek(0)
        if trailer[0:8] == b"XPAKSTOP" and trailer[12:16] == b"STOP":
            raw = _read_xpak(fh, archive_size, trailer, source)
            if source.endswith(".gpkg.tar"):
                # Named like a gpkg, shaped like an xpak: worth saying out loud,
                # since that combination is what a spoof attempt looks like.
                raw.notes.append(
                    "named .gpkg.tar but read as xpak — the file has no tar header, "
                    "so the name and the contents disagree"
                )
            return _assemble(raw, source, archive_size)

    raise UnsupportedFormat(
        f"{source}: not a binary package this reads — no tar header "
        f"(first bytes {head[:4].hex() or '(empty file)'}) and no xpak trailer. "
        "Understood: gpkg (a plain tar, usually .gpkg.tar) and xpak (.tbz2, .xpak)."
    )


def _read_gpkg(fh, source: str, *, head_only: bool = False) -> _Raw:
    """Stream the outer tar and stop at the metadata member.

    GLEP 78 fixes the order — `gpkg-1`, metadata, image — so streaming means the
    image is not merely left unextracted, it is never read at all. That order is
    also required rather than hoped for: the format marker has to arrive before the
    metadata this trusts (`_require_gpkg`), and the members that come after it are
    reached by a second header-only pass (`_reread`) instead of by reading forward
    through the image.
    """
    raw = _Raw(fmt="tar")
    try:
        with tarfile.open(fileobj=fh, mode="r|") as tf:
            for member in tf:
                name = member.name[2:] if member.name.startswith("./") else member.name
                top, slash, rest = name.partition("/")
                inner = rest if slash else top
                if slash:
                    raw.top = top
                if not inner or "/" in inner:
                    continue
                if inner.startswith("gpkg-"):
                    raw.fmt = inner  # "gpkg-1"
                    continue
                if inner.endswith((".sig", ".asc")):
                    raw.signatures.append(inner)
                    continue
                if inner == "Manifest":
                    # Some producers put it ahead of the metadata; take it while
                    # the stream is here rather than seeking back for it later.
                    if member.size <= MAX_FILE_BYTES:
                        raw.manifest = _bounded(
                            tf.extractfile(member), MAX_FILE_BYTES, f"{source}:Manifest"
                        ).decode("utf-8", "replace")
                    continue
                if not inner.startswith("metadata.tar"):
                    continue  # image.tar*
                if member.size > MAX_METADATA_ARCHIVE:
                    raise BinpkgError(
                        f"{source}: {inner} is {member.size} bytes, over the "
                        f"{MAX_METADATA_ARCHIVE} byte cap — refusing to read it"
                    )
                _require_gpkg(raw, inner, source)
                blob = _bounded(tf.extractfile(member), MAX_METADATA_ARCHIVE, f"{source}:{inner}")
                _authenticate(fh, raw, inner, blob, source, head_only=head_only)
                plain = _decompress(blob, inner[len("metadata.tar"):], f"{source}:{inner}")
                _load_metadata_tar(plain, raw, source)
                return raw
    except tarfile.TarError as exc:
        raise BinpkgError(f"{source}: corrupt or truncated archive: {exc}") from None
    except (EOFError, OSError) as exc:
        raise BinpkgError(f"{source}: archive ends mid-member: {exc}") from None

    raise MetadataNotFound(
        f"{source}: a tar with no metadata.tar member — not a binary package, "
        "or truncated before the metadata"
    )


def _require_gpkg(raw: _Raw, inner: str, source: str) -> None:
    """A tar carrying metadata is not yet a gpkg; the format marker says it is.

    GLEP 78 makes `gpkg-1` the first member, so requiring it before believing the
    metadata costs nothing on a real package and refuses two things that are not
    one: a hand-built tar wearing the layout, and a container version this code
    has never seen.
    """
    if raw.fmt in GPKG_FORMATS:
        return
    if raw.fmt == "tar":
        raise UnsupportedFormat(
            f"{source}: a tar holding {inner} but no gpkg format marker before it — "
            "GLEP 78 puts `gpkg-1` first, so this is not a binary package portage wrote"
        )
    raise UnsupportedFormat(
        f"{source}: container format {raw.fmt!r}, and this reads only "
        f"{'/'.join(sorted(GPKG_FORMATS))} — the package may be fine, this cannot tell"
    )


def _authenticate(fh, raw: _Raw, inner: str, blob: bytes, source: str, *,
                  head_only: bool) -> None:
    """Check the metadata member against the Manifest, and refuse appended bytes.

    Portage's own gpkg reader verifies every member against the Manifest and
    refuses the package on a mismatch. Metadata that disagrees with it is metadata
    portage would never act on, so believing it here is how two tools end up
    describing the same file differently — which is precisely the gap an appended
    xpak segment, or a swapped metadata member, lives in. This raises rather than
    notes, because unverifiable-and-wrong is a read failure, not a fit.
    """
    if head_only:
        raw.notes.append(
            "only the head of this object was fetched: the Manifest and any signature "
            "are past the slice, so this metadata is the peer's unchecked word"
        )
        return

    manifest, end, missing = _reread(fh, raw, source)
    manifest = raw.manifest or manifest
    if end >= 0:
        _refuse_trailing(fh, end, source)
    if not manifest:
        raw.notes.append(missing or (
            "no Manifest in this package — nothing certifies that the metadata below "
            "is the metadata portage would read"
        ))
        return
    raw.notes += _verify_manifest(manifest, inner, blob, source)


def _reread(fh, raw: _Raw, source: str) -> tuple[str, int, str]:
    """Second pass over the container's headers: the Manifest, and where the tar ends.

    Random-access mode seeks over member data instead of reading it, so this stays
    header-only — the image is skipped here exactly as it is skipped above. This is
    also the only pass that sees the members *after* the metadata, which is where
    GLEP 78 puts the signature.
    Returns (Manifest text, offset just past the last member or -1 if the container
    could not be walked, and a sentence for why there is no Manifest text).
    """
    try:
        fh.seek(0)
        with tarfile.open(fileobj=fh, mode="r") as tf:
            manifest = ""
            missing = ""
            for member in tf.getmembers():
                inner = member.name.rsplit("/", 1)[-1]
                if inner.endswith((".sig", ".asc")):
                    raw.signatures.append(inner)
                    continue
                if inner != "Manifest" or not member.isfile():
                    continue
                if member.size > MAX_FILE_BYTES:
                    missing = (f"the Manifest is {member.size} bytes, over the "
                               f"{MAX_FILE_BYTES} byte cap — metadata uncertified")
                    continue
                manifest = _bounded(
                    tf.extractfile(member), MAX_FILE_BYTES, f"{source}:Manifest"
                ).decode("utf-8", "replace")
            return manifest, tf.offset, missing
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        return "", -1, (f"the container could not be re-read to check its Manifest "
                        f"({exc}) — metadata uncertified")


def _refuse_trailing(fh, end: int, source: str) -> None:
    """Refuse non-zero bytes after the tar's end-of-archive marker.

    A tar ends in zero blocks and padding, and every reader stops there. That is
    what makes appending an xpak segment to a valid gpkg a *spoof* rather than a
    corruption: portage keeps reading the real metadata.tar and never sees the
    appendage. Nothing legitimate writes data there, so it is refused rather than
    parsed — the tar-header-first rule above already ignores it, and this says why.
    """
    fh.seek(0, io.SEEK_END)
    size = fh.tell()
    if size <= end:
        return
    if size - end > MAX_TRAILING:
        raise BinpkgError(
            f"{source}: {size - end} bytes follow the end of the tar archive — a tar "
            "reader ignores them, so whatever is in there is not what portage will read"
        )
    fh.seek(end)
    tail = fh.read(size - end)
    if tail.strip(b"\x00"):
        raise BinpkgError(
            f"{source}: {len(tail)} bytes are appended after the archive's "
            "end-of-archive marker, and they are not padding. A tar reader — portage "
            "included — ignores them and reads the real metadata.tar, so trusting them "
            "here would describe a different package. Refusing to read it."
        )


def _verify_manifest(text: str, member: str, blob: bytes, source: str) -> list[str]:
    """Size and digest of the metadata member as the Manifest states them."""
    entry = _manifest_entry(text, member)
    if entry is None:
        return [f"the Manifest does not cover {member} — its metadata is uncertified"]
    size, digests = entry
    if size is not None and size != len(blob):
        raise BinpkgError(
            f"{source}: the Manifest says {member} is {size} bytes, the archive holds "
            f"{len(blob)} — portage refuses a gpkg on this and so does this"
        )
    for name in MANIFEST_HASHES:
        want = digests.get(name)
        if not want:
            continue
        got = _digest(name, blob)
        if got != want.lower():
            raise BinpkgError(
                f"{source}: {member} does not match the Manifest ({name} {got[:16]}… "
                f"claimed {want.lower()[:16]}…) — the metadata is not the metadata this "
                "package is signed and checksummed for"
            )
        return []
    return [
        f"the Manifest covers {member} with no hash this can compute "
        f"({', '.join(sorted(digests)) or 'none listed'}) — metadata uncertified"
    ]


def _digest(name: str, blob: bytes) -> str:
    if name == "BLAKE2B":
        return hashlib.blake2b(blob).hexdigest()  # Gentoo's BLAKE2B is blake2b-512
    return hashlib.new(name.lower(), blob).hexdigest()


def _manifest_entry(text: str, member: str) -> tuple[int | None, dict[str, str]] | None:
    """`DATA metadata.tar 4096 BLAKE2B ab.. SHA512 cd..` -> (4096, {...})."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[1].rsplit("/", 1)[-1] != member:
            continue
        try:
            size: int | None = int(parts[2])
        except ValueError:
            size = None
        rest = parts[3:] if size is not None else parts[2:]
        digests = {rest[i].upper(): rest[i + 1] for i in range(0, len(rest) - 1, 2)}
        return size, digests
    return None


def _load_metadata_tar(blob: bytes, raw: _Raw, source: str) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r|") as tf:
            for member in tf:
                name = member.name[2:] if member.name.startswith("./") else member.name
                if name.startswith("metadata/"):
                    name = name[len("metadata/"):]
                if not name or "/" in name:
                    continue
                if not member.isfile():
                    # A symlink or hardlink named USE is not an absent USE — the
                    # value is elsewhere and this deliberately does not follow it.
                    # Skipping it silently is what let `USE` read as "no flags
                    # enabled", which agrees with a lockfile of -flags by accident.
                    raw.unreadable.append(name)
                    continue
                if len(raw.files) >= MAX_FILES:
                    raise BinpkgError(
                        f"{source}: metadata holds more than {MAX_FILES} files"
                    )
                if member.size > MAX_FILE_BYTES:
                    raw.oversize.append(name)
                    continue
                data = _bounded(tf.extractfile(member), MAX_FILE_BYTES, f"{source}:{name}")
                raw.files[name] = data.decode("utf-8", "replace")
    except tarfile.TarError as exc:
        raise BinpkgError(f"{source}: metadata tar is corrupt: {exc}") from None


def _read_xpak(fh, size: int, trailer: bytes, source: str) -> _Raw:
    """Parse the xpak segment appended to an old-style .tbz2.

    Layout, back from EOF: "XPAKSTOP" + uint32(segment length) + "STOP", where
    the segment is "XPAKPACK" + uint32(index len) + uint32(data len) + index +
    data + "XPAKSTOP". The bzip2'd tarball in front of it is never touched — the
    metadata is stored uncompressed, which is why this format needs no codec.
    """
    raw = _Raw(fmt="xpak")
    infosize = int.from_bytes(trailer[8:12], "big")
    if not 24 <= infosize <= MAX_METADATA_ARCHIVE or infosize + 8 > size:
        raise BinpkgError(
            f"{source}: xpak trailer claims a {infosize} byte segment in a "
            f"{size} byte file — corrupt"
        )
    fh.seek(size - (infosize + 8))
    header = fh.read(16)
    if header[:8] != b"XPAKPACK":
        raise BinpkgError(f"{source}: xpak trailer present but the XPAKPACK header is not")
    index_len = int.from_bytes(header[8:12], "big")
    data_len = int.from_bytes(header[12:16], "big")
    if index_len + data_len + 24 != infosize:
        raise BinpkgError(
            f"{source}: xpak sizes disagree (index {index_len} + data {data_len} "
            f"+ 24 != {infosize})"
        )
    index = fh.read(index_len)
    data = fh.read(data_len)
    if len(index) != index_len or len(data) != data_len:
        raise BinpkgError(f"{source}: xpak segment is truncated")

    pos = 0
    while pos < index_len:
        if pos + 4 > index_len:
            raise BinpkgError(f"{source}: xpak index ends mid-entry")
        name_len = int.from_bytes(index[pos:pos + 4], "big")
        pos += 4
        if name_len == 0 or pos + name_len + 8 > index_len:
            raise BinpkgError(f"{source}: xpak index entry has an impossible name length")
        name = index[pos:pos + name_len].decode("utf-8", "replace")
        pos += name_len
        offset = int.from_bytes(index[pos:pos + 4], "big")
        length = int.from_bytes(index[pos + 4:pos + 8], "big")
        pos += 8
        if length > MAX_FILE_BYTES:
            raw.oversize.append(name)
            continue
        if offset + length > data_len:
            raise BinpkgError(f"{source}: xpak entry {name!r} points outside the segment")
        if len(raw.files) >= MAX_FILES:
            raise BinpkgError(f"{source}: xpak holds more than {MAX_FILES} entries")
        raw.files[name] = data[offset:offset + length].decode("utf-8", "replace")
    return raw


def _bounded(fileobj, limit: int, what: str) -> bytes:
    if fileobj is None:
        raise BinpkgError(f"{what}: not a regular file")
    blob = fileobj.read(limit + 1)
    if len(blob) > limit:
        raise BinpkgError(f"{what}: exceeds the {limit} byte cap")
    return blob


def _decompress(blob: bytes, suffix: str, what: str) -> bytes:
    """Inflate a metadata member, bounded. An unknown codec is reported, not guessed.

    Every decoder failure is converted here. `zlib.error` and `lzma.LZMAError`
    subclass Exception rather than OSError, so a corrupt gz or xz member used to
    escape every guard between here and `cli.main` — and because `cmd_binpkg`
    examines a whole directory in one comprehension, one poisoned package on a
    binhost aborted the entire scan with a traceback instead of printing one ERROR
    row. (bz2 avoided it only by raising OSError, which was caught by accident.)
    """
    if suffix in ("", ".tar"):
        return blob  # a real gpkg variant: BINPKG_COMPRESS unset
    if suffix == ".zst":
        return _unzstd(blob, MAX_METADATA_BYTES, what)
    limit = MAX_METADATA_BYTES + 1
    try:
        if suffix == ".gz":
            plain = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(blob, limit)
        elif suffix == ".bz2":
            plain = bz2.BZ2Decompressor().decompress(blob, max_length=limit)
        elif suffix in (".xz", ".lzma"):
            plain = lzma.LZMADecompressor().decompress(blob, max_length=limit)
        else:
            raise UnsupportedFormat(
                f"{what}: metadata is compressed with {suffix!r} and there is no decoder "
                "for it here (stdlib covers gz/bz2/xz; zstd needs Python 3.14 or the "
                "zstd binary). The package may be fine — this cannot tell."
            )
    except (zlib.error, lzma.LZMAError, EOFError, OSError, ValueError) as exc:
        raise BinpkgError(f"{what}: the {suffix.lstrip('.')} stream is corrupt: {exc}") from None
    return _cap(plain, what)


def _cap(blob: bytes, what: str) -> bytes:
    if len(blob) > MAX_METADATA_BYTES:
        raise BinpkgError(f"{what}: decompresses to more than {MAX_METADATA_BYTES} bytes")
    return blob


def _unzstd(blob: bytes, limit: int, what: str) -> bytes:
    """zstd via the stdlib if this Python has it, else the zstd binary.

    `compression.zstd` only exists from 3.14. The AIos node runs 3.14.6 so it
    takes the first path, but the same code has to run on whatever python a
    developer's host has, and a package it cannot decompress must produce a
    sentence rather than a traceback.
    """
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        pass
    else:
        try:
            with zstd.ZstdFile(io.BytesIO(blob)) as fh:
                return _cap(fh.read(limit + 1), what)
        except BinpkgError:
            raise  # the cap, which is a verdict about the package and not a codec fault
        except (zstd.ZstdError, EOFError, OSError, ValueError) as exc:
            raise BinpkgError(f"{what}: zstd stream is corrupt: {exc}") from None

    for name, args in (("zstd", ["-dcq"]), ("zstdcat", ["-q"])):
        exe = shutil.which(name)
        if exe is None:
            continue
        # Via a temp file, not a pipe: writing 16 MiB into stdin while not
        # draining stdout deadlocks, and this has to stop reading at the cap.
        with tempfile.NamedTemporaryFile(prefix="forge-binpkg-", suffix=".zst") as tmp:
            tmp.write(blob)
            tmp.flush()
            proc = subprocess.Popen(
                [exe, *args, tmp.name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                out = proc.stdout.read(limit + 1)
            finally:
                proc.stdout.close()
                proc.terminate()
                err = proc.stderr.read(4096).decode("utf-8", "replace")
                proc.stderr.close()
                proc.wait(timeout=30)
        if not out:
            raise BinpkgError(f"{what}: {name} decompressed nothing: {err.strip()}")
        return _cap(out, what)

    raise UnsupportedFormat(
        f"{what}: metadata is zstd-compressed and nothing here can decompress it "
        "— this Python predates `compression.zstd` (3.14) and neither `zstd` nor "
        "`zstdcat` is on PATH. Install app-arch/zstd, or read this package on the node."
    )


def _assemble(raw: _Raw, source: str, archive_size: int) -> Metadata:
    files = raw.files
    unread: list[str] = list(raw.notes)
    #: A variable this could not read. Absence and unreadability land in the same
    #: set on purpose: both mean "no value", and neither may be reported as one.
    undetermined: set[str] = set()
    for name in raw.oversize:
        unread.append(f"{name}: larger than the {MAX_FILE_BYTES} byte per-file cap, not read")
        undetermined.add(name)
    for name in raw.unreadable:
        unread.append(f"{name}: present but not a regular file (symlink or hardlink), not read")
        undetermined.add(name)

    def determined(name: str, absent: str) -> str | None:
        """The variable's text, or None — with `absent` recorded — when there is none."""
        if name in undetermined:
            return None  # already explained by the oversize/unreadable pass
        if name not in files:
            undetermined.add(name)
            unread.append(absent)
            return None
        return files[name]

    category = files.get("CATEGORY", "").strip()
    pf = files.get("PF", "").strip()
    build_id = files.get("BUILD_ID", "").strip()
    if not pf and raw.top:
        pf, guessed_id = _strip_build_id(raw.top)
        build_id = build_id or guessed_id
        unread.append(f"no PF in metadata — name taken from the container dir {raw.top!r}")
    name, version = _split_pf(pf)
    if not category:
        unread.append("no CATEGORY in metadata — the atom is a guess from the filename")
        category = _category_from_path(source)
    atom = f"{category}/{name}" if category and name else (name or "unknown")

    use = _tokens(determined(
        "USE", "no USE in metadata — nothing can be said about enabled flags"
    ) or "")
    iuse = frozenset(flag.lstrip("+-") for flag in _tokens(determined(
        "IUSE",
        "no IUSE in metadata — a flag missing from USE cannot be told apart "
        "from one this package never offered",
    ) or ""))

    needed = _parse_needed_elf2(files.get("NEEDED.ELF.2", "")) or _parse_needed(
        files.get("NEEDED", "")
    )
    requires = tuple(sorted(_tokens(files.get("REQUIRES", "")) - {"arm64:", "amd64:", "x86:"}))
    if not needed and not requires:
        unread.append(
            "no NEEDED.ELF.2, NEEDED or REQUIRES — what the binaries link is unknown, "
            "so a flag that lies about a linked library cannot be caught here"
        )

    chost = (determined(
        "CHOST", "no CHOST in metadata — cannot prove this was built for this machine"
    ) or "").strip()
    if "CHOST" in files and not chost:
        undetermined.add("CHOST")
        unread.append("CHOST in metadata is empty — cannot prove this targets this machine")
    # Signatures are NOT recorded as a gap: a signed package is strictly better
    # evidence than an unsigned one, and downgrading it for carrying a signature
    # this cannot verify would punish the peer for doing more. `fit` says out loud
    # that it went unchecked.

    return Metadata(
        atom=atom,
        version=version,
        format=raw.fmt,
        source=source,
        build_id=build_id,
        build_time=_int(files.get("BUILD_TIME", "")),
        slot=files.get("SLOT", "").strip(),
        use=use,
        iuse=iuse,
        cflags=" ".join(files.get("CFLAGS", "").split()),
        cxxflags=" ".join(files.get("CXXFLAGS", "").split()),
        chost=chost,
        features=_tokens(files.get("FEATURES", "")),
        needed=needed,
        requires=requires,
        size=_int(files.get("SIZE", "")),
        archive_size=archive_size,
        unread=tuple(unread),
        undetermined=frozenset(undetermined),
        signatures=tuple(sorted(set(raw.signatures))),
    )


def _tokens(text: str) -> frozenset[str]:
    return frozenset(text.split())


def _int(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        return 0


def _strip_build_id(top: str) -> tuple[str, str]:
    """`tmux-3.5a-1` -> (`tmux-3.5a`, `1`). Only when PF itself is missing."""
    head, _, tail = top.rpartition("-")
    if head and tail.isdigit():
        return head, tail
    return top, ""


def _split_pf(pf: str) -> tuple[str, str]:
    match = _PV.match(pf)
    if not match:
        return pf, ""
    return match.group("name"), match.group("version")


def _category_from_path(source: str) -> str:
    """A binhost stores packages as `<category>/<pf>.gpkg.tar`; use that if metadata lost it."""
    parent = Path(urllib.parse.urlparse(source).path or source).parent.name
    return parent if "-" in parent and "/" not in parent else ""


def _parse_needed_elf2(text: str) -> tuple[Elf, ...]:
    """One ELF per line: `<multilib cat>;<path>;<soname>;<rpath>;<needed,...>`."""
    out: list[Elf] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(";")
        if len(parts) < 5:
            continue
        out.append(
            Elf(
                path=parts[1],
                soname=parts[2],
                rpath=parts[3],
                needed=tuple(lib for lib in parts[4].split(",") if lib),
            )
        )
    return tuple(out)


def _parse_needed(text: str) -> tuple[Elf, ...]:
    """The older `NEEDED`: `<path> <lib>,<lib>`. No soname, no rpath."""
    out: list[Elf] = []
    for line in text.splitlines():
        path, _, libs = line.strip().partition(" ")
        if not path:
            continue
        out.append(Elf(path=path, needed=tuple(lib for lib in libs.split(",") if lib)))
    return tuple(out)


# --- fitting ----------------------------------------------------------------


@dataclass(frozen=True)
class Reason:
    """One piece of evidence, naming what it was read from."""

    # flag | chost | arch | libc | link | cflags | extra | expand | unknown | note | error
    kind: str
    text: str


@dataclass(frozen=True)
class Fit:
    verdict: str
    atom: str
    reasons: tuple[Reason, ...] = ()
    meta: Metadata | None = None
    source: str = ""

    def of_kind(self, *kinds: str) -> tuple[Reason, ...]:
        return tuple(r for r in self.reasons if r.kind in kinds)

    @property
    def contradictions(self) -> tuple[Reason, ...]:
        """USE says the feature is off; the ELF says the library is linked."""
        return self.of_kind("link")

    @property
    def ok(self) -> bool:
        """Fit, fully understood, and every enabled feature justified by an intent.

        This is the exit code of `forge binpkg`, and the exit code is a *prediction
        that emerge will reuse the package*. So the bar is portage's, not a softer
        one: `--binpkg-respect-use=y` compares every IUSE flag and rebuilds on any
        difference, which means a package with an enabled feature the lockfile
        never decided (`extra`) is one emerge will refuse — and a package whose
        IUSE could not be read (`unknown`) is one it cannot even compare. Both used
        to exit 0 under a "usable" verdict, promising a reuse that would not happen.
        A feature with no `why` is also the thing README rule 2 exists to prevent.
        """
        return self.verdict in (EXACT, USABLE) and not self.of_kind("link", "extra", "unknown")


@dataclass(frozen=True)
class Decision:
    """One USE flag the lockfile decided, and how specifically it decided it."""

    flag: str
    enabled: bool
    why: str
    #: "package" — the lock names this flag on this atom's line.
    #: "make.conf" — a global default that applies to whatever offers the flag.
    scope: str = "package"
    #: For a USE_EXPAND member, the group it belongs to ("python_single_target").
    group: str = ""
    #: True when that group admits exactly one member, so any other member being
    #: the enabled one is a mismatch rather than a difference of taste.
    exclusive: bool = False


def decisions(lock: dict, atom: str) -> dict[str, Decision]:
    """Every USE flag the lockfile decided for `atom`.

    Global `make.conf` USE counts: `-X` there is exactly as much an intent as
    `-X` on the package line, and portage applies it to any package with an X
    flag. The scope is kept because the two are not equally binding — a global
    `+ssl` asks for ssl wherever it exists, while `+ssl` on the package line
    asks *this* package for it and is unsatisfiable if the package has no such
    flag. Losing that distinction makes every package without a globally-enabled
    flag report as a mismatch, which is exactly the nonsense this command exists
    to replace.

    USE_EXPAND variables count too, and for the same reason. `lower.py` lets the
    agent author any make.conf key outside SPEC_OWNED/FORBIDDEN, `portage._make_conf`
    writes it verbatim, and portage expands `PYTHON_SINGLE_TARGET=python3_12` into
    the ordinary IUSE flag `python_single_target_python3_12` — which it will reject
    a binpkg over. Reading only the literal `USE` entry meant a package built
    against a different interpreter, or for a different -march, showed up as an
    undifferentiated "the lockfile never decided this".
    """
    out: dict[str, Decision] = {}
    for entry in lock.get("make_conf", []):
        key = str(entry.get("key", "")).strip()
        why = str(entry.get("why", ""))
        if key == "USE":
            for token in str(entry.get("value", "")).split():
                flag = token.lstrip("+-")
                if not _FLAG.match(flag):
                    continue  # `*`, `-*`, or prose that leaked into the value
                out[flag] = Decision(
                    flag=flag,
                    enabled=not token.startswith("-"),
                    why=f"make.conf USE — {why}",
                    scope="make.conf",
                )
        elif key in USE_EXPAND:
            group = key.lower()
            for token in str(entry.get("value", "")).split():
                flag = f"{group}_{token.lstrip('+-')}"
                if not _FLAG.match(flag):
                    continue
                out[flag] = Decision(
                    flag=flag,
                    enabled=not token.startswith("-"),
                    why=f"make.conf {key} — {why}",
                    scope="make.conf",
                    group=group,
                    exclusive=key.endswith("_SINGLE_TARGET"),
                )
    try:
        pkg = lock_mod.package(lock, atom)
    except lock_mod.LockError:
        return out
    for flag in pkg.get("use", []):
        name = str(flag["flag"])
        group = _expand_group(name)  # a package line may name an expanded flag directly
        out[name] = Decision(
            flag=name, enabled=bool(flag["enabled"]), why=str(flag.get("why", "")),
            group=group, exclusive=group.endswith("_single_target"),
        )
    return out


def fit(meta: Metadata, lock: dict, atom: str | None = None) -> Fit:
    """Compare a built package against the lockfile's decisions."""
    atom = atom or meta.atom
    system = lock.get("system", {})
    make_conf = {e["key"]: e for e in lock.get("make_conf", [])}
    reasons: list[Reason] = []
    verdict = EXACT

    def worsen(to: str) -> None:
        nonlocal verdict
        if _SEVERITY[to] > _SEVERITY[verdict]:
            verdict = to

    # 0. USE is the one field nothing can substitute for. Unread, it arrives as an
    #    empty set of enabled flags — which "agrees" with a lockfile made mostly of
    #    negative decisions, and so certifies anything. There is no fit to compute.
    if "USE" in meta.undetermined:
        return Fit(
            verdict=ERROR,
            atom=atom,
            reasons=(Reason(
                "error",
                "USE could not be read from this package, so which features were "
                "compiled in is unknown. An empty USE agrees with every -flag in the "
                "lockfile by accident, so this cannot be fitted at all — see below.",
            ),),
            meta=meta,
            source=meta.source,
        )

    # 1. Can it be installed at all. CHOST first: it is the one field that makes
    #    a package unusable rather than merely wrong.
    want_chost = str(system.get("chost") or make_conf.get("CHOST", {}).get("value", ""))
    if meta.chost and want_chost and meta.chost != want_chost:
        reasons.append(Reason("chost", f"built for {meta.chost}, this machine is {want_chost}"))
        worsen(FOREIGN)

    want_arch = str(system.get("keyword", ""))
    built_arch = meta.use & ARCH_FLAGS
    if want_arch and built_arch and want_arch not in built_arch:
        reasons.append(
            Reason("arch", f"built for {'/'.join(sorted(built_arch))}, this machine is {want_arch}")
        )
        worsen(FOREIGN)

    want_libc = "elibc_" + str(system.get("libc", ""))
    built_libc = meta.use & LIBC_FLAGS
    if system.get("libc") and built_libc and want_libc not in built_libc:
        reasons.append(
            Reason("libc", f"built against {'/'.join(sorted(built_libc))}, this machine is {want_libc}")
        )
        worsen(FOREIGN)

    # 2. The flags the lock decided. `meta.feature` applies the IUSE
    #    intersection, so an absent-from-USE flag reads as disabled only when
    #    IUSE proves the package offered it.
    decided = decisions(lock, atom)
    if not any(p["atom"] == atom for p in lock.get("packages", [])):
        reasons.append(
            Reason("note", f"the lockfile does not name {atom} — compared against "
                           "make.conf USE and CHOST only")
        )
        worsen(USABLE)

    for flag in sorted(decided):
        decision = decided[flag]
        built = meta.feature(flag)
        if built is None:
            # The package does not offer the flag — but "not offered" is unknowable
            # in one direction only. USE naming the flag is direct proof the feature
            # was compiled in, whatever IUSE says, which is exactly the case the
            # IUSE intersection was built for: an ebuild that dropped the flag from
            # IUSE between the peer's build and this lock. Reading it as "cannot
            # tell" made a package built +systemd against a lock that disables
            # systemd report `exact` with no mention of systemd anywhere.
            if not decision.enabled and flag in meta.use:
                group = _profile_set(flag)
                if group:
                    reasons.append(
                        Reason("note", f"the lockfile disables {flag}, which is {group} rather "
                                       f"than a feature of this package — not judged  "
                                       f"({decision.why})")
                    )
                    worsen(USABLE)
                else:
                    reasons.append(
                        Reason("flag", f"want -{flag}, built +{flag} — {flag} is not in this "
                                       f"package's IUSE, but its USE names it, so the feature "
                                       f"is in the build (USE is authoritative about what was "
                                       f"enabled; IUSE only tells 'disabled' from 'not "
                                       f"offered')  ({decision.why})")
                    )
                    worsen(MISMATCH)
                continue
            # A decision aimed at this atom is unsatisfiable; a global default
            # simply does not apply to a package without the flag.
            if decision.enabled and decision.scope == "package":
                reasons.append(
                    Reason("unknown", f"want +{flag}, but {flag} is not in this package's IUSE "
                                      f"— cannot be satisfied by it  ({decision.why})")
                )
                worsen(MISMATCH)
            continue
        if built != decision.enabled:
            reasons.append(
                Reason(
                    "flag",
                    f"want {'+' if decision.enabled else '-'}{flag}, built "
                    f"{'+' if built else '-'}{flag}  ({decision.why})",
                )
            )
            worsen(MISMATCH)

    # 2b. Mutually exclusive USE_EXPAND groups. PYTHON_SINGLE_TARGET and friends
    #     admit exactly one member, so a package built against another member is
    #     not "undecided", it is the wrong interpreter — and portage rejects it.
    wrong_member: set[str] = set()
    for group, wanted in _exclusive_groups(decided).items():
        built_members = {f for f in meta.use & meta.iuse if _expand_group(f) == group}
        wrong = sorted(built_members - wanted)
        if not wrong:
            continue
        wrong_member.update(wrong)
        reasons.append(
            Reason("flag", f"built {group.upper()}={' '.join(_member(f, group) for f in wrong)}, "
                           f"the lockfile asks for "
                           f"{' '.join(sorted(_member(f, group) for f in wanted))} — this group "
                           f"admits one member  ({decided[sorted(wanted)[0]].why})")
        )
        worsen(MISMATCH)

    # 3. Enabled features the lock never mentioned. Split in two, because the two
    #    halves mean opposite things. A plain feature with no decision has no `why`
    #    — README rule 2 — and `--binpkg-respect-use=y` will reject the package for
    #    it, so it cannot count as reusable. A USE_EXPAND member (python_targets_*,
    #    abi_*, cpu_flags_*) is set by the profile for nearly every package, and the
    #    lockfile does not record the profile; judging those would mean no real
    #    package is ever `exact`, which is the nonsense this command replaces.
    #    Membership in IUSE is not the test for "is a feature": USE naming a flag is,
    #    and the flags that legitimately appear in USE without any ebuild offering
    #    them are a fixed, known population (`_profile_set`). Filtering on IUSE
    #    instead let an enabled flag disappear from the report entirely whenever the
    #    package's IUSE had drifted — the same blind spot as (2) above, one step down.
    undecided = {flag for flag in meta.use
                 if flag not in decided and (flag in meta.iuse or not _profile_set(flag))}
    undecided -= wrong_member
    extra = sorted(f for f in undecided if not _expand_group(f))
    expanded = sorted(f for f in undecided if _expand_group(f))
    if extra:
        reasons.append(
            Reason("extra", "built +" + ", +".join(extra) + " — no decision in the lockfile "
                            "justifies these, and `emerge --binpkg-respect-use=y` "
                            "(portage.emerge_argv) rebuilds rather than reuses a package whose "
                            "enabled flags differ from the ones computed here")
        )
        worsen(USABLE)
    if expanded:
        reasons.append(
            Reason("expand", "built +" + ", +".join(expanded) + " — USE_EXPAND members the "
                             "profile sets and the lockfile does not record; reported, not judged")
        )

    if not meta.iuse:
        reasons.append(
            Reason("unknown", "no IUSE in this package: agreement on the lock's disabled "
                              "flags could not be verified, and portage cannot compare it "
                              "either — it rebuilds instead")
        )
        worsen(USABLE)

    # 4. Compiler flags. Reported because a peer built with -march=native is worth
    #    knowing about, but portage does not reject on them and neither does this.
    want_cflags = " ".join(str(make_conf.get("CFLAGS", {}).get("value", "")).split())
    if meta.cflags and want_cflags and meta.cflags != want_cflags:
        reasons.append(
            Reason("cflags", f"CFLAGS {meta.cflags!r}, lock says {want_cflags!r} "
                             "— reported, not a mismatch")
        )
    want_cxx = " ".join(str(make_conf.get("CXXFLAGS", {}).get("value", "")).split())
    want_cxx = want_cxx.replace("${CFLAGS}", want_cflags).replace("$CFLAGS", want_cflags)
    if meta.cxxflags and want_cxx and meta.cxxflags != want_cxx:
        reasons.append(
            Reason("cflags", f"CXXFLAGS {meta.cxxflags!r}, lock says {want_cxx!r} "
                             "— reported, not a mismatch")
        )

    reasons += contradictions(meta, decided)

    # 5. Signatures, said out loud either way: this checks the Manifest digest, not
    #    OpenPGP, and an AIos binhost runs with -binpkg-request-signature anyway.
    if meta.signatures:
        reasons.append(
            Reason("note", f"carries {', '.join(meta.signatures)} — a signature nothing here "
                           "verifies; the Manifest digest over metadata.tar is what was checked")
        )

    # 6. Everything the read could not determine. "exact" has to mean every field
    #    this compares was read and agreed — not that everything readable agreed.
    #    Missing CHOST alone belongs here: it is the only proof of the target.
    if meta.unread:
        reasons.append(
            Reason("note", f"{len(meta.unread)} thing(s) about this package could not be "
                           f"determined (listed below), so this is not `exact`")
        )
        worsen(USABLE)

    return Fit(verdict=verdict, atom=atom, reasons=tuple(reasons), meta=meta, source=meta.source)


def _expand_group(flag: str) -> str:
    """The USE_EXPAND group a flag belongs to, or "" — `python_targets_python3_13`."""
    for group in _EXPAND_PREFIXES:
        if flag.startswith(group + "_") and len(flag) > len(group) + 1:
            return group
    return ""


def _member(flag: str, group: str) -> str:
    return flag[len(group) + 1:]


def _exclusive_groups(decided: dict[str, Decision]) -> dict[str, set[str]]:
    """group -> the members the lockfile enabled, for one-member-only groups."""
    groups: dict[str, set[str]] = {}
    for decision in decided.values():
        if decision.exclusive and decision.enabled and decision.group:
            groups.setdefault(decision.group, set()).add(decision.flag)
    return groups


def _profile_set(flag: str) -> str:
    """Why a flag can legitimately be in USE and not in IUSE, or "" if it cannot.

    Portage puts arch, libc, kernel, userland and USE_EXPAND members into USE from
    the profile, and no ebuild lists them in IUSE. A lockfile entry naming one is a
    nonsense entry to report, not evidence about how the package was built — which
    is the one exception to "USE proves the feature is in the build".

    Deliberately a fixed list rather than the package's own IUSE_EFFECTIVE: this
    decides whether a lock-disabled feature found in USE is a mismatch, so letting
    the package widen the exempt population would hand it the verdict.
    """
    if flag in ARCH_FLAGS:
        return "the profile's arch flag"
    if flag in LIBC_FLAGS:
        return "the profile's libc flag"
    if flag in IMPLICIT_FLAGS:
        return "a profile-forced implicit flag"
    if flag.startswith(("kernel_", "userland_", "elibc_", "abi_")):
        return "a profile-set implicit flag"
    group = _expand_group(flag)
    return f"a {group.upper()} member the profile sets" if group else ""


def contradictions(meta: Metadata, decided: dict[str, Decision]) -> list[Reason]:
    """Flags the lock turned OFF whose library the binaries link anyway.

    The lockfile's negative intent is a claim about the finished system: `-X`
    means no X11 in it. USE is only a claim about how configure was called.
    NEEDED.ELF.2 is the finished system talking, so where the two disagree this
    reports the ELF and says the flag is not evidence.
    """
    found: list[Reason] = []
    for flag in sorted(decided):
        decision = decided[flag]
        if decision.enabled or meta.feature(flag) is True:
            continue  # only the "switched off, yet linked" case is a contradiction
        prefixes = LIBRARY_EVIDENCE.get(flag)
        if not prefixes:
            continue
        hits = meta.linkers_of(prefixes)
        if not hits:
            continue
        where = ", ".join(f"{path} -> {lib}" for path, lib in hits[:4])
        if len(hits) > 4:
            where += f", +{len(hits) - 4} more"
        found.append(
            Reason(
                "link",
                f"built -{flag} yet the binaries link it: {where}  ({decision.why}) "
                "— the USE flag disagrees with the ELF; believe the ELF",
            )
        )
    return found


# --- sources ----------------------------------------------------------------


def walk(directory: str | os.PathLike) -> list[Path]:
    """Every binary package under `directory`, sorted. Nothing else is opened."""
    root = Path(directory)
    if not root.is_dir():
        raise BinpkgError(f"{root} is not a directory")
    found = [p for p in root.rglob("*") if p.is_file() and p.name.endswith(SUFFIXES)]
    return sorted(found)


def examine(source: str, lock: dict, *, atom: str | None = None,
            timeout: float = PEER_TIMEOUT) -> Fit:
    """Read one package and fit it, turning any read failure into a verdict."""
    try:
        meta = fetch(source, timeout=timeout) if _is_url(source) else read(source)
    except BinpkgError as exc:
        return Fit(verdict=ERROR, atom=atom or Path(source).name,
                   reasons=(Reason("error", str(exc)),), source=source)
    return fit(meta, lock, atom)


def _is_url(source: str) -> bool:
    return urllib.parse.urlparse(source).scheme in ("http", "https")


def peer_sources(url: str, *, timeout: float = PEER_TIMEOUT) -> list[str]:
    """Package URLs on a peer's binhost, from its `Packages` index.

    A URL naming a package file is taken as-is; anything else is treated as a
    binhost root and its index is read. The index is a peer's text and therefore
    untrusted: a PATH that tries to leave the binhost is dropped, not fetched.
    """
    _require_http(url)
    if url.endswith(SUFFIXES):
        return [url]
    base = url.rstrip("/")
    index = _http_get(f"{base}/Packages", timeout=timeout, limit=PEER_MAX_BYTES)
    out: list[str] = []
    for rel in _index_paths(index.decode("utf-8", "replace")):
        if rel.startswith("/") or ".." in rel.split("/") or "://" in rel or "\\" in rel:
            print(f"forge binpkg: dropped index entry {rel!r} — it points outside {base}")
            continue
        out.append(f"{base}/{rel}")
    return out


def _index_paths(text: str) -> list[str]:
    """Relative paths from a `Packages` index. Pre-PATH indexes get one derived."""
    paths: list[str] = []
    for stanza in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in stanza.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
        if "CPV" not in fields:
            continue  # the header stanza
        if fields.get("PATH"):
            paths.append(fields["PATH"])
        else:
            paths.append(fields["CPV"] + ".tbz2")  # the pre-gpkg layout
    return paths


def fetch(url: str, *, timeout: float = PEER_TIMEOUT,
          probe_bytes: int = PEER_PROBE_BYTES, cap: int = PEER_MAX_BYTES) -> Metadata:
    """Read a peer's package over HTTP without downloading the image.

    GLEP 78 puts the metadata near the front, so the first slice of the file is
    normally the whole answer — this asks for exactly that with a Range request
    and only falls back to the full object when the metadata was not in it. An
    xpak .tbz2 always takes the fallback: its trailer is at the other end.
    """
    _require_http(url)
    blob = _http_get(url, timeout=timeout, limit=probe_bytes, first=probe_bytes)
    # A short answer was the whole object; a full one may be a slice of a larger
    # file, and a slice's tail is a boundary the fetcher chose rather than the end
    # of anything. `read_stream` is told which it has.
    partial = len(blob) >= probe_bytes
    try:
        return read_stream(io.BytesIO(blob), source=url, archive_size=len(blob),
                           head_only=partial)
    except BinpkgError:
        # A short answer was the whole object, so the failure is the real one.
        # A full one means the metadata was simply further in than we asked for.
        if not partial:
            raise
    blob = _http_get(url, timeout=timeout, limit=cap)
    return read_stream(io.BytesIO(blob), source=url, archive_size=len(blob))


def _require_http(url: str) -> None:
    if not _is_url(url):
        raise BinpkgError(f"{url}: only http:// and https:// peers are fetched")


class _PinnedRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that leaves the peer the operator named.

    urllib follows 3xx transparently, and the default opener will follow one to any
    host and to ftp://. The peer is untrusted by this module's own admission, so
    that made `forge binpkg --peer` a server-side request forgery primitive: a
    binhost could 302 a package fetch at 169.254.169.254, or at an admin port on
    the node's own loopback, and forge would fetch it and hand back the body.

    Pinned to the same origin rather than filtered by address: an AIos binhost is
    normally a LAN or cluster name (`aios-repo:8080`, 127.0.0.1 in the tests), so a
    private-address blocklist would refuse the legitimate case and still allow a
    redirect sideways to another host on the same LAN. Same-origin allows a
    trailing-slash or path redirect and nothing else.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        refuse = BinpkgError(
            f"{req.full_url}: refused an HTTP {code} redirect to {newurl} — a peer does "
            "not get to send this fetch to another host, scheme or port"
        )
        if urllib.parse.urlparse(newurl).scheme not in ("http", "https"):
            raise refuse
        if _origin(newurl) != _origin(req.full_url):
            raise refuse
        return super().redirect_request(req, fp, code, msg, headers, newurl)


#: One opener, built once. `build_opener` drops the default redirect handler in
#: favour of the subclass above.
_OPENER = urllib.request.build_opener(_PinnedRedirect())


def _origin(url: str) -> tuple[str, str, int]:
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise BinpkgError(f"{url}: only http:// and https:// are fetched")
    return parts.scheme, (parts.hostname or "").lower(), parts.port or (
        443 if parts.scheme == "https" else 80
    )


def _http_get(url: str, *, timeout: float, limit: int, first: int | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "forge-binpkg"})
    if first is not None:
        request.add_header("Range", f"bytes=0-{first - 1}")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            blob = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        exc.close()  # it holds the response body open, and nothing here wants it
        raise BinpkgError(f"{url}: HTTP {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BinpkgError(f"{url}: {exc}") from None
    if first is not None:
        # Python's own http.server, and plenty of others, ignore Range and send
        # the whole object. Keeping the head and dropping the connection is the
        # same result for a fraction of the bytes — and NOT an error, which is
        # what a package larger than the probe window would otherwise become.
        return blob[:first]
    if len(blob) > limit:
        raise BinpkgError(f"{url}: larger than the {limit} byte fetch cap")
    return blob


# --- output -----------------------------------------------------------------


_MARK = {EXACT: "exact", USABLE: "usable", MISMATCH: "mismatch", FOREIGN: "FOREIGN", ERROR: "ERROR"}


def render(fits: list[Fit]) -> list[str]:
    """The compact table, then every reason, then a summary line."""
    lines = _table(fits)
    for item in fits:
        detail = _detail(item)
        if detail:
            lines.append("")
            lines += detail
    lines.append("")
    lines.append(_summary(fits))
    lines += _provenance(fits)
    return lines


def _provenance(fits: list[Fit]) -> list[str]:
    """Say who the evidence came from when any of it came over the wire.

    `forge build --peer` is the caller, and an AIos binhost is unsigned by
    construction — `portage.binhost_env` sets FEATURES=-binpkg-request-signature
    over plain http. So a verdict about a peer's package is a verdict about what
    that peer said. The Manifest check catches a mangled package, not a lying one.
    """
    peers = sorted({urllib.parse.urlparse(item.source).netloc
                    for item in fits if _is_url(item.source)})
    if not peers:
        return []
    return [
        f"read over plain HTTP from {', '.join(peers)} — unsigned, so these verdicts are "
        "only as trustworthy as that peer. metadata.tar is checked against the package's "
        "own Manifest where the whole object was fetched, which catches a corrupted "
        "package, not a dishonest one."
    ]


def _table(fits: list[Fit]) -> list[str]:
    rows = []
    for item in fits:
        meta = item.meta
        # A contradicted package must not read as "exact" in a column somebody
        # skims — the flags fit, the linked libraries do not.
        mark = _MARK.get(item.verdict, item.verdict)
        rows.append(
            (
                f"{mark} !link" if item.contradictions else mark,
                meta.cpv if meta else item.atom,
                f"#{meta.build_id}" if meta and meta.build_id else "",
                _bytes(meta.size) if meta and meta.size else "",
                meta.chost if meta else "",
            )
        )
    head = ("verdict", "package", "build", "size", "chost")
    widths = [max(len(head[i]), *(len(r[i]) for r in rows)) if rows else len(head[i])
              for i in range(len(head))]
    out = ["  ".join(head[i].ljust(widths[i]) for i in range(len(head))).rstrip()]
    for row in rows:
        out.append("  ".join(row[i].ljust(widths[i]) for i in range(len(row))).rstrip())
    return out


def _detail(item: Fit) -> list[str]:
    meta = item.meta
    if not item.reasons and not (meta and meta.unread):
        return []
    head = f"{meta.cpv if meta else item.atom}  {_MARK.get(item.verdict, item.verdict)}"
    if item.contradictions:
        head += " but contradicted by its own ELFs"
    lines = [f"{head}  {item.source or (meta.source if meta else '')}"]
    for reason in item.reasons:
        mark = "!!" if reason.kind == "link" else "  "
        lines.append(f"{mark} {reason.kind:<8} {reason.text}")
    for note in meta.unread if meta else ():
        lines.append(f"   {'undetermined':<8} {note}")
    return lines


def _summary(fits: list[Fit]) -> str:
    counts: dict[str, int] = {}
    for item in fits:
        counts[item.verdict] = counts.get(item.verdict, 0) + 1
    links = sum(1 for item in fits if item.contradictions)
    parts = [f"{counts[v]} {v}" for v in (EXACT, USABLE, MISMATCH, FOREIGN, ERROR) if v in counts]
    tail = f", {links} contradicting its own USE flags" if links else ""
    reusable = sum(1 for item in fits if item.ok)
    return f"{reusable}/{len(fits)} reusable on this machine: {', '.join(parts)}{tail}"


def _bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{count}B"
