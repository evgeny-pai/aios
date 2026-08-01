"""Offline tests for `forge binpkg`: real archives, no mocked parser.

Every fixture here is a binary package this file builds with `tarfile` and
`struct`-shaped bytes — a gpkg is a plain tar and an xpak .tbz2 is a blob with a
trailer, so both are cheap to author honestly. Nothing stubs the reader: a test
that mocks the parser cannot catch the one bug that matters here, which is
comparing raw USE against the lockfile without intersecting IUSE.

    python3 -m unittest tests.test_binpkg -v
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import shutil
import tarfile
import tempfile
import threading
import unittest
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from forge import binpkg as binpkg_mod
from forge import lock as lock_mod
from forge import portage as portage_mod
from forge.cli import main

# --- fixtures ---------------------------------------------------------------

SYSTEM = {
    "name": "t",
    "arch": "aarch64",
    "libc": "musl",
    "chost": "aarch64-unknown-linux-musl",
    "keyword": "arm64",
    "cflags": "-O2 -pipe",
    "makeopts": "-j8",
    "features": "buildpkg",
    "init": "dinit",
    "profile": "default",
}

#: app-misc/tmux-3.5a-1 as this machine actually built it. USE carries only the
#: enabled flags plus the implicit arm64/elibc_musl/kernel_linux; every feature
#: the lockfile disables is in IUSE and absent from USE.
TMUX = {
    "CATEGORY": "app-misc\n",
    "PF": "tmux-3.5a\n",
    "BUILD_ID": "1\n",
    "BUILD_TIME": "1785171285\n",
    "SLOT": "0\n",
    "SIZE": "1259008\n",
    "USE": "arm64 elibc_musl kernel_linux\n",
    "IUSE": "debug jemalloc selinux sixel systemd utempter vim-syntax\n",
    "IUSE_EFFECTIVE": "arm64 debug elibc_musl jemalloc kernel_linux selinux sixel "
                      "systemd utempter vim-syntax\n",
    "CFLAGS": "-O2 -pipe\n",
    "CXXFLAGS": "-O2 -pipe\n",
    "CHOST": "aarch64-unknown-linux-musl\n",
    "FEATURES": "assume-digests binpkg-docompress binpkg-dostrip binpkg-logs "
                "binpkg-multi-instance buildpkg\n",
    "NEEDED.ELF.2": "arm64;/usr/bin/tmux;;;libtinfow.so.6,libevent_core-2.1.so.7,libc.so;\n",
    "REQUIRES": "arm64: libc.so libevent_core-2.1.so.7 libtinfow.so.6\n",
    "RDEPEND": "sys-libs/ncurses:0= dev-libs/libevent:=\n",
    "tmux-3.5a.ebuild": "# a stub; the reader must not care what is in here\n",
}

TMUX_LOCK_USE = [
    {"flag": "debug", "enabled": False, "why": "no intent asked for debug builds"},
    {"flag": "systemd", "enabled": False, "why": "target init is dinit, not systemd"},
    {"flag": "utempter", "enabled": False, "why": "no intent asked for utmp records"},
    {"flag": "vim-syntax", "enabled": False, "why": "no intent asked for tmux.conf syntax"},
]


def meta_tar(files: dict[str, str]) -> bytes:
    """The inner metadata tar: one file per variable, under metadata/."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, text in files.items():
            blob = text.encode("utf-8")
            info = tarfile.TarInfo(f"metadata/{name}")
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))
    return buf.getvalue()


def manifest_for(members: list[tuple[str, bytes]]) -> bytes:
    """A gpkg Manifest as portage writes it — and verifies it on read.

    Real digests, because the reader now checks them: metadata portage would refuse
    is metadata forge must not describe. Tests that want a tampered package pass
    `manifest=` explicitly.
    """
    lines = [
        f"DATA {name} {len(blob)} BLAKE2B {hashlib.blake2b(blob).hexdigest()} "
        f"SHA512 {hashlib.sha512(blob).hexdigest()}"
        for name, blob in members
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def gpkg(
    directory: Path,
    *,
    files: dict[str, str],
    basename: str = "tmux-3.5a-1",
    metadata_name: str = "metadata.tar",
    metadata: bytes | None = None,
    marker: str | None = "gpkg-1",
    image: bytes = b"",
    manifest: bytes | None = None,
    extra_members: list[tuple[str, bytes]] | None = None,
    append: bytes = b"",
    truncate_to: int | None = None,
) -> Path:
    """Write a real gpkg tar. `image.tar.zst` is deliberate garbage: nothing reads it."""
    path = directory / f"{basename}.gpkg.tar"
    body = metadata if metadata is not None else meta_tar(files)
    image = image or (b"\x00\xff" * 32768)
    covered = [(metadata_name, body), ("image.tar.zst", image)]
    entries = ([(marker, b"")] if marker else []) + [covered[0]]
    entries += extra_members or []   # GLEP 78 puts the signature next to its member
    entries.append(covered[1])
    entries.append(("Manifest", manifest if manifest is not None else manifest_for(covered)))
    with tarfile.open(path, mode="w") as tf:
        for name, blob in entries:
            info = tarfile.TarInfo(f"{basename}/{name}")
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))
    if truncate_to is not None:
        raw = path.read_bytes()
        path.write_bytes(raw[:truncate_to] if truncate_to >= 0 else raw[:truncate_to])
    if append:
        with path.open("ab") as fh:
            fh.write(append)
    return path


def xpak_segment(files: dict[str, str]) -> bytes:
    """The bytes an xpak .tbz2 carries after its tarball, trailer included."""
    index = b""
    data = b""
    for name, text in files.items():
        blob = text.encode("utf-8")
        index += (
            len(name).to_bytes(4, "big")
            + name.encode("utf-8")
            + len(data).to_bytes(4, "big")
            + len(blob).to_bytes(4, "big")
        )
        data += blob
    segment = (
        b"XPAKPACK"
        + len(index).to_bytes(4, "big")
        + len(data).to_bytes(4, "big")
        + index
        + data
        + b"XPAKSTOP"
    )
    return segment + len(segment).to_bytes(4, "big") + b"STOP"


def xpak_tbz2(directory: Path, *, files: dict[str, str], basename: str = "tmux-3.5a") -> Path:
    """The pre-gpkg format: a compressed tarball with an uncompressed xpak appended."""
    import bz2

    tarball = bz2.compress(b"pretend this is the image tarball; it is never read")
    path = directory / f"{basename}.tbz2"
    path.write_bytes(tarball + xpak_segment(files))
    return path


