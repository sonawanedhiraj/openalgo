# Full Trading-Day E2E Test Coverage Plan

**Date:** 2026-06-24
**Status:** PLAN ONLY — no source or test code changes
**Branch:** `docs/full-trading-day-e2e-plan`
**Refs:** prior plan [`2026-06-22_production_flows_test_plan.md`](2026-06-22_production_flows_test_plan.md) (Flow-cards 1–8, broker-seam focus); this doc extends to the FULL TRADING DAY and proposes the three-layer harness architecture.

---

## Executive Summary

A complete OpenAlgo trading day spans 21 distinct flows across 5 phases (pre-open, market-open, intraday, close, post-close). A single production failure at any seam — stale feed going undetected, background process not starting on login, mode surviving a restart — caused real trading losses (2026-06-10 EOD orphan gap, 2026-06-12 tick-starvation collapse, 2026-06-15 first sandbox cycle 0 signals).

Out of 21 enumerated flows:

- **10 flows are ❌ untested end-to-end** (they have unit coverage on individual components but no test exercises the SEAM — the path from trigger to observable side-effect).
- **8 flows are ⚠️ partially covered** — the happy path exists but error paths, cascades, or the cross-component wiring are unverified.
- **3 flows are ✅ well covered** — deep unit + integration test suites.

This plan proposes a **three-layer harness** (Layer A: backend integration, Layer B: Playwright UI, Layer C: synthetic day) delivering 21–25 tests. Layer A tests run in <5s each, Layer C synthetic day in <60s. Total estimated effort: 4–5 weeks.

---

## Phase 1 — Complete Trading-Day Flow Enumeration

### PRE-OPEN / SETUP (Flows 1–6)

---

### Flow 1 — OpenAlgo admin login

**Trigger:** `POST /auth/login` with username + password (+ optional TOTP)

**Internal path:**
1. `blueprints/auth.py:272-398` — `login()` — validates CSRF token, dispatches to `authenticate_user`
2. `database/user_db.py:249` — `authenticate_user(username, password)` — Argon2 verify against `password_hash + API_KEY_PEPPER`
3. `blueprints/auth.py:329-340` — TOTP branch if `totp_enabled`
4. `blueprints/auth.py:342` — `session["user"] = username`; `session["logged_in"] = True`
5. `blueprints/auth.py:183` — `_try_resume_broker_session(username)` — reads auth_db for a valid cached token; if found, skips OAuth

**External boundaries:** None (pure in-process)

**Backend state changes:** Flask session cookie written; auth_db cache read (no write on plain login)

**Frontend reflection:** Redirect to dashboard; nav bar shows logged-in state

**Current coverage:** ❌ No test — `test/test_logout_csrf.py` tests CSRF on logout only; `test/test_csrf.py` (0 tests, empty file); `test/test_boot_broker_session.py` (0 tests). No test exercises the full login path end-to-end including session establishment.

---

### Flow 2 — Zerodha OAuth login + token storage

**Trigger:** `GET /broker` → redirect to Zerodha → callback `GET /zerodha/callback?request_token=...`

**Internal path:**
1. `blueprints/auth.py:559` — `broker_login()` — redirects to Zerodha OAuth URL
2. `blueprints/brlogin.py:36` — `broker_callback(broker)` — receives `request_token`
3. `blueprints/brlogin.py:63` — dispatches to `broker/zerodha/api/auth_api.authenticate(token)`
4. `utils/auth_utils.py:394` — `handle_auth_success(auth_token, ...)`:
   - `database/auth_db.py:512` — `upsert_auth(...)` — Fernet-encrypts broker token, writes to `openalgo.db:auth` table
   - `database/auth_db.py:543` — `_cipher_suite.encrypt(auth_token.encode())`
   - `utils/auth_utils.py:475` — `notify_broker_session_refreshed(username, broker)` — publishes ZMQ `CACHE_INVALIDATE_AUTH_<user_id>`

**External boundaries:** Zerodha OAuth server (mocked in tests)

**Backend state changes:** `openalgo.db:auth` row upserted with encrypted token + feed_token; Flask session `logged_in=True`, `broker=zerodha`

**Frontend reflection:** Redirect to dashboard; WS proxy reconnects (see Flow 21)

**Current coverage:** ⚠️ Partial — `test/test_broker_session_auto_reconnect.py` (7 tests) covers ZMQ reconnect in isolation; `test/upstream/test_auth_resume.py` (5 tests) covers the resume path after a cached token. No test walks the full OAuth callback → upsert_auth → ZMQ publish chain.

---

### Flow 3 — Background processes started on broker login

**Trigger:** `notify_broker_session_refreshed()` publishes `CACHE_INVALIDATE_AUTH_*` via ZMQ → `websocket_proxy/server.py` subscribes

**All background processes started at app boot (not on login):**

| Process | Registration point | How it starts |
|---------|-------------------|---------------|
| WebSocket proxy subprocess | `app.py:~159` + `websocket_proxy/app_integration.py` | `start_websocket_proxy()` at app boot |
| ZMQ PUB/SUB bus (port 5555) | `websocket_proxy/connection_manager.py:111` | Shared singleton at import time |
| APScheduler instance | `services/historify_scheduler_service.py` | Shared scheduler, all strategies attach to it |
| Sector follow service + 5 jobs | `app.py:784` → `services/sector_follow_service.py:1794` | `init_sector_follow_service(app)` → `register_jobs()` |
| Futures follow service + 5 jobs | `app.py:835` → `services/futures_follow_service.py:1407` | `init_futures_follow_service(app)` → `register_jobs()` |
| Simplified engine service | `blueprints/chartink.py` at first webhook | Lazy init via `get_simplified_stock_engine_service()` |
| EOD watchdog | `app.py:~1140` → `services/eod_watchdog_service.py:92` | `start_eod_watchdog()` |
| Scanner comparison EOD | `app.py:849` → `services/scanner_comparison_eod_service.py:458` | `init_scanner_comparison_eod_service()` → `register_jobs()` |
| Sector follow backfill | `app.py:750` → `services/sector_follow_backfill_scheduler.py:282` | `init_sector_follow_backfill(app)` |
| Scanner universe backfill | `app.py:770` → `services/scanner_backfill_scheduler.py:335` | `init_scanner_backfill_scheduler(app)` |
| WS recovery service | `app.py` → `services/ws_recovery_service.py` | `init_ws_recovery_service(app)` |
| Telegram inbound bot | `services/telegram_inbound_service.py` | Only when `TELEGRAM_INBOUND_ENABLED=true` |
| Event subscribers | `app.py:172` → `subscribers.register_all()` | At boot |

**Processes triggered by login (not boot):**
- WS proxy adapter reconnect (via ZMQ CACHE_INVALIDATE — see Flow 21)
- Master contract refresh (via `hook_into_master_contract_download` on auth success — see Flow 4)
- Boot data convergence (daemon thread waits for broker session — see Flow 5)

**Current coverage:** ❌ No test verifies the full registration cascade — that `init_sector_follow_service` actually registers 5 APScheduler jobs with the correct triggers, or that the boot data convergence daemon thread starts. `test/test_singleton_guard.py` (2 tests) checks singleton init safety, not job registration.

---

### Flow 4 — Master contract load

**Trigger:** `handle_auth_success()` calls `hook_into_master_contract_download(username, broker)` → schedules a background download job via APScheduler

