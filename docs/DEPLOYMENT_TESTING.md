# Deployment testing (Docker) — part of the Definition of Done

Every issue/PR that changes runtime behaviour must pass a **Docker deployment
smoke test** before it is closed. Unit tests prove the logic; the deploy-test
proves the change still **builds into the shipped image and boots** — catching
packaging, dependency, migration, and entrypoint regressions that `pytest` can't.

This is tracked in GitHub issue **#6**.

## TL;DR

```bash
# from the repo root, in Git Bash (Windows) or any POSIX shell:
scripts/docker_smoke.sh            # build + boot + health-check + teardown
# or, on Windows PowerShell:
pwsh scripts/docker_smoke.ps1
```

Exit code `0` = PASS, `1` = FAIL. Paste the final `RESULT:` block (and the image
SHA / git commit) into the issue when you close it.

## What it does — and why it is safe to run alongside the live instance

The live trading instance runs on the host on `:5000` / `:8765` and holds the
broker session. The smoke test is deliberately **isolated** so it can run on the
same laptop without touching it:

| Concern | Isolation |
| --- | --- |
| Image | builds `openalgo:smoketest`, never the live `openalgo:latest` |
| Container name | `openalgo-smoketest` (compose uses the fixed `openalgo-web`) |
| Ports | `5055 -> 5000`, `8799 -> 8765` (live keeps `5000`/`8765`) |
| Credentials | a **generated throwaway `.env`** — random `APP_KEY`/`API_KEY_PEPPER`, `SIMPLIFIED_ENGINE_MODE=sandbox`, and **no broker credentials**, so the container never logs in to the broker and cannot disturb the live session's shared daily token |
| Data | no named volumes mounted — a **fresh `db/`** each run (also exercises first-boot DB init), removed on teardown |

> Even with the market closed, do **not** point the smoke test at the live
> `./.env`: a second broker login can invalidate the live session's token. The
> script never does this; if you run `docker compose up` by hand for a deeper
> test, swap in a throwaway `.env` first.

## The procedure (what the script automates)

1. **Pre-flight** — Docker daemon reachable; target ports free; remove any stale
   `openalgo-smoketest` container.
2. **Throwaway `.env`** — generated in a temp file (random secrets, sandbox mode,
   no broker), deleted on teardown.
3. **Build** — `docker build -t openalgo:smoketest .`
   (multi-stage: `uv sync` + frontend `npm run build` + chromium — **several
   minutes on a cold cache**).
4. **Boot** — `docker run -d` with the alt ports, throwaway `.env`, ephemeral DB.
5. **Health** — poll `GET /auth/check-setup` (the same endpoint the compose
   healthcheck uses) until `200`, up to `BOOT_TIMEOUT` (default 150s); also check
   `GET /`.
6. **Log scan** — fail if the boot logs contain a `Traceback`, `CRITICAL`, or
   `no such table` (fresh-DB init regression).
7. **Result + teardown** — print `PASS`/`FAIL`, then `docker rm -f` the container.

## Knobs (env vars)

| Var | Default | Purpose |
| --- | --- | --- |
| `BUILD` | `1` | `0` to skip the build and reuse an existing `openalgo:smoketest` |
| `KEEP` | `0` | `1` to leave the container running for manual poking |
| `HOST_HTTP_PORT` | `5055` | host port mapped to container `5000` |
| `HOST_WS_PORT` | `8799` | host port mapped to container `8765` |
| `BOOT_TIMEOUT` | `150` | seconds to wait for the health endpoint |
| `IMAGE` | `openalgo:smoketest` | image tag to build/run |

## When to run it

- Before closing any issue/PR that touches Python runtime code, dependencies
  (`pyproject.toml`/`uv.lock`), the Dockerfile, `start.sh`, or boot/scheduler
  wiring.
- Not required for pure docs, test-only, or strategy-parameter changes.

## CI (optional, future)

The repo already has `.github/workflows/ci-self-hosted.yml`. A follow-up can run
`scripts/docker_smoke.sh` there on PRs to `dev`/`main` so the deploy-test is
enforced automatically rather than by hand. Left out for now because the build is
heavy and the self-hosted runner's resource budget needs confirming.

## Manual compose alternative (deeper test)

For a fuller test (persistent volumes, the real entrypoint, healthcheck), use a
throwaway `.env` and override the ports/name to avoid the live instance:

```bash
cp .env /tmp/smoke.env        # then edit /tmp/smoke.env: dummy secrets, sandbox,
                              # remove broker creds
FLASK_PORT=5055 WEBSOCKET_PORT=8799 \
  docker compose -p openalgo-smoke up --build -d
curl -f http://127.0.0.1:5055/auth/check-setup
docker compose -p openalgo-smoke logs --tail 50
docker compose -p openalgo-smoke down -v
```

> Note: `docker-compose.yaml` hard-codes `container_name: openalgo-web` and mounts
> `./.env`; the `-p` project prefix does **not** override a fixed container name,
> so stop the live `openalgo-web` first if it is containerised, or prefer the
> `scripts/docker_smoke.sh` `docker run` path which has no such clash.
