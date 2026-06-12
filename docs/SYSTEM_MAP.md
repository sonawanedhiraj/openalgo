# OpenAlgo System Map

Single-source overview of all processes, scheduled tasks, logs, databases, and
inter-component side effects in this deployment. Read at the start of any
session that involves diagnostics, mid-market changes, or unexpected behavior.

> **Golden rule for diagnostics:** when something looks wrong mid-market, read
> the scheduled-task session transcripts **first**, `errors.jsonl` **last**.
> The `fno-scan-cycle` task can run pytest, mutate code, and restart OpenAlgo
> silently — those side effects masquerade as engine faults in `errors.jsonl`.

## Processes

### 1. OpenAlgo Flask app (port 5000)
- **Entry:** `app.py` (`uv run app.py` dev; `gunicorn --worker-class eventlet -w 1 app:app` prod)
- **DBs written:** `db/openalgo.db`, `db/logs.db`, `db/sandbox.db`,
  `db/historify.duckdb`, `db/latency.db`, `db/health.db`
- **Logs:** `log/openalgo_YYYY-MM-DD.log` (text, if `LOG_TO_FILE=True`),
  `log/errors.jsonl` (structured ERROR+, always on)
- **Boot sequence:** imports ~22 `init_db()` functions (`app.py:90-114`) → multi-DB
  table init → master-contract load → scanner-history warm-up thread
  (`app.py:842-851`, gated by `SCANNER_HISTORY_WARMUP_ENABLED`) → WS subscribe →
  "Ready" banner. Boot logs a WARNING if `git status --porcelain` is non-empty
  (`OPENALGO_BOOT_DIRTY_CHECK_ENABLED`, default true).
- **Side effects on restart:** clears in-memory positions/stops/EOD timer; broker
  WS often does not resume cleanly; triggers a ~3-second SQLite "database locked"
  burst (~180 errors) during the multi-DB init.
- **Manage via:** `uv run app.py`, or bridge `POST /restart-app`.

### 2. Bridge FastAPI (port 5001)
- **Entry:** `bridge/server.py` (`uv run python bridge/server.py`)
- **Endpoints + side effects:**

| Endpoint | Method | Side effect |
|---|---|---|
| `/fix-bug` | POST | Spawns Claude Code subprocess → prompt runs `uv run pytest test/ -v` (FULL SUITE — `server.py:427`) → **may mutate any file** |
| `/run-tests` | POST | Spawns Claude Code subprocess → also runs `uv run pytest {test_target} -v` (`server.py:449,456`) |
| `/restart-app` | POST | Kills PID on port 5000 via PowerShell `Stop-Process -Force` → respawns `uv run app.py` (`server.py:494-516`) |
| `/run` | POST | Arbitrary Claude Code prompt — may mutate files |
| `/review-signal`, `/reflect` | POST | LLM calls; review/journal helpers. `/review-signal` candidate now carries an explicit `direction` (`BUY`/`SELL`) so the veto prompt frames the side correctly instead of inferring it from the `source` string |
| `/status`, `/read-errors`, `/engine-status` | GET | Read-only |

- **Busy lock:** all task endpoints 409 if `state.status == BUSY`. A wedged task
  (e.g. a hung restart on Windows) leaves the bridge permanently busy — see
  memory `bridge-restart-app-hangs-windows`.
- **Logs:** `log/bridge_stderr.log` — **UNRELIABLE** (may show a stale mtime even
  after recent calls; not every invocation reaches it).
- **Pollution risk:** `/fix-bug` + `/run-tests` pytest runs write to the SHARED
  `log/errors.jsonl` and hit localhost (polluting `db/logs.db` traffic) unless
  conftest isolation kicks in. Has caused 300-400 error storms that lock preflight
  45+ min.

### 3. Cowork scheduled tasks (host-side, NOT in OpenAlgo)
- **Configured in:** Cowork app via SKILL.md files at
  `C:\Users\Dheeraj\OneDrive\Documents\Claude\Scheduled\<name>\SKILL.md`
  (tracked snapshots under `docs/skills/`).
