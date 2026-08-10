"""Tests for the skills-mesh push, run with no network and no real daemon.

Same shape as test_mesh.py: a scripted fake transport, no real socket anywhere.
What's worth pinning:

- discover() parses the frontmatter shape every skills/*/SKILL.md actually uses,
  and skips anything malformed rather than dying on it;
- a skill's id is the SAME uuid on every call for the same (repo, slug) — that is
  what lets a re-push UPDATE instead of duplicating the row in ConductorAI's
  shared store;
- the request never carries an x-conductorai-port header — that header is what
  makes the daemon register the caller as a peer with a callback address nothing
  is listening on (see aios.skills's module docstring);
- `since` is set to "now", not 0 — a push-only client has no use for the mesh's
  entire history back;
- a transport failure returns a one-line string and never raises — a mesh push
  must not cost the caller its turn;
- the token, when set, rides as a Bearer header and nowhere else.

    python3 -m unittest aios.test_skills -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path

from . import mesh, skills


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
    """A scripted POST transport. Records every request; replays queued responses."""

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


def _write_skill(root: Path, slug: str, name: str, description: str, body: str) -> Path:
    path = root / "skills" / slug / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")
    return path


class DiscoverTests(unittest.TestCase):
    def test_parses_frontmatter_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root, "a-skill", "a-skill", "when to use it", "# body\ntext")
            found = skills.discover(root)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].slug, "a-skill")
            self.assertEqual(found[0].name, "a-skill")
            self.assertEqual(found[0].when_to_use, "when to use it")
            self.assertEqual(found[0].body, "# body\ntext")

    def test_skips_malformed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills" / "broken").mkdir(parents=True)
            (root / "skills" / "broken" / "SKILL.md").write_text("not frontmatter at all")
            _write_skill(root, "no-name", "", "desc", "body")
            self.assertEqual(skills.discover(root), [])

    def test_no_skills_directory_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(skills.discover(Path(tmp)), [])


class IdTests(unittest.TestCase):
    def test_stable_across_calls(self) -> None:
        self.assertEqual(
            skills._skill_id("aios", "colima-kind-lifecycle"),
            skills._skill_id("aios", "colima-kind-lifecycle"),
        )

    def test_differs_by_slug_and_repo(self) -> None:
        a = skills._skill_id("aios", "one")
        b = skills._skill_id("aios", "two")
        c = skills._skill_id("other-repo", "one")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)


class PushTests(unittest.TestCase):
    def _one(self) -> skills.Skill:
        return skills.Skill(
            slug="s", name="s", when_to_use="use it", body="the body", updated_at=1_700_000_000_000
        )

    def test_empty_list_makes_no_request(self) -> None:
        fake = Fake()
        result = skills.push([], transport=fake)
        self.assertEqual(result, "no skills to push")
        self.assertEqual(fake.calls, [])

    def test_never_sends_port_header(self) -> None:
        fake = Fake({"now": 1, "peer_state": {}, "pull": {}})
        with environment(AIOS_MESH_URL="http://host.docker.internal:4748", CONDUCTORAI_SHARE_TOKEN=None):
            skills.push([self._one()], by="aios-1-anvil", transport=fake)
        self.assertEqual(len(fake.calls), 1)
        _, headers, _ = fake.calls[0]
        self.assertNotIn("x-conductorai-port", headers)
        self.assertEqual(headers.get("x-conductorai-host"), "aios-1-anvil")

    def test_since_is_now_not_zero(self) -> None:
        fake = Fake({"now": 1, "peer_state": {}, "pull": {}})
        skills.push([self._one()], transport=fake)
        _, _, body = fake.calls[0]
        payload = json.loads(body)
        self.assertGreater(payload["since"], 1_700_000_000_000)
        self.assertEqual(payload["push"]["memory"], [])
        self.assertEqual(payload["push"]["repo_status"], [])

    def test_row_shape_and_deterministic_id(self) -> None:
        fake = Fake({"now": 1, "peer_state": {}, "pull": {}})
        skills.push([self._one()], repo="aios", transport=fake)
        _, _, body = fake.calls[0]
        row = json.loads(body)["push"]["skills"][0]
        self.assertEqual(row["id"], skills._skill_id("aios", "s"))
        self.assertEqual(row["name"], "s")
        self.assertEqual(row["when_to_use"], "use it")
        self.assertEqual(row["body"], "the body")
        self.assertEqual(json.loads(row["tags"]), ["aios", "aios-authored"])
        self.assertEqual(row["deleted_at"], None)

    def test_token_rides_as_bearer_only(self) -> None:
        fake = Fake({"now": 1, "peer_state": {}, "pull": {}})
        with environment(CONDUCTORAI_SHARE_TOKEN="sh4re-t0ken"):
            skills.push([self._one()], transport=fake)
        _, headers, body = fake.calls[0]
        self.assertEqual(headers.get("Authorization"), "Bearer sh4re-t0ken")
        self.assertNotIn("sh4re-t0ken", body.decode())

    def test_no_token_sends_no_authorization_header(self) -> None:
        fake = Fake({"now": 1, "peer_state": {}, "pull": {}})
        with environment(CONDUCTORAI_SHARE_TOKEN=None):
            skills.push([self._one()], transport=fake)
        _, headers, _ = fake.calls[0]
        self.assertNotIn("Authorization", headers)

    def test_transport_failure_never_raises(self) -> None:
        fake = Fake(urllib.error.URLError("connection refused"))
        result = skills.push([self._one()], transport=fake)
        self.assertIn("push failed", result)

    def test_http_error_never_raises(self) -> None:
        fake = Fake(urllib.error.HTTPError("http://x", 401, "unauthorized", {}, None))
        result = skills.push([self._one()], transport=fake)
        self.assertIn("push failed", result)

    def test_success_message_names_count_and_endpoint(self) -> None:
        fake = Fake({"now": 1, "peer_state": {}, "pull": {}})
        with environment(AIOS_MESH_URL="http://host.docker.internal:4748"):
            result = skills.push([self._one(), self._one()], transport=fake)
        self.assertIn("2 skill(s)", result)
        self.assertIn("host.docker.internal:4748", result)


class MainTests(unittest.TestCase):
    def test_bad_args_is_usage_error(self) -> None:
        self.assertEqual(skills.main([]), 2)
        self.assertEqual(skills.main(["bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
