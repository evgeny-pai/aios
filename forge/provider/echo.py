"""Offline stub backend: no network, fully deterministic.

Two jobs. It makes the test suite hermetic, and it lets `forge lower` be
demonstrated end-to-end on a machine with no credentials — the pipeline below
the lowering pass never knows the difference, which is the point of the
provider layer.

Set AIOS_ECHO_FIXTURE to a JSON file to replay a captured real lowering; that is
how a regression is pinned once a live model produces a bad decision.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import ProviderError

#: A plausible hand-written lowering for the starter spec. Not model output —
#: just enough shape for the rest of the pipeline to be exercised honestly.
FIXTURE: dict = {
    "packages": [
        {
            "atom": "app-editors/vim",
            "why": "intent[0]: edit code over ssh",
            "use": [
                {"flag": "syntax", "enabled": True, "why": "intent[0]: syntax highlighting"},
                {"flag": "X", "enabled": False, "why": "intent[0]: no X11"},
                {"flag": "gui", "enabled": False, "why": "intent[0]: no X11"},
                {"flag": "nls", "enabled": False, "why": "no intent requires localization"},
                {"flag": "perl", "enabled": False, "why": "no intent requires perl scripting"},
                {"flag": "python", "enabled": False, "why": "no intent requires python scripting"},
                {"flag": "ruby", "enabled": False, "why": "no intent requires ruby scripting"},
                {"flag": "acl", "enabled": False, "why": "no intent requires POSIX ACLs"},
            ],
            "accept_keywords": [],
            "probes": ["vim"],
        },
        {
            "atom": "sys-devel/make",
            "why": "intent[1]: build C projects with make",
            "use": [{"flag": "nls", "enabled": False, "why": "no intent requires localization"}],
            "accept_keywords": [],
            "probes": [],
        },
        {
            "atom": "net-misc/openssh",
            "why": "intent[2]: reach the machine over ssh",
            "use": [
                {"flag": "X509", "enabled": False, "why": "intent[2]: key auth only"},
                {"flag": "pam", "enabled": False, "why": "intent[2]: no password auth"},
                {"flag": "kerberos", "enabled": False, "why": "intent[2]: key auth only"},
                {"flag": "ldns", "enabled": False, "why": "no intent requires SSHFP validation"},
            ],
            "accept_keywords": [],
            "probes": [],
        },
    ],
    "make_conf": [
        {
            "key": "USE",
            "value": "-X -gtk -gnome -kde -qt5 -wayland -systemd -nls",
            "why": "no intent implies a graphical session or localization",
        },
        {
            "key": "FEATURES",
            "value": "buildpkg",
            "why": "minimization rebuilds packages repeatedly; cache them",
        },
    ],
    "notes": ["produced by the offline echo backend — not a real lowering"],
}


class EchoProvider:
    def __init__(self, model: str = "") -> None:
        self.name = "echo"
        self.model = model or "fixture"
        self.fixture_path = os.environ.get("AIOS_ECHO_FIXTURE", "")

    def describe(self) -> str:
        return f"echo:{self.fixture_path or 'builtin'} (offline, deterministic)"

    def complete_json(self, *, system: str, prompt: str, schema: dict) -> dict:
        if self.fixture_path:
            path = Path(self.fixture_path)
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise ProviderError(f"AIOS_ECHO_FIXTURE={path}: {exc}") from None
            except json.JSONDecodeError as exc:
                raise ProviderError(f"AIOS_ECHO_FIXTURE={path}: {exc}") from None

        properties = (schema or {}).get("properties", {})
        if "packages" in properties and "make_conf" in properties:
            return json.loads(json.dumps(FIXTURE))
        return _skeleton(schema)


def _skeleton(schema: dict):
    """Smallest value satisfying a schema, for schemas the fixture doesn't cover."""
    kind = schema.get("type")
    if kind == "object":
        return {
            key: _skeleton(sub) for key, sub in (schema.get("properties") or {}).items()
        }
    if kind == "array":
        return []
    if kind == "boolean":
        return False
    if kind in ("integer", "number"):
        return 0
    return ""
