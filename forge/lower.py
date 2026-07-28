"""The lowering pass: intent -> portage decisions.

The one nondeterministic step in the pipeline, and the only one that talks to a
model. Its output is validated, then frozen into the lockfile; nothing
downstream ever calls a provider.

The schema stays inside the intersection of what every backend's structured-output
mode accepts: closed objects, every property required, no recursion, no numeric
or string constraints. Optional fields are expressed as empty arrays rather than
absent keys.
"""

from __future__ import annotations

import re

from . import lock as lock_mod
from . import portage as portage_mod
from . import spec as spec_mod
from .provider import Provider

ATOM_RE = re.compile(r"^[a-z0-9][a-z0-9+._-]*/[a-zA-Z0-9][a-zA-Z0-9+._-]*$")
FLAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_@-]*$")


class LoweringError(Exception):
    """The model's answer was structurally valid JSON but semantically unusable."""


_USE_FLAG = {
    "type": "object",
    "additionalProperties": False,
    "required": ["flag", "enabled", "why"],
    "properties": {
        "flag": {"type": "string", "description": "portage USE flag name, without +/- prefix"},
        "enabled": {"type": "boolean"},
        "why": {
            "type": "string",
            "description": (
                "Justification naming the intent it serves, e.g. "
                "'intent[0]: syntax highlighting'. For a disabled flag, say which "
                "capability no intent asked for."
            ),
        },
    },
}

_PACKAGE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["atom", "why", "use", "accept_keywords", "probes"],
    "properties": {
        "atom": {"type": "string", "description": "category/name, unversioned"},
        "why": {"type": "string", "description": "which intent requires this package"},
        "use": {"type": "array", "items": _USE_FLAG},
        "accept_keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": "keywords to accept for this atom; empty for stable",
        },
        "probes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "probe names from the spec that exercise this package",
        },
    },
}

_MAKE_CONF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "value", "why"],
    "properties": {
        "key": {"type": "string"},
        "value": {"type": "string"},
        "why": {"type": "string"},
    },
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["packages", "make_conf", "notes"],
    "properties": {
        "packages": {"type": "array", "items": _PACKAGE},
        "make_conf": {"type": "array", "items": _MAKE_CONF},
        "notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "assumptions, ambiguities, or intents you could not satisfy",
        },
    },
}

SYSTEM_PROMPT = """\
You are the lowering pass of AIos: you compile a user's stated intent into Gentoo
portage build configuration. You do not build anything and you do not run
commands — you emit decisions that a deterministic renderer turns into
/etc/portage state.

Rules, in order of importance:

1. Every decision carries a `why` that names the intent it serves, as
   `intent[N]: <the part of the intent it satisfies>`. A decision you cannot
   justify from an intent does not belong in the output.
2. Minimal by default. Enable a USE flag only when an intent needs the capability
   it provides. Explicitly disable the flags a reader would expect to be on —
   that record is what makes the result auditable and minimizable later.
3. List only packages an intent requires. Do not include @system packages, a
   toolchain, or a base profile's contents; those come from the profile. Do not
   invent a package to satisfy an intent that is already covered.
4. Use real, current Gentoo atoms in `category/name` form, unversioned. If you
   are unsure whether an atom exists, say so in `notes` rather than guessing a
   plausible-looking name.
5. Respect the target: the given arch, libc and init system. Never assume
   systemd, X11, or a graphical session. On musl, note in `notes` any package
   you know to be glibc-dependent instead of silently adding it.
6. `make.conf` entries are global build policy (a base USE set, PKGDIR, ...).
   CHOST, CFLAGS, CXXFLAGS, MAKEOPTS, ACCEPT_KEYWORDS and FEATURES are owned by
   the spec — do not emit them; they will be discarded. PORTAGE_BINHOST and
   DISTCC_HOSTS are refused outright: naming another machine describes the
   network at this moment rather than the machine being built, and both are set
   per build from the environment. An intent about sharing or distributing
   compilation is served by the packages it needs, never by a host list.
7. Bind each package to the probe names the spec offers that would exercise it.
   Probes are the only guardrail for later feature minimization, so a package
   with no probe can never be minimized. If an intent has no probe that covers
   it, note that too.

Terse output. No prose outside the schema.
"""


