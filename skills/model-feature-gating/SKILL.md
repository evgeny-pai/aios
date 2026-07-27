---
name: model-feature-gating
description: Use on "This model does not support the effort parameter" or any HTTP 400 invalid_request_error naming a parameter, when sending output_config/effort/thinking to a Claude model, or when one model family works and another 400s on the same code path.
---

# Gate model-only parameters in, never out

**Failed:** the provider always sent `output_config.effort`. Against `claude-opus-5` it worked; the in-box agent ran `claude-haiku-4-5-20251001` and every lowering died:

```
HTTP 400 {"type":"invalid_request_error",
  "message":"This model does not support the effort parameter."}
```

The agent retried eight times, edited the spec to change models (which did nothing — see [k8s-apply-destroys-state](../k8s-apply-destroys-state/SKILL.md) for why env vars shadowed it), then gave up and hand-patched the generated lockfile.

**Why:** `effort` is a Claude 5 feature. Sending an unsupported parameter is a hard 400, not a warning — so a permissive default fails closed on every older or newer model. Bisected: `output_config.format` is accepted by haiku-4-5; only `effort` is rejected.

**Fix:** opt in by family. An unknown model silently loses the knob; that beats a request that cannot succeed.

```python
EFFORT_FAMILIES = ("opus-5", "sonnet-5", "fable-5")

def supports_effort(model: str) -> bool:
    return any(f in model for f in EFFORT_FAMILIES)

output_config = {"format": {"type": "json_schema", "schema": schema}}
if supports_effort(self.model):
    output_config["effort"] = self.effort
```

**Verify:** bisect against the real API — same payload with the parameter and without, per model family:

```python
call('bare', base); call('+format', ...); call('+format+effort', ...)
```