**Internal path:**
1. `utils/auth_utils.py:159` — `from database.master_contract_cache_hook import hook_into_master_contract_download`
2. `utils/auth_utils.py:327` — `hook_into_master_contract_download(username, broker)` — dispatches APScheduler one-shot job
3. `database/master_contract_cache_hook.py` — downloads broker's instrument file, populates `SymToken` table
4. On completion: `socketio.emit("cache_loaded", {...})` — notifies connected UI clients

**External boundaries:** Broker's instrument file download URL (HTTP)

**Backend state changes:** `openalgo.db:sym_token` table populated (potentially 10k+ rows); APScheduler job result stored

**Frontend reflection:** Nav bar symbol-search becomes available; `GET /master_contract_status` returns `loaded=True`

**Current coverage:** ⚠️ Partial — `test/test_master_contract_instrumenttype.py` (3 tests) verifies field mapping; `blueprints/master_contract_status.py` is wired but no test hits the load completion → SocketIO emit → UI state change.

---

### Flow 5 — Boot data convergence (sector_follow + scanner)

**Trigger:** On every app start, a daemon thread waits for a broker session (ZMQ CACHE_INVALIDATE), then runs convergence checks once

**Internal path (sector_follow):**
1. `services/sector_follow_backfill_scheduler.py:282` — `init_sector_follow_backfill(app)` — starts daemon thread
2. Thread polls for broker session, then calls `check_and_refresh_if_stale(today)` on:
   - `services/sector_follow_index_backfill.py:145` — 8 sector indices
   - `services/sector_follow_stock_backfill.py:136` — 30 LOCK_STATIC_30 stocks
3. Reads `MAX(timestamp)` per symbol from `historify.duckdb` via `data_freshness_service.compute_stale_symbols`
4. Fetches only stale symbols via `historify_service.create_and_start_job`
5. **Transient DuckDB lock** (`is_transient_lock_error`) → `logger.info` skip, mark not-fresh, retry

**Internal path (scanner):**
- Same pattern in `services/scanner_backfill_scheduler.py:335`
- Covers BOTH `1m` AND `D` intervals for full SCANNER_SYMBOLS F&O universe (~200 names)
- Writes `data_health_check` rows for `strategy_name='scanner_universe_1m'` and `'scanner_universe_D'`

**Current coverage:** ✅ `test/test_sector_follow_backfill_convergence.py` (10 tests), `test/test_scanner_universe_backfill.py` (17 tests). Well covered at unit level. Gap: no test exercises the DAEMON THREAD path end-to-end (boot → wait for session → trigger convergence → verify historify rows written).

---

### Flow 6 — Strategy mode resolution on startup

**Trigger:** Each strategy reads its mode on the first job execution (sector_follow.run_entry, futures_follow.run_entry, simplified engine on first webhook)

**Internal path:**
1. `database/strategy_mode_db.py:91` — `get_mode(strategy_name)` — reads `strategy_mode` table
2. `services/mode_service.resolve_strategy_mode(strategy_name, date)` — fall-through: unified → legacy → env → `sandbox/run`
3. `database/strategy_runtime_override_db.py` — checked at EVERY entry for an active pause/kill_switch override
4. Strategy dashboard `GET /api/strategies/status` — reads all strategy modes, exposes to UI

**Backend state changes:** Mode row read (no write); `strategy_runtime_override` rows checked

**Frontend reflection:** `/strategies` dashboard shows correct mode badges (sandbox/live), pause indicators

**Current coverage:** ✅ `test/test_strategy_mode.py` (11 tests), `test/test_mode_service.py` (19 tests), `test/test_strategy_runtime_override.py` (10 tests), `test/test_strategies_dashboard_api.py` (20 tests). The most comprehensive test coverage of any flow. Gap: no test verifies the strategies dashboard UI reflects mode changes reactively (Layer B gap).

---

### MARKET HOURS (Flows 7–12)

---

### Flow 7 — Live tick ingestion pipeline

**Trigger:** Broker WebSocket sends a tick message for a subscribed symbol

**Internal path:**
1. `broker/zerodha/streaming/zerodha_adapter.py` — receives tick, normalises to OpenAlgo format
2. Publishes to ZMQ PUB socket on `tcp://127.0.0.1:5555`, topic `market_data_<symbol>`
3. `websocket_proxy/server.py:103` — ZMQ SUB socket receives message
4. Delivers to connected browser WebSocket clients (port 8765)
5. In-process: `services/scanner_service.py` — bar aggregator consumes tick via separate ZMQ subscription → builds 1m/5m bars

**External boundaries:** Broker WebSocket (Zerodha kite.trade), ZMQ (inter-process)

**Backend state changes:** In-memory bar aggregator state updated; no DB write per-tick

**Frontend reflection:** `/websocket` page shows live LTP; scanner service accumulates bars

**Current coverage:** ⚠️ Partial — `test/test_bar_aggregator.py` (15 tests) covers bar building in isolation; `test/test_websocket_service.py` (8 tests) covers the service layer; `test/test_ws_proxy_full_integration.py` (0 tests implemented, file is scaffolded) and `test/test_ws_proxy_health.py` (6 tests) cover health checks. **Critical gap:** No test verifies the ZMQ PUB → ZMQ SUB → in-process aggregator chain end-to-end. The `test_ws_proxy_full_integration.py` is marked Linux-only (zmq.asyncio differs on Windows) so it never runs in CI.

---

### Flow 8 — Scanner evaluation (5m bar close → scan_results → SocketIO)

**Trigger:** 5m bar close event from bar aggregator; `_evaluate_definitions` runs in the scanner service

**Internal path:**
1. `services/scanner_service.py:1069` — `_evaluate_definitions(symbol, bar)` — runs all active definition rules
2. Rule modules in `services/scan_rules/` — e.g. `fno_intraday_buy_chartink.py` — evaluate gates
3. Market-hours gate: skips if outside `[09:15, 15:30]` IST (Tier-1 hardening 2026-06-15)
4. D-bar-date verify: aborts post-settle if latest daily bar is pre-today
5. On match: `database/scanner_db.py` — writes to `scan_results` table
6. SocketIO: `scan_hit` event emitted to connected clients

**External boundaries:** None (all in-process)

**Backend state changes:** `openalgo.db:scan_results` row inserted; `scan_cycle` heartbeat updated

**Frontend reflection:** `/scanner` page shows new signal row; signal count badge increments

**Current coverage:** ✅ `test/test_scanner_service.py` (40 tests), `test/test_fno_intraday_buy_chartink.py` (18 tests), `test/test_fno_intraday_sell_chartink.py` (18 tests), `test/test_scan_rules.py` (3), `test/test_scan_rules_parameterised.py` (5). Deep coverage. Gap: No test verifies the `scan_results` row write → SocketIO emit → frontend update chain (Layer B gap).

---

### Flow 9 — Scanner UI update (SocketIO subscription)

**Trigger:** `scan_hit` SocketIO event emitted after a scan_results row is written

**Frontend path:**
- React scanner page subscribes to `scan_hit` event via socket.io-client
- On event: appends new signal row to the table without page reload
- "Latest signals" section shows signals with date/time labels

**API polling fallback:**
- `GET /api/scanner/results?date=<today>` — used on page load to hydrate

**Current coverage:** ❌ No test. The SocketIO scan_hit emit is confirmed in code but no Layer B (Playwright) test verifies the browser table updates. The 2026-06-22 incident showed the UI displays historical rows without date labels — a UI regression that only Playwright would catch.

