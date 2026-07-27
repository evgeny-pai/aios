"""The first screen a human sees on a running AIos machine.

An AIos machine is not a distribution somebody else assembled — it is the
compiled form of a few lines of intent. So this screen is a *manifest*: the
machine reporting what it is made of, one fact per line, and then quoting the
intent lines from `aios.lock.json` verbatim. The last thing you read before the
prompt is why this machine exists at all.

Every value is read live — from `os.uname`, from `/proc`, from the lockfile, from
the environment. Nothing here is hardcoded and nothing is decorative: a fact that
cannot be sourced does not appear, because a boot screen that flatters the
machine is worse than no boot screen.

Stdlib only, no subprocess, no network, no import from `forge`. This runs as the
first thing PID 1 does, on a musl target whose only interpreter is the one
portage already requires.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from . import generation

LOCK_NAME = "aios.lock.json"
PROC = Path("/proc")

MEASURE_MAX = 88  # a manifest is read, not scanned — hold the measure
LABEL_W = 10
NUMBER_W = 13  # "1.9 / 8.0 GiB" — the widest number on the panel sets the column
NOTE_MIN_GAP = 4

# One accent, spent three times: the AI in the wordmark, the lock digest, and the
# prompt caret. Everything else is the terminal's own foreground or a grey chosen
# to stay legible on light and dark backgrounds alike.
ACCENT_RGB = (127, 168, 201)
LABEL_RGB = (130, 138, 148)
FAINT_RGB = (94, 102, 112)
ACCENT_256, LABEL_256, FAINT_256 = 110, 245, 240


@dataclass(frozen=True)
class Theme:
    accent: str = ""
    label: str = ""
    faint: str = ""
    reset: str = ""

    @classmethod
    def detect(cls, env: os._Environ | dict) -> Theme:
        if env.get("NO_COLOR") is not None or env.get("TERM") in ("dumb", "unknown"):
            return cls()
        if env.get("COLORTERM", "") in ("truecolor", "24bit"):
            rgb = lambda c: "\x1b[38;2;{};{};{}m".format(*c)  # noqa: E731
            return cls(rgb(ACCENT_RGB), rgb(LABEL_RGB), rgb(FAINT_RGB), "\x1b[0m")
        c256 = lambda n: f"\x1b[38;5;{n}m"  # noqa: E731
        return cls(c256(ACCENT_256), c256(LABEL_256), c256(FAINT_256), "\x1b[0m")

    def paint(self, text: str, colour: str) -> str:
        return f"{colour}{text}{self.reset}" if colour and text else text


@dataclass(frozen=True)
class Glyphs:
    top: str
    stem: str
    tick: str
    foot: str
    rule: str
    dot: str
    caret: str
    cut: str


UNICODE = Glyphs("╷", "│", "├", "╵", "─", "·", "▸", "…")
ASCII = Glyphs(".", "|", "+", "'", "-", "-", ">", "...")


@dataclass(frozen=True)
class Fact:
    label: str
    value: str
    note: str = ""
    number: bool = False  # right-aligned into the number column


@dataclass(frozen=True)
class Intent:
    index: int
    text: str
    probes: tuple[str, ...] = ()


@dataclass
class Manifest:
    machine: str
    digest: str
    generation: int = 0
    facts: list[Fact] = field(default_factory=list)
    intents: list[Intent] = field(default_factory=list)
    absent: str = ""  # why the intent block is empty, when it is


@dataclass(frozen=True)
class Line:
    text: str
    drop: int = 0  # 0 never drops; higher ranks are shed first on a short terminal


# --- facts -------------------------------------------------------------------


def read(g: Glyphs) -> Manifest:
    """Every value on the screen originates here, and nowhere else."""
    lock = _load_lock()
    system = lock.get("system", {})
    packages = lock.get("packages", [])
    uname = os.uname()

    arch = uname.machine
    target = system.get("arch", arch)
    libc = system.get("libc", "")

    facts = [
        # On the target these agree; on a build host they do not, and saying so
        # is the difference between a manifest and a poster.
        Fact("arch", f"{arch} {g.dot} {libc}" if libc else arch,
             note=f"target {target}" if target != arch else ""),
        Fact("kernel", f"{uname.sysname} {uname.release}"),
        Fact("init", system.get("init") or "unknown", note=_pid1()),
        Fact("agent", *_provider(g)),
        # Who the machine thinks it is talking to. Read, never minted, here: a boot
        # screen that assigns you a name as a side effect of being displayed would
        # hand out a new one every time something rendered it.
        Fact("operator", _operator()),
    ]
    if lock:
        use_count = sum(len(p.get("use", [])) for p in packages)
        facts.append(Fact("packages", str(len(packages)), note=f"{use_count} use flags",
                          number=True))
    for label, value in (("uptime", _uptime()), ("memory", _memory())):
        if value:
            facts.append(Fact(label, value, number=True))

    intents = [
        Intent(i, str(item.get("text", "")), tuple(item.get("probes", ())))
        for i, item in enumerate(lock.get("intents", ()))
    ]
    absent = "" if intents else (
        f"no lockfile here {g.dot} write an intent in aios.toml, then  forge lower"
        if not lock else f"the lockfile carries no intent {g.dot} run  forge lower"
    )

    return Manifest(
        machine=system.get("name") or uname.nodename,
        digest=lock.get("digest", "")[7:19],  # drop "sha256:", keep 12, as forge prints it
        generation=generation.current(),
        facts=facts,
        intents=intents,
        absent=absent,
    )


def _load_lock() -> dict:
    root = os.environ.get("AIOS_ROOT")
    bases = ([Path(root)] if root else []) + [Path.cwd(), *Path(__file__).resolve().parents]
    for base in bases:
        path = base / LOCK_NAME
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
    return {}


def _provider(g: Glyphs) -> tuple[str, str]:
    """Provider and credential state from the environment only — never a call."""
    name = os.environ.get("AIOS_PROVIDER") or "anthropic"
    keyed = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    state = {
        "echo": "offline stub",
        "ollama": os.environ.get("AIOS_MODEL") or "local weights",
        "openai": _host(os.environ.get("AIOS_OPENAI_BASE_URL", "")) or "no base url",
    }.get(name, "reachable" if keyed else "no credentials")
    return f"{name} {g.dot} {state}", "" if keyed or name != "anthropic" else "degraded"


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def _operator() -> str:
    """The handle from the state volume, or a placeholder until login mints one."""
    root = Path(os.environ.get("AIOS_ROOT", "/aios"))
    try:
        return (root / ".aios" / "operator").read_text(encoding="utf-8").strip() or "unnamed"
    except OSError:
        return "unnamed"


def _pid1() -> str:
    try:
        return "pid1 " + (PROC / "1" / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _uptime() -> str:
    try:
        seconds = int(float((PROC / "uptime").read_text(encoding="utf-8").split()[0]))
    except (OSError, ValueError, IndexError):
        return ""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}"
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}m {seconds:02d}s"


def _memory() -> str:
    try:
        text = (PROC / "meminfo").read_text(encoding="utf-8")
    except OSError:
        return ""
    fields = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable") and rest.split():
            fields[key] = int(rest.split()[0]) * 1024
    total = fields.get("MemTotal")
    if not total:
        return ""
    used = total - fields.get("MemAvailable", total)
    if total < 1 << 30:
        return f"{used >> 20} / {total >> 20} MiB"
    return f"{used / (1 << 30):.1f} / {total / (1 << 30):.1f} GiB"


# --- rendering ---------------------------------------------------------------


def render(m: Manifest, width: int, height: int, t: Theme, g: Glyphs) -> list[str]:
    margin = 2 if width >= 46 else 1
    measure = max(22, min(width - margin * 2, MEASURE_MAX))
    pad = " " * margin
    inner = measure - 3  # every spine line spends three columns on "├─ "

    def spine(glyph: str, body: str = "", drop: int = 0) -> Line:
        lead = glyph + (g.rule if glyph == g.tick else " ") + " "
        return Line((pad + t.paint(lead, t.faint) + body).rstrip(), drop)

    def heading(word: str, note: str = "") -> Line:
        spaced = " ".join(word)  # letterspaced lowercase carries the small-caps register
        if note and len(spaced) + NOTE_MIN_GAP + len(note) > inner:
            note = ""
        body = t.paint(spaced, t.label)
        if note:
            body += " " * (inner - len(spaced) - len(note)) + t.paint(note, t.faint)
        return spine(g.stem, body)

    lines = [Line("", 4)]
    lines += _header(m, pad, measure, t)
    lines += [Line("", 3), spine(g.top)]
    lines.append(heading("system"))
    lines += [spine(g.tick, _fact(f, inner, t)) for f in m.facts]
    lines.append(spine(g.stem))
    lines.append(heading("intent", "verbatim from aios.lock.json" if inner >= 58 else ""))

    fixed = len(lines) + 5  # foot, blank, prompt, blank, and one line of slack
    lines += _intent_lines(m, spine, inner, t, g, budget=max(1, height - fixed))
    lines.append(spine(g.foot))
    lines.append(Line("", 3))
    lines.append(Line(pad + t.paint(g.caret, t.accent) + "  " + _clip(
        "describe what you need — the machine decides what to compile.", measure - 3, g)))
    lines.append(Line("", 5))
    return _fit(lines, height)


def _header(m: Manifest, pad: str, measure: int, t: Theme) -> list[Line]:
    """Wordmark and machine name, then the tagline and the lock's short digest."""
    mark, tag = "A I o s", "a machine compiled from intent"
    # The generation rides with the machine name: which incarnation you are talking
    # to is part of its identity, not a statistic.
    name = f"{m.machine} #{m.generation}" if m.generation else m.machine
    name_p = (
        m.machine + t.paint(f" #{m.generation}", t.accent) if m.generation else m.machine
    )
    rows = [
        (mark, t.paint("A I", t.accent) + " o s", name, name_p),
        (tag, t.paint(tag, t.label), f"lock {m.digest}" if m.digest else "",
         t.paint("lock ", t.label) + t.paint(m.digest, t.accent)),
    ]
    out = []
    for left, left_p, right, right_p in rows:
        if not right:
            out.append(Line(pad + left_p))
        elif len(left) + NOTE_MIN_GAP + len(right) <= measure:
            out.append(Line(pad + left_p + " " * (measure - len(left) - len(right)) + right_p))
        else:  # too narrow to set them on one line — stack, still flush right
            out += [Line(pad + left_p), Line(pad + " " * max(0, measure - len(right)) + right_p)]
    return out