def meta_tar_unreadable(files: dict[str, str], name: str, *, link: str) -> bytes:
    """A metadata tar where `name` is a symlink: present, not readable, NOT absent.

    The real value sits in the link target, which the reader deliberately does not
    follow — so this is the shape that used to make `USE` read as "no flags enabled".
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for key, text in files.items():
            if key == name:
                info = tarfile.TarInfo(f"metadata/{key}")
                info.type = tarfile.SYMTYPE
                info.linkname = link
                tf.addfile(info)
                blob = text.encode("utf-8")
                target = tarfile.TarInfo(f"metadata/{link}")
                target.size = len(blob)
                tf.addfile(target, io.BytesIO(blob))
                continue
            blob = text.encode("utf-8")
            info = tarfile.TarInfo(f"metadata/{key}")
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))
    return buf.getvalue()


def write_lock(
    directory: Path,
    *,
    packages: list[dict],
    global_use: str = "-X -wayland -gtk -systemd -pam",
    system: dict | None = None,
    cflags: str = "-O2 -pipe",
    extra_make_conf: list[dict] | None = None,
) -> Path:
    lock = {
        "schema": lock_mod.SCHEMA,
        "spec_digest": "sha256:" + "0" * 64,
        "generated_by": {"provider": "echo", "model": "fixture"},
        "system": system or SYSTEM,
        "intents": [],
        "make_conf": [
            {"key": "CFLAGS", "value": cflags, "why": "spec: system.cflags"},
            {"key": "CHOST", "value": (system or SYSTEM)["chost"],
             "why": "spec: system.arch + system.libc"},
            {"key": "CXXFLAGS", "value": "${CFLAGS}", "why": "spec: system.cflags"},
            {"key": "USE", "value": global_use, "why": "intent[0]: no X11 anywhere"},
        ] + (extra_make_conf or []),
        "packages": packages,
        "minimized": {},
        "notes": [],
    }
    return lock_mod.save(lock, directory / "aios.lock.json")


def pkg(atom: str, use: list[dict], why: str = "intent[0]: a fixture") -> dict:
    return {"atom": atom, "why": why, "use": use, "accept_keywords": [], "probes": []}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-binpkg-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def tmux_lock(self, **kwargs) -> dict:
        path = write_lock(self.tmp, packages=[pkg("app-misc/tmux", TMUX_LOCK_USE)], **kwargs)
        return lock_mod.load(path)

    def tmux_lock_path(self, **kwargs) -> Path:
        return write_lock(self.tmp, packages=[pkg("app-misc/tmux", TMUX_LOCK_USE)], **kwargs)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = main(list(argv))
        return code, buf.getvalue()


# --- reading ----------------------------------------------------------------


class TestRead(Fixture):
    def test_reads_gpkg_metadata(self):
        meta = binpkg_mod.read(gpkg(self.tmp, files=TMUX))
        self.assertEqual(meta.atom, "app-misc/tmux")
        self.assertEqual(meta.version, "3.5a")
        self.assertEqual(meta.cpv, "app-misc/tmux-3.5a")
        self.assertEqual(meta.build_id, "1")
        self.assertEqual(meta.build_time, 1785171285)
        self.assertEqual(meta.chost, "aarch64-unknown-linux-musl")
        self.assertEqual(meta.cflags, "-O2 -pipe")
        self.assertEqual(meta.size, 1259008)
        self.assertEqual(meta.format, "gpkg-1")
        self.assertIn("buildpkg", meta.features)
        self.assertEqual(meta.use, frozenset({"arm64", "elibc_musl", "kernel_linux"}))
        self.assertIn("vim-syntax", meta.iuse)
        self.assertEqual(meta.unread, ())

    def test_needed_elf2_is_one_entry_per_elf(self):
        files = dict(
            TMUX,
            **{
                "NEEDED.ELF.2": (
                    "arm64;/usr/bin/tmux;;;libtinfow.so.6,libc.so;\n"
                    "arm64;/usr/lib/libfoo.so.1;libfoo.so.1;/usr/lib;libc.so;\n"
                )
            },
        )
        meta = binpkg_mod.read(gpkg(self.tmp, files=files))
        self.assertEqual([elf.path for elf in meta.needed],
                         ["/usr/bin/tmux", "/usr/lib/libfoo.so.1"])
        self.assertEqual(meta.needed[1].soname, "libfoo.so.1")
        self.assertEqual(meta.needed[1].rpath, "/usr/lib")
        self.assertIn("libtinfow.so.6", meta.sonames())

    def test_implicit_flags_are_not_package_features(self):
        meta = binpkg_mod.read(gpkg(self.tmp, files=TMUX))
        self.assertEqual(meta.implicit, frozenset({"arm64", "elibc_musl", "kernel_linux"}))
        for implicit in ("arm64", "elibc_musl", "kernel_linux"):
            self.assertIsNone(meta.feature(implicit), f"{implicit} read as a package feature")

    def test_iuse_intersection_tells_disabled_from_not_offered(self):
        meta = binpkg_mod.read(gpkg(self.tmp, files=TMUX))
        self.assertIs(meta.feature("systemd"), False)   # offered, not enabled
        self.assertIsNone(meta.feature("X"))            # tmux never offered it
        self.assertIs(meta.feature("jemalloc"), False)

    def test_reads_xpak_tbz2(self):
        meta = binpkg_mod.read(xpak_tbz2(self.tmp, files=TMUX))
        self.assertEqual(meta.format, "xpak")
        self.assertEqual(meta.atom, "app-misc/tmux")
        self.assertIs(meta.feature("systemd"), False)

    def test_uncompressed_and_zstd_metadata_both_read(self):
        plain = binpkg_mod.read(gpkg(self.tmp, files=TMUX))
        if not _zstd_available():
            self.skipTest("no zstd codec here (needs Python 3.14 or the zstd binary)")
        blob = _zstd_compress(meta_tar(TMUX))
        path = gpkg(self.tmp, files={}, basename="z", metadata_name="metadata.tar.zst",
                    metadata=blob)
        self.assertEqual(binpkg_mod.read(path).use, plain.use)

    def test_unknown_metadata_codec_is_reported_not_guessed(self):
        path = gpkg(self.tmp, files={}, metadata_name="metadata.tar.lz4",
                    metadata=b"\x04\x22\x4d\x18 not really lz4")
        with self.assertRaises(binpkg_mod.UnsupportedFormat) as caught:
            binpkg_mod.read(path)
        self.assertIn("lz4", str(caught.exception))

    def test_unsupported_format_is_reported_not_crashed(self):
        path = self.tmp / "junk.gpkg.tar"
        path.write_bytes(b"PK\x03\x04 this is a zip, not a binary package" * 40)
        with self.assertRaises(binpkg_mod.UnsupportedFormat) as caught:
            binpkg_mod.read(path)
        self.assertIn("not a binary package", str(caught.exception))

    def test_empty_file_is_reported_not_crashed(self):
        path = self.tmp / "empty.gpkg.tar"
        path.write_bytes(b"")
        with self.assertRaises(binpkg_mod.BinpkgError):
            binpkg_mod.read(path)

    def test_truncated_archive_is_reported_not_crashed(self):
        # 1024 bytes in is the middle of the metadata member's own data.
        path = gpkg(self.tmp, files=TMUX, truncate_to=1600)
        with self.assertRaises(binpkg_mod.BinpkgError) as caught:
            binpkg_mod.read(path)
        self.assertIn(path.name, str(caught.exception))

    def test_corrupt_metadata_tar_is_reported_not_crashed(self):
        path = gpkg(self.tmp, files={}, metadata=b"this is not a tar at all" * 100)
        with self.assertRaises(binpkg_mod.BinpkgError):
            binpkg_mod.read(path)

    def test_tar_without_metadata_is_reported(self):
        path = self.tmp / "no-metadata.gpkg.tar"
        with tarfile.open(path, mode="w") as tf:
            info = tarfile.TarInfo("x-1/image.tar.zst")
            info.size = 4
            tf.addfile(info, io.BytesIO(b"junk"))
        with self.assertRaises(binpkg_mod.MetadataNotFound):
            binpkg_mod.read(path)

    def test_image_is_never_read(self):
        """Truncate inside the image member: the read must already be finished."""
        path = gpkg(self.tmp, files=TMUX)
        full = path.stat().st_size
        path.write_bytes(path.read_bytes()[: full - 40000])
        meta = binpkg_mod.read(path)
        self.assertEqual(meta.atom, "app-misc/tmux")

    def test_oversize_metadata_file_is_skipped_and_reported(self):
        path = gpkg(self.tmp, files=TMUX)
        with mock.patch.object(binpkg_mod, "MAX_FILE_BYTES", 8):
            meta = binpkg_mod.read(path)
        self.assertTrue(meta.unread)
        self.assertTrue(any("IUSE" in note for note in meta.unread), meta.unread)

    def test_decompression_bomb_is_bounded(self):
        import gzip

        blob = gzip.compress(meta_tar(TMUX))
        path = gpkg(self.tmp, files={}, metadata_name="metadata.tar.gz", metadata=blob)
        with mock.patch.object(binpkg_mod, "MAX_METADATA_BYTES", 64):
            with self.assertRaises(binpkg_mod.BinpkgError) as caught:
                binpkg_mod.read(path)
        self.assertIn("64 bytes", str(caught.exception))
        self.assertEqual(binpkg_mod.read(path).atom, "app-misc/tmux")  # fine unbounded

    def test_missing_metadata_variables_are_reported_not_invented(self):
        path = gpkg(self.tmp, files={"CATEGORY": "app-misc\n", "PF": "tmux-3.5a\n"})
        meta = binpkg_mod.read(path)
        joined = " | ".join(meta.unread)
        self.assertIn("IUSE", joined)
        self.assertIn("CHOST", joined)
        self.assertIn("NEEDED.ELF.2", joined)


# --- fitting ----------------------------------------------------------------


class TestFit(Fixture):
    def test_exact_when_every_decided_flag_agrees(self):
        meta = binpkg_mod.read(gpkg(self.tmp, files=TMUX))
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.EXACT)
        self.assertEqual(verdict.reasons, ())
        self.assertTrue(verdict.ok)

    def test_lock_disabled_flag_absent_from_use_reads_as_agreeing(self):
        """The bug this command exists to avoid: USE lists only enabled flags."""
        meta = binpkg_mod.read(gpkg(self.tmp, files=TMUX))
        decided = binpkg_mod.decisions(self.tmux_lock(), "app-misc/tmux")
        for flag in ("debug", "systemd", "utempter", "vim-syntax"):
            self.assertIn(flag, decided)
            self.assertEqual(decided[flag].scope, "package")
            self.assertNotIn(flag, meta.use)
            self.assertIs(meta.feature(flag), False, f"{flag} misread as not offered")
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.of_kind("flag", "unknown"), ())

    def test_mismatch_names_want_and_built(self):
        files = dict(TMUX, USE="arm64 elibc_musl kernel_linux systemd\n")
        meta = binpkg_mod.read(gpkg(self.tmp, files=files))
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.MISMATCH)
        self.assertFalse(verdict.ok)
        texts = [r.text for r in verdict.of_kind("flag")]
        self.assertTrue(any("want -systemd, built +systemd" in t for t in texts), texts)
        self.assertTrue(any("dinit" in t for t in texts), "the lock's why is not quoted")

    def test_undecided_enabled_feature_is_usable_but_not_reusable(self):
        """An enabled feature with no `why` is what emerge will rebuild over.

        `--binpkg-respect-use=y` (portage.emerge_argv) compares every IUSE flag
        against the locally computed USE and rejects on any difference, so exiting 0
        for a package built +jemalloc promises a reuse that will not happen — and
        installs feature surface no intent asked for (README rule 2).
        """
        files = dict(TMUX, USE="arm64 elibc_musl kernel_linux jemalloc\n")
        meta = binpkg_mod.read(gpkg(self.tmp, files=files))
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.USABLE)
        self.assertFalse(verdict.ok, "an unjustified enabled feature counted as reusable")
        self.assertIn("+jemalloc", verdict.of_kind("extra")[0].text)
        self.assertIn("binpkg-respect-use", verdict.of_kind("extra")[0].text)

    def test_foreign_chost_cannot_be_installed(self):
        files = dict(TMUX, CHOST="x86_64-pc-linux-gnu\n",
                     USE="amd64 elibc_glibc kernel_linux\n")
        meta = binpkg_mod.read(gpkg(self.tmp, files=files))
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.FOREIGN)
        self.assertFalse(verdict.ok)
        self.assertTrue(verdict.of_kind("chost"))
        self.assertTrue(verdict.of_kind("arch"))
        self.assertTrue(verdict.of_kind("libc"))

    def test_foreign_libc_alone_is_still_foreign(self):
        files = dict(TMUX, USE="arm64 elibc_glibc kernel_linux\n")
        meta = binpkg_mod.read(gpkg(self.tmp, files=files))
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.FOREIGN)
        self.assertIn("elibc_musl", verdict.of_kind("libc")[0].text)

    def test_flag_the_package_does_not_offer_is_a_mismatch_not_silence(self):
        lock = write_lock(
            self.tmp,
            packages=[pkg("app-misc/tmux", [
                {"flag": "sixel", "enabled": True, "why": "intent[9]: inline images"},
                {"flag": "clipboard", "enabled": True, "why": "intent[9]: not a real tmux flag"},
            ])],
        )
        meta = binpkg_mod.read(gpkg(self.tmp, files=TMUX))
        verdict = binpkg_mod.fit(meta, lock_mod.load(lock))
        self.assertEqual(verdict.verdict, binpkg_mod.MISMATCH)
        texts = " | ".join(r.text for r in verdict.reasons)
        self.assertIn("want +sixel, built -sixel", texts)      # offered, off
        self.assertIn("clipboard is not in this package's IUSE", texts)

    def test_globally_enabled_flag_the_package_lacks_is_not_a_mismatch(self):
        """Found by running this against the machine's real lockfile.

        `USE="ssl"` in make.conf asks for ssl wherever it exists. tmux has no ssl
        flag, so the global default does not apply to it — reporting that as
        unsatisfiable made every package on the binhost read as a mismatch.
        """
        meta = binpkg_mod.read(gpkg(self.tmp, files=TMUX))
        lock = self.tmux_lock(global_use="-X -pam ssl")
        self.assertEqual(binpkg_mod.decisions(lock, "app-misc/tmux")["ssl"].scope, "make.conf")
        verdict = binpkg_mod.fit(meta, lock)
        self.assertEqual(verdict.verdict, binpkg_mod.EXACT, [r.text for r in verdict.reasons])
        self.assertEqual(verdict.reasons, ())

    def test_global_make_conf_use_counts_as_a_decision(self):
        files = dict(TMUX, IUSE="debug systemd pam\n",
                     USE="arm64 elibc_musl kernel_linux pam\n")
        meta = binpkg_mod.read(gpkg(self.tmp, files=files))
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.MISMATCH)
        self.assertIn("want -pam, built +pam", verdict.of_kind("flag")[0].text)
        self.assertIn("make.conf USE", verdict.of_kind("flag")[0].text)

    def test_cflags_difference_is_reported_but_not_a_mismatch(self):
        files = dict(TMUX, CFLAGS="-O3 -pipe -march=native\n",
                     CXXFLAGS="-O3 -pipe -march=native\n")
        meta = binpkg_mod.read(gpkg(self.tmp, files=files))
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.EXACT)
        self.assertTrue(verdict.ok)
        self.assertEqual(len(verdict.of_kind("cflags")), 2)
        self.assertIn("-march=native", verdict.of_kind("cflags")[0].text)

    def test_package_the_lock_never_names_is_usable_at_best(self):
        meta = binpkg_mod.read(gpkg(self.tmp, files=TMUX))
        lock = write_lock(self.tmp, packages=[pkg("app-editors/vim", [])])
        verdict = binpkg_mod.fit(meta, lock_mod.load(lock))
        self.assertEqual(verdict.verdict, binpkg_mod.USABLE)
        self.assertIn("does not name app-misc/tmux", verdict.of_kind("note")[0].text)

    def test_missing_iuse_downgrades_instead_of_claiming_exact(self):
        files = {k: v for k, v in TMUX.items() if k != "IUSE"}
        meta = binpkg_mod.read(gpkg(self.tmp, files=files))
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.USABLE)
        self.assertTrue(verdict.of_kind("unknown"))


class TestContradiction(Fixture):
    """The lock says -X; the ELF says libX11. Believe the ELF."""

    VIM = {
        "CATEGORY": "app-editors\n",
        "PF": "vim-9.1.0866\n",
        "BUILD_ID": "1\n",
        "BUILD_TIME": "1785171999\n",
        "SIZE": "4194304\n",
        "CHOST": "aarch64-unknown-linux-musl\n",
        "CFLAGS": "-O2 -pipe\n",
        "CXXFLAGS": "-O2 -pipe\n",
        "USE": "arm64 elibc_musl kernel_linux\n",
        "IUSE": "X acl cscope gpm lua minimal nls perl python racket ruby sound tcl terminal\n",
        "NEEDED.ELF.2":
            "arm64;/usr/bin/vim;;;libX11.so.6,libtinfow.so.6,libc.so;\n"
            "arm64;/usr/bin/vimdiff;;;libc.so;\n",
    }

    def vim_lock(self) -> dict:
        return lock_mod.load(write_lock(
            self.tmp,
            packages=[pkg("app-editors/vim", [
                {"flag": "X", "enabled": False,
                 "why": "intent[0]: explicitly no X11 and no clipboard integration"},
            ])],
        ))

    def test_disabled_flag_whose_library_is_linked_is_flagged(self):
        meta = binpkg_mod.read(gpkg(self.tmp, files=self.VIM, basename="vim-9.1.0866-1"))
        self.assertIs(meta.feature("X"), False)  # USE agrees with the lock...
        verdict = binpkg_mod.fit(meta, self.vim_lock())
        self.assertEqual(verdict.of_kind("flag"), ())  # ...so no flag mismatch
        self.assertTrue(verdict.contradictions, "a -X build linking libX11 was not flagged")
        text = verdict.contradictions[0].text
        self.assertIn("libX11.so.6", text)
        self.assertIn("/usr/bin/vim", text)
        self.assertIn("no X11", text)  # the intent that is being contradicted
        self.assertFalse(verdict.ok, "a package contradicting its own USE is not reusable")
        # ...and the table must not print a bare "exact" for it.
        self.assertIn("!link", "\n".join(binpkg_mod.render([verdict])))

    def test_no_contradiction_when_nothing_links_the_library(self):
        files = dict(self.VIM, **{"NEEDED.ELF.2": "arm64;/usr/bin/vim;;;libc.so;\n"})
        meta = binpkg_mod.read(gpkg(self.tmp, files=files, basename="vim-9.1.0866-1"))
        verdict = binpkg_mod.fit(meta, self.vim_lock())
        self.assertEqual(verdict.contradictions, ())
        self.assertTrue(verdict.ok)

    def test_requires_is_used_when_needed_elf2_is_absent(self):
        files = {k: v for k, v in self.VIM.items() if k != "NEEDED.ELF.2"}
        files["REQUIRES"] = "arm64: libX11.so.6 libc.so\n"
        meta = binpkg_mod.read(gpkg(self.tmp, files=files, basename="vim-9.1.0866-1"))
        verdict = binpkg_mod.fit(meta, self.vim_lock())
        self.assertTrue(verdict.contradictions)
        self.assertIn("(REQUIRES)", verdict.contradictions[0].text)

    def test_enabled_flag_linking_its_library_is_not_a_contradiction(self):
        files = dict(self.VIM, USE="X arm64 elibc_musl kernel_linux\n")
        lock = lock_mod.load(write_lock(
            self.tmp,
            packages=[pkg("app-editors/vim", [
                {"flag": "X", "enabled": True, "why": "intent[0]: X11"},
            ])],
            global_use="-wayland",
        ))
        meta = binpkg_mod.read(gpkg(self.tmp, files=files, basename="vim-9.1.0866-1"))
        verdict = binpkg_mod.fit(meta, lock)
        self.assertEqual(verdict.contradictions, ())


# --- the container is untrusted input ---------------------------------------


class TestSpoofedContainer(Fixture):
    """Two audit findings that are one bug: the format sniff believed the tail.

    A gpkg IS a tar, and every tar reader stops at the end-of-archive blocks and
    ignores whatever follows. Testing for an xpak trailer before testing the tar
    header therefore let ~120 appended bytes decide which metadata forge read,
    while portage went on reading the real metadata.tar — one file, two answers,
    and the dishonest one is the one that gates `emerge --getbinpkg`.
    """

    DIRTY = dict(TMUX, USE="arm64 elibc_musl kernel_linux systemd utempter debug\n")
    #: What the appended segment claims: tmux's honest, lock-abiding metadata.
    CLAIM = {key: TMUX[key] for key in ("CATEGORY", "PF", "BUILD_ID", "USE", "IUSE", "CHOST")}

    def test_appended_xpak_trailer_does_not_replace_the_real_metadata(self):
        honest = gpkg(self.tmp, files=self.DIRTY, basename="spoof-1")
        self.assertEqual(
            binpkg_mod.fit(binpkg_mod.read(honest), self.tmux_lock()).verdict,
            binpkg_mod.MISMATCH,
        )
        spoofed = gpkg(self.tmp, files=self.DIRTY, basename="spoof-2",
                       append=xpak_segment(self.CLAIM))
        with self.assertRaises(binpkg_mod.BinpkgError) as caught:
            binpkg_mod.read(spoofed)
        self.assertIn("appended", str(caught.exception))
        # The archive is untouched — that is what made this a spoof and not a
        # corruption. Portage still reads all four members from it.
        with tarfile.open(spoofed) as tf:
            self.assertIn("spoof-2/metadata.tar", tf.getnames())

    def test_cli_does_not_exit_zero_for_a_spoofed_package(self):
        lock = self.tmux_lock_path()
        path = gpkg(self.tmp, files=self.DIRTY, basename="spoof-3",
                    append=xpak_segment(self.CLAIM))
        code, out = self.run_cli("--lock", str(lock), "binpkg", str(path))
        self.assertEqual(code, 1, out)
        self.assertIn("ERROR", out)
        self.assertIn("0/1 reusable", out)

    def test_zero_padding_after_the_marker_is_still_a_valid_package(self):
        """Only *data* past the end is refused; tar writes zero padding there."""
        path = gpkg(self.tmp, files=TMUX, basename="padded-1", append=b"\x00" * 4096)
        self.assertEqual(binpkg_mod.read(path).atom, "app-misc/tmux")

    def test_a_tar_without_the_gpkg_marker_is_not_a_binary_package(self):
        path = gpkg(self.tmp, files=TMUX, basename="nomarker-1", marker=None)
        with self.assertRaises(binpkg_mod.UnsupportedFormat) as caught:
            binpkg_mod.read(path)
        self.assertIn("gpkg-1", str(caught.exception))

    def test_a_newer_container_version_is_reported_not_guessed_at(self):
        path = gpkg(self.tmp, files=TMUX, basename="future-1", marker="gpkg-2")
        with self.assertRaises(binpkg_mod.UnsupportedFormat) as caught:
            binpkg_mod.read(path)
        self.assertIn("gpkg-2", str(caught.exception))

    def test_a_genuine_xpak_is_still_read(self):
        """The fix must not cost the format it was distinguishing from."""
        meta = binpkg_mod.read(xpak_tbz2(self.tmp, files=TMUX))
        self.assertEqual(meta.format, "xpak")


class TestManifest(Fixture):
    """Portage verifies every gpkg member against the Manifest; so does this now.

    Metadata that disagrees with the Manifest is metadata portage would refuse, so
    a verdict derived from it describes a package nobody will ever install — and
    the divergence is exactly where a swapped metadata member hides.
    """

    def test_metadata_that_does_not_match_the_manifest_is_refused(self):
        honest = meta_tar(TMUX)
        swapped = meta_tar(dict(TMUX, USE="arm64 elibc_musl kernel_linux systemd\n"))
        path = gpkg(self.tmp, files={}, basename="swap-1", metadata=swapped,
                    manifest=manifest_for([("metadata.tar", honest)]))
        with self.assertRaises(binpkg_mod.BinpkgError) as caught:
            binpkg_mod.read(path)
        self.assertIn("does not match the Manifest", str(caught.exception))

    def test_manifest_size_disagreement_is_refused(self):
        path = gpkg(self.tmp, files=TMUX, basename="size-1",
                    manifest=b"DATA metadata.tar 17 SHA512 " + b"0" * 128 + b"\n")
        with self.assertRaises(binpkg_mod.BinpkgError) as caught:
            binpkg_mod.read(path)
        self.assertIn("17 bytes", str(caught.exception))

    def test_a_blake2b_only_manifest_is_verified(self):
        blob = meta_tar(TMUX)
        line = (f"DATA metadata.tar {len(blob)} "
                f"BLAKE2B {hashlib.blake2b(blob).hexdigest()}\n")
        path = gpkg(self.tmp, files={}, basename="b2-1", metadata=blob,
                    manifest=line.encode("utf-8"))
        self.assertEqual(binpkg_mod.read(path).unread, ())

    def test_a_package_with_no_manifest_reads_but_is_never_exact(self):
        path = gpkg(self.tmp, files=TMUX, basename="bare-1", manifest=b"")
        meta = binpkg_mod.read(path)
        self.assertTrue(any("Manifest" in note for note in meta.unread), meta.unread)
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.USABLE)

    def test_a_manifest_that_ignores_the_metadata_member_is_not_certification(self):
        path = gpkg(self.tmp, files=TMUX, basename="elsewhere-1",
                    manifest=b"DATA image.tar.zst 4 SHA512 " + b"0" * 128 + b"\n")
        meta = binpkg_mod.read(path)
        self.assertTrue(any("does not cover" in note for note in meta.unread), meta.unread)

    def test_a_signature_is_named_and_never_claimed_to_have_been_checked(self):
        path = gpkg(self.tmp, files=TMUX, basename="signed-1",
                    extra_members=[("metadata.tar.sig", b"-----BEGIN PGP SIGNATURE-----\n")])
        meta = binpkg_mod.read(path)
        self.assertEqual(meta.signatures, ("metadata.tar.sig",))
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        # A signature is evidence, not a penalty: carrying one this cannot verify
        # must not make the package read worse than an unsigned one.
        self.assertEqual(verdict.verdict, binpkg_mod.EXACT, [r.text for r in verdict.reasons])
        self.assertTrue(any("nothing here verifies" in r.text for r in verdict.of_kind("note")))


class TestUndetermined(Fixture):
    """A gap is not agreement.

    `USE` unread arrives as an empty set of enabled flags, and an empty set agrees
    with a lockfile made almost entirely of negative decisions — so all three of
    these vectors used to certify a package about which nothing was established.
    """

    def unfittable(self, path: Path) -> binpkg_mod.Fit:
        verdict = binpkg_mod.examine(str(path), self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.ERROR, [r.text for r in verdict.reasons])
        self.assertFalse(verdict.ok)
        return verdict

    def test_absent_use_cannot_be_fitted_at_all(self):
        files = {k: v for k, v in TMUX.items() if k != "USE"}
        verdict = self.unfittable(gpkg(self.tmp, files=files, basename="nouse-1"))
        self.assertIn("USE could not be read", verdict.of_kind("error")[0].text)

    def test_use_stored_as_a_symlink_is_not_an_absent_use(self):
        blob = meta_tar_unreadable(
            dict(TMUX, USE="arm64 elibc_musl kernel_linux systemd debug\n"),
            "USE", link="USE.real",
        )
        path = gpkg(self.tmp, files={}, basename="symuse-1", metadata=blob)
        meta = binpkg_mod.read(path)
        self.assertIn("USE", meta.undetermined)
        self.assertTrue(any("not a regular file" in n for n in meta.unread), meta.unread)
        self.unfittable(path)

    def test_use_over_the_per_file_cap_is_not_an_empty_use(self):
        files = dict(TMUX, USE="arm64 elibc_musl kernel_linux systemd\n" + "#" * 8192)
        path = gpkg(self.tmp, files=files, basename="biguse-1")
        with mock.patch.object(binpkg_mod, "MAX_FILE_BYTES", 4096):
            meta = binpkg_mod.read(path)
            self.assertIn("USE", meta.undetermined)
            self.assertEqual(binpkg_mod.fit(meta, self.tmux_lock()).verdict, binpkg_mod.ERROR)

    def test_missing_chost_prevents_exact(self):
        """CHOST is the only proof the package targets this machine."""
        files = {k: v for k, v in TMUX.items() if k != "CHOST"}
        meta = binpkg_mod.read(gpkg(self.tmp, files=files, basename="nochost-1"))
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.USABLE)
        self.assertTrue(any("CHOST" in note for note in meta.unread), meta.unread)

    def test_neither_chost_nor_use_is_not_reusable(self):
        files = {k: v for k, v in TMUX.items() if k not in ("USE", "CHOST")}
        path = gpkg(self.tmp, files=files, basename="nothing-1")
        code, out = self.run_cli("--lock", str(self.tmux_lock_path()), "binpkg", str(path))
        self.assertEqual(code, 1, out)
        self.assertIn("0/1 reusable", out)

    def test_an_undetermined_package_is_never_exact(self):
        files = {k: v for k, v in TMUX.items() if k not in ("NEEDED.ELF.2", "REQUIRES")}
        meta = binpkg_mod.read(gpkg(self.tmp, files=files, basename="noelf-1"))
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.USABLE)
        self.assertTrue(any("could not be determined" in r.text for r in verdict.of_kind("note")))


class TestIuseDrift(Fixture):
    """USE is authoritative about what was enabled; IUSE only adds a distinction.

    A flag absent from IUSE but present in USE is not unknowable — USE naming it is
    proof the feature is in the build. That is the "the ebuild changed" case the
    IUSE intersection exists for, and it used to fail open and silently.
    """

    NO_ELF = {k: v for k, v in TMUX.items() if k not in ("NEEDED.ELF.2", "REQUIRES")}

    def test_disabled_flag_dropped_from_iuse_but_present_in_use_is_a_mismatch(self):
        files = dict(self.NO_ELF, USE="arm64 elibc_musl kernel_linux systemd utempter\n",
                     IUSE="debug jemalloc selinux sixel vim-syntax\n")
        meta = binpkg_mod.read(gpkg(self.tmp, files=files, basename="drift-1"))
        self.assertIsNone(meta.feature("systemd"), "the setup no longer reproduces the case")
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.MISMATCH)
        self.assertFalse(verdict.ok)
        texts = " | ".join(r.text for r in verdict.of_kind("flag"))
        self.assertIn("want -systemd, built +systemd", texts)
        self.assertIn("want -utempter, built +utempter", texts)
        self.assertIn("dinit", texts)  # the intent being contradicted, quoted

    def test_the_library_cross_check_is_not_what_catches_it(self):
        """Same package with no NEEDED/REQUIRES at all: the flags alone must convict.

        The contradiction check only covers the ~40 flags in LIBRARY_EVIDENCE, so
        relying on it would leave debug, vim-syntax, minimal, webdav, xmss and the
        rest of the lockfile permanently uncatchable.
        """
        files = dict(self.NO_ELF, USE="arm64 elibc_musl kernel_linux debug vim-syntax\n",
                     IUSE="jemalloc selinux sixel systemd utempter\n")
        verdict = binpkg_mod.fit(
            binpkg_mod.read(gpkg(self.tmp, files=files, basename="drift-2")), self.tmux_lock()
        )
        self.assertEqual(verdict.contradictions, ())
        self.assertEqual(verdict.verdict, binpkg_mod.MISMATCH)
        texts = " | ".join(r.text for r in verdict.of_kind("flag"))
        self.assertIn("want -debug, built +debug", texts)
        self.assertIn("want -vim-syntax, built +vim-syntax", texts)

    def test_an_undecided_enabled_flag_outside_iuse_is_still_reported(self):
        """The same blind spot one step down: `extra` used to filter on IUSE too.

        Nothing but the fixed profile population (arch, libc, kernel_*, USE_EXPAND)
        legitimately appears in USE without an ebuild offering it, so a feature there
        is a feature — and portage rebuilds over an IUSE difference anyway.
        """
        files = dict(TMUX, USE="arm64 elibc_musl kernel_linux sixel\n",
                     IUSE="debug systemd utempter vim-syntax\n")
        verdict = binpkg_mod.fit(
            binpkg_mod.read(gpkg(self.tmp, files=files, basename="drift-3")), self.tmux_lock()
        )
        self.assertIn("+sixel", verdict.of_kind("extra")[0].text)
        self.assertFalse(verdict.ok)
        # ...while the implicit trio stays out of it, on this and every other package.
        for implicit in ("arm64", "elibc_musl", "kernel_linux"):
            self.assertNotIn(implicit, verdict.of_kind("extra")[0].text)

    def test_a_lock_entry_naming_a_profile_flag_is_a_note_not_a_mismatch(self):
        """The one legitimate USE-minus-IUSE population must stay reportable-only."""
        lock = lock_mod.load(write_lock(self.tmp, packages=[pkg(
            "app-misc/tmux",
            TMUX_LOCK_USE + [{"flag": "kernel_linux", "enabled": False,
                              "why": "intent[9]: a nonsense entry naming an implicit flag"}],
        )]))
        meta = binpkg_mod.read(gpkg(self.tmp, files=TMUX, basename="implicit-1"))
        verdict = binpkg_mod.fit(meta, lock)
        self.assertEqual(verdict.verdict, binpkg_mod.USABLE, [r.text for r in verdict.reasons])
        self.assertEqual(verdict.of_kind("flag"), ())
        self.assertTrue(any("kernel_linux" in r.text for r in verdict.of_kind("note")))


class TestEbuildDefaults(Fixture):
    """The live failure: `forge binpkg` said `exact` about a vim portage refused.

    `forge binpkg app-editors/vim` reported `exact` while the same emerge rejected the
    package with "use flag configuration mismatch". Both were looking at the same file
    and only one of them was computing portage's answer: `fit` compared the flags the
    LOCKFILE names, and portage compares the package's USE against the USE it computes
    for this machine — which starts from the ebuild's own IUSE defaults. vim is
    `+crypt`, the peer's package had crypt off, and the lockfile never mentioned crypt,
    so forge saw no disagreement anywhere and answered the operator's "why did this
    rebuild instead of reusing" with the most confidently wrong word available.
    """

    #: app-editors/vim as a peer built it. `+crypt` and `+nls` are the ebuild's own
    #: defaults; neither is in USE, and the lockfile names neither.
    VIM = {
        "CATEGORY": "app-editors\n",
        "PF": "vim-9.1.0866\n",
        "BUILD_ID": "1\n",
        "BUILD_TIME": "1785172222\n",
        "SLOT": "0\n",
        "SIZE": "4194304\n",
        "CHOST": "aarch64-unknown-linux-musl\n",
        "CFLAGS": "-O2 -pipe\n",
        "CXXFLAGS": "-O2 -pipe\n",
        "IUSE": "X acl +crypt cscope -debug gpm lua minimal +nls perl python sound "
                "tcl terminal\n",
        "USE": "acl arm64 elibc_musl kernel_linux terminal\n",
        "IUSE_EFFECTIVE": "X acl arm64 +crypt cscope -debug elibc_musl gpm kernel_linux "
                          "lua minimal +nls perl python sound tcl terminal\n",
        "NEEDED.ELF.2": "arm64;/usr/bin/vim;;;libtinfow.so.6,libc.so;\n",
        "REQUIRES": "arm64: libc.so libtinfow.so.6\n",
    }

    #: Every flag the lockfile decides for vim. crypt and nls are deliberately absent:
    #: that silence is the whole bug.
    VIM_LOCK_USE = [
        {"flag": "X", "enabled": False, "why": "intent[0]: no X11 or clipboard"},
        {"flag": "acl", "enabled": True, "why": "intent[2]: ACLs survive an edit"},
        {"flag": "terminal", "enabled": True, "why": "intent[3]: :term inside vim"},
        {"flag": "lua", "enabled": False, "why": "no intent asked for lua scripting"},
        {"flag": "perl", "enabled": False, "why": "no intent asked for perl scripting"},
        {"flag": "python", "enabled": False, "why": "no intent asked for python scripting"},
    ]

    def vim_lock(self, use: list[dict] | None = None, **kwargs) -> dict:
        return lock_mod.load(write_lock(
            self.tmp,
            packages=[pkg("app-editors/vim", self.VIM_LOCK_USE if use is None else use)],
            **kwargs,
        ))

    def read_vim(self, **files) -> binpkg_mod.Metadata:
        return binpkg_mod.read(gpkg(self.tmp, files=dict(self.VIM, **files),
                                    basename="vim-9.1.0866-1"))

    def test_iuse_defaults_are_read_and_the_flag_names_stay_clean(self):
        meta = self.read_vim()
        self.assertEqual(meta.defaults, frozenset({"crypt", "nls"}))
        self.assertIn("crypt", meta.iuse)          # the name, without its marker
        self.assertNotIn("+crypt", meta.iuse)
        self.assertIs(meta.feature("crypt"), False)
        self.assertEqual(meta.default_off(), frozenset({"crypt", "nls"}))

    def test_default_on_flag_missing_from_use_is_not_exact(self):
        """The reported failure, end to end: no lock disagreement, and portage rebuilds."""
        meta = self.read_vim()
        lock = self.vim_lock()
        self.assertNotIn("crypt", binpkg_mod.decisions(lock, "app-editors/vim"),
                         "the fixture no longer reproduces the case: the lock names crypt")
        verdict = binpkg_mod.fit(meta, lock)
        self.assertEqual(verdict.verdict, binpkg_mod.REBUILD,
                         [r.text for r in verdict.reasons])
        self.assertFalse(verdict.ok, "a package emerge will rebuild counted as reusable")
        self.assertEqual(verdict.of_kind("flag", "unknown"), ())  # every named flag agrees
        text = verdict.of_kind("default")[0].text
        self.assertIn("agrees with every flag the lockfile names", text)
        self.assertIn("2 flag(s)", text)
        self.assertIn("the lockfile does not constrain crypt", text)
        self.assertIn("this package has it off and the ebuild defaults it on", text)
        self.assertIn("IUSE +crypt", text)
        self.assertIn("rebuilds it", text)
        self.assertIn("nls", text)

    def test_a_minus_defaulted_flag_absent_from_use_is_agreement(self):
        """`-debug` is the ebuild declining the flag: the package matching that is a fit.

        The IUSE here keeps its `-debug` marker and drops the two `+` ones, so this also
        pins that a marked IUSE is believed as-is and IUSE_EFFECTIVE is not consulted
        behind its back.
        """
        meta = self.read_vim(
            IUSE="X acl crypt cscope -debug gpm lua minimal nls perl python sound tcl "
                 "terminal\n"
        )
        self.assertEqual(meta.defaults, frozenset())
        verdict = binpkg_mod.fit(meta, self.vim_lock())
        self.assertEqual(verdict.of_kind("default"), ())
        self.assertEqual(verdict.verdict, binpkg_mod.EXACT, [r.text for r in verdict.reasons])
        self.assertTrue(verdict.ok)

    def test_a_default_on_flag_present_in_use_is_agreement(self):
        """Portage computes +crypt here too, so this one really is a reuse.

        Reporting it as a flag emerge would rebuild over would be this same bug pointed
        the other way — and the exit code is a prediction about emerge, not a grade.
        """
        meta = self.read_vim(USE="acl arm64 crypt elibc_musl kernel_linux nls terminal\n")
        verdict = binpkg_mod.fit(meta, self.vim_lock())
        self.assertEqual(verdict.of_kind("default"), ())
        self.assertEqual(verdict.of_kind("extra"), ())
        self.assertEqual(verdict.verdict, binpkg_mod.EXACT, [r.text for r in verdict.reasons])
        self.assertTrue(verdict.ok)
        # ...and the flag the lockfile never constrained is still said out loud.
        said = verdict.of_kind("unconstrained")[0].text
        self.assertIn("the lockfile does not constrain crypt", said)
        self.assertIn("has it on and the ebuild defaults it on", said)

    def test_iuse_effective_supplies_the_defaults_when_iuse_lost_its_markers(self):
        """Some producers strip IUSE's markers; IUSE_EFFECTIVE is then the only copy."""
        meta = self.read_vim(
            IUSE="X acl crypt cscope debug gpm lua minimal nls perl python sound tcl "
                 "terminal\n"
        )
        self.assertEqual(meta.defaults, frozenset({"crypt", "nls"}))
        verdict = binpkg_mod.fit(meta, self.vim_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.REBUILD)
        self.assertIn("the lockfile does not constrain crypt",
                      verdict.of_kind("default")[0].text)

    def test_a_flag_the_lockfile_disables_is_judged_once_not_twice(self):
        """A decision overrides the ebuild default, so (2) has already judged it.

        The lockfile disabling crypt is portage computing -crypt: the package built
        without it is what the machine asked for, and reporting the ebuild's default
        against it would invent a rebuild that will not happen.
        """
        verdict = binpkg_mod.fit(self.read_vim(), self.vim_lock(
            use=self.VIM_LOCK_USE + [
                {"flag": "crypt", "enabled": False, "why": "intent[4]: no encrypted files"},
                {"flag": "nls", "enabled": False, "why": "intent[4]: one language, C locale"},
            ],
        ))
        self.assertEqual(verdict.of_kind("default"), ())
        self.assertEqual(verdict.verdict, binpkg_mod.EXACT, [r.text for r in verdict.reasons])
        self.assertTrue(verdict.ok)

    def test_a_globally_disabled_default_is_agreement_too(self):
        """`-crypt` in make.conf USE is as binding as `-crypt` on the package line."""
        verdict = binpkg_mod.fit(self.read_vim(), self.vim_lock(global_use="-X -crypt -nls"))
        self.assertEqual(verdict.of_kind("default"), ())
        self.assertEqual(verdict.verdict, binpkg_mod.EXACT, [r.text for r in verdict.reasons])

    def test_a_global_flag_the_package_lacks_is_still_a_wildcard(self):
        """The rule `test_globally_enabled_flag_the_package_lacks_is_not_a_mismatch` pins.

        A global `USE="ssl"` asks for ssl wherever it exists; vim has no ssl flag, so
        the default check must not turn that into a demand while it reports crypt.
        """
        verdict = binpkg_mod.fit(self.read_vim(), self.vim_lock(global_use="-X ssl"))
        self.assertEqual(verdict.of_kind("unknown"), ())
        self.assertEqual(verdict.verdict, binpkg_mod.REBUILD)
        text = verdict.of_kind("default")[0].text
        self.assertIn("crypt", text)
        self.assertNotIn("ssl", text)

    def test_a_real_mismatch_outranks_the_default_clash_and_both_are_printed(self):
        """`mismatch` is worse: it denies something an intent asked for."""
        meta = self.read_vim(USE="X acl arm64 elibc_musl kernel_linux terminal\n")
        verdict = binpkg_mod.fit(meta, self.vim_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.MISMATCH)
        self.assertFalse(verdict.ok)
        self.assertIn("want -X, built +X", " | ".join(r.text for r in verdict.of_kind("flag")))
        text = verdict.of_kind("default")[0].text
        self.assertIn("the lockfile does not constrain crypt", text)
        # The claim of full agreement is dropped once a named flag was denied.
        self.assertNotIn("agrees with every flag", text)

    def test_pkguse_names_who_switched_the_ebuilds_default_off(self):
        """The builder's own package.use is provenance, never a licence."""
        verdict = binpkg_mod.fit(self.read_vim(PKGUSE="-crypt -nls\n"), self.vim_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.REBUILD)
        self.assertIn("PKGUSE carried -crypt", verdict.of_kind("default")[0].text)

    def test_a_defaulted_use_expand_member_is_reported_not_judged(self):
        """The profile owns these and the lockfile does not record the profile."""
        meta = self.read_vim(
            IUSE="X acl +crypt cscope -debug gpm lua minimal +nls perl python sound tcl "
                 "terminal +python_targets_python3_13\n",
            USE="acl arm64 crypt elibc_musl kernel_linux nls terminal\n",
        )
        self.assertIn("python_targets_python3_13", meta.defaults)
        verdict = binpkg_mod.fit(meta, self.vim_lock())
        self.assertEqual(verdict.of_kind("default"), ())
        self.assertEqual(verdict.verdict, binpkg_mod.EXACT, [r.text for r in verdict.reasons])

    def test_cli_prints_rebuild_and_withholds_exit_zero(self):
        lock = write_lock(self.tmp, packages=[pkg("app-editors/vim", self.VIM_LOCK_USE)])
        path = gpkg(self.tmp, files=self.VIM, basename="vim-9.1.0866-1")
        code, out = self.run_cli("--lock", str(lock), "binpkg", str(path))
        self.assertEqual(code, 1, out)
        self.assertIn("rebuild", out)
        self.assertIn("1 rebuild", out)          # and it is counted as its own verdict
        self.assertIn("0/1 reusable", out)
        self.assertIn("the lockfile does not constrain crypt", out)