---

### Flow 10 — Chartink webhook → simplified engine arm → 5m candle → sandbox order

**Trigger:** `POST /chartink/simplified-stock-engine/<webhook_id>` from Chartink

**Internal path:**
1. `blueprints/chartink.py:959` — `simplified_stock_engine_webhook(webhook_id)` — validates API key + webhook ID
2. `blueprints/chartink.py:974` — `scan_cycle_service.start_cycle("chartink")` — creates audit row
3. Parses stock names from payload; calls `engine.activate_buy_symbol(symbol)` or `activate_sell_symbol(symbol)`
4. Engine loads historical candles from historify or live aggregator
5. On next 5m bar close: `engine.on_new_candle(symbol, candle)` — evaluates breakout rules
6. On signal: `svc._place_entry_order(sig, ...)` → `sandbox_place_order(...)` (in sandbox mode)
7. `database/trade_journal_db.py` — `record_entry(...)` writes `trade_journal` row

**External boundaries:** Chartink HTTP webhook (inbound); sandbox DB

**Backend state changes:** `scan_cycle` audit row; `trade_journal` entry row; sandbox order row

**Frontend reflection:** `/chartink/simplified-engine/api/status` reflects armed symbols

**Current coverage:** ✅ `test/e2e/test_chartink_webhook_to_sandbox.py` (4 scenarios — BUY breakout, empty payload, ATR stop, trailing stop). `test/test_simplified_stock_engine_service.py` (63 tests). `test/test_chartink_webhook_audit.py` (4 tests). Well covered. **Gap:** No test hits the actual `/chartink/simplified-stock-engine/<id>` HTTP route end-to-end (the e2e tests call `engine.on_new_candle` directly, bypassing the webhook HTTP layer).

---

### Flow 11 — Sandbox order placement → sandbox.db write → position visible

**Trigger:** `_place_entry_order` or `_place_exit_order` in any strategy service, in sandbox mode

**Internal path:**
1. `services/sandbox_service.py:sandbox_place_order(order_dict, api_key)` — validates margin via `fund_manager`
2. `sandbox/fund_manager.py` — checks available capital, blocks if margin insufficient
3. `sandbox/execution_engine.py:36` — `ExecutionEngine.place_order(...)` — simulates fill at LTP; writes `SandboxOrders`, `SandboxPositions`, `SandboxTrades` rows
4. Publishes `SandboxOrderFilledEvent` (event bus, NOT ZMQ)
5. `socketio.emit("analyzer_update")` — UI notified

**External boundaries:** None (pure in-process, sandbox.db only)

**Backend state changes:** `sandbox.db` tables: `SandboxOrders` (new row), `SandboxPositions` (qty change), `SandboxTrades` (fill row)

**Frontend reflection:** `/positions` page shows new position; `SandboxTrades` visible in order history

**Current coverage:** ✅ `test/sandbox/test_fund_manager.py` (5), `test/sandbox/test_margin_scenarios.py` (4), `test/sandbox/test_sandbox_order_flow.py` (4), `test/sandbox/test_cnc_sell_validation.py` (6). Good sandbox-layer coverage. **Gap:** No test verifies the `sandbox_place_order` → `fund_manager` → `ExecutionEngine` → DB write → `positions` API chain. The sandbox tests call `ExecutionEngine` directly; the `sandbox_place_order` service wrapper is untested end-to-end.

---

### Flow 12 — Sandbox position visible on `/positions` UI

**Trigger:** After Flow 11 writes to sandbox.db

**Internal path:**
1. `GET /api/v1/positions` → `restx_api/positions.py` — reads `SandboxPositions` or live broker positions
2. `resolve_effective_mode()` — determines sandbox vs. live path
3. Response JSON: `[{symbol, qty, avg_price, ltp, pnl}]`
4. SocketIO: `analyzer_update` event pushes real-time MTM updates

**Current coverage:** ❌ No test. `test/test_read_services_dispatch.py` (16 tests) tests dispatch routing but mocks the response; no test verifies `SandboxPositions` rows are returned correctly via the API after a real sandbox order fill.

---

### CLOSE-OF-MARKET (Flows 13–18)

---

### Flow 13 — Sector_follow 15:18 smoke check + 15:20 entry

**Trigger:** APScheduler fires `sector_follow_smoke_check` job at 15:18 IST, then `sector_follow_entry` at 15:20 IST

**Internal path (smoke check):**
1. `services/sector_follow_service.py:1849` — job fires, calls `_SINGLETON.assert_data_pipeline_healthy()`
2. `services/sector_follow_service.py:1398` — checks: (a) aggregator has today's bars for ≥50% of universe, (b) historify has prior-day data for sample symbol, (c) broker session live
3. On fail: writes `strategy_runtime_override(type='pause', expires_at=15:30)` + Telegram alert

**Internal path (entry):**
1. `services/sector_follow_service.py:1813` — `run_entry()` fires at 15:20
2. `services/sector_follow_service.py:1195` — `run_entry()`:
   - Checks `_entry_held_by_override()` (smoke_check pause, kill_switch)
   - `evaluate_candidates(universe, as_of)` — reads aggregator for today's data, historify for 20d lookback
   - Sorts by vol_ratio descending, selects ≤5
   - Places BUY orders via `production_order_placer(mode, order_dict)` → sandbox/live

**Current coverage:** ✅ `test/test_sector_follow_service.py` (60 tests), `test/test_scanner_smoke_check.py` (13 tests), `test/test_sector_follow_full_cycle.py` (scaffolded — 0 tests implemented). **Critical gap:** The `test_sector_follow_full_cycle.py` is empty — the full `smoke_check → held_by_override → run_entry → evaluate_candidates → order_placer` chain is never exercised in a single test with real DB fixtures.

---

### Flow 14 — Futures_follow 15:20 entry

**Trigger:** APScheduler `futures_follow_entry` fires at 15:20 IST

**Internal path:**
1. `services/futures_follow_service.py:1436` — job fires, calls `_SINGLETON.run_entry()`
2. `services/futures_follow_service.py:958` — `run_entry()`:
   - Reuses sector_follow `evaluate_candidates()` to get today's top-5 stock signals
   - For each signal: resolves NIFTY near-month futures symbol via master contract (skips contracts expiring ≤1 day)
   - Sizes NIFTY lots up to 50% of capital as overnight SPAN margin
   - Places NFO NRML MARKET order via sandbox/live
3. Writes `futures_follow_trades` journal row

**External boundaries:** Master contract (SymToken table) for futures symbol resolution

**Backend state changes:** `futures_follow_trades` row; sandbox order (NFO NRML)

**Frontend reflection:** `/futures_follow_cap50/api/status` reflects open positions

**Current coverage:** ✅ `test/test_futures_follow_service.py` (46 tests), `test/test_futures_follow_blueprint.py` (9 tests). Good coverage. **Gap:** No test verifies the master contract futures symbol resolution when the current-month contract expires within 1 day (the "last Tuesday" edge case — NIFTY monthly expiry roll).

---

### Flow 15 — EOD watchdog 15:14 flatten

**Trigger:** APScheduler `futures_follow_eod_watchdog` (and `simplified_engine_eod_watchdog`) fires at 15:14 IST

