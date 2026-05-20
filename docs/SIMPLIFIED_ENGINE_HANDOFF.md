# Simplified Stock Engine Integration — Hand-off

Status: feature branch ready for verification + merge.
Branch: `feat/simplified-engine-integration`
Author of these commits: Claude (via Cowork session, 2026-05-17 to 2026-05-20)
This doc exists so a fresh Claude Code session (or any human) can pick up the
work without replaying the conversation that produced it.

---

## What was integrated

The source script `pythonTradingAutomator/src/simplified_stock_engine.py` is a
3,391-line standalone Zerodha-coupled algo engine. An earlier partial port
brought roughly 40% of it into openalgo as `services/simplified_stock_engine_core.py`
and `services/simplified_stock_engine_service.py`. This branch adds five
features that closed the gap, plus cleanup.

| # | Commit | What it does | LOC |
|---|--------|--------------|-----|
| 0 | `7dc2cbd5` | Pre-existing port + LF normalisation + .gitattributes | 1,393 |
| 1 | `cad03419` | `SIMPLIFIED_ENGINE_MODE` for order routing | +392 |
| 2 | `23dd1b6f` | Live-mode broker-position-aware EOD flatten | +495 |
| 3 | `170a909b` | Live-mode funds gate before opening trades | +419 |
| 4 | `64a03382` | EOD trading summary with charge breakdown | +520 |
| 5 | `3878ea86` | JSONL tick logging with batched async writer | +643 |
| 6 | `3bea2c2f` | Post-integration cleanup (DRY_RUN removed, frontend wired) | +185 |
| 7 | `4c670493` | `pythonpath = ["."]` for newer pytest | +4 |
| 8 | `9096ebc8` | Eager-import services submodules in test file | +12 |
| 9 | `f8e04ed4` | conftest.py to break restx_api / services import cycle | +32 |

### Step 1: routing modes (`cad03419`)

Replaces the boolean `SIMPLIFIED_ENGINE_DRY_RUN` short-circuit (which bypassed
`place_order` entirely) with a tri-state `SIMPLIFIED_ENGINE_MODE`:

- **`disabled`** — engine processes ticks and emits entry/exit signals but
  sends no orders anywhere. Engine state still advances locally. Useful for
  paper tracing.
- **`sandbox`** (default) — orders routed directly to
  `services.sandbox_service.sandbox_place_order`, bypassing the global
  `analyze_mode` toggle. Positions, trades, and funds tracked in `sandbox.db`
  (virtual ₹1Cr capital).
- **`live`** — orders go through `services.place_order_service.place_order`,
  which still honours the global `analyze_mode` flag.

`_dispatch_order` is the single routing point. Mode is validated in
`SimplifiedEngineConfig.__post_init__`; invalid env values fall back to
`sandbox` with a warning.

### Step 2: EOD broker-position reconciliation (`23dd1b6f`)

At `eod_exit_time` (default 15:20 IST), `_maybe_flatten_eod()` runs once.
In live mode it queries `services.positionbook_service.get_positionbook` for
every API key the engine has seen, then for each broker-reported non-zero
position on the configured exchange/product:

- If the engine already tracks the symbol: skip (engine's `check_eod_exits`
  handles it). Qty mismatches log a warning but aren't auto-reconciled.
- If the engine doesn't know about the symbol (drift): dispatch a market
  order in the opposite direction to flatten.
- If the engine tracks something the broker doesn't report: log warning,
  no order issued.

Sandbox and disabled modes skip this entirely (sandbox can't drift; disabled
sends no orders).

### Step 3: live-mode funds gate (`170a909b`)

In live mode only, `_check_live_funds(api_key)` is called before
`_dispatch_order` for every opening trade. Reads `availablecash` via
`services.funds_service.get_funds`; rejects the order if it's below
`config.effective_funds_floor` (default = `account_capital`, override via
`SIMPLIFIED_ENGINE_FUNDS_FLOOR`).

Failure modes:
- Fetch failure / exception → fail open (allow the order), leave cache empty
  so next entry re-fetches.
- Unparseable response → fail open.
- Insufficient cash → block, clear pending entry, log warning.

Per-API-key cache (`_funds_cache`) keyed by api_key with
`SIMPLIFIED_ENGINE_FUNDS_CACHE_SECONDS` TTL (default 30s) prevents hammering
the broker funds endpoint during entry bursts.

### Step 4: EOD trading summary (`64a03382`)

After `_maybe_flatten_eod` (and in all three modes — even disabled, since the
engine confirms trades locally there), `_maybe_log_eod_summary()` writes a
single multi-line log block with per-trade rows and totals.

New core types:
- `CompletedTrade` dataclass — round-trip record with `buy_value` /
  `sell_value` / `turnover` / `gross_pnl` properties. Populated in
  `engine.confirm_exit(symbol, exit_price, reason)`.
