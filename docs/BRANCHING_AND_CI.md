# Branching, Merge, and CI Strategy

> Status: **first-pass plan** (2026-06-07). Captures the lessons from the
> 47-stale-branch cleanup and the CI that arrived via the upstream
> "dev-stability foundation" merge. Nothing here is enforced until the
> "Next steps" section is executed and the operator approves.

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

### Decision: **(a) GitHub Actions, hosted ubuntu — but trimmed to a lean
fork profile.** Plus **(c) a local `scripts/ci.sh` / `ci.ps1` pre-push
gate** as the cheap first line of defense.

We do **not** self-hosted-runner this. The only spare machine is the
production trading box; a runner there could contend for SQLite locks or
CPU during market hours, and market-hours restarts are forbidden. Hosted
ubuntu runners are isolated and free (see quotas).

We **prune the inherited upstream CI**: drop `docker-build`,
`docker-manifest` (no Docker Hub secrets, multi-arch is the biggest minute
sink), `commit-dist` (we commit `dist` by hand per CLAUDE.md), and
`frontend-e2e` (Playwright install ≈ 3–5 min/run for little solo value).
Keep a lean backend-focused pipeline.

### Triggers

- **PR → `dev` and PR → `main`:** full lean pipeline (the real gate).
- **Push → `main`:** lean pipeline (post-merge confirmation).
- **Weekly schedule:** keep `security.yml` (bandit + pip-audit) as-is.
- `concurrency` with `cancel-in-progress` (already in `ci.yml`) so rapid
  re-pushes don't stack runs — important at 10 merges/week.

### Pipeline jobs (lean profile, est. wall-clock on ubuntu)

| Job | Command | ~min |
| --- | --- | --- |
| gate | `make gate` (ruff smoke-scope + import smoke + engine tests) | 2–3 |
| backend-lint | `ruff check .` + `ruff format --check .` (non-blocking) | 1–2 |
| backend-test | `pytest -m unit` (once markers are codified) | 2–4 |
| security-scan | `bandit -ll` + `pip-audit` (non-blocking) | 2–3 |
| frontend-lint+build | `npm ci && npm run lint && npm run build` (only if `frontend/**` changed) | 3–5 |

Total ≈ **6–10 min/run** with `uv` + npm caching, jobs in parallel.

### Quotas — free-tier math

- **If the repo is PUBLIC** (forks of public repos default to public):
  GitHub Actions on standard runners is **free and unlimited**. Cadence is
  irrelevant. ✅
- **If PRIVATE** (GitHub Free): **2,000 Linux min/month**. At ~30 PRs +
  ~30 main pushes/month × ~8 min = **~480 min/month** for the lean profile
  → comfortably under 2,000 (≈24% used). ✅ The *inherited* profile
  (Docker multi-arch ≈ 20–30 min/run) would blow this — another reason to
  trim. Marginal headroom only if we kept Docker; ample once trimmed.

**Verdict:** Free tier covers us either way **after trimming**. Confirm
visibility, but no paid plan is needed.

## Branch Protection (GitHub Settings → Branches)

For a solo dev, protection is a safety net against *yourself*, not a review
gate. On **`main`** (and optionally `dev`):

- ✅ Require status checks to pass before merging → select `gate`,
  `backend-test`.
- ✅ Require branches to be up to date before merging.
- ❌ Require PR reviews / approvals — **off** (solo; no reviewers).
- ✅ Require conversation resolution (cheap, catches self-notes).
- ✅ *Automatically delete head branches* (repo-level — the anti-sprawl lever).
- ❌ Do not allow force-push / deletion of `main`.
- Allow yourself to bypass in a genuine market-hours emergency, but log it.

## Pre-commit policy

Keep the existing hooks. The `--no-verify` merge was a one-off; the rule
going forward: **`--no-verify` is allowed only for merge commits that trip
`check-added-large-files` on `frontend/dist`**, and that bypass must be
mentioned in the commit body. Everything else fixes the hook failure.

## Local pre-push gate

Add `scripts/ci.sh` (+ `scripts/ci.ps1` for Windows) wrapping the same
checks CI runs, so a green local run predicts green CI. This is the
first-line defense — fast, no GitHub minutes. (Example below; not committed
in this plan.)

## What we're NOT doing yet (and why)

- **Docker-based CI / multi-arch / Docker Hub push** — we don't publish
  images; pure minute waste. Removed from the lean profile.
- **Frontend e2e (Playwright) in CI** — heavy install, low solo ROI; run
  locally before UI ships.
- **Deploy automation** — production is hand-managed precisely because
  market-hours restarts are forbidden. No auto-deploy, ever.
- **Required PR reviews** — no second human.

## Next steps to implement (ordered)

1. **Confirm repo visibility** (Settings → General) — determines whether
   minutes are even a concern. *(Operator action.)*
2. **Codify pytest markers** — add `markers = ["unit", "integration",
   "live"]` to `[tool.pytest.ini_options]` and tag the fast tests `unit`,
   so CI can run `pytest -m unit` instead of the hardcoded 5-file list.
