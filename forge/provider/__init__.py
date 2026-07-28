"""Pluggable model backends for the lowering pass.

Every provider implements exactly one operation: given a system prompt, a user
prompt, and a JSON Schema, return a dict that validates against that schema.
That is the whole surface the pipeline needs, which is what makes the backends
interchangeable — hosted API today, local weights on the target later, with no
change above this layer.

Transport is stdlib `urllib` rather than each vendor's SDK, deliberately: this
code has to run on a bare aarch64/musl target where the only interpreter is the
python3 portage already requires. A pip dependency tree is not available there,
and a provider layer that only works on the build host defeats the point.

Schemas passed in here must stay inside the intersection of what every backend
accepts: every object closed (`additionalProperties: false`) with all of its
properties listed in `required`, no recursion, and no numeric or string
constraints (`minimum`, `maxLength`, ...). Use empty arrays instead of omitting
a field.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Protocol, runtime_checkable

TIMEOUT = float(os.environ.get("AIOS_HTTP_TIMEOUT", "600"))


class ProviderError(Exception):
    """The backend failed, refused, or returned something unusable."""


@runtime_checkable
class Provider(Protocol):
    name: str
    model: str

    def complete_json(self, *, system: str, prompt: str, schema: dict) -> dict:
        """Return a dict validating against `schema`, or raise ProviderError."""

    def describe(self) -> str: ...


def post_json(url: str, headers: dict[str, str], payload: dict, *, timeout: float = TIMEOUT) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"content-type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise ProviderError(f"{url} -> HTTP {exc.code}\n{detail}") from None
    except urllib.error.URLError as exc:
        raise ProviderError(f"{url} unreachable: {exc.reason}") from None
    except TimeoutError:
        raise ProviderError(f"{url} timed out after {timeout:.0f}s") from None
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{url} returned non-JSON: {exc}") from None


def parse_json_text(text: str, *, who: str) -> dict:
    """Parse a model's JSON answer, tolerating a stray fenced code block."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{who} did not return JSON ({exc}):\n{text[:800]}") from None
    if not isinstance(value, dict):
        raise ProviderError(f"{who} returned {type(value).__name__}, expected a JSON object")
    return value


def load(
    provider: str, model: str = "", *, effort: str = "medium", respect_env: bool = True
) -> Provider:
    """Instantiate a provider by name. Env vars win over the spec.

    AIOS_PROVIDER, AIOS_MODEL, AIOS_EFFORT override their spec counterparts so a
    build host and an on-target agent can share one committed spec.

    `respect_env=False` is for building the links of a fallback chain. Without it,
    AIOS_PROVIDER=fallback would win again for every link and the chain would resolve
    to itself forever — the env override is right for the caller's choice of backend
    and wrong for a backend the chain has already chosen.
    """
    if respect_env:
        provider = os.environ.get("AIOS_PROVIDER", provider or "anthropic")
        model = os.environ.get("AIOS_MODEL", model)
    else:
        provider = provider or "anthropic"
    effort = os.environ.get("AIOS_EFFORT", effort)

    if provider == "fallback":
        from .fallback import build

        # The spec's provider/model describe ONE backend; the chain needs to know which
        # link they belong to, so they are passed through rather than applied here.
        spec_provider = os.environ.get("AIOS_SPEC_PROVIDER", "")
        return build(spec_provider, model, effort=effort)
    if provider == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(model=model, effort=effort)
    if provider == "openai":
        from .openai import OpenAICompatibleProvider

        return OpenAICompatibleProvider(model=model)
    if provider == "ollama":
        from .ollama import OllamaProvider

        return OllamaProvider(model=model)
    if provider == "echo":
        from .echo import EchoProvider

        return EchoProvider(model=model)
    raise ProviderError(
        f"unknown provider {provider!r} (anthropic, openai, ollama, echo, fallback)"
    )