**Internal path:**
1. `services/eod_watchdog_service.py:92` — `start_eod_watchdog()` — registers with APScheduler
2. `flatten_strategy_positions(strategy_name)` — reads `trade_journal` for open entries, places SELL/BUY orders to close
3. Hard cap: fires at `min(strategy.eod_exit_time, SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME=15:14)` — LOAD-BEARING: sandbox rejects MIS orders at/after 15:15

**Current coverage:** ✅ `test/test_eod_watchdog_service.py` (29 tests). Well covered. **Gap:** `test/test_eod_three_layer_defense.py` is scaffolded but has 0 implemented tests — the sequential three-layer (tick-driven → watchdog → reconciliation) integration is unverified.

---

### Flow 16 — Sector_follow 15:25 exit (T+1 square-off)

**Trigger:** APScheduler `sector_follow_exit` fires at 15:25 IST

**Internal path:**
1. `services/sector_follow_service.py:1820` — `run_exit()` fires
2. `services/sector_follow_service.py:1224` — `run_exit()`:
   - Reads `paper_book` (in-memory positions dict) + `sector_follow_trades` DB for open T+1 holds
   - Does NOT check override gate (exits are never blocked)
   - Places SELL MARKET orders via order placer
3. Writes exit row to `sector_follow_trades`

**Current coverage:** ⚠️ Partial — `test_sector_follow_service.py` tests the run_exit logic but as a unit (fake placer). `test_critical_flows.py::TestSectorFollowFullCycle.test_entry_then_next_day_exit` covers the entry→exit cycle but is marked `skip` (retired intent-DB architecture). **Gap:** No active integration test exercises the 15:20 entry → T+1 15:25 exit cycle with real DB writes + real order placer routing.

---

### Flow 17 — EOD reconciliation 15:30

**Trigger:** `_maybe_reconcile_eod_journal(today)` called before `_maybe_log_eod_summary` in the simplified engine; also the 15:30 `sector_follow_eod_summary` job

**Internal path (simplified engine):**
1. `services/engine_eod_reconciliation_service.py:150` — `reconcile_engine_journal(today)` — reads `sandbox.db` positions **read-only**
2. For positions closed by sandbox MIS auto-square-off (not by the engine itself): stamps `trade_journal` exit row with `exit_reason='sandbox_eod_squareoff'`, gross P&L
3. Idempotent: skips if exit row already exists

**Current coverage:** ✅ `test/e2e/test_engine_eod_reconciliation.py` (8 tests). Well covered. **Gap:** No test exercises the reconciliation path when sandbox closes positions AT the 15:30 boundary vs. a mid-day close.

---

### Flow 18 — Scanner-vs-Chartink 15:45 comparison

**Trigger:** APScheduler `scanner_comparison_eod` fires at 15:45 IST (configurable via `SCANNER_COMPARISON_EOD_TIME`)

**Internal path:**
1. `services/scanner_comparison_eod_service.py:458` — job fires; calls `run_comparison_for_date(today)`
2. `services/scanner_comparison_eod_service.py:373` — `compute_comparison(date)`:
   - Chartink side: `scan_cycle` rows with `cycle_kind='chartink'`
   - In-house side: `scan_results` rows with `source='inhouse'`
   - Computes per-side counts, intersection, Jaccard, recall, top diffs
3. Writes `scanner_comparison` row (delete-then-insert — idempotent)
4. Sends Telegram via `notification_service.notify("scanner_comparison", ...)`

**Current coverage:** ✅ `test/test_scanner_comparison_eod_service.py` (3 tests), `test/test_scan_comparison.py` (8 tests), `test/test_scanner_comparison_blueprint.py` (5 tests). Covered. **Gap:** No test verifies the Telegram notification fires correctly (distinct from the comparison computation itself).

---

### POST-CLOSE (Flows 19–21)

---

### Flow 19 — EOD report generation (sector_follow + futures_follow)

**Trigger:** `sector_follow_eod_summary` APScheduler job at 15:30 IST; `futures_follow_eod_summary` at 15:30 IST

**Internal path:**
1. `services/sector_follow_service.py:1835` — `run_eod_summary()` — formats Markdown, writes `strategies/sector_follow_cap5_vol/eod_reports/YYYY-MM-DD.md`, sends Telegram
2. Same pattern in `services/futures_follow_service.py:1450` for futures_follow

**Current coverage:** ⚠️ Partial — `test/e2e/test_critical_flows.py::TestEODReport` (2 tests — `test_report_written_with_sections`, `test_telegram_sink_failure_does_not_block_file`) are SKIPPED (retired intent-DB architecture). These were good tests that need rewriting for the mode-only arch.

---

### Flow 20 — Data freshness check 16:30

**Trigger:** APScheduler `sector_follow_data_health` fires at 16:30 IST

**Internal path:**
1. `services/sector_follow_service.py:1841` — `run_data_health_check()` fires
2. `services/data_freshness_service.py` — `check_strategy_data_ready(strategy, date, max_staleness_business_days=1)` — reads `historify.duckdb` for 8 indices + 30 stocks
3. On stale: writes `data_health_check` row, sends Telegram alert, writes `strategy_runtime_override(type='pause', expires_at=tomorrow 15:30)` to auto-pause tomorrow's entries
4. Self-expiring: a one-off stale day never permanently disables the strategy

**Current coverage:** ✅ `test/test_data_freshness_service.py` (15 tests). `test/e2e/test_critical_flows.py::TestDataFreshnessValidation` (2 tests — stale + fresh) but SKIPPED. **Gap:** The auto-pause side-effect (writing `strategy_runtime_override`) is tested only in the skipped file; no active test covers this.

---

### Flow 21 — WS proxy reconnect on re-login (ZMQ CACHE_INVALIDATE)

**Trigger:** `upsert_auth()` (called from `handle_auth_success`) publishes ZMQ `CACHE_INVALIDATE_AUTH_<user_id>`

**Internal path:**
1. `database/cache_invalidation.py:45` — `CacheInvalidationPublisher.publish_invalidation()` — ZMQ PUB send
2. `websocket_proxy/server.py:1699` — ZMQ SUB listener wakes on `CACHE_INVALIDATE` prefix
3. `websocket_proxy/server.py:1456` — `_handle_cache_invalidation()` — snapshots held subscriptions
4. Closes existing broker adapter connection
5. Re-reads fresh token via `adapter.initialize()` (reads from auth_db)
6. Reconnects; re-subscribes all held symbols
7. Failure-graceful: rejected token → `logger.exception`, retains snapshot for next auth

**Current coverage:** ✅ `test/test_broker_session_auto_reconnect.py` (7 tests) — hermetic, uses `WebSocketProxy.__new__` to bypass port binding. **Gap:** No test starts a REAL WebSocket proxy process and verifies that a ZMQ CACHE_INVALIDATE event (from a real `upsert_auth` call) causes observable subscription resumption. `test/test_ws_proxy_full_integration.py` is scaffolded but Linux-only and 0 tests implemented.

---

### Additional Flows (discovered)

---

### Flow 22 — Pre-entry data pipeline (sector_follow 15:18 aggregator smoke check)

**Trigger:** 15:18 smoke check job (Flow 13 sub-step), but the aggregator sourcing for TODAY's data (vs. historify for 20d lookback) is a distinct, separately-testable flow introduced 2026-06-15

**Key seam:** `services/sector_follow_service.py` `production_intraday_provider` → `ScannerService.get_today_ohlcv(symbol, date)` — reads in-process aggregator bars, NOT historify

