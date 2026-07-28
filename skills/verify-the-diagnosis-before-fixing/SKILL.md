---
name: verify-the-diagnosis-before-fixing
description: Use when about to add documentation, a hint, or a capability because something "clearly didn't know about" an existing feature. Check that the thing you are about to add is actually absent — the most convincing diagnoses are the ones that explain the symptom without being true.
---

# The feature you are about to add may already be there

**Failed:** an in-box agent burned eleven minutes and fourteen tool calls exploring the codebase trying to work out how to integrate with the coordination mesh:

```
 8x  run_shell
 6x  read_file
advice  agent is stuck in exploratory loops trying to understand how to implement …
```

The diagnosis wrote itself: it does not know it already has a mesh client, so the fix is to tell it — put the capability in its briefing. Clean story, matched every symptom.

Then I ran the briefing it actually receives:

```
  BUILD MESH — MabrukV.local can build aarch64-unknown-linux-musl for you: 8 core(s)…
  Before a long compile, take its binaries instead:
    eval "$(python3 -m aios.mesh env MabrukV.local)"
```

It had been told, in the exact place I was about to add the telling, with the exact command it needed. `aios/mesh.py` was 22 KB on its own disk. The real problem was behaviour under an unscoped task, which is a different fix entirely — and the documentation I was about to write would have shipped, looked reasonable, and changed nothing.

**Why:** "X doesn't know about Y" explains almost any exploratory stall, costs nothing to believe, and suggests a fix that is easy and safe to write. Adding a hint is never *wrong*, which is what makes it dangerous: the change lands, the symptom persists, and now there is duplicated documentation drifting out of sync with the code.

**Fix:** before adding it, produce the artifact and look.

```sh
python3 -m aios.node          # what the briefing actually says, right now
ls -la aios/mesh.py           # does the capability already exist
grep -rn "mesh" aios/agent.py # is it already wired in
```

If the thing is present, the diagnosis is wrong and the real cause is still unfound. Ask instead: it had this — why did it not use it? Wrong place, wrong moment, too far from the point of use, or not a knowledge problem at all.

**Verify:** state the diagnosis as a falsifiable claim ("the briefing does not mention the mesh") and run the one command that would disprove it. A diagnosis you cannot disprove in a single command is a hypothesis you have not tested.
