# Production Trading Flows — Audit & Test Plan

**Issue:** #94
**Date:** 2026-06-22
**Status:** PLAN ONLY — no source or test code changes. Execution awaits operator approval.
**Branch:** `docs/prod-trading-flows-test-plan`

---

## Testing strategy

### What CI gates exist today

| Gate | Job | Blocking? | What it catches |
|------|-----|-----------|----------------|
| Unit + integration (120+ tests) | `ci-unit-tests` | YES (required check) | Individual service logic, DB writes, mode resolution, engine mechanics |
| Docker boot + E2E | `cd-docker-e2e` | YES (required check) | App boots cleanly, `/auth/check-setup` reachable, basic e2e smoke |
| Quality (ruff, bandit, semgrep) | `quality` | NO (informational only) | Style, security, silent-drop anti-patterns |
| Silent drops only | `silent-drops` | Required on `main` | P0/P1 anti-patterns (basket, multiorder, journal-failure) |

**Where this falls short for trading-critical flows:**

1. **No full-cycle order path test in CI.** `test_place_order_dispatch.py` mocks both sandbox and broker at the service boundary — it never exercises the real sandbox DB write, quote-fetch, or margin validation in `sandbox/`. The real sandbox path is covered only in `test/sandbox/` which is NOT run in CI (`test/e2e` is run in Docker but `test/sandbox/` is excluded from the CI test command).