class TestUseExpand(Fixture):
    """USE_EXPAND variables in make.conf are build policy exactly like USE.

    `lower.py` lets the agent author any make.conf key outside SPEC_OWNED/FORBIDDEN
    (the live lock already carries BINPKG_COMPRESS, CCACHE_DIR, PKGDIR), portage
    writes them verbatim, and it rejects a binpkg built for the wrong member. Only
    the literal `USE` entry used to count, so the wrong interpreter arrived as an
    undifferentiated "the lockfile never decided this".
    """

    IUSE = ("debug jemalloc selinux sixel systemd utempter vim-syntax "
            "python_single_target_python3_12 python_single_target_python3_13 "
            "lua_single_target_luajit lua_single_target_lua5-4\n")

    def expand_lock(self) -> dict:
        return lock_mod.load(write_lock(
            self.tmp,
            packages=[pkg("app-misc/tmux", TMUX_LOCK_USE)],
            extra_make_conf=[
                {"key": "PYTHON_SINGLE_TARGET", "value": "python3_12",
                 "why": "intent[3]: one interpreter, the one the probes run"},
                {"key": "LUA_SINGLE_TARGET", "value": "lua5-4", "why": "intent[3]: lua 5.4"},
            ],
        ))

    def test_use_expand_entries_become_decisions(self):
        decided = binpkg_mod.decisions(self.expand_lock(), "app-misc/tmux")
        self.assertIn("python_single_target_python3_12", decided)
        self.assertIn("lua_single_target_lua5-4", decided)
        one = decided["python_single_target_python3_12"]
        self.assertTrue(one.exclusive)
        self.assertEqual(one.group, "python_single_target")
        self.assertIn("PYTHON_SINGLE_TARGET", one.why)

    def test_the_wrong_single_target_is_a_mismatch_not_an_extra(self):
        files = dict(TMUX, IUSE=self.IUSE,
                     USE="arm64 elibc_musl kernel_linux python_single_target_python3_13 "
                         "lua_single_target_luajit\n")
        meta = binpkg_mod.read(gpkg(self.tmp, files=files, basename="expand-1"))
        verdict = binpkg_mod.fit(meta, self.expand_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.MISMATCH)
        self.assertFalse(verdict.ok)
        texts = " | ".join(r.text for r in verdict.of_kind("flag"))
        self.assertIn("PYTHON_SINGLE_TARGET=python3_13", texts)
        self.assertIn("python3_12", texts)
        self.assertIn("LUA_SINGLE_TARGET=luajit", texts)
        # ...and not buried in the undecided line, which is where it used to land.
        self.assertEqual(verdict.of_kind("extra"), ())

    def test_the_decided_single_target_agrees(self):
        files = dict(TMUX, IUSE=self.IUSE,
                     USE="arm64 elibc_musl kernel_linux python_single_target_python3_12 "
                         "lua_single_target_lua5-4\n")
        meta = binpkg_mod.read(gpkg(self.tmp, files=files, basename="expand-2"))
        verdict = binpkg_mod.fit(meta, self.expand_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.EXACT, [r.text for r in verdict.reasons])
        self.assertTrue(verdict.ok)

    def test_profile_set_expand_members_are_reported_without_sinking_the_verdict(self):
        """On a real machine these land on nearly every package.

        Judging them would mean no real vim or tmux ever reports `exact`, which is
        the nonsense this command replaces; hiding them would lose the signal. So
        they get their own line and no verdict.
        """
        files = dict(TMUX,
                     IUSE="debug jemalloc selinux sixel systemd utempter vim-syntax "
                          "python_targets_python3_13 abi_x86_64\n",
                     USE="arm64 elibc_musl kernel_linux python_targets_python3_13\n")
        meta = binpkg_mod.read(gpkg(self.tmp, files=files, basename="expand-3"))
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.EXACT, [r.text for r in verdict.reasons])
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.of_kind("extra"), ())
        self.assertIn("python_targets_python3_13", verdict.of_kind("expand")[0].text)


