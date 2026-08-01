---
name: docker-compose-plugin-unregistered
description: Use when `docker compose` fails with "docker: unknown command: docker compose" or "unknown flag: --env-file" even though docker itself works.
---

# `docker compose` is missing while the compose binary is installed

**Failed:** `./run.sh start`, which calls `docker compose --env-file .env -f compose.yml up`
→ `unknown flag: --env-file`, and directly:
`docker: unknown command: docker compose`

**Why:** the compose plugin exists at `/opt/homebrew/lib/docker/cli-plugins/docker-compose`
but the running docker CLI does not scan that directory, so the subcommand is absent and
docker parses `--env-file` as one of its own global flags.

**Fix:** use the standalone binary rather than mutating the user's docker config.

```bash
if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
else
  COMPOSE_CMD=(docker-compose)   # /opt/homebrew/bin/docker-compose
fi
"${COMPOSE_CMD[@]}" --env-file .env -f compose.yml up -d --wait
```

**Verify:** `docker-compose version` prints a version where `docker compose version` fails.
