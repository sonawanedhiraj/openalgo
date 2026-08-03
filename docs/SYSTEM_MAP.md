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
| `/review-signal`, `/reflect` | POST | LLM calls; review/journal helpers. **`/review-signal` is no longer called by the LLM veto** — as of #266 Phase 1 (#267) the veto invokes `claude -p` in-process via `services/llm_review_client.py` (dedicated real OS thread), retiring the bridge dependency for the veto path. The endpoint still exists but is unused by the veto; `/reflect` still backs `journal_reflection_service` |
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

### 3.5. GitHub Actions CI/CD Pipeline (self-hosted runner, port-independent)
- **Status:** ✅ ACTIVE (PR #9 merged 2026-06-20)
- **Workflow file:** `.github/workflows/ci-cd.yml`
- **Trigger:** Every PR targeting `dev` or `main`; also on direct push to those branches
- **Runner:** Self-hosted on Windows/WSL at `C:\actions-runner\`
  - Runner auto-updates on each job; restarts may occur mid-job
  - Runner pool visible via `gh run list` and `gh run view`
- **Two-stage pipeline:**

  | Stage | Job ID | Duration | Purpose |
  |-------|--------|----------|---------|
  | 1: CI | `ci-unit-tests` | ~4 min | Run 120+ unit/integration tests in parallel with pytest-xdist (`-n auto`). All tests must pass to proceed to stage 2 |
  | 2: CD | `cd-docker-e2e` | ~3 min (build 1m21s, boot 2m5s, tests 20s) | Build Docker image, boot container via docker-compose, run E2E tests against running app, teardown. Depends on CI |

- **Branch protection:** Both `ci-unit-tests` and `cd-docker-e2e` are **required checks** for merge to `dev`. Only these two are blocking; `quality`, `backend-lint`, `security-scan`, `silent-drops`, `pipeline` are informational only.
- **CI environment:** Test-only secrets provided at runtime:
  - `API_KEY_PEPPER`, `APP_KEY`, `FERNET_SALT` (not stored in repo, only in GitHub Secrets)
  - Conftest redirects all `DATABASE_URL` env vars to throwaway temp dir (full isolation)
  - 3 tests marked `@pytest.mark.xfail` due to self-hosted timing/isolation sensitivity
- **CD environment:** `.env` generated with random secrets (`uv run python -c "import secrets; print(secrets.token_hex(32))"`)
  - Docker Compose loads via `env_file: [.env]` (not volume mount — that fails on GitHub Actions)
  - Health check curls `http://127.0.0.1:5000/auth/check-setup` (30s interval, 40s start period, 3 retries)
- **DB isolation:** No live DB pollution — conftest tripwire aborts immediately if any test imports `db/openalgo.db`
- **Known issues:**
  - GitHub API check-status sync can be slow (5-10 min); required checks may show "expected" despite completing successfully. Workaround: wait or temporarily disable branch protection to merge.
  - Runner auto-update exits gracefully after current job; next dispatch uses new binary
  - `docker-compose up` on self-hosted occasionally takes 2+ min to boot; health retries allow 120s max boot time
- **Links:** PR #9 (merged), workflow runs at https://github.com/sonawanedhiraj/openalgo/actions

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
  | `sector_follow_data_health` | 16:30 | **Market-data freshness check** (after the 16:05 index backfill should have landed). Validates the 8 sector indices + 30 universe stocks via `data_freshness_service.check_strategy_data_ready`; writes a `data_health_check` row. On stale data: Telegram-alerts the operator **and** auto-pauses tomorrow's *entries* by writing a self-expiring `strategy_runtime_override` row (mode-only B6: `override_type='pause'`, `expires_at=`tomorrow 15:30 IST, `set_by='sector_follow'`) — the engine job-entry gate honors it; mode untouched, exits/EOD still run. Gated by `DATA_FRESHNESS_VALIDATION_ENABLED` (default `true`) |

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
  `strategies/sector_follow_cap5_vol/PLAN.md`. Its sector-index + universe-stock
  1m feeds are kept fresh by a boot-time + periodic state-convergence check
  (`services/sector_follow_backfill_scheduler.py`), not a cron — see the note
  under the APScheduler jobs table below.

### 5. FuturesFollowService (in-process, OpenAlgo eventlet worker)
- **Entry:** `services/futures_follow_service.py` — built + wired at boot by
  `init_futures_follow_service(app, scheduler)` (called from `app.py`). Lives inside
  the single OpenAlgo worker; **not** a separate process or a Cowork host task. A
  **leveraged broad-market-beta** sleeve built on the sector_follow signal set.
- **Mode flag:** env `FUTURES_FOLLOW_MODE` = `sandbox` (default) | `live` — **no
  scaffold / observe-only state.** **`sandbox` ACTIVELY trades** — it places real
  orders into `db/sandbox.db` (virtual ₹1Cr) from boot; `live` places real broker
  orders. An unknown value force-falls-back to `sandbox`. Operator can pause active
  trading via `/api/pause` (durable `strategy_runtime_override`) without changing
  mode; only a `strategy_mode` row can escalate sandbox→live.
- **Signal reuse:** does NOT reimplement gates — `production_signal_evaluator`
  calls the live `services/sector_follow_service` evaluator (config, sector map,
  DuckDB metrics, `passes_gates`, `select_entries`) so it fires on exactly the
  equity book's ≤5 daily signals.
- **Sizing:** 1 NIFTY **near-month** index future lot per signal (NIFTY futures are
  MONTHLY — the resolver `production_contract_resolver` picks the front-month from
  the master contract via `fno_search_symbols_db`; there is no weekly NIFTY future),
  greedy in vol-ratio order, HARD-CAPPED at **50% of capital as overnight SPAN
  margin** (`compute_lots_to_buy`); signals beyond the cap are skipped. Product
  **NRML**, exchange **NFO**, MARKET orders. **No stop loss.**
- **Registers 5 APScheduler jobs** on the shared scheduler (all `mon-fri`
  `Asia/Kolkata`, `replace_existing`):

  | Job id | Cron (IST) | What it does |
  |---|---|---|
  | `futures_follow_daily_reset` | 09:00 | Clear kill switch + daily P&L + intraday journals (manual pause persists) |
  | `futures_follow_eod_watchdog` | 15:28 | Post-primary-exit retry backstop (#334): flatten any prior-day position still open after the 15:25 exit (rejected order / missed job) before the 15:30 NFO close. Exits are never gated |
  | `futures_follow_entry` | 15:20 | Reuse the sector_follow evaluator, resolve the NIFTY near-month future, **place** 1 lot/signal BUY up to the 50% margin cap (sandbox/live; honors override gate + kill switch + freshness). First sandbox cycle 2026-06-15 |
  | `futures_follow_exit` | 15:25 | Square off every position opened on a prior trading day (T+1). Never blocked by the kill switch / override |
  | `futures_follow_eod_summary` | 15:30 | Best-effort Telegram EOD summary **+** writes a Day-N markdown report to `strategies/futures_follow_cap50/eod_reports/YYYY-MM-DD.md` (independent sinks) |

- **Pre-entry freshness gate:** `run_entry` aborts (no orders, alerts) when the
  sector_follow feed is stale beyond `MAX_STALENESS_BUSINESS_DAYS` (default 1).
  `run_exit` only *warns*. Gated by `DATA_FRESHNESS_VALIDATION_ENABLED`.
- **Kill switch:** trips when day P&L < −`daily_loss_kill_pct`% of capital (default
  3%); blocks new entries, open positions still run to T+1 exit.
- **DBs written:** `db/openalgo.db` → `futures_follow_trades` (trade journal,
  sandbox/live), `strategies` (one seeded row, natural key
  `name='futures_follow_cap50'`), `strategy_runtime_override` (pause/kill_switch
  holds); plus order rows in `db/sandbox.db` via the sandbox order path.
- **File output:** `strategies/futures_follow_cap50/eod_reports/YYYY-MM-DD.md`
  (git-ignored, observational; path hardcoded).
- **Control API:** see "Strategy control endpoints" below
  (`/futures_follow_cap50/api/*`).
- **Status:** **ACTIVE in sandbox**, `deployable: true` (default
  `FUTURES_FOLLOW_MODE=sandbox`; first sandbox cycle 2026-06-15 15:20 IST).
  **Caveat:** leveraged beta, NOT alpha — the signal does not predict NIFTY
  (hit-rate 53.4%, corr 0.295); it will struggle in a sustained flat/bear NIFTY
  regime. Backtest (NIFTY-only CAP50): CAGR 14.44%, Sharpe 1.27, MaxDD −8.0% on ₹10L.
  See `strategies/futures_follow_cap50/PLAN.md`.

### 6. Tick-liveness watchdog + WS-proxy supervisor (in-process daemon threads, issue #376)

Two resilience daemons wired at boot in `app.py`'s background init thread,
after `init_ws_recovery_service`. Both are the durable fix for the 2026-07-07
libzmq WSAENOBUFS (10055) incident: the WS/ZMQ side hard-aborted at 13:42 IST
while Flask kept serving port 5000, producing **42 minutes of silent tick
outage** that every existing guard missed (the completeness metric only fires
on a window roll, which needs a bar close, which needs ticks — a total outage
never rolls it).

- **WS-proxy supervisor** (`services/ws_proxy_supervisor.py`,
  `WSProxySupervisor`): a daemon thread polls the WS-proxy handle every **30s**
  via `websocket_proxy.app_integration.get_websocket_runtime_status()`
  (subprocess under gunicorn+eventlet / real OS thread under the dev server /
  `none` in Docker-standalone where the proxy runs externally). On an
  **unexpected exit** it `logger.error`s, alerts Telegram (`ws_proxy_died`
  event), waits `60s` backoff, then respawns the child via
  `restart_websocket_server()` — capped at `WS_PROXY_MAX_RESTARTS_PER_DAY`
  (default 3) per IST day. Beyond the cap it fires one CRITICAL alert and stops
  (operator territory). Auto-restart runs at any hour; Telegram alerts are
  suppressed outside 08:45-16:00 IST on trading days (no off-hours spam). Never
  touches the main Flask process. No-op in Docker/standalone.
- **Tick-liveness watchdog** (`services/tick_liveness_watchdog.py`,
  `TickLivenessWatchdog`): a daemon thread polls — on its **own clock**,
  independent of the tick stream — the wall-time of the last LIVE scanner bar
  close (`scanner_service.get_last_live_bar_close_wall`, stamped in
  `_on_bar_close` for non-replay bars only). On trading days between **09:25 and
  15:30 IST** (holiday-aware via `data_freshness_service.is_trading_day`),
  silence beyond `SCANNER_LIVENESS_MAX_SILENT_MIN` (default 10) minutes →
  `logger.error` + Telegram CRIT (`tick_liveness` event), re-alerted at most
  every `SCANNER_LIVENESS_REALERT_MIN` (default 30) min, with one INFO
  "recovered after X min" line + notice when bars resume. An **auto-heal ladder**
  (`TICK_LIVENESS_AUTOHEAL_ENABLED`, default true) then escalates one step per
  ~2 min while dark: (1) re-subscribe nudge (`scanner_pre_subscriber.ensure(...,
  reset=True)`), (2) broker-adapter reconnect (FEED cache-invalidate — the same
  ZMQ event the ~3AM re-login rides), (3) WS-proxy restart via
  `ws_proxy_supervisor.request_supervised_restart` (shares the supervisor's
  daily cap), (4) terminal CRITICAL ("manual OpenAlgo restart required");
  cooldown-throttled per `SCANNER_LIVENESS_LADDER_COOLDOWN_MIN` (default 30).
  The same thread emits an **hourly resource-trend INFO line** (process
  handle/TCP/thread counts via psutil, ctypes `GetProcessHandleCount` fallback;
  the 10055 lead). No-op when `SCANNER_ENABLED` != true (no bars to watch).
- **Master flags:** `TICK_LIVENESS_WATCHDOG_ENABLED` (default true),
  `TICK_LIVENESS_AUTOHEAL_ENABLED` (default true). Tests:
  `test/test_tick_liveness_watchdog.py`, `test/test_ws_proxy_supervisor.py`,
  `test/test_scanner_live_bar_heartbeat.py`.

## In-process APScheduler jobs (OpenAlgo worker)

These cron jobs run **inside** the single eventlet worker on the shared
APScheduler instance (`services/historify_scheduler_service.py`). They are NOT
Cowork host tasks (§3 above) — they live and die with the OpenAlgo process and
need no external scheduler.

| Job id | Cron (IST) | What it does | Gating / writes |
|---|---|---|---|
| `scanner_comparison_eod` | `45 15 * * 1-5` (15:45 IST) | **In-house-scanner-vs-Chartink EOD comparison** — the in-process replacement for the retired Cowork `scanner-vs-chartink-daily-comparison` task (§3). For today: unions the Chartink BUY/SELL webhook lists (`scan_cycle`, `cycle_kind='chartink'`) and the in-house scanner hits (`scan_results`, `source='inhouse'`, grouped by `scan_definition.screener_type`), computes per-side counts/intersection/Jaccard/recall + a tuning verdict, writes one `scanner_comparison` row per side (idempotent delete-then-insert per `(date, side)`), and Telegrams the summary via `notify()`. Read-only on every DB except its own table. | Per-fire gate env `SCANNER_COMPARISON_EOD_ENABLED` (default `true`); fire time env `SCANNER_COMPARISON_EOD_TIME` (default `15:45`); Telegram toggle `NOTIFY_SCANNER_COMPARISON` (default `true`). Body: `services/scanner_comparison_eod_service._eod_comparison_job` (registered by `init_scanner_comparison_eod_service`). |
| `telegram_inbound_morning_prompt` | ~~`45 8 * * 1-5`~~ | **RETIRED (mode-only, 2026-06-12, B5).** The morning intent prompt is gone — there is no per-day run/pause/halt to set (strategies run continuously in their persistent `strategy_mode`). `register_jobs` no longer schedules this job and removes any stale instance. The Telegram bot now only serves `/status` (reports modes); all intent commands return a deprecation notice pointing at `/api/pause`. | No longer registered. Was gated on `TELEGRAM_INBOUND_ENABLED=true`. |
| `multi_account_login_reminder_0900` / `_1500` + `multi_account_eod_summary` | `0 9 * * 1-5`, `0 15 * * 1-5`, `35 15 * * 1-5` (IST) | **Multi-account observability (issue #476).** Reminders: Telegram-lists enabled child accounts with strategies selected but no fresh `acct:<id>` session (the 15:00 firing is a LAST-CALL nudge before the 15:20 entries). EOD summary: one line per child account over today's `account_orders` rows (placed/skipped/rejected). | All three fire-time gated on `MULTI_ACCOUNT_ENABLED` (default `false`) AND `data_freshness_service.is_trading_day` — silent for single-account installs and on holidays. Body: `services/account_mirror_summary_service.py` (registered by `init_account_mirror_summary_service` from `app.py`). |
| `eod_watchdog_<strategy>` | `mon-fri` at `min(strategy.eod_exit_time, SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME)` — default **15:14** for `trending_equity_intraday` | Safety-net EOD flatten for the simplified engine. One cron job per registered intraday strategy; calls `flatten_strategy_positions` (open `trade_journal` rows → opposite-side MARKET via `place_order`, mode-aware sandbox/live). Backstop for the tick-driven `_maybe_flatten_eod`, which can't fire when the broker tick stream dies before close. **Fires at 15:14, one minute before the 15:15 sandbox/broker MIS auto-square-off** — the cap is the 2026-06-10 fix: the watchdog used to fire at the declared 15:20, *after* sandbox had force-closed and started rejecting flatten orders, stranding OIL/HINDZINC/TATAELXSI. Belt to the 15:30 EOD reconciliation suspenders. | Runs on a **dedicated `BackgroundScheduler`** (not the shared instance), `services/eod_watchdog_service.py`. Gated by env `SIMPLIFIED_ENGINE_EOD_WATCHDOG_ENABLED` (default `true`); cap via `SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME` (default `15:14`). `misfire_grace_time=300`. Started from `app.py` boot after journal rehydrate. |

> The `sector_follow_cap5_vol` strategy also registers its own entry/exit/reset/
> EOD jobs on this same scheduler — see the SectorFollowService process entry.
> The `futures_follow_cap50` strategy likewise registers its own
> reset/entry/exit/watchdog/EOD jobs (09:00/15:20/15:25/15:28/15:30 IST) — see the
> FuturesFollowService process entry (§5).
> The `open15_vol_breakout` strategy (issue #425, sandbox) registers 4 jobs on
> this same scheduler: `open15_arm` 09:10 / `open15_exit` / `open15_exit_retry`
> (+2 min) / `open15_summary` (+5 min), mon-fri. Exit defaults to 09:30 (so
> retry 09:32 / summary 09:35) but is **UI-configurable** together with the
> entry cutoff (issue #451: `open15_config.no_entry_after`/`exit_time`, env
> defaults `OPEN15_NO_ENTRY_AFTER` 09:29 / `OPEN15_EXIT_TIME` 09:30, exit
> capped 15:10 to precede the 15:15 MIS square-off) — jobs are (re)pointed at
> the effective times at boot registration and every 09:10 arm. The **trade
> side** is UI-configurable too (issue #503: `open15_config.trade_side`, env
> default `OPEN15_TRADE_SIDE` `both`) — `long_only` / `short_only` gate the
> 09:15 selection itself, so the excluded side is never watched, never
> triggers and never journals a row. Its tick feed
> is an **own additive
> ZMQ SUB** on the proxy bus (5555), active only 09:14:50 .. exit+5s IST
> (`services/open15_breakout_service.py`). **Ops: the app must be booted before
> 09:15 IST or the day is marked `skipped_late_boot`** — the 09:15 first candle is
> built from live ticks and cannot be reconstructed. Journal: `open15_trades` +
> `open15_day_logs` (per-day decision-log JSON) in `db/openalgo.db`
> (`database/open15_breakout_db.py`); API `/open15_vol_breakout/api/status|trades|decision_log`
> + self-contained viewer at `/open15_vol_breakout/logs`. **Tick capture:** every
> universe symbol's ticks across the whole 09:14:50 → exit+5s window are persisted
> to `tick_logs/open15/` (365d retention; `OPEN15_TICK_CAPTURE` +
> `OPEN15_TICK_CAPTURE_UNIVERSE` both default true; ~120k ticks ≈ 10 MB/day, no
> extra broker load — issue #528). `OPEN15_TICK_CAPTURE_UNIVERSE=false` restores
> the pre-#528 selected-symbols-only capture. Tick-resolution replay data for
> re-backtesting. Flags `OPEN15_*` (PARAMETER_LOG).

**sector_follow 1m feed: boot-time + periodic state-convergence (not a cron).**
The `sector_follow_index_backfill` (`5 16 * * 1-5`) and `sector_follow_stock_backfill`
(`10 16 * * 1-5`) cron jobs were **removed** (commit `5c2a06eff` registered them;
they are gone from `historify_scheduler_service.py`). They are replaced by a
state-convergence check in `services/sector_follow_backfill_scheduler.py`
(`init_sector_follow_backfill`, wired in `app.py`): each backfill service now
exposes `check_and_refresh_if_stale(today)` which reads `MAX(timestamp)` per
symbol from `db/historify.duckdb` and incrementally fetches **only** the indices /
stocks behind today's expected 15:30 IST close (idempotent when fresh;
fail-graceful — a dead-token fetch is logged and alerted, never raised). It runs
**once at boot** (a daemon thread that waits for a broker session, so a restart
after the daily ~3 AM Zerodha re-login auto-catches up overnight staleness) and
then **periodically** every `SECTOR_FOLLOW_PERIODIC_INTERVAL_MIN` minutes
(default 30) inside the `15:30`..`SECTOR_FOLLOW_PERIODIC_END_TIME` (default
`17:00`) IST window on trading days, backing off until the next day once both
universes are fresh. Gated by `SECTOR_FOLLOW_PERIODIC_CHECK_ENABLED` (default
`true`). The per-window CLIs (`python -m services.sector_follow_index_backfill` /
`…stock_backfill --from --to`) remain for manual multi-day historical catch-up;
both still need an active broker session. Writes 1m bars to `market_data`.

**scanner universe feed: boot-time + periodic state-convergence (1m AND daily).**
The scanner-side sibling of the sector_follow convergence above, fixing the two
supply bugs the 2026-06-13 Friday-screener replay surfaced (the in-house
scanner's `SCANNER_SYMBOLS` F&O universe was never backfilled; the stored daily
`D` interval that `ScannerHistoryProvider` reads was universally stale).
`services/scanner_backfill_scheduler.py` (`init_scanner_backfill_scheduler`,
wired in `app.py` next to `init_sector_follow_backfill`) + the backfill module
`services/scanner_universe_backfill.py` keep the scanner universe fresh in **both
storage intervals** (`1m` and `D`): `check_and_refresh_if_stale(today,
interval=…)` reads `MAX(timestamp)` per symbol for the interval from
`historify.duckdb` and incrementally fetches **only** the symbols behind today's
close (idempotent when fresh; fail-graceful; no-op when `SCANNER_SYMBOLS` is
unset). The symbol set is derived live from the `SCANNER_SYMBOLS` env (each
symbol routed to `NSE`/`NSE_INDEX` via
`scanner_presubscribe.resolve_exchange_for_symbol`). Same boot-once +
periodic-in-the-post-close-window shape as sector_follow, gated by
`SCANNER_BACKFILL_ENABLED` (master, default `true`),
`SCANNER_BACKFILL_PERIODIC_CHECK_ENABLED` (default `true`),
`SCANNER_BACKFILL_PERIODIC_INTERVAL_MIN` (default `30`),
`SCANNER_BACKFILL_PERIODIC_END_TIME` (default `17:00`), and
`SCANNER_BACKFILL_INTERVALS` (default `1m,D`). Each interval check writes a
`data_health_check` row (`strategy_name='scanner_universe_1m'` /
`'scanner_universe_D'`) — no schema change. CLI for deep manual catch-up
(notably the one-time initial deep 1m backfill of never-fetched symbols): `python
-m services.scanner_universe_backfill --from --to --interval {1m|D}`. Writes
`1m`/`D` bars to `market_data`. A **third daemon loop** — the **mid-session
straggler re-check** (`ScannerStragglerRecheck`, issue #390) — runs every
`SCANNER_STRAGGLER_RECHECK_MIN` (default 15) min inside **09:20-15:30 IST** on
trading days: it reuses the same `run_backfill_checks(resettle=False)` machinery
to catch up any symbols the 09:16 pre-entry refresh left stale, then invokes the
scanner smoke-check `re_check_and_release()` so a handful of morning-stale
symbols self-heal and the (now per-symbol) smoke post-hold narrows/clears
intraday instead of at 15:30. Gated by `SCANNER_STRAGGLER_RECHECK_ENABLED`
(default `true`).

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
| `db/openalgo.db` | users, orders, positions, settings, **scan_cycle** (canonical Chartink fire history), strategies, **trade_journal** (one row per round trip; `ltp_at_signal` REAL holds the decision-time LTP for slippage analysis, added 2026-06-07 via boot-time `ALTER TABLE` in `trade_journal_db.init_db`), **sector_follow_trades** (sector_follow_cap5_vol journal — one row per entry/exit in all modes; created idempotently by `database/sector_follow_db.init_db`), **futures_follow_trades** (futures_follow_cap50 journal — one row per NIFTY-futures order leg in sandbox/live; futures-specific columns `nifty_symbol`/`lots`/`entry_price`/`exit_price`/`gross_pnl`/`charges_inr`/`net_pnl`/`margin_inr`/`signal_id`; created idempotently by `database/futures_follow_db.init_db`, also in the boot `db_init_functions` list), **daily_intent** (legacy simplified-engine per-day intent, still read), **strategy_daily_intent** (unified per-strategy `{mode, intent, daily_capital_cap}` control surface keyed `(strategy_name, intent_date)`; created by `database/strategy_daily_intent_db.init_db`; legacy `daily_intent` rows backfilled into it at boot via `migrate_legacy_daily_intent`; read via `services/mode_service.resolve_strategy_mode`), **strategy_mode** (mode-only architecture: the single *persistent* per-strategy operator control — `{strategy_name PK, mode ∈ {live, sandbox} default sandbox, updated_at, updated_by, notes}`; created by `database/strategy_mode_db.init_db`; backfilled from the latest `strategy_daily_intent` row per strategy by `scripts/migrate_strategy_daily_intent_to_strategy_mode.py` (drops the intent/cap axes; legacy `mode='skip'` → `sandbox`); read via `services/mode_service.resolve_mode`; supersedes the `strategy_daily_intent` `mode` column — the intent/pause/halt axis is being moved to a separate self-expiring `strategy_runtime_override` table for automated safety guards), **strategy_runtime_override** (mode-only architecture: the ephemeral, self-expiring safety-guard table — `{id PK, strategy_name, override_type ∈ {pause, kill_switch}, expires_at (UTC), reason, set_by, created_at}`; created by `database/strategy_runtime_override_db.init_db`; written ONLY by automated guards (data-health auto-pause, daily kill-switch) and the sector_follow `/api/pause` emergency override — never an operator daily prompt or Telegram; **lazy expiry** — reads ignore rows past `expires_at`; blocks new ENTRIES only, never exits/EOD; read at engine job-entry via `is_entry_blocked`), **data_health_check** (daily market-data freshness verdicts per strategy — `check_at`, `overall_ok`, `stale_symbols` JSON, `details_json`, `alert_sent`; created by `database/data_health_db.init_db`; written by the 16:30 IST `sector_follow_data_health` job AND by the scanner backfill convergence — one row per interval, `strategy_name='scanner_universe_1m'`/`'scanner_universe_D'`, via `services/scanner_backfill_scheduler`), **signal_decision** (Stage-1 LLM veto-layer audit — one row per candidate review; `direction` TEXT column (`BUY`/`SELL`, nullable) records the side the engine armed, added 2026-06-11 via idempotent boot-time `ALTER TABLE` in `signal_decision_db._migrate_add_direction_column`; previously the side was unrecoverable because the chartink `source` string carries "buy" for both legs), **futures_follow_eval_snapshots** (issue #352: one row per `(strategy_name, eval_date)` — the 15:20 futures_follow entry-evaluation breakdown as JSON (`payload_json`): per-symbol sector_ret/stock_ret/vol_ratio/intraday_source/outcome sorted by closeness to passing, plus source counts, per-gate fail counts, cap-skips and LLM vetoes; created by `database/futures_follow_eval_db.init_db`; written fail-graceful by `FuturesFollowService.run_entry` AFTER placement decisions, idempotent upsert per day; read by `GET /futures_follow_cap50/api/entry_breakdown`), **scanner_comparison** (daily in-house-scanner-vs-Chartink parity verdict — one row per `(date, screener_side)`: `inhouse_count`, `chartink_count`, `intersection_count`, `jaccard`, `ratio`, `false_positives_json`, `false_negatives_json`, `tuning_suggestion`, `telegram_sent`; created by `database/scanner_comparison_db.init_db`; written by the 15:45 IST `scanner_comparison_eod` job; idempotent delete-then-insert per date+side), **broker_accounts** + **account_strategies** (multi-account Phase 1, issue #468 — child broker accounts that will mirror the primary's strategy orders in Phase 2: `broker_accounts` = `{id PK, display_name UNIQUE, broker, broker_client_id, api_key_encrypted, api_secret_encrypted, totp_secret_encrypted NULL, capital_inr, is_enabled default FALSE, last_login_at, created_at, updated_at}` with Kite Connect app credentials Fernet-encrypted via `database.auth_db.encrypt_token`; `account_strategies` = allow-list rows `(account_id, strategy_name) PK` — a row means "this account mirrors this strategy"; created by `database/broker_accounts_db.init_db` (in the boot `db_init_functions` list); a child's DAILY access token is NOT here — it lives in the existing `auth` table under `name='acct:<id>'` written by `services/broker_accounts_service.complete_login` with none of the primary-login side effects; managed via `/broker_accounts/api/*` (blueprint `broker_accounts.py`, session-gated `"user" in session` per #462) and the React `/accounts` page; Phase-2 fan-out gates on the UI master switch — **multi_account_settings** single-row table (issue #484: `{enabled default FALSE, primary_book_capital default 10L, updated_at, updated_by}`; DB row WINS, env `MULTI_ACCOUNT_ENABLED`/`PRIMARY_BOOK_CAPITAL` are only the first-read seed/fallback; written via `PUT /broker_accounts/api/settings` from the /accounts control card; consulted at fire time so UI flips apply without restart), **account_orders** (multi-account Phase 2, issue #474 — one row per ATTEMPTED child mirror order: `{account_id, strategy_name, symbol, exchange, action, product, parent_qty, child_qty, factor, parent_orderid, status ∈ {placed, rejected, skipped_no_session, skipped_zero_qty, skipped_no_position, skipped_no_capital, skipped_no_quote, error}, broker_orderid, error_text, created_at}`; created by `database/account_orders_db.init_db` (boot list); written fail-graceful by `services/account_fanout_service.py`, which is invoked fire-and-forget from the ONE seam at the tail of `place_order_service.place_order_with_auth`'s LIVE-accepted branch — mirrors fire ONLY when `MULTI_ACCOUNT_ENABLED=true` AND the parent resolved LIVE AND was broker-accepted AND the `mode_key` is a known strategy with ≥1 enabled child selecting it; sizing is CAPITAL-PER-TRADE (issue #496, supersedes the ratio model): each `account_strategies` row carries `capital_per_trade_inr` (boot ALTER; unset → `skipped_no_capital`, default deny) and OPENING qty = floor(capital ÷ price) / floor(capital ÷ (premium × lotsize)) × lotsize, priced via parent LIMIT price or a live quote (`resolve_sizing_price`, journaled in `account_orders.sizing_price`; quote failure → `skipped_no_quote`); `multi_account_settings.primary_book_capital` is RETIRED from sizing (dormant column), EXIT orders flatten the child's own broker position (`get_open_position` with the child's `acct:<id>` token) instead of scaling; children placed via `broker.<broker>.api.order_api.place_order_api` directly (no recursion through `place_order`); every non-placed outcome Telegrams via `notify("multi_account_mirror", …)`) | Main DB. Pooling: `NullPool` |
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
| `/sector_follow_cap5_vol/api/pause` | POST | Sets in-memory `manual_pause` **and** writes a durable `strategy_runtime_override` `pause` row (same-day expiry, mode-only B6) so the hold survives a restart and the engine job-entry gate honors it. Halts new entries; open positions still exit T+1. `/api/resume` clears both. Mode flips are laptop-only (`strategy_mode`) |
| `/sector_follow_cap5_vol/api/resume` | POST | Clears manual pause **and** the kill switch |
| `/sector_follow_cap5_vol/api/close_all` | POST | **Emergency square-off of every open position** (mode-aware; not blocked by kill switch). Requires body `{"confirm":"yes"}` |

Blueprint `blueprints/futures_follow.py`, URL prefix `/futures_follow_cap50`. Same
API-key auth + `503`-if-uninitialised model; all read/control the in-process
FuturesFollowService singleton.

| Endpoint | Method | Side effect |
|---|---|---|
| `/futures_follow_cap50/api/status` | GET | Read-only: mode, kill switch, lots held, margin used vs the 50% cap, today's entries/exits, open book + live MTM |
| `/futures_follow_cap50/api/data_health` | GET | Read-only: live market-data freshness for the **sector_follow** signal feed (the futures sleeve fires on that signal set). Queries only — does not write the `data_health_check` row |
| `/futures_follow_cap50/api/positions` | GET | Read-only: open positions (with MTM) + today's entries/exits + lots held + margin used |
| `/futures_follow_cap50/api/entry_breakdown` | GET | Read-only (issue #352): the persisted per-day 15:20 entry-evaluation breakdown (`futures_follow_eval_snapshots` — per-symbol gate inputs/outcomes, source counts, cap-skips, vetoes). Defaults to today (IST); `?date=YYYY-MM-DD` for a past day; `data=null` = no evaluation recorded yet. Auth: API key **or** a valid logged-in browser session (the React strategy-page card uses the session cookie) — the only endpoint on this blueprint with session auth; never triggers an evaluation |
| `/futures_follow_cap50/api/pause` | POST | Sets in-memory `manual_pause` **and** a durable `strategy_runtime_override` `pause` row (same-day expiry). Halts new entries; open positions still exit T+1. Mode flips are laptop-only (`strategy_mode`) |
| `/futures_follow_cap50/api/resume` | POST | Clears manual pause **and** the kill switch |
| `/futures_follow_cap50/api/close_all` | POST | **Emergency square-off of every open position** (mode-aware; not blocked by kill switch). Requires body `{"confirm":"yes"}` |

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
`STRATEGY_DAILY_INTENT_ENABLED` (default `true`). Since issue #440 (2026-07-23)
`place_order_service` (and basket/split/smart/GTT-place/close_position)
dispatches live-vs-sandbox **per strategy** via
`services/mode_service.resolve_order_mode(mode_key)`: LIVE only when the navbar
Analyze/Live toggle is on Live AND that strategy's `strategy_mode` row says
`live` (the strategies-page toggle); no row / unknown label → sandbox (default
deny). The hidden `strategy_mode['__global__']` gate and legacy `daily_intent`
fall-through are retired from dispatch (leftover `__global__` rows are purged
at boot); env mode flags are capped at sandbox. `resolve_effective_mode()`
survives as the analyze overlay only (read decorations + cancel/modify/close
routing, with a sandbox-book orderid lookup for per-order targeting). The
simplified engine's sandbox dispatch still bypasses `place_order_service`
entirely. Full design: `docs/design/strategy_daily_intent.md` + issue #440.

`sector_follow_trades` columns (`database/sector_follow_db.py`): `id`, `strategy_id`,
`mode`, `side` (BUY/SELL), `symbol`, `exchange`, `product`, `quantity`, `price`
(reference price at decision time), `entry_date`, `vol_ratio`, `stock_ret`,
`sector_ret`, `order_id`, `note`, `created_at`. Append-only; no retention/pruning job.

## Broker session lifecycle (login → token persist → WS reinit)

Indian broker tokens expire daily ~3 AM IST, so the operator re-logs in each
trading morning. The WS market-data feed must pick up the new token **without an
OpenAlgo restart** (a restart wipes in-memory state and is risky mid-market).

Flow on every broker login:

1. **Login completion** — `blueprints/brlogin.py` `broker_callback` →
   `utils/auth_utils.handle_auth_success(...)`.
2. **Token persist** — `handle_auth_success` calls
   `database/auth_db.upsert_auth(...)`, which encrypts + stores the token in the
   `auth` table (`db/openalgo.db`), clears local auth caches, and **publishes a ZMQ
   `CACHE_INVALIDATE_ALL_{user}` event** (`database/cache_invalidation.py` →
   `SharedZmqPublisher`). This is the single cross-process signal.
3. **UI notification** — `handle_auth_success` → `notify_broker_session_refreshed`
   emits a `broker_session_refreshed` **SocketIO** event (browser dashboard only;
   not the reconnect trigger).
4. **WS proxy reinit** — the **separate WS-proxy subprocess** (`websocket_proxy/
   server.py`, port 8765) receives the ZMQ event in `zmq_listener` →
   `_handle_cache_invalidation` → `_reconnect_broker_adapter(user_id)`: snapshots
   the adapter's held subscriptions, disconnects, `initialize()` (re-reads the new
   token from `auth_db`), `connect()`, and re-subscribes the symbol set. **No
   feature flag** — it is the unconditional default. Failure-graceful (logs
   `logger.exception`, retains the snapshot in `_last_known_subscriptions`, drops
   the dead adapter for lazy rebuild) and idempotent (one adapter reused; disconnect
   precedes every reconnect).

**Why ZMQ, not SocketIO, for the reinit:** the WS proxy runs as its own subprocess
(spawned in `websocket_proxy/app_integration.py` under eventlet), so Flask
SocketIO — in-process to Flask + browser clients — cannot reach it. ZMQ is the only
in-band cross-process channel. Tests: `test/test_broker_session_auto_reconnect.py`
(hermetic; builds the proxy via `WebSocketProxy.__new__`).

5. **Scanner bar-gap recovery (in-process)** — `services/ws_recovery_service.py`
   (`WSRecoveryService`, registered at boot via `init_ws_recovery_service(app)`).
   `notify_broker_session_refreshed` additionally publishes an in-process
   `BrokerSessionRefreshedEvent` on `utils/event_bus`; the recovery service
   subscribes (topic `broker_session_refreshed`) and, per tracked symbol (scanner
   `SCANNER_SYMBOLS` + sector_follow locked-static-30 stocks + mapped sector
   indices), fetches the last `WS_RECOVERY_LOOKBACK_MIN` min of 1m bars via
   `history_service.get_history` and folds them into the live scanner aggregator
   via `MultiIntervalAggregator.replay_bars` — closing the tick-starvation gap
   without an OpenAlgo restart. Idempotent (per-`BarBuilder` timestamp dedup),
   best-effort (per-symbol failures logged + skipped, callback never raises),
   Telegram-summarized. Limitation: Zerodha current-day history lags ~5-15 min, so
   the most-recent bars may be unavailable on a fast reconnect (reported, caught up
   next refresh). No flag. Test: `test/test_ws_recovery_service.py`.

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
  `dev`/`main` and pushes to `dev`. **Two jobs (split 2026-06-14):**
  `silent-drops` (minimal — only the custom Semgrep ERROR rules; the **lone job
  to be required on `main`**) and `quality` (ruff blocking + bandit `|| true` +
  Semgrep WARNING informational + public `--config=auto` best-effort —
  **informational** for now, promoted to required once ruff debt clears). Split
  because GitHub gates required checks at the job level, and ruff debt keeps
  `quality` red. CI pins Python 3.12 (no 3.14 eventlet wheels).
- **Custom Semgrep rules** `.semgrep/silent-drops.yml` (6 rules) — silent-drop /
  partial-success anti-patterns. Rule catalog: `audit/silent_drop_audit_2026-06-11.md`.
  Run locally via `uvx semgrep` (NOT in the uv lockfile — version conflict; see
  CLAUDE.md "Code-quality gates"). 3 ERROR rules block; 3 WARNING rules inform.
- **Pre-commit** `.pre-commit-config.yaml` — ruff, bandit, semgrep (ERROR-only),
  detect-secrets, biome on staged files. Enable: `uv pip install pre-commit &&
  pre-commit install`.
- **GitHub Action** `.github/workflows/code-direct-push-guard.yml` — Telegram-alerts
  the operator on a **direct** (non-PR-merge) push to `dev` that touches a runtime
  code path (`services/`, `broker/`, `restx_api/`, `database/`, `blueprints/`,
  `utils/`, `mcp/`, `websocket_proxy/`, `sandbox/`, `frontend/src/`, top-level
  `app.py`, `bridge/`). **Alert-only — never blocks.** Doc/test/CI-only pushes and
  PR-merge commits are exempt. Needs repo secrets `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID`. Details: `.github/workflows/README.md`.
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
  kept fresh by the boot+periodic convergence check
  (`services/sector_follow_backfill_scheduler.py`), not a cron. Plan/decisions: `PLAN.md`.
- `strategies/futures_follow_cap50/` — leveraged-beta NIFTY-futures sleeve on the
  sector_follow signal set (**ACTIVE in sandbox, `deployable: true`**; first sandbox
  cycle 2026-06-15). FuturesFollowService (`services/futures_follow_service.py`)
  reuses the
  sector_follow evaluator and registers 5 APScheduler jobs
  (reset/entry/exit/watchdog/EOD, 09:00/15:20/15:25/15:28/15:30 IST); control API at
  `/futures_follow_cap50/api/*`; trade journal in `db/openalgo.db`
  `futures_follow_trades`. Caveat: leveraged beta, not alpha (signal does not predict
  NIFTY). Plan/decisions: `strategies/futures_follow_cap50/PLAN.md`.
- `docs/SIMPLIFIED_ENGINE_HANDOFF.md` — engine integration context
- `docs/COWORK_SESSION_LEARNINGS.md` — Cowork-specific learnings, webhook IDs
- `audit/README.md` — read-only scheduled-task policy + `proposed_fixes.jsonl` schema