def _fact(f: Fact, inner: int, t: Theme) -> str:
    """label · value · optional right-aligned note. Numbers share a right edge."""
    value = f.value.rjust(NUMBER_W) if f.number else f.value
    body = t.paint(f.label.ljust(LABEL_W), t.label) + value
    used = LABEL_W + len(value)
    if f.note and used + NOTE_MIN_GAP + len(f.note) <= inner:
        body += " " * (inner - used - len(f.note)) + t.paint(f.note, t.faint)
    return body


def _intent_lines(m: Manifest, spine, inner: int, t: Theme, g: Glyphs, budget: int) -> list[Line]:
    if m.absent:
        return [spine(g.tick, t.paint(_clip(m.absent, inner, g), t.label))]

    gutter = len(str(m.intents[-1].index)) + 3
    text_w = inner - gutter
    # Wrapped is the intended setting; if the terminal cannot hold every intent at
    # full length, clip instead of hiding one. A visibly clipped intent is honest;
    # a missing intent is a lie about what the machine is for.
    wrapped = [textwrap.wrap(i.text, text_w) or [""] for i in m.intents]
    if sum(len(w) for w in wrapped) > budget:
        wrapped = [[_clip(i.text, text_w, g)] for i in m.intents]

    rows: list[Line] = []
    for intent, body in zip(m.intents, wrapped):
        tag = ("probes " if len(intent.probes) > 1 else "probe ") + ", ".join(intent.probes)
        if intent.probes and len(body[-1]) + NOTE_MIN_GAP + len(tag) <= text_w:
            body[-1] += " " * (text_w - len(body[-1]) - len(tag)) + t.paint(tag, t.faint)
        index = t.paint(str(intent.index).ljust(gutter - 1), t.label) + " "
        rows.append(spine(g.tick, index + body[0]))
        rows += [spine(g.stem, " " * gutter + line) for line in body[1:]]
    return rows