- **Inspect via:** `mcp__scheduled-tasks__list_scheduled_tasks` and
  `mcp__session_info__list_sessions` / `read_transcript`.
- **These run read-only on repo code** by policy — they append to
  `audit/proposed_fixes.jsonl` instead of editing source (see `audit/README.md`).
  The exception is `fno-scan-cycle` step 6, which **calls the bridge** (which is
  not bound by that policy).
- **Active tasks** (verify current state — list may drift):

| Task | Cron | Side effects |
|---|---|---|
| `fno-scan-cycle` | `*/15 9-16 * * 1-5` (every 15 min, market hrs) | Scans Chartink → POSTs engine webhook → **step 6 calls bridge `/fix-bug` → can run full pytest + restart OpenAlgo mid-market** |
| `scanner-vs-chartink-daily-comparison` | `45 15 * * 1-5` (15:45 IST) | **RETIRED 2026-06-12** — moved in-process to the `scanner_comparison_eod` APScheduler job (§ In-process jobs). Operator should disable the Cowork task. It silently failed in the sandbox anyway (no repo/folder access) |
| `daily-trading-pipeline` | `30 9 * * 1-5` | DISABLED (deprecated) |

### 4. SectorFollowService (in-process, OpenAlgo eventlet worker)
- **Entry:** `services/sector_follow_service.py` — built + wired at boot by
  `init_sector_follow_service(app, scheduler)` (called from `app.py`). Lives inside
  the single OpenAlgo worker; it is **not** a separate process or a Cowork host task.
- **Mode flag:** env `SECTOR_FOLLOW_CAP5_VOL_MODE` = `scaffold` (default) | `sandbox`
  | `live`. **`scaffold` places NO orders** — it computes signals, logs, and writes
  the trade journal only. `sandbox` routes to `db/sandbox.db`; `live` places real
  broker orders. An unknown value force-falls-back to `scaffold`.
- **Registers 4 APScheduler jobs** on the shared scheduler (all `mon-fri`
  `Asia/Kolkata`, `replace_existing`):

  | Job id | Cron (IST) | What it does |
  |---|---|---|
  | `sector_follow_entry` | 15:20 | Evaluate 30-name universe, select ≤5 gate-passers (vol-ratio tiebreaker), place/paper BUYs (mode-aware; honors kill switch + manual pause) |
  | `sector_follow_exit` | 15:25 | Square off every position opened on a prior trading day (T+1 exit). Exits are **never** blocked by the kill switch |
  | `sector_follow_daily_reset` | 09:00 | Clear kill switch + daily P&L + intraday journals (manual pause persists) |
  | `sector_follow_eod_summary` | 15:30 | Best-effort Telegram EOD summary (silent if TG off) **+** writes a Day-N markdown report to `strategies/sector_follow_cap5_vol/eod_reports/YYYY-MM-DD.md` (independent sinks — one failing never blocks the other) |
  | `sector_follow_data_health` | 16:30 | **Market-data freshness check** (after the 16:05 index backfill should have landed). Validates the 8 sector indices + 30 universe stocks via `data_freshness_service.check_strategy_data_ready`; writes a `data_health_check` row. On stale data: Telegram-alerts the operator **and** auto-pauses tomorrow's `strategy_daily_intent` (`updated_by='data_health:auto-pause'`, operator-overridable). Gated by `DATA_FRESHNESS_VALIDATION_ENABLED` (default `true`) |

- **Pre-entry freshness gate:** `run_entry` aborts (places no orders, alerts) when
  the index OR stock feed is stale beyond `MAX_STALENESS_BUSINESS_DAYS` (default 1).
  `run_exit` only *warns* on stale index data — exits are never blocked. Both gated
  by `DATA_FRESHNESS_VALIDATION_ENABLED`.
