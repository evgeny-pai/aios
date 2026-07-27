"""Ollama backend — a local daemon on the build host or on the target itself.

Ollama's `format` field takes a JSON Schema directly, which is the closest thing
to a constrained-decoding guarantee available locally.
"""

from __future__ import annotations

import os

from . import ProviderError, parse_json_text, post_json

DEFAULT_BASE = "http://127.0.0.1:11434"


class OllamaProvider:
    def __init__(self, model: str = "") -> None:
        self.name = "ollama"
        self.base = (os.environ.get("OLLAMA_HOST") or DEFAULT_BASE).rstrip("/")
        if not self.base.startswith("http"):
            self.base = f"http://{self.base}"
        self.model = model
        if not self.model:
            raise ProviderError(
                "the ollama backend needs an explicit model: "
                "set AIOS_MODEL, or agent.model in aios.toml"
            )

    def describe(self) -> str:
        return f"ollama:{self.model} @ {self.base}"

    def complete_json(self, *, system: str, prompt: str, schema: dict) -> dict:
        payload = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        response = post_json(f"{self.base}/api/chat", {}, payload)
        content = (response.get("message") or {}).get("content")
        if not content:
            raise ProviderError(f"{self.describe()} returned no message content: {response}")
        return parse_json_text(content, who=self.model)
