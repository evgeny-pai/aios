# AIos — design

Target: `aarch64`, musl, no systemd. A running AIos machine can rebuild itself.

## 1. Why Gentoo underneath

Portage already provides source-based packaging, per-package feature toggles
(USE), profiles, binary-package caching, and stage/image building (catalyst).
Rewriting that is a multi-year detour that buys nothing — the novel work is
entirely in the *lowering pass* (intent → build configuration) and in
*minimization* (removing features no probe exercises).

So AIos is: a Gentoo profile + a local overlay + `forge`. The distro is a
substrate, not the product.

## 2. The pipeline, and where determinism lives

```
aios.toml ──▶ [ agent ] ──▶ aios.lock.json ──▶ [ pure ] ──▶ /etc/portage ──▶ [ emerge ] ──▶ root
   human      nondeterministic    reviewable       deterministic                deterministic
```

The lockfile is the trust boundary. It is canonically serialized (sorted keys,
2-space indent) and content-digested, so:

- the same spec + same lock digest ⇒ byte-identical portage state
- a model re-run shows up as `git diff`, reviewed like any other change
- a build failure is debuggable, because nothing between the lock and the
  filesystem involves a model

`forge diff` fails loudly if `aios.lock.json` was hand-edited without
recomputing its digest, or if the spec has drifted from the lock it produced.

### Spec wins over the agent

`CFLAGS`, `CXXFLAGS`, `MAKEOPTS`, `CHOST`, `ACCEPT_KEYWORDS` are spec-owned. If
the lowering pass proposes a value for one of them it is discarded and recorded
in `lock.notes`. The agent gets to decide *packages and features*, not the
machine's identity.

## 3. Probes: the thing that makes minimization safe

A probe is a small set of shell checks that assert a capability the user
actually depends on:

```toml
name = "vim"

[[check]]
name = "edits a file non-interactively"
script = '''
printf 'alpha\nbeta\n' > "$T/f.txt"
vim -es -c '2d' -c 'wq' "$T/f.txt"
grep -qx alpha "$T/f.txt" && ! grep -qx beta "$T/f.txt"
'''
```

Probes run in a scratch dir (`$T`) under `bash -e`. They are the minimizer's
only definition of "still works," which means:

- an intent with no probe can be lowered but never minimized
- the probe suite is the regression suite for the *machine*, not for a package

Upstream test suites are the wrong guardrail here — they assert that every
feature works, which is precisely the opposite of the goal.

## 4. Minimization as search

```
levers   = enabled USE flags for the atom, then EXTRA_ECONF flags from the
           forge recipe, then patches
objective= installed size (bytes) of the package's own files, secondary:
           dynamic-link count, build time
constraint= all probes bound to the atom pass
```

Greedy, deterministic, one lever at a time in sorted order, journaled to
`forge.journal.jsonl` (one JSON object per attempt: lever, verdict, size delta,
duration). Greedy is the right start — it's O(n) rebuilds, resumable, and every
step is independently explainable. Delta-debugging over lever *subsets* is a
later optimization and only pays off once builds are cached.

Each attempt is a real build. That is the cost of the approach and the reason
binary-package caching (`FEATURES=buildpkg`, a local binhost) is a phase-1
requirement rather than a nicety.

## 5. Self-modification: A/B roots

The target rebuilds itself, so the root filesystem must never be the thing
being mutated in place.

```
/dev/disk (GPT)
├── esp        FAT32   — bootloader + two kernel/initramfs pairs
├── slot-a     ext4    — root, mounted / (active)
├── slot-b     ext4    — root, staged target for the next generation
└── data       ext4    — /var, /home, /aios  (survives both slots, never rebuilt)
```

`/aios` on the data partition holds the machine's own source of truth:
`aios.toml`, `aios.lock.json`, `overlay/`, `probes/`, `forge.journal.jsonl`, and
the binary-package cache. It is a git repo. Every generation is a commit.

### The self-modify cycle

1. **Intend** — user edits `/aios/aios.toml`, or asks the on-target agent to,
   which edits it as a proposal and shows the diff.
2. **Lower** — `forge lower` writes a new lock. Diff reviewed (or auto-approved
   under a policy the spec declares).
3. **Build into the inactive slot** — `emerge --root=/mnt/slot-b` against the
   new lock. The running system is untouched; a failed build costs nothing but
   time and disk.
4. **Probe the new slot** — probes run against `slot-b` via chroot (fast, most
   coverage) and then in a QEMU boot of the slot's kernel (catches init, kernel
   config, and device-node problems chroot cannot).
5. **Promote** — write the bootloader's `next` entry to `slot-b`, set a
   *one-shot* boot flag plus a boot-counter, reboot.
6. **Confirm** — if the new slot reaches a healthy target and the probes pass
   post-boot, `forge confirm` makes the entry permanent. Otherwise the
   bootloader's counter expires and the next reset falls back to `slot-a`
   automatically. An unbootable generation is self-healing, not a rescue-USB
   event.

Rollback is therefore just "boot the other slot" — the previous generation's
files were never modified.

### Where the model runs

| Phase | Agent location | Why |
|---|---|---|
| 0 (now) | build host (this Mac / a Linux VM), hosted API | fastest iteration; target isn't bootable yet |
| 1 | build host, cross-building into slot images | target boots but has no toolchain for the agent |
| 2 | on-target, hosted API via `openai`/`anthropic` provider | machine edits itself with network |
| 3 | on-target, local weights via an OpenAI-compatible server | no network dependency at all |