- **Kill switch:** trips when day P&L < −`daily_loss_kill_pct`% of capital (default 3%);
  blocks new entries for the session, open positions still run to their T+1 exit.
- **DBs written:** `db/openalgo.db` → `sector_follow_trades` (trade journal, all
  modes), `strategies` (one seeded row, natural key `name='sector_follow_cap5_vol'`),
  `data_health_check` (one row per 16:30 freshness check), and
  `strategy_daily_intent` (tomorrow's auto-pause row on stale data).
- **File output:** `strategies/sector_follow_cap5_vol/eod_reports/YYYY-MM-DD.md` —
  one markdown file per trading day, written by the 15:30 IST `sector_follow_eod_summary`
  APScheduler job. Mirrors the Telegram EOD summary content (date/mode, signals,
  capital deployed, P&L, sector breakdown, per-position table, kill-switch state).
  Git-ignored (observational, not source); path hardcoded (no env var).
- **Logs:** standard `log/openalgo_YYYY-MM-DD.log` + `log/errors.jsonl` (no
  dedicated log file).
- **Control API:** see "Strategy control endpoints" below.
- **Status:** scaffold-only, `deployable: false` — see
  `strategies/sector_follow_cap5_vol/PLAN.md`. A companion 16:05 index-refresh job
  (added on the Phase 3 branch) keeps its sector-index 1m feed fresh.
## In-process APScheduler jobs (OpenAlgo worker)

These cron jobs run **inside** the single eventlet worker on the shared
APScheduler instance (`services/historify_scheduler_service.py`). They are NOT
Cowork host tasks (§3 above) — they live and die with the OpenAlgo process and
need no external scheduler.

| Job id | Cron (IST) | What it does | Gating / writes |
|---|---|---|---|
| `sector_follow_index_backfill` | `5 16 * * 1-5` (16:05, after close) | 1m backfill of the sector indices mapped in `strategies/sector_follow_cap5_vol/sector_map.json` (+ 2 defensive 1m-missing indices), so the strategy's 15:20 signal reads a fresh index feed rather than a stale one. Incremental, 4-day lookback; self-heals a missed run/weekend. Additive — routes through the same `historify_service.create_and_start_job` pipeline as the stock backfill, never touching watchlist schedules. | Gated by env `SECTOR_FOLLOW_INDEX_BACKFILL_ENABLED` (default `true`); writes 1m bars to `db/historify.duckdb` `market_data`. Body: `services/sector_follow_index_backfill.refresh_sector_follow_indices` (registered by `_register_sector_follow_index_job`). One-shot CLI: `uv run python -m services.sector_follow_index_backfill --from YYYY-MM-DD --to YYYY-MM-DD`. |
| `scanner_comparison_eod` | `45 15 * * 1-5` (15:45 IST) | **In-house-scanner-vs-Chartink EOD comparison** — the in-process replacement for the retired Cowork `scanner-vs-chartink-daily-comparison` task (§3). For today: unions the Chartink BUY/SELL webhook lists (`scan_cycle`, `cycle_kind='chartink'`) and the in-house scanner hits (`scan_results`, `source='inhouse'`, grouped by `scan_definition.screener_type`), computes per-side counts/intersection/Jaccard/recall + a tuning verdict, writes one `scanner_comparison` row per side (idempotent delete-then-insert per `(date, side)`), and Telegrams the summary via `notify()`. Read-only on every DB except its own table. | Per-fire gate env `SCANNER_COMPARISON_EOD_ENABLED` (default `true`); fire time env `SCANNER_COMPARISON_EOD_TIME` (default `15:45`); Telegram toggle `NOTIFY_SCANNER_COMPARISON` (default `true`). Body: `services/scanner_comparison_eod_service._eod_comparison_job` (registered by `init_scanner_comparison_eod_service`). |
| `telegram_inbound_morning_prompt` | `45 8 * * 1-5` (08:45, pre-open) | Sends the morning intent prompt (inline Run/Pause/Halt keyboard per strategy) to every allowlisted chat_id, so the operator can set today's intent from the phone. No-op when the inbound bot isn't running. | Registered ONLY when `TELEGRAM_INBOUND_ENABLED=true`; body `services/telegram_inbound_service._morning_prompt_job`. See the Telegram inbound bot process entry below. |
| `eod_watchdog_<strategy>` | `mon-fri` at `min(strategy.eod_exit_time, SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME)` — default **15:14** for `trending_equity_intraday` | Safety-net EOD flatten for the simplified engine. One cron job per registered intraday strategy; calls `flatten_strategy_positions` (open `trade_journal` rows → opposite-side MARKET via `place_order`, mode-aware sandbox/live). Backstop for the tick-driven `_maybe_flatten_eod`, which can't fire when the broker tick stream dies before close. **Fires at 15:14, one minute before the 15:15 sandbox/broker MIS auto-square-off** — the cap is the 2026-06-10 fix: the watchdog used to fire at the declared 15:20, *after* sandbox had force-closed and started rejecting flatten orders, stranding OIL/HINDZINC/TATAELXSI. Belt to the 15:30 EOD reconciliation suspenders. | Runs on a **dedicated `BackgroundScheduler`** (not the shared instance), `services/eod_watchdog_service.py`. Gated by env `SIMPLIFIED_ENGINE_EOD_WATCHDOG_ENABLED` (default `true`); cap via `SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME` (default `15:14`). `misfire_grace_time=300`. Started from `app.py` boot after journal rehydrate. |

> The `sector_follow_cap5_vol` strategy also registers its own entry/exit/reset/
> EOD jobs on this same scheduler — see the SectorFollowService process entry.

### Telegram inbound intent bot (Phase 6)

- **Process:** `services/telegram_inbound_service.py` — a `python-telegram-bot`
  poller running on a **real OS thread** with its own asyncio event loop (same
  eventlet-bypass pattern as `telegram_bot_service`). Started from `app.py` boot
  ONLY when `TELEGRAM_INBOUND_ENABLED=true` (default `false` → no-op on deploy).
- **What it does:** polls Telegram for operator commands, gates on the
  `bot_config.telegram_chat_ids` allowlist, and writes the unified
  `strategy_daily_intent` table (`run`/`pause`/`halt` + capital cap). It is the
  INBOUND counterpart to the send-only outbound bot. **Mode flips are not
  exposed** (laptop-only); intent changes preserve the existing routing mode.
  Audit trail: `updated_by=telegram:<chat_id>:<message_id>`.
- **Single poller per token:** Telegram permits one `getUpdates` consumer per bot
  token — do not run the full interactive `telegram_bot_service` poller on the
  same token while this is enabled.
- **DB written:** `db/openalgo.db` → `strategy_daily_intent` (+ reads
  `bot_config`). A lightweight idempotent migration adds the
  `bot_config.telegram_chat_ids` column on older DBs.
- **Design:** [`docs/design/telegram_inbound.md`](design/telegram_inbound.md).

### Simplified-engine EOD journal reconciliation

- **Module:** `services/engine_eod_reconciliation_service.py`
  (`reconcile_engine_journal(date=None, *, strategy_name, dry_run)`).
- **Why:** the engine only writes a `trade_journal` exit row when *it* fires an
  exit (stop/target/trailing/its own EOD flatten). Positions still open at the
  close are flattened by **sandbox's own MIS auto-square-off**, which the engine
  never journaled — so the Telegram EOD summary under-counted trades and P&L
  (confirmed 2026-06-10: 4 entries, 1 journaled exit, 3 invisible square-offs;
  +₹352 shown vs +₹8,327 real).
- **What it does:** for each open journal row on the day, reads `sandbox.db`
  (`sandbox_positions` flat-check + `sandbox_trades` closing fills, **read-only**)
  and stamps the matching exit columns on the open row with
  `exit_reason='sandbox_eod_squareoff'` and gross P&L. Multiple partial close
  fills are summed into one exit row (qty-weighted avg price). Idempotent (the
  `exited_at IS NULL` filter is the dedup key); mid-day safe (skips non-flat
  positions); strategy-scoped so T+1/positional rows are never force-closed.
- **Ordering (load-bearing):** the engine's `_maybe_log_eod_summary` calls
  `_maybe_reconcile_eod_journal(today)` **first**, then reads the journal
  aggregate and fires the Telegram EOD summary — so reconcile → summarize, and an
  all-square-off day (empty in-memory ledger) still summarizes from the journal.
- **Flag:** `ENGINE_EOD_RECONCILIATION_ENABLED` (default `true`; sandbox-mode
  only) — see `docs/PARAMETER_LOG.md`.
- **Backfill (operator-run, not wired):**
  `services/engine_eod_reconciliation_backfill.py` runs reconciliation over a
  date range; **dry-run by default**, writes only with `--apply`.

### E2E test suite

- `test/e2e/test_critical_flows.py` — cross-component seam tests (mode resolution
  fall-through, the unified intent gate as the engines read it, the sector_follow
  entry→exit cycle + kill switch + EOD file sink, and the Phase-6 Telegram inbound
  bot end-to-end). The DB layer is real but bound to a temp SQLite (no production
  DB touched); broker/Telegram boundaries are mocked. Run: `uv run pytest test/e2e/ -v`.
- `test/e2e/test_fno_flows.py` — simplified-engine FnO + LLM veto critical flows
  (21 tests): BUY/SELL breakout→sandbox order, journal entry/exit pairing, veto
  shadow-vs-active enforcement, **veto direction consistency** (the TATAELXSI
  regression anchor — now PASSING after the 2026-06-11 fix that passes
  `signal.action` through as an explicit `direction` kwarg; the SELL-reviewed-as-BUY
  bug is closed), ATR stop, RR trailing, daily kill switch, trade-limit
  and cooldown gates, EOD square-off, and the Telegram EOD-summary semantics
  (gross / realized / closed-only — the anchor for the Telegram-vs-`/mypnl`
  mismatch; the Telegram line is now self-describing: "Realized (closed, gross,
  simplified-engine only) … see /mypnl for net account P&L"). Same hermetic pattern (temp/in-memory SQLite, mocked broker + veto,
  injected clock, no network). Investigation: `outputs/fno_eod_veto_investigation_2026-06-10/`.
