# Upstream Sync Procedure (runbook)

> Operational companion to [`BRANCHING_AND_CI.md`](BRANCHING_AND_CI.md) →
> "Upstream Sync". That section explains *why* (divergence, conflict
> surface, isolation rules); this doc is the *how* — the step-by-step
> checklist for the monthly merge of `upstream/main`
> (`marketcalls/openalgo`) into our fork. Read both before a sync.
>
> Status: **first-pass plan (2026-06-07), not yet enacted.** Assumes the
> Open-question #2 decision (canonical sync target) is resolved first.

This runbook assumes the target branch is `main` (per the two-tier model).
If Open-question #2 resolves to "upstream lands on `dev`", substitute `dev`
throughout.

---

## A. Pre-sync checklist (gate — do not start the merge until all pass)

- [ ] **Market is closed.** Never sync during market hours — a botched
      restart is forbidden. Prefer a weekend.
- [ ] **Working tree clean** — `git status --porcelain` empty. Stash or
      commit any WIP first (the boot dirty-check warns on restart otherwise).
- [ ] **`dev` is merged/up to date** so we sync into the canonical line
      first, then propagate. (Resolve Open-question #2 if `main` is behind
      `dev` — back-fill `main` to `dev`'s upstream base before merging.)
- [ ] **Known-good baseline captured** — production smoke-tested on the
      *current* state so we can tell post-sync regressions from pre-existing
      ones. Record current platform version (`utils/version.py`).
- [ ] **Backups taken:**
      - `cp .env .env.bak.$(date +%Y%m%d-%H%M%S)` (env keys / fork config)
      - `cp db/*.db db/backups/` (migrations may reorder columns/tables)
      - Note current `git rev-parse main dev upstream/main` SHAs.
- [ ] **Read the upstream changelog** — `git log --oneline <last-sync-sha>..upstream/main`
      and skim for: DB schema migrations, dependency major bumps, auth/mode
      changes, removed endpoints. Flag anything that touches fork-private
      services.

## B. During-sync procedure

```bash
git fetch upstream
git checkout main && git pull          # canonical line, clean
git checkout -b sync/upstream-$(date +%Y-%m-%d)
git merge --no-ff upstream/main        # attribution preserved; expect conflicts
```

### Per-file-class resolution

Resolve by **class**, not file-by-file guessing. The audit (BRANCHING doc)
gives the current conflict surface; treat each conflict by its class:

| Class | Files (current) | How to resolve |
| --- | --- | --- |
| **`frontend/**` + `frontend/dist`** | 662 upstream files | **Always take theirs** (`git checkout --theirs -- frontend/`). We edit no React. **Only rebuild** (`cd frontend && npm ci && npm run build`) if *our* React actually changed this cycle — it hasn't to date, so the committed `dist` from upstream is authoritative. |
| **`uv.lock`** | regenerated | **Take neither.** Resolve `pyproject.toml` first, then `git checkout --theirs -- uv.lock` and run `uv lock` to regenerate against the merged `pyproject.toml`. Never hand-edit. |
| **`pyproject.toml`** | deps + pytest cfg | **Hand-merge, keep BOTH.** Preserve fork deps (`fastapi`, `pandas-ta-classic`, `feedparser`) **and** the pytest block (`pythonpath=["."]`, `--import-mode=importlib`, `asyncio_mode="auto"`, `pytest-asyncio`). Take upstream's pin bumps (`openalgo`, `starlette`, `idna`). **Bump the platform `version` manually** — take upstream's number, or our-higher if we forked the version. |
| **`app.py`** | fork boot wiring | **Hand-merge.** Until Isolation #1 lands, this is the painful one: keep every fork block (preflight bp, scanner DB init, csrf exempts, scanner pre-subscribe boot-retry, engine rehydration) **and** upstream's boot changes. Diff three ways (`git diff :1:app.py :2:app.py` ours, `:1 :3` theirs). After Isolation #1 this collapses to keeping one `wire_fork()` line. |
| **`CLAUDE.md`** | operator notes | **Hand-merge, keep both.** Our operational sections + any upstream additions. Never take-theirs (drops our learnings). |
| **fork-only services** | `simplified_stock_engine_*`, `preflight_service`, `scanner_*`, `sector_rotation_etf_*`, `bridge/`, `audit/` | **Will not conflict** (upstream has no copy). If one *does* conflict, upstream added a same-named file — investigate before resolving. |

### Take theirs vs ours vs hand-merge — the rule

- **Take theirs** when the file is upstream-owned and we have no semantic
  edits (all `frontend/**`, generated artifacts).
- **Take ours** rarely — only for a file we fully own that upstream
  accidentally re-added. Investigate first; a surprise upstream file usually
  means a feature collision.
- **Hand-merge** for every shared file with real fork edits: `app.py`,
  `pyproject.toml`, `CLAUDE.md`. Use a 3-way view and keep both intents.

### Database migration ordering

If upstream added Alembic/SQLAlchemy migrations, they may assume a column
order or table set that differs from our fork-added tables (`scanner_db`,
journal, etc.). After merging:

1. Diff `database/` init functions for any reordered `CREATE TABLE`.
2. On a **copy** of `db/*.db` (from the backup), boot the app and confirm
   no migration errors in `log/errors.jsonl`.
3. Only then point at the live DB. If a migration is destructive, defer the
   sync and raise with the operator.

## C. Post-sync verification (all must pass before PR → `main`)

- [ ] `make gate` clean (ruff smoke-scope + import smoke + engine/journal tests).
- [ ] `uv run python scripts/smoke_boot.py` — import smoke passes.
- [ ] `uv lock` clean; `uv sync` resolves with no conflicts.
- [ ] **Sector-rotation ETF dry-run:**
      `uv run python -m services.sector_rotation_etf_cli --asof <last-trading-day> --current-positions '{}'`
      emits recommended-orders JSON, no errors (read-only on `historify.duckdb`).
- [ ] **Simplified engine smoke** — boot in **sandbox** mode only, confirm
      engine status endpoint responds; do **not** flip to live.
- [ ] **No stale paths** — grep Cowork scheduled-task SKILL snapshots
      (`docs/skills/`) and `docs/SYSTEM_MAP.md` for any path upstream
      renamed; update in the same commit (docs-with-code rule).
- [ ] `git status --porcelain` clean except the intended merge.

Then: PR `sync/upstream-YYYY-MM-DD` → `main` as a **merge commit** (never
squash — preserves upstream attribution). After `main` absorbs it,
fast-forward `dev` from `main`.

## D. Log the sync

Append one entry to [`UPSTREAM_SYNC_LOG.md`](UPSTREAM_SYNC_LOG.md):

```
## 2026-MM-DD
- upstream: <sha-before> → <sha-after>
- fork base: <main-sha-before> → <main-sha-after>
- hand-merged: app.py, pyproject.toml, CLAUDE.md
- regenerated: uv.lock, frontend/dist (took theirs)
- notable: <dep bumps / migrations / anything that surprised you>
- verification: make gate ✓, smoke_boot ✓, sector CLI dry-run ✓
```

## E. Rollback

If post-sync verification fails and can't be fixed quickly: the merge lives
on the throwaway `sync/upstream-*` branch and has **not** touched `main`
yet, so just `git checkout main && git branch -D sync/upstream-YYYY-MM-DD`.
If you already merged to `main`, revert the merge commit
(`git revert -m 1 <merge-sha>`) — never force-push `main`. Restore `db/*.db`
from the backup if a migration ran.
