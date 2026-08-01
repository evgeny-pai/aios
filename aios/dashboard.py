"""What this machine is doing right now, read from its own audit journal.

Two surfaces, one derivation. `status` prints one short line for the tmux status
bar at the top of the cockpit; `watch` fills the tall narrow pane on the right.
Both answer the same question — is anything happening, and is it stuck — and both
answer it from `.aios/agent.jsonl`, the file `aios.agent` already appends every
request, reply, tool call, spawn, escalation and verdict to. No second mechanism,
because a monitor with its own bookkeeping is a monitor that can disagree with the
machine it is monitoring.

**Health is derived, never asserted.** Every token comes out of the journal —
timestamps and repetition while a run is open (`ok`, `STUCK`, `LOOPING`), and the
run's own terminal record once it has closed (`done`, `RED`, `ASK`) — so the
display cannot flatter the machine. No model has a vote on the machine being *fine*:
the supervisor's judgement, below, can raise an alarm and can never clear one, and
that asymmetry is the whole of what a model is trusted with here. `IDLE` means
nothing is running *and* nothing ended: it is not a fault and
is not coloured like one, because a box with nothing to do is a box working
correctly. A run that gave up is a different fact from a run that finished, and
folding both into `IDLE` made the one thing an operator returning to the machine
needs to know invisible. The heartbeat glyph advances only while an agent is
actually open, so a still spinner means "nothing is running", not "the display
froze". It freezes on purpose next to `STUCK`.

**Liveness is scoped to what the *agent* wrote.** `aios.supervisor` appends its own
advisory lines to this same journal, on purpose: they are audit trail and this pane
displays them. But a watcher whose own writes count as activity certifies itself
alive — the one line a human glances at read `ok` through eleven minutes of a stalled
run *because* the supervisor kept writing about how stalled it was. So every record
names its writer, and only agent-authored ones move the liveness clock, the
heartbeat, the set of open agents and the "has anything happened" fingerprint.

The judgement travels the other way. When the supervisor has diagnosed a stall the
status bar says `STUCK`: a diagnosis that only reaches the advice pane is a diagnosis
nobody sees. It is retired by *evidence* rather than by arrival — `RETIRE_N`
agent-authored records newer than the diagnosis, or one that ends the run — because
the shape of a stall is a loop that keeps logging. Retiring on the next record of any
kind meant one record per iteration cleared the verdict on every pass, while the only
thing that can re-raise it is a model call rate-limited to one a minute: the stall
this half exists to catch was the one stall that could hide from it. The
`fingerprint` still moves on the first record, so the supervisor gets to look again
while its verdict stands, and the two halves still cannot confirm each other —
nothing the watcher writes moves anything measured here.

**Timestamps come out of the journal too.** Every threshold below is a subtraction
from a number a record supplied, so a single record dated ahead of the clock — a
skewed clock, a hand-written line — pinned silence at zero and disabled
`MODEL_SILENCE_S`, `TOOL_SILENCE_S` and `COLD_S` at once, permanently and silently.
Nothing can have happened later than `now`, so a record's stamp is clamped to it for
the per-agent clocks, and anything further ahead than `SKEW_S` is not evidence of
life at all: it is excluded from the liveness clock, counted, and reported. A clock
that disagrees with the audit trail is itself the kind of fault this pane is for.

**Silence is phase-aware.** A `tool` record is written when the tool *returns*, so
the journal legitimately goes quiet for the length of an `emerge` — up to
`tools.MAX_TIMEOUT`. Silence while a tool is in flight is therefore normal;
silence while we are waiting for a model reply is not. One threshold for both
would either cry STUCK through every build or never fire at all. Which phase we
are in is counted, not guessed: one assistant turn can issue several tool calls
and they return one record at a time, so a fast `read_file` coming back does not
mean the `emerge` beside it is finished.

**A running build is not silence at all.** `aios.build` runs an emerge as a detached
job with no time limit, and the job writes to its own log rather than to this journal —
so a three-hour compile is a journal that says nothing for three hours. That is the
case this pane exists for and the case it would have got most wrong: read through the
thresholds above it is `STUCK`, and read with nothing open it is `IDLE`. Neither is
true, and both are the specific failure `skills/tool-budget-shorter-than-task` is
about — a machine that reports honest long work as a fault teaches everyone to stop
reading the indicator. So the build registry is a second input, read the same way as
the journal (from disk, without writing a byte: `record=False`, because a monitor that
appends to what it reads certifies its own subject alive), and a running build outranks
every silence threshold here. It also excuses the one shape of repetition this whole
mechanism is *made* of: polling `build_status` for the same job is not a loop, it is
the design, and counted as a loop it would paint `LOOPING` through every long build.

**Pure reader.** No writes, no model call, no network, no subprocess: tmux runs
`status` every two seconds and a status bar that can block is a multiplexer that
hangs. The journal is read from the *tail* — a single tool record can be 20 kB, so
the file grows to megabytes — which also means a half-written final line is the
normal case and is skipped rather than fatal.

**Everything it prints is untrusted.** The journal holds the stdout of executed
package code verbatim. So every string taken out of it is stripped of escape and
control bytes before it reaches a terminal (the same reason `agent._plain`
exists), and every `#` is doubled before it reaches tmux, where `#[...]` and `#()`
are live syntax. That includes the fields that look like identifiers: `reply.model`
is whatever `ANTHROPIC_BASE_URL` returned, and a label is a name the journal
supplied — an unscrubbed `\x1b[2J` in either one wipes the pane, and its bytes also
defeat the clipping, which measures `len()`. A dashboard is exactly the surface a
hostile build log would want to write to.

`agent.py` is deliberately *not* imported for the two helpers this duplicates: it
drags `llm`, `node` and `repo` into a command that must start in milliseconds, and
the one thing the monitor must keep doing while the agent is being edited or is
broken is render.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import build as build_mod
from . import generation, welcome

JOURNAL = ".aios/agent.jsonl"

#: Tool calls whose repetition is the mechanism working, not an agent going round in
#: circles. A three-hour build is *supposed* to be many identical `build_status` calls —
#: that is the whole start/poll/read pattern — so counting them as a loop would make
#: `LOOPING` the permanent reading of every long compile. Discounted only while a build
#: is actually running: polling a job that ended hours ago is a loop like any other.
POLL_TOOLS = frozenset({"build_status", "build_tail"})

#: How much of the journal's tail is read. One tool record can carry 20 kB of
#: build log, so this is a few dozen records — the window the cockpit shows —
#: rather than a number of lines. Reading the whole file would make the status
#: command O(session) on the hot path.
TAIL_BYTES = 256 * 1024

# --- health ------------------------------------------------------------------

#: While a run is open.
HEALTH_OK = "ok"
HEALTH_STUCK = "STUCK"
HEALTH_LOOPING = "LOOPING"

#: Once it has closed, named by how it closed. `MANUAL.md` sells "three identical
#: verdicts end the run as *not verified* rather than quietly done" — so the last
#: verdict has to reach the status bar, not just the journal list. `IDLE` is what is
#: left: nothing running, and no ending in the window to report.
HEALTH_IDLE = "IDLE"
HEALTH_DONE = "done"
HEALTH_RED = "RED"
HEALTH_ASK = "ASK"

#: How each token is painted, on both surfaces. Only the two "the machine is fine"
#: tokens are green, `IDLE` is deliberately not a fault colour, and everything else
#: wants the eye. Unknown tokens warn: a health word this table has not heard of is
#: itself something to look at.
TONES = {
    HEALTH_OK: "ok",
    HEALTH_DONE: "ok",
    HEALTH_IDLE: "faint",
    HEALTH_STUCK: "warn",
    HEALTH_LOOPING: "warn",
    HEALTH_RED: "warn",
    HEALTH_ASK: "warn",
}

#: An event this recent means work is landing right now.
FRESH_S = 20.0

#: Silence while the journal is waiting for a *model reply*. `llm.TIMEOUT` is 600s
#: and a 429 is retried with backoff, so anything past this is not a slow call.
MODEL_SILENCE_S = 900.0

#: Silence while a *tool* is in flight. `tools.MAX_TIMEOUT` is 3600s because an
#: emerge takes that long, and the tool itself raises when it expires — so this is
#: the point past which the harness, not the work, has stopped.
TOOL_SILENCE_S = 3600.0

#: Past this, an agent left open in the journal is not running: no tool call and
#: no request budget can reach here. A Ctrl-C at the prompt closes a run without
#: recording an ending, and reporting that as STUCK for the rest of the day would
#: be a fault indicator nobody believes.
COLD_S = 7200.0

#: Identical tool calls, or identical red verdicts, in a row before it is a loop
#: rather than a retry. Matches `agent.REPEAT_LIMIT` in spirit: the loop's own
#: brake counts verdicts, and this also counts tool calls, which nothing else does.
LOOP_N = 3

#: Agent records newer than a stall diagnosis before the diagnosis is retired. One is
#: not evidence: the stalls the supervisor exists to name are the ones that log
#: steadily and repeat nothing, so a run emitting a record per iteration cleared the
#: verdict on every pass — and it can only be re-raised once per
#: `supervisor.ADVICE_EVERY_S`. Two records, or one that ends the run, is progress.
RETIRE_N = 2

#: How far ahead of the reader's clock a record may be stamped and still be believed.
#: `now` is sampled before the file is opened, so a record appended in between is
#: legitimately a few milliseconds in the future; past this it is a skewed clock or a
#: hand-written line, and either way its timestamp is not evidence of anything.
SKEW_S = 2.0

#: The heartbeat advances on this grid, so it steps once per `status-interval`.
BEAT_S = 2.0

MIN_WIDTH = 20


@dataclass(frozen=True)
class Marks:
    """The glyphs `welcome.Glyphs` does not already carry, in both registers."""

    frames: tuple[str, ...]
    rest: str
    into: str
    spawn: str
    back: str
    bang: str
    up: str
    stop: str
    star: str


MARKS_U = Marks(
    frames=("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
    rest="·", into="→", spawn="⤷", back="⤶", bang="!", up="↑", stop="⊘", star="⁂",
)
MARKS_A = Marks(
    frames=("|", "/", "-", "\\"),
    rest=".", into="->", spawn="<", back=">", bang="!", up="^", stop="x", star="*",
)

#: The two colours `welcome.Theme` has no name for. Restated as the 256-colour
#: codes `agent.Ink` already uses, so the health token in the status bar is the
#: same green and the same amber as an `ok`/`FAIL` line at the prompt.
OK_SGR = "\x1b[38;5;79m"
WARN_SGR = "\x1b[38;5;179m"
OK_TMUX = "colour79"
WARN_TMUX = "colour179"

#: Escape sequences and bare control bytes, dropped from everything the journal
#: hands us. `agent._CONTROL` is the same expression for the same reason — a
#: verdict is `forge probe` stdout, which is executed package code's stdout, and
#: `\x1b[2J\x1b[H` in a tool result must not be able to repaint this pane.
_CONTROL = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-_][0-?]*[ -/]*[@-~]"
    r"|\x1b."
    r"|[\x00-\x1f\x7f]"
)


def plain(value: object, limit: int = 200) -> str:
    """One line of a journal string, fit to print: no escapes, no control bytes."""
    flat = " ".join(_CONTROL.sub("", str(value)).split())
    return flat if len(flat) <= limit else flat[: max(0, limit - 1)] + "…"


def _int(value: object) -> int:
    """A journal field that should be a number. It is whatever was written there."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


