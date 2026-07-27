# AIos skills

One skill per mistake that cost real time, written the moment it was fixed.

Kept **laconic** on purpose: the verbatim error, one sentence of cause, the fix,
one verify command. A skill is read by someone mid-failure searching for their
error text — length buries the fix.

| Skill | The mistake it prevents |
|---|---|
| [gentoo-portage-keywords](gentoo-portage-keywords/SKILL.md) | `ACCEPT_KEYWORDS="aarch64"` — the keyword is `arm64`, and the wrong one hides every package |
| [portage-overlay-config](portage-overlay-config/SKILL.md) | Deprecated `PORTDIR_OVERLAY` instead of `repos.conf`, warning on every emerge |
| [python-in-shell-quoting](python-in-shell-quoting/SKILL.md) | Escaping quotes that were already literal → `SyntaxError: unexpected character after line continuation character` |
| [python-unittest-discovery](python-unittest-discovery/SKILL.md) | `ImportError: Start directory is not importable` |
| [negative-assertions](negative-assertions/SKILL.md) | `assertNotIn` matching the comment that explains the ban |
| [colima-kind-lifecycle](colima-kind-lifecycle/SKILL.md) | Starting colima resurrects every kind cluster and steals their host ports |
| [conductorai-join-first](conductorai-join-first/SKILL.md) | `Not joined. Call the join tool first` mid-session |
| [workflow-interrupt-recovery](workflow-interrupt-recovery/SKILL.md) | A mid-turn message silently kills in-flight background agents |
| [slow-container-pull](slow-container-pull/SKILL.md) | Killing a single-layer pull that looked stalled, then "optimising" onto a mirror 8x slower per byte |
| [subprocess-stdin-hang](subprocess-stdin-hang/SKILL.md) | `capture_output=True` leaves stdin inherited; `vim -es` blocks forever |
| [host-dependent-assertions](host-dependent-assertions/SKILL.md) | A test asserting "the real command succeeds" encoded a fact about one laptop |
| [k8s-apply-destroys-state](k8s-apply-destroys-state/SKILL.md) | A manifest declaring `KEY: ""` wipes the user's credential on every `apply` |
| [zsh-no-word-splitting](zsh-no-word-splitting/SKILL.md) | `$K` holding a command prefix works in bash, fails in zsh |
| [model-feature-gating](model-feature-gating/SKILL.md) | `effort` sent to every model → HTTP 400 "This model does not support the effort parameter" on haiku |
| [protect-generated-artifacts](protect-generated-artifacts/SKILL.md) | A blocked agent hand-patched the lockfile and hand-computed its sha256 to match |
| [env-shadows-config](env-shadows-config/SKILL.md) | `AIOS_PROVIDER` in the manifest made `aios.toml` silently read-only from inside the machine |
| [vacuous-probe-checks](vacuous-probe-checks/SKILL.md) | Two probe checks passed with the binary not installed — and the minimizer trusts green probes |
| [tool-budget-shorter-than-task](tool-budget-shorter-than-task/SKILL.md) | A 120s `run_shell` cap drove the agent to background, self-kill, and finally fabricate a fake repo |
| [pkill-self-match](pkill-self-match/SKILL.md) | `pkill -f emerge` matched its own command line and SIGTERMed the shell (exit 143) |
| [validate-dont-trust-existence](validate-dont-trust-existence/SKILL.md) | An idempotency guard skipped because a fabricated directory was non-empty |
| [ephemeral-port-exhaustion](ephemeral-port-exhaustion/SKILL.md) | `can't assign requested address` looked like a dead cluster; it was local source-port exhaustion |
| [kubectl-exec-needs-stdin](kubectl-exec-needs-stdin/SKILL.md) | A heredoc to `kubectl exec` ran nothing and printed nothing — no `-i`, so silence read as success |
| [detached-agent-loop](detached-agent-loop/SKILL.md) | A never-quit agent in a detached tmux pane kept spending on a red verdict nobody was reading |

## Adding one

Only for something that **actually failed and was then fixed** — a guess about what
might go wrong is documentation, not a skill. Keep the failing command and its
error verbatim; the next reader is searching for that text, not for prose.

```markdown
---
name: <kebab-case>
description: <when to use — include the literal error text>
---

# <one-line title>

**Failed:** `<command>` → `<verbatim error>`

**Why:** <one sentence>

**Fix:**
```<lang>
<corrected code>
```

**Verify:** `<one command>`
```
