#!/bin/sh
# Build an AIos machine and boot it in the local kind cluster.
#
#   container/build.sh              build, load, deploy, wait, show the welcome screen
#   container/build.sh --image      build the image only
#   container/build.sh --deploy     load + deploy an already-built image
#
# Portable sh: no bashisms, no GNU-only flags.
set -eu

CLUSTER=aios
CONTEXT=kind-aios
IMAGE=aios:latest
NS=aios
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$here"

step() { printf '\n\033[38;5;117m==>\033[0m %s\n' "$*"; }

do_image=yes
do_deploy=yes
case "${1:-}" in
    --image)  do_deploy=no ;;
    --deploy) do_image=no ;;
    '') ;;
    *) echo "usage: $0 [--image|--deploy]" >&2; exit 2 ;;
esac

if [ "$do_image" = yes ]; then
    step "checking the userland before building"
    # The image build runs this too, but failing here costs seconds instead of
    # minutes and gives a readable error instead of a buildkit trace.
    # The same gate the image build and every release apply use, so "green here"
    # and "green in the target" cannot mean different sets of suites.
    python3 -m aios.update gate .

    step "building $IMAGE (linux/arm64)"
    docker build --platform linux/arm64 -f container/Dockerfile -t "$IMAGE" .
    docker images "$IMAGE" --format '    {{.Repository}}:{{.Tag}}  {{.Size}}'
fi

if [ "$do_deploy" = yes ]; then
    step "loading $IMAGE into the $CLUSTER node"
    # kind nodes have their own containerd; an image in the host daemon is
    # invisible to them until it is loaded, and imagePullPolicy:Never then fails
    # with ErrImageNeverPull rather than anything self-explanatory.
    kind load docker-image "$IMAGE" --name "$CLUSTER"

    step "restarting the machine onto the new image"
    # Delete before apply, and apply exactly once. A Pod is immutable except for
    # its image, so applying a changed pod spec over a running one fails with
    # "pod updates may not change fields other than spec.containers[*].image" —
    # and under `set -e` that aborts the whole script.
    kubectl --context "$CONTEXT" -n "$NS" delete pod aios --ignore-not-found --wait=true
    kubectl --context "$CONTEXT" apply -f container/k8s/aios.yaml

    step "waiting for the machine to boot"
    kubectl --context "$CONTEXT" -n "$NS" wait --for=condition=Ready pod/aios --timeout=180s

    step "welcome screen as rendered at boot"
    printf '\n'
    kubectl --context "$CONTEXT" -n "$NS" logs aios
    printf '\n\033[2m    attach a session with:  kubectl exec -it -n %s aios -- aios\033[0m\n' "$NS"
fi