class TestCorruptCodec(Fixture):
    """Every read failure is a verdict. `zlib.error` and `LZMAError` are not OSError.

    They subclass Exception directly, so a corrupt gz or xz metadata member escaped
    every guard between the decompressor and `cli.main` — and because the command
    builds all its rows in one comprehension, one poisoned package on a binhost
    aborted the whole scan with a traceback and printed no table at all.
    """

    def poisoned(self, basename: str, suffix: str, blob: bytes) -> Path:
        raw = bytearray(blob)
        raw[30:70] = b"\xff" * 40
        return gpkg(self.tmp, files={}, basename=basename,
                    metadata_name=f"metadata.tar{suffix}", metadata=bytes(raw))

    def test_corrupt_gz_metadata_is_a_verdict(self):
        import gzip

        path = self.poisoned("badgz-1", ".gz", gzip.compress(meta_tar(TMUX)))
        verdict = binpkg_mod.examine(str(path), self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.ERROR)
        self.assertIn("corrupt", verdict.of_kind("error")[0].text)

    def test_corrupt_xz_metadata_is_a_verdict(self):
        import lzma

        path = self.poisoned("badxz-1", ".xz", lzma.compress(meta_tar(TMUX)))
        verdict = binpkg_mod.examine(str(path), self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.ERROR)
        self.assertIn("corrupt", verdict.of_kind("error")[0].text)

    def test_one_poisoned_package_does_not_abort_the_scan(self):
        import gzip

        cache = self.tmp / "binpkgs" / "app-misc"
        cache.mkdir(parents=True)
        gpkg(cache, files=TMUX)
        raw = bytearray(gzip.compress(meta_tar(TMUX)))
        raw[30:70] = b"\xff" * 40
        gpkg(cache, files={}, basename="poison-1", metadata_name="metadata.tar.gz",
             metadata=bytes(raw))
        code, out = self.run_cli("--lock", str(self.tmux_lock_path()), "binpkg",
                                 "--dir", str(self.tmp / "binpkgs"))
        self.assertEqual(code, 1, out)
        self.assertIn("exact", out)   # the good row survived...
        self.assertIn("ERROR", out)   # ...and the bad one is a row, not a traceback
        self.assertIn("1/2 reusable", out)