2. **No live-broker path in CI at all.** The mock broker (`test/fixtures/mock_broker/`) exists (from PR #71 / Tier-3 CI) but it covers OAuth login only — there is no mock broker test that exercises `place_order_api`, `cancel_order_api`, or `modify_order_api` over the mock HTTP adapter.

3. **Strategy engine tests are hermetic but shallow at the broker seam.** The 1,615-line `test_simplified_stock_engine_service.py` and 1,090-line `test_sector_follow_service.py` are excellent mechanically but inject the order placer as a lambda — they never verify the `_dispatch_order → sandbox_place_order / place_order` fork actually fires in the right mode.

4. **EOD flows have layered tests but gaps at the seam.** `test_eod_watchdog_service.py` (862 lines) is comprehensive; `test_engine_eod_reconciliation_backfill.py` covers the backfill CLI; but the three-layer EOD defense (tick-driven → watchdog → reconciliation) has no integration test that exercises all three in sequence on the same open position.

5. **WS proxy health and reconnect are unit-tested but not exercised with a running proxy.** `test_broker_session_auto_reconnect.py` uses `WebSocketProxy.__new__` to bypass port binding — no test starts a real proxy and verifies that a ZMQ `CACHE_INVALIDATE` event propagates to an actual subscription resumption.

6. **Watchdog/tripwire services are new and lightly tested.** `scanner_smoke_check_service.py`, `scanner_dry_tripwire_service.py`, and `thread_watchdog_service.py` (all post-2026-06-15) have `test_scanner_smoke_check.py`, `test_scanner_dry_tripwire.py`, and `test_thread_watchdog.py` respectively, but none are currently run in the CI parallelized suite — need to confirm they appear in the `pytest test/` glob.

### Mock broker fixture and leverage plan

`test/fixtures/mock_broker/app.py` is a FastAPI server with:
- `/_test/mock_auth` — shim to insert a valid auth row directly (bypasses the real Zerodha OAuth flow)
- `BROKER_API_URL` env override — when set, all broker adapter imports resolve to the mock server

Current coverage: OAuth login + master contract download (PR #71). **Not yet covered:** order placement, cancel, modify, quote fetch.

Leverage plan for new tests:
- Add `/_test/place_order`, `/_test/cancel_order`, `/_test/modify_order` endpoints to the mock broker that record calls and return configurable responses (success / broker-side error / network timeout).
- Add `/_test/get_quotes` endpoint that returns deterministic LTP values for the sandbox `ExecutionEngine`.
- Use `BROKER_API_URL=http://127.0.0.1:<mock_port>` in `conftest.py` or per-test monkeypatching to route adapter calls to the mock.
- The Docker E2E stage already boots a real app container — mock broker can be added to `docker-compose.test.yml` as a second service.

### Sandbox as a second-class production target

`db/sandbox.db` is a real SQLite database with live schema, real orders/positions/trades rows, and a running `ExecutionEngine` polling loop. Tests that exercise the sandbox path should:
- Use `conftest.py`'s DB redirect to a `tempfile.mkdtemp()` sandbox DB (already in place for `SANDBOX_DATABASE_URL`).
- Call `sandbox_place_order` directly to avoid needing a live quote feed.
- Verify the actual `SandboxOrders`, `SandboxPositions`, `SandboxTrades` row changes, not just the response dict.

### Recommended phasing

| Priority | Label | Scope | Effort |
|----------|-------|-------|--------|
| **P0** | Must-have before next live-trading session | WS proxy reconnect integration, full-cycle sandbox order placement, EOD three-layer integration | 1–2 days |
| **P1** | Should-have within the sprint | Mock-broker order APIs, mode-resolution boundary tests, kill-switch integration, data-freshness auto-pause E2E | 2–4 days |
| **P2** | Stretch / good-hygiene | Thread watchdog with real health DB, master contract refresh smoke, Telegram alert path, kill-switch 3% cap | 4–8 days |

---

## Flow cards

---

### Flow 1 — External `/api/v1/placeorder` → live broker

**Entry points:**
`POST /api/v1/placeorder` → `restx_api/place_order.py` → `services/place_order_service.place_order`

**Code path:**
1. `restx_api/place_order.py` — Flask-RESTX request parsing + API key validation
2. `place_order_service.place_order(order_data, api_key)` — check Action Center routing (`order_router_service.should_route_to_pending`); if semi-auto → queue and return
3. `validate_order_data` — schema validation (required fields, exchange/action/price_type/product_type enum checks)
4. `get_auth_token_broker(api_key)` — auth_db TTL cache lookup → Fernet-decrypt broker token
5. `place_order_with_auth` → `resolve_effective_mode()` — checks `strategy_mode['__global__']` → legacy `daily_intent` → env → SANDBOX default
6. If `analyze_mode` ON → `sandbox_place_order` (sandbox.db)
7. If mode LIVE → `import_broker_module(broker)` → `broker.zerodha.api.order_api.place_order_api(order_data, auth_token)` → httpx POST to Zerodha
8. On success: `bus.publish(OrderPlacedEvent)` → SocketIO `order_update` emit to UI
9. Return `{"status": "success", "orderid": "…"}`

**Side effects:**
- `db/openalgo.db` → `orderbook` row written by broker adapter (on live success)
- `db/logs.db` → traffic log
- SocketIO `order_update` event → React dashboard live update

**Success signal:** HTTP 200 + `{"status": "success", "orderid": "<broker_order_id>"}`

**Failure modes:**
- `API_KEY_PEPPER` import-time crash if env missing (F1 — catastrophic, fast fail at boot)
- `get_auth_token_broker` returns None → 403 "Invalid openalgo apikey" (morning 401 class — memory)
- Broker token expired (daily ~3 AM IST) → broker returns 401 inside `place_order_api` → logged but NOT automatically retried
- `analyze_mode` ON silently routes live-declared orders to sandbox (B2 class — documented mode-only fix; resolve_effective_mode now defaults SANDBOX)
- Action Center semi-auto mode queues order silently — caller sees 200 but order not placed
- Validation passes but broker rejects (wrong lot size, exchange holiday, position limit) — response propagates error from broker JSON
- `import_broker_module` failure if broker string mismatched → 404

**Current test coverage:**
`test/test_place_order_dispatch.py` — covers mode routing (live/sandbox/analyze) with mocked `sandbox_place_order` and mocked `place_order_api`. **THIN:** no test with a real mock HTTP broker, no test that verifies the `orderbook` DB row, no test for Action Center queue path.

**Test plan:**
- **P0:** Integration test with mock broker HTTP endpoint: POST /api/v1/placeorder → verify mock broker received the call + correct headers. Assert the response orderid matches what the mock returned.
- **P1:** Test Action Center semi-auto routing: set `order_mode='semi_auto'`, assert `should_route_to_pending` fires and order lands in `pending_orders` DB row.
- **P1:** Test API key expiry / invalid key returns exactly 403 with no DB write.
- **P2:** Test broker HTTP timeout (mock broker sleeps 30s) → 500 + `OrderFailedEvent` published.

---

### Flow 2 — External `/api/v1/placeorder` → sandbox route

**Entry points:** Same as Flow 1 but `resolve_effective_mode()` returns SANDBOX (default).

**Code path:**
1–5: identical to Flow 1
6. `sandbox_place_order(order_data, api_key, original_data)` (`services/sandbox_service.py`) → validates margin via `FundManager.check_and_reserve_margin` → writes `SandboxOrders` row with `order_status='open'`
7. `sandbox/execution_engine.py` polling loop (every 5s) picks up the order → fetches LTP via `get_multiquotes` → executes at market price → writes `SandboxTrades`, updates `SandboxPositions`, updates `SandboxOrders.order_status='complete'`
8. `bus.publish(OrderPlacedEvent(mode='analyze'))` → SocketIO `analyzer_update`

**Side effects:** `db/sandbox.db` — SandboxOrders, SandboxPositions, SandboxTrades rows; funds deducted

**Success signal:** HTTP 200 + `{"status": "success", "orderid": "<sandbox_order_id>"}`. Position appears in `/api/v1/positions`.

**Failure modes:**
- Insufficient margin → `sandbox_place_order` returns `{"status": "error", "message": "Insufficient margin"}` (400)
- `get_multiquotes` fails (no broker session, WS proxy down) → `ExecutionEngine` falls back to individual quote → also fails → order stays `open` indefinitely
- `analyze_mode` ON but API key missing → `emit_analyzer_error` path fires → 400 with no DB write
- MIS orders after 15:15 IST rejected by `squareoff_manager` check in `order_manager`

**Current test coverage:**
`test/sandbox/` has execution engine and order flow tests. CI does NOT run `test/sandbox/` (excluded from `pytest test/ -n auto`). **CRITICAL GAP: the sandbox order path is entirely untested in CI.**

**Test plan:**
- **P0:** Add `test/sandbox/` to the CI test run. Verify it doesn't pollute the live `sandbox.db` (conftest redirect already handles `SANDBOX_DATABASE_URL`).
- **P0:** Integration test: call `sandbox_place_order` with a known symbol + mocked `get_multiquotes` returning a deterministic LTP → assert `SandboxTrades` row exists with correct fill price.
- **P1:** Test margin insufficient branch → assert 400 + no `SandboxOrders` row.
- **P1:** Test MIS after 15:15 IST rejection (time-mocked).

---

### Flow 3 — Order cancel and modify (live + sandbox)

**Entry points:**
`POST /api/v1/cancelorder` → `restx_api/cancel_order.py` → `services/cancel_order_service.cancel_order`
`POST /api/v1/modifyorder` → `restx_api/modify_order.py` → `services/modify_order_service.modify_order`

**Code path (cancel):**
1. API key validation + `resolve_effective_mode()`
2. If SANDBOX → `sandbox/order_manager.cancel_order(orderid)` → sets `order_status='cancelled'`
3. If LIVE → `broker.zerodha.api.order_api.cancel_order_api(orderid, auth_token)` → Zerodha DELETE

**Code path (modify):**
1–2 same; LIVE → `modify_order_api` → Zerodha PUT with new price/qty

**Note from `order_router_service.py`:** cancel, modify, cancel-all, close-position are in `IMMEDIATE_EXECUTION_OPERATIONS` and are NEVER queued by Action Center — they always execute immediately regardless of semi-auto mode. This is load-bearing (a stale queued cancel against a triggered GTT is unsafe).

**Side effects:** `SandboxOrders.order_status` update (sandbox); broker orderbook row update (live); SocketIO `order_update`

**Failure modes:**
- Cancel on non-existent `orderid` → broker 400 / sandbox DB row not found
- Modify a filled order → broker rejects (order already executed)
- Action Center accidentally queues cancel (regression risk if `IMMEDIATE_EXECUTION_OPERATIONS` set shrinks) — was intentionally excluded

**Current test coverage:**
`test_cancel_order_dispatch.py`, `test_modify_order_dispatch.py` — basic dispatch routing with mocked service calls. **THIN:** no test that `IMMEDIATE_EXECUTION_OPERATIONS` exemption holds; no modify-after-fill regression test.

**Test plan:**
- **P1:** Parameterized test asserting each operation in `IMMEDIATE_EXECUTION_OPERATIONS` bypasses the Action Center queue even when `order_mode='semi_auto'`.
- **P1:** Sandbox cancel: assert `SandboxOrders` row transitions to `cancelled`.
- **P2:** Modify-after-fill error path: mock broker returning 400 → assert 400 propagated, `OrderFailedEvent` published.

---

### Flow 4 — Chartink webhook → simplified engine → sandbox order

**Entry points:**
`POST /chartink/simplified-stock-engine/<webhook_id>` → `blueprints/chartink.simplified_stock_engine_webhook`

**Code path:**
1. Webhook validation: `get_strategy_by_webhook_id(webhook_id)` → 404 if unknown; `strategy.is_active` check; time-window check (start_time < now < squareoff_time)
2. `scan_cycle_service.start_cycle("chartink")` → `scan_cycle` DB row with `cycle_heartbeat` audit trail
3. `engine_service.process_chartink_webhook(data)` → `parse_chartink_symbols(payload)` → for each symbol: `engine.process_chartink_webhook` → writes to in-memory watch dict
4. Engine waits for next 5m candle close via `on_quote(symbol, quote)` → `_handle_candle(symbol, candle)` at bar close → if breakout + ATR SL + volume check → `_schedule_entry(signal)` → `_place_entry_order` → `_entry_held_by_override()` check (runtime override gate) → `_dispatch_order`
5. `_dispatch_order` (sandbox): `sandbox_service.sandbox_place_order` directly (bypasses `place_order_service`)
6. `_dispatch_order` (live): `place_order_service.place_order(payload, api_key=api_key)` → resolves mode via `resolve_effective_mode`
7. On order placed: `_journal_record_entry` → `trade_journal` row; `_notify_trade_opened` → Telegram
8. `scan_cycle_service.complete_cycle(post_status='ok')`

**Side effects:** `db/openalgo.db` → `scan_cycle`, `cycle_heartbeat`, `trade_journal`; `db/sandbox.db` → SandboxOrders; Telegram entry alert

**Success signal:** `{"status": "success", "message": "Symbols armed: RELIANCE, INFY"}`. Orders appear in sandbox positions after the next 5m candle close.

**Failure modes:**
- Webhook fires after `squareoff_time` (15:20 for intraday) → 400 "Cannot arm engine after square off time" — correct guard
- No matching strategy for `webhook_id` → 404
- Preflight check (`_is_test_source_entry` on Windows path normalization bug) — fixed in commit `b698194f` but regression risk remains
- Scan cycle DB write fails → wrapped as fail-safe (engine still receives trigger)
- LLM veto returns `skip` → entry blocked (VETO_LAYER_MODE=active in sandbox; shadow in live) → journal row NOT written (gap: no rejected-entry audit row)
- `_entry_held_by_override()` returns True (active pause/kill_switch) → entry silently blocked; only a debug log (no Telegram alert for this path)
- Engine mode `disabled` → on_quote silently returns without arming

**Current test coverage:**
`test/e2e/test_fno_flows.py` (hermetic, mocked broker + veto), `test_simplified_stock_engine_service.py` (1,615 lines, core mechanics). Webhook HTTP route itself tested in `test_chartink_webhook_audit.py`, `test_chartink_webhook_sell_routing.py`. **GAP:** No test that a webhook → engine arm → 5m candle → `_dispatch_order` chain actually writes a `SandboxOrders` row in the temp sandbox DB.

**Test plan:**
- **P0:** Integration test: POST webhook → inject a fake 5m candle via `engine.on_quote` → verify `SandboxOrders` row created in temp sandbox DB. No live quote feed needed — inject tick directly.
- **P1:** Test `_entry_held_by_override` blocks entry when a `pause` override is active; assert Telegram NOT called and `trade_journal` row NOT written.
- **P1:** Test veto `skip` verdict: mock `signal_review_service.review_signal` returning `skip` → assert no `SandboxOrders` row + no `trade_journal` entry row.
- **P2:** Test webhook after `squareoff_time` → 400 with `scan_cycle.post_status='skipped'`.

---

### Flow 5 — Sector_follow scheduled entry (15:20 IST) → ranking → sandbox

**Entry points:**
APScheduler job `sector_follow_entry` (15:20 IST, mon-fri) → `SectorFollowService.run_entry()`

**Code path:**
1. `_entry_held_by_override()` → check `strategy_runtime_override` table (pause/kill_switch)
2. `data_freshness_enabled()` → `production_data_health_checker(...)` → validates 8 sector indices + 30 stocks freshness in `historify.duckdb` via `data_freshness_service`
3. `_compute_metrics(universe, as_of, ...)` → per-symbol: `production_intraday_provider` reads scanner aggregator today bars; `production_history_reader` reads historify lookback bars
4. `passes_gates(metrics, config)` → C1: `sector_ret > gate_sector_ret` (1.0%); W2: `vol_ratio > 1.0`; E4: market-wide filter
5. `select_entries(candidates, config)` → sort by vol_ratio descending, cap at K=5
6. For each selected: `compute_qty(max_position_inr, price)` → `production_order_placer(mode, order)` → `place_order(order, api_key=api_key)` (sandbox: sandbox_service; live: broker)
7. Write `sector_follow_trades` journal row (openalgo.db)
8. Kill-switch check: if day P&L < -3% capital → set kill switch, block future entries

**Side effects:**
- `db/openalgo.db` → `sector_follow_trades`, `strategy_runtime_override` (on kill switch trip)
- `db/sandbox.db` → SandboxOrders (sandbox mode)
- Telegram entry alert
- `strategies/sector_follow_cap5_vol/eod_reports/<date>.md` (15:30 job)

**Failure modes:**
- Freshness gate blocks (stale historify) → 0 entries + Telegram alert → `strategy_runtime_override` pause written for tomorrow (auto-pause mechanism, 2026-06-12 B6)
- Scanner aggregator has no today bars (<50% coverage) → `intraday_source='none'` → Telegram WARNING/CRITICAL
- Sector index NOT in scanner universe → falls back to historify for `sector_ret` (WARNING logged)
- Kill switch tripped earlier → entries blocked; exits still run
- `passes_gates` too strict → 0 signals (genuine quiet day vs data gap — 2026-06-15 incident)
- `resolve_mode` DB error falls through to `env` → safe (default sandbox)
- 15:18 smoke check fires and writes pause override → entries held (smoke check auto-expires 15:30)

**Current test coverage:**
`test_sector_follow_service.py` (1,090 lines) — unit tests for `passes_gates`, `select_entries`, metrics providers, freshness gate, smoke check. `test/e2e/test_critical_flows.py` exercises the entry→exit cycle hermetically. **GAP:** No test that verifies the `production_intraday_provider` → scanner aggregator data path (exercises real `ScannerService.get_today_ohlcv`); no integration test that asserts a `SandboxOrders` row is written with correct symbol/qty/product.

**Test plan:**
- **P0:** Integration test with a real (temp) sandbox DB: mock the scanner aggregator to return today bars for 3 symbols, assert `sector_follow_trades` row and `SandboxOrders` row both written.
- **P1:** Test auto-pause: mock freshness check returning stale → assert `strategy_runtime_override` row written with `expires_at=tomorrow 15:30` + Telegram alert called.
- **P1:** Test kill-switch trip: inject `daily_pnl = -3.1%` → next entry call → assert `_entry_held_by_override()` returns True.
- **P2:** Test sector index historify fallback (symbol not in scanner universe) → WARNING logged, entry still proceeds with historify-sourced sector_ret.

---

### Flow 6 — Futures_follow scheduled entry (15:20 IST) → futures lot sizing → sandbox

**Entry points:**
APScheduler job `futures_follow_entry` (15:20 IST, mon-fri) → `FuturesFollowService.run_entry()` (indirectly via `place_entry`)

**Code path:**
1. `_entry_held_by_override()` → `strategy_runtime_override` check (reuses same infra)
2. `evaluate_signals(as_of)` → calls `production_signal_evaluator` which delegates to **`SectorFollowService` evaluator** (shares config/gates — not reimplemented)
3. `production_contract_resolver(signal_symbol, as_of)` → `fno_search_symbols_db` for the NIFTY near-month contract; skips contracts expiring within 1 day (monthly expiry safety)
4. `compute_lots_to_buy(signals, capital, lot_margin, cap_margin_pct=0.5)` → greedy vol-ratio order; hard cap 50% of capital as overnight SPAN margin
5. For each lot: `production_order_placer(mode, order)` → NRML, NFO, MARKET → sandbox or broker
6. `_default_trade_recorder` → `futures_follow_trades` row
7. EOD watchdog job at 15:14 also fires for the futures strategy (registered separately)

**Side effects:** `db/openalgo.db` → `futures_follow_trades`; `db/sandbox.db` → SandboxOrders (NRML lot)

**Failure modes:**
- `production_contract_resolver` finds no non-expiring NIFTY contract (last-Tuesday expiry edge case) → signals silently skipped (need a logged WARNING here — currently unclear if present)
- Sector_follow evaluator returns 0 signals → 0 lots bought (correct behavior; Telegram EOD shows 0 trades)
- Capital cap 50% → signals beyond cap skipped (no alert — operator must read EOD summary)
- Futures require SPAN margin; if sandbox capital insufficient → sandbox `order_manager` rejects → `futures_follow_trades` row written as failed (check if error path exists)
- Mode defaults to `sandbox` — `FUTURES_FOLLOW_MODE` env var → but the default is already `sandbox` so this is fine

**Current test coverage:**
`test_futures_follow_service.py` (780 lines) — covers evaluator, lot sizing, contract resolver, 50% cap, kill switch. `test_futures_follow_blueprint.py`, `test_futures_follow_db.py`. **GAP:** No test that exercises the `production_contract_resolver` against the actual master contract DB (uses a temp DB with seeded rows); no integration test that asserts a `SandboxOrders` row written with `NRML`/`NFO`.

**Test plan:**
- **P1:** Integration test: seed `master_contract` with a valid NIFTY near-month row in temp DB → call `evaluate_signals` → `place_entry` → assert `SandboxOrders` row with `product='NRML'`, `exchange='NFO'`.
- **P1:** Test expiry edge: seed NIFTY contract expiring today → assert it's skipped, next-month picked.
- **P2:** Test capital cap: 3 signals but cap 50% → only first 2 lots placed; 3rd skipped with INFO log.

---

### Flow 7 — EOD square-off: three-layer defense

**Entry points (three independent layers):**
1. **Tick-driven** (primary): `SimplifiedStockEngineService.on_quote(symbol, quote)` → `_maybe_flatten_eod()` at each tick after `eod_exit_time`
2. **APScheduler watchdog** (backstop): `eod_watchdog_service` BackgroundScheduler job at 15:14 IST → `flatten_strategy_positions(strategy_name)` → reads open `trade_journal` rows → `place_order(opposite-side MARKET, api_key)`
3. **EOD reconciliation** (cleanup): `_maybe_reconcile_eod_journal(today)` called before `_notify_eod_summary` → reads sandbox.db positions read-only → stamps missing exit rows (`exit_reason='sandbox_eod_squareoff'`)

**Code path (watchdog layer, most critical):**
1. `eod_watchdog_service._run_watchdog(strategy_name)` (15:14 IST)
2. `flatten_strategy_positions` → open `trade_journal` rows where `exited_at IS NULL`
3. For each open row: `place_order(opposite-side MARKET, api_key=api_key)` → sandbox/live
4. On success: journal row stamped with `exit_reason='watchdog'`
5. On failure: `NotificationService.publish_eod_watchdog_failure` → Telegram alert

**Side effects:**
- `db/openalgo.db` → `trade_journal` exit rows stamped
- `db/sandbox.db` → SandboxOrders (exit order written)
- Telegram EOD summary (15:30 IST) picks up reconciled rows

**Critical timing constraint:** watchdog cap at 15:14 IS LOAD-BEARING. Sandbox rejects MIS orders at/after 15:15. Moving the cap to ≥15:15 re-creates the 2026-06-10 OIL/HINDZINC/TATAELXSI orphan class.

**Failure modes:**
- Tick stream dies before `eod_exit_time` → layer 1 never fires → layer 2 (watchdog at 15:14) is the actual line of defense
- Watchdog misfires by >5 min (APScheduler busy) → `misfire_grace_time=300` → catches up within 5 min; can still be after 15:15 in worst case
- Watchdog fires but `place_order` returns error (broker down, token expired) → Telegram alert but position NOT closed → falls to sandbox auto-square-off → reconciliation picks it up at 15:30
- EOD reconciliation reads sandbox.db as stale (positions not yet closed by sandbox) → `exited_at` not stamped → Telegram EOD shows wrong P&L (2026-06-10 bug; fixed by reconcile-before-summarize ordering)
- Futures strategy EOD watchdog registered separately — verify `futures_follow_eod_watchdog` job also fires at 15:14 and NOT at strategy's `eod_exit_time` (which is 15:25)

**Current test coverage:**
`test_eod_watchdog_service.py` (862 lines) — extensive unit tests for watchdog scheduling, flatten call, timing cap. `test/e2e/test_engine_eod_reconciliation.py` covers reconciliation E2E hermetically. `test_engine_journal_integration.py`. **GAP:** No integration test that exercises all three layers in sequence on the same open position in a temp sandbox DB; no test that simulates a 15:14 watchdog fire while the tick stream is dead.

**Test plan:**
- **P0:** Three-layer integration test: open a `trade_journal` row, kill the (mocked) tick stream, fire the watchdog directly, verify `SandboxOrders` row + `trade_journal.exit_reason='watchdog'`, then run reconciliation and verify Telegram EOD shows correct P&L.
- **P1:** Timing cap regression test: assert watchdog is registered at exactly 15:14 (not strategy's `eod_exit_time` if > 15:14). Parameterized over strategies.
- **P1:** Watchdog failure → Telegram alert: mock `place_order` to raise → assert `publish_eod_watchdog_failure` called.
- **P2:** Reconciliation idempotence: run reconcile twice → assert no duplicate `trade_journal` rows.

---

### Flow 8 — WS market data live path: broker → ZMQ → proxy → client

**Entry points:**
`websocket_proxy/server.py` (subprocess, port 8765) — started by `app.py` as a separate process via `multiprocessing`.

**Code path:**
1. Broker WebSocket adapter (`broker/zerodha/streaming/ZerodhaWebSocketAdapter`) connects to `wss://ws.kite.trade` → subscribes to symbol tokens
2. On tick received: adapter publishes to ZMQ PUB socket (`port 5555`, `tcp://127.0.0.1:5555`)
3. `WebSocketProxy.zmq_listener()` coroutine polls ZMQ SUB → deserializes tick
4. Looks up `subscription_index[(symbol, exchange, mode)]` → set of client_ids (O(1) via defaultdict)
5. 50ms throttle per `(symbol, exchange, mode)` — skips message if last was < 50ms ago
6. Sends filtered tick JSON to each subscribed client WebSocket (port 8765)
7. In-process `ScannerService.on_quote` also receives ticks (directly, not via WS) for bar aggregation

**Connection pooling:** `ConnectionPool` in `websocket_proxy/connection_manager.py` manages up to 3 connections × 1000 symbols per WS = 3000 symbols max. `ConnectionPool.initialize()` predicate must return `True`/`False` — the 2026-06-22 crash (#76) was `is False` vs `not` predicate (now fixed).

**Side effects:** Ticks routed to client WebSockets (port 8765); `ScannerService` bar aggregators updated in-process; `latency.db` updated by health monitor

**Failure modes:**
- **Port 8765 conflict** → proxy fails to start → 0 ticks → cascading 0-signal day (2026-06-22 memory)
- **Thread leak** → `thread_count` climbs past 100/200 → Windows `select()` FD_SETSIZE ~512 kills the process (2026-06-22 #76; now caught by thread watchdog)
- **ZMQ port 5555 not set** → `ZMQ_PORT` env var unset → `None` in connect string → proxy crashes silently at startup
- **ConnectionPool.initialize predicate** returns truthy non-True → pool thinks init failed → `is False` vs `not` fix (PR #89)
- **Broker adapter disconnect** → `_last_known_subscriptions` snapshot retained → reconnect restores them
- 50ms throttle → fast-moving stocks may miss ticks; bar aggregator sees sparse feed (not a bug but a known trade-off)

**Current test coverage:**
`test_broker_session_auto_reconnect.py` (207 lines, hermetic), `test_connection_pool.py` (new, PR #89), `test_connection_manager_predicate.py`, `test_ws_proxy_health.py` (new, PR #85). **GAP:** No test starts a real proxy process and verifies ZMQ → WebSocket data flow end-to-end with a real client subscriber. Thread leak scenario has no test.

**Test plan:**
- **P0:** Integration test: start a real proxy + mock ZMQ publisher → connect a WebSocket client → publish a tick → verify client receives it within 100ms. (Can use `pytest-asyncio` + `websockets` client.)
- **P1:** Test 50ms throttle: publish 3 ticks for the same symbol within 100ms → client receives ≤2 (first + one after throttle window).
- **P1:** Test ConnectionPool auth-error detection: mock adapter `initialize()` returning `{"status": "error"}` → verify pool returns False and logs the failure (regression guard for the `is False` / `not` bug).
- **P2:** Thread count stress test: run ZMQ listener for 1000 messages → verify `threading.active_count()` does not grow monotonically (thread leak detection).

---

### Flow 9 — WS proxy reconnect after broker session refresh + historical replay

**Entry points:**
`database.auth_db.upsert_auth()` → publishes ZMQ `CACHE_INVALIDATE_ALL_{user_id}` message
`WebSocketProxy._handle_cache_invalidation()` → `_reconnect_broker_adapter(user_id)`
`utils.auth_utils.notify_broker_session_refreshed()` → in-process `BrokerSessionRefreshedEvent` → `WSRecoveryService.on_broker_session_refreshed()`

**Code path (proxy reconnect):**
1. `upsert_auth(name, token, broker)` → writes `db/openalgo.db` auth row → ZMQ PUB `CACHE_INVALIDATE_ALL_{user_id}`
2. Proxy subprocess: `zmq_listener` receives it → `_handle_cache_invalidation` → unconditional call to `_reconnect_broker_adapter`
3. `_reconnect_broker_adapter`: snapshot `_last_known_subscriptions[user_id]` → `adapter.disconnect()` → `adapter.initialize(broker, user_id, fresh_token)` → `adapter.connect()` → re-subscribe all symbols
4. If `initialize()` fails: log exception, retain `_last_known_subscriptions`, drop dead adapter

**Code path (historical replay, in-process):**
5. `notify_broker_session_refreshed(username, broker)` → SocketIO `broker_session_refreshed` (UI) + `bus.publish(BrokerSessionRefreshedEvent)`
6. `WSRecoveryService.on_broker_session_refreshed(event)` → for each tracked symbol: `history_service.get_history(symbol, interval='1m', lookback=WS_RECOVERY_LOOKBACK_MIN=20)` (rate-limited 3 req/sec) → `MultiIntervalAggregator.replay_bars(symbol, bars)` (dedup by timestamp)
7. Telegram structured alert: symbols re-synced / elapsed / gap / bars replayed; WARN if >20% fail

**Side effects:** `db/openalgo.db` auth row updated; SocketIO UI event; bar aggregator state updated with replayed bars

**Failure modes:**
- `initialize()` returns truthy non-True (the `is False` bug class, now fixed) → proxy thinks reconnect failed
- Historical API returns 0 bars (Zerodha 5-15min lag on current-day data) → aggregator partially warmed, bars catch up on next refresh
- `history_service.get_history` rate-limited → 250 symbols take ~85s; UI unresponsive to tick updates during replay
- `replay_bars` receives overlapping bars (reconnect during live session) → dedup by timestamp is the guard
- `on_broker_session_refreshed` callback raises → never bubbles back to login (event bus subscriber isolation)

**Current test coverage:**
`test_broker_session_auto_reconnect.py` (7 hermetic unit tests via `__new__`), `test_ws_recovery_service.py` (197 lines). **GAP:** No test starts a real proxy subprocess and verifies the ZMQ → reconnect → re-subscribe chain; no test exercises the `WSRecoveryService` with a real (temp) `historify.duckdb`.

**Test plan:**
- **P0:** Integration test for reconnect: create a real proxy via `__new__` (or actual subprocess) → inject a `CACHE_INVALIDATE` ZMQ message → verify `initialize()` called on the adapter → verify `_last_known_subscriptions` restored.
- **P1:** Test replay with mocked `history_service`: provide 3 bars per symbol → verify `MultiIntervalAggregator.replay_bars` called for each tracked symbol with those bars.
- **P1:** Test dedup: replay the same bar twice → verify bar aggregator sees it only once.
- **P2:** Test partial failure (one symbol's `get_history` raises) → verify other symbols still replayed + Telegram warning if >20% fail.

---

### Flow 10 — Broker session refresh at ~3 AM IST → downstream notification

**Entry points:**
Manual Zerodha login at `GET /zerodha` + `POST` callback → `blueprints/auth.py` → `utils.auth_utils.handle_auth_success()`

**Code path:**
1. Zerodha redirects to `/zerodha` callback with `request_token`
2. `blueprints/auth.py` exchanges token → `broker.zerodha.api.auth_api.authenticate(request_token)` → gets `access_token`, `feed_token`
3. `upsert_auth(name, access_token, 'zerodha', feed_token)` → DB write + ZMQ `CACHE_INVALIDATE` → triggers WS proxy reconnect (Flow 9)
4. Master contract download check (`should_download_master_contract`) → if needed: `async_master_contract_download(broker)` on background thread
5. `notify_broker_session_refreshed(username, broker)` → SocketIO `broker_session_refreshed` + in-process event bus
6. `master_contract_cache_hook.load_symbols_to_cache()` → `socketio.emit("cache_loaded")` → UI notified
7. Sector_follow + futures_follow boot-time backfill convergence checks are also triggered on broker session presence (separate daemon thread)

**Side effects:** `db/openalgo.db` auth row + master contract tables updated; WS proxy reconnects; bar aggregator replays; React UI receives `cache_loaded` event

**Failure modes:**
- Zerodha OAuth callback with invalid `request_token` → auth fails → login page redirect; no downstream effects
- Master contract download fails → `socketio.emit("cache_loaded")` with `status='error'` → symbol lookups fail until next download
- `FERNET_SALT` not set → auth token storage uses legacy static salt (WARNING logged) → not a functional failure but a security posture issue
- WS proxy not running (port 8765 dead) → ZMQ `CACHE_INVALIDATE` published but no consumer → proxy stays disconnected; login still succeeds

**Current test coverage:**
`test_boot_broker_session.py` — covers session detection logic. `test_cache_performance.py`, `test_cache_compatibility.py` — cover symbol cache. **GAP:** No integration test exercises the full auth callback → `upsert_auth` → ZMQ publish → WS reconnect chain.

**Test plan:**
- **P1:** Integration test: mock Zerodha token exchange → call `handle_auth_success` → verify `upsert_auth` DB row written with expected token + verify `BrokerSessionRefreshedEvent` published on bus.
- **P1:** Test `master_contract_cache_hook.load_symbols_to_cache()` with a seeded temp DB → verify cache populated + `cache_loaded` SocketIO emit called.
- **P2:** Test `FERNET_SALT` not set → WARNING logged but auth still succeeds (fallback path).

---

### Flow 11 — Data freshness checks → auto-pause / alert

**Entry points:**
APScheduler job `sector_follow_data_health` (16:30 IST) → `data_freshness_service.check_strategy_data_ready`
Pre-entry gate in `SectorFollowService.run_entry()` (before every 15:20 IST entry)
Boot-time + periodic convergence check (`sector_follow_backfill_scheduler.check_and_refresh_if_stale`)
Scanner smoke check job (09:18 IST) → `scanner_smoke_check_service`

**Code path (16:30 post-close check):**
1. `check_strategy_data_ready('sector_follow', today, max_staleness_business_days=1)` → reads `MAX(timestamp)` per symbol from `historify.duckdb` → compares to today's expected close (business-day aware)
2. If stale: `database.data_health_db.insert_check(...)` row → `strategy_runtime_override` written (`pause`, expires tomorrow 15:30 IST, `set_by='sector_follow'`) → Telegram alert
3. Pre-entry gate (15:20 IST): if stale → abort entries + Telegram alert (does NOT write override; that's the 16:30 job)
4. Boot convergence: `check_and_refresh_if_stale(today)` → DuckDB lock-safe read (uses `connect_historify_readonly` fallback) → triggers incremental backfill for stale symbols

**Side effects:** `db/openalgo.db` → `data_health_check` row, `strategy_runtime_override` row (on stale data after close); `historify.duckdb` → new `market_data` 1m bars (if backfill triggered)

**Failure modes:**
- DuckDB "different configuration" error (in-process, same process already holds read-write connection) → `connect_historify_readonly` fallback should handle (memory: 2026-06-15 fix); regression risk
- `MAX_STALENESS_BUSINESS_DAYS=1` reads "Friday present=fresh" even when today's intraday bars are missing (2026-06-15 incident: the threshold checked prior-day but not today's intraday bars)
- Backfill fails (expired Zerodha token) → all symbols fail → Telegram anomaly alert; but pre-entry gate still reads the stale check and blocks entries
- `strategy_runtime_override` expires at 15:30 IST → self-clears; but if data is STILL stale on the next day, the 16:30 job fires again (correct behavior)

**Current test coverage:**
`test_data_freshness_service.py` — unit tests for `check_strategy_data_ready`. `test_sector_follow_backfill_convergence.py`, `test_scanner_universe_backfill.py`. **GAP:** No test covers the 16:30 job's auto-pause write path end-to-end (freshness fail → `strategy_runtime_override` row → next-day entry blocked). The DuckDB "different configuration" fallback has no dedicated test.

**Test plan:**
- **P1:** Integration test: seed temp DuckDB with stale data → run `check_strategy_data_ready` → assert `data_health_check` row written with `overall_ok=False` + `strategy_runtime_override` row with tomorrow's `expires_at`.
- **P1:** Test that the runtime override from the 16:30 job propagates to the 15:20 entry gate: insert the override row → call `run_entry` → assert 0 orders placed + Telegram alert.
- **P1:** Test DuckDB "different configuration" fallback: monkeypatch `connect_historify_readonly` to fail on first try → verify fallback fires and read succeeds.
- **P2:** Test business-day awareness: staleness on Friday 17:00 IST → Monday check should not flag Friday's close as stale (1-business-day window).

---

### Flow 12 — Watchdog flows

**Four independent watchdogs:**

#### 12a — Thread-count watchdog (`services/thread_watchdog_service.py`)

**Entry:** Background daemon thread polling every 30s; reads `health_metrics.thread_count` from `db/health.db`

**Thresholds:** WARN at 100, CRIT at 200. Dedup: one transition alert per severity change + reminder every 15 min if sustained.

**Failure modes:** The 2026-06-22 incident (#76) reached 543 threads (asyncio thread leak) before Windows `select()` FD_SETSIZE ~512 killed the WS proxy. The watchdog should have fired at 100 → WARN and 200 → CRIT giving ~2h of warning before the crash.

**Current coverage:** `test_thread_watchdog.py` — unit tests for threshold/dedup logic. **GAP:** No test reads from a real `health.db` with seeded thread counts.

**Test plan:**
- **P1:** Integration test: seed `health_metrics` row in temp health.db with count=150 → run watchdog → assert `HealthAlert` row written + Telegram alert fired. Then count=250 → assert CRIT alert.
- **P1:** Dedup test: fire WARN at 150 twice within 15min → assert Telegram called once (not twice).

#### 12b — Scanner smoke check (`services/scanner_smoke_check_service.py`)

**Entry:** APScheduler job at 09:18 IST → asserts tick aggregator coverage, stored-1m freshness, stored-D freshness, broker session live

**Failure modes:** 2026-06-19 outage: scanner-universe 1m stale since 06-15, OpenAlgo down pre-12:31 IST → smoke check had no morning assertion → full-day 0 signals undetected.

**Current coverage:** `test_scanner_smoke_check.py` — unit tests. **GAP:** No test exercises the full check with a seeded temp health.db + broker session mock.

**Test plan:**
- **P1:** Integration test: mock all 4 checks (aggregator coverage, stored-1m ok, stored-D ok, broker session live) → assert smoke check returns `ok`. Then fail broker session check → assert `data_health_check` row written with `overall_ok=False` + Telegram CRIT alert.

#### 12c — Scanner dry tripwire (`services/scanner_dry_tripwire_service.py`)

**Entry:** APScheduler job every 5 min during market hours (09:30–15:30 IST) → measures gap since latest `scan_results` row

**Logic:** If gap > 30 min AND Chartink also dry → WARN; if gap > 30 min AND Chartink has rows → CRIT (pipeline broken, not market quiet)

**Current coverage:** `test_scanner_dry_tripwire.py` — unit tests. **GAP:** No integration test with real `scan_results` + `scan_cycle` DB rows.

**Test plan:**
- **P1:** Integration test: seed temp openalgo.db with a `scan_results` row 45 min old and a Chartink `scan_cycle` row → assert CRIT alert. Then no Chartink row → assert WARN.

#### 12d — EOD watchdog (covered in Flow 7 above)

---

### Flow 13 — Telegram alert delivery

**Entry points:**
`services/notification_service.NotificationService.notify(event_type, message, ...)` → routes to `telegram_bot_service` (primary) or `telegram_inbound_service.send_message_to_all()` (Phase 6 fallback)

**Code path:**
1. `notify(event_type, message)` → checks `NOTIFY_<EVENT>` env var (per-event toggle, default `true`)
2. If `telegram_bot_service` active (legacy bot running) → `bot_service.send_message(chat_id, text)`
3. If legacy bot inactive (Phase 6 inbound poller) → `telegram_inbound_service.send_message_to_all(text)` (same `chat_id` allowlist)
4. On failure: `logger.exception` (never raises, never retries)
5. For chart attachments (e.g. P&L plots): `_render_plotly_png` runs in a real OS thread (eventlet bypass pattern) → `bot.send_photo`

**Failure modes:**
- Single token shared between legacy outbound bot and Phase 6 inbound poller → Telegram `getUpdates Conflict` error if both run simultaneously (2026-06-11 memory)
- `chat_id` not in allowlist → message silently dropped (never an error, just ignored)
- Telegram network timeout → `logger.exception` + silent failure; no retry; no dead-letter queue
- `_render_plotly_png` on eventlet green thread → blocks if not in a real thread (existing architecture uses real thread correctly)
- `NOTIFY_<EVENT>=false` silently disables entire event class

**Current test coverage:**
`test_telegram_alerts.py`, `test_telegram_bot_service.py`, `test_telegram_config.py`, `test_telegram_startup.py`, `test_telegram_inbound_deprecation.py`. Mostly unit tests with mocked bot API. **GAP:** No test exercises the `notify()` → Phase 6 fallback path end-to-end; no test verifies the per-event `NOTIFY_<EVENT>` toggle gates correctly.

**Test plan:**
- **P2:** Test `notify()` with legacy bot inactive → fallback path fires `send_message_to_all`. Assert fallback called (not primary).
- **P2:** Test `NOTIFY_SCANNER_COMPARISON=false` → `notify('scanner_comparison', ...)` → assert Telegram call NOT made.

---

### Flow 14 — Master contract refresh + symbol cache

**Entry points:**
`blueprints/master_contract_status.py` `POST /api/master-contract/download` (manual trigger)
Auto-download at login (`handle_auth_success`, gated by `should_download_master_contract`)
Cache load: `database/master_contract_cache_hook.load_symbols_to_cache()` → symbol lookup cache warmed

**Code path:**
1. `should_download_master_contract(broker)` → checks `master_contract_status` table for today's download
2. `async_master_contract_download(broker)` → background thread → `broker.zerodha.database.master_contract_db.master_contract_download(auth_token)` → writes `SymToken` rows
3. `load_symbols_to_cache()` → reads `SymToken` → warms in-memory LRU cache
4. `socketio.emit("cache_loaded", {"status": "success"})` → React UI notified
5. Subsequent order symbol resolution: `get_symbol_info(symbol, exchange)` → cache hit

**Failure modes:**
- Master contract download fails (broker API rate limit, 429) → `master_contract_status` row written with `status='error'` → UI shows error; symbol lookups fail (orders rejected with "symbol not found")
- Cache not loaded → symbol lookup DB fallback (slower but functional)
- `makedirs('')` crash (empty `DATABASE_URL` dir) — fixed in self-hosted CI smoke PR #5 but regression risk
- Download happens at login but symbol-lookup order arrives before download completes (race) → symbol not found → order fails

**Current test coverage:**
`test_cache_performance.py`, `test_cache_compatibility.py`. **GAP:** No test exercises the `should_download_master_contract` → download → `load_symbols_to_cache` → `cache_loaded` SocketIO chain; no race-condition test.

**Test plan:**
- **P2:** Integration test: seed temp openalgo.db with no today download → call `should_download_master_contract` → assert `True`; run download with mocked broker → assert `SymToken` rows + `cache_loaded` SocketIO emitted.
- **P2:** Test symbol-lookup race: start download thread + immediately query cache → assert fallback DB read happens without error.

---

### Flow 15 — Engine kill switch (3% daily capital cap)

**Entry points:**
`SimplifiedStockEngineService._handle_candle` / `on_quote` → P&L update → `_check_kill_switch()`
`SectorFollowService.update_daily_pnl()` → kill switch check
`FuturesFollowService.update_daily_pnl()` → kill switch check

**Code path:**
1. After each exit or P&L update: `day_pnl / capital < -daily_loss_kill_pct (3%)`
2. If tripped: write `strategy_runtime_override` row (type=`kill_switch`, expires EOD, `set_by=engine`)
3. `_entry_held_by_override()` returns `True` for all subsequent entry calls that day
4. Exit jobs (`sector_follow_exit` at 15:25, `futures_follow_exit` at 15:25, engine's own tick-driven exit) are NEVER blocked by the kill switch
5. `09:00 IST daily_reset` job clears the override for the next day

**Failure modes:**
- Kill switch tripped → `strategy_runtime_override` row written → DB write fails → kill switch silently not enforced (no fallback in-memory state for failure case)
- Override never expires (system clock wrong, IST timezone handling bug) → kill switch permanent until next manual clear
- Sector_follow: kill switch does not prevent T+1 exits from the prior day (correct); but a cascading loss day can trip it before entries fire, causing 0 entries (intended behavior)
- `daily_reset` at 09:00 removes override but if OpenAlgo restarts mid-day, in-memory state is reset — new entries could fire even with a tripped kill switch (only the DB override persists, and `_entry_held_by_override()` reads the DB, so a restart after a trip should still be blocked — needs verification)

**Current test coverage:**
`test_strategy_runtime_override.py` covers the DB layer. Engine kill-switch unit tests in `test_simplified_stock_engine_service.py` and `test_sector_follow_service.py`. **GAP:** No integration test verifies that after an OpenAlgo restart, the `strategy_runtime_override` DB row still blocks entries (verifying the DB-first design holds under restart).

**Test plan:**
- **P0:** Restart-resilience test: trip kill switch (write `strategy_runtime_override` row to temp DB) → create a fresh engine/service instance → call entry → assert 0 orders placed (verifies DB-first design, not in-memory state).
- **P1:** Test daily reset: write a `kill_switch` override → run `daily_reset` (09:00 job) → verify override row removed + next entry succeeds.
- **P1:** Test kill switch expiry logic: set `expires_at` to 5 seconds in the future → verify `_entry_held_by_override()` returns True now, then False after 5s.

---

## Summary matrix

| Flow | Current coverage verdict | Highest-priority gap | P0 tests proposed |
|------|--------------------------|---------------------|-------------------|
| 1. External order → live | Thin (mocked broker) | Mock HTTP broker for place_order_api | 1 |
| 2. External order → sandbox | **CRITICAL: not in CI** | Add test/sandbox/ to CI run | 1 |
| 3. Cancel/modify | Thin | IMMEDIATE_EXECUTION_OPERATIONS regression guard | 0 |
| 4. Chartink webhook → engine | Good mechanics, gap at DB seam | Webhook → candle → SandboxOrders integration | 1 |
| 5. Sector_follow entry | Good, gap at aggregator→DB seam | Aggregator → SandboxOrders integration | 1 |
| 6. Futures_follow entry | Good, gap at contract resolver→DB | NIFTY contract resolver → SandboxOrders | 0 |
| 7. EOD three-layer defense | Well-covered individually, gap in sequence | Three-layer integration test on shared open position | 1 |
| 8. WS live data path | Good unit tests, no real ZMQ/WS test | Real proxy + ZMQ publisher → client receives tick | 1 |
| 9. WS reconnect + replay | Hermetic unit tests, no real subprocess test | Reconnect integration + replay with temp DuckDB | 1 |
| 10. Broker session refresh | Good unit tests, gap at full chain | `handle_auth_success` → ZMQ publish → event bus | 0 |
| 11. Data freshness → auto-pause | Good unit tests, gap at end-to-end pause chain | Stale DuckDB → override row → next-day entry blocked | 0 |
| 12. Watchdogs (thread/smoke/tripwire) | Good unit tests, gap with real DB | Thread watchdog + seeded health.db integration | 0 |
| 13. Telegram delivery | Good unit tests, Phase 6 fallback gap | `notify()` → fallback path | 0 |
| 14. Master contract refresh | Thin, no full-chain test | Download → cache load → `cache_loaded` event | 0 |
| 15. Kill switch | Good unit, gap at restart resilience | Restart → DB override still blocks entries | 1 |

**Total P0 tests proposed: 8**
**Total flows enumerated: 15 (+4 watchdog sub-flows = 18 distinct paths)**

---

## Appendix — Referenced incidents and fixes

| Date | Incident | Affected flow | Fix/Status |
|------|----------|---------------|------------|
| 2026-06-22 | Thread count 20→543, WS proxy killed by Windows FD_SETSIZE | Flow 8 | Thread watchdog (PR #90) |
| 2026-06-22 | `ConnectionPool.initialize()` `is False` predicate bug | Flow 8/9 | PR #89 (fixed) |
| 2026-06-19 | Scanner 0 signals all day (stale 1m + OpenAlgo down pre-12:31) | Flow 12b | Scanner smoke check (new) |
| 2026-06-15 | 0 sector_follow signals (no today intraday bars in historify) | Flow 5/11 | Aggregator as today source + loud failure |
| 2026-06-12 | Tick starvation → in-house scanner 7 hits/day | Flow 8/9 | WS recovery service |
| 2026-06-11 | E2E test `test_fno_flows.py` wrote to live `trade_journal` | Flow 2/4 | conftest global DB redirect |
| 2026-06-10 | EOD watchdog fired at 15:20 (after sandbox 15:15 MIS close) → 3 orphans | Flow 7 | Watchdog cap at 15:14 |
| 2026-06-10 | Telegram EOD showed +₹352 vs real +₹8,327 | Flow 7 | EOD reconciliation service |
| 2026-06-10 | TATAELXSI SELL veto reviewed as BUY | Flow 4 | `direction` param added to veto |
