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

## Upstream Sync (OpenAlgo `marketcalls/main` → our `main`)

- **Cadence:** monthly, or when a needed upstream fix lands. Not per-commit.
- **Procedure:** `git fetch upstream` → branch `sync/upstream-YYYY-MM-DD`
  off our `main` → `git merge upstream/main` → resolve conflicts (watch
  `frontend/dist`, `CLAUDE.md`, strategy dirs) → run the full local gate →
  PR into `main` with a **merge commit** (attribution preserved).
- After `main` absorbs upstream, fast-forward `dev` from `main` so daily
  work builds on the latest base.

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
7. Link this doc from `CLAUDE.md` once enacted.

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

## Open question for the operator

**Is `github.com/sonawanedhiraj/openalgo` public or private?** Forks of
public repos default to public (→ unlimited free Actions). The CLI can't
tell us. Confirm in Settings → General. The lean CI profile fits the free
tier either way, so this changes urgency, not the plan.