# --- sources and CLI --------------------------------------------------------


class TestSources(Fixture):
    def test_walk_finds_both_formats_and_nothing_else(self):
        (self.tmp / "app-misc").mkdir()
        one = gpkg(self.tmp / "app-misc", files=TMUX)
        two = xpak_tbz2(self.tmp / "app-misc", files=TMUX)
        (self.tmp / "Packages").write_text("PACKAGES: 2\n", encoding="utf-8")
        self.assertEqual(binpkg_mod.walk(self.tmp), sorted([one, two]))

    def test_walk_reports_a_missing_directory(self):
        with self.assertRaises(binpkg_mod.BinpkgError):
            binpkg_mod.walk(self.tmp / "nope")

    def test_index_paths_reads_both_index_generations(self):
        index = (
            "ARCH: arm64\nPACKAGES: 2\n\n"
            "BUILD_ID: 1\nCPV: app-misc/tmux-3.5a\nPATH: app-misc/tmux-3.5a-1.gpkg.tar\n\n"
            "CPV: app-editors/vim-9.1.0866\n"
        )
        self.assertEqual(
            binpkg_mod._index_paths(index),
            ["app-misc/tmux-3.5a-1.gpkg.tar", "app-editors/vim-9.1.0866.tbz2"],
        )

    def test_examine_turns_an_unreadable_package_into_a_verdict(self):
        path = self.tmp / "broken.gpkg.tar"
        path.write_bytes(b"not a package")
        verdict = binpkg_mod.examine(str(path), self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.ERROR)
        self.assertFalse(verdict.ok)
        self.assertTrue(verdict.of_kind("error"))

    def test_peer_rejects_a_non_http_url(self):
        with self.assertRaises(binpkg_mod.BinpkgError):
            binpkg_mod.peer_sources("file:///var/cache/binpkgs")