**Current coverage:** ⚠️ `test/test_scanner_smoke_check.py` (13 tests) covers the smoke check pass/fail. No test exercises the aggregator-read path vs. historify fallback for today's intraday data.

---

### Flow 23 — Strategy mode persistence across restart

**Trigger:** App restart (e.g. after deploy, code fix, or nightly Zerodha token expiry restart)

**Critical property:** `strategy_mode` rows in `openalgo.db` survive — strategies do NOT revert to scaffold after restart; they resume at their last explicit `mode`

**Verification path:**
1. Write `strategy_mode(strategy_name='sector_follow_cap5_vol', mode='sandbox')` row
2. Tear down and recreate Flask app
3. Call `database/strategy_mode_db.get_mode('sector_follow_cap5_vol')` — must return `sandbox`

**Current coverage:** ❌ No test. The mode persistence contract is load-bearing (an accidental revert to sandbox→scaffold would disable trading silently) but is only verified by reading code, not by an automated test.

---

### Flow 24 — Strategy dashboard API (real-time mode + override status)

**Trigger:** Frontend polls or SocketIO-receives updates for `/api/strategies/status`

**Internal path:**
1. `blueprints/strategies_dashboard.py` or `restx_api/` — reads all strategy modes, active overrides, kill-switch status
2. Returns JSON for each strategy: `{name, mode, intent, override_active, kill_switch, daily_pnl}`

**Current coverage:** ✅ `test/test_strategies_dashboard_api.py` (20 tests) — comprehensive. **Gap:** No Layer B test verifies the `/strategies` page SHOWS the correct badge/indicator when a `strategy_runtime_override` is active.

---

## Phase 2 — Coverage Map

| Flow | Name | Layer-A tests? | Layer-B tests? | Status |
|------|------|---------------|---------------|--------|
| 1 | OpenAlgo login | ❌ None | ❌ None | ❌ No test |
| 2 | Zerodha OAuth | ⚠️ Resume path only | ❌ None | ⚠️ Partial |
| 3 | Background processes on login | ❌ None | ❌ None | ❌ No test |
| 4 | Master contract load | ⚠️ Field mapping only | ❌ None | ⚠️ Partial |
| 5 | Boot data convergence | ✅ Unit (backfill logic) | ❌ None | ⚠️ Daemon thread path untested |
| 6 | Strategy mode resolution | ✅ Comprehensive | ❌ None | ✅ Well covered |
| 7 | Live tick ingestion | ⚠️ Bar aggregator unit | ❌ None | ⚠️ ZMQ chain untested |
| 8 | Scanner evaluation | ✅ Comprehensive | ❌ None | ✅ Well covered |
| 9 | Scanner UI update | ❌ None | ❌ None | ❌ No test |
| 10 | Chartink webhook → engine | ✅ Engine core + service | ❌ None | ⚠️ HTTP route not tested |
| 11 | Sandbox order placement | ✅ Sandbox layer | ❌ None | ⚠️ Service wrapper untested |
| 12 | Position visible in UI | ❌ None | ❌ None | ❌ No test |
| 13 | Sector_follow 15:18+15:20 | ⚠️ Unit; full_cycle=0 tests | ❌ None | ⚠️ Full cycle gap |
| 14 | Futures_follow 15:20 | ✅ 46 tests | ❌ None | ⚠️ Expiry-roll edge untested |
| 15 | EOD watchdog 15:14 | ✅ 29 tests | ❌ None | ⚠️ 3-layer defense empty |
| 16 | Sector_follow 15:25 exit | ⚠️ Unit; e2e=skipped | ❌ None | ⚠️ Partial |
| 17 | EOD reconciliation 15:30 | ✅ 8 e2e tests | ❌ None | ✅ Well covered |
| 18 | Scanner comparison 15:45 | ✅ 16 tests total | ❌ None | ✅ Well covered |
| 19 | EOD reports | ⚠️ Tests skipped | ❌ None | ⚠️ Skipped |
| 20 | Data freshness 16:30 | ⚠️ Unit; e2e=skipped | ❌ None | ⚠️ Auto-pause side-effect untested |
| 21 | WS proxy reconnect | ✅ 7 hermetic tests | ❌ None | ⚠️ Real proxy path untested |
| 22 | Aggregator smoke check | ⚠️ Smoke unit | ❌ None | ⚠️ Aggregator-vs-historify switch |
| 23 | Mode persistence (restart) | ❌ None | ❌ None | ❌ No test |
| 24 | Strategy dashboard | ✅ 20 tests | ❌ None | ⚠️ UI layer gap |

**Summary:**
- ❌ No test: **6 flows** (1, 3, 9, 12, 23 + new flow 3 background-process cascade)
- ⚠️ Partial: **11 flows** (2, 4, 5, 7, 10, 11, 13, 16, 19, 20, 21)
- ✅ Well covered: **7 flows** (6, 8, 14, 15, 17, 18, 24)

**Layer B (UI) coverage: 0 of 24 flows.** No Playwright test exists for any frontend interaction.

---

## Phase 3 — E2E Harness Architecture

### Layer A — Backend Integration Harness

**New class:** `test/harness.py` — `BootHarness`

```python
# Proposed API — docs only, not implemented here
class BootHarness:
    """Boots a minimal Flask app with all strategy services wired, using in-memory DBs."""

    @classmethod
    def create(cls, *, broker_mode="mock", strategy_modes=None) -> "BootHarness":
        # 1. Creates temp dir for all DB env vars (conftest already does this globally)
        # 2. Calls create_app(testing=True)
        # 3. Injects a mock broker adapter (no network calls)
        # 4. Builds APScheduler in manual-trigger mode (no background threads)
        # 5. Calls all init_* functions (sector_follow, futures_follow, backfill, etc.)
        # 6. Returns self with app + test_client

    def seed_historify(self, symbol: str, bars: list[dict]) -> None:
        """Write synthetic OHLCV rows into temp historify.duckdb."""

    def inject_tick(self, symbol: str, ltp: float, volume: int) -> None:
        """Push a tick into the in-process bar aggregator directly (no ZMQ)."""

    def advance_bars(self, symbol: str, *, n_minutes: int) -> None:
        """Advance the bar aggregator clock by N minutes, closing bars along the way."""

    def fire_job(self, job_id: str) -> None:
        """Manually trigger a named APScheduler job synchronously."""

    def mock_broker_login(self, *, user="admin", broker="zerodha") -> None:
        """Insert a valid auth row directly, bypassing OAuth."""

    def assert_strategy_state(self, strategy: str, *, mode: str, override_active: bool = False) -> None:
        """Assert strategy_mode table + runtime_override table match expected state."""

    def assert_sandbox_position(self, symbol: str, *, qty_gt: int = 0) -> None:
        """Assert sandbox.db has an open position for symbol."""

    def assert_journal_entry(self, symbol: str, *, exit_reason: str | None = None) -> None:
        """Assert trade_journal has a row for symbol with matching exit_reason."""
```

**Key design decisions:**

1. **APScheduler in manual mode.** Tests call `harness.fire_job("sector_follow_entry")` rather than waiting for wall-clock time. This makes the full 15:18→15:20→15:25→15:30 sequence runnable in milliseconds.

2. **No ZMQ for bar ingestion in Layer A.** `inject_tick` writes directly into the bar aggregator's in-process dict, bypassing the ZMQ PUB/SUB hop. This keeps tests deterministic and sub-second. ZMQ integration is Layer B territory.

