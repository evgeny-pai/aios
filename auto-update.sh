#!/usr/bin/env bash
# Auto-update: when origin/main moves, fast-forward this checkout and rebuild the node.
#
# Modelled on conductorai's auto-update.sh, deliberately — that mechanism works and is
# already understood on this fleet. Every failure path is a safe no-op: a dirty tree, a
# diverged local branch, being offline, or a non-main branch all skip this cycle and log
# why. It NEVER merges, rebases, force-updates, or discards your work.
#
# The difference from conductorai's, and the reason for the extra guards below: that one
# restarts a daemon, this one REBUILDS AN IMAGE AND RECREATES A POD. Recreating the pod
# takes the container's ephemeral layer with it — the machine reconstitutes from the
# binpkg cache at boot, but anything running inside dies. So it also refuses to act while
# a human is in the cockpit, or while another agent holds the cluster.
set -uo pipefail
cd "$(dirname "$0")"

# launchd hands a minimal PATH. On this fleet node/docker/kind/kubectl are Homebrew and
# `python3` is a ~/.local/bin symlink to brew python@3.13 — the CommandLineTools python
# is 3.9 and cannot import forge at all (no tomllib), so omitting ~/.local/bin makes the
# rebuild fail inside its own gate for a reason that looks nothing like PATH.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

LOG="$HOME/.aios/update.log"
mkdir -p "$HOME/.aios"
ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "$(ts) $*" >>"$LOG"; }

CONTEXT="${AIOS_KUBE_CONTEXT:-kind-aios}"
NS="${AIOS_NAMESPACE:-aios}"
DB="$HOME/.conductorai/conductorai.db"
LEASE="${AIOS_LEASE_NAME:-kind-cluster:aios}"

command -v git >/dev/null || { log "skip: git not found"; exit 0; }
command -v docker >/dev/null || { log "skip: docker not found (colima down?)"; exit 0; }

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || { log "skip: not a git checkout"; exit 0; }
[ "$branch" = "main" ] || { log "skip: on '$branch', not main"; exit 0; }

if ! git fetch --quiet origin main 2>>"$LOG"; then
  log "skip: fetch failed (offline or no SSH key in this session)"; exit 0
fi

local_rev=$(git rev-parse HEAD)
remote_rev=$(git rev-parse origin/main)
[ "$local_rev" = "$remote_rev" ] && exit 0            # already current — quiet no-op

# Refuse anything that is not a clean fast-forward.
if ! git merge-base --is-ancestor "$local_rev" "$remote_rev"; then
  log "skip: local diverged from origin/main (manual merge needed)"; exit 0
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  log "skip: uncommitted local changes — not clobbering them"; exit 0
fi

# --- Guard: is a human in the cockpit? ---------------------------------------------
# `tmux list-clients` non-empty means someone has `aios` open. Recreating the pod under
# them would kill their shell, their build and the agent mid-sentence. No server, no
# session, or an unreachable pod all mean "nobody is attached", which is the safe read.
attached=$(kubectl --context "$CONTEXT" -n "$NS" exec aios -- \
             tmux list-clients 2>/dev/null | grep -c . || true)
if [ "${attached:-0}" -gt 0 ]; then
  log "skip: $attached client(s) attached to the cockpit — not recreating the pod under them"
  exit 0
fi

# --- Guard: does another agent hold the cluster? ------------------------------------
# Read-only look at ConductorAI's bus. This script is not an agent and cannot claim, so
# the only honest behaviour is to stand down when anyone holds the lease — the machine
# is exactly the resource the post-mortem was written about. Absent DB or sqlite3 means
# no coordination is running, which is not a reason to refuse.
if [ -r "$DB" ] && command -v sqlite3 >/dev/null; then
  held=$(sqlite3 "$DB" "SELECT count(*) FROM leases
                        WHERE resource = '$LEASE'
                          AND released_at IS NULL
                          AND expires_at > strftime('%s','now')*1000;" 2>/dev/null || echo 0)
  if [ "${held:-0}" -gt 0 ]; then
    log "skip: $LEASE is leased by another agent"; exit 0
  fi
fi

# --- Is a rebuild even the right tool for this commit? -------------------------------
# aios-init runs `aios.update git-apply` in the pod every 300s, which promotes new code
# through the gate-then-swap path and — via aios.update.INSTALL — copies aios-init,
# aios-login, manual and tmux.conf back out to /sbin and /etc. So a code-only commit is
# already delivered, in place, with no downtime.
#
# What that CANNOT deliver is anything baked at image build or declared in the manifest:
# the Dockerfile (profile.d, the stage3 base, the layout of /aios) and container/k8s
# (env, volumes, Service ports). Those are the only reason to pay for a rebuild and a
# pod recreate. Rebuilding for a commit the in-pod updater handles would recreate the
# machine — killing whatever it was building — to deliver something it was going to
# receive anyway.
#
# So: fast-forward always (the checkout should track main either way), rebuild only when
# the diff touches something the pod cannot take by itself.
image_relevant=$(git diff --name-only "$local_rev" "$remote_rev" -- \
                   container/Dockerfile container/k8s 2>/dev/null)

log "updating ${local_rev:0:7} -> ${remote_rev:0:7}"
if ! git merge --ff-only --quiet origin/main 2>>"$LOG"; then
  log "skip: fast-forward failed"; exit 0
fi

if [ -z "$image_relevant" ]; then
  log "no rebuild: nothing outside the pod's own reach changed — the in-pod git-apply delivers this"
  exit 0
fi
log "rebuilding: $(echo "$image_relevant" | tr '\n' ' ')changed"

# container/build.sh runs the release gate before it builds anything, so a bad commit
# fails here rather than becoming the running machine.
if ./container/build.sh >>"$LOG" 2>&1; then
  log "updated to $(git rev-parse --short HEAD) and rebuilt the node"
else
  log "BUILD FAILED after updating to $(git rev-parse --short HEAD) — the pod may be on the old image"
fi