class PeerFixture(Fixture):
    """A real HTTP server serving a binpkg tree, plus an index with one escaping path.

    Shared rather than inherited between the two peer suites, so neither re-runs the
    other's cases — one of them builds a 2 MiB package.
    """

    def peer_handler(self):
        """The handler this suite's binhost runs. Resolved late, so it can name a
        handler defined further down the file — `TestPeerCredential` swaps in one
        that demands the credential instead of ignoring it."""
        return _QuietHandler

    def setUp(self):
        super().setUp()
        root = self.tmp / "binpkgs"
        (root / "app-misc").mkdir(parents=True)
        gpkg(root / "app-misc", files=TMUX)
        (root / "Packages").write_text(
            "ARCH: arm64\nPACKAGES: 2\n\n"
            "BUILD_ID: 1\nCPV: app-misc/tmux-3.5a\nPATH: app-misc/tmux-3.5a-1.gpkg.tar\n\n"
            "CPV: evil/escape-1\nPATH: ../../etc/passwd\n",
            encoding="utf-8",
        )
        handler = partial(self.peer_handler(), directory=str(self.tmp))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/binpkgs"


class TestPeer(PeerFixture):
    """The Range handling and the index are the point, so nothing here is mocked."""

    def test_peer_index_lists_packages_and_drops_escaping_paths(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            sources = binpkg_mod.peer_sources(self.base)
        self.assertEqual(sources, [f"{self.base}/app-misc/tmux-3.5a-1.gpkg.tar"])
        self.assertIn("dropped index entry", out.getvalue())

    def test_fetches_a_peers_package_without_the_image(self):
        meta = binpkg_mod.fetch(f"{self.base}/app-misc/tmux-3.5a-1.gpkg.tar")
        self.assertEqual(meta.atom, "app-misc/tmux")
        self.assertEqual(meta.build_time, 1785171285)

    def test_fetch_reads_only_the_head_of_a_package_bigger_than_the_window(self):
        """The case that matters: a real package is megabytes and the image is all of it.

        `http.server` ignores Range, so this also covers the server that answers
        200 with the whole object. The cap is set below the file size on purpose:
        needing the full download would fail the test rather than pass it slowly.
        """
        root = self.tmp / "binpkgs" / "app-misc"
        gpkg(root, files=TMUX, basename="big-1.0-1", image=b"\x00" * (2 << 20))
        size = (root / "big-1.0-1.gpkg.tar").stat().st_size
        self.assertGreater(size, 1 << 20)
        meta = binpkg_mod.fetch(
            f"{self.base}/app-misc/big-1.0-1.gpkg.tar",
            probe_bytes=64 << 10, cap=100_000,
        )
        self.assertEqual(meta.atom, "app-misc/tmux")

    def test_fetching_an_xpak_falls_back_to_the_whole_object(self):
        """An xpak's trailer is at the far end, so the head slice cannot decide."""
        xpak_tbz2(self.tmp / "binpkgs" / "app-misc", files=TMUX)
        meta = binpkg_mod.fetch(f"{self.base}/app-misc/tmux-3.5a.tbz2", probe_bytes=512)
        self.assertEqual(meta.format, "xpak")
        self.assertEqual(meta.atom, "app-misc/tmux")

    def test_a_whole_object_fetch_still_certifies_against_the_manifest(self):
        """`exact` has to stay reachable over --peer, or the verdict is useless.

        A package smaller than the probe window arrives whole, so its Manifest is
        there to check and nothing is left undetermined.
        """
        meta = binpkg_mod.fetch(f"{self.base}/app-misc/tmux-3.5a-1.gpkg.tar")
        self.assertEqual(meta.unread, ())
        self.assertEqual(binpkg_mod.fit(meta, self.tmux_lock()).verdict, binpkg_mod.EXACT)

    def test_a_trailer_at_the_probe_boundary_does_not_spoof_a_ranged_fetch(self):
        """The worse half of the appended-trailer bug, and the harder one to see.

        `fetch` slices exactly `probe_bytes`, so the tail of what it reads is a
        boundary the fetcher chose. Planting an xpak trailer at that offset — inside
        image.tar.zst, where it changes nothing else — let a peer serve ONE file that
        read clean to `forge binpkg --peer` while its real tail and real metadata
        stayed honest for every other reader.
        """
        root = self.tmp / "binpkgs" / "app-misc"
        dirty = dict(TMUX, USE="arm64 elibc_musl kernel_linux systemd\n")
        path = gpkg(root, files=dirty, basename="planted-1", image=b"\x00" * (1 << 18))
        probe = 1 << 16
        raw = path.read_bytes()
        claim = xpak_segment({k: TMUX[k] for k in ("CATEGORY", "PF", "USE", "IUSE", "CHOST")})
        path.write_bytes(raw[:probe - len(claim)] + claim + raw[probe:])
        self.assertEqual(len(path.read_bytes()), len(raw), "the file's length must not change")

        meta = binpkg_mod.fetch(f"{self.base}/app-misc/planted-1.gpkg.tar", probe_bytes=probe)
        self.assertEqual(meta.format, "gpkg-1")
        self.assertIn("systemd", meta.use)
        verdict = binpkg_mod.fit(meta, self.tmux_lock())
        self.assertEqual(verdict.verdict, binpkg_mod.MISMATCH)
        self.assertFalse(verdict.ok)
        # A ranged read cannot see the Manifest, and says so instead of implying it did.
        self.assertTrue(any("only the head" in note for note in meta.unread), meta.unread)

    def test_missing_package_on_the_peer_is_reported(self):
        with self.assertRaises(binpkg_mod.BinpkgError) as caught:
            binpkg_mod.fetch(f"{self.base}/app-misc/absent-1.gpkg.tar")
        self.assertIn("404", str(caught.exception))

    def test_cli_against_a_peer(self):
        lock = write_lock(self.tmp, packages=[pkg("app-misc/tmux", TMUX_LOCK_USE)])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["--lock", str(lock), "binpkg", "--peer", self.base])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("app-misc/tmux-3.5a", out.getvalue())