- `test/e2e/test_engine_eod_reconciliation.py` — EOD reconciliation (8 tests):
  engine-exit no-op, sandbox square-off journaled, the full 2026-06-10 mixed-day
  scenario (1 engine exit + 3 square-offs → 4 trades, correct total P&L),
  idempotency, mid-day still-open no-op, multiple partial close fills summed into
  one exit row, orphan-fill (no entry created), and past-date backfill. Both
  `trade_journal_db` and `sandbox_db` rebound to temp SQLite — fully hermetic.

## Databases

| DB | Holds | Notes |
|---|---|---|
| `db/openalgo.db` | users, orders, positions, settings, **scan_cycle** (canonical Chartink fire history), strategies, **trade_journal** (one row per round trip; `ltp_at_signal` REAL holds the decision-time LTP for slippage analysis, added 2026-06-07 via boot-time `ALTER TABLE` in `trade_journal_db.init_db`), **sector_follow_trades** (sector_follow_cap5_vol journal — one row per entry/exit in all modes; created idempotently by `database/sector_follow_db.init_db`), **daily_intent** (legacy simplified-engine per-day intent, still read), **strategy_daily_intent** (unified per-strategy `{mode, intent, daily_capital_cap}` control surface keyed `(strategy_name, intent_date)`; created by `database/strategy_daily_intent_db.init_db`; legacy `daily_intent` rows backfilled into it at boot via `migrate_legacy_daily_intent`; read via `services/mode_service.resolve_strategy_mode`), **strategy_mode** (mode-only architecture: the single *persistent* per-strategy operator control — `{strategy_name PK, mode ∈ {live, sandbox} default sandbox, updated_at, updated_by, notes}`; created by `database/strategy_mode_db.init_db`; backfilled from the latest `strategy_daily_intent` row per strategy by `scripts/migrate_strategy_daily_intent_to_strategy_mode.py` (drops the intent/cap axes; legacy `mode='skip'` → `sandbox`); read via `services/mode_service.resolve_mode`; supersedes the `strategy_daily_intent` `mode` column — the intent/pause/halt axis is being moved to a separate self-expiring `strategy_runtime_override` table for automated safety guards), **data_health_check** (daily market-data freshness verdicts per strategy — `check_at`, `overall_ok`, `stale_symbols` JSON, `details_json`, `alert_sent`; created by `database/data_health_db.init_db`; written by the 16:30 IST `sector_follow_data_health` job), **signal_decision** (Stage-1 LLM veto-layer audit — one row per candidate review; `direction` TEXT column (`BUY`/`SELL`, nullable) records the side the engine armed, added 2026-06-11 via idempotent boot-time `ALTER TABLE` in `signal_decision_db._migrate_add_direction_column`; previously the side was unrecoverable because the chartink `source` string carries "buy" for both legs), **scanner_comparison** (daily in-house-scanner-vs-Chartink parity verdict — one row per `(date, screener_side)`: `inhouse_count`, `chartink_count`, `intersection_count`, `jaccard`, `ratio`, `false_positives_json`, `false_negatives_json`, `tuning_suggestion`, `telegram_sent`; created by `database/scanner_comparison_db.init_db`; written by the 15:45 IST `scanner_comparison_eod` job; idempotent delete-then-insert per date+side) | Main DB. Pooling: `NullPool` |
| `db/logs.db` | `traffic_logs` (HTTP request log) | Polluted by pytest hitting localhost |
| `db/latency.db` | latency monitoring | `NullPool` |
| `db/health.db` | health monitoring | `NullPool` |
| `db/sandbox.db` | sandbox trading (₹1 Cr virtual capital) | Engine default target; isolated from live. Auto square-off at exchange close |
| `db/historify.duckdb` | historical OHLC market data (`market_data`); **`fo_bhavcopy_eod`** = expired-contract F&O option EOD recovered from NSE bhavcopy | DuckDB, not SQLite |

