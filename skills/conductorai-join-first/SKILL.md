---
name: conductorai-join-first
description: Use on "Not joined. Call the join tool first to register this agent." from a ConductorAI tool — including mid-session, after join already succeeded once.
---

# ConductorAI registration is per-connection, not per-session

**Failed:** `claim(...)` and `declare_intent(...)`, batched together long after a successful `join` → both returned `Not joined. Call the join tool first to register this agent.` A wasted round trip right before starting a shared VM.

**Why:** registration is tied to the live MCP connection, not the conversation. A server restart, a reconnect, or a resumed session drops the `agent_id` with no notification — the next call is simply rejected.

**Fix:** treat it as recoverable, not a failure. Re-`join`, then re-issue the call. Never batch `join` with calls that depend on it — parallel calls can't see a sibling's registration.

Order: `join` → `declare_intent` → `claim` (before touching shared infra) → `set_focus`/`remember` → `release(all=true)`.

Re-read what `join` returns; on a re-join it may contain state that appeared since the last one.

**Verify:** `status()` succeeds only when registered. A successful `claim` returns `{"granted": true}` — a *conflict* is a real coordination signal, not this problem.