# --- the journal --------------------------------------------------------------

#: Who wrote a record. Two writers share this file — `agent._record` and
#: `supervisor._log` — and only one of them is the machine doing work. Everything
#: about liveness below asks this question and nothing else, because keying on the
#: *kind* means the next thing the watcher learns to write is a new way for the
#: monitor to look busy to itself.
AUTHOR_AGENT = "agent"
AUTHOR_SUPERVISOR = "supervisor"

#: Kinds the supervisor wrote before records carried an author. `advice` is the whole
#: list: it is the only record that module has ever appended.
_LEGACY_SUPERVISOR_KINDS = frozenset({"advice"})


def _author(record: dict, kind: str) -> str:
    """Who wrote this record — stated if the writer said so, inferred if it is old.

    Old records carry no `author` at all and must keep rendering, so absence is read
    as "written before this field existed" and answered from the kind: the agent
    wrote everything except `advice`. A *present* name is returned as it stands, even
    one this reader has never heard of — which makes it not the agent, and therefore
    not activity. That is the safe direction: an unknown writer counted as activity is
    the exact failure this field exists to prevent, and health must fail towards
    STUCK, which gets read, rather than towards `ok`, which does not.
    """
    stated = record.get("author")
    if stated is None or not str(stated).strip():
        return AUTHOR_SUPERVISOR if kind in _LEGACY_SUPERVISOR_KINDS else AUTHOR_AGENT
    return plain(stated, 40).lower()


@dataclass(frozen=True)
class Event:
    ts: float
    kind: str
    label: str
    data: dict = field(default_factory=dict)
    author: str = AUTHOR_AGENT

    @property
    def by_agent(self) -> bool:
        return self.author == AUTHOR_AGENT


def _base(root: Path | str | None) -> Path:
    return Path(root if root is not None else os.environ.get("AIOS_ROOT", "."))


def journal_path(root: Path | str | None = None) -> Path:
    return _base(root) / JOURNAL


def _tail(path: Path, limit: int = TAIL_BYTES) -> tuple[list[str], str]:
    """The last `limit` bytes of the journal as whole lines, plus why not more."""
    start = 0
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - limit)
            handle.seek(start)
            blob = handle.read()
    except FileNotFoundError:
        return [], "no journal yet"
    except OSError as exc:
        return [], f"journal unreadable ({type(exc).__name__})"
    if not size:
        return [], "journal empty"
    lines = blob.decode("utf-8", "replace").splitlines()
    if start and lines:
        # The seek landed inside a record. That record is not ours to read.
        lines.pop(0)
    return lines, ""


