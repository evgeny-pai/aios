---
name: workflow-interrupt-recovery
description: Use when a background Workflow or subagent produces no files, seems stalled, or returns null — especially after the user sends a message mid-turn, or after a session restart.
---

# A mid-turn message kills in-flight background agents

**Failed:** a background workflow ran 15 minutes and produced nothing. No error, no notification. Transcripts looked healthy (~100 KB, agents visibly reading files); the last line of every one was `[Request interrupted by user]` — all at the same second the user sent a follow-up while the turn was running.

**Why:** a mid-turn message interrupts the turn, and the interrupt propagates to every agent it spawned. Correct behaviour, invisible from the parent: `journal.jsonl` still shows them as `started` with no terminal event. **Resume caching doesn't help** — only *completed* `agent()` calls are cached, and interrupted ones completed nothing.

**Fix:** diagnose from the transcripts, then relaunch with the run id (the script was persisted at launch).

```bash
D=~/.claude/projects/<project>/<session>/subagents/workflows/<runId>
tail -3 "$D"/agent-*.jsonl     # look for [Request interrupted by user]
```

```
Workflow({ scriptPath: "<path from the original result>", resumeFromRunId: "<runId>" })
```

Say that work was lost — a workflow that produced nothing looks identical to one that's slow. Prefer many small agents that write to disk as they go over few long ones that only return strings.

**Verify:** transcript mtimes advancing, and the expected output files appearing.
