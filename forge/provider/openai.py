"""Any endpoint wearing the OpenAI chat-completions shape.

This is the interesting backend for AIos, not because of the hosted service but
because llama.cpp's server, vLLM, and most local runtimes expose this API. It is
the path to a machine that rebuilds itself with no network at all — same code,
different base URL.

No default model: guessing an identifier for an arbitrary endpoint fails in
confusing ways, so AIOS_MODEL is required.
"""

from __future__ import annotations

import os

from . import ProviderError, parse_json_text, post_json

DEFAULT_BASE = "https://api.openai.com/v1"


class OpenAICompatibleProvider:
    def __init__(self, model: str = "") -> None:
        self.name = "openai"
        base = (
            os.environ.get("AIOS_OPENAI_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or DEFAULT_BASE
        )
        self.base = base.rstrip("/")
        self.url = f"{self.base}/chat/completions"
        self.model = model
        if not self.model:
            raise ProviderError(
                "the openai-compatible backend needs an explicit model: "
                "set AIOS_MODEL, or agent.model in aios.toml"
            )
        self._headers = {}
        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("AIOS_OPENAI_API_KEY")
        if key:
            self._headers["authorization"] = f"Bearer {key}"
        elif self.base == DEFAULT_BASE:
            raise ProviderError("OPENAI_API_KEY is not set (local endpoints usually need no key)")

    def describe(self) -> str:
        return f"openai:{self.model} @ {self.base}"

    def complete_json(self, *, system: str, prompt: str, schema: dict) -> dict:
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "aios_lowering", "strict": True, "schema": schema},
            },
        }
        response = post_json(self.url, self._headers, payload)

        choices = response.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.describe()} returned no choices: {response}")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise ProviderError(f"{self.model} truncated its answer (finish_reason=length)")
        content = (choice.get("message") or {}).get("content")
        if not content:
            raise ProviderError(f"{self.model} returned an empty message")
        return parse_json_text(content, who=self.model)