def _events(lines: Sequence[str]) -> tuple[list[Event], int]:
    """Parse what parses.

    A journal is appended to while it is being read, so the *last* line failing to
    parse is the ordinary case and is not counted: reporting "1 unreadable line"
    every time the agent writes a record would train the reader to ignore the count.
    Any earlier line that fails is real damage and is counted.
    """
    events: list[Event] = []
    skipped = 0
    mid_append = False
    for line in lines:
        text = line.strip()
        if not text:
            continue
        record = None
        try:
            record = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        if not isinstance(record, dict):
            skipped, mid_append = skipped + 1, True
            continue
        mid_append = False
        try:
            ts = float(record.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        kind = str(record.get("kind") or "?")
        events.append(
            Event(ts, kind, str(record.get("label") or ""), record, _author(record, kind))
        )
    return events, skipped - (1 if mid_append else 0)


# --- what is running ----------------------------------------------------------


@dataclass
class Live:
    """One agent the journal says is still open."""

    label: str
    model: str = ""
    started: float = 0.0
    last: float = 0.0
    tool: str = ""
    verdict: str = ""
    verdict_ok: bool | None = None
    #: Tool calls issued by the last reply that have not written a record yet.
    #: Counted rather than inferred from the newest record: `agent._results_turn`
    #: invokes one turn's calls in order and journals each as it *returns*, so a
    #: 20 ms `read_file` finishing tells us nothing about the `emerge` next to it.
    #: Nonzero means a tool is in flight, which is what picks the silence threshold.
    outstanding: int = 0
    #: The same fact in words, for the pane.
    awaiting: str = "a reply"


@dataclass(frozen=True)
class Ending:
    """How the last closed run ended — the first thing an operator asks about.

    Derived from the record that closed it, so it says what the machine did rather
    than what anything claims: a green `verify`, `stuck` (gave up on identical
    verdicts), a spent `budget`, or `needs_decision` (it is waiting for a human).
    """

    kind: str = ""
    detail: str = ""
    ts: float = 0.0
    repeats: int = 0


@dataclass(frozen=True)
class State:
    now: float
    generation: int = 0
    agents: tuple[Live, ...] = ()
    events: tuple[Event, ...] = ()
    health: str = HEALTH_IDLE
    why: str = ""
    model: str = ""
    advice: str = ""
    advice_ts: float = 0.0
    #: The supervisor's last "this run is not progressing", if the agent has not
    #: written anything since. Empty is the normal case, including while advice is
    #: being displayed: only a verdict the writer flagged as a stall lands here.
    stalled: str = ""
    last_ts: float = 0.0
    #: The newest agent record's timestamp exactly as written, and how many agent
    #: records carry it. Distinct from `last_ts`, which is clamped to `now` and is
    #: what every threshold measures: these two are the change signal, and a change
    #: signal must not move because the clock moved.
    mark: float = 0.0
    mark_n: int = 0
    #: Agent records dated more than `SKEW_S` ahead of `now`. A fault in itself.
    ahead: int = 0
    ended: Ending = Ending()
    note: str = ""
    skipped: int = 0
    #: Every detached build this node knows about, newest first — the second input, and
    #: the only one that does not come out of the journal. Read, never written.
    builds: tuple[build_mod.Status, ...] = ()

    @property
    def building(self) -> tuple[build_mod.Status, ...]:
        """The builds that are actually running. Long silence from one of these is the
        work happening, so this is what outranks the thresholds below."""
        return tuple(state for state in self.builds if state.running)

    @property
    def silence(self) -> float:
        """How long the *agent* has been quiet. Watcher writes are not activity."""
        return max(0.0, self.now - self.last_ts) if self.last_ts else 0.0

    @property
    def fingerprint(self) -> str:
        """Has anything the *agent* did changed since last time.

        Supervisor-authored records are excluded deliberately: the supervisor appends
        its own advice to this file, and a fingerprint that counted it would make
        every advisory call justify the next one for as long as the pane is open —
        one call a minute into an empty room.

        Which is why this is the newest agent record's own stamp and the number of
        agent records sharing it, and not a count over the window. The window is the
        journal's last `TAIL_BYTES`, so counting inside it made the fingerprint move
        whenever it *slid* — and the supervisor's own advice writes are what slide it,
        which is the self-feeding signal the rate limit exists to prevent, arriving
        through the gate that was supposed to stop it. The stamp is the raw one, not
        `last_ts`: a clamped stamp tracks `now`, and a fingerprint that changes every
        second buys a model call every minute.
        """
        return f"{self.mark:.6f}/{self.mark_n}"


def _names(value: object) -> list[str]:
    """A journal field that should be a list of names, whatever it actually is.

    Nothing in this module may assume the journal's *types*. It is appended to by a
    live process, it carries payload text, and a `TypeError` from a status command
    would put a traceback in the tmux status bar — which is the one place a fault
    must be legible.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _tool_detail(data: dict) -> str:
    args = data.get("input")
    if not isinstance(args, dict):
        return plain(args, 60) if args else ""
    for key in ("command", "path", "atom", "task", "names"):
        if key in args:
            return plain(args[key], 60)
    return plain(" ".join(f"{k}={v}" for k, v in list(args.items())[:2]), 60)


#: Scratch directory names, folded so one bug does not read as many failures.
#: `forge probe` runs every check in a fresh tempdir and inlines its stderr, which
#: is why `agent._same_failure` folds the same shape before comparing verdicts.
_SCRATCH = re.compile(r"\b(forge-probe-|tmp)[A-Za-z0-9_]{6,}")


def _fold(detail: str) -> str:
    return _SCRATCH.sub(r"\1#", plain(detail, 400))


def _run_length(items: Sequence[object]) -> int:
    if not items:
        return 0
    last, count = items[-1], 0
    for item in reversed(items):
        if item != last:
            break
        count += 1
    return count


#: Records that end a run, or open the next one. `looping` is scanned only from the
#: last of these onwards: the trailing run of identical tool calls a *finished* run
#: left behind is still the trailing run the instant a new `request` arrives, so
#: without a boundary a fresh run inherits a dead one's loop and the pane names a
#: command this run never issued. `agent.run` scopes its own repeat counter per
#: request for the same reason.
_BOUNDARY = frozenset({"request", "stuck", "needs_decision"})


def _is_boundary(event: Event) -> bool:
    if event.kind in _BOUNDARY:
        return True
    if event.kind == "budget":
        # A sub-agent running out of budget does not end the run it was spawned from,
        # so it must not hide the coordinator's own repetition either.
        return (event.label or "main") == "main"
    return event.kind == "verify" and bool(event.data.get("ok"))


def looping(
    events: Sequence[Event], limit: int = LOOP_N, ignore: frozenset[str] = frozenset()
) -> str:
    """The same call or the same red verdict `limit` times in a row, described.

    `events` must already be scoped to one run — see `_BOUNDARY`.

    `ignore` names tools whose records are dropped from the sequence rather than
    breaking it — see `POLL_TOOLS`. Dropped rather than treated as a boundary on
    purpose: an agent alternating one real repeated call with a poll would otherwise
    have every repetition hidden by the polling it is entitled to do.
    """
    calls = [
        (
            str(e.data.get("name") or "?"),
            json.dumps(e.data.get("input"), sort_keys=True, default=str),
        )
        for e in events
        if e.kind == "tool" and str(e.data.get("name") or "?") not in ignore
    ]
    repeats = _run_length(calls)
    if repeats >= limit:
        return f"{plain(calls[-1][0], 30)} {repeats}x with the same arguments"

    # Green verdicts stay in the list so a red-green-red alternation does not read
    # as a run of reds.
    verdicts = [
        (bool(e.data.get("ok")), "" if e.data.get("ok") else _fold(str(e.data.get("detail"))))
        for e in events
        if e.kind == "verify"
    ]
    repeats = _run_length(verdicts)
    if verdicts and not verdicts[-1][0] and repeats >= limit:
        return f"the same red verdict {repeats}x"
    return ""


def digest(
    events: Sequence[Event],
    now: float,
    *,
    note: str = "",
    skipped: int = 0,
    gen: int = 0,
    builds: Sequence[build_mod.Status] = (),
) -> State:
    """Fold the journal into "what is open, and is it healthy".

    Agents are opened by `request` (main) and `spawn` (sub), and closed by the
    records that end a run — a green verify, `stuck`, `needs_decision`, a spent
    budget, `spawn_done`. Any labelled record also opens its agent if we have not
    seen its start: the tail may begin mid-run, and reporting IDLE while an agent
    is plainly logging would be the one failure this display must not have.

    The record that closed the last run is kept (`Ending`), because "it gave up" and
    "it finished" are the same shape here — nothing running — and they are not the
    same news.

    Only agent-authored records get any of that. The supervisor's own lines are read
    for what they say and then skipped: they open no agent, close no run, and do not
    touch the clock every health token is measured against.
    """
    running: dict[str, Live] = {}
    last_model = ""
    advice, advice_ts = "", 0.0
    #: The outstanding stall diagnosis, the stamp of the advice that raised it, and how
    #: many agent records have landed since — see `RETIRE_N`.
    stalled, stall_ts, evidence = "", 0.0, 0
    last_ts, mark, mark_n, ahead = 0.0, 0.0, 0, 0
    ended = Ending()
    boundary = 0  # where the current run starts, for the repetition scan

    def touch(label: str, model: str, ts: float) -> Live:
        live = running.get(label)
        if live is None:
            live = running[label] = Live(label=label, started=ts, last=ts)
        if model:
            live.model = model
        live.last = max(live.last, ts)
        return live

    for index, event in enumerate(events):
        data, label = event.data, event.label or "main"
        if not event.by_agent:
            # The watcher's own writes. Displayed, audited, and invisible to every
            # line below: this is the whole of "a monitor must not be able to read
            # its own heartbeat as the machine's".
            if event.kind == "advice":
                advice, advice_ts = plain(data.get("text"), 400), event.ts
                # The newest advice supersedes the last one, in both directions: the
                # writer withdrawing its own verdict is the writer's business.
                stalled = plain(data.get("text"), 200) if data.get("stall") else ""
                stall_ts, evidence = event.ts, 0
            continue

        # Nothing can have happened later than now, so this is what the per-agent
        # clocks and the endings are stamped with — an elapsed time computed against a
        # record from the future is not a duration.
        ts = min(event.ts, now)
        credible = event.ts <= now + SKEW_S
        if credible:
            last_ts = max(last_ts, ts)
        else:
            # Past the tolerance the stamp is not evidence of anything: not of life,
            # which is what folding it into `last_ts` silently claimed — that one line
            # disabled every staleness threshold at once — and not of newness either,
            # which is why it cannot retire a diagnosis below.
            ahead += 1
        # The change signal, off the raw stamp: see `State.fingerprint`.
        if event.ts > mark:
            mark, mark_n = event.ts, 1
        elif event.ts == mark:
            mark_n += 1

        if stalled and credible and event.ts > stall_ts:
            # Retired by evidence, not by arrival: a stalled run still logs, and one
            # record per iteration cleared the verdict faster than the supervisor's
            # rate limit could re-raise it. A record that ends the run is enough on its
            # own — that is progress by definition, not another turn of the same loop.
            evidence += 1
            if evidence >= RETIRE_N or _is_boundary(event):
                stalled, evidence = "", 0
        if _is_boundary(event):
            boundary = index + 1
        if event.kind == "request":
            running.clear()
            ended = Ending()  # a new run: the previous outcome is no longer the news
            touch("main", last_model, ts)
        elif event.kind == "spawn":
            model = str(data.get("model") or "")
            touch(f"sub:{model}", model, ts)
        elif event.kind in ("spawn_done", "spawn_failed"):
            running.pop(f"sub:{data.get('model') or ''}", None)
        elif event.kind == "reply":
            live = touch(label, str(data.get("model") or ""), ts)
            last_model = live.model or last_model
            calls = _names(data.get("tool_calls"))
            live.outstanding = len(calls)
            live.awaiting = _awaiting(len(calls))
        elif event.kind == "tool":
            live = touch(label, "", ts)
            live.tool = f"{plain(data.get('name'), 30)} {_tool_detail(data)}".strip()
            # One call of the turn came back. The rest are still out there.
            live.outstanding = max(0, live.outstanding - 1)
            live.awaiting = _awaiting(live.outstanding)
        elif event.kind == "verify":
            live = touch("main", last_model, ts)
            live.verdict_ok = bool(data.get("ok"))
            live.verdict = plain(data.get("detail"), 400)
            if live.verdict_ok:
                running.clear()  # green is the only way a request ends satisfied
                ended = Ending("verify", live.verdict, ts)
        elif event.kind in ("stuck", "needs_decision"):
            running.clear()
            said = data.get("detail") if event.kind == "stuck" else data.get("question")
            ended = Ending(event.kind, plain(said, 400), ts, _int(data.get("repeats")))
        elif event.kind == "budget":
            if label == "main":
                running.clear()
                ended = Ending("budget", plain(data.get("detail"), 400), ts)
            else:
                running.pop(label, None)
        elif event.kind in ("escalate", "deferral"):
            touch("main", last_model, ts)

    silence = max(0.0, now - last_ts) if last_ts else 0.0
    cold = bool(last_ts) and silence > COLD_S
    agents = () if cold else tuple(running.values())
    building = tuple(state for state in builds if state.running)
    health, why = _health(
        agents, events[boundary:], now, last_ts, note, bool(running) and cold, ended,
        stalled, building,
    )
    if ahead:
        health, why = _skewed(health, why, ahead, bool(last_ts) or bool(building))

    return State(
        now=now,
        generation=gen,
        agents=agents,
        events=tuple(events),
        health=health,
        why=why,
        model=(agents[-1].model if agents and agents[-1].model else last_model),
        advice=advice,
        advice_ts=advice_ts,
        stalled=stalled,
        last_ts=last_ts,
        mark=mark,
        mark_n=mark_n,
        ahead=ahead,
        ended=ended,
        note=note,
        skipped=skipped,
        builds=tuple(builds),
    )


def _skewed(health: str, why: str, ahead: int, measurable: bool) -> tuple[str, str]:
    """Report the records dated ahead of the clock, and say so first if it is all we have.

    Surfaced rather than normalised away: a record from the future is a fault in its
    own right — a skewed clock, or a line somebody wrote by hand into an append-only
    machine-written file — and it is the one input that can make every threshold above
    unreachable at once. When it is the *only* thing in the journal there is no
    liveness left to measure, and an unmeasurable machine fails towards `STUCK`, the
    token that gets read, rather than towards "nothing has run yet", which does not.
    """
    if not measurable:
        return HEALTH_STUCK, f"{ahead_of_clock(ahead)}, and nothing credible to measure"
    return health, f"{why}; {ahead_of_clock(ahead)}"


def ahead_of_clock(ahead: int) -> str:
    """Said once, because both surfaces say it — and the pane is 34 columns wide."""
    return f"{ahead} record(s) ahead of the clock"


def _awaiting(outstanding: int) -> str:
    if not outstanding:
        return "a reply"
    return f"{outstanding} tool call{'s' if outstanding != 1 else ''}"


def _health(
    agents: Sequence[Live],
    window: Sequence[Event],
    now: float,
    last_ts: float,
    note: str,
    abandoned: bool,
    ended: Ending,
    stalled: str = "",
    building: Sequence[build_mod.Status] = (),
) -> tuple[str, str]:
    """`window` is the current run's events only — anything earlier is another run.

    `building` is the running detached builds, and it outranks every silence threshold
    below. A compile writes to its own log, not to this journal, so a build that has
    printed nothing here for twenty minutes is a build — and the thresholds would call
    it `STUCK` while an empty journal alongside it would call it `IDLE`. Both are wrong
    and both are worse than wrong: an indicator that fires during correct operation is
    an indicator nobody reads, which is exactly how a real stall gets missed. The one
    signal ranked above it is `LOOPING`, because that is a statement about the *agent*
    rather than about the work, and an agent going in circles beside a healthy build is
    still an agent going in circles.

    `stalled` is the supervisor's outstanding "this is not progressing", and it is the
    only input here that did not come out of the machine's own records. It is ranked
    below both signals that did: `looping` and the silence thresholds name what is
    wrong from the journal itself, and a diagnosis is only needed for the stalls those
    two cannot see — the eleven-minute exploratory loop that logs steadily and repeats
    nothing. It can only ever raise an alarm, never clear one, so the worst a wrong
    line of advice buys is a STUCK a human reads and disagrees with.

    That makes *dropping* it the one unsafe direction, and it was dropped in the worst
    case there is: consulted only while an agent was open, a flagged stall vanished the
    moment the run went quiet past `COLD_S` — a machine hung with nobody home reported
    the faint `IDLE` of a box with nothing to do. So the two arms that would otherwise
    say IDLE consult it too. The arm that does not is `ended`: a terminal record is the
    machine's own account of how the run closed, `RED` and `ASK` are already alarms,
    and any of those records retires the diagnosis anyway (`RETIRE_N`).
    """
    if not last_ts and not building:
        return HEALTH_IDLE, note or "nothing has run yet"

    silence = max(0.0, now - last_ts) if last_ts else 0.0
    if agents:
        # Scoped before the build check, because a loop is the one fault a running
        # build does not excuse. Poll calls are discounted only while one is running.
        loop = looping(window, ignore=POLL_TOOLS if building else frozenset())
        if loop:
            return HEALTH_LOOPING, loop
    if building:
        return HEALTH_OK, building_why(building, now)
    if agents:
        allowed = TOOL_SILENCE_S if agents[-1].outstanding else MODEL_SILENCE_S
        if silence > allowed:
            return HEALTH_STUCK, (
                f"nothing logged for {ago(silence)}, waiting for {agents[-1].awaiting}"
            )
        if stalled:
            return HEALTH_STUCK, f"supervisor: {stalled}"
        if silence <= FRESH_S:
            return HEALTH_OK, f"last event {ago(silence)} ago"
        return HEALTH_OK, f"waiting for {agents[-1].awaiting}, {ago(silence)} so far"
    if abandoned:
        quiet = f"a run went quiet {ago(silence)} ago without an ending"
        if stalled:
            return HEALTH_STUCK, f"{quiet} — supervisor: {stalled}"
        return HEALTH_IDLE, quiet
    if ended.kind:
        return _outcome(ended, max(0.0, now - ended.ts))
    if stalled:
        return HEALTH_STUCK, f"no agent running — supervisor: {stalled}"
    return HEALTH_IDLE, f"no agent running, last event {ago(silence)} ago"


def building_why(
    running: Sequence[build_mod.Status], now: float, limit: int = 200
) -> str:
    """Why the machine is healthy while its journal says nothing: it is compiling.

    Says the elapsed time because that is the number an operator is actually deciding
    on — "building for 8s" and "building for 3h 20m" are the same health and completely
    different news — and names what is being built, scrubbed like every other string
    here: the atoms came from the model and travelled through the registry on disk.
    """
    oldest = max(running, key=lambda state: state.elapsed)
    what = plain(oldest.job.what if oldest.job else "?", 60)
    more = f" (+{len(running) - 1} more)" if len(running) > 1 else ""
    return plain(
        f"building {what}{more} for {ago(oldest.elapsed)} — no time limit, and a quiet "
        "build log is a build compiling",
        limit,
    )


def _outcome(ended: Ending, age: float) -> tuple[str, str]:
    """The closed run's own verdict, as a token. Not a mood — the record's own word."""
    when = f"{ago(age)} ago"
    if ended.kind == "verify":
        return HEALTH_DONE, f"verified green {when}"
    if ended.kind == "stuck":
        counted = f"{ended.repeats} identical verdicts" if ended.repeats else "repeated verdicts"
        return HEALTH_RED, f"gave up after {counted} {when} — still red: {ended.detail}"
    if ended.kind == "budget":
        spent = f" — still red: {ended.detail}" if ended.detail else ""
        return HEALTH_RED, f"budget spent {when} without a green verdict{spent}"
    return HEALTH_ASK, f"asked you {when}: {ended.detail}"


def ago(seconds: float) -> str:
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {total % 3600 // 60:02d}m"


def heartbeat(state: State, marks: Marks) -> str:
    """A glyph that moves while work is open — and freezes when stuck.

    A running build counts as work even with no agent open, which is the ordinary shape
    of a long compile: the agent started the job and stopped, and the job has hours to
    go. A still glyph means "nothing is running", so leaving it still through a
    three-hour build would make it say the one thing that was not true.
    """
    if not state.agents and not state.building:
        return marks.rest
    if state.health == HEALTH_STUCK:
        return marks.frames[0]
    return marks.frames[int(state.now / BEAT_S) % len(marks.frames)]


def short_label(label: str) -> str:
    """`sub:claude-sonnet-5` -> `sub sonnet-5`. The label is the agent's own name."""
    head, sep, tail = plain(label, 60).partition(":")
    return f"{head} {short_model(tail)}" if sep else head


def short_model(name: str) -> str:
    """`claude-haiku-4-5-20251001` -> `haiku-4-5`. The pane is 34 columns wide.

    Scrubbed before it is shortened, not after: `reply.model` is echoed from
    whatever endpoint answered, and an identifier is exactly the field nobody
    thinks to sanitise. Shortening first would carry the escape bytes through.
    """
    name = plain(name, 60)
    parts = (name[7:] if name.startswith("claude-") else name).split("-")
    if parts and len(parts[-1]) == 8 and parts[-1].isdigit():
        parts = parts[:-1]
    return "-".join(p for p in parts if p) or name


def read(root: Path | str | None = None, now: float | None = None) -> State:
    when = time.time() if now is None else now
    lines, note = _tail(journal_path(root))
    events, skipped = _events(lines)
    return digest(
        events,
        when,
        note=note,
        skipped=skipped,
        gen=_generation(root),
        builds=_builds(root, when),
    )


def _builds(root: Path | str | None, now: float) -> tuple[build_mod.Status, ...]:
    """The build registry, read the way the journal is: from disk, and never written.

    `record=False` is the load-bearing argument. `aios.build.status` journals the moment
    it first observes a job's exit, and a display that took that write would be
    appending to the file it reads its own subject's health out of — the bug
    `skills/monitor-writes-what-it-reads` is named after, where the status bar said `ok`
    for eleven minutes because the watcher kept writing about the stall. Whoever polls a
    build on purpose gets to record its ending; this pane only looks.

    Broad `except` for the reason `main` has one: this is the tmux status hot path, and
    a pane that stops drawing is indistinguishable from a machine with nothing to do.
    """
    try:
        return tuple(build_mod.report(root, now=now, record=False, last_line=True))
    except Exception:
        return ()


def _generation(root: Path | str | None) -> int:
    """The same counter `generation.current()` reads, against the root we were given."""
    try:
        text = (_base(root) / ".aios" / generation.FILE).read_text(encoding="utf-8")
        return int(text.strip() or 0)
    except (OSError, ValueError):
        return 0


# --- the status bar -----------------------------------------------------------

#: tmux expands `#[...]` and `#()` in what a status job prints, so a `#` from the
#: journal is executable syntax. Doubling is tmux's own escape for a literal one.
#: `plain` first, because tmux passes an ESC byte straight through to the terminal
#: and neutralising only the syntax tmux owns leaves the pane repaintable.
def _tmux_safe(text: str) -> str:
    return plain(text).replace("#", "##")


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def status_line(state: State, colour: bool | None = None, marks: Marks | None = None) -> str:
    """One short line for `status-right`. Everything on it is a live reading.

    Nothing here is remembered between calls, so there is no stale value to print:
    a fact that cannot be read says `?` and a journal that cannot be read says so.
    """
    theme = welcome.Theme.detect(os.environ)
    colour = bool(theme.reset) if colour is None else colour
    marks = marks or (MARKS_A if welcome.glyphs_for(sys.stdout) is welcome.ASCII else MARKS_U)

    def paint(text: str, tmux_colour: str = "", bold: bool = False) -> str:
        if not colour or not tmux_colour:
            return text
        attr = ",bold" if bold else ""
        return f"#[fg={tmux_colour}{attr}]{text}#[default]"

    faint, label = _hex(welcome.FAINT_RGB), _hex(welcome.LABEL_RGB)
    count = len(state.agents)
    middle = _tmux_safe(state.note or (short_model(state.model) if state.model else "model ?"))
    tone = _health_tone(state.health)
    health = paint(
        _tmux_safe(state.health),
        {"ok": OK_TMUX, "warn": WARN_TMUX}.get(tone, faint),
        bold=tone == "warn",
    )
    gen = str(state.generation) if state.generation else "?"

    dot = paint(marks.rest, faint)
    segments = [
        paint(heartbeat(state, marks), label),
        paint(f"agents {count}", label),
    ]
    # A build takes hours and the agent that started it is usually gone, so without
    # this the one line a human glances at has nothing on it while the machine is
    # doing the longest thing it ever does. In the label colour, not the warn one:
    # a compile is work, not a fault.
    if state.building:
        segments += [dot, paint(_tmux_safe(build_segment(state.building)), label)]
    return " ".join(
        segments
        + [
            dot,
            paint(middle, faint),
            dot,
            paint(f"gen {gen}", faint),
            dot,
            health,
        ]
    )


def build_segment(running: Sequence[build_mod.Status]) -> str:
    """`build 2h 14m`, or `builds 3 2h 14m` — the longest-running one's elapsed time.

    Short because the status bar is: the atoms go on the pane, which has room, and the
    number a glance is for is how long this has been going.
    """
    longest = max(state.elapsed for state in running)
    if len(running) == 1:
        return f"build {ago(longest)}"
    return f"builds {len(running)} {ago(longest)}"


# --- the pane -----------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """A line as (text, tone) segments, so clipping happens before colouring."""

    parts: tuple[tuple[str, str], ...] = ()

    @property
    def plain(self) -> str:
        return "".join(text for text, _ in self.parts)


def _row(*parts: tuple[str, str]) -> Row:
    return Row(tuple(parts))


def _indent(row: Row, spaces: int = 1) -> Row:
    return Row(((" " * spaces, ""), *row.parts)) if row.parts else row


def _clip(row: Row, width: int, cut: str) -> Row:
    """Never wrap: the pane is narrow and a torn line is worse than a short one."""
    if len(row.plain) <= width:
        return row
    kept: list[tuple[str, str]] = []
    room = max(0, width - len(cut))
    for text, tone in row.parts:
        if room <= 0:
            break
        kept.append((text[:room], tone))
        room -= len(text[:room])
    kept.append((cut, "faint"))
    return Row(tuple(kept))


def _paint(row: Row, theme: welcome.Theme) -> str:
    if not theme.reset:
        return row.plain
    out = []
    for text, tone in row.parts:
        if tone == "ok":
            out.append(f"{OK_SGR}{text}{theme.reset}")
        elif tone == "warn":
            out.append(f"{WARN_SGR}{text}{theme.reset}")
        elif tone == "bold":
            out.append(f"\x1b[1m{text}{theme.reset}")
        else:
            out.append(theme.paint(text, {"accent": theme.accent, "label": theme.label,
                                          "faint": theme.faint}.get(tone, "")))
    return "".join(out)


def _heading(word: str, note: str, width: int) -> Row:
    """Letterspaced lowercase, the same small-caps register the welcome screen sets."""
    spaced = " ".join(word)
    gap = width - len(spaced) - len(note)
    if note and gap >= 2:
        return _row((spaced, "label"), (" " * gap, ""), (note, "faint"))
    return _row((spaced, "label"))


def _health_tone(health: str) -> str:
    return TONES.get(health, "warn")


def _agent_rows(state: State, width: int, glyphs: welcome.Glyphs, marks: Marks) -> list[Row]:
    rows: list[Row] = []
    if not state.agents:
        # Said plainly and in the faint colour: an idle machine is not a fault, and
        # the header line above already carries the reason.
        return [_row(("no agent running", "faint"))]

    for live in state.agents:
        # Both of these are journal fields, so both are scrubbed by the shorteners.
        name = short_label(live.label)
        model = short_model(live.model) if live.model else "model ?"
        gap = width - len(name) - len(model) - 2
        if gap >= 2:
            rows.append(_row((f"{glyphs.caret} ", "accent"), (name, "accent"),
                             (" " * gap, ""), (model, "faint")))
        else:
            rows.append(_row((f"{glyphs.caret} ", "accent"), (name, "accent")))
            rows.append(_row(("  ", ""), (model, "faint")))
        elapsed = ago(max(0.0, state.now - live.started))
        rows.append(_row(("  ", ""), (f"{elapsed} ", "label"),
                         (f"{glyphs.dot} waiting for {live.awaiting}", "faint")))
        if live.tool:
            rows.append(_row(("  ", ""), (f"{glyphs.dot} ", "faint"), (live.tool, "")))
        if live.verdict_ok is not None:
            tone = "ok" if live.verdict_ok else "warn"
            head = "ok  " if live.verdict_ok else "FAIL "
            rows.append(_row(("  ", ""), (head, tone), (live.verdict, "")))
    return rows


#: How many builds the pane shows. The registry keeps every job this node ever ran, and
#: a pane thirty-four columns wide has room for the ones still deciding something.
BUILD_ROWS = 3


def _build_rows(state: State, width: int, glyphs: welcome.Glyphs) -> list[Row]:
    """The builds section: what is compiling, for how long, and its last log line.

    Running ones first regardless of age, because they are the only ones that are still
    a question. A finished one stays visible for a while on purpose — `exited 1` is the
    thing an operator most needs to walk back into, and the journal line that recorded
    it scrolls away.
    """
    order = sorted(state.builds, key=lambda s: (not s.running, -s.elapsed))
    rows: list[Row] = []
    for entry in order[:BUILD_ROWS]:
        job = entry.job
        if job is None:
            continue
        what = plain(job.what, max(8, width - 12))
        if entry.running:
            head, tone = f"{ago(entry.elapsed)} building", "accent"
        elif entry.state == build_mod.EXITED and entry.code == 0:
            head, tone = f"{ago(entry.elapsed)} ok  exit 0", "ok"
        elif entry.state == build_mod.EXITED:
            head, tone = f"{ago(entry.elapsed)} FAIL exit {entry.code}", "warn"
        else:
            # Gone with no status recorded. Not a pass, and painted like the open
            # question it is: nothing here ever proved this build finished.
            head, tone = f"{ago(entry.elapsed)} vanished, exit unknown", "warn"
        rows.append(_row((f"{glyphs.caret} ", tone), (what, tone)))
        rows.append(_row(("  ", ""), (head, tone)))
        if entry.last:
            # The build's own stdout, so `plain` is not decoration here: this is
            # executed package code writing into a terminal pane.
            rows.append(_row(("  ", ""), (f"{glyphs.dot} ", "faint"),
                             (plain(entry.last, max(8, width - 4)), "")))
    hidden = len(state.builds) - len(order[:BUILD_ROWS])
    if hidden > 0:
        rows.append(_row((f"{hidden} older build(s)", "faint")))
    return rows


def journal_rows(
    state: State, limit: int, glyphs: welcome.Glyphs, marks: Marks, stamp: str = "%H:%M:%S"
) -> list[Row]:
    """The last `limit` records in human form — the same vocabulary the prompt prints."""
    rows: list[Row] = []
    for event in state.events[-limit:] if limit else ():
        text, tone = _event_body(event, glyphs, marks)
        when = time.strftime(stamp, time.localtime(event.ts)) if event.ts else "--:--:--"
        rows.append(_row((f"{when} ", "faint"), (text, tone)))
    return rows


def _event_body(event: Event, glyphs: welcome.Glyphs, marks: Marks) -> tuple[str, str]:
    data, kind = event.data, event.kind
    if kind == "request":
        return f"you  {plain(data.get('text'))}", "accent"
    if kind == "reply":
        said = plain(data.get("text"))
        calls = _names(data.get("tool_calls"))
        if said:
            return f"say  {said}", ""
        if calls:
            return f"{marks.into} {plain(', '.join(calls))}", "faint"
        return "say  (nothing said)", "faint"
    if kind == "tool":
        secs = data.get("duration_s")
        took = f"  {secs}s" if isinstance(secs, (int, float)) else ""
        name = plain(data.get("name"), 30)
        if data.get("is_error"):
            return f"{marks.bang} {name} refused{took}", "warn"
        return f"{glyphs.dot} {name} {_tool_detail(data)}{took}", ""
    if kind == "verify":
        detail = plain(data.get("detail"))
        return (f"ok  verify  {detail}", "ok") if data.get("ok") else (f"FAIL {detail}", "warn")
    if kind == "spawn":
        return f"{marks.spawn} spawn {short_model(str(data.get('model') or ''))}", "faint"
    if kind == "spawn_done":
        model = short_model(str(data.get("model") or ""))
        return f"{marks.back} {model} {plain(data.get('status'), 40)}", "faint"
    if kind == "spawn_failed":
        return f"{marks.back} {short_model(str(data.get('model') or ''))} did not finish", "warn"
    if kind == "escalate":
        return f"{marks.up} escalating to {short_model(str(data.get('to') or ''))}", "warn"
    if kind == "stuck":
        return f"{marks.stop} gave up after {data.get('repeats', '?')} verdicts", "warn"
    if kind == "needs_decision":
        return f"?  {plain(data.get('question'))}", "warn"
    if kind == "deferral":
        return f"{marks.bang} deferral {plain(data.get('quote'), 60)}", "warn"
    if kind == "budget":
        return f"{marks.stop} budget spent ({plain(data.get('label'), 20)})", "warn"
    if kind == build_mod.KIND_STARTED:
        return f"{marks.into} build {plain(' '.join(_names(data.get('atoms'))) or '@aios', 40)}", ""
    if kind == build_mod.KIND_EXITED:
        code = data.get("code")
        if data.get("stopped"):
            return f"{marks.stop} build stopped ({plain(data.get('job'), 24)})", "warn"
        if code is None:
            # The audit trail's version of `vanished`: no status was ever recorded, so
            # this line must not read like an ending that went fine.
            return f"{marks.bang} build vanished, exit unknown", "warn"
        took = f"  {data.get('elapsed_s')}s" if data.get("elapsed_s") is not None else ""
        if _int(code) == 0:
            return f"ok  build exit 0{took}", "ok"
        return f"FAIL build exit {plain(code, 8)}{took}", "warn"
    if kind == "advice":
        return f"{marks.star} {plain(data.get('text'))}", "label"
    if kind == "log_failed":
        return f"{marks.bang} the journal could not be written", "warn"
    return plain(kind, 40), "faint"


def watch_rows(
    state: State,
    width: int,
    height: int,
    glyphs: welcome.Glyphs,
    marks: Marks,
    notes: Sequence[str] = (),
) -> list[Row]:
    body = max(MIN_WIDTH - 1, width - 1)  # one column of left margin, like the manifest
    clock = time.strftime("%H:%M:%S", time.localtime(state.now))

    head: list[Row] = [
        _row(
            (f"{heartbeat(state, marks)} ", "label"),
            (state.health, _health_tone(state.health)),
            (f"  gen {state.generation or '?'}", "faint"),
            (f"  {clock}", "faint"),
        ),
        _row((plain(state.why, body), "faint")),
        _row((glyphs.rule * body, "faint")),
    ]
    # Above the agents, not below the advice: these are the *display's* own state —
    # why advice is off, what could not be read, what raised — and a short pane clips
    # from the bottom, so anything after the advisory block is the first thing lost.
    head += [_row((plain(note, body), "faint")) for note in notes]
    if state.skipped:
        head.append(_row((f"{state.skipped} unreadable line(s)", "faint")))
    if state.ahead:
        # Not faint, unlike the two above: an unreadable line loses one record, a
        # record from the future was quietly voiding every threshold on this pane.
        head.append(_row((ahead_of_clock(state.ahead), "warn")))

    head.append(_heading("agents", str(len(state.agents)), body))
    head += _agent_rows(state, body, glyphs, marks)

    # Above the advice and below the agents: a build is the machine's own work, and the
    # section is omitted entirely when there are none rather than saying "no builds" —
    # unlike agents, whose absence is itself the answer to "is anything happening".
    if state.builds:
        head.append(_row())
        head.append(_heading("builds", str(len(state.building)), body))
        head += _build_rows(state, body, glyphs)

    if state.advice:
        head.append(_row())
        head.append(_heading("advice", ago(max(0.0, state.now - state.advice_ts)) + " ago", body))
        # Two lines at most; the supervisor clips it, and this refuses to be the
        # place where an advisory paragraph pushes the machine's own facts away.
        for line in state.advice.split(" | ")[:2]:
            head.append(_row((marks.star + " ", "label"), (plain(line, body), "label")))

    head.append(_row())
    head.append(_heading("journal", "", body))
    tail = journal_rows(state, 40, glyphs, marks, "%H:%M:%S" if body >= 40 else "%H:%M")

    if not tail:
        tail = [_row(("(nothing yet)", "faint"))]
    room = max(0, height - len(head))
    rows = (head + tail[-room:] if room else head)[:height]
    return [_clip(_indent(row), width, glyphs.cut) for row in rows]


def watch_lines(
    state: State,
    width: int | None = None,
    height: int | None = None,
    theme: welcome.Theme | None = None,
    glyphs: welcome.Glyphs | None = None,
    notes: Sequence[str] = (),
) -> list[str]:
    size = shutil.get_terminal_size((80, 24))
    theme = welcome.Theme.detect(os.environ) if theme is None else theme
    glyphs = welcome.glyphs_for(sys.stdout) if glyphs is None else glyphs
    marks = MARKS_A if glyphs is welcome.ASCII else MARKS_U
    rows = watch_rows(
        state,
        max(MIN_WIDTH, size.columns if width is None else width),
        max(6, size.lines if height is None else height),
        glyphs,
        marks,
        notes,
    )
    return [_paint(row, theme) for row in rows]


def frame(lines: Sequence[str], theme: welcome.Theme | None = None) -> str:
    """A redraw. Home-and-erase rather than clear-and-print, so it does not flicker.

    Strictly *between* the lines: `watch_rows` returns exactly as many rows as the
    pane has, so a newline after the last one lands on the bottom row and scrolls
    the pane by one — which puts row 1, the header carrying the whole answer to "is
    anything wrong", off the top until the next redraw, and pushes a dead frame into
    the scrollback every two seconds. The pane must scroll back through history, not
    through a flip-book of itself.

    With colour off there is no cursor control either: NO_COLOR and TERM=dumb are
    also the two cases where a pane is being piped somewhere that wants text.
    """
    theme = welcome.Theme.detect(os.environ) if theme is None else theme
    if not theme.reset:
        return "\n".join(lines) + "\n"
    return "\x1b[H" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J"


# --- the cheat sheet ----------------------------------------------------------


@dataclass(frozen=True)
class Key:
    """One advertised binding, and the text `container/tmux.conf` must contain.

    The screen that teaches the keys and the file that implements them disagreeing
    is the ordinary way a cockpit becomes a liability, so `probe` is asserted
    against the real config by `test_cockpit`. Import KEYS to print them; do not
    restate them.
    """

    keys: str
    what: str
    probe: str


#: Most useful first, because that is the order they are *shed* in: pane 0 of the
#: cockpit is about thirteen rows and the tail of this list is what a short pane
#: drops. `C-b ?` is last on purpose — it is the one that says where the rest went.
KEYS = (
    Key("F1 F2 F3", "jump to prompt / shell / dashboard", "bind -n F1 select-pane"),
    Key("F4", "the manifest — what this machine is made of", "bind -n F4 new-window"),
    Key("mouse", "click a pane to focus it, scroll to scroll back", "set -g mouse on"),
    Key("C-b d", "detach; the machine keeps working", "bind d detach-client"),
    Key("C-b z", "zoom this pane", "bind z resize-pane -Z"),
    Key("M-arrows", "move between panes, no prefix", "bind -n M-Left select-pane -L"),
    Key("C-b |", "split vertical", "bind | split-window -h"),
    Key("C-b -", "split horizontal", "bind - split-window -v"),
    Key("C-b r", "reload the tmux config", "bind r source-file"),
    Key("C-b ?", "every binding, tmux's own list", "bind ? list-keys"),
)

PANES = (
    ("0", "this prompt — always here, nothing steals it"),
    ("1", "a root shell, for watching a build"),
    ("2", "the dashboard: agents, health, journal"),
)


def keys_text(width: int | None = None, height: int | None = None) -> str:
    """The cockpit's keys, as pane 0 prints them before the prompt.

    `height` is the *pane's*, and honouring it is the whole point: pane 0 is about
    thirteen rows, so a sheet longer than that scrolls its own top away — the pane
    map, the part you cannot guess — and leaves the human looking at the middle of a
    key list. Key rows are shed from the end (KEYS is ordered for it) and the last
    line says how many went and where they are. None means "print all of it".
    """
    theme = welcome.Theme.detect(os.environ)
    glyphs = welcome.glyphs_for(sys.stdout)
    size = shutil.get_terminal_size((80, 24))
    total = max(MIN_WIDTH, size.columns if width is None else width)
    body = total - 2
    rows = [_heading("panes", "", body)]
    rows += [
        _row((f"{index}  ", "accent"), (plain(what, body - 3), "faint")) for index, what in PANES
    ]
    rows += [_row(), _heading("keys", "", body)]
    keys = [
        _row((key.keys.ljust(10), "accent"), (plain(key.what, body - 10), "faint"))
        for key in KEYS
    ]
    if height is not None and len(rows) + len(keys) > height:
        room = max(1, height - len(rows) - 1)
        shed = len(keys) - room
        keys = keys[:room] + [
            _row((f"{glyphs.cut} {shed} more {glyphs.dot} C-b ? lists every binding", "faint"))
        ]
    return "\n".join(
        _paint(_clip(_indent(row, 2), total, glyphs.cut), theme) for row in rows + keys
    )


# --- entry points -------------------------------------------------------------

USAGE = "usage: python3 -m aios.dashboard status|watch [--once]|keys"


def watch(root: Path | str | None = None, interval: float = BEAT_S, once: bool = False) -> int:
    """The pure-reader pane: read, redraw, sleep. No model, ever.

    `aios.supervisor` is this loop plus a capped advisory line; this one is what
    runs when there are no credentials, or when you want a pane that cannot spend.
    """
    while True:
        state = read(root)
        try:
            sys.stdout.write(frame(watch_lines(state)))
            sys.stdout.flush()
        except (OSError, ValueError):
            return 0  # the pane went away; that is not an error
        if once:
            return 0
        try:
            time.sleep(max(0.2, interval))
        except KeyboardInterrupt:
            return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "status"
    if command == "status":
        try:
            print(status_line(read()))
        except Exception as exc:
            # Deliberately broad, and it prints rather than raises: this command's
            # stdout is the tmux status bar. A traceback there is unreadable, and a
            # status bar that went blank looks exactly like a healthy quiet machine.
            print(f"dashboard unavailable ({type(exc).__name__})")
        return 0
    if command == "watch":
        try:
            interval = float(os.environ.get("AIOS_WATCH_INTERVAL", "") or BEAT_S)
        except ValueError:
            interval = BEAT_S
        return watch(interval=interval, once="--once" in argv)
    if command == "keys":
        # Sized to the pane it is being printed into, less the blank line and the
        # prompt line the agent puts under it.
        print(keys_text(height=max(6, shutil.get_terminal_size((80, 24)).lines - 2)))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
