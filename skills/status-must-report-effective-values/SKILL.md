---
name: status-must-report-effective-values
description: Use when writing a status/info/doctor command, or when documentation sends a reader to one to debug a config problem. Printing the default instead of the value actually in force makes the command useless for the bug it exists to diagnose.
---

# A status command that prints constants cannot diagnose anything

**Failed:** `/status` reported the orchestrator model like this:

```python
row("orchestrator", llm.ORCHESTRATOR)      # a module constant
```

while the client that actually ran was built from the environment:

```python
llm.Client(model=os.environ.get("AIOS_MODEL", llm.ORCHESTRATOR))
```

Verified live: with `AIOS_MODEL=claude-opus-5 AIOS_PROVIDER=echo` set, `/status` printed `claude-haiku-4-5-20251001`. It had no provider row at all, and the lowering provider is resolved somewhere else entirely.

The sting: the manual's troubleshooting table sent readers to `/status` for the symptom *"an edit to aios.toml has no effect"* — whose cause is precisely those env vars. The one command offered to reveal the shadowing was the one command that could not see it.

**Why:** a status line is written next to the constant it names, not next to the resolution logic, so it drifts the moment an override is added. And it fails silently in the most convincing way possible — by printing a plausible value.

**Fix:** resolve the value the same way the real code does, and name the source that won.

```python
def sourced(env_var, fallback, fallback_label):
    override = os.environ.get(env_var)
    if override:
        return f"{override} ({env_var} overrides {fallback_label})"
    return f"{fallback} ({fallback_label})"
```

```
orchestrator    claude-opus-5 (AIOS_MODEL overrides built-in default)
lowering        echo:claude-opus-5 (AIOS_PROVIDER, AIOS_MODEL shadowing aios.toml)
```

Naming the source is the part that pays: it turns "why is my config ignored" from an investigation into a line of output. Related: [env-shadows-config](../env-shadows-config/SKILL.md), which prescribes exactly this and had not been applied to its own status command.

**Verify:** set every override to a value different from the default, run the status command, and confirm it reports the override *and* says where it came from. If it prints the default, it is decoration.
