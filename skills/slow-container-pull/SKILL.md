---
name: slow-container-pull
description: Use when a docker pull looks stalled or shows no progress, or before switching to a "smaller/faster" mirror or base image to speed one up.
---

# Measure before you switch mirrors

**Failed:** `docker pull gentoo/stage3:... | tail -5 &` → log file **0 bytes** after 12 minutes; a second pull printed nothing for 25s; `docker system df` flat. Looked dead, so it was killed. It was ~85% done.

Then the "obvious" recovery — Gentoo's own mirror ships the same rootfs as 397 MB xz vs a 635 MB gzip layer, so fewer bytes had to be faster. Measured:

```
docker hub CDN: 0.74 MB/s -> 635 MB ETA  14 min
gentoo mirror:  0.06 MB/s -> 397 MB ETA 116 min   (8x slower per byte)
```

**Why:** three things hid progress at once — `| tail` can't emit until the pull *finishes*; the image is **one 635 MB layer**, so there are no per-layer completion events; and a second concurrent pull is deduplicated against the first, so its silence means the first is *working*.

**Fix:** never pipe a pull you intend to watch; compare ETAs, not sizes.

```sh
docker pull <image> 2>&1 | tr '\r' '\n' > pull.log &   # tr is line-oriented
curl -sL --max-time 8 -o /dev/null -w "%{speed_download} B/s\n" <candidate-url>
docker pull hello-world                                 # isolates "daemon broken" from "image slow"
```

Layer count and size come from the registry manifest without downloading. One layer means expect silence. Evidence of death is a flat byte counter over time, not absent log output.

macOS has no `timeout`: use `cmd > log 2>&1 & P=$!; sleep 25; tail log; kill $P`.

**Verify:** `docker images <repo> --format '{{.Tag}} {{.Size}}'`; for progress, sample `df -k` on the image store 15s apart.
