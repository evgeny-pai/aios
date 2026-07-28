---
name: set-focus-is-not-a-lock
description: Use before restarting, deleting or rebuilding shared local infrastructure — a kind cluster, a pod, a dev server, a VM. Announcing what you are doing reserves nothing. Only a claim blocks anyone, and an unregistered peer is invisible to both.
---

# Announcing your work is not the same as reserving it

**Failed:** I coordinated at the start of a session — registered, and wrote a detailed focus line:

```
set_focus: "BUILDING NODE #6 (in progress, this session) ... image rebuild + pod
            recreate deliberately deferred until the workflow lands"
```

Then ran `container/build.sh`, which does `kubectl delete pod aios` and `apply`. Another session was working in that pod. It was destroyed under them, twice.

The focus line described the destructive act in advance, in the right place, and stopped nothing. It is a note on a wall, not a lock on a door. The tools I needed and skipped were `declare_intent` (so a peer sees it) and `claim` (so a peer is *blocked*).

There was a visible symptom, missed at the time: the machine's generation counter jumped 5 → 7 when I expected 6. An extra boot I could not account for is another agent recreating the machine, and I explained it away as my own documentation instead.

**Why:** the announcement APIs and the locking APIs sit next to each other and both feel like "coordinating". Only one has teeth. And the failure is silent in both directions — the other session got no warning, and I got no conflict.

**Fix:** claim the resource, not the intention, and re-check immediately before acting because grants take seconds to converge.

```
claim(resource="kind-cluster:aios", reason="rebuilding the image and recreating the pod",
      ttl_seconds=3600)          # denied -> coordinate, do not proceed
status()                         # re-check right before the destructive step
...                              # delete/apply/kind load/colima restart
release(lease_id=...)
```

Claim before: any `delete`/`apply` on a shared pod, a cluster rebuild, `kind load`, a VM restart, a dev-server restart. Not after, and not "instead" of an announcement — do both.

**The half you do not control:** `status` showed `peers: []`. The other session never registered, so no amount of checking would have revealed it. Coordination protects you only when both sides participate, which means a clean `status` is *weak* evidence of safety, not proof. Before something irreversible, prefer evidence that does not depend on the other party cooperating: is the resource in use right now, are there live connections, is a process running in it.

**Verify:** ask whether the destructive command would have been *stopped*. If the only thing standing between it and a running peer is a string you wrote, nothing was protecting anything. Put state on volumes too, so the blast radius is survivable when coordination fails anyway — here PVCs are the only reason 32,695 ebuilds and 296 binpkgs came back.
