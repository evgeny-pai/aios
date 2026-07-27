---
name: live-state-diverges-from-repo
description: Use when iterating on a running container or VM by copying files in, or before building an image from a repo whose artifacts were last regenerated somewhere else. The deployable source can go stale while the live machine is correct, and the image ships the stale copy.
---

# Fixing the live machine does not fix the artifact you deploy

**Failed:** to iterate without restarting a long-lived pod, every change went in with `kubectl cp`. A generated artifact — the lockfile — was then regenerated *inside* the pod by a live model:

```
repo : {'model': 'fixture', 'provider': 'echo'}  sha256:12d584becb6e
#2   : {'model': 'claude-opus-5', ...}           sha256:53dccc35b8b3
```

The running machine had a real lowering. The repo — which is what `docker build` copies — still had the offline stub, whose own `notes` field says *"produced by the offline echo backend — not a real lowering"*. Every claim about the machine was true of the pod and false of the image. The next node built from that repo would have booted with the fixture, and nothing would have failed loudly.

Found by a reviewer checking a document against the code, not by any test.

**Why:** `cp` into a container is one-way, and iterating that way trains you to treat the live filesystem as the source of truth. It is the opposite: the repo is what deploys, the container is a cache of it. Generated artifacts make this worse, because the *right* copy is the one produced where the credentials and the real tree were — which is the container.

**Fix:** copy generated artifacts back, and verify the round trip before building anything.

```sh
kubectl cp ns/pod:/aios/aios.lock.json ./aios.lock.json
kubectl cp ns/pod:/aios/aios.toml /tmp/spec-from-pod.toml
diff aios.toml /tmp/spec-from-pod.toml     # a differing spec means the lock will not verify
python3 -m forge diff                     # "in sync" — the digest proves the pair matches
```

Two rules that would have caught it: for every file you `cp` in, know whether anything regenerates it in there; and make the build refuse an artifact that is obviously not real — a lockfile whose provider is the offline stub has no business in a shipped image.

**Verify:** `python3 -m forge diff` reports in-sync in the repo, and the artifact names the real generator:

```sh
python3 -m forge show | head -3        # "lowered by anthropic:claude-opus-5"
```