def build_prompt(spec: spec_mod.Spec) -> str:
    system = spec.system
    lines = [
        "# Target",
        f"arch:    {system.arch}",
        f"libc:    {system.libc}",
        f"init:    {system.init}",
        f"profile: {system.profile}",
        f"CHOST:   {system.chost}",
        "",
        "# Intent",
    ]
    for index, intent in enumerate(spec.intents):
        probes = ", ".join(intent.probes) if intent.probes else "(none — not minimizable)"
        lines.append(f"intent[{index}]: {intent.text}")
        lines.append(f"           probes: {probes}")
    lines += [
        "",
        "# Probe names available for binding",
        ", ".join(spec.all_probes()) or "(none)",
        "",
        "Lower this into packages, USE flag decisions, and make.conf policy.",
    ]
    return "\n".join(lines)


def validate(payload: dict, spec: spec_mod.Spec) -> dict:
    """Structural and semantic checks a schema cannot express."""
    if not isinstance(payload, dict):
        raise LoweringError(f"expected a JSON object, got {type(payload).__name__}")

    packages = payload.get("packages")
    if not isinstance(packages, list):
        raise LoweringError("`packages` must be an array")
    if not packages:
        raise LoweringError(
            "the lowering produced no packages — either the spec has no actionable "
            "intent, or the model refused the task"
        )

    for index, pkg in enumerate(packages):
        where = f"packages[{index}]"
        if not isinstance(pkg, dict):
            raise LoweringError(f"{where} must be an object")
        atom = pkg.get("atom", "")
        if not isinstance(atom, str) or not ATOM_RE.match(atom):
            raise LoweringError(f"{where}.atom = {atom!r} is not a category/name atom")
        if not str(pkg.get("why", "")).strip():
            raise LoweringError(f"{where} ({atom}) has no `why` — every decision needs provenance")
        use = pkg.get("use", [])
        if not isinstance(use, list):
            raise LoweringError(f"{where}.use must be an array")
        for flag_index, flag in enumerate(use):
            flag_where = f"{where}.use[{flag_index}]"
            if not isinstance(flag, dict):
                raise LoweringError(f"{flag_where} must be an object")
            name = flag.get("flag", "")
            if not isinstance(name, str) or not FLAG_RE.match(name):
                raise LoweringError(f"{flag_where}.flag = {name!r} is not a USE flag name")
            if not isinstance(flag.get("enabled"), bool):
                raise LoweringError(f"{flag_where}.enabled must be a boolean")
            if not str(flag.get("why", "")).strip():
                raise LoweringError(f"{flag_where} ({atom}:{name}) has no `why`")

    make_conf = payload.get("make_conf", [])
    if not isinstance(make_conf, list):
        raise LoweringError("`make_conf` must be an array")
    for index, entry in enumerate(make_conf):
        if not isinstance(entry, dict):
            raise LoweringError(f"make_conf[{index}] must be an object")
        for key in ("key", "value", "why"):
            if not isinstance(entry.get(key), str):
                raise LoweringError(f"make_conf[{index}].{key} must be a string")
        if not entry["key"].strip():
            raise LoweringError(f"make_conf[{index}].key is empty")
        # The renderer's rule, enforced where the untrusted text arrives. `lock.build`
        # refuses PORTAGE_BINHOST and DISTCC_HOSTS by *key*, so without this a value
        # of `foo"\nDISTCC_HOSTS="peer/8` under an allowed key renders both variables
        # into make.conf, the digest stamps over it, and two nodes with the same spec
        # get different portage trees — the one thing the lockfile exists to prevent.
        reason = portage_mod.make_conf_reason(
            entry["key"].strip(), entry["value"], entry["why"]
        )
        if reason:
            raise LoweringError(f"make_conf[{index}]: {reason}")
        # Stricter here than in the renderer, and only here: `CXXFLAGS="${CFLAGS}"`
        # is a line forge itself writes, but a model proposing an expansion is
        # proposing something whose meaning depends on portage's environment at
        # build time rather than on the lockfile.
        if "$" in entry["value"]:
            raise LoweringError(
                f"make_conf[{index}].value contains '$': a lowering pass does not get "
                "to write a shell expansion into build policy"
            )

    notes = payload.get("notes", [])
    if not isinstance(notes, list) or any(not isinstance(n, str) for n in notes):
        raise LoweringError("`notes` must be an array of strings")

    return payload


def lower(spec: spec_mod.Spec, provider: Provider) -> dict:
    """Run the lowering pass and return a stamped lockfile."""
    payload = provider.complete_json(
        system=SYSTEM_PROMPT, prompt=build_prompt(spec), schema=SCHEMA
    )
    validate(payload, spec)
    return lock_mod.build(
        spec, payload, {"provider": provider.name, "model": provider.model}
    )
