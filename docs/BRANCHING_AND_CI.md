# Branching, Merge, and CI Strategy

> Status: **first-pass plan** (2026-06-07; CI section revised same day to
> local-first execution — see "Continuous Integration"). Captures the lessons
> from the 47-stale-branch cleanup and the CI that arrived via the upstream
> "dev-stability foundation" merge. Nothing here is enforced until the "Next
> steps" section is executed and the operator approves.

## Goals

1. **Ship safely on a real-money system** — every change is reviewable,
   testable, and revertible. Production is a single Windows box with a
   persistent broker session; **market-hours restarts are forbidden**, so
   CI must never touch the live process.
2. **Avoid branch sprawl** — the 47-branch incident must be structurally
   impossible, not merely discouraged. Delete-after-merge is policy.
3. **Stay inside GitHub's free tier** (or document why we'd pay/self-host).
4. **Keep ceremony small** — solo dev (Dheeraj), self-review only. No
   approval gates that block a one-person workflow.

## Current state (audited 2026-06-07)

- **Remotes:** `origin = github.com/sonawanedhiraj/openalgo` (our fork),
  `upstream = github.com/marketcalls/openalgo`. **Visibility unconfirmed
  from CLI** — see "Open question" below. It drives the whole CI-cost
  decision.
- **Trunks today:** `dev` (integration) and `main` (stable / upstream-aligned).
  This is already a sane two-tier layout — we formalize it, not replace it.
- **Branch naming already in use:** `feat/`, `fix/`, `chore/`, `docs/`,
  `validate/` — all short-lived, all merged into `dev` with `--no-ff` merge
  commits (`Merge feat/X into dev`). Good instinct; we codify it.
- **Cadence:** 524 commits / 61 merges in 60 days (~30 merges/month, ~10/week).
  High volume — CI cost matters.
