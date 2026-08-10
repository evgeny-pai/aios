# AIos

A self-building Linux. You describe *what you use*; the system decides what to
compile. Nothing is installed because a distro maintainer thought you'd want it.

```
aios.toml  ──lower──▶  aios.lock.json  ──render──▶  /etc/portage/*  ──emerge──▶  system
 (intent)    (agent)      (artifact)      (pure)                        (pure)
```

Two tools:

- **`emerge`** — Gentoo's portage, unchanged. Builds what an ebuild describes.
- **`forge`** — this repo. Lowers intent into portage state, and minimizes
  packages down to the features your probes actually exercise.

## The two rules

**1. The AI is never in the build path.** Model output is nondeterministic;
builds are not. `forge lower` writes a *lockfile* — resolved atoms, USE flags,
compiler flags, each carrying the intent that justified it. Everything
downstream reads only the lockfile. Re-running the agent produces a reviewable
diff, not a different machine.

**2. Every flag carries a `why`.** Provenance back to the intent line is the
actual product — it's the tribal knowledge a Gentoo user accretes over years,
made explicit and auditable.

```jsonc
{ "flag": "syntax", "enabled": true,  "why": "intent[0]: syntax highlighting" }
{ "flag": "X",      "enabled": false, "why": "intent[0]: no X11 or clipboard" }
```

## `forge minimize` — the interesting half

Gentoo's USE flags only expose the feature switches an ebuild author chose to
expose. `forge minimize` goes past that: it treats feature removal as a search
problem with a measurable objective.

```
baseline: build → run probes → record size
loop:     drop one lever → rebuild → probes still green? smaller? → keep it
```

The levers are USE flags, then `EXTRA_ECONF` configure flags, then patches. The
constraint is *your* probes — executable checks derived from your intent, not
the upstream test suite. Test-driven package configuration: the objective is
binary size and linked surface; the guardrail is "the things I do still work."

This only works because the probes exist. An intent that can't be turned into a
check can't be minimized against.

## Quickstart

```bash
bin/forge init                      # write a starter aios.toml
bin/forge lower                     # intent -> aios.lock.json (needs a provider)
bin/forge show                      # what got decided, and why
bin/forge render --root ./out       # lockfile -> /etc/portage tree
bin/forge probe vim --dry-run       # run capability checks
bin/forge minimize app-editors/vim --dry-run
```

Everything except `lower` runs offline. `lower` needs a model:

```bash
export AIOS_PROVIDER=anthropic ANTHROPIC_API_KEY=...     # or an ant auth profile
export AIOS_PROVIDER=openai AIOS_OPENAI_BASE_URL=http://localhost:8080/v1 AIOS_MODEL=...
export AIOS_PROVIDER=ollama AIOS_MODEL=qwen3:32b
export AIOS_PROVIDER=echo                                # offline stub, for tests
```

`openai` speaks the OpenAI-compatible chat-completions shape, so llama.cpp's
server, vLLM, and anything else wearing that API work unmodified — which is how
the on-target agent will eventually run with no network at all.

## Running a machine

A real AIos node — Gentoo arm64/musl, root, no password — in the local kind
cluster:

```bash
container/build.sh
./auto-update.sh install              # optional: keep this host checkout + node current
                                      # auto-update tests v3 (SSH key auth confirmed working)
```

That builds the image, loads it into the `aios` cluster, applies the manifests,
waits for the pod, and prints the welcome screen as rendered at boot. Then:

```bash
kubectl exec -it -n aios aios -- aios
```

The cluster deliberately binds **no host ports** — it is reached through the API
server, so it coexists with any other kind cluster on the machine. The agent needs
credentials to do anything but read:

```bash
kubectl -n aios create secret generic aios-agent \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-... --dry-run=client -o yaml | kubectl apply -f -
kubectl -n aios delete pod aios && kubectl apply -f container/k8s/aios.yaml
```

Without them the machine still boots and everything except lowering works.

## Layout

| Path | What |
|---|---|
| `aios.toml` | the spec — system facts + intent, hand-written |
| `aios.lock.json` | the artifact — resolved, canonical, digested, committed |
| `forge/spec.py` | spec parsing and validation |
| `forge/lower.py` | intent → lock (the one nondeterministic step) |
| `forge/lock.py` | canonical serialization, digest, diff |
| `forge/portage.py` | lock → `make.conf`, `package.use`, sets |
| `forge/probe.py` | intent-derived capability checks |
| `forge/minimize.py` | the feature-minimization search |
| `forge/provider/` | pluggable model backends |
| `probes/` | probe definitions |
| `overlay/` | the local ebuild overlay `forge` authors into |
| `DESIGN.md` | boot layout, A/B roots, on-target self-modification |

## Status

Phase 0 — the spec → lock → portage pipeline and the minimization loop run
end-to-end against a dry runner. Nothing has been built on real hardware yet.
See `DESIGN.md` for what's next.
