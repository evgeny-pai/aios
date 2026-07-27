---
name: ephemeral-port-exhaustion
description: Use on "dial tcp 127.0.0.1:PORT: connect: can't assign requested address" from kubectl, curl, or any client after many short-lived local connections. The server is fine; the client ran out of source ports.
---

# "can't assign requested address" is the client, not the server

**Failed:** three times across a long session, a batch of `kubectl` calls died mid-sequence:

```
Unable to connect to the server: dial tcp 127.0.0.1:50964: connect: can't assign requested address
```

Every time it looked like the cluster had gone away. It had not — the very next attempt seconds later succeeded, and the pod had been `Running` throughout. One casualty was a `kubectl cp` that silently didn't happen, so a later step read a stale file and I misdiagnosed my own code.

**Why:** `EADDRNOTAVAIL` on *connect* means the kernel could not allocate a source port. Hundreds of short-lived connections to the same `127.0.0.1:port` leave sockets in `TIME_WAIT`, and the local ephemeral range for that exact 4-tuple fills. It is a client-side resource limit and it clears on its own in tens of seconds. Distinguishing it from a real outage matters: `connection refused` means nothing is listening, `no route to host` is networking, **`can't assign requested address` means you are the problem.**

**Fix:** retry with backoff, and stop treating each call as independent.

```sh
k() { for i in 1 2 3 4 5; do kubectl -n "$NS" "$@" && return 0; sleep 5; done; return 1; }
k apply -f manifests.yaml
```

Better, reduce the churn: batch many `exec` calls into one `sh -c` doing all the work, prefer a long-lived `port-forward` over repeated `exec`, and never put a bare `kubectl` in a tight loop.

Check the pressure directly: `netstat -an | grep -c <port>`.

**Verify:** the same command succeeding on retry with no change to the cluster is the diagnosis — and confirm nothing was silently skipped, especially file copies.