- **Existing CI (inherited from upstream, not authored here):**
  `.github/workflows/ci.yml` (10 jobs incl. **multi-arch Docker push to
  Docker Hub**, Playwright e2e, frontend build/test/lint, `make gate`),
  `security.yml` (weekly bandit + pip-audit → SARIF), `dependabot.yml`.
  Several jobs are **fork-irrelevant** (Docker Hub push needs
  `DOCKERHUB_*` secrets we don't have; multi-arch + e2e are minute hogs).
- **Local gate exists:** `make gate` = `lint` (ruff on smoke scripts) +
  `smoke` (import-only `scripts/smoke_boot.py`) + `test-engine` (engine +
  journal subset). No `scripts/ci.sh` yet.
- **Pre-commit hooks** (`.pre-commit-config.yaml`): ruff `--fix`,
  ruff-format, biome (frontend), **detect-secrets** (baseline),
  trailing-whitespace, end-of-file-fixer, check-yaml/json,
  **check-added-large-files (max 1000 KB)**. The strategy-module merge was
  committed with `--no-verify` — almost certainly because the large-file
  check or detect-secrets tripped on a merge commit. We define a documented
  exception instead of habitual bypass (see "Pre-commit policy").
- **Tests:** large `test/` tree; CI runs a **hardcoded 5-file "CI-safe"
  subset**. `@pytest.mark.unit` exists in exactly one file
  (`test_sector_rotation_etf.py`) and is **not declared in pyproject** — so
  it's a new, uncodified convention that currently emits an unknown-mark
  warning. We codify it.
- **Tags:** dozens of upstream `openalgo-*` feature tags; **no semver tag**.
  A past `v1.0.0` existed as a *branch*, which is wrong — releases are tags.

## Branching Strategy

**Pick: GitHub Flow with a two-tier trunk** (`dev` → `main`). Trunk-based
with short-lived branches is the right model for a solo real-money dev: it
keeps integration continuous and branches disposable, while the extra
`main` tier gives a stable, upstream-aligned line to tag releases from and
to merge `upstream/main` into without destabilizing daily work. Git Flow's
release/hotfix ceremony is pure overhead for one person.

Lifecycle of a change:

```
dev ──┬─ feat/x ─(work, ≤14 days)─► PR to dev ─► squash/merge ─► delete feat/x
      ├─ fix/y  ─────────────────► PR to dev ─► merge ────────► delete fix/y
      └─ (periodically) dev ─► PR to main ─► merge commit ─► tag vX.Y.Z on main

upstream/main ─► merge into a sync/upstream-YYYY-MM-DD branch ─► PR to main
```

`dev` is the default branch. Every working branch is cut from `dev`, lives
days not weeks, and is deleted the moment it merges.

## Branch Naming

| Prefix | Use | Example |
| --- | --- | --- |
| `feat/` | New capability | `feat/sector-rotation-etf` |
| `fix/` | Bug fix | `fix/preflight-premarket-filter` |
| `chore/` | Tooling, deps, cleanup | `chore/validation-scan-cycle` |
| `docs/` | Docs only | `docs/branching-and-ci-plan` |
| `validate/` | Throwaway strategy/data validation | `validate/chartink-rule-day1` |
| `exp/` | Experiments expected to be discarded | `exp/atr-trailing-sweep` |
| `sync/` | Upstream merges | `sync/upstream-2026-06-07` |

Rules: lowercase kebab-case after the prefix; **no date or issue suffix**
(redundant for solo work — git already timestamps); keep it under ~40 chars.
`exp/` and `validate/` branches are explicitly disposable — never merged,
deleted at end of experiment.

## Merge Strategy

| Source → Target | Merge type | Why |
| --- | --- | --- |
| `dev ← feat/*` (≥3 commits) | **Merge commit (`--no-ff`)** | Preserves the feature grouping; matches today's `Merge feat/X into dev` history and keeps revertible units. |
| `dev ← feat/* / fix/* / chore/*` (1–2 noisy commits) | **Squash** | Collapses WIP noise into one clean, revertible commit on `dev`. |
| `dev ← docs/* / validate/*` | **Squash** | Single tidy commit; history value is low. |
| `main ← dev` (release) | **Merge commit (`--no-ff`)** | Marks the release boundary; FF would erase it. |
| `main ← sync/upstream-*` | **Merge commit** | **Preserves upstream author attribution** — never squash upstream work. |

Default for ad-hoc small branches: squash. Default for substantial,
multi-commit features: merge commit. When in doubt, squash — fewer, cleaner
revert points beat a tangled `dev`.

## Branch Lifecycle (the anti-sprawl rules)

These exist specifically so the 47-branch incident cannot recur:

1. **Delete-after-merge is automatic** — enable GitHub's repo setting
   *Automatically delete head branches* so every merged PR's branch is
   removed server-side. Locally, prune on every fetch (see step 4).
2. **Max age 14 days unmerged** — anything older is either finished and
   merged, or abandoned and deleted. No long-running feature branches.
3. **`exp/` and `validate/` are deleted at experiment end**, never merged.
4. **Monthly hygiene audit** (target: a Cowork scheduled task, weekend /
   market-closed):
   ```bash
   git fetch --prune                      # drop refs deleted on origin
   git branch --merged dev | grep -vE '^\*|dev|main' | xargs -r git branch -d
   git branch -a --sort=-committerdate    # eyeball anything >14d old
   ```
   The task runs **read-only on code** (per the audit-trail policy) — it
   reports stale branches; the operator deletes.

## Release / Tagging

- Releases are **annotated tags on `main`**, never branches: `git tag -a
  v1.2.0 -m "..."` then `git push origin v1.2.0`. (Fixes the old
  `v1.0.0`-as-a-branch mistake.)
- Format: **semver `vMAJOR.MINOR.PATCH`** for our fork's milestones. Keep
  this distinct from the platform version in `utils/version.py` and from
  the upstream `openalgo-*` tags — those are not ours to manage.
- Only the operator pushes tags. Tagging is manual and deliberate, tied to
  a meaningful, tested state of `main`.

## Upstream Sync (OpenAlgo `marketcalls/main` → our fork)

The full step-by-step runbook lives in a sibling doc —
[`docs/UPSTREAM_SYNC_PROCEDURE.md`](UPSTREAM_SYNC_PROCEDURE.md) — so this
section stays strategic. Below is the *why*: the measured divergence, where
conflicts actually live, and the structural rules that keep the next merge
cheap.

### Divergence snapshot (audited 2026-06-07)

Audited against upstream HEAD `e4916a7d` (`chore: auto-build frontend dist`,
2026-06-05).

| Line | Merge-base w/ upstream | Fork commits ahead | Upstream commits to absorb | Both-edited (conflict surface) |
| --- | --- | --- | --- | --- |
| `main` | `4280d879` (2026-04-22) | **1** | **261** | **1** (`CLAUDE.md`) |
| `dev` | `c3bb4436` (2026-05-26) | **173** | **47** | **4** |

**Finding — the trunks are inverted.** `dev`'s merge-base (2026-05-26) is
*newer* than `main`'s (2026-04-22): `dev` already absorbed the upstream
"dev-stability foundation" merge, but `main` did not. The "sync into `main`
first, then fast-forward `dev`" model below is therefore **not what
happened** — upstream landed directly on `dev`. Before the next sync the
operator must reconcile this (see Open question). All conflict analysis here
uses the **`dev`** base, since that is where fork code actually lives.

### Conflict-surface analysis (what will actually bite)

The `dev`-vs-upstream conflict surface is only **4 files**:

| File | Fork diff (lines) | Upstream diff (lines) | Nature | Resolution |
| --- | --- | --- | --- | --- |
| **`app.py`** | **453** | 46 | Fork boot wiring (preflight bp, scanner DB init, csrf exempts, scanner pre-subscribe boot-retry, engine rehydration) scattered inline vs upstream boot tweaks | **Hand-merge** — the chronic zone (see Isolation #1) |
| `pyproject.toml` | ~10 | ~6 | Fork adds deps (`fastapi`, `pandas-ta-classic`, `feedparser`) + pytest config (`pythonpath`, `--import-mode=importlib`, `asyncio_mode`); upstream bumps version + `openalgo`/`starlette`/`idna` pins | **Hand-merge** — keep both (see procedure doc) |
| `uv.lock` | regenerated | regenerated | Lockfile | **Take neither — regenerate** with `uv lock` after merging `pyproject.toml` |
| `CLAUDE.md` | 7 commits | n/a | Operator notes vs upstream additions | **Hand-merge** — keep both |

Everything else fast-forwards cleanly. Crucially, upstream's 47 commits are
**frontend-dominated**: 662 `frontend/**` files, 58 `broker/**`, and only
**2 `services/`** (`market_data_service.py`, `option_greeks_service.py`) and
**3 `blueprints/`**. The fork edits *no* frontend files, so the 662-file
frontend churn is a clean "take theirs + rebuild" — not a conflict.

**Why the surface is so small:** nearly every chronic fork-edited file is
**fork-only** — upstream has no copy to conflict with. Verified fork-only:
`services/simplified_stock_engine_service.py` (16 fork commits),
`services/preflight_service.py` (10), `services/scanner_service.py` (6),
`services/scan_cycle_service.py` (5), `services/backtest_service.py` (5),
`services/signal_review_service.py`, `services/notification_service.py`. The
only chronic file upstream *does* have is **`blueprints/chartink.py`** — a
**latent** conflict (upstream did not touch it in these 47 commits, but it
is shared, so a future upstream edit will collide with our simplified-engine
routes at `chartink.py:947+`).

### Isolation patterns (keep new code fork-private)

The audit shows isolation already works: upstream touches **0** files under
`strategies/sector_rotation_etf/`, `services/sector_rotation_etf_*`,
`bridge/`, `strategies/simplified_engine/`, and `services/simplified_stock_engine_*`.
The two weak spots are files where fork logic was added *inline* to an
upstream file. Concrete extraction recommendations, in ROI order:

1. **Extract `app.py` boot wiring → `services/fork_bootstrap.py`** (highest
   ROI). app.py is the fork's #1 churned file (28 commits) *and* the only
   real conflict file. Move the ~453 lines of fork boot logic (preflight
   blueprint registration, scanner DB init, csrf exempts, scanner
   pre-subscribe boot-retry thread, simplified-engine rehydration) behind a
   single `wire_fork(app, socketio)` call. This turns a 453-line scattered
   diff into a **one-line addition** inside `create_app()` — which a
   `# FORK-START`/`# FORK-END` marker pair makes trivially mergeable.
2. **Move simplified-engine routes out of `blueprints/chartink.py` →
   `blueprints/simplified_engine_routes.py`** (defuses the one latent
   shared-file conflict). The webhook/status/direction-toggle routes at
   `chartink.py:947+` are fork-only behavior bolted onto an upstream
   blueprint; a dedicated fork blueprint registered via `wire_fork` keeps
   `chartink.py` byte-identical to upstream.
3. **Quarantine fork env vars in `.sample.env`** (13 fork commits). Not
   currently a conflict, but it is a shared config file; wrap the fork keys
   (`SIMPLIFIED_ENGINE_*`, `SCANNER_*`, `PREFLIGHT_*`, bridge keys) in a
   clearly delimited `# === FORK CONFIG ===` block at the end so upstream
   additions never interleave with ours.

### Edit-in-place conventions (for upstream files we must touch)

When a fork change genuinely cannot be isolated (e.g. the single
`wire_fork` call in `app.py`, deps in `pyproject.toml`, notes in
`CLAUDE.md`):

- **Marker comments** around every fork block:
  `# FORK-START: <one-line reason>` / `# FORK-END:`. A merge can then `grep`
  for our edits and a reviewer can see at a glance what is ours.
- **Bias to additions over modifications.** Appending a blueprint
  registration merges cleanly; rewriting upstream's `create_app` body does
  not. Never reorder or reformat upstream code we are not changing.
- **Keep upstream's structure** even where we would organize differently —
  structural drift is what turns a 3-way merge into a hand-rewrite.

### Cadence (revised) + cherry-pick-vs-merge rule

**Keep monthly for the full merge, but add a cherry-pick fast-path.**
Evidence: upstream ran **261 commits in 44 days (~5.9/day)** since `main`'s
base, **47 in ~10 days (~4.7/day)** since `dev`'s. That velocity sounds
alarming for a monthly cadence, but it is **frontend-dominated and
conflict-free for us** — a monthly merge absorbing ~120+ commits is fine
because the backend conflict surface stays ~4 files regardless of commit
count. Monthly stays.

What the velocity *does* argue for is a **between-sync cherry-pick path** for
anything urgent:

- **Cherry-pick** (onto a `fix/` branch off `dev`) when you need **one
  isolated upstream commit now** and don't want the whole frontend refresh:
  e.g. (a) a `broker/zerodha/api/order_api.py` order-placement bugfix mid-month;
  (b) an upstream auth/CSRF security fix you can't wait a month for.
- **Full merge** (the monthly `sync/upstream-*` branch) when changes **move
  as a coherent set**: e.g. (a) the 662-file `frontend/**` + `frontend/dist`
  refresh — never cherry-pick generated `dist` piecemeal; (b) a
  platform-version bump that spans `pyproject.toml` + `uv.lock` +
  `requirements*.txt` together.

Rule of thumb: **isolated + urgent → cherry-pick; coupled or generated →
full merge.**

### Tracking log

Every sync appends one entry to
[`docs/UPSTREAM_SYNC_LOG.md`](UPSTREAM_SYNC_LOG.md): date, upstream SHA
before/after, fork base before/after, files hand-merged, and anything
notable (dep bumps, migration reorders). This is the audit trail and
satisfies the "docs change WITH code" rule in `CLAUDE.md` — the log entry
ships in the same merge commit.

## Continuous Integration

> **2026-06-07 pivot — local CI is now primary.** The original plan picked
> GitHub-hosted ubuntu; the operator has decided to **run CI on the local
> machine instead**. This section is rewritten around that. The hosted
> rationale is kept as a short note below for posterity.

### The constraint we design around

The only available machine is **the production trading box** — one Windows
host running OpenAlgo (persistent broker session, live SQLite DBs, Cowork
scans every ~15 min during market hours). **Market-hours restarts are
forbidden**; CI must never contend for CPU/SQLite locks, touch the live DBs,
or read broker creds. That was the original reason to reject a self-hosted
runner. The operator is overriding it — so the task is to make local CI safe
*given* this is the only host.

### Options

| | What | GitHub gate | New risk on the box |
| --- | --- | --- | --- |
| **L1** | `pre-push` hook → `scripts/ci.ps1` (wraps `make gate`); no Actions | Advisory only (GitHub sees no result) | **None** — no daemon |
| **L2** | Self-hosted `[self-hosted, windows]` runner; push/PR reports status to GitHub | **Enforced** | Always-on executor running workflow YAML on the host with broker creds + live DBs |
| **L3** | L1 hook **+** L2 runner | Enforced + fast local feedback | Same as L2 |
| **L3-Docker** ✅ | L1 hook **+** self-hosted runner *inside a hardened Docker container* | Enforced + fast local feedback | **Contained** — no host mounts / socket / net; 1 CPU / 2 GB (see Isolation guarantees) |

L2/L3 buy an *enforced* status check by putting an always-on agent that runs
arbitrary workflow code onto the real-money host, and would need market-hours
gating (Task Scheduler), below-normal process priority, a `db_test/` sandbox,
and a secrets allow-list just to be tolerable.

### Decision: **L3-with-Docker-isolation.**

> **2026-06-07 update — supersedes the L1-only call below.** The operator now
> wants an *enforced* GitHub status check, so we add a self-hosted runner on
> this same box — accepting the runner risk **only because it runs inside a
> hardened Docker container** (no host mounts, no Docker socket, no host
> networking, 1 CPU / 2 GB, ephemeral per job). The L1-only reasoning is kept
> below for posterity.

L3 = the L1 `pre-push` hook (fast local feedback, unchanged — see *Market-hours
contention* below) **plus** a self-hosted runner that reports `gate` /
`backend-test` status to GitHub, so a red gate can *block* a merge instead of
merely advising. The runner is the part that previously made L2/L3
unacceptable; Docker isolation is what makes it tolerable on the trading box.

**Why Docker flips the trade-off.** The original objection was an always-on
executor running workflow code on the host with broker creds + live DBs. The
container removes every leg: no host filesystem (no bind-mounts), no host Docker
(no socket), no reach to OpenAlgo/bridge (no host networking), capped at 1 CPU /
2 GB so it can't starve live trading, fresh container per job. Config lives in
`ci/runner/docker-compose.yml`; bring-up in `scripts/install-runner.ps1`.

**Residual risks (accepted):** the runner is a long-lived process (bounded by
the caps + `unless-stopped`; stop it during sensitive windows); container escape
is the theoretical worst case (mitigated by no socket / no privileged / no
mounts / private repo); WSL2 carries a baseline RAM cost. None touch live DBs or
broker creds.

#### Isolation guarantees

**CAN see:** the GitHub API / Actions service; public package mirrors (PyPI via
`uv`, npm); the repo it clones fresh into its own `/runner/_work` each job.

**CANNOT see** (hard guarantees from `ci/runner/docker-compose.yml`): the host's
`db/*.db`, `.env`, broker credentials, `log/`, `audit/`, `bridge/`, or any host
path outside its own work dir; the host Docker daemon (`/var/run/docker.sock` is
**not** mounted); OpenAlgo on `localhost:5000` or the Cowork bridge on
`localhost:5001` (default bridge network only — **no** `network_mode: host`,
**no** `host.docker.internal` wiring).

**Trust boundary:** the workflow YAML lives in the repo and the repo is private,
so only the operator can push workflow code — there is no untrusted-contributor
path that runs on the box. The token lives only in the gitignored
`ci/runner/.env.runner`.

### Market-hours contention — how L1 stays safe

- **No daemon.** The hook fires only on an explicit `git push` — nothing
  scheduled or webhook-driven. The operator picks the moment.
- **Guard in `ci.ps1`.** 09:00–15:35 IST on weekdays → runs only the light
  steps (ruff + import smoke; seconds, single-process), skipping
  `test-engine`. `-Full` forces everything; `-Skip` aborts the push.
- **Never touches live state.** Runs `make gate` + import-only
  `smoke_boot.py`; does not start the app, open `db/*.db`, or read broker
  creds. Any future DB-touching step must point at `db_test/`.

### Why the recommendation moved (posterity)

The prior plan picked GitHub-hosted ubuntu, then pivoted to **L1 local-only**
(rejecting any self-hosted runner because the only spare machine is the
production box). The current plan keeps L1's local hook but **adds the runner
back as L3-with-Docker-isolation** — Docker contains the exact risk that got it
rejected, in exchange for an *enforced* GitHub status check. GitHub-minute
quotas remain N/A: the self-hosted runner consumes none, and the trimmed hosted
`ci.yml` is now `workflow_dispatch`-only (frontend checks, on demand).

## Branch Protection (GitHub Settings → Branches)

For a solo dev, protection is a safety net against *yourself*, not a review
gate — and under **L3-Docker the self-hosted runner now reports a real status
check you can require** (the L1-only plan had none — CI ran locally and GitHub
never saw a result). So protection can finally enforce a green gate. On
**`main`** (and optionally `dev`):

- ✅ **Required status checks — available under L3-Docker.** Enable *Require
  status checks to pass* and select **`gate`** (add **`backend-test`** once
  pytest markers land — see next steps; until then `-m unit` collects nothing
  and that check would be perpetually red). The local `pre-push` hook stays the
  fast first line of defence.
- ✅ Require branches to be up to date before merging.
- ❌ Require PR reviews / approvals — **off** (solo; no reviewers).
- ✅ Require conversation resolution (cheap, catches self-notes).
- ✅ *Automatically delete head branches* (repo-level — the anti-sprawl lever).
- ❌ Do not allow force-push / deletion of `main`.
- The real gate is the `pre-push` hook; the discipline is **don't
  `--no-verify`** except the documented large-file merge case below.

## Pre-commit policy

Keep the existing hooks. The `--no-verify` merge was a one-off; the rule
going forward: **`--no-verify` is allowed only for merge commits that trip
`check-added-large-files` on `frontend/dist`**, and that bypass must be
mentioned in the commit body. Everything else fixes the hook failure.

## Local pre-push gate (the primary CI mechanism)

Under L1 this *is* the CI. `scripts/ci.ps1` (Windows-native, primary) and
`scripts/ci.sh` (Git Bash / WSL fallback) wrap the same `make gate` checks; a
tracked `scripts/git-hooks/pre-push` calls the script, and
`scripts/install-hooks.ps1` copies it into the non-versioned `.git/hooks/`.
Ready-to-use scripts are in Appendix A / B. Run manually with
`pwsh scripts/ci.ps1` (add `-Full` to force the engine subset during market
hours).

**In operator terms:**

- *When I `git push`* → the hook runs `ci.ps1` first; the push proceeds only
  if it exits 0.
- *When CI fails* → the push aborts and nothing leaves the box (the gate runs
  *before* the network step — no remote state to roll back). Fix and re-push.
- *When I'm mid-bug-fix on a market-open day* → push still works; the guard
  runs only the light checks (seconds). Or `git push --no-verify` to skip,
  then `pwsh scripts/ci.ps1 -Full` once the market closes.

## What we're NOT doing yet (and why)

- **Docker socket mount, host networking, or host bind-mounts on the runner**
  — **non-negotiable: never.** The runner's safety rests entirely on the
  container seeing none of `/var/run/docker.sock`, `network_mode: host`, or any
  host path (`db/`, `.env`, `log/`, `audit/`, `bridge/`). Adding any one
  re-opens the exact risk that kept a runner off this box.
- **GitHub-hosted backend CI** — replaced by the self-hosted runner; the
  trimmed `ci.yml` keeps only frontend checks and only on `workflow_dispatch`.
  We don't publish images, so `docker-build` / `docker-manifest` /
  `commit-dist` stay deleted regardless.
- **Frontend e2e (Playwright) in CI** — heavy install, low solo ROI; run
  locally before UI ships.
- **Deploy automation** — production is hand-managed precisely because
  market-hours restarts are forbidden. No auto-deploy, ever.
- **Required PR reviews** — no second human.

## Next steps to implement (ordered)

1. **Add the local gate (L1 core)** — `scripts/ci.ps1` + `scripts/ci.sh` +
   `scripts/git-hooks/pre-push` (Appendix A / B), then run
   `pwsh scripts/install-hooks.ps1` to install the hook into `.git/hooks/`.
2. **Codify pytest markers** — add `markers = ["unit", "integration",
   "live"]` to `[tool.pytest.ini_options]` and tag the fast tests `unit`,
   so the gate can run `pytest -m unit` instead of the hardcoded 5-file list.
3. **Stand up the L3-Docker runner** — create a fine-grained PAT
   (Administration: read/write on `openalgo`), copy
   `ci/runner/.env.runner.example` → `ci/runner/.env.runner`, paste it as
   `ACCESS_TOKEN`, then run `pwsh scripts/install-runner.ps1`. Verify
   **openalgo-laptop** shows *Idle* under repo Settings → Actions → Runners.
   (`ci.yml` is already trimmed to `workflow_dispatch`-only; backend CI now
   lives in `.github/workflows/ci-self-hosted.yml`.)
4. **Configure branch protection + auto-delete head branches** in repo
   Settings — enable *Require status checks to pass* and select **`gate`**
   (add **`backend-test`** after pytest markers land, step 2).
5. **Schedule the monthly branch-hygiene audit** as a read-only Cowork task.
6. **Reconcile `main` with `dev`'s upstream base** — bring `main` up to the
   2026-05-26 upstream state `dev` already has, so the "sync into `main`
   first" model is true before the next sync (see Upstream Sync finding).
7. **Extract `app.py` fork boot wiring → `services/fork_bootstrap.py`**
   (Upstream Sync, Isolation #1) — the single highest-leverage refactor for
   cheap future merges. Wrap the call site in `# FORK-START`/`# FORK-END`.
8. **Move simplified-engine routes out of `blueprints/chartink.py`** into a
   fork-private `blueprints/simplified_engine_routes.py` (Isolation #2) to
   defuse the one latent shared-file conflict.
9. **Quarantine fork env vars** into a marked block in `.sample.env`
   (Isolation #3).
10. **Create `docs/UPSTREAM_SYNC_LOG.md`** (empty header + schema) so the
    first sync has somewhere to record.
11. Link this doc from `CLAUDE.md` once enacted.

---

## Appendix A — L1 local gate (Windows-native, primary)

*Not committed in this plan — review first. Three tracked files plus an
installer. The `pre-push` hook is the gate; `ci.ps1` does the work, inlining
`make gate`'s commands so it runs without `make` on Windows.*

```powershell
# scripts/ci.ps1 — local CI gate (primary). No GitHub minutes.
#   -Full forces the engine subset; -Skip aborts (lets pre-push refuse a push).
param([switch]$Full, [switch]$Skip)
$ErrorActionPreference = 'Stop'
if ($Skip) { Write-Host 'CI skipped by request'; exit 1 }

# Market-hours guard: 09:00-15:35 IST Mon-Fri -> light checks unless -Full.
$ist = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
    (Get-Date), 'India Standard Time')
$mkt = ($ist.DayOfWeek -in 'Monday','Tuesday','Wednesday','Thursday','Friday') `
    -and ($ist.TimeOfDay -ge '09:00:00') -and ($ist.TimeOfDay -le '15:35:00')

Write-Host '==> ruff lint (smoke scope)'
uv run ruff check scripts/smoke_boot.py scripts/smoke_engine_live.py
if ($LASTEXITCODE) { exit 1 }
Write-Host '==> import smoke'; uv run python scripts/smoke_boot.py
if ($LASTEXITCODE) { exit 1 }

if ($mkt -and -not $Full) {
    Write-Host '==> MARKET HOURS: skipping test-engine (pass -Full to force)'
} else {
    Write-Host '==> engine + journal tests'
    uv run pytest test/test_simplified_stock_engine_service.py `
        test/test_engine_journal_integration.py `
        test/test_eod_watchdog_service.py `
        test/test_trade_journal_service.py -q
    if ($LASTEXITCODE) { exit 1 }
}

Write-Host '==> bandit + detect-secrets (advisory, never block)'
uv run --group dev bandit -r . -x .venv,test,frontend,node_modules -ll -q
uv run --group dev detect-secrets scan --baseline .secrets.baseline | Out-Null
Write-Host 'OK: local gate passed - safe to push'; exit 0
```

```bash
# scripts/git-hooks/pre-push — tracked source (.git/hooks isn't versioned).
# Install: pwsh scripts/install-hooks.ps1 . Bypass: git push --no-verify
#!/usr/bin/env bash
set -euo pipefail
if command -v pwsh >/dev/null 2>&1; then pwsh -NoProfile -File scripts/ci.ps1
elif command -v powershell >/dev/null 2>&1; then powershell -NoProfile -File scripts/ci.ps1
else bash scripts/ci.sh; fi   # Git Bash / WSL fallback
```

```powershell
# scripts/install-hooks.ps1 — copy tracked hook into .git/hooks/.
# Re-run after cloning or when scripts/git-hooks/* changes.
$src = Join-Path $PSScriptRoot 'git-hooks\pre-push'
$dst = Join-Path (git rev-parse --git-path hooks) 'pre-push'
Copy-Item $src $dst -Force; Write-Host "Installed pre-push hook -> $dst"
```

## Appendix B — `scripts/ci.sh` (Git Bash / WSL fallback)

*Same checks as `ci.ps1`, for environments without PowerShell.*

```bash
#!/usr/bin/env bash
# scripts/ci.sh — local gate fallback. Mirrors scripts/ci.ps1.
set -euo pipefail
echo '==> ruff lint (smoke scope)'; make lint
echo '==> import smoke';            make smoke
# Market-hours guard (09:00-15:35 IST, Mon-Fri): skip engine subset unless FULL=1.
dow=$(TZ=Asia/Kolkata date +%u); hm=$((10#$(TZ=Asia/Kolkata date +%H%M)))
if [ "${FULL:-0}" != 1 ] && [ "$dow" -le 5 ] && (( hm >= 900 && hm <= 1535 )); then
  echo '==> MARKET HOURS: skipping test-engine (FULL=1 to force)'
else
  echo '==> engine + journal tests'; make test-engine
fi
echo '==> bandit (high severity only)'    # advisory
uv run --group dev bandit -r . -x .venv,test,frontend,node_modules -ll -q || true
echo '==> detect-secrets'                 # advisory
uv run --group dev detect-secrets scan --baseline .secrets.baseline || true
echo 'OK: local gate passed - safe to push'
```

## Open questions for the operator

1. **Do you have any other machine** — a laptop, an old PC, a cheap VPS —
   you'd rather host CI on? If yes, **L3 becomes attractive**: the local hook
   for fast feedback *plus* a self-hosted runner on *that* box gives enforced
   GitHub status checks with **none** of the trading-box risk. This is the
   single biggest lever on the recommendation — answer this first.
2. **Fate of the inherited `.github/workflows/ci.yml`?** Under L1 it still
   fires on every push. Disable it, or keep it as free belt-and-braces if the
   fork is public? (Depends on visibility, below.)
3. **Repo visibility — public or private?** *Downgraded:* no longer drives
   cost (L1 uses zero GitHub minutes). Now matters only for whether the
   inherited `ci.yml` runs free (public → unlimited) and for PR-badge /
   branch-protection cosmetics. Confirm in Settings → General.
4. **Trunk model is inverted for upstream sync.** `dev`'s upstream
   merge-base (2026-05-26) is newer than `main`'s (2026-04-22) — upstream
   landed on `dev`, not `main`. Should we (a) make `main` the canonical
   upstream-sync target and back-fill it to `dev`'s level first, or (b)
   accept that upstream lands on `dev` and adjust the two-tier model
   accordingly? The procedure doc assumes (a). **Needs a decision before the
   next sync.**
