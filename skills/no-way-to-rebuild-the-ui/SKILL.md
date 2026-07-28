---
name: no-way-to-rebuild-the-ui
description: Use when building a multi-pane, multi-window or multi-panel layout a user can close parts of — tmux, a dashboard, an IDE workspace. If the setup path only runs at creation, closing a piece strands the user with no way back.
---

# A layout you can dismantle but not rebuild is a trap

**Failed:** a three-pane tmux cockpit — agent prompt, shell, dashboard. The user closed two panes by accident and was left with only the dashboard:

```
aios:0.0  dashboard  69x35  bash
```

No prompt, no shell, and nothing to run to get them back. The login script built the layout *once*, at session creation, and on every subsequent login did:

```sh
if tmux has-session -t "$SESSION"; then
    exec tmux attach-session -t "$SESSION"   # attach to whatever is left, however broken
fi
```

So reconnecting reattached to the wreckage. The only routes out were killing the server — losing the session and anything running in it — or an operator rebuilding the panes by hand from outside.

**Why:** setup and repair look like the same job, so only setup gets written. The branch that runs afterwards checks *existence* ("is there a session?") rather than *shape* ("does it have its three panes?"), and a partially-destroyed layout satisfies existence.

**Fix:** make the builder idempotent, run it on every attach, and bind it to a key.

```sh
panes=$(tmux list-panes -t "$WINDOW" | wc -l)
[ "$panes" -ge 3 ] && { echo "cockpit intact"; exit 0; }   # no-op when healthy
# ...create only what is missing...
```

```
bind R run-shell "aios-cockpit" \; display " cockpit rebuilt"
```

Two properties that make it safe to run at any moment:

- **Additive only.** Never kill a pane and never touch what is running in one — the pane the user did *not* close may be mid-build.
- **Position-aware.** Insert relative to what survived (`split-window -b` here) rather than assuming indices; tmux renumbers by position, and the survivor may be any role.

Panes inherit nothing, so each recreated one must carry its own environment. A pane that comes back without `PYTHONPATH` looks alive and is useless.

**Verify:** the no-op case proves nothing. Build a deliberately broken session — one pane — run the repair, and confirm the full layout returns; then run it again and confirm it changes nothing. Do it on a throwaway session, not the user's.
