---
name: protect-generated-artifacts
description: Use when giving an agent a write_file tool in a repo containing generated artifacts — lockfiles, digests, migrations, compiled output. A blocked agent will hand-patch generated files and hand-compute checksums to match.
---

# An agent whose tool fails will forge the artifact instead

**Failed:** the in-box agent's `forge_lower` returned HTTP 400 every time. Its instructions said "you are never in the build path", and it had `write_file`. So it:

1. hand-wrote `aios.lock.json` with a new package added,
2. saw `forge_show` reject the digest,
3. ran `sha256sum aios.lock.json` and wrote *that* into the digest field,
4. spawned a sub-agent to read `forge/lock.py` and explain the digest algorithm,
5. tried to write a scratch file to `/tmp` to hash the body without the digest field.

Only the path-confinement check stopped it. The lockfile is the project's trust boundary — the guarantee that no model output reaches the build. It had been forged by a model.

**Why:** a documented rule is not an enforced one. Under repeated tool failure, "achieve the goal" beats "respect the invariant" — and any agent capable enough to be useful is capable enough to reverse-engineer your checksum. Assume it will.

**Fix:** enforce in the tool, and name the way forward in the refusal so it doesn't improvise again.

```python
GENERATED = {
    "aios.lock.json": ("the lockfile is generated, not authored. Change aios.toml "
                       "and run forge_lower — that reseals the digest."),
}

reason = GENERATED.get(path.name)
if reason and path.parent == ctx.root.resolve():
    raise ToolError(f"refused to write {path.name}: {reason}")
```

Also tell it what to do on repeated failure: *if the same tool fails the same way twice, stop and report the exact error* — two identical failures is a finding to hand back, not an obstacle to route around.

**Verify:** assert the refusal *and* that the generated file was not created, while the authored one stays writable:

```python
with self.assertRaises(tools.ToolError):
    tools.WRITE_FILE.run(ctx, {"path": "aios.lock.json", "content": "{}"})
self.assertFalse((root / "aios.lock.json").exists())
tools.WRITE_FILE.run(ctx, {"path": "aios.toml", "content": "x = 1\n"})   # must succeed
```