- `TradeCharges` dataclass — brokerage / STT / exchange / SEBI / GST / stamp.
- `compute_zerodha_intraday_charges(buy_value, sell_value)` — Zerodha NSE
  equity intraday formulas (ballpark for other brokers/products; docstring
  flags the caveat).

`engine.completed_trades` ledger clears on day rollover in
`_reset_trade_day_if_needed`.

### Step 5: tick logging (`3878ea86`)

New module `services/simplified_stock_engine_ticklog.py` (337 lines):
- Bounded `queue.Queue` with **drop-oldest** semantics on overrun (preserves
  recent data for live debugging).
- Daemon writer thread, lazy startup on first enqueue.
- Batched writes by count (default 200) or time (default 1.0s).
- Daily filename rotation: `ticks-YYYYMMDD-<pid>.jsonl[.gz]`.
- Optional gzip.
- Retention pruning on writer startup (default 14 days).
- Off by default — opt in with `SIMPLIFIED_ENGINE_TICK_LOG=true`.

Hooked into `on_quote()` after price/volume extraction; no-op when disabled.

---

## Env vars (all `SIMPLIFIED_ENGINE_*`)

| Name | Default | Purpose |
|---|---|---|
| `MODE` | `sandbox` | Routing mode: `disabled` / `sandbox` / `live` |
| `CAPITAL` | `20000` | Notional account capital |
| `LEVERAGE` | `5` | Account leverage for sizing |
| `MAX_RISK_PER_TRADE` | `500` | Per-trade risk cap (₹) |
| `MIN_RISK_PER_SHARE` | `1.0` | Minimum risk per share (₹) |
| `MAX_TRADES_PER_DAY` | `6` | Daily opening-trade cap |
| `EXCHANGE` | `NSE` | Single exchange the engine touches |
| `PRODUCT` | `MIS` | Order product (intraday by default) |
| `NO_NEW_ENTRIES_AFTER` | `15:10` | Cutoff for new entries |
| `EOD_EXIT_TIME` | `15:20` | Engine-side EOD flatten + summary trigger |
| `ATR_PERIOD` | `14` | Wilder ATR window |
| `ATR_SL_MULT` | `1.2` | SL = ATR × this |
| `ATR_ENTRY_MIN_MULT` | `0.5` | Min candle range / ATR for entry |
| `VOLUME_MULTIPLIER` | `2.5` | Required volume vs reference candle |
| `TRAIL_ATR_MULT` | `0.5` | Trailing distance floor in ATR units |
| `SL_CONFIRM_SECONDS` | `3.0` | SL confirmation debounce |
| `GLOBAL_PROFIT_LOCK` | `true` | Enable portfolio-wide profit lock |
| `BUY_ENABLED` / `SELL_ENABLED` | `true` | Per-direction kill switches |
| `FUNDS_FLOOR` | `=CAPITAL` | Live-mode funds gate floor |
| `FUNDS_CACHE_SECONDS` | `30` | Funds reading cache TTL |
| `TICK_LOG` | `false` | Enable JSONL tick log |
| `TICK_LOG_DIR` | `tick_logs` | Tick log directory |
| `TICK_LOG_QUEUE` | `10000` | Max queued ticks |
| `TICK_LOG_BATCH` | `200` | Flush after N ticks |
| `TICK_LOG_FLUSH_SECONDS` | `1.0` | Or flush after T seconds |
| `TICK_LOG_COMPRESS` | `false` | Gzip output |
| `TICK_LOG_RETENTION_DAYS` | `14` | Prune older files on startup |
| `SYMBOL_MAP` | `{}` | JSON dict of Chartink → OpenAlgo overrides |
| `HISTORY_SOURCE` | `api` | History service source |
| `HISTORY_LOOKBACK_DAYS` | `3` | Days of history to seed |
| `ORDER_POLL_ATTEMPTS` | `5` | Order-status polling attempts |
| `ORDER_POLL_INTERVAL` | `1.0` | Polling interval (seconds) |

`SIMPLIFIED_ENGINE_DRY_RUN` is no longer honoured (removed in `3bea2c2f`).

---

## Endpoints

- `POST /chartink/simplified-stock-engine/<webhook_id>` — Chartink webhook
  entry (CSRF-exempt via `app.py:280`).
- `GET /chartink/simplified-engine/api/status` — session-auth status JSON.
  Surface includes: `mode`, `engine_mode`, `eod_flatten_done`,
  `eod_summary_done`, `completed_trades_today`, `funds`, `tick_log`,
  `direction_enabled`, `positions`, `pending_entries`, `pending_exits`,
  `subscribed_symbols`.
- `POST /chartink/simplified-engine/api/toggle` — flip BUY or SELL kill
  switch.

---

## Frontend

