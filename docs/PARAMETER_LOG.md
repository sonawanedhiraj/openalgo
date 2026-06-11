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

### Strategy control — unified daily intent

#### STRATEGY_DAILY_INTENT_ENABLED
- **Current value:** `true` (default; ships hot)
- **Set in:** env var (not yet in `.sample.env` — operator WIP held that file;
  add `STRATEGY_DAILY_INTENT_ENABLED=true` there at next convenient edit). Read
  with a safe default in `services/mode_service.py:_flag_enabled` (default
  `true`).
- **Code default:** `true` (`services/mode_service._flag_enabled`)
- **What it gates:** when `true`, `resolve_strategy_mode(strategy_name)` consults
  the new `strategy_daily_intent` table (`db/openalgo.db`) first, then falls
  through to the legacy `daily_intent` table (simplified only) → env mode flag
  (`SIMPLIFIED_ENGINE_MODE` / `SECTOR_FOLLOW_CAP5_VOL_MODE`) → `sandbox/run`
  default. When `false`, the unified-row step is skipped (pure legacy behavior).
- **History:**
  - **2026-06-10:** Introduced with the unified `{mode, intent}` control surface
    (feat/sector-rotation-etf → `206a5d14`). Default `true`, but **deploy is a
    no-op**: with no `strategy_daily_intent` row for `(strategy, today)` the
    resolver falls through to each strategy's existing env/legacy behavior. The
    operator opts a strategy in by inserting a row (`set_intent`); rolls back by
    deleting it. Migration backfills legacy `daily_intent` rows into the unified
    table once at boot (idempotent, `updated_by='migration'`, `intent='run'`).
    `place_order_service`'s global `resolve_effective_mode()` floor is unchanged
    — the intent gate lives in the engines. Design:
    `docs/design/strategy_daily_intent.md`.
- **Related state:** `db/openalgo.db` → `strategy_daily_intent` table
  (`strategy_name`, `intent_date`, `mode` live/sandbox/skip, `intent`
  run/pause/halt, `daily_capital_cap`). Live env at time of ship:
  `SECTOR_FOLLOW_CAP5_VOL_MODE=sandbox`, `SIMPLIFIED_ENGINE_MODE=live`.
- **Test coverage:** `test/test_strategy_daily_intent.py` (flag-on/off,
  fall-through, migration), plus intent-gate tests in
  `test/test_sector_follow_service.py` and
  `test/test_simplified_stock_engine_service.py`.

#### TELEGRAM_INBOUND_ENABLED
- **Current value:** `false` (default; ships cold)
- **Set in:** env var (not yet in `.sample.env` — operator WIP held that file;
  add `TELEGRAM_INBOUND_ENABLED=false` there at next convenient edit). Read with
  a safe default in `services/telegram_inbound_service.py:_inbound_enabled`
  (default `false`).
- **Code default:** `false` (`services/telegram_inbound_service._inbound_enabled`)
- **What it gates:** when `true`, `init_telegram_inbound_service` (called at boot
  from `app.py`) starts the Phase-6 INBOUND Telegram poller and registers the
  08:45 IST `telegram_inbound_morning_prompt` APScheduler job. The bot lets the
  operator set the unified `strategy_daily_intent` row (run/pause/halt + capital
  cap) from the phone. When `false` (default) the whole module is a no-op — no
  poller, no scheduler job. **Mode flips are never exposed via Telegram** (intent
  axis + cap only); a Telegram intent change preserves the row's existing routing
  mode. Authorization gates on the `bot_config.telegram_chat_ids` allowlist.
- **History:**
  - **2026-06-10:** Introduced with the Phase-6 inbound bot
    (feat/sector-rotation-etf → `00737983`). Default `false` so deploy starts no
    poller; operator opts in by adding their chat_id to
    `bot_config.telegram_chat_ids` (or `add_authorized_chat_id`) and flipping the
    flag to `true`, then restarting. Single-poller-per-token caveat: do not run
    the full interactive `telegram_bot_service` poller on the same bot token
    while this is enabled. Design: `docs/design/telegram_inbound.md`.
