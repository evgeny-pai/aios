---
name: env-shadows-config
description: Use when an edit to a config file has no effect, when an agent or user changes a setting and the old value persists, or when deciding precedence between env vars and a config file. An env var that overrides the config makes the config unchangeable from inside the running system.
---

# Env-over-config makes the config unchangeable

**Failed:** the provider resolved settings as `os.environ.get("AIOS_PROVIDER", spec.provider)` — env wins. The pod manifest helpfully set `AIOS_PROVIDER=anthropic` and `AIOS_MODEL=...`. So when the in-box agent edited `[agent] provider` in the spec to work around a failing backend:

```
· write_file path=/aios/aios.toml ...
· forge_lower dry_run=true
  It's still trying to use anthropic despite my change. Let me check if there's a cached config:
· list_dir path=/aios/.aios
```

It rewrote the spec three times, hunted for a cache that did not exist, and then abandoned the supported path entirely. The setting it was told is the source of truth was read-only in that deployment, and nothing said so.

**Why:** env-over-config is the right default for *operator* overrides injected from outside. It is the wrong default when something *inside* the system is supposed to be able to change the setting — the file becomes decorative, and the failure is silent because a write to it still succeeds.

**Fix:** don't inject the override unless you mean it. Remove it from the deployment and let the file govern; keep the env var available for a deliberate operator override, and say that where the reader will look.

```yaml
env:
  # Deliberately NO AIOS_PROVIDER / AIOS_MODEL. Those override aios.toml, which
  # made the spec unchangeable from inside the machine.
  - name: TERM
    value: xterm-256color
```

If both must coexist, make precedence visible at the point of use — log which source won (`provider from env` vs `provider from aios.toml`), so a config edit that cannot take effect says so instead of being ignored.

**Verify:** change the setting in the file, run the thing, and confirm the new value is used — `printenv | grep <PREFIX>_` first to see what the deployment is silently injecting.
