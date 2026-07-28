---
name: monitor-writes-what-it-reads
description: Use when a watcher, supervisor or health check appends to the same log, table or journal it derives health from. Symptom - the status line says "ok" while the watcher's own entries say the thing is stuck, or a "has anything changed" gate keeps firing on the watcher's own writes.
---

# A monitor must not be able to read its own heartbeat

**Failed:** the dashboard derived liveness from the newest record in `.aios/agent.jsonl`. The supervisor journals its advisory lines to that same file. An agent stalled for eleven minutes; the supervisor diagnosed it three times, into the journal —

```
advice  agent is stuck in exploratory loops trying to understand how to implement …
advice  agent is stuck exploring codebase …; human should clarify scope
```

— and the status bar read:

```
⠙ agents 1 · haiku-4-5 · gen 7 · ok
```

**Why:** the watcher's writes were indistinguishable from the watched process's writes, so every diagnosis refreshed the clock that decides "recent activity". The more it had to say, the healthier the machine looked. The same shape hits the gate that spends money: if the watcher's own entry counts as "the log changed", it will talk to itself forever on an idle machine.

**Fix:** authorship, not kind — keying on `kind == "advice"` breaks the day the watcher writes a second kind of record.

```python
record = {"ts": ..., "kind": kind, **payload, "author": AUTHOR_AGENT}   # last: unforgeable
...
if not event.by_agent:
    continue                      # displayed and audited, never activity
```

Then make it two-way, or the diagnosis dies in a pane nobody reads: when the watcher says *stuck*, that has to reach the one line a human glances at. Have the writer flag it (`"stall": True`) rather than the reader parse prose, and let the next record from the watched process retire it — which is also what stops the two halves confirming each other.

Absent `author` means "written before the field existed": attribute by kind and keep rendering.

**Verify:** append only watcher records to the log and re-read. The clock, the freshness token and the "changed since last time" fingerprint must all be byte-identical to before the append.