`fo_bhavcopy_eod` (cols: trade_date, symbol, expiry, strike, option_type, OHLC,
settle, volume, oi, lot_size, source) is a **research/backtest artifact**, not
written by the Flask app. Backfilled offline from NSE bhavcopy (UDiFF ≥2024-07-06,
legacy before) by `outputs/r29v2_options_hybrid_2026-06-07/phase1_backfill.py` to
recover daily prices for expired stock options that Kite's master cache purges
(~4.7M rows: 30-symbol R29 universe over 2024-01→2025-11 + 2026-01→05, plus
all-symbol coverage on R8's 55 swing dates). Used to replay equity signals as
options (see `outputs/r29v2_options_hybrid_2026-06-07/`).
Read-only for the app; short-lived
DuckDB RW connections from the backfill coexist with the running app.

All SQLite DBs use `NullPool` (fresh connection per op) — never `StaticPool`.
Indian broker tokens expire ~03:00 IST daily; sandbox reset schedule is
configurable at `/sandbox`.

## Logs — where to look

| File | What's in it | Reliability |
|---|---|---|
| `log/errors.jsonl` | structured ERROR+ (truncated to last 1000 on boot) | **Polluted by pytest** unless isolated — filter test noise first |
| `log/openalgo_YYYY-MM-DD.log` | full text log | Only if `LOG_TO_FILE=True` |
| `log/bridge_stderr.log` | bridge stderr | **UNRELIABLE** (may show stale mtime even after recent calls) |
| `db/openalgo.db` → `scan_cycle` | canonical Chartink fire history | **Trustworthy** — start here for trading-action audits |
| `db/logs.db` → `traffic_logs` | HTTP request log | Polluted by pytest hitting localhost |
| scheduled-task session transcripts | what each Cowork task actually did | **MOST reliable** for "what fired" — `mcp__session_info__read_transcript` |

## Investigation order when something looks wrong mid-market

1. `mcp__scheduled-tasks__list_scheduled_tasks` — what's enabled, `lastRunAt`.
2. `mcp__session_info__list_sessions` — find today's "Fno scan cycle" sessions.
3. `mcp__session_info__read_transcript` — read what each cycle actually did
   (auto-fix? restart? pytest?).
4. `scan_cycle` table (`db/openalgo.db`) — the canonical Chartink fire record.
5. `/preflight` endpoint — current gate state.
6. `errors.jsonl` (last — and only AFTER filtering pytest noise per memory
   `pytest-pollutes-live-db-and-preflight`).

## Symbol format + API auth conventions

See `CLAUDE.md` → "Symbol Format" and "API Authentication" sections. Not
duplicated here. Quick reminder: API key goes in JSON body (`apikey`) or
`X-API-KEY` header; equity symbols are the bare base symbol.

## Strategy control endpoints (sector_follow_cap5_vol)

Blueprint `blueprints/sector_follow.py`, URL prefix `/sector_follow_cap5_vol`.
**API-key authenticated** (`X-API-KEY` header, or `apikey` in JSON body / query
string — same model as `/api/v1`). All read/control the in-process
SectorFollowService singleton; they return `503` if the service isn't initialised.

| Endpoint | Method | Side effect |
|---|---|---|
| `/sector_follow_cap5_vol/api/status` | GET | Read-only: mode, kill switch, today's entries/exits, open book + live MTM |
| `/sector_follow_cap5_vol/api/data_health` | GET | Read-only: live market-data freshness for the 8 indices + 30 stocks (`overall_ok`, `checked_at`, per-symbol `last_ts`/`staleness_days`/`ok`). Queries only — does not write the `data_health_check` row (that's the 16:30 job) |
| `/sector_follow_cap5_vol/api/positions` | GET | Read-only: open positions (with MTM) + today's entries/exits |
| `/sector_follow_cap5_vol/api/pause` | POST | Sets in-memory `manual_pause` — halts new entries; open positions still exit T+1. Runtime emergency override; for pre-market planning use the `strategy_daily_intent` table instead |
| `/sector_follow_cap5_vol/api/resume` | POST | Clears manual pause **and** the kill switch |
| `/sector_follow_cap5_vol/api/close_all` | POST | **Emergency square-off of every open position** (mode-aware; not blocked by kill switch). Requires body `{"confirm":"yes"}` |

### Unified daily intent (`strategy_daily_intent`)

The pre-market control surface for BOTH the simplified engine and sector_follow
is the `strategy_daily_intent` table (`db/openalgo.db`). One row per
`(strategy_name, intent_date)` declares `mode` (`live`/`sandbox`/`skip` — HOW
orders route) and `intent` (`run`/`pause`/`halt` — WHETHER to act), plus an
optional `daily_capital_cap`. The engines consult
`services/mode_service.resolve_strategy_mode(strategy_name)` at job-entry:
`pause` blocks new entries (exits still run), `halt` blocks everything including
exits. Fall-through when no row exists (flag on): legacy `daily_intent`
(simplified only) → env mode flag → `sandbox/run` default — so deploy is a no-op
until the operator inserts a row. Feature-flagged by
`STRATEGY_DAILY_INTENT_ENABLED` (default `true`). `place_order_service` is
deliberately NOT wired through this — its global `resolve_effective_mode` floor
is unchanged; the gate lives in the engines (the simplified engine's sandbox
dispatch bypasses `place_order_service` entirely). Full design:
`docs/design/strategy_daily_intent.md`.

`sector_follow_trades` columns (`database/sector_follow_db.py`): `id`, `strategy_id`,
`mode`, `side` (BUY/SELL), `symbol`, `exchange`, `product`, `quantity`, `price`
(reference price at decision time), `entry_date`, `vol_ratio`, `stock_ret`,
`sector_ret`, `order_id`, `note`, `created_at`. Append-only; no retention/pruning job.

## Known recurring patterns

- **Morning Zerodha token rollover** ~02:00–03:00 IST → WS reconnect burst
  ~02:10–08:55 (pre-market noise, filtered by preflight). A morning
  "Invalid openalgo apikey" 401 is the expired broker session, **not** a bad
  API key — fix by re-login, don't regenerate the key
  (memory `morning-401-broker-session-not-key`).
- **Restart during market hours** → SQLite database-locked burst ~3 sec,
  ~180 errors during multi-DB init.
- **Bridge `/fix-bug` call** → full pytest suite + restart → ~300-400 error
  storm; can lock preflight 45+ min.
- **Bridge `/restart-app` on Windows can hang** → wedges bridge into permanent
  BUSY (409 on all task endpoints). Start OpenAlgo directly with `Start-Process`
  instead (memory `bridge-restart-app-hangs-windows`).
- **Scanner late-start / tick gaps** are usually tick starvation downstream of
  Chartink (scanner passively reads ZMQ), not scanner bugs
  (memory `inhouse-scanner-starved-no-self-subscribe`).

## CI / code-quality gate

- **GitHub Action** `.github/workflows/quality-gate.yml` — runs on PRs to
  `dev`/`main` and pushes to `dev`. Steps: ruff (blocking), bandit
  (`|| true`, non-blocking), Semgrep custom rules ERROR (blocking) + WARNING
  (informational), public Semgrep `--config=auto` (best-effort). CI pins Python
  3.12 (no 3.14 eventlet wheels).
- **Custom Semgrep rules** `.semgrep/silent-drops.yml` (6 rules) — silent-drop /
  partial-success anti-patterns. Rule catalog: `audit/silent_drop_audit_2026-06-11.md`.
  Run locally via `uvx semgrep` (NOT in the uv lockfile — version conflict; see
  CLAUDE.md "Code-quality gates"). 3 ERROR rules block; 3 WARNING rules inform.
- **Pre-commit** `.pre-commit-config.yaml` — ruff, bandit, semgrep (ERROR-only),
  detect-secrets, biome on staged files. Enable: `uv pip install pre-commit &&
  pre-commit install`.
- Branch protection on `dev`/`main` is operator-enabled via the GitHub UI.

## Cross-references

- `CLAUDE.md` — coding conventions, deployment specifics, version bumping
- `COWORK_OBJECTIVE.md` — strategic objective
- `strategies/simplified_engine/LEARNINGS.md` — strategy-specific daily learnings
- `strategies/sector_rotation_etf/` — monthly ETF rotation strategy (**scaffold
  only, not live**). Signal computation: `services/sector_rotation_etf_service.py`
  (pure, read-only on `historify.duckdb`, emits recommended-orders JSON — no order
  placement). CLI entry: `services/sector_rotation_etf_cli.py`. Not wired to any
  scheduler; no live mode.
- `strategies/sector_follow_cap5_vol/` — intraday sector-follow strategy, cap-5
  positions, volume tiebreaker (**scaffold-only, `deployable: false`**). Daemon-style
  SectorFollowService (`services/sector_follow_service.py`) registers 5 APScheduler
  jobs (entry/exit/reset/EOD/data-health); control API at `/sector_follow_cap5_vol/api/*`;
  trade journal in `db/openalgo.db` `sector_follow_trades`. Sector-index 1m feed
  kept fresh by the `sector_follow_index_backfill` job. Plan/decisions: `PLAN.md`.
- `docs/SIMPLIFIED_ENGINE_HANDOFF.md` — engine integration context
- `docs/COWORK_SESSION_LEARNINGS.md` — Cowork-specific learnings, webhook IDs
- `audit/README.md` — read-only scheduled-task policy + `proposed_fixes.jsonl` schema
