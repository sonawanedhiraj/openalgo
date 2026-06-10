# Parameter Log

Canonical history of tunable parameters across the system. Every parameter change
(env var, DB row, config flag, threshold default) MUST get an entry here in the
same commit that makes the change.

**This file lives on `dev` and is updated by direct commits — never via feature
branches.** This guarantees every fresh branch and every spawned task inherits
the latest decisions automatically.

## How to use this file

- **Before changing any parameter:** add the entry here in the same commit
- **Before any parameter-dependent work:** read this file AND verify against `.env`
  (or the DB row, or wherever the parameter lives). The doc records intent; the
  live source records reality. Mismatches are real and must be resolved.
- **Spawned tasks:** include "read PARAMETER_LOG before parameter work" in the brief

## Active parameters

### Scanner — Chartink BUY rule

#### CHARTINK_RULE_BUY_GAP_PCT
- **Current value:** `1.5` (1.5% gap-up vs previous daily close)
- **Set in:** `.env` line `CHARTINK_RULE_BUY_GAP_PCT=1.5`
- **Code default:** `3.0` in `services/scan_rules/fno_intraday_buy_chartink.py:113`
- **History:**
  - **2026-06-?? (verified 2026-06-09):** Operator lowered to 1.5 from 3.0 default. Reason: collect more signal data to validate the rule on a wider historical window. The 3.0 default in code matches the original Chartink screener formula; .env override is the working value.
- **Related state:** `db/openalgo.db scan_definitions.id=1.rule_module = fno_intraday_buy_chartink` (set 2026-06-09; was `fno_intraday_buy_20` placeholder)
- **Test coverage:** `test/test_fno_intraday_buy_chartink.py` covers both 1.5 and 3.0 thresholds via monkeypatch

### Scanner — Chartink SELL rule

#### scan_definitions.id=2.rule_module
- **Old value:** `fno_intraday_sell_20` (placeholder rule)
- **New value:** `fno_intraday_sell_chartink`
- **Set in:** `db/openalgo.db scan_definitions.id=2.rule_module` (DB row, not env)
- **Date:** 2026-06-10 (post-close, ~17:08 IST)
- **Why:** Today's scanner-vs-Chartink comparison showed the in-house SELL leg
  fired on 209 of ~220 F&O stocks vs Chartink's 5 (Jaccard 0.024) — the
  `fno_intraday_sell_20` placeholder is far too lenient. Swap to the
  Chartink-equivalent mirror rule `fno_intraday_sell_chartink`. Mirror of the
  BUY-side fix applied this morning (id=1 → `fno_intraday_buy_chartink`).
- **Effective:** immediately. `ScannerService._evaluate_definitions`
  (`services/scanner_service.py:901`) calls `get_scan_definitions(enabled_only=True)`
  on every bar evaluation, and `get_scan_definitions` opens a fresh DB session
  per call (`scanner_service.py:199`) — no boot cache. Rule
  `fno_intraday_sell_chartink` is registered (verified via `get_rule`). No restart
  required.

### Scanner — legacy `_20` rule files removed

#### services/scan_rules/fno_intraday_{buy,sell}_20.py
- **Change:** removed (file deletion). Dropped the two import lines from
  `services/scan_rules/__init__.py` so the package no longer registers them.
- **Date:** 2026-06-10 (post-close)
- **What:** deleted `services/scan_rules/fno_intraday_buy_20.py` and
  `services/scan_rules/fno_intraday_sell_20.py` (the lenient placeholder rules:
  volume surge ≥2× 20-bar avg + close vs 20-EMA).
- **Why:** both were replaced earlier today by their Chartink-mirror equivalents
  (`fno_intraday_buy_chartink` / `fno_intraday_sell_chartink`) and the live DB
  `scan_definitions.id=1/2.rule_module` no longer points at either (see the BUY
  and SELL rule entries above). The dead files were a source of confusion — a
  registered-but-unused rule that looked active. No other production code
  imported them (only the `scan_rules` package self-registration).
- **Test coverage:** the chartink mirrors keep their dedicated tests
  (`test/test_fno_intraday_{buy,sell}_chartink.py`). `test/test_scanner_service.py`
  was decoupled to use self-contained test rules instead of the deleted `_20`
  rules; `test/test_scan_rules.py` now covers only generic registry mechanics.
- **Backout plan:** revert this commit — the rule files remain in git history at
  their last commit on `dev`.

### sector_follow_cap5_vol — strategy

#### SECTOR_FOLLOW_CAP5_VOL_MODE
- **Current value:** `scaffold` (`.sample.env` line `SECTOR_FOLLOW_CAP5_VOL_MODE=scaffold`)
- **Set in:** env; read in `services/sector_follow_service.py` (`SectorFollowService.__init__`)
- **Values:** `scaffold` | `sandbox` | `live`
  - `scaffold` (default): compute signals, log, write trade journal — **NO orders placed**
  - `sandbox`: orders routed to `db/sandbox.db` (virtual ₹1Cr)
  - `live`: real broker orders
  - Any unknown value force-falls-back to `scaffold` (logged WARNING).
- **Who flips:** **operator only** — the strategy ships scaffold; `sandbox`/`live` is a deliberate operator decision, never automated.
- **History:**
  - **2026-06-10 (Phase 1+2, merged `3266858f`):** Introduced with the SectorFollowService core + observability endpoints. Default `scaffold` so wiring the service into boot changes no live trading behavior.

