---
name: k8s-apply-destroys-state
description: Use when a Secret value reverts to empty, when a manifest declares a Secret holding user-supplied credentials, when an env var from a Secret is stale in a running pod, or on "pod updates may not change fields other than spec.containers[*].image".
---

# A manifest that declares a Secret destroys the value in it

**Failed:** the manifest shipped `stringData: { ANTHROPIC_API_KEY: "" }` as a "placeholder". The user created the real key out-of-band (verified, 108 bytes), then a routine `kubectl apply -f aios.yaml` →

```
secret bytes now: 0
pod sees an EMPTY key
```

Exit 0, no warning, no diff. The symptom looked like a credential-detection bug in the app. `build.sh` ran the same apply, so **every rebuild wiped the key**.

**Why:** `apply` is declarative — an empty declared value is an instruction to make it empty, not a placeholder. Two adjacent facts complete the trap: `envFrom` is copied at **container start** (a Secret edit needs a pod restart, which is what tempts you into the destructive apply), and pods are immutable except `image`, so applying a changed pod spec fails and aborts a `set -e` script.

**Fix:** don't declare a Secret you don't own the value of; mark the reference optional so the pod still boots.

```yaml
envFrom:
  - secretRef:
      name: aios-agent
      optional: true
```

```sh
kubectl -n aios create secret generic aios-agent --from-literal=ANTHROPIC_API_KEY=...
kubectl -n aios delete pod aios --wait=true && kubectl apply -f manifests.yaml   # delete first, apply once
```

**Verify:** create a *dummy* value, `apply`, compare byte counts before and after — equal means the manifest no longer owns it. Check lengths, never print values.