class TestPeerCredential(PeerFixture):
    """A peer URL authenticates by userinfo, so `--peer` is handed a live token.

    `portage.binhost_env` puts the credential in the URL because portage fetches a
    binhost with wget/curl and PORTAGE_BINHOST has no header to set, and
    `aios.mesh.binhost_url` builds exactly that URL — its own docstring says it is
    unredacted. Two things have to hold for such a URL, and they pull in opposite
    directions: the credential must reach the WIRE, and it must reach nothing else.

    urllib gave neither. It does not implement userinfo — `Request._parse` hands
    `x:tok@host:port` to `http.client` as the hostname — so every fetch died in DNS,
    and that message is prefixed with the URL, which put the live token on the
    terminal. `_credential` moves it into an RFC 7617 header, so this suite's binhost
    DEMANDS that header: every test below that reads anything is also proof the
    credential was spent, and no stand-in transport stands between forge and the
    socket.
    """

    #: Distinctive enough that a substring search cannot pass by accident, and shaped
    #: like the mesh token it stands in for.
    TOKEN = "s3cr3t-mesh-token"

    def peer_handler(self):
        return partial(_AuthHandler, expect=basic_auth("x", self.TOKEN))

    def authed(self, path: str = "", token: str | None = None) -> str:
        scheme, _, rest = self.base.partition("://")
        return f"{scheme}://x:{self.TOKEN if token is None else token}@{rest}{path}"

    def host_port(self) -> str:
        return f"127.0.0.1:{self.server.server_address[1]}"

    def test_a_userinfo_url_is_fetched_by_spending_the_credential(self):
        """The bug itself: `--peer http://x:tok@host/` could not read a binhost at all.

        The peer answers 401 to a request without the header, so reading tmux's
        metadata off it is not merely "the fetch stopped failing" — it is the token
        arriving where wget and curl would have put it.
        """
        meta = binpkg_mod.fetch(self.authed("/app-misc/tmux-3.5a-1.gpkg.tar"))
        self.assertEqual(meta.atom, "app-misc/tmux")
        self.assertEqual(meta.build_time, 1785171285)
        self.assertNotIn(self.TOKEN, meta.source)

    def test_the_peer_really_refuses_a_fetch_that_spends_nothing(self):
        """Guards the test above: without it, an ungated server would pass it too."""
        with self.assertRaises(binpkg_mod.BinpkgError) as caught:
            binpkg_mod.fetch(f"{self.base}/app-misc/tmux-3.5a-1.gpkg.tar")
        self.assertIn("401", str(caught.exception))

    def test_a_wrong_token_is_a_401_and_is_not_quoted_back(self):
        with self.assertRaises(binpkg_mod.BinpkgError) as caught:
            binpkg_mod.fetch(self.authed("/app-misc/tmux-3.5a-1.gpkg.tar", token="wrong"))
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("wrong", str(caught.exception))

    def test_a_successful_peer_check_names_the_host_and_not_the_token(self):
        lock = write_lock(self.tmp, packages=[pkg("app-misc/tmux", TMUX_LOCK_USE)])
        code, out = self.run_cli("--lock", str(lock), "binpkg", "--peer", self.authed())
        self.assertEqual(code, 0, out)
        self.assertIn("app-misc/tmux-3.5a", out, "the check has to have really run")
        self.assertNotIn(self.TOKEN, out)
        # Redacted, not suppressed: which peer answered is the point of the output.
        self.assertIn(self.host_port(), out)
        self.assertIn(portage_mod.REDACTION, out)

    def test_a_read_failure_quotes_the_url_redacted(self):
        """The error path is the leakiest: ~30 messages are prefixed with the source."""
        lock = write_lock(self.tmp, packages=[pkg("app-misc/tmux", TMUX_LOCK_USE)])
        code, out = self.run_cli("--lock", str(lock), "binpkg",
                                 self.authed("/app-misc/absent-1.gpkg.tar"))
        self.assertEqual(code, 1, out)
        self.assertIn("404", out, "the failure has to be the one we asked for")
        self.assertNotIn(self.TOKEN, out)
        self.assertIn(self.host_port(), out)

    def test_a_dropped_index_entry_does_not_quote_the_token_either(self):
        """The index is a peer's text, and the line rejecting it names the binhost."""
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            sources = binpkg_mod.peer_sources(self.authed())
        printed = buf.getvalue()
        self.assertIn("dropped index entry", printed)
        self.assertNotIn(self.TOKEN, printed)
        # The URL that gets FETCHED still carries the credential — redacting that one
        # would break the fetch instead of protecting it.
        self.assertEqual(len(sources), 1)
        self.assertIn(self.TOKEN, sources[0])

    def test_the_fit_a_caller_may_log_carries_no_credential(self):
        """`Fit.source` and `Metadata.source` are report fields, not fetch targets."""
        lock = self.tmux_lock()
        fit = binpkg_mod.examine(self.authed("/app-misc/tmux-3.5a-1.gpkg.tar"), lock)
        broken = binpkg_mod.examine(self.authed("/app-misc/absent-1.gpkg.tar"), lock)
        self.assertEqual(fit.verdict, binpkg_mod.EXACT)
        for text in (fit.source, fit.meta.source, *(r.text for r in fit.reasons)):
            self.assertNotIn(self.TOKEN, text)
        self.assertEqual(broken.verdict, binpkg_mod.ERROR)
        for text in (broken.source, *(r.text for r in broken.reasons)):
            self.assertNotIn(self.TOKEN, text)

    def test_the_whole_object_fallback_authenticates_too(self):
        """`fetch` dials twice for an xpak, and the second dial is a separate Request."""
        xpak_tbz2(self.tmp / "binpkgs" / "app-misc", files=TMUX)
        meta = binpkg_mod.fetch(self.authed("/app-misc/tmux-3.5a.tbz2"), probe_bytes=512)
        self.assertEqual(meta.format, "xpak")
        self.assertEqual(meta.atom, "app-misc/tmux")

    def test_a_percent_encoded_token_is_decoded_the_way_wget_decodes_it(self):
        """Two fetchers, one URL: forge must send the credential portage sends.

        wget and curl percent-decode a URL's userinfo before authenticating with it,
        so a `+` written `%2B` is a `+` on the wire for them. Sending the literal
        `%2B` instead would authenticate for exactly one of the two tools.
        """
        self.assertEqual(
            binpkg_mod._credential("http://portage:a%2Bb%40c@host:4748/binpkgs"),
            ("http://host:4748/binpkgs", basic_auth("portage", "a+b@c")),
        )

    def test_a_url_without_userinfo_is_dialled_unchanged_and_unauthenticated(self):
        self.assertEqual(
            binpkg_mod._credential("http://aios-repo:8080/binpkgs"),
            ("http://aios-repo:8080/binpkgs", ""),
        )