def _fit(lines: list[Line], height: int) -> list[str]:
    """Shed the softest lines first when the terminal is shorter than the manifest."""
    excess = len(lines) - height
    shed = set(sorted((i for i, line in enumerate(lines) if line.drop),
                      key=lambda i: -lines[i].drop)[:max(0, excess)])
    return [line.text for i, line in enumerate(lines) if i not in shed]


def _clip(text: str, width: int, g: Glyphs) -> str:
    if len(text) <= width:
        return text
    return text[: max(0, width - len(g.cut))].rstrip() + g.cut


def glyphs_for(stream) -> Glyphs:
    try:
        "╷│├╵─·▸…".encode(getattr(stream, "encoding", None) or "ascii")
    except (UnicodeEncodeError, LookupError):
        return ASCII
    return UNICODE


# --- public entry points ------------------------------------------------------


def facts() -> dict:
    """The screen's contents as data, for anything that wants the numbers.

    The agent quotes these in its `/status` output, so the prompt and the boot
    screen can never disagree about what machine this is.
    """
    m = read(glyphs_for(sys.stdout))
    return {
        "machine": m.machine,
        "digest": m.digest,
        "facts": {f.label: f.value for f in m.facts},
        "notes": {f.label: f.note for f in m.facts if f.note},
        "intents": [
            {"index": i.index, "text": i.text, "probes": list(i.probes)} for i in m.intents
        ],
    }


def screen(width: int | None = None, height: int | None = None) -> str:
    """The whole welcome screen as one string, sized for `width` x `height`.

    Defaults to the real terminal, falling back to 80x24 — which is also what
    `kubectl logs` gets, since a pod's log stream has no window size to report.
    """
    detected = shutil.get_terminal_size((80, 24))
    glyphs = glyphs_for(sys.stdout)
    return "\n".join(
        render(
            read(glyphs),
            width if width is not None else detected.columns,
            height if height is not None else detected.lines,
            Theme.detect(os.environ),
            glyphs,
        )
    )


def main() -> int:
    sys.stdout.write(screen() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
