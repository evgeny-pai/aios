"""Tests for aios.update's git-sourced self-update path: git_head/check_git/apply_git.

The "remote" is a real local git repository, not a mock of subprocess — git
ls-remote/clone against a plain filesystem path is real git, offline, and fast
enough to run on every gate. GATE and PAYLOAD are patched to a minimal set for the
duration of each test: the release gate's own suites, and this project's real
PAYLOAD, are exercised elsewhere (the gate itself, and every real apply). What is
under test here is the git plumbing swap_in()/gate() are wired into, not those
functions' own bodies.

    python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aios import update as update_mod

#: The IMAGE has no git — it arrives at boot, restored from the binpkg cache, long
#: after the Dockerfile's release gate has already run. A skip says "not covered
#: here"; a failure would say self-update is broken on every image build, for a tool
#: this suite never had. See skills/host-dependent-assertions and tests/test_ci.py's
#: identical `needs_git`.
needs_git = unittest.skipUnless(
    shutil.which("git"), "needs git, which the image does not carry until boot restores it"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


@needs_git
class GitUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self.remote = root / "remote"
        self.remote.mkdir()
        _git(self.remote, "init", "--quiet", "--initial-branch=main")
        (self.remote / "aios").mkdir()
        (self.remote / "aios" / "marker.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.remote / "aios.toml").write_text("# spec\n", encoding="utf-8")
        (self.remote / "aios.lock.json").write_text("{}\n", encoding="utf-8")
        _git(self.remote, "add", "-A")
        _git(self.remote, "commit", "--quiet", "-m", "one")

        self.aios_root = root / "aios-root"
        self.aios_root.mkdir()

        self.src_dir = root / "srv-src"
        env_patch = mock.patch.dict(os.environ, {"AIOS_SRC_DIR": str(self.src_dir)})
        env_patch.start()
        self.addCleanup(env_patch.stop)

        for name, value in (
            ("PAYLOAD", ("aios", "aios.toml", "aios.lock.json")),
            ("INSTALL", ()),
            ("ROOT", self.aios_root),
            ("STAGE", self.aios_root.with_name(self.aios_root.name + ".next")),
            ("PREVIOUS", self.aios_root.with_name(self.aios_root.name + ".prev")),
            ("STATE", self.aios_root / ".aios"),
            ("VERSION_FILE", self.aios_root / ".code-version"),
        ):
            patch = mock.patch.object(update_mod, name, value)
            patch.start()
            self.addCleanup(patch.stop)

        gate_patch = mock.patch.object(update_mod, "gate", lambda candidate: [])
        gate_patch.start()
        self.addCleanup(gate_patch.stop)

    def _head(self) -> str:
        return subprocess.run(
            ["git", "rev-parse", "main"], cwd=self.remote,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def test_git_head_matches_the_remotes_branch(self) -> None:
        self.assertEqual(update_mod.git_head(str(self.remote), "main"), self._head())

    def test_git_head_reports_a_missing_branch(self) -> None:
        with self.assertRaises(update_mod.UpdateError):
            update_mod.git_head(str(self.remote), "no-such-branch")

    def test_check_git_reports_unknown_as_an_update(self) -> None:
        report = update_mod.check_git(str(self.remote), "main")
        self.assertIn("update available", report)
        self.assertIn("unknown", report)

    def test_apply_git_promotes_and_records_the_version(self) -> None:
        result = update_mod.apply_git(str(self.remote), "main")
        self.assertIn("promoted", result)
        self.assertEqual(
            (self.aios_root / "aios" / "marker.py").read_text(encoding="utf-8"),
            "VALUE = 1\n",
        )
        self.assertEqual(update_mod.installed(), self._head())
        # STAGE is cleaned up; nothing left half-applied.
        self.assertFalse(update_mod.STAGE.exists())

    def test_apply_git_is_a_noop_once_current(self) -> None:
        update_mod.apply_git(str(self.remote), "main")
        second = update_mod.apply_git(str(self.remote), "main")
        self.assertEqual(second, f"already running {self._head()[:12]}")

    def test_apply_git_republishes_for_peers(self) -> None:
        result = update_mod.apply_git(str(self.remote), "main")
        self.assertIn("published", result)
        manifest = json.loads((self.src_dir / update_mod.MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], self._head()[:12])
        # A peer applying from this manifest gets the same tree just promoted.
        archive = self.src_dir / manifest["path"]
        self.assertTrue(archive.is_file())
        self.assertEqual(update_mod._sha256(archive), manifest["sha256"])

    def test_apply_git_force_reapplies_the_same_commit(self) -> None:
        update_mod.apply_git(str(self.remote), "main")
        forced = update_mod.apply_git(str(self.remote), "main", force=True)
        self.assertIn("promoted", forced)

    def test_apply_git_refuses_a_candidate_that_fails_its_gate(self) -> None:
        with mock.patch.object(update_mod, "gate", lambda candidate: ["fake failure"]):
            with self.assertRaises(update_mod.UpdateError):
                update_mod.apply_git(str(self.remote), "main")
        # Refused before promotion: no version recorded, target tree untouched.
        self.assertEqual(update_mod.installed(), "")
        self.assertFalse((self.aios_root / "aios" / "marker.py").exists())

    def test_apply_git_picks_up_a_new_commit(self) -> None:
        update_mod.apply_git(str(self.remote), "main")
        first_head = self._head()

        (self.remote / "aios" / "marker.py").write_text("VALUE = 2\n", encoding="utf-8")
        _git(self.remote, "add", "-A")
        _git(self.remote, "commit", "--quiet", "-m", "two")

        result = update_mod.apply_git(str(self.remote), "main")
        self.assertIn("promoted", result)
        self.assertNotEqual(update_mod.installed(), first_head)
        self.assertEqual(
            (self.aios_root / "aios" / "marker.py").read_text(encoding="utf-8"),
            "VALUE = 2\n",
        )
        # The previous generation is kept, not discarded, for rollback.
        self.assertEqual(
            (update_mod.PREVIOUS / "aios" / "marker.py").read_text(encoding="utf-8"),
            "VALUE = 1\n",
        )


if __name__ == "__main__":
    unittest.main()