- **Related state:** `db/openalgo.db` → `bot_config.telegram_chat_ids` (new column,
  comma-separated allowlist; idempotent ALTER-TABLE migration adds it) and the
  reused Fernet-encrypted `bot_config.token`; writes `strategy_daily_intent`.
- **Test coverage:** `test/e2e/test_critical_flows.py`
  (`TestTelegramInboundEndToEnd`, `TestChatAllowlist`).

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

### Scanner — EOD Chartink-vs-inhouse comparison job

#### SCANNER_COMPARISON_EOD_ENABLED
- **Current value:** `true` (default; ships hot)
- **Set in:** env var (not yet in `.sample.env` — operator WIP held that file;
  add at next convenient edit). Read with a safe default in
  `services/scanner_comparison_eod_service._eod_comparison_job`.
- **Code default:** `true`
- **What it gates:** the per-fire body of the `scanner_comparison_eod`
  APScheduler job (15:45 IST mon-fri). When `true`, the job computes the
  in-house-scanner-vs-Chartink comparison for the day, writes one
  `scanner_comparison` row per side, and Telegrams the verdict. When `false`,
  the job is registered but the body is a no-op (so flipping the flag needs only
  a restart, not a re-registration).
- **History:**
  - **2026-06-12:** Introduced with the EOD comparison job that retires the
    Cowork-side `scanner-vs-chartink-daily-comparison` scheduled task (which ran
    read-only but silently failed in the sandbox — no repo/folder access). The
    in-process job is durable: it persists a row AND Telegrams every trading day.

#### SCANNER_COMPARISON_EOD_TIME
- **Current value:** `15:45` (default)
- **Set in:** env var; read in
  `services/scanner_comparison_eod_service.register_jobs` at boot.