The provider layer exists so phases 2 and 3 differ by one environment variable.
`forge` itself is Python-stdlib-only (portage already requires Python, so this
adds nothing to the target) — no pip, no vendored dependency tree to build for
musl/aarch64.

**Trust boundary on-target:** the agent proposes; it never promotes. Step 5
requires either an interactive confirmation or an explicit policy in the spec,
and every promotion is a signed commit in `/aios`. A model that can edit the
spec but cannot promote a generation cannot brick the machine.

## 6. Roadmap

**Phase 0 — pipeline** *(here)*
- [x] spec → lock → portage rendering, canonical + digested
- [x] provider layer (anthropic / openai-compatible / ollama / offline echo)
- [x] probe runner
- [x] minimization loop against a dry runner
- [ ] real `LocalRunner`: emerge into a root, size from portage's `CONTENTS`

**Phase 1 — first real build**
- [ ] aarch64 musl stage3 in a Linux VM (lima/UTM) on the Mac
- [ ] `aios` profile + local overlay skeleton in `overlay/`
- [ ] binary package cache / local binhost (minimization is unaffordable without it)
- [ ] minimize `vim` for real; publish the size delta and the dropped levers

**Phase 2 — bootable**
- [ ] kernel config lowered from the spec too (the biggest single minimization win)
- [ ] dinit service set generated from intent
- [ ] A/B partition layout + bootloader `next`/counter plumbing
- [ ] QEMU smoke-boot in the probe path

**Phase 3 — self-hosting**
- [ ] `forge` and its probes shipped in the image
- [ ] on-target `forge lower` → build into inactive slot → promote → confirm
- [ ] local-weights provider on-device
- [ ] `x86_64` port (mechanical once `CHOST`/profile derivation is exercised twice)

## 7. Per-host binary diversity: measured, and declined for now

The question is whether each node should get a *slightly different* binary of the
same package — randomised layout, so one exploit does not work everywhere. Measured
on this machine's actual toolchain (gcc 15.3.0, GNU ld 2.46.0, aarch64/musl):

| Lever | State here | What it actually buys |
|---|---|---|
| PIE / ASLR | **already on** — `-fPIE [enabled]`, `pie` in gcc's specs | Randomises addresses per *execution*, which is strictly stronger than per-host-but-fixed layout. This is the defence that matters, and it is free. |
| `-frandom-seed=<per-host>` | accepted by gcc | Changes internal symbol/hash naming. Object files differ; code layout does not meaningfully move. It exists for *reproducible* builds, not diversity — weak as a security measure. |
| Section shuffling | **not available** — GNU ld 2.46 has no `--shuffle-sections` | Real layout diversity needs LLVM's `lld --shuffle-sections=<seed>`, so it would mean `sys-devel/lld` and a linker switch. |
| `-ffunction-sections -fdata-sections` + custom link order | possible | Genuine reordering, but with GNU ld it means generating a linker script per host. |

The reason it is declined is not difficulty, it is that **diversity and reuse are
the same axis pointed in opposite directions**:

- A per-host-unique binary cannot be shared, so the binhost (§ the network) stops
  saving anything — every node compiles everything, forever.
- It breaks the guarantee the lockfile exists to make. "Same spec plus same digest
  produces the same system" is what makes a build reviewable and a failure
  debuggable. Randomising the output makes two nodes with an identical lock
  *provably different*, and there is then no artifact that says what a node is.

So the honest position: ASLR already covers the threat, and paying for static
diversity with the lockfile's reproducibility is a bad trade at this stage. If it is
ever wanted, the shape is a spec field — `system.diversity = "seed"` — because it is
a property of one machine, and a node that sets it must also stop consuming binary
packages, since by construction none of them can fit it.

## 8. Making the rebuild loop cheap

`forge minimize` rebuilds one package once per lever. Everything that makes that
cheaper is load-bearing, not an optimisation:

- **`--oneshot`, no `--deep`** for the minimizer's single-atom rebuilds. `--deep`
  re-resolved the whole dependency graph on every lever; `--changed-use` alone
  catches the one flag that moved, and `--oneshot` stops a measurement run from
  rewriting `@world` dozens of times as a side effect.
- **`buildpkg`, spec-owned.** Every build leaves a binary package behind, which is
  both the peer cache and the local one. A model cannot turn this off.
- **A compiler cache** (`dev-util/ccache`), which is what an intent now asks for:
  a dozen near-identical compiles of one package is exactly its best case.
- **Binary packages from a peer**, with `--binpkg-respect-use=y` so a prebuilt
  package whose flags disagree with the lock is *rejected and rebuilt* rather than
  installed. Reuse may save time; it may not change what you get.
- Not yet done, in rough order of payoff: `PORTAGE_TMPDIR` on tmpfs (this pod's
  `/dev/shm` is 64 MB, so it needs a sized memory-backed volume and a memory limit
  raised to match — an OOM mid-build is worse than a slow build), and `distcc`
  across the network, which is the obvious use of more than one node.

## 9. Open questions

- **Kernel minimization objective.** Size is the obvious metric, but attack
  surface (enabled syscalls, loadable modules) is the interesting one and needs
  a probe vocabulary that doesn't exist yet.
- **Probe coverage measurement.** Nothing currently tells you a probe suite is
  too thin until a minimized package breaks in a way probes missed. Mutation
  testing over levers — deliberately break a feature a probe claims to cover and
  assert the probe fails — is the plausible answer.
- **Cross-package minimization.** Dropping a USE flag from a library invalidates
  consumers. Currently out of scope; needs a reverse-dependency-aware pass.
- **Reproducibility.** Portage is not bit-reproducible out of the box. Worth
  aiming at, but not before Phase 2.
