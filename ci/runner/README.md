# OpenAlgo self-hosted CI runner (Docker-isolated)

> **Template directory.** These files are the canonical config for the L3 self-hosted runner,
> but the active runtime lives at `C:\actions-runner\` (not in the repo). This avoids
> overlapping disk I/O between git operations and the runner's working tree. Use
> `scripts/install-runner.ps1` to deploy these templates to `C:\actions-runner\`.

Everything needed to run a **GitHub Actions self-hosted runner** for
`sonawanedhiraj/openalgo` inside a hardened Docker container on the operator's
Windows production box. It backs the **L3-with-Docker-isolation** CI described
in [`docs/BRANCHING_AND_CI.md`](../../docs/BRANCHING_AND_CI.md).

The runner executes the jobs in
[`.github/workflows/ci-self-hosted.yml`](../../.github/workflows/ci-self-hosted.yml)
— gate, backend tests, lint, security scan.

## Why this is safe to run on the trading box

The container is deliberately blind to the host:

| The container CAN reach | The container CANNOT reach |
| --- | --- |
| GitHub API + Actions | Host filesystem (`db/`, `.env`, `log/`, `audit/`, `bridge/`) |
| Python / npm package mirrors | The host's Docker daemon (no socket mount) |
| The repo it clones into `/runner/_work` | OpenAlgo `localhost:5000`, bridge `localhost:5001` |

Guardrails enforced in `docker-compose.yml` (do **not** remove any):
- **No host bind-mounts** — fresh `git clone` per job inside the container.
- **No Docker socket** (`/var/run/docker.sock`).
- **No host networking** (`network_mode: host`) and **not** `privileged`.
- **1 CPU / 2 GB RAM** caps so live trading keeps its headroom.
- **Ephemeral** — a clean container per job, de-registered after each run.

## Prerequisites

- **Docker Desktop** installed and running with the **WSL2 backend**
  (Settings → General → "Use the WSL 2 based engine"). The image is Linux; it
  runs on Windows through WSL2.

## 1. Get a token

This setup uses an **ephemeral** runner that re-registers itself after every
job, so the image needs a token it can use to *mint* fresh registration tokens
— a **fine-grained Personal Access Token (PAT)**, not the short-lived
registration token from the "New self-hosted runner" page.

1. github.com → your avatar → **Settings** (account settings).
2. **Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token**.
3. Set:
   - **Resource owner:** `sonawanedhiraj`
   - **Repository access:** *Only select repositories* → `openalgo`
   - **Permissions → Repository permissions → Administration:** *Read and write*
   - **Expiration:** the shortest you can tolerate (rotate on expiry).
4. **Generate** and copy the token (starts with `github_pat_…`).

> **One-shot alternative (no auto re-register):** use the short-lived token from
> `github.com/sonawanedhiraj/openalgo` → **Settings → Actions → Runners → New
> self-hosted runner** (the value after `--token`), put it in `RUNNER_TOKEN=`
> instead, and set `EPHEMERAL: "false"` in `docker-compose.yml`. That token
> expires in ~1 h, so an ephemeral runner can't re-register with it.

## 2. Configure

`scripts/install-runner.ps1` deploys these templates to the runtime dir
(`C:\actions-runner\` by default) and creates `.env.runner` **there**, not in
the repo — so configure the token in the runtime copy:

```powershell
# First run install-runner.ps1 once to copy the templates to C:\actions-runner\,
# then create the token file alongside them:
Copy-Item C:\actions-runner\.env.runner.example C:\actions-runner\.env.runner
# edit C:\actions-runner\.env.runner and set:  ACCESS_TOKEN=github_pat_...
```

The runtime `.env.runner` lives **outside the repo**, so the token is never at
risk of being committed (the in-repo `ci/runner/.env.runner` is also gitignored
as a belt-and-braces guard).

## 3. Start

From the repo root — this copies the templates to `C:\actions-runner\` and
starts the container there:

```powershell
pwsh scripts/install-runner.ps1
```

To deploy to a different runtime dir, pass `-Target`:

```powershell
pwsh scripts/install-runner.ps1 -Target D:\actions-runner
```

## 4. Verify

- **Container:** `docker compose -f C:\actions-runner\docker-compose.yml logs --tail=50`
  shows `Listening for Jobs`.
- **GitHub:** `github.com/sonawanedhiraj/openalgo` → **Settings → Actions →
  Runners** lists **openalgo-laptop** with status **Idle** within ~60 s.

## Manage

```powershell
docker compose -f C:\actions-runner\docker-compose.yml stop            # pause
docker compose -f C:\actions-runner\docker-compose.yml start           # resume
docker compose -f C:\actions-runner\docker-compose.yml down            # remove container
docker compose -f C:\actions-runner\docker-compose.yml logs -f         # follow logs
```

## Troubleshooting

**Runner doesn't appear in GitHub after 60 s**
1. `docker compose -f C:\actions-runner\docker-compose.yml logs` — look for auth errors.
2. `401/403` → token wrong/expired or lacks **Administration: R/W**. Regenerate
   the PAT (step 1) and update `.env.runner`.
3. `404` → check `REPO_URL` in `docker-compose.yml` matches the real repo.
4. Confirm Docker Desktop is running (`docker version`).
5. After fixing `.env.runner`, recreate the container:
   `docker compose -f C:\actions-runner\docker-compose.yml up -d --force-recreate`.

**Jobs queue forever / "no runner matching labels"**
The workflow targets `[self-hosted, linux, docker, openalgo-laptop]`; the
container advertises exactly those labels (`LABELS` in `docker-compose.yml`).
Don't change one without the other.