- **Code default:** `15:45` (matches the retired Cowork task's cron)
- **What it controls:** the `HH:MM` IST fire time of the `scanner_comparison_eod`
  cron job. Junk values fall back to the default. Changing it requires a restart
  (the trigger is built at registration).
- **History:**
  - **2026-06-12:** Introduced alongside `SCANNER_COMPARISON_EOD_ENABLED`.

#### NOTIFY_SCANNER_COMPARISON
- **Current value:** `true` (default)
- **Set in:** env var; snapshotted at `NotificationService` construction
  (`services/notification_service.py`), so a change needs a process restart.
- **Code default:** `true`
- **What it controls:** whether the `scanner_comparison` notification event is
  delivered to Telegram. When `false`, `notify("scanner_comparison", …)` no-ops
  (the DB row is still written; only the Telegram send is suppressed).
- **History:**
  - **2026-06-12:** Introduced with the EOD comparison job's Telegram summary.

### sector_follow_cap5_vol — strategy

#### SECTOR_FOLLOW_CAP5_VOL_MODE
- **Current value:** `sandbox` (operator `.env`; `.sample.env` still ships `scaffold` default)
- **Set in:** env; read in `services/sector_follow_service.py` (`SectorFollowService.__init__`)
- **Values:** `scaffold` | `sandbox` | `live`
  - `scaffold` (default): compute signals, log, write trade journal — **NO orders placed**
  - `sandbox`: orders routed to `db/sandbox.db` (virtual ₹1Cr)
  - `live`: real broker orders
  - Any unknown value force-falls-back to `scaffold` (logged WARNING).
- **Who flips:** **operator only** — the strategy ships scaffold; `sandbox`/`live` is a deliberate operator decision, never automated.
- **History:**
  - **2026-06-10 (Phase 1+2, merged `3266858f`):** Introduced with the SectorFollowService core + observability endpoints. Default `scaffold` so wiring the service into boot changes no live trading behavior.
  - **2026-06-10 (Phase 5 kickoff):** Operator flipped `scaffold → sandbox` in `.env` (not committed; `.env` is gitignored). Orders now route to `db/sandbox.db` (virtual ₹1Cr) — no live broker orders. First scheduled fire: 2026-06-11 15:20 IST. No engine config changed.

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

### Simplified engine — EOD watchdog timing

#### SIMPLIFIED_ENGINE_EOD_WATCHDOG_ENABLED
- **Current value:** unset → code default `true`
- **Set in:** env; read in `services/eod_watchdog_service.py.start_eod_watchdog`
  (via local `_env_bool`)
- **Values:** `true` / `false` (any value other than `1/true/yes/on`, case-insensitive, disables)
- **Effect:** master on/off switch for the APScheduler EOD watchdog (the
  tick-independent backstop that flattens open `trade_journal` rows at end of day
  via `place_order`). When `false`, `start_eod_watchdog` returns early and
  registers no jobs (app boot logs the disable). When `true` (default), one daily
  mon-fri job is registered per intraday strategy. Belt to the tick-driven
  `_maybe_flatten_eod` and the 15:30 reconciliation.
- **Who flips:** operator only (leave `true` — disabling re-opens the
  stranded-position risk the watchdog exists to cover).
- **History:**
  - **2026-06-11:** Introduced alongside the fire-time cap. Default `true`.

#### SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME
- **Current value:** unset → code default `15:14` (IST, `HH:MM`)
- **Set in:** env; read in `services/eod_watchdog_service.py.start_eod_watchdog`
- **Values:** `HH:MM` 24h IST. Invalid values log an error and fall back to `15:14`.
- **Effect:** caps each strategy's watchdog fire time. The job fires at
  `min(strategy.eod_exit_time, SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME)` — it honors an
  earlier declared cut-off but never runs later than the cap. The default `15:14`
  is deliberately **one minute before** the 15:15 sandbox/broker MIS
  auto-square-off: sandbox *rejects* MIS orders placed at/after 15:15, so the old
  behavior of firing at the declared `eod_exit_time` (15:20) was always too late
  and stranded positions (the 2026-06-10 OIL/HINDZINC/TATAELXSI orphans, only
  recovered by the 15:30 reconciliation). **Do not set ≥15:15.**
- **Who flips:** operator only.
- **History:**
  - **2026-06-11:** Introduced. Default `15:14` — fixes the 15:20 → post-square-off
    race for the simplified engine's intraday EOD flatten.

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

### Preflight — recent-errors gate noise immunity

#### PREFLIGHT_REQUIRE_PRODUCTION_LOGGER
- **Current value:** unset → code default `false`
- **Set in:** env; read in
  `services/preflight_service.py._count_recent_errors` (via `_env_bool`)
- **Values:** `true` / `false` (default `false`)
- **Effect:** opt-in defense-in-depth for the `recent_errors` preflight gate
  (Failure 4, 2026-06-11). When `true`, an errors.jsonl entry is counted toward
  the abort threshold only if its `logger` field names a known OpenAlgo
  production namespace (`_PRODUCTION_LOGGER_PREFIXES`: services, blueprints,
  database, broker, restx_api, websocket_proxy, sandbox, utils, app, …). An entry
  with a present-but-non-production logger is treated as noise and ignored; an
  entry with no logger field is still counted (real prod entries always carry a
  logger). This makes a pytest-polluted errors.jsonl unable to brick preflight
  even if test DB isolation regresses. When `false` (default) the gate behaves
  exactly as before — every non-test-origin ERROR counts.
- **Caveat:** some legitimate prod errors log under non-namespace names (e.g.
  `zerodha_websocket`); enabling this trades catching those against stronger
  noise immunity. Leave `false` unless a pollution incident recurs.
- **Who flips:** operator only.
- **History:**
  - **2026-06-11:** Introduced. Default `false`. (`.sample.env` doc line deferred —
    that file was operator WIP at commit time; add the documented default there in
    a follow-up.)

> Note: the separator-agnostic fix in the same gate (Windows `\test\` traceback
> paths now match the `test/` marker) is **not** a tunable — it is an always-on
> correctness fix in `_is_test_source_entry`, so it has no PARAMETER_LOG knob.

## Other tunables (placeholder — populate as discovered)

The following are known tunables that should be cataloged in subsequent commits
as they're touched:
- `SIMPLIFIED_ENGINE_MODE` (sandbox / live / disabled)
- `SIMPLIFIED_ENGINE_*` parameters (ATR mult, max trades, cooldown, etc.)
- `OPENALGO_BOOT_DIRTY_CHECK_ENABLED` (default True)
- Sector rotation ETF params: `capital_inr`, `mode`, `deployable`, window times
- Various others in `.sample.env`

This list is not exhaustive — add entries as you touch parameters.
