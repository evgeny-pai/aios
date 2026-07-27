---
name: colima-kind-lifecycle
description: Use on "dial unix /var/run/docker.sock: connect: no such file or directory" on macOS, or before starting/stopping colima on a machine hosting kind clusters — starting the VM resurrects them and re-binds their host ports.
---

# Starting colima resurrects kind clusters you didn't ask for

**Failed (1):** every docker/kind command → `dial unix /var/run/docker.sock: ... no such file or directory`. No Docker Desktop here — the daemon is a **colima** VM, and it was stopped. `kubectl config get-contexts` still listed every cluster, which disguises it as a socket bug.

**Failed (2):** after `colima start`, a deliberately-archived cluster came back on its own and took ports 80/443, plus the CPU budget of a 4-CPU VM.

**Why:** `kind create cluster` sets `--restart=on-failure:1`, and dockerd restores containers by restart policy on boot. "A human stopped this on purpose" is not recorded anywhere the daemon can see — so *stopped* is not durable across a VM restart. Only `kind delete cluster` is.

**Fix:** diagnose with `colima list` (STATUS, not a docker bug), then take inventory immediately after starting:

```sh
colima start --cpu 4 --memory 12
docker ps --format '{{.Names}}\t{{.Ports}}'   # anything you didn't start is a resurrection
```

Design new clusters to sidestep it — no `extraPortMappings`, reach workloads via `kubectl exec`/`port-forward`, and any number coexist. Claim the VM before restarting it if others share the machine.

**Verify:** `docker ps --format '{{.Ports}}' | grep -E ':(80|443)->' || echo "no contested ports"`
