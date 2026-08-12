"""Tests for the status-mesh push, run with no network and no real daemon.

Same shape as test_skills.py and test_mesh.py. What's worth pinning:

- facts() reports MEASURED values (packages actually under /var/db/pkg), not
  the lockfile's declared list — skills/status-must-report-effective-values is
  the reason this module exists at all, so its own output had better not repeat
  the mistake it was written to fix elsewhere;
- a missing/unreadable lockfile does not crash the report, it says "unknown";
- the pushed repo_status row targets the canonical repo string, not whatever
  this checkout's own remote happens to be spelled as;
- since is "now", not 0, and the port header is never sent — same reasoning as
  aios.skills, verified the same way;
- a transport failure returns a one-line string and never raises.

    python3 -m unittest aios.test_status -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path

from . import status


@contextmanager
def environment(**values: str | None):
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Fake:
    def __init__(self, *responses: object) -> None:
        self.queue = list(responses)
        self.calls: list[tuple[str, dict, bytes]] = []

    def __call__(self, url: str, headers: dict, body: bytes, timeout: float) -> bytes:
        self.calls.append((url, headers, body))
        if not self.queue:
            raise AssertionError(f"transport ran dry after {len(self.calls)} requests")
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, bytes) else json.dumps(item).encode("utf-8")


class FactsTests(unittest.TestCase):
    def test_reports_measured_package_count_not_lock_declared_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "aios.lock.json").write_text(
                json.dumps({"digest": "sha256:" + "a" * 64, "packages": [1, 2, 3, 4, 5]})
            )
            pkg_db = root / "pkgdb"
            (pkg_db / "app-misc" / "opencode-1.18.16").mkdir(parents=True)
            (pkg_db / "app-editors" / "vim-9.1").mkdir(parents=True)
            text = status.facts(root, pkg_db=pkg_db)
            self.assertIn("2 packages installed", text)
            self.assertNotIn("5 packages", text)

    def test_missing_lockfile_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = status.facts(Path(tmp), pkg_db=Path(tmp) / "no-such-pkg-db")
            self.assertIn("lock unknown", text)

    def test_installed_count_sums_versions_across_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)
            (db / "app-misc" / "opencode-1.18.16").mkdir(parents=True)
            (db / "app-editors" / "vim-9.1").mkdir(parents=True)
            (db / "sys-apps" / "coreutils-9.11").mkdir(parents=True)
            self.assertEqual(status._installed_count(db), 3)

    def test_nonexistent_pkg_db_reports_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(status._installed_count(Path(tmp) / "does-not-exist"), 0)


class PushTests(unittest.TestCase):
    def test_empty_focus_makes_no_request(self) -> None:
        fake = Fake()
        result = status.push("", transport=fake)
        self.assertEqual(result, "no status to push")
        self.assertEqual(fake.calls, [])

    def test_targets_canonical_repo_string(self) -> None:
        fake = Fake({"now": 1, "peer_state": {}, "pull": {}})
        status.push("some status", transport=fake)
        _, _, body = fake.calls[0]
        row = json.loads(body)["push"]["repo_status"][0]
        self.assertEqual(row["repo"], "github.com/evgeny-pai/aios")
        self.assertEqual(row["focus"], "some status")

    def test_never_sends_port_header(self) -> None:
        fake = Fake({"now": 1, "peer_state": {}, "pull": {}})
        status.push("s", by="aios-1-anvil", transport=fake)
        _, headers, _ = fake.calls[0]
        self.assertNotIn("x-conductorai-port", headers)
        self.assertEqual(headers.get("x-conductorai-host"), "aios-1-anvil")

    def test_since_is_now_not_zero(self) -> None:
        fake = Fake({"now": 1, "peer_state": {}, "pull": {}})
        status.push("s", transport=fake)
        _, _, body = fake.calls[0]
        payload = json.loads(body)
        self.assertGreater(payload["since"], 1_700_000_000_000)
        self.assertEqual(payload["push"]["memory"], [])

    def test_token_rides_as_bearer_only(self) -> None:
        fake = Fake({"now": 1, "peer_state": {}, "pull": {}})
        with environment(CONDUCTORAI_SHARE_TOKEN="sh4re-t0ken"):
            status.push("s", transport=fake)
        _, headers, body = fake.calls[0]
        self.assertEqual(headers.get("Authorization"), "Bearer sh4re-t0ken")
        self.assertNotIn("sh4re-t0ken", body.decode())

    def test_transport_failure_never_raises(self) -> None:
        fake = Fake(urllib.error.URLError("connection refused"))
        result = status.push("s", transport=fake)
        self.assertIn("push failed", result)

    def test_http_error_never_raises(self) -> None:
        fake = Fake(urllib.error.HTTPError("http://x", 401, "unauthorized", {}, None))
        result = status.push("s", transport=fake)
        self.assertIn("push failed", result)

    def test_success_message_names_endpoint(self) -> None:
        fake = Fake({"now": 1, "peer_state": {}, "pull": {}})
        with environment(AIOS_MESH_URL="http://host.docker.internal:4748"):
            result = status.push("s", transport=fake)
        self.assertIn("host.docker.internal:4748", result)


class MainTests(unittest.TestCase):
    def test_bad_args_is_usage_error(self) -> None:
        self.assertEqual(status.main([]), 2)
        self.assertEqual(status.main(["bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
