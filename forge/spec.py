"""The spec: a human-authored description of a machine.

Parsed from aios.toml. This is the only file a user is expected to edit by hand.
Everything else in the pipeline is generated from it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ARCHES = ("aarch64", "x86_64")
LIBCS = ("musl", "glibc")
INITS = ("dinit", "s6", "runit", "openrc")
#: `fallback` is a chain rather than a backend — it asks the local model first and the
#: hosted one when that fails. It belongs here because the spec is where a machine
#: states its model policy, and "prefer local, keep working without it" is a policy.
PROVIDERS = ("fallback", "anthropic", "openai", "ollama", "echo")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class SpecError(Exception):
    """The spec is malformed. Message is user-facing."""


@dataclass(frozen=True)
class System:
    """Machine identity. Spec-owned — the lowering pass cannot override these."""

    name: str = "aios"
    arch: str = "aarch64"
    libc: str = "musl"
    init: str = "dinit"
    profile: str = "default"
    cflags: str = "-O2 -pipe"
    makeopts: str = "-j4"
    #: Portage FEATURES. Spec-owned rather than agent-chosen, for two reasons that
    #: both bite hard: `buildpkg` is what makes `forge minimize` affordable (one
    #: rebuild per lever is unpayable without a binary package cache, and it is what
    #: a node serves to its peers), and the sandbox set has to match the machine's
    #: actual capabilities. A model must not be able to turn either into a guess.
    features: str = "buildpkg parallel-fetch"

    @property
    def chost(self) -> str:
        abi = "musl" if self.libc == "musl" else "gnu"
        return f"{self.arch}-unknown-linux-{abi}"

    @property
    def keyword(self) -> str:
        """Portage's arch keyword, which is not the uname string.

        `aarch64` is `arm64` and `x86_64` is `amd64` in ACCEPT_KEYWORDS; getting
        this wrong masks every package on the target.
        """
        return {"aarch64": "arm64", "x86_64": "amd64"}[self.arch]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "arch": self.arch,
            "libc": self.libc,
            "init": self.init,
            "profile": self.profile,
            "cflags": self.cflags,
            "makeopts": self.makeopts,
            "features": self.features,
            "chost": self.chost,
            "keyword": self.keyword,
        }


@dataclass(frozen=True)
class Intent:
    """One thing the user does with the machine, in their own words.

    `probes` names the capability checks that make this intent minimizable. An
    intent with no probes can be lowered but never minimized against.
    """

    text: str
    probes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"text": self.text, "probes": list(self.probes)}


@dataclass(frozen=True)
class Agent:
    """Which model performs the lowering pass. Env vars override these."""

    provider: str = "anthropic"
    model: str = ""
    effort: str = "medium"

    def as_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "effort": self.effort}


@dataclass(frozen=True)
class Spec:
    system: System
    agent: Agent
    intents: tuple[Intent, ...]
    path: Path | None = field(default=None, compare=False)

    def as_dict(self) -> dict:
        """Digestible form. Deliberately excludes `path`."""
        return {
            "system": self.system.as_dict(),
            "agent": self.agent.as_dict(),
            "intents": [i.as_dict() for i in self.intents],
        }

    def all_probes(self) -> tuple[str, ...]:
        seen: list[str] = []
        for intent in self.intents:
            for probe in intent.probes:
                if probe not in seen:
                    seen.append(probe)
        return tuple(seen)


def _one_of(value: str, allowed: tuple[str, ...], where: str) -> str:
    if value not in allowed:
        raise SpecError(f"{where}: {value!r} is not one of {', '.join(allowed)}")
    return value


def _table(raw: dict, key: str) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise SpecError(f"[{key}] must be a table")
    return value


def _str(raw: dict, key: str, default: str, where: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise SpecError(f"{where}.{key} must be a string")
    return value


def parse(raw: dict, path: Path | None = None) -> Spec:
    sys_raw = _table(raw, "system")
    system = System(
        name=_str(sys_raw, "name", "aios", "system"),
        arch=_one_of(_str(sys_raw, "arch", "aarch64", "system"), ARCHES, "system.arch"),
        libc=_one_of(_str(sys_raw, "libc", "musl", "system"), LIBCS, "system.libc"),
        init=_one_of(_str(sys_raw, "init", "dinit", "system"), INITS, "system.init"),
        profile=_str(sys_raw, "profile", "default", "system"),
        cflags=_str(sys_raw, "cflags", "-O2 -pipe", "system"),
        makeopts=_str(sys_raw, "makeopts", "-j4", "system"),
        features=_str(sys_raw, "features", System.features, "system"),
    )

    agent_raw = _table(raw, "agent")
    agent = Agent(
        provider=_one_of(
            _str(agent_raw, "provider", "anthropic", "agent"), PROVIDERS, "agent.provider"
        ),
        model=_str(agent_raw, "model", "", "agent"),
        effort=_one_of(_str(agent_raw, "effort", "medium", "agent"), EFFORTS, "agent.effort"),
    )

    intents_raw = raw.get("intent", [])
    if not isinstance(intents_raw, list) or not intents_raw:
        raise SpecError("at least one [[intent]] is required — the spec is intent, not config")

    intents: list[Intent] = []
    for index, item in enumerate(intents_raw):
        where = f"intent[{index}]"
        if not isinstance(item, dict):
            raise SpecError(f"{where} must be a table")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SpecError(f"{where}.text is required and must be a non-empty string")
        probes = item.get("probes", [])
        if not isinstance(probes, list) or any(not isinstance(p, str) for p in probes):
            raise SpecError(f"{where}.probes must be a list of strings")
        intents.append(Intent(text=text.strip(), probes=tuple(probes)))

    return Spec(system=system, agent=agent, intents=tuple(intents), path=path)


def load(path: str | Path) -> Spec:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SpecError(f"no spec at {path} — run `forge init` to write a starter") from None
    except tomllib.TOMLDecodeError as exc:
        raise SpecError(f"{path}: {exc}") from None
    return parse(raw, path=path)


STARTER = '''\
# AIos machine spec. Describe what you do; forge decides what to compile.

[system]
name     = "aios-dev"
arch     = "aarch64"           # aarch64 | x86_64
libc     = "musl"              # musl | glibc
init     = "dinit"             # no systemd
profile  = "default"
cflags   = "-O2 -pipe"
makeopts = "-j8"

[agent]
provider = "anthropic"         # anthropic | openai | ollama | echo
model    = ""                  # empty = provider default
effort   = "medium"            # low | medium | high | xhigh | max

# Each intent is one thing you actually do. Name the probes that prove it still
# works — an intent with no probes can be lowered, but never minimized against.

[[intent]]
text   = "edit code over ssh: syntax highlighting, multiple buffers, no X11 and no clipboard integration"
probes = ["vim"]

[[intent]]
text   = "build small C projects locally with make"
probes = []

[[intent]]
text   = "reach the machine over ssh with key auth only, no password auth, no X forwarding"
probes = []
'''
