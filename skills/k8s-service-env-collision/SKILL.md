---
name: k8s-service-env-collision
description: Use when a pod's own env var has a value it never set — a `tcp://10.x.x.x:PORT` URL where a port or host was expected, or `ValueError: invalid literal for int()`. Kubernetes injects legacy Docker-link variables named after every Service in the namespace, and they silently overwrite yours.
---

# Your Service's name overwrites your env var

**Failed:** the repo server read its port from `AIOS_REPO_PORT` and died on startup:

```
ValueError: invalid literal for int() with base 10: 'tcp://10.96.210.93:8080'
```

Nothing set that. Kubernetes did: there is a Service named `aios-repo` in the namespace, and kubelet injects Docker-link-style variables for every Service a pod can see —

```
AIOS_REPO_PORT=tcp://10.96.210.93:8080
AIOS_REPO_SERVICE_HOST=10.96.210.93
AIOS_REPO_SERVICE_PORT=8080
AIOS_REPO_PORT_8080_TCP_PORT=8080
```

The Service publishing this very server had taken the variable the server used to configure itself. The name collided with *itself*.

**Why:** the injected name is the Service name upper-cased with `-` → `_`, so `aios-repo` owns the whole `AIOS_REPO_*` namespace. Any variable of yours under that prefix is silently replaced, and `_PORT` is the trap because it looks like a port and is a URL. It only appears in-cluster, so it never reproduces locally, and injection happens at container start — a Service created later poisons the *next* restart, not this one, which decouples cause from symptom.

**Fix:** don't share a prefix with any Service name.

```python
PORT_ENV = "AIOS_SERVE_PORT"          # not AIOS_REPO_PORT — Service `aios-repo` owns that

def _port(default: int = 8080) -> int:
    raw = (os.environ.get(PORT_ENV) or "").strip()
    if not raw:
        return default
    if "://" in raw:                  # tcp://10.96.210.93:8080 — take the port
        raw = raw.rsplit(":", 1)[-1]
    try:
        return int(raw)
    except ValueError:
        return default                # say so on stderr; never crash on a hostile env
```

Rename yours, not the Service: the Service name is the API peers dial. Parse defensively anyway — the collision can come back through a Service you did not create. `enableServiceLinks: false` in the pod spec disables the injection wholesale, but it also removes it for every other Service, so prefer a non-colliding name.

**Verify:** ask the running pod, not your laptop:

```sh
kubectl exec POD -- printenv | grep -E '^<YOUR_PREFIX>_'
```

Anything there you did not put in the manifest is injected. Cross-check your own variable names against `kubectl get svc -o name`.