3. **Add `scripts/ci.sh` + `scripts/ci.ps1`** (local pre-push gate).
4. **Trim `.github/workflows/ci.yml`** to the lean profile (remove
   `docker-build`, `docker-manifest`, `commit-dist`, `frontend-e2e`;
   gate `frontend-*` on `paths: frontend/**`).
5. **Configure branch protection + auto-delete head branches** in repo
   Settings.
6. **Schedule the monthly branch-hygiene audit** as a read-only Cowork task.
7. **Reconcile `main` with `dev`'s upstream base** — bring `main` up to the
   2026-05-26 upstream state `dev` already has, so the "sync into `main`
   first" model is true before the next sync (see Upstream Sync finding).
8. **Extract `app.py` fork boot wiring → `services/fork_bootstrap.py`**
   (Upstream Sync, Isolation #1) — the single highest-leverage refactor for
   cheap future merges. Wrap the call site in `# FORK-START`/`# FORK-END`.
9. **Move simplified-engine routes out of `blueprints/chartink.py`** into a
   fork-private `blueprints/simplified_engine_routes.py` (Isolation #2) to
   defuse the one latent shared-file conflict.
10. **Quarantine fork env vars** into a marked block in `.sample.env`
    (Isolation #3).
11. **Create `docs/UPSTREAM_SYNC_LOG.md`** (empty header + schema) so the
    first sync has somewhere to record.
12. Link this doc from `CLAUDE.md` once enacted.

---

## Appendix A — example `.github/workflows/ci.yml` (lean profile)

*Not committed — review before replacing the inherited `ci.yml`.*

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main, dev]
permissions:
  contents: read
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v7
        with: { enable-cache: true, python-version: "3.12" }
      - run: uv sync --dev
      - name: Provision .env for smoke
        run: cp .sample.env .env
      - name: Pre-merge gate
        run: make gate

  backend-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v7
        with: { enable-cache: true, python-version: "3.12" }
      - run: uv sync --dev
      - name: Unit tests
        run: uv run pytest -m unit --timeout=60   # after markers are codified

  backend-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v7
        with: { enable-cache: true, python-version: "3.12" }
      - run: uv sync --dev
      - run: uv run ruff check .
        continue-on-error: true     # ~1500 pre-existing legacy warnings
      - run: uv run ruff format --check .
        continue-on-error: true

  security-scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v7
        with: { enable-cache: true, python-version: "3.12" }
      - run: uv sync --dev
      - run: uv run bandit -r . -x .venv,test,frontend,node_modules -ll -f txt
        continue-on-error: true
      - run: uv run pip-audit
        continue-on-error: true

  frontend:
    runs-on: ubuntu-latest
    # Gate on frontend changes only — most PRs are backend.
    if: ${{ contains(github.event.pull_request.labels.*.name, 'frontend') || github.event_name == 'push' }}
    defaults:
      run:
        working-directory: frontend
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-node@v6
        with: { node-version: '22', cache: 'npm', cache-dependency-path: frontend/package-lock.json }
      - run: npm ci
      - run: npm run lint
      - run: npm run build
```

> Keep `security.yml` and `dependabot.yml` as-is. Drop the `docker-build`,
> `docker-manifest`, `commit-dist`, `frontend-e2e`, `frontend-test` jobs
> from the inherited workflow (or gate the frontend ones on a `frontend`
> label / `paths` filter as above).

## Appendix B — example `scripts/ci.sh` (local pre-push gate)

*Not committed — review first. Mirrors the CI gate so a green local run
predicts green CI. Run before every push: `bash scripts/ci.sh`.*

```bash
#!/usr/bin/env bash
# Local pre-push gate — fast, no GitHub minutes. Mirrors .github/workflows/ci.yml.
set -euo pipefail
echo "==> ruff format (check)"; uv run ruff format --check . || true
echo "==> ruff lint (smoke scope)"; make lint
echo "==> import smoke"; make smoke
echo "==> engine + journal tests"; make test-engine
echo "==> unit tests"; uv run pytest -m unit --timeout=60 || \
  echo "  (no unit-marked tests yet — codify markers, see plan step 2)"
echo "==> bandit (high severity only)"; \
  uv run --group dev bandit -r . -x .venv,test,frontend,node_modules -ll -q || true
echo "==> detect-secrets"; \
  uv run --group dev detect-secrets scan --baseline .secrets.baseline || true
echo "✓ local gate passed — safe to push"
```

For Windows, a `scripts/ci.ps1` with the same steps (`uv run` calls are
identical; replace `make` targets with the underlying `uv run` commands or
invoke `make` via Git Bash / WSL).

## Open questions for the operator

1. **Is `github.com/sonawanedhiraj/openalgo` public or private?** Forks of
   public repos default to public (→ unlimited free Actions). The CLI can't
   tell us. Confirm in Settings → General. The lean CI profile fits the free
   tier either way, so this changes urgency, not the plan.
2. **Trunk model is inverted for upstream sync.** `dev`'s upstream
   merge-base (2026-05-26) is newer than `main`'s (2026-04-22) — upstream
   landed on `dev`, not `main`. Should we (a) make `main` the canonical
   upstream-sync target and back-fill it to `dev`'s level first, or (b)
   accept that upstream lands on `dev` and adjust the two-tier model
   accordingly? The procedure doc assumes (a). **Needs a decision before the
   next sync.**
