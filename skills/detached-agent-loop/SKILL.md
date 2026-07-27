---
name: detached-agent-loop
description: Use before putting an agent loop in a tmux/screen pane, a systemd unit, a nohup, or any detached process. A loop designed never to quit until verified has no brake when nobody is reading it.
---

# Never-quit plus detached equals unbounded

**Failed:** the login path opened a tmux session with the agent in pane 0, so a long build could not own the terminal. The agent had just been reworked so that a red verification verdict re-enters the loop instead of returning — deliberately, because it kept giving up with work undone.

Both changes were right. Together they were not: the session outlived every client, the probe for an unbuilt package stayed red, and the loop kept spending API calls into a pane with no human in front of it. Three stale `attach-session` clients were still lingering when it was killed.

```
39008  26:09  tmux new-session -d -s aios ... exec python3 -m aios.agent
39035  10:53  tmux attach-session -t aios
39070  09:31  tmux attach-session -t aios
```

**Why:** "keep going until verified" is safe only while a person can see it and stop it. Detaching removes the reader *and* the brake at once, and the property that made the loop good — refusing to stop on red — is exactly what makes it expensive unattended. Neither change looks dangerous in review; the interaction is the defect.

**Fix:** the brake must be in the loop, not in the person watching it.

- Bound it in the loop itself: step budget, wall-clock budget, and a
  consecutive-identical-verdict counter that breaks out rather than retrying forever.
- Make the detached case a different mode: an unattended run gets a hard ceiling and
  exits with a report; only an attended session gets "keep trying".
- Default to attached. Here the split became opt-in (`AIOS_SPLIT=1`) and the prompt
  went back on the terminal you typed into.

Kill a runaway multiplexer with `tmux kill-server` — it takes every pane and client with it, and unlike `pkill -f tmux` it cannot match its own command line (see [pkill-self-match](../pkill-self-match/SKILL.md)).

**Verify:** start the loop detached with a condition it cannot satisfy, wait, and count the API calls or log lines it produced. If the number keeps climbing, the brake is the human.
