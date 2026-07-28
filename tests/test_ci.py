"""The CI runner on the node that serves the mirrors.

The assertions that matter are about honesty of the report rather than about running a
real gate: a missing tool must be a skip with a reason, a skip must not read as green,
and a failing gate must not raise past the runner and take the other repositories'
results with it.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aios import ci as ci_mod


def bare_repo(root: Path, name: str) -> Path:
    """A real bare repo with one commit, so `git` behaves as it will in the pod."""
    work = root / f"{name}-work"
    work.mkdir(parents=True)
    run = lambda *a: subprocess.run(a, cwd=work, capture_output=True, check=True)
    run("git", "init", "--quiet", "-b", "main")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "t")
    (work / "README.md").write_text("hi\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "--quiet", "-m", "one")
    bare = root / f"{name}.git"
    subprocess.run(("git", "clone", "--bare", "--quiet", str(work), str(bare)), check=True,
                   capture_output=True)
    return bare


class TestMirrors(unittest.TestCase):
    def test_mirrors_are_named_without_the_git_suffix(self):
        root = Path(tempfile.mkdtemp())
        (root / "aios.git").mkdir()
        (root / "conductorai.git").mkdir()
        (root / "notarepo").mkdir()
        self.assertEqual(ci_mod.mirrors(root), ["aios", "conductorai"])

    def test_a_missing_git_root_is_empty_not_an_exception(self):
        self.assertEqual(ci_mod.mirrors(Path("/nonexistent/git")), [])

    def test_no_mirrors_at_all_is_an_error_worth_saying(self):
        with self.assertRaises(ci_mod.CIError):
            ci_mod.run_all(Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "r.json")


class TestRunOne(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def test_an_absent_tool_is_skipped_with_the_reason_and_the_command(self):
        """conductorai needs node; this node has none. That is not a failure."""
        bare = bare_repo(self.root, "conductorai")
        with mock.patch.object(ci_mod.shutil, "which", return_value=None):
            got = ci_mod.run_one("conductorai", bare)
        self.assertEqual(got["status"], "skipped")
        self.assertIn("npm", got["reason"])
        self.assertEqual(got["gate"], "npm test")

    def test_an_unknown_repository_is_skipped_rather_than_guessed_at(self):
        got = ci_mod.run_one("something-else", self.root / "nope.git")
        self.assertEqual(got["status"], "skipped")
        self.assertIn("no gate", got["reason"])

    def test_a_passing_gate_records_the_commit_it_tested(self):
        bare = bare_repo(self.root, "aios")
        with mock.patch.dict(ci_mod.GATES, {"aios": ("true",)}, clear=False):
            got = ci_mod.run_one("aios", bare)
        self.assertEqual(got["status"], "pass")
        self.assertRegex(got["commit"], r"^[0-9a-f]{7,}$")
        self.assertEqual(got["exit_code"], 0)

    def test_a_failing_gate_is_reported_not_raised(self):
        bare = bare_repo(self.root, "aios")
        with mock.patch.dict(ci_mod.GATES, {"aios": ("false",)}, clear=False):
            got = ci_mod.run_one("aios", bare)
        self.assertEqual(got["status"], "fail")
        self.assertNotEqual(got["exit_code"], 0)

    def test_a_hanging_gate_becomes_an_error_instead_of_hanging_the_report(self):
        bare = bare_repo(self.root, "aios")
        with mock.patch.dict(ci_mod.GATES, {"aios": ("sleep", "30")}, clear=False):
            got = ci_mod.run_one("aios", bare, timeout_s=1)
        self.assertEqual(got["status"], "error")
        self.assertIn("exceeded", got["reason"])


class TestReport(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.report = Path(tempfile.mkdtemp()) / "ci" / "latest.json"

    def test_a_report_of_only_skips_is_not_green(self):
        """The trap this guards: nothing ran, nothing failed, so it looked fine."""
        bare_repo(self.root, "conductorai")
        with mock.patch.object(ci_mod.shutil, "which", return_value=None):
            summary = ci_mod.run_all(self.root, self.report)
        self.assertTrue(summary["ok"], "a skip is not a failure")
        self.assertEqual(summary["passed"], 0)
        self.assertEqual(summary["skipped"], 1)

    def test_one_failure_fails_the_whole_report(self):
        bare_repo(self.root, "aios")
        with mock.patch.dict(ci_mod.GATES, {"aios": ("false",)}, clear=False):
            summary = ci_mod.run_all(self.root, self.report)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["failed"], 1)

    def test_the_report_is_written_where_peers_can_fetch_it(self):
        bare_repo(self.root, "aios")
        with mock.patch.dict(ci_mod.GATES, {"aios": ("true",)}, clear=False):
            ci_mod.run_all(self.root, self.report)
        written = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(written["repos"][0]["repo"], "aios")
        self.assertIn("ok", written)


if __name__ == "__main__":
    unittest.main()