3. **Mock broker adapter.** A thin `MockBrokerAdapter` records calls and returns configurable responses. Wired via `BROKER_API_URL` env override (same pattern as `test/fixtures/mock_broker/`). Not a full FastAPI server — just a dict of stub functions.

4. **Real DB layer.** `conftest.py` already redirects all DB env vars to a temp dir. Layer A tests use this — all SQLAlchemy writes go to real (temp) SQLite, making assertions against actual DB state (not mocks).

5. **deterministic `now()`.** Every service that takes a `now` provider uses an injected clock. `BootHarness` provides `harness.set_clock(dt.datetime(2026, 6, 24, 15, 20))` to control what "now" is for scheduled jobs.

---

### Layer B — Playwright Frontend E2E

**Approach:** Start the `BootHarness` backend, then drive Playwright against `http://127.0.0.1:5000`.

```python
# test/e2e_playwright/conftest.py
@pytest.fixture(scope="session")
def app_server():
    harness = BootHarness.create(broker_mode="mock")
    harness.mock_broker_login()
    thread = threading.Thread(target=harness.app.run, kwargs={"port": 5000})
    thread.daemon = True
    thread.start()
    yield harness
    # teardown

@pytest.fixture(scope="session")
def browser(app_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
```

**5 target flows for Layer B:**

1. Admin login → dashboard (Flow 1)
2. Scanner page receives scan_hit SocketIO event → row appears (Flow 9)
3. Strategies page shows correct mode badges (Flow 24)
4. Position appears in `/positions` after sandbox fill (Flow 12)
5. Strategies dashboard updates badge when runtime_override is active (Flow 6 + 24 combo)

---

### Layer C — Synthetic Trading Day

**A single test in `test/e2e/test_synthetic_day.py`** that exercises all phases in sequence:

```
harness = BootHarness.create(clock=dt.datetime(2026, 6, 24, 9, 0))

# PRE-OPEN
harness.mock_broker_login()
harness.seed_historify(UNIVERSE, bars_20d)
harness.fire_job("sector_follow_boot_convergence")
assert harness.assert_historify_fresh(UNIVERSE)

# 15:18 SMOKE CHECK
harness.set_clock(15, 18)
harness.inject_aggregator_bars(UNIVERSE, today_bars)
harness.fire_job("sector_follow_smoke_check")
assert harness.no_smoke_override_active()

# 15:20 ENTRY
harness.set_clock(15, 20)
harness.fire_job("sector_follow_entry")
assert harness.n_open_positions("sector_follow") == 5

# 15:14 WATCHDOG (simplified engine)
harness.seed_open_trade_journal("simplified_engine", "RELIANCE")
harness.set_clock(15, 14)
harness.fire_job("simplified_engine_eod_watchdog")
assert harness.assert_journal_entry("RELIANCE", exit_reason__in=["eod_watchdog", "sandbox_eod_squareoff"])

# 15:25 EXIT
harness.set_clock(15, 25)
harness.fire_job("sector_follow_exit")
assert harness.n_open_positions("sector_follow") == 0

# 15:30 EOD RECONCILIATION
harness.fire_job("sector_follow_eod_summary")
assert harness.eod_report_written_today("sector_follow_cap5_vol")

# 15:45 COMPARISON
harness.fire_job("scanner_comparison_eod")
assert harness.scanner_comparison_row_exists(today)

# 16:30 DATA HEALTH
harness.fire_job("sector_follow_data_health")
assert harness.data_health_row_exists(today, overall_ok=True)

# ASSERT NO ERRORS
assert harness.error_log_entries() == 0
```

Total runtime target: **<60 seconds**.

---

## Phase 4 — Per-Flow Test Plan

### Flow 1 — OpenAlgo admin login

**Layer:** A + B
**File:** `test/test_login_flow.py` (new; extend `test/test_logout_csrf.py`)
**Setup:** BootHarness with seeded user (password in temp user_db)
**Acts:** POST `/auth/login` with correct credentials, then with wrong credentials
**Asserts:** (A) 302 redirect on success, session cookie set, `_try_resume_broker_session` called; 401 on wrong password. (B-Layer) Playwright: login form → dashboard redirect → nav bar shows username
**Priority:** P0

---

### Flow 2 — Zerodha OAuth callback + token storage

**Layer:** A
**File:** `test/test_zerodha_auth.py` (new)
**Setup:** BootHarness; mock `broker/zerodha/api/auth_api.authenticate` to return `{"access_token": "tok_xyz"}`
**Acts:** GET `/zerodha/callback?request_token=rt_abc` with a logged-in session
**Asserts:** `auth_db.get_auth_token("admin")` returns decrypted `"tok_xyz"`; session `logged_in=True`; ZMQ publish called once with `CACHE_INVALIDATE_AUTH_*` topic
**Priority:** P0

---

### Flow 3 — All background jobs registered at boot

**Layer:** A
**File:** `test/test_boot_broker_session.py` (exists, 0 tests — fill it)
**Setup:** BootHarness.create() with APScheduler in list mode
**Acts:** Read `scheduler.get_jobs()`
**Asserts:** Job IDs present: `sector_follow_smoke_check`, `sector_follow_entry`, `sector_follow_exit`, `sector_follow_eod_summary`, `sector_follow_data_health`, `futures_follow_entry`, `futures_follow_exit`, `futures_follow_eod_watchdog`, `scanner_comparison_eod`. All have correct cron triggers (IST timezone, correct hour/minute).
**Priority:** P0

---

### Flow 4 — Master contract load + SocketIO emit

**Layer:** A
**File:** `test/test_master_contract_flow.py` (new)
**Setup:** BootHarness; mock broker instrument-file HTTP download
**Acts:** Call `hook_into_master_contract_download("admin", "zerodha")` + wait for job
**Asserts:** `sym_token` table has ≥1 row; `socketio.emit("cache_loaded")` was called (monkeypatched)
**Priority:** P1

---

### Flow 5 — Boot data convergence daemon (convergence runs once on session)

**Layer:** A
**File:** `test/test_sector_follow_backfill_convergence.py` (exists, 10 tests — add 1 more)
**Setup:** BootHarness with seeded historify rows dated yesterday; mock broker session present
**Acts:** Call `init_sector_follow_backfill(app)` and trigger the daemon thread synchronously via monkeypatching the thread creation
**Asserts:** `check_and_refresh_if_stale` was called; stale symbols fetched; `data_health_check` rows written
**Priority:** P1

---

### Flow 6 — Mode persists after app restart

**Layer:** A
**File:** `test/test_strategy_mode.py` (exists — add 1 test)
**Setup:** Write `strategy_mode(strategy='sector_follow_cap5_vol', mode='sandbox')` row
**Acts:** Call `create_app(testing=True)` a second time in the same temp DB env
**Asserts:** `get_mode('sector_follow_cap5_vol')` returns `mode='sandbox'`
**Priority:** P0

---

### Flow 7 — Tick → bar aggregator → scanner evaluation fires

**Layer:** A
**File:** `test/test_tick_to_scan_integration.py` (new)
**Setup:** BootHarness; seed scanner definitions; inject 5 minutes of 1m bars via `inject_tick`
**Acts:** Close the 5m bar; call `_evaluate_definitions` directly
**Asserts:** `scan_results` table has a row for the matching symbol; `scan_hit` SocketIO event emitted
**Priority:** P1