class TestPeerCredentialRedirect(Fixture):
    """Where the credential may follow a 3xx, and where it must not.

    `add_unredirected_header` alone drops it on every redirect, which is safe and
    breaks the ordinary binhost that 302s a path to another path on itself.
    `_PinnedRedirect` re-attaches it only after proving the target is the same
    origin, so these two cases are the whole rule.
    """

    TOKEN = "s3cr3t-mesh-token"

    def setUp(self):
        super().setUp()
        _Internal.hits = 0
        _Internal.credentials = []
        self.internal = self.serve(_Internal)
        root = self.tmp / "binpkgs" / "app-misc"
        root.mkdir(parents=True)
        gpkg(root, files=TMUX)

    def serve(self, handler) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_a_same_origin_redirect_still_carries_the_credential(self):
        base = self.serve(partial(
            _AuthAliasHandler, directory=str(self.tmp), expect=basic_auth("x", self.TOKEN)
        ))
        scheme, _, rest = base.partition("://")
        url = f"{scheme}://x:{self.TOKEN}@{rest}/alias/binpkgs/app-misc/tmux-3.5a-1.gpkg.tar"
        meta = binpkg_mod.fetch(url)
        self.assertEqual(meta.atom, "app-misc/tmux")

    def test_a_redirect_to_another_host_never_sees_the_credential(self):
        peer = self.serve(type("_ToInternal3", (_Redirector,), {"target": self.internal + "/x"}))
        scheme, _, rest = peer.partition("://")
        with self.assertRaises(binpkg_mod.BinpkgError) as caught:
            binpkg_mod.fetch(f"{scheme}://x:{self.TOKEN}@{rest}/app-misc/tmux-3.5a-1.gpkg.tar")
        self.assertIn("refused", str(caught.exception))
        self.assertNotIn(self.TOKEN, str(caught.exception))
        self.assertEqual(_Internal.hits, 0, "the redirect target was contacted anyway")
        self.assertEqual(_Internal.credentials, [])


class TestPeerRedirect(Fixture):
    """A peer does not get to choose what this fetches.

    urllib follows 3xx transparently and the default opener will follow one to any
    host, and to ftp://. The index and the packages are a peer's, so that made
    `forge binpkg --peer` a server-side request forgery primitive: a compromised
    binhost could 302 the fetch at cloud metadata, or at an admin port on the node's
    own loopback, and forge would fetch it and hand the body back as a package.
    """

    def setUp(self):
        super().setUp()
        _Internal.hits = 0
        self.internal = self.serve(_Internal)
        root = self.tmp / "binpkgs" / "app-misc"
        root.mkdir(parents=True)
        gpkg(root, files=TMUX)

    def serve(self, handler) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_a_redirect_to_another_host_is_refused_and_not_followed(self):
        peer = self.serve(type("_ToInternal", (_Redirector,), {"target": self.internal + "/x"}))
        with self.assertRaises(binpkg_mod.BinpkgError) as caught:
            binpkg_mod.fetch(f"{peer}/app-misc/tmux-3.5a-1.gpkg.tar")
        self.assertIn("refused", str(caught.exception))
        self.assertEqual(_Internal.hits, 0, "the redirect target was contacted anyway")

    def test_a_redirect_to_a_non_http_scheme_is_refused(self):
        peer = self.serve(type("_ToFile", (_Redirector,), {"target": "ftp://127.0.0.1/x"}))
        with self.assertRaises(binpkg_mod.BinpkgError) as caught:
            binpkg_mod.fetch(f"{peer}/app-misc/tmux-3.5a-1.gpkg.tar")
        self.assertIn("refused", str(caught.exception))

    def test_the_peer_index_is_fetched_under_the_same_rule(self):
        peer = self.serve(type("_ToInternal2", (_Redirector,), {"target": self.internal + "/i"}))
        with self.assertRaises(binpkg_mod.BinpkgError):
            binpkg_mod.peer_sources(f"{peer}/binpkgs")
        self.assertEqual(_Internal.hits, 0)

    def test_a_same_origin_redirect_is_still_followed(self):
        """Pinning, not banning: a binhost that 302s to a path on itself still works."""
        base = self.serve(partial(_AliasHandler, directory=str(self.tmp)))
        meta = binpkg_mod.fetch(f"{base}/alias/binpkgs/app-misc/tmux-3.5a-1.gpkg.tar")
        self.assertEqual(meta.atom, "app-misc/tmux")


class _Redirector(BaseHTTPRequestHandler):
    """302s every request to `target`, whatever was asked for."""

    target = ""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", self.target)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Internal(BaseHTTPRequestHandler):
    """Stands in for whatever else the node can reach. Counts every hit."""

    hits = 0
    #: Every Authorization header this was offered. A redirect that leaks a peer's
    #: token to a third host is a worse outcome than one that merely fetches from it.
    credentials: list[str] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        _Internal.hits += 1
        if self.headers.get("Authorization"):
            _Internal.credentials.append(self.headers["Authorization"])
        body = b"SECRET-internal-credentials-body"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _AliasHandler(SimpleHTTPRequestHandler):
    """Serves the tree, and redirects /alias/<path> to /<path> on this same origin."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/alias/"):
            self.send_response(302)
            self.send_header("Location", self.path[len("/alias"):])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output readable
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            # A client that reads the head of a package and hangs up is the
            # behaviour under test, not a server fault. Without this the
            # stdlib prints a traceback over the test output.
            self.close_connection = True


def basic_auth(user: str, password: str) -> str:
    """The RFC 7617 header wget and curl send for `http://user:password@host/`."""
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")


class _AuthHandler(_QuietHandler):
    """A token-gated binhost: 401 to anything that does not present the credential.

    The one thing a stand-in transport cannot check is whether the token reaches the
    wire at all. Serving the tree only to a correct Authorization header makes every
    successful read in `TestPeerCredential` evidence that it did.
    """

    def __init__(self, *args, expect: str, **kwargs):
        # Before super(): BaseHTTPRequestHandler.__init__ handles the whole request.
        self.expect = expect
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.headers.get("Authorization") != self.expect:
            body = b"unauthorized\n"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="binhost"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class _AuthAliasHandler(_AuthHandler):
    """Token-gated, and 302s /alias/<path> to /<path> on this same origin.

    The redirect itself is answered unauthenticated, as a front end that routes
    before it authenticates would: the credential has to survive onto the SECOND
    request or the package comes back 401.
    """

    def do_GET(self):
        if self.path.startswith("/alias/"):
            self.send_response(302)
            self.send_header("Location", self.path[len("/alias"):])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()


class TestCli(Fixture):
    def test_exit_zero_and_table_on_a_fitting_package(self):
        lock = write_lock(self.tmp, packages=[pkg("app-misc/tmux", TMUX_LOCK_USE)])
        path = gpkg(self.tmp, files=TMUX)
        code, out = self.run_cli("--lock", str(lock), "binpkg", str(path))
        self.assertEqual(code, 0, out)
        self.assertIn("verdict", out)
        self.assertIn("exact", out)
        self.assertIn("1/1 reusable", out)

    def test_exit_nonzero_on_mismatch(self):
        lock = write_lock(self.tmp, packages=[pkg("app-misc/tmux", TMUX_LOCK_USE)])
        path = gpkg(self.tmp, files=dict(TMUX, USE="arm64 elibc_musl kernel_linux systemd\n"))
        code, out = self.run_cli("--lock", str(lock), "binpkg", str(path))
        self.assertEqual(code, 1, out)
        self.assertIn("want -systemd, built +systemd", out)
        self.assertIn("0/1 reusable", out)

    def test_exit_nonzero_on_foreign(self):
        lock = write_lock(self.tmp, packages=[pkg("app-misc/tmux", TMUX_LOCK_USE)])
        path = gpkg(self.tmp, files=dict(TMUX, CHOST="x86_64-pc-linux-gnu\n"))
        code, out = self.run_cli("--lock", str(lock), "binpkg", str(path))
        self.assertEqual(code, 1, out)
        self.assertIn("FOREIGN", out)

    def test_exit_nonzero_on_a_contradiction_that_the_flags_hide(self):
        lock = write_lock(self.tmp, packages=[pkg("app-editors/vim", [
            {"flag": "X", "enabled": False, "why": "intent[0]: no X11"},
        ])])
        path = gpkg(self.tmp, files=TestContradiction.VIM, basename="vim-9.1.0866-1")
        code, out = self.run_cli("--lock", str(lock), "binpkg", str(path))
        self.assertEqual(code, 1, out)
        self.assertIn("!! link", out)
        self.assertIn("libX11.so.6", out)

    def test_exit_nonzero_on_an_unreadable_package(self):
        lock = write_lock(self.tmp, packages=[pkg("app-misc/tmux", TMUX_LOCK_USE)])
        path = self.tmp / "broken.gpkg.tar"
        path.write_bytes(b"garbage")
        code, out = self.run_cli("--lock", str(lock), "binpkg", str(path))
        self.assertEqual(code, 1, out)
        self.assertIn("ERROR", out)

    def test_dir_walks_a_cache(self):
        cache = self.tmp / "binpkgs" / "app-misc"
        cache.mkdir(parents=True)
        gpkg(cache, files=TMUX)
        gpkg(cache, files=dict(TMUX, USE="arm64 elibc_musl kernel_linux debug\n"),
             basename="tmux-3.5a-2")
        lock = write_lock(self.tmp, packages=[pkg("app-misc/tmux", TMUX_LOCK_USE)])
        code, out = self.run_cli("--lock", str(lock), "binpkg", "--dir", str(self.tmp / "binpkgs"))
        self.assertEqual(code, 1, out)
        self.assertIn("want -debug, built +debug", out)
        self.assertIn("1/2 reusable", out)

    def test_no_sources_is_a_message_not_a_traceback(self):
        lock = write_lock(self.tmp, packages=[])
        code, out = self.run_cli("--lock", str(lock), "binpkg")
        self.assertEqual(code, 1, out)
        self.assertIn("nothing to check", out)

    def test_atom_override_compares_against_a_different_lock_entry(self):
        lock = write_lock(self.tmp, packages=[pkg("app-misc/tmux", TMUX_LOCK_USE)])
        path = gpkg(self.tmp, files=dict(TMUX, CATEGORY="app-i-forgot\n"))
        code, out = self.run_cli("--lock", str(lock), "binpkg", "--atom", "app-misc/tmux",
                                 str(path))
        self.assertEqual(code, 0, out)
        self.assertIn("exact", out)


# --- helpers ----------------------------------------------------------------


def _zstd_available() -> bool:
    try:
        import compression.zstd  # noqa: F401
    except ImportError:
        return shutil.which("zstd") is not None
    return True


def _zstd_compress(blob: bytes) -> bytes:
    try:
        from compression import zstd
    except ImportError:
        import subprocess

        return subprocess.run(
            [shutil.which("zstd"), "-q", "-c"], input=blob, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=True,
        ).stdout
    return zstd.compress(blob)


if __name__ == "__main__":
    unittest.main()
