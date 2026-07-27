"""A self-contained Anthropic Messages API client with tool-use support.

Why this exists next to `forge/provider/anthropic.py` rather than reusing it: the
lowering pass needs exactly one JSON answer, while the agent needs a
*conversation* — tool_use blocks going out, tool_result blocks coming back,
thinking blocks echoed verbatim, many turns. That is a different shape. And
`forge/` sits on the far side of the AI-out-of-the-build-path rule, so nothing
the agent runs may be reachable from the build. The forty duplicated lines of
auth and stop_reason handling are the price of that separation, and they are
deliberately consistent with the provider's version.

Stdlib `urllib` only: this has to run on a target whose only interpreter is the
python3 portage already requires, with no pip and no vendored dependency tree.

Four things about this model family are load-bearing here:

- Thinking is on by default, and `max_tokens` caps thinking *plus* visible
  output. 8192 is a floor with headroom, not a budget to tune downward.
- `temperature` / `top_p` / `top_k` are rejected outright by claude-opus-5 and
  claude-sonnet-5. This client never sends them; behaviour is steered by prompt.
- Thinking blocks must go back into the conversation byte-identical when an
  assistant turn is echoed. Rewriting or dropping one 400s the *next* call, which
  is why `Reply.assistant_turn()` hands back the raw content list untouched.
- A safety refusal arrives as HTTP 200 with `stop_reason: "refusal"`, so the stop
  reason has to be checked before `content` is read at all.

The transport is injectable (`Client(transport=...)`) so the agent loop can be
tested end-to-end with no network and no credentials.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

API_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MAX_TOKENS = 8192
TIMEOUT = float(os.environ.get("AIOS_HTTP_TIMEOUT", "600"))

# The escalation ladder. The orchestrator runs on the cheapest one and buys more
# capability per task, rather than paying for the ceiling on every turn.
ORCHESTRATOR = "claude-haiku-4-5-20251001"
REASONER = "claude-sonnet-5"
ARBITER = "claude-opus-5"
MODELS = (ORCHESTRATOR, REASONER, ARBITER)

RETRY_STATUS = frozenset({429, 529})
MAX_RETRIES = 4
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0

API_KEY_ENV = "ANTHROPIC_API_KEY"
TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"

MISSING_CREDENTIALS = (
    "no Anthropic credentials.\n"
    f"  export {API_KEY_ENV}=...\n"
    "  or, with an `ant auth login` profile:\n"
    f"    export {TOKEN_ENV}=$(ant auth print-credentials --access-token)"
)


class LLMError(Exception):
    """The model backend failed, refused, or returned something unusable."""


class CredentialsError(LLMError):
    """No API key and no OAuth token. Recoverable by the user, not by us."""


class RefusalError(LLMError):
    """stop_reason == "refusal". The content is not an answer; do not read it."""


class TruncatedError(LLMError):
    """stop_reason == "max_tokens". The answer is cut off, so it is not an answer."""


class TransportError(LLMError):
    """A non-2xx HTTP response, carrying the body so the failure is diagnosable."""

    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"{url} -> HTTP {status}\n{body}")
        self.status = status
        self.body = body


# (url, headers, body) -> raw response bytes; raises TransportError on non-2xx.
Transport = Callable[[str, dict[str, str], bytes], bytes]


def base_url() -> str:
    return os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def have_credentials() -> bool:
    """Whether a request could authenticate at all — checked before we start."""
    return bool(os.environ.get(API_KEY_ENV) or os.environ.get(TOKEN_ENV))


def auth_headers() -> dict[str, str]:
    """API key, or an OAuth bearer token from `ant auth login`.

    An unset ANTHROPIC_API_KEY does not mean there are no credentials: an OAuth
    profile is equally valid, but over raw HTTP it rides on `authorization` and
    needs its own beta header rather than `x-api-key`.
    """
    base = {"anthropic-version": API_VERSION}

    key = os.environ.get(API_KEY_ENV)
    if key:
        return {**base, "x-api-key": key}

    token = os.environ.get(TOKEN_ENV)
    if token:
        return {
            **base,
            "authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        }

    raise CredentialsError(MISSING_CREDENTIALS)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class ToolOutcome:
    call_id: str
    output: str
    is_error: bool = False


@dataclass(frozen=True)
class Reply:
    model: str
    stop_reason: str
    content: list[dict]
    usage: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(
            block["text"] for block in self.content if block.get("type") == "text"
        ).strip()

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [
            ToolCall(id=block["id"], name=block["name"], input=block.get("input") or {})
            for block in self.content
            if block.get("type") == "tool_use"
        ]

    def assistant_turn(self) -> dict:
        """The turn to append to the conversation — content passed through as-is.

        Thinking blocks included. They are opaque and signed; the next request
        400s if they are edited, reordered, or dropped.
        """
        return {"role": "assistant", "content": self.content}


def user_turn(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def tool_results_turn(outcomes: Iterable[ToolOutcome]) -> dict:
    """All results for one assistant turn go back in a single user message.

    Splitting them across messages is accepted but teaches the model to stop
    issuing parallel calls, which costs a round trip on every later turn.
    """
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": outcome.call_id,
                "content": outcome.output,
                "is_error": outcome.is_error,
            }
            for outcome in outcomes
        ],
    }


def _http(url: str, headers: dict[str, str], body: bytes) -> bytes:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise TransportError(
            exc.code, exc.read().decode("utf-8", "replace")[:4000], url
        ) from None
    except urllib.error.URLError as exc:
        raise LLMError(f"{url} unreachable: {exc.reason}") from None
    except TimeoutError:
        raise LLMError(f"{url} timed out after {TIMEOUT:.0f}s") from None


@dataclass
class Client:
    """One model, one endpoint. Stateless — the conversation lives in the caller."""

    model: str = ORCHESTRATOR
    max_tokens: int = DEFAULT_MAX_TOKENS
    transport: Transport = _http
    sleep: Callable[[float], None] = time.sleep
    max_retries: int = MAX_RETRIES
    _auth: dict[str, str] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.transport is not _http and not have_credentials():
            # An injected transport is a test or an offline stand-in; it neither
            # needs nor should demand a key. It still gets the version header, so
            # a transport that does reach a real endpoint sends a valid request.
            self._auth = {"anthropic-version": API_VERSION}

    def describe(self) -> str:
        return f"anthropic:{self.model}"

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[dict],
        tools: Sequence[dict] = (),
    ) -> Reply:
        payload: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": list(messages),
        }
        if tools:
            payload["tools"] = list(tools)

        response = self._post(payload)
        stop = response.get("stop_reason")

        if stop == "refusal":
            details = response.get("stop_details") or {}
            raise RefusalError(
                f"{self.model} declined the request "
                f"(category={details.get('category')!r}): "
                f"{details.get('explanation') or 'no detail given'}"
            )
        if stop == "max_tokens":
            raise TruncatedError(
                f"{self.model} hit max_tokens ({self.max_tokens}) before finishing — the "
                "answer is truncated, so it is not an answer. Raise max_tokens, or split "
                "the task into smaller steps."
            )

        return Reply(
            model=response.get("model", self.model),
            stop_reason=stop or "",
            content=response.get("content") or [],
            usage=response.get("usage") or {},
        )

    def _post(self, payload: dict) -> dict:
        url = base_url() + "/v1/messages"
        headers = {"content-type": "application/json", **self._headers()}
        body = json.dumps(payload).encode("utf-8")

        attempt = 0
        while True:
            try:
                raw = self.transport(url, headers, body)
                break
            except TransportError as exc:
                if exc.status not in RETRY_STATUS or attempt >= self.max_retries:
                    raise
                self.sleep(min(BACKOFF_BASE * 2**attempt, BACKOFF_CAP))
                attempt += 1

        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMError(f"{url} returned non-JSON: {exc}") from None

    def _headers(self) -> dict[str, str]:
        if self._auth is None:
            self._auth = auth_headers()
        return self._auth