---

### Flow 8 — Scanner evaluation (extend existing)

**Layer:** A
**File:** `test/test_scanner_service.py` (exists, 40 tests — add 2 more for market-hours gate + D-bar date verify)
**Asserts:** `_evaluate_definitions` called at 17:00 → 0 results (post-close gate); `_evaluate_definitions` with stale D-bar → WARNING logged
**Priority:** P1

---

### Flow 9 — Scanner UI: scan_hit event → row appears in browser

**Layer:** B (Playwright)
**File:** `test/e2e_playwright/test_scanner_ui.py` (new)
**Setup:** Start BootHarness backend; navigate to `/scanner`
**Acts:** Emit `scan_hit` SocketIO event with a synthetic signal
**Asserts:** A new `<tr>` row containing the symbol appears within 2 seconds; row has today's date label
**Priority:** P1

---

### Flow 10 — Chartink webhook HTTP route end-to-end

**Layer:** A
**File:** `test/test_chartink_webhook_audit.py` (exists, 4 tests — add 1 HTTP-route test)
**Setup:** BootHarness; inject valid webhook_id via seeded `scan_definitions`
**Acts:** `client.post("/chartink/simplified-stock-engine/<webhook_id>", json={"stocks": "RELIANCE", ...})`
**Asserts:** HTTP 200; `scan_cycle` row created; engine `_armed_symbols` contains "RELIANCE"
**Priority:** P0

---

### Flow 11 — `sandbox_place_order` → sandbox.db → fund manager

**Layer:** A
**File:** `test/sandbox/test_sandbox_order_flow.py` (exists, 4 tests — add 1 cross-service test)
**Setup:** BootHarness; inject LTP via mock broker quote service
**Acts:** Call `sandbox_service.sandbox_place_order({"symbol": "RELIANCE", "action": "BUY", "qty": 10, "product": "MIS", ...}, api_key="k")`
**Asserts:** `SandboxOrders` DB row exists with status FILLED; `SandboxPositions` qty=10; returned `status=='success'`; `fund_manager.available_margin` reduced
**Priority:** P0

---

### Flow 12 — Position visible via `/api/v1/positions` after sandbox fill

**Layer:** A + B
**File:** `test/test_positions_api.py` (new)
**Setup:** BootHarness; seed `SandboxPositions` row
**Acts-A:** `client.get("/api/v1/positions?apikey=k")`
**Asserts-A:** Response contains the seeded symbol with `qty > 0`
**Acts-B:** Playwright on `/positions` page; assert position card visible
**Priority:** P1

---

### Flow 13 — Sector_follow full cycle (smoke → entry → override gate)

**Layer:** A
**File:** `test/test_sector_follow_full_cycle.py` (exists, 0 tests — fill it)
**Setup:** BootHarness; seed historify (20d lookback); inject today's aggregator bars; `strategy_mode = sandbox`
**Acts:** `harness.fire_job("sector_follow_smoke_check")` → `harness.fire_job("sector_follow_entry")`
**Asserts:** No pause override written (smoke passed); `sector_follow_trades` has 5 rows; each row has `status='pending_exit'`
**Priority:** P0

---

### Flow 14 — Futures_follow expiry-roll edge case

**Layer:** A
**File:** `test/test_futures_follow_service.py` (exists, 46 tests — add 1)
**Setup:** Seed SymToken with NIFTY-JUN26 expiry = today (expiring); NIFTY-JUL26 expiry = 30 days out
**Acts:** `futures_follow_service.resolve_futures_symbol("NIFTY", as_of=today)`
**Asserts:** Returns JUL26 contract (skips same-day expiry); never returns JUN26
**Priority:** P1

---

### Flow 15 — EOD three-layer defense in sequence

**Layer:** A
**File:** `test/test_eod_three_layer_defense.py` (exists, 0 tests — fill it)
**Setup:** BootHarness; seed `trade_journal` with open MIS position in simplified engine
**Acts:** (1) `engine.check_eod_exits()` — tick-driven layer skips (clock = 15:10); (2) `harness.fire_job("simplified_engine_eod_watchdog")` — watchdog at 15:14 closes it; (3) `reconcile_engine_journal(today)` — reconciliation is a no-op (already closed)
**Asserts:** After step 2: `trade_journal` has exit row with `exit_reason='eod_watchdog'`; after step 3: no duplicate exit row; `sandbox_eod_squareoff` count = 0
**Priority:** P0

---

### Flow 16 — Sector_follow T+1 exit cycle (entry → next-day exit)

**Layer:** A
**File:** `test/test_sector_follow_full_cycle.py` (continue from Flow 13 test)
**Setup:** Sector_follow positions from Flow 13 test; advance clock to T+1 15:25
**Acts:** `harness.fire_job("sector_follow_exit")`
**Asserts:** All `sector_follow_trades` rows have `exit_at` set; `paper_book == {}`; SELL orders placed via mock order placer
**Priority:** P0

---

### Flow 17 — EOD reconciliation (sandbox_eod_squareoff stamps journal)

**Layer:** A (extend existing)
**File:** `test/e2e/test_engine_eod_reconciliation.py` (exists, 8 tests — already ✅)
**No new test needed.** Existing suite covers this well.
**Priority:** Already covered

---

### Flow 18 — Scanner comparison Telegram notification

**Layer:** A
**File:** `test/test_scanner_comparison_eod_service.py` (exists, 3 tests — add 1)
**Setup:** Seed `scan_cycle` + `scan_results` rows; mock `notification_service.notify`
**Acts:** `run_comparison_for_date(today)`
**Asserts:** `notify` called once with `event_type="scanner_comparison"` and a non-empty message body
**Priority:** P2

---

### Flow 19 — EOD report written (sector_follow, mode-only arch)

**Layer:** A
**File:** `test/e2e/test_critical_flows.py` (exists, but skipped — REWRITE 2 tests)
**Setup:** BootHarness; sector_follow with strategy_mode=sandbox (not intent table)
**Acts:** `harness.fire_job("sector_follow_eod_summary")`
**Asserts:** `eod_reports/YYYY-MM-DD.md` exists; file contains `## Summary`, `## Positions`, `## Kill switch`
**Priority:** P1

---

### Flow 20 — Data freshness auto-pause side-effect

**Layer:** A
**File:** `test/test_data_freshness_service.py` (exists, 15 tests — add 1)
**Setup:** BootHarness; monkeypatch `check_strategy_data_ready` to return stale NIFTY
**Acts:** `harness.fire_job("sector_follow_data_health")`
**Asserts:** `data_health_check` row written (overall_ok=False); `strategy_runtime_override` row written with `type='pause'`, `expires_at=tomorrow 15:30`
**Priority:** P0

---

### Flow 21 — WS proxy reconnect (real ZMQ, non-Windows CI)

**Layer:** A
**File:** `test/test_ws_proxy_full_integration.py` (exists, 0 tests — fill it)
**Skip-marker:** `pytest.mark.skipif(sys.platform == "win32", ...)` (zmq.asyncio constraint)
**Setup:** Start `WebSocketProxy` with a real ZMQ SUB socket; seed auth_db with a token
**Acts:** Call `upsert_auth("admin", "new_tok", "zerodha")` → publish ZMQ CACHE_INVALIDATE
**Asserts:** Within 2 seconds: `proxy._last_known_subscriptions` was snapshotted; `adapter.initialize()` called with new token; subscription re-established
**Priority:** P1 (Linux CI only)