#### config_snapshot.json (locked Phase-0.5 decisions)
- **File:** `strategies/sector_follow_cap5_vol/config_snapshot.json` — canonical source for the strategy's non-env tunables. Loaded by `load_config()`; the `SectorFollowConfig` dataclass mirrors it.
- **Locked values:** `capital_inr` 250000, `max_position_inr` 50000, `max_concurrent_positions` 5, `daily_loss_kill_pct` 3.0, `cooldown_days` 0, entry/exit window 15:20–15:25 IST, daily reset 09:00 IST, gates (sector >1.0%, stock >0.5%, vol >1.0×20d), tiebreaker `volume_ratio_desc`, universe `LOCK_STATIC_30` (30 names), `mode: scaffold-only`, `deployable: false`.
- **Who changes:** operator, recorded in `strategies/sector_follow_cap5_vol/VERSION_LOG.md`.

#### SECTOR_FOLLOW_INDEX_BACKFILL_ENABLED
- Env var (default `true`) gating the daily 16:05 IST sector-index 1m refresh job. Introduced on the Phase 3 branch — full entry lands with that merge.
### sector_follow_cap5_vol — sector-index 1m refresh

#### SECTOR_FOLLOW_INDEX_BACKFILL_ENABLED
- **Current value:** unset → code default `true`
- **Set in:** env (not in `.sample.env`; read in `services/historify_scheduler_service.py._register_sector_follow_index_job`)
- **Values:** `true` / `false` (any value other than `true`, case-insensitive, disables)
- **Effect:** gates registration of the daily 16:05 IST `sector_follow_index_backfill` APScheduler job, which keeps the strategy's mapped sector-index 1m feed fresh in `db/historify.duckdb`. Disabling it leaves the index feed to go stale → the 15:20 signal fails-closed at the sector gate (no entries).
- **Who flips:** operator only.
- **History:**
  - **2026-06-09 (Phase 3):** Introduced with the sector-index feed wiring (`feat/sector_follow_cap5_vol_phase3`, commit `3bfa4a08`). Default `true` so a fresh deploy keeps the feed current without extra config.

### Data-freshness validation (sector_follow_cap5_vol)

#### DATA_FRESHNESS_VALIDATION_ENABLED
- **Current value:** unset → code default `true`
- **Set in:** env; read in `services/sector_follow_service.py.data_freshness_enabled()`
- **Values:** `true` / `false` (any value other than `true`, case-insensitive, disables)
- **Effect:** master switch for the freshness layer — the daily 16:30 IST
  `sector_follow_data_health` APScheduler job (alert + auto-pause on stale data),
  the pre-entry gate in `run_entry` (aborts entries on stale data), and the
  exit-job staleness warning. When `false`, all three are no-ops (pure legacy
  behavior). The `/sector_follow_cap5_vol/api/data_health` endpoint always works
  (it just queries, never gates).
- **Who flips:** operator only.
- **History:**
  - **2026-06-10:** Introduced after the 2026-05-29→06-10 index-feed staleness
    incident (the daily index backfill job did not exist until that day's Phase 3
    commit, so the feed silently sat 12 days stale). Default `true` — ships hot,
    behavior additive (read + alert; auto-pause only on confirmed staleness).

#### MAX_STALENESS_BUSINESS_DAYS
- **Current value:** unset → code default `1`
- **Set in:** env; read in
  `services/data_freshness_service.py.default_max_staleness_business_days()`
- **Values:** non-negative integer. `1` == "yesterday's close is acceptable" (the
  realistic state at 15:20 IST, before today's after-close backfill runs);
  day-before-yesterday is stale.
- **Effect:** the per-symbol staleness threshold (business days behind the
  reference trading day) above which a symbol is flagged stale. Weekend-aware;
  market holidays are NOT modelled (a mid-week holiday inflates measured staleness
  by one business day — the default-1 threshold absorbs the common case).
- **Who flips:** operator only.
- **History:**
  - **2026-06-10:** Introduced with `DATA_FRESHNESS_VALIDATION_ENABLED`. Default 1.

### Simplified engine — EOD journal reconciliation

#### ENGINE_EOD_RECONCILIATION_ENABLED
- **Current value:** unset → code default `true`
- **Set in:** env; read in
  `services/simplified_stock_engine_service.py.SimplifiedStockEngineService._maybe_reconcile_eod_journal`
  (via `_env_bool`)
- **Values:** `true` / `false` (any value other than `true`, case-insensitive, disables)
- **Effect:** master switch for the EOD reconciliation step. When `true` (and the
  engine is in `sandbox` mode), the engine — right before it fires the Telegram
  EOD summary — calls
  `services/engine_eod_reconciliation_service.reconcile_engine_journal(today)`,
  which closes any open `trade_journal` row whose sandbox position was already
  flattened by sandbox's MIS auto-square-off (writing the missing exit row with
  `exit_reason='sandbox_eod_squareoff'`). When `false`, the step is a no-op and
  the journal under-reports square-off closures (the 2026-06-10 bug). Read-only on
  `sandbox.db`; idempotent. No effect outside sandbox mode (live/disabled skip it).
- **Who flips:** operator only (rollback lever — leave `true` for correct Telegram
  EOD counts).
- **History:**
  - **2026-06-11:** Introduced. Default `true`.

## Other tunables (placeholder — populate as discovered)

The following are known tunables that should be cataloged in subsequent commits
as they're touched:
- `SIMPLIFIED_ENGINE_MODE` (sandbox / live / disabled)
- `SIMPLIFIED_ENGINE_*` parameters (ATR mult, max trades, cooldown, etc.)
- `OPENALGO_BOOT_DIRTY_CHECK_ENABLED` (default True)
- Sector rotation ETF params: `capital_inr`, `mode`, `deployable`, window times
- Various others in `.sample.env`

This list is not exhaustive — add entries as you touch parameters.