`frontend/src/api/simplified-engine.ts` types and
`frontend/src/pages/SimplifiedEngine.tsx` page are updated to consume the
new status fields. New `EngineStateCard` shows funds, EOD progress badges,
and tick-log stats. `ModeBanner` drives the label off `engine_mode` and
surfaces an "ANALYZER OVERRIDE" warning when the global `analyze_mode` flag
would redirect a live engine into sandbox.

Production build artifacts (`frontend/dist/`) are gitignored. Build step is
required before serving.

---

## Tests

68 tests across two files:

- `test/test_simplified_stock_engine_core.py` — 9 tests on the broker-agnostic
  engine (pre-existing; not modified in this branch).
- `test/test_simplified_stock_engine_service.py` — 59 tests covering mode
  routing, EOD flatten, funds gate, EOD summary, tick log, frontend status
  block.

`conftest.py` at the project root pre-imports `restx_api` to break a circular
import between `services.place_order_service` and `services.options_multiorder_service`.
Without this, mock.patch on lazy-imported submodules fails with
`AttributeError: module 'services' has no attribute 'sandbox_service'`.

`pyproject.toml` declares `pythonpath = ["."]` so `from services.foo import bar`
works under newer pytest (Python 3.14+) without relying on legacy import-mode
behaviour.

---

## Known limitations / deferred items

- **Partial-fill drift not reconciled.** If the broker shows a different qty
  than the engine for a tracked symbol, the engine closes its known qty and
  logs a warning. The extra qty stays open. Acceptable for v1.
- **Engine-orphan positions don't trigger orders.** If the engine thinks it
  holds something the broker doesn't, we warn but don't issue compensating
  orders.
- **Charge formulas are Zerodha-specific.** Other brokers will see ballpark
  numbers; the docstring on `compute_zerodha_intraday_charges` is explicit.
- **`on_order_update` partial-fill / scale-in / reversal handling not ported.**
  Sandbox doesn't partial-fill; live uses synchronous order-status polling.
- **Source's `_cap_position_risk` not ported.** Post-fill SL recomputation
  from executed price already respects `max_risk_per_trade` via sizing.

---

## Pending work — Claude Code, please do this next

Run in order; stop and report if any step errors out non-obviously.

1. **Backend tests.**
   ```
   cd C:\workspace\ai-trade-agent\openalgo
   uv run pytest test/test_simplified_stock_engine_core.py test/test_simplified_stock_engine_service.py -v
   ```
   Expected: all green (68 tests). If anything fails, diagnose against
   `git log --oneline -10 feat/simplified-engine-integration`, propose a
   minimal fix, apply it, commit on the same branch, re-run.

2. **Frontend build.**
   ```
   cd C:\workspace\ai-trade-agent\openalgo\frontend
   npm install
   npm run build
   ```
   Expected: clean build, no TypeScript errors. The new
   `SimplifiedEngineFundsSummary` / `SimplifiedEngineTickLogStats` types and
   `EngineStateCard` should compile.

3. **Lint pass.**
   ```
   cd C:\workspace\ai-trade-agent\openalgo
   uv run ruff check services/simplified_stock_engine_core.py services/simplified_stock_engine_service.py services/simplified_stock_engine_ticklog.py test/test_simplified_stock_engine_service.py
   ```
   Fix any warnings inline.

4. **Smoke-boot the app once in sandbox mode** to confirm imports / routes
   register cleanly:
   ```
   cd C:\workspace\ai-trade-agent\openalgo
   set SIMPLIFIED_ENGINE_MODE=sandbox
   uv run app.py
   ```
   Verify `http://127.0.0.1:5000/chartink/simplified-engine/api/status`
   returns the new status JSON, then Ctrl+C. Don't leave it running.

5. **Summarise** what you ran, what you fixed (if anything), and what's
   ready to merge. Don't push or merge — Dheeraj will do that.

**Constraints**
- Stay on `feat/simplified-engine-integration`. No new branches.
- Don't touch `frontend/dist/` (gitignored, CI builds it).
- Don't push to remotes.
- If you need to change something outside `services/`, `test/`, `frontend/src/`,
  `docs/`, or `.sample.env`, surface it as a question first.

---

## Useful pointers

- CLAUDE.md at the project root has the openalgo conventions (uv, eventlet,
  6-database isolation, broker plugin pattern).
- `services/simplified_stock_engine_core.py` is the broker-agnostic engine.
- `services/simplified_stock_engine_service.py` is the openalgo integration
  shim (webhook ingest, order dispatch, EOD logic, funds gate).
- `services/simplified_stock_engine_ticklog.py` is the tick log writer.
- `blueprints/chartink.py:947+` has the three webhook / status / toggle routes.
- Source script lives at
  `C:\workspace\ai-trade-agent\pythonTradingAutomator\src\simplified_stock_engine.py`
  if you need to cross-reference.
