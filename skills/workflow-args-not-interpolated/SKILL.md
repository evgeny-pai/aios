---
name: workflow-args-not-interpolated
description: Use when workflow subagents report a path or parameter as the literal string "undefined", or return zero findings claiming the source tree is missing.
---

# A workflow's `args` did not reach the script, and every agent got "undefined"

**Failed:** a workflow launched with `args: {"buzz": "/path/to/checkout"}` whose script
read `const BUZZ = args.buzz` → three of five subagents returned zero claims:
`the task gave the checkout path as the literal string 'undefined' (a failed variable
substitution in the orchestrator)`

**Why:** an undefined `args` field interpolates into a template literal as the text
`undefined` instead of failing, so the prompt is well-formed and the agents burn a full
run searching the filesystem for a path that cannot exist.

**Fix:** assert the inputs before spawning anything.

```js
const BUZZ = args?.buzz
if (!BUZZ) throw new Error('args.buzz is required: pass {"buzz": "<checkout path>"}')
log(`source: ${BUZZ}`)   // one line, so the value is visible in the run
```

**Verify:** `grep -c 'undefined' <transcriptDir>/journal.jsonl` — zero, and the first
`log()` line names a real path.