---

### Flow 22 — Aggregator-vs-historify source switch for today's data

**Layer:** A
**File:** `test/test_scanner_smoke_check.py` (exists, 13 tests — add 2)
**Test A:** Aggregator has today bars → `intraday_source='aggregator'`, no fallback warning logged
**Test B:** Aggregator empty → `intraday_source='historify'`, WARNING logged "aggregator had no today bars"
**Priority:** P1

---

### Flow 23 — Strategy mode persistence across restart

**Layer:** A
**File:** `test/test_strategy_mode.py` (exists, 11 tests — add 1)
**Setup:** Write `strategy_mode(strategy='sector_follow_cap5_vol', mode='sandbox')`
**Acts:** Call `create_app(testing=True)` again in same temp DB dir; call `init_db()` for strategy_mode
**Asserts:** `get_mode('sector_follow_cap5_vol')` returns `{'mode': 'sandbox'}` — row survived
**Priority:** P0

---

### Flow 24 — Strategies dashboard UI reflects active override

**Layer:** B (Playwright)
**File:** `test/e2e_playwright/test_strategies_ui.py` (new)
**Setup:** BootHarness; write `strategy_runtime_override(type='pause', strategy='sector_follow_cap5_vol')`
**Acts:** Navigate to `/strategies`
**Asserts:** sector_follow card shows "PAUSED" badge or equivalent indicator
**Priority:** P2

---

### Synthetic day (Layer C)

**Layer:** C
**File:** `test/e2e/test_synthetic_day.py` (new)
**Runs ALL 24 flows in sequence** using BootHarness with manual APScheduler triggers and an injected clock. Total runtime target: <60s.
**Priority:** P1 (after Layer A harness is built)

---

## Phase 5 — Effort + Rollout Plan

### Phase 1 of build: BootHarness + P0 Layer-A tests (~3–4 days)

1. Implement `test/harness.py` — `BootHarness` class:
   - Manual APScheduler mode
   - Mock broker adapter (records calls)
   - `inject_tick`, `seed_historify`, `fire_job`, `set_clock` helpers
   - `assert_strategy_state`, `assert_sandbox_position`, `assert_journal_entry`
2. Implement P0 tests (Flows 1, 3, 6, 10, 11, 13, 15, 16, 20, 23) — the ones where a failure would mean silent trading day impact

### Phase 2: Complete Layer A coverage (~1.5–2 weeks)

- All remaining Layer-A tests (Flows 2, 4, 5, 7, 8, 12, 14, 17, 18, 19, 21, 22, 24-A)
- Rewrite the 2 skipped tests in `test/e2e/test_critical_flows.py` (now retired for mode-only arch) for Flows 19 + 20

### Phase 3: Playwright Layer B for 5 flows (~1 week)

- Set up Playwright fixture in `test/e2e_playwright/conftest.py`
- Implement Flows 9 (scanner UI), 12-B (positions), 1-B (login), 24-B (dashboard badges)
- Add to CI as a separate optional job (`e2e-playwright`) — not blocking initially

### Phase 4: Synthetic day Layer C (~3–5 days)

- Build `test/e2e/test_synthetic_day.py` once Layer A harness is stable
- Wire as a CI job that runs on PR to `main` only (slower, ~60s)

---

## Phase 6 — Harness vs. Existing conftest Relationship

The proposed `BootHarness` is ADDITIVE to `test/conftest.py`. The global DB redirect in conftest stays as the structural guard; BootHarness builds on top:

- `conftest.py` ensures all DB env vars point to temp dir BEFORE any import
- `BootHarness.create()` calls `create_app(testing=True)` + `init_*` services using those already-redirected env vars
- `harness.fire_job(id)` calls the APScheduler job function directly, bypassing the real scheduler clock
- No fork/subprocess — everything runs in the pytest process for determinism

The synthetic day (Layer C) is the one test that DOES depend on all services being registered — it's the integration smoke-test for the harness itself.

---

## Appendix A — Empty / Scaffolded Test Files (Action Required)

These files have docstrings + helpers but 0 implemented test functions. They represent work that was planned but not yet built:

| File | Planned scope | Action |
|------|---------------|--------|
| `test/e2e/test_chartink_webhook_to_sandbox.py` | ✅ Actually HAS 4 tests (Scenarios 1–4) | No action |
| `test/e2e/test_critical_flows.py` | 11 tests — ALL SKIPPED (retired intent-DB arch) | Rewrite for mode-only arch |
| `test/e2e/test_fno_flows.py` | Has tests for FnO engine (see docstring) | Count actual implemented tests |
| `test/test_sector_follow_full_cycle.py` | Full entry→exit cycle | Implement (Flow 13 + 16) |
| `test/test_eod_three_layer_defense.py` | Three-layer EOD integration | Implement (Flow 15) |
| `test/test_ws_proxy_full_integration.py` | WS proxy with real ZMQ | Implement (Flow 21, Linux only) |
| `test/test_boot_broker_session.py` | Background process registration | Implement (Flow 3) |
| `test/test_csrf.py` | CSRF validation | Implement (Flow 1 sub-test) |
| `test/test_connection_pool.py` | ConnectionPool predicate | Implement (P1 from 2026-06-22 memory) |
| `test/test_connection_manager_predicate.py` | Connection manager | Implement |

---

## Appendix B — Top 5 Untested High-Impact Flows

These flows, if they silently regress, cause the biggest trading-day impact:

1. **Flow 3 — Background jobs not registered at boot.** If `init_sector_follow_service` fails silently, NO scheduled jobs fire all day. Currently zero tests verify the APScheduler job roster after boot.

2. **Flow 13 — Sector_follow full cycle.** The `test_sector_follow_full_cycle.py` file has 0 implemented tests despite being the most critical daily trading path. The 2026-06-15 incident (0 signals, no alert) would have been caught by a smoke_check→entry→evaluate_candidates integration test.

3. **Flow 23 — Mode persistence across restart.** A regression here would silently revert `sector_follow` to `scaffold` mode after any app restart (nightly Zerodha re-login restart). Currently no test.

4. **Flow 1 — Admin login.** The first seam in every trading day. `test/test_csrf.py` is empty; `test/test_boot_broker_session.py` is empty. A rogue import-time failure in `blueprints/auth.py` would show up as a 500 with no test to catch it.

5. **Flow 20 — Data freshness auto-pause side-effect.** The 2026-06-12 incident (all entries held for the day) was triggered by the data freshness gate writing a `strategy_runtime_override` pause. The auto-pause tests are in the SKIPPED `test_critical_flows.py`. No active test verifies that stale feed → auto-pause tomorrow.

---

## Appendix C — Coverage Count

| Metric | Count |
|--------|-------|
| Total flows enumerated | 24 |
| ❌ No test | 6 (Flows 1, 3, 9, 12, 23 + partial new) |
| ⚠️ Partial | 11 |
| ✅ Well covered | 7 |
| Layer-A tests proposed | 22 |
| Layer-B (Playwright) tests proposed | 4 |
| Layer-C (synthetic day) | 1 |
| **Total new tests proposed** | **27** |
| Empty scaffolded files to fill | 10 |
| Skipped tests to rewrite | 11 (all of `test_critical_flows.py`) |
| **Estimated effort** | **4–5 weeks** |

---

*This plan is read-only. No source or test code was modified. Execute via the umbrella issue once reviewed.*
