"""Anthropic Messages API backend.

Notes that matter for this model family (Claude Opus 5):

- Thinking is on by default. `max_tokens` caps thinking *plus* response text, so
  it needs headroom well above the size of the answer.
- `temperature` / `top_p` / `top_k` are rejected outright. Behaviour is steered
  by the prompt and by `output_config.effort`.
- Structured output goes in `output_config.format` as a json_schema; the older
  top-level `output_format` parameter is deprecated.
- A safety refusal arrives as HTTP 200 with `stop_reason: "refusal"`, so the
  stop reason has to be checked before touching `content`.
"""

from __future__ import annotations

import os

from . import ProviderError, parse_json_text, post_json

URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/") + "/v1/messages"
DEFAULT_MODEL = "claude-opus-5"
API_VERSION = "2023-06-01"
MAX_TOKENS = 16000

#: `output_config.effort` is a Claude 5 feature. Older models reject it outright —
#:
#:   HTTP 400 invalid_request_error: This model does not support the effort parameter.
#:
#: so it is opt-in by model family, not opt-out. An unknown model silently loses the
#: knob, which beats a request that cannot succeed. `output_config.format` is fine
#: everywhere tested, including claude-haiku-4-5, so only effort is gated.
EFFORT_FAMILIES = ("opus-5", "sonnet-5", "fable-5")


def supports_effort(model: str) -> bool:
    return any(family in model for family in EFFORT_FAMILIES)


class AnthropicProvider:
    def __init__(self, model: str = "", effort: str = "medium") -> None:
        self.name = "anthropic"
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self._headers = _auth_headers()

    def describe(self) -> str:
        effort = f", effort={self.effort}" if supports_effort(self.model) else ""
        return f"anthropic:{self.model}{effort}"

    def complete_json(self, *, system: str, prompt: str, schema: dict) -> dict:
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if supports_effort(self.model):
            output_config["effort"] = self.effort

        payload = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        response = post_json(URL, self._headers, payload)

        stop = response.get("stop_reason")
        if stop == "refusal":
            details = response.get("stop_details") or {}
            raise ProviderError(
                f"{self.model} declined the request "
                f"(category={details.get('category')!r}): {details.get('explanation') or 'no detail'}"
            )
        if stop == "max_tokens":
            raise ProviderError(
                f"{self.model} hit max_tokens ({MAX_TOKENS}) before finishing — the answer is "
                "truncated. Split the spec into fewer intents, or lower `effort`."
            )

        for block in response.get("content", []):
            if block.get("type") == "text":
                return parse_json_text(block["text"], who=self.model)
        raise ProviderError(f"{self.model} returned no text block (stop_reason={stop!r})")


def _auth_headers() -> dict[str, str]:
    """API key, or an OAuth bearer token from `ant auth login`.

    An unset ANTHROPIC_API_KEY does not mean there are no credentials: an OAuth
    profile is equally valid, but over raw HTTP it goes on `Authorization` and
    needs its own beta header rather than `x-api-key`.
    """
    base = {"anthropic-version": API_VERSION}

    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return {**base, "x-api-key": key}

    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if token:
        return {
            **base,
            "authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        }

    raise ProviderError(
        "no Anthropic credentials.\n"
        "  export ANTHROPIC_API_KEY=...\n"
        "  or, with an `ant auth login` profile:\n"
        "    export ANTHROPIC_AUTH_TOKEN=$(ant auth print-credentials --access-token)\n"
        "  or use a different backend: AIOS_PROVIDER=ollama|openai|echo"
    )
