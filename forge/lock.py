"""The lockfile: the only thing downstream stages are allowed to read.

Canonically serialized and content-digested, so the same spec plus the same
digest always renders byte-identical portage state. The lowering pass is the
only nondeterministic step in the pipeline, and it ends here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import spec as spec_mod

SCHEMA = 1
DEFAULT_PATH = "aios.lock.json"

#: Keys the spec owns outright. A lowering pass that proposes one of these is
#: overruled and the attempt is recorded in `notes` — the agent decides packages
#: and features, not the machine's identity.
SPEC_OWNED = ("CHOST", "CFLAGS", "CXXFLAGS", "MAKEOPTS", "ACCEPT_KEYWORDS", "FEATURES")

#: Keys the lockfile refuses to carry at all, because they describe the NETWORK
#: rather than the machine. A live lowering put
#: `PORTAGE_BINHOST='http://peer.aios.local/binpkgs/'` here — an invented hostname
#: that resolves nowhere, so every emerge would have stalled on it. Two problems, and
#: the second is the architectural one: which peers exist is a fact about this
#: moment, so baking one into the lock makes two nodes with identical specs render
#: different portage trees, which is precisely the guarantee the lockfile exists to
#: make. Topology belongs in the environment — `forge build --peer` and
#: `portage.binhost_env` — where it can be wrong without being permanent.
FORBIDDEN = {
    "PORTAGE_BINHOST": (
        "network topology is not part of the machine's description. Use "
        "`forge build --peer <url>` or AIOS_BINHOST, which set it per build."
    ),
    # The same rule one level down: a binhost names who has already compiled, a
    # distcc host list names who could compile right now. Both go stale the moment a
    # laptop closes, and both would make two nodes with identical specs render
    # different portage trees. FEATURES="distcc" is the half that IS spec-owned.
    "DISTCC_HOSTS": (
        "which machines can compile for this one is network topology, not the "
        "machine's description. Use `forge build --distcc` or "
        '`eval "$(python3 -m aios.mesh distcc)"`, which set it per build.'
    ),
}


class LockError(Exception):
    """The lockfile is malformed, stale, or its digest does not match."""


def canonical(obj) -> str:
    """Deterministic JSON. Sorted keys, fixed indent, trailing newline."""
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def digest(obj) -> str:
    """Content digest of everything except the digest field itself."""
    body = {k: v for k, v in obj.items() if k != "digest"}
    return "sha256:" + hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def stamp(lock: dict) -> dict:
    lock["digest"] = digest(lock)
    return lock


def verify(lock: dict) -> None:
    recorded = lock.get("digest")
    actual = digest(lock)
    if recorded != actual:
        raise LockError(
            "lockfile digest mismatch — it was edited by hand.\n"
            f"  recorded: {recorded}\n"
            f"  actual:   {actual}\n"
            "Re-run `forge lower`, or `forge reseal` if the edit was deliberate."
        )


def spec_digest(spec: spec_mod.Spec) -> str:
    return "sha256:" + hashlib.sha256(canonical(spec.as_dict()).encode("utf-8")).hexdigest()


def _sorted_use(flags: list[dict]) -> list[dict]:
    return sorted(
        (
            {"flag": f["flag"], "enabled": bool(f["enabled"]), "why": f["why"]}
            for f in flags
        ),
        key=lambda f: f["flag"],
    )


def build(spec: spec_mod.Spec, lowered: dict, provider: dict) -> dict:
    """Assemble a lockfile from a spec and a validated lowering result."""
    system = spec.system
    notes = list(lowered.get("notes", []))

    make_conf: dict[str, dict] = {
        "CHOST": {"value": system.chost, "why": "spec: system.arch + system.libc"},
        "CFLAGS": {"value": system.cflags, "why": "spec: system.cflags"},
        "CXXFLAGS": {"value": "${CFLAGS}", "why": "spec: system.cflags"},
        "MAKEOPTS": {"value": system.makeopts, "why": "spec: system.makeopts"},
        "ACCEPT_KEYWORDS": {"value": system.keyword, "why": "spec: system.arch"},
        "FEATURES": {"value": system.features, "why": "spec: system.features"},
    }
    for entry in lowered.get("make_conf", []):
        key = str(entry["key"]).strip()
        if key in FORBIDDEN:
            notes.append(
                f"refused agent-proposed {key}={entry['value']!r}: {FORBIDDEN[key]}"
            )
            continue
        if key in SPEC_OWNED:
            notes.append(
                f"discarded agent-proposed {key}={entry['value']!r}: spec-owned "
                f"(kept {make_conf[key]['value']!r})"
            )
            continue
        make_conf[key] = {"value": str(entry["value"]), "why": str(entry["why"])}

    packages: dict[str, dict] = {}
    for pkg in lowered.get("packages", []):
        atom = str(pkg["atom"]).strip()
        if atom in packages:
            notes.append(f"duplicate atom {atom} in lowering result: merged USE flags")
            merged = {f["flag"]: f for f in packages[atom]["use"]}
            for flag in pkg.get("use", []):
                merged[flag["flag"]] = flag
            packages[atom]["use"] = _sorted_use(list(merged.values()))
            continue
        packages[atom] = {
            "atom": atom,
            "why": str(pkg["why"]),
            "use": _sorted_use(pkg.get("use", [])),
            "accept_keywords": sorted({str(k) for k in pkg.get("accept_keywords", [])}),
            "probes": sorted({str(p) for p in pkg.get("probes", [])}),
        }

    known_probes = set(spec.all_probes())
    for atom, pkg in packages.items():
        unknown = [p for p in pkg["probes"] if p not in known_probes]
        if unknown:
            notes.append(
                f"{atom}: dropped probe reference(s) {', '.join(unknown)} — not named by any intent"
            )
            pkg["probes"] = [p for p in pkg["probes"] if p in known_probes]

    lock = {
        "schema": SCHEMA,
        "spec_digest": spec_digest(spec),
        "generated_by": {
            "provider": provider.get("provider", "unknown"),
            "model": provider.get("model", ""),
        },
        "system": system.as_dict(),
        "intents": [i.as_dict() for i in spec.intents],
        "make_conf": [
            {"key": key, "value": entry["value"], "why": entry["why"]}
            for key, entry in sorted(make_conf.items())
        ],
        "packages": [packages[atom] for atom in sorted(packages)],
        "minimized": {},
        "notes": notes,
    }
    return stamp(lock)


def load(path: str | Path = DEFAULT_PATH, *, check: bool = True) -> dict:
    path = Path(path)
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LockError(f"no lockfile at {path} — run `forge lower` first") from None
    except json.JSONDecodeError as exc:
        raise LockError(f"{path}: {exc}") from None
    if lock.get("schema") != SCHEMA:
        raise LockError(f"{path}: schema {lock.get('schema')!r}, expected {SCHEMA}")
    if check:
        verify(lock)
    return lock


def save(lock: dict, path: str | Path = DEFAULT_PATH) -> Path:
    path = Path(path)
    path.write_text(canonical(stamp(lock)), encoding="utf-8")
    return path


def package(lock: dict, atom: str) -> dict:
    for pkg in lock["packages"]:
        if pkg["atom"] == atom:
            return pkg
    raise LockError(f"{atom} is not in the lockfile")


def enabled_use(lock: dict, atom: str) -> list[str]:
    return [f["flag"] for f in package(lock, atom)["use"] if f["enabled"]]


def diff(old: dict | None, new: dict) -> list[str]:
    """Human-readable change list. Empty means the lock is unchanged."""
    if old is None:
        return [
            f"+ new lockfile: {len(new['packages'])} packages, "
            f"{sum(len(p['use']) for p in new['packages'])} USE decisions"
        ]

    lines: list[str] = []
    old_pkgs = {p["atom"]: p for p in old["packages"]}
    new_pkgs = {p["atom"]: p for p in new["packages"]}

    for atom in sorted(set(new_pkgs) - set(old_pkgs)):
        pkg = new_pkgs[atom]
        lines.append(f"+ {atom}  ({pkg['why']})")
    for atom in sorted(set(old_pkgs) - set(new_pkgs)):
        lines.append(f"- {atom}")

    for atom in sorted(set(old_pkgs) & set(new_pkgs)):
        was = {f["flag"]: f for f in old_pkgs[atom]["use"]}
        now = {f["flag"]: f for f in new_pkgs[atom]["use"]}
        for flag in sorted(set(now) | set(was)):
            a, b = was.get(flag), now.get(flag)
            if a is None:
                lines.append(f"~ {atom}  +{_sign(b)}  ({b['why']})")
            elif b is None:
                lines.append(f"~ {atom}  -{_sign(a)}")
            elif a["enabled"] != b["enabled"]:
                lines.append(f"~ {atom}  {_sign(a)} -> {_sign(b)}  ({b['why']})")

    old_mc = {e["key"]: e for e in old["make_conf"]}
    new_mc = {e["key"]: e for e in new["make_conf"]}
    for key in sorted(set(old_mc) | set(new_mc)):
        a, b = old_mc.get(key), new_mc.get(key)
        if a is None:
            lines.append(f"+ make.conf {key}={b['value']!r}  ({b['why']})")
        elif b is None:
            lines.append(f"- make.conf {key}")
        elif a["value"] != b["value"]:
            lines.append(f"~ make.conf {key}: {a['value']!r} -> {b['value']!r}")

    if old.get("spec_digest") != new.get("spec_digest"):
        lines.append("~ spec changed since the previous lock")
    return lines


def _sign(flag: dict) -> str:
    return f"{'+' if flag['enabled'] else '-'}{flag['flag']}"
