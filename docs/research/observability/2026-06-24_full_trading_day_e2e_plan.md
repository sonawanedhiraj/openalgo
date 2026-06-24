# Comprehensive Trading-Day Reliability Plan

**Date:** 2026-06-24
**Status:** PLAN ONLY — no source or test code changes
**Branch:** `docs/full-trading-day-e2e-plan`

## Scope

Three reliability problems combined into one sequenced roadmap:

| Phase | Problem | Why it's a blocker |
|-------|---------|-------------------|
| **Phase 0** | DuckDB write-lock contention (`fix/124-duckdb-write-lock`) | Foundation: without serialized writes the scanner backfill crashes, which triggers Phase 1 |
| **Phase 1** | Scanner-dark race — 141ms boot window poisons history cache | Root cause of 5-day scan blackout (last scan_results: 2026-06-19) |
| **Phase 2** | BootHarness + P0 integration tests | Structural: Phase 1 fix must be regression-tested before landing |
| **Phase 3** | Full Layer-A integration coverage (24 trading-day flows) | Prevents any recurrence of the Phase 0/1 failure class |
| **Phase 4** | Playwright (Layer B) + synthetic day (Layer C) | UI consistency + full-day smoke test |

---

## Issue Transitions

### Issue #124 — "Fix DuckDB write lock contention" (CLOSED)
PR #125 merged a first-pass fix to dev. Branch `fix/124-duckdb-write-lock` carries a
**more comprehensive follow-on** (14 write functions serialized, RLock) that is NOT yet
on dev — it should be Phase 0's PR. The issue is already closed; the follow-on should
open a new small issue: **"DuckDB write serialization — apply `@_synchronized_write` to
all 14 write functions + fix duplicate-decorator bug"**.

### Issue #94 — "Audit & test plan: end-to-end production trading flows" (OPEN, P1)
The batch-2 work from #94 (Layer A P1 flows, mock-broker order APIs) never landed.
This document supersedes it. Recommend: comment on #94 pointing to the new combined
umbrella, then close #94 as "superseded".

### Issue #127 — prior umbrella from this session (OPEN)
Created 30 min ago as the E2E-only umbrella. Superseded by the new combined umbrella
below. Close #127 with a "superseded by #<new>" comment immediately after creating the
new umbrella.

---

## Phase 0 — Land `fix/124-duckdb-write-lock` (DuckDB write serialization)

### What the branch does

`fix/124-duckdb-write-lock` adds a module-level `threading.RLock` to
`database/historify_db.py` and wraps **14 write functions** with a
`@_synchronized_write` decorator:

| Function | Why it must be serialized |
|----------|--------------------------|
| `upsert_market_data` | Called by all backfill workers simultaneously (ThreadPoolExecutor, 5 concurrent) |
| `create_download_job` | Called by HTTP handlers while workers run |
| `update_job_status` / `update_job_item_status` / `update_job_progress` / `delete_download_job` | Worker-level status writes |
| `add_to_watchlist` / `bulk_add_to_watchlist` / `clear_watchlist` | UI-triggered writes competing with worker writes |
| `delete_market_data` / `bulk_delete_market_data` | Admin-triggered bulk deletes |
| `upsert_symbol_metadata` | Contract load competing with backfill |
| `create_schedule` / `update_schedule` / `delete_schedule` / `create_schedule_execution` / `update_schedule_execution` / `increment_schedule_run_counts` | Scheduler self-writes during download |

The RLock (`threading.RLock`) is reentrant, so a write function that internally calls
another write function (nested write) acquires the lock twice safely without deadlocking.

### Known bug — duplicate `@_synchronized_write` on every function

The second commit on the branch (`a7e3adc03 fix: use RLock (reentrant lock)`) applied
`@_synchronized_write` a SECOND time to each function instead of replacing the Lock
with RLock in the existing decorator. Every write function now has:

```python
@_synchronized_write   # outer — added by 2nd commit
@_synchronized_write   # inner — added by 1st commit
def upsert_market_data(...):
    ...
```

Because it's an RLock, this works correctly at runtime (same thread acquires twice,
releases twice). But it's dead code and confusing. **Must be fixed before merging.**
Fix: squash both commits OR remove the duplicate decorators in a 3rd commit.

### Why everything else builds on this

Without serialized writes, concurrent download jobs and Flask handlers hit Windows
OS-level DuckDB exclusive-file-lock errors. Under that pressure:
- Backfill jobs fail and surface the sentinel-poisoning race (Phase 1) with empty tables
- `connect_historify_readonly()` exception-broadening (issue #126) hides the write
  errors as transient lock warnings rather than exposing the structural cause

Merging Phase 0 reduces noise and makes Phase 1 the only remaining failure mode.

### Tests needed for Phase 0

Existing: `test/test_historify_create_download_job.py` (4 tests) covers job lifecycle.
New: **one concurrency smoke test** — two threads call `upsert_market_data` simultaneously
and assert no exception is raised (confirms the lock actually serializes). File:
`test/test_historify_write_lock.py`.

### Delivery

- Fix the duplicate-decorator bug (one commit on the branch or squash)
- PR from `fix/124-duckdb-write-lock` → `dev`
- All subsequent Phases chain their implementation branches off `fix/124-duckdb-write-lock`

---

## Phase 1 — Scanner-dark race condition (5-day blackout fix)

### Root Cause Analysis

**Symptom:** in-house scanner has produced 0 `scan_results` since 2026-06-19 (5 days
dark) despite the WS feed running and Chartink posting signals.

**Race timeline at boot:**

```
T+0ms    app.py boots → init_scanner_backfill_scheduler(app)
T+50ms   daemon thread starts → waits for broker session
T+80ms   broker session found → starts async D download for ~200 symbols
           via historify_service.create_and_start_job (non-blocking, returns immediately)
           writes are queued in historify's ThreadPoolExecutor
T+141ms  scanner_history_provider.run_boot_warmup() fires
           → get_provider().refresh()
           → for each sym: _fetch(sym, "D", ...) → historify_db.get_ohlcv()
           → DuckDB write lock held by worker; read returns empty / 0 rows
           → refresh() stores: new_daily[sym] = pd.DataFrame()   ← EMPTY SENTINEL
T+141ms  cache swap: self._daily = new_daily  (all symbols now → empty DF)
```

**Why lazy-load is permanently bypassed:** `scanner_history_provider.py:80-92`

```python
def _get(self, symbol, interval, cache, lookback_bars):
    sym = symbol.upper()
    with self._lock:
        frame = cache.get(sym)
        if frame is not None:           # ← True for pd.DataFrame() (empty)
            return frame if not frame.empty else None   # ← returns None HERE
                                                        # execution NEVER reaches line 86
    # ← lazy-load below is unreachable when sentinel is present
    logger.info(f"ScannerHistoryProvider lazy-loading ...")
    frame = self._fetch(sym, interval, lookback_bars)
    with self._lock:
        cache[sym] = frame if frame is not None else pd.DataFrame()
    return frame if frame is not None and not frame.empty else None
```

The `return` on line 83 is inside the `with self._lock:` block. When `frame` is a
`pd.DataFrame()` (empty), `frame is not None` is `True`, so the function returns `None`
immediately. The lazy-load at line 86 is **structurally unreachable** for sentineled
symbols. From the moment `refresh()` races with the download, every subsequent call to
`get_daily(sym)` returns `None` for that symbol for the entire trading session, until
the 16:00 IST `refresh()` job runs (if it runs at all — it's not always wired).

**Consequence chain:**
1. D-bar gates in `fno_intraday_buy_chartink.py` receive `None` daily data
2. Rules return `False` for all symbols
3. `_evaluate_definitions` emits 0 hits
4. `scan_results` table gets no rows
5. The sector_follow + simplified engine see no Chartink signals to mirror
6. Scanner comparison EOD: Chartink side has signals, in-house side has 0 → Jaccard=0
   but this is not an alerting threshold breach

**Why it wasn't caught:** `scan_results` can legitimately be 0 on a quiet day. The
scanner comparison EOD would reveal the divergence, but the comparison job was checking
`source='inhouse'` counts — if those are 0 while Chartink is non-zero, the Jaccard
score would be 0. But there's no "Jaccard<0.1 → CRITICAL" alert threshold.

### Three Candidate Fixes

**Fix A — Block warmup until backfill completes (serialize at boot)**

```python
# In run_boot_warmup():
def run_boot_warmup() -> dict | None:
    from services.scanner_backfill_scheduler import wait_for_boot_backfill
    wait_for_boot_backfill(timeout_sec=120)   # blocks until D download done
    return get_provider().refresh()
```

Pro: eliminates the race entirely. Con: delays boot warmup by up to 120s; `wait_for_boot_backfill` must be added; hard to test.

**Fix B — Invalidation hook from backfill completion**

```python
# In scanner_universe_backfill.check_and_refresh_if_stale() after each symbol download:
from services.scanner_history_provider import get_provider
get_provider().invalidate(symbol)   # remove symbol from cache

# In ScannerHistoryProvider:
def invalidate(self, symbol: str) -> None:
    sym = symbol.upper()
    with self._lock:
        self._daily.pop(sym, None)
        self._weekly.pop(sym, None)
```

Pro: cache stays live; new data lands within the next `_get()` call; testable.
Con: coupling between scheduler and provider; requires the provider singleton to be
initialized before backfill starts (it is, via `init_scanner_backfill_scheduler`).

**Fix C — Don't return `None` for empty sentinels; fall through to lazy-load (recommended)**

Change `_get()` to only bypass lazy-load when the frame has real rows:

```python
def _get(self, symbol, interval, cache, lookback_bars):
    sym = symbol.upper()
    with self._lock:
        frame = cache.get(sym)
        if frame is not None and not frame.empty:
            return frame                  # fast path: cached + non-empty
        # frame is None (never fetched) OR frame.empty (stale sentinel)
        # → fall through to lazy-load

    # Lazy-load (runs even for sentineled symbols)
    logger.info(f"ScannerHistoryProvider lazy-loading {interval} for {sym}")
    frame = self._fetch(sym, interval, lookback_bars)
    with self._lock:
        if frame is not None and not frame.empty:
            cache[sym] = frame        # only cache real data
        # don't store empty → next call retries the fetch
    return frame if (frame is not None and not frame.empty) else None
```

Pro: minimal change, surgical, self-healing (lazy-load retries on each call until data
lands), testable. Con: lazy-load runs per-symbol-per-tick until data arrives (acceptable;
DuckDB reads are fast and the check itself is cheap).

**Recommendation: Fix C as the immediate fix, Fix B as a follow-on hardening.**

Fix C unblocks the scanner immediately without any new dependencies. Fix B is a
structural improvement (the invalidation hook is the right abstraction for the
scheduler→provider coupling) and should be added in the same PR as a separate commit.

Fix A is rejected: blocking boot on a 2-minute download creates a new class of failure
(slow/failed broker session on restart day → boot hangs for 2 minutes before trading).

### Additional hardening: alert when Jaccard < 0.2

The scanner comparison EOD job should alert when `jaccard < 0.2 AND chartink_count > 5`
— a genuine divergence between the two sides. Currently this threshold is unmonitored.
Add to `scanner_comparison_eod_service.compute_comparison()`:

```python
if chartink_count > 5 and jaccard < 0.2:
    notify_service.notify("scanner_comparison_dark", {
        "jaccard": jaccard, "chartink": chartink_count, "inhouse": inhouse_count
    })
```

This would have caught the 2026-06-19 blackout on the same day it started.

### Five regression tests for Phase 1

**Test 1 — `_get` falls through for empty sentinel**
```
File: test/test_scanner_history_provider.py (new — or extend test_scanner_history_warmup.py)
Setup: ScannerHistoryProvider with symbol A; manually set _daily["A"] = pd.DataFrame() (sentinel)
Acts:  get_daily("A") — with a monkeypatched _fetch that returns a real 3-row df
Assert: returns the 3-row df (lazy-load ran); sentinel is replaced in cache
```

**Test 2 — race simulation: refresh with empty DuckDB → lazy-load still works**
```
File: same
Setup: provider.refresh() on a DB with 0 rows for symbol A (simulates race)
Acts:  write data to DuckDB; call get_daily("A")
Assert: returns the written data (lazy-load ran, not permanently blocked by sentinel)
```

**Test 3 — invalidation hook clears stale sentinel**
```
File: same
Setup: sentinel in cache; call provider.invalidate("A")
Acts:  get_daily("A") with real data in DuckDB
Assert: returns data; sentinel gone from cache
```

**Test 4 — Jaccard < 0.2 fires alert**
```
File: test/test_scanner_comparison_eod_service.py (exists, 3 tests — add 1)
Setup: seed scan_cycle rows with 10 chartink signals, 0 inhouse rows
Acts:  run_comparison_for_date(today)
Assert: notify called once with event_type="scanner_comparison_dark"; jaccard=0.0
```

**Test 5 — warmup then backfill: data visible after download completes**
```
File: test/test_scanner_history_warmup.py (exists, 6 tests — add 1)
Setup: run_boot_warmup() with empty DuckDB → sentinels cached
       then write real data to DuckDB
       then call get_daily(sym)
Assert: returns real data (Fix C ensures lazy-load ran despite sentinel)
```

### Delivery

- New issue: "Scanner-dark race: `ScannerHistoryProvider._get()` returns None for empty
  sentinels, permanently bypassing lazy-load"
- Branch: `fix/<N>-scanner-history-sentinel` off `fix/124-duckdb-write-lock`
- Commits: (1) Fix C in `_get()`, (2) Fix B invalidation hook, (3) Jaccard alert
- All 5 regression tests in the same PR

---

## Phase 2 — BootHarness + P0 Integration Tests

### Why BootHarness before the rest of the test plan

The Phase 1 fix above needs regression tests that exercise the full boot sequence
under controlled timing. Writing those tests with bare `monkeypatch` + per-function
stubs is fragile. The `BootHarness` gives them a stable foundation and pays off on
every subsequent Phase 3/4 test.

### BootHarness design (`test/harness.py`)

```python
# Proposed API — docs only, not implemented here
class BootHarness:
    """Boots a minimal Flask app with all strategy services wired, using in-memory DBs."""

    @classmethod
    def create(cls, *, broker_mode="mock", strategy_modes=None) -> "BootHarness":
        # 1. Relies on conftest.py DB redirect (already in place)
        # 2. Calls create_app(testing=True)
        # 3. Injects a mock broker adapter (no network calls)
        # 4. Creates APScheduler in manual-trigger mode (no background threads)
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

    def set_clock(self, hour: int, minute: int = 0) -> None:
        """Inject IST time for all strategy services that take a now provider."""

    def mock_broker_login(self, *, user="admin", broker="zerodha") -> None:
        """Insert a valid auth row directly, bypassing OAuth."""

    def assert_strategy_state(self, strategy: str, *, mode: str, override_active: bool = False) -> None:
        """Assert strategy_mode + runtime_override table match expected state."""

    def assert_sandbox_position(self, symbol: str, *, qty_gt: int = 0) -> None:
        """Assert sandbox.db has an open position for symbol."""

    def assert_journal_entry(self, symbol: str, *, exit_reason: str | None = None) -> None:
        """Assert trade_journal has a row for symbol with matching exit_reason."""

    def assert_no_errors(self) -> None:
        """Assert log/errors.jsonl has 0 entries since harness creation."""
```

**Key design decisions:**

1. **APScheduler in manual mode.** `fire_job("sector_follow_entry")` runs the job function
   synchronously. No waiting for wall-clock time. The full 15:18→15:20→15:25→15:30→16:30
   sequence runs in milliseconds.

2. **No ZMQ for bar ingestion in Layer A.** `inject_tick` writes directly into the bar
   aggregator's in-process dict, bypassing the ZMQ PUB/SUB hop. Deterministic, sub-second.

3. **Real DB layer.** `conftest.py` already redirects all DB env vars. All SQLAlchemy +
   DuckDB writes go to real (temp) SQLite/DuckDB, making assertions against actual DB state.

4. **Deterministic `now()`.** `set_clock(15, 20)` injects an IST datetime across all
   strategy services that accept a `now` provider.

5. **Additive to `test/conftest.py`.** Global DB redirect stays as the structural guard.
   BootHarness builds on top.

### Three P0 Layer-A tests (required before Phase 3)

**P0-T1 — All APScheduler jobs registered at boot (Flow 3)**
```
File: test/test_boot_broker_session.py (exists, 0 tests — fill it)
Setup: BootHarness.create()
Assert: scheduler.get_jobs() contains all expected job IDs with correct IST triggers
        sector_follow_smoke_check(15:18), sector_follow_entry(15:20), sector_follow_exit(15:25),
        sector_follow_eod_summary(15:30), sector_follow_data_health(16:30),
        futures_follow_entry(15:20), futures_follow_eod_watchdog(15:14),
        scanner_comparison_eod(15:45)
```

**P0-T2 — Strategy mode persists across restart (Flow 23)**
```
File: test/test_strategy_mode.py (exists, 11 tests — add 1)
Setup: write strategy_mode(strategy='sector_follow_cap5_vol', mode='sandbox')
Acts:  tear down + recreate Flask app in same temp DB dir
Assert: get_mode('sector_follow_cap5_vol') returns {'mode': 'sandbox'}
```

**P0-T3 — Sector_follow full cycle: smoke check + entry + exit (Flows 13+16)**
```
File: test/test_sector_follow_full_cycle.py (exists, 0 tests — fill it)
Setup: BootHarness; seed historify (20d lookback D bars); inject today's aggregator bars;
       strategy_mode='sandbox'; set_clock(15, 18)
Acts:  fire_job("sector_follow_smoke_check") — must pass
       set_clock(15, 20); fire_job("sector_follow_entry")
       set_clock(15, 25); fire_job("sector_follow_exit")
Assert: smoke check: no pause override written
        entry: sector_follow_trades has 5 rows, status='pending_exit'
        exit: all rows have exit_at set, paper_book=={}
```

### Delivery

- Build `test/harness.py` + 3 P0 tests
- Effort: ~3–4 days
- Branch: `test/harness-and-p0-tests` off the Phase 1 fix branch

---

## Phase 3 — Complete Layer-A Integration Coverage (24 Trading-Day Flows)

### Complete Trading-Day Flow Enumeration

#### PRE-OPEN / SETUP (Flows 1–6)

---

**Flow 1 — OpenAlgo admin login**

**Trigger:** `POST /auth/login` with username + password (+ optional TOTP)

**Code path:**
1. `blueprints/auth.py:272-398` — `login()` — validates CSRF, calls `authenticate_user`
2. `database/user_db.py:249` — Argon2 verify against `password_hash + API_KEY_PEPPER`
3. `blueprints/auth.py:342` — `session["user"] = username`; `session["logged_in"] = True`
4. `blueprints/auth.py:183` — `_try_resume_broker_session(username)` — skips OAuth if valid cached token

**Backend state:** Flask session cookie written
**Frontend:** Redirect to dashboard; nav bar shows logged-in state
**Coverage:** ❌ No test — `test/test_csrf.py` (0 tests), `test/test_logout_csrf.py` (logout only)

---

**Flow 2 — Zerodha OAuth login + token storage**

**Trigger:** `GET /broker` → Zerodha OAuth → `GET /zerodha/callback?request_token=...`

**Code path:**
1. `blueprints/brlogin.py:36` — `broker_callback("zerodha")` — receives `request_token`
2. `utils/auth_utils.py:394` — `handle_auth_success(auth_token, ...)`:
   - `database/auth_db.py:512` — `upsert_auth(...)` — Fernet-encrypts token
   - `utils/auth_utils.py:475` — `notify_broker_session_refreshed()` → ZMQ publish

**Backend state:** `openalgo.db:auth` row upserted with encrypted token
**Coverage:** ⚠️ Partial — `test/test_broker_session_auto_reconnect.py` (7 tests, ZMQ only)

---

**Flow 3 — Background processes registered at boot** *(P0-T1 above)*

**All APScheduler jobs registered by `app.py:750-854`:**

| Job ID | Trigger | Registered by |
|--------|---------|---------------|
| `sector_follow_smoke_check` | 15:18 IST mon-fri | `services/sector_follow_service.py:1849` |
| `sector_follow_entry` | 15:20 IST mon-fri | `services/sector_follow_service.py:1813` |
| `sector_follow_exit` | 15:25 IST mon-fri | `services/sector_follow_service.py:1820` |
| `sector_follow_eod_summary` | 15:30 IST mon-fri | `services/sector_follow_service.py:1834` |
| `sector_follow_data_health` | 16:30 IST mon-fri | `services/sector_follow_service.py:1841` |
| `futures_follow_entry` | 15:20 IST mon-fri | `services/futures_follow_service.py:1436` |
| `futures_follow_exit` | 15:25 IST mon-fri | `services/futures_follow_service.py` |
| `futures_follow_eod_watchdog` | 15:14 IST mon-fri | `services/futures_follow_service.py` |
| `futures_follow_eod_summary` | 15:30 IST mon-fri | `services/futures_follow_service.py:1450` |
| `scanner_comparison_eod` | 15:45 IST mon-fri | `services/scanner_comparison_eod_service.py:458` |

**Coverage:** ❌ No test

---

**Flow 4 — Master contract load**

**Trigger:** `handle_auth_success()` → `hook_into_master_contract_download(username, broker)`

**Code path:**
1. `utils/auth_utils.py:327` → APScheduler one-shot job
2. `database/master_contract_cache_hook.py` — downloads instrument file, populates `sym_token`
3. On completion: `socketio.emit("cache_loaded")` — UI notified

**Coverage:** ⚠️ Partial — `test/test_master_contract_instrumenttype.py` (3 tests, field mapping only)

---

**Flow 5 — Boot data convergence (sector_follow + scanner)**

**Code path:**
1. `services/sector_follow_backfill_scheduler.py:282` → daemon thread → waits for broker session
2. Calls `check_and_refresh_if_stale(today)` on index (8 symbols) + stock (30 symbols) backfill
3. `services/scanner_backfill_scheduler.py:335` → same pattern, covers both `1m` and `D` for ~200 scanner symbols
4. Transient DuckDB lock during Phase 1 race: `is_transient_lock_error` → skip + retry

**Coverage:** ✅ `test/test_sector_follow_backfill_convergence.py` (10 tests), `test/test_scanner_universe_backfill.py` (17 tests). Gap: daemon-thread path untested

---

**Flow 6 — Strategy mode resolution on startup**

**Code path:**
1. `database/strategy_mode_db.py:91` — `get_mode(strategy_name)` reads `strategy_mode` table
2. `services/mode_service.resolve_strategy_mode()` — unified → legacy → env → `sandbox/run`
3. `database/strategy_runtime_override_db.py` — active pause/kill_switch override check

**Coverage:** ✅ `test/test_strategy_mode.py` (11), `test/test_mode_service.py` (19), `test/test_strategy_runtime_override.py` (10). Gap: mode persistence (Flow 23/P0-T2)

---

#### MARKET HOURS (Flows 7–12)

---

**Flow 7 — Live tick ingestion pipeline**

**Code path:**
1. `broker/zerodha/streaming/zerodha_adapter.py` → normalise tick → ZMQ PUB on `tcp://127.0.0.1:5555`
2. `websocket_proxy/server.py:103` — ZMQ SUB receives, delivers to browser WS clients (port 8765)
3. `services/scanner_service.py` — bar aggregator subscribes in-process → builds 1m/5m bars

**Coverage:** ⚠️ Partial — `test/test_bar_aggregator.py` (15 tests). `test/test_ws_proxy_full_integration.py` (0 tests implemented, Linux-only)

---

**Flow 8 — Scanner evaluation (5m bar close → scan_results → SocketIO)**

**Code path:**
1. `services/scanner_service.py:1069` — `_evaluate_definitions(symbol, bar)`
2. Market-hours gate (Tier-1): skips outside `[09:15, 15:30]` IST
3. D-bar-date verify: aborts post-settle if latest D-bar is pre-today
4. `database/scanner_db.py` — writes to `scan_results` table; SocketIO `scan_hit` emitted

**Coverage:** ✅ `test/test_scanner_service.py` (40 tests), `test/test_fno_intraday_buy_chartink.py` (18), `test/test_fno_intraday_sell_chartink.py` (18)

---

**Flow 9 — Scanner UI update (SocketIO → browser row)**

**Trigger:** `scan_hit` SocketIO event → React scanner page appends row
**Coverage:** ❌ No test (Layer B gap)

---

**Flow 10 — Chartink webhook → simplified engine → 5m breakout → sandbox**

**Code path:**
1. `blueprints/chartink.py:959` — `simplified_stock_engine_webhook(webhook_id)` — validates webhook, creates scan_cycle audit row
2. `engine.activate_buy_symbol(symbol)` → loads history → `engine.on_new_candle()` on next bar
3. On signal: `svc._place_entry_order(sig, ...)` → `sandbox_place_order(...)` → `trade_journal`

**Coverage:** ✅ `test/e2e/test_chartink_webhook_to_sandbox.py` (4 scenarios). Gap: HTTP route itself not tested end-to-end

---

**Flow 11 — `sandbox_place_order` → sandbox.db → position visible**

**Code path:**
1. `services/sandbox_service.sandbox_place_order(order_dict, api_key)` → `fund_manager` margin check
2. `sandbox/execution_engine.py:36` → `ExecutionEngine.place_order()` → simulated fill
3. `sandbox.db:SandboxOrders`, `SandboxPositions`, `SandboxTrades` written

**Coverage:** ⚠️ Partial — `test/sandbox/*` (19 tests). Gap: `sandbox_place_order` service wrapper untested end-to-end

---

**Flow 12 — Positions visible via `/api/v1/positions` after sandbox fill**

**Code path:**
1. `GET /api/v1/positions` → `restx_api/positions.py` → reads `SandboxPositions` (sandbox mode)
2. `resolve_effective_mode()` determines sandbox vs. live path

**Coverage:** ❌ No test. `test/test_read_services_dispatch.py` (16 tests) mocks the response

---

#### CLOSE-OF-MARKET (Flows 13–18)

---

**Flow 13 — Sector_follow 15:18 smoke check + 15:20 entry** *(P0-T3 above)*

**Code path:**
1. `services/sector_follow_service.py:1849` → `assert_data_pipeline_healthy()` at 15:18
2. On fail: `strategy_runtime_override(type='pause', expires_at=15:30)` + Telegram alert
3. 15:20: `run_entry()` → `evaluate_candidates()` → top-5 by vol_ratio → BUY orders

**Coverage:** ⚠️ `test/test_sector_follow_service.py` (60 tests), but `test_sector_follow_full_cycle.py` (0 tests)

---

**Flow 14 — Futures_follow 15:20 entry**

**Code path:**
1. `services/futures_follow_service.py:958` — `run_entry()` reuses sector_follow evaluator
2. Resolves NIFTY near-month futures symbol via `SymToken` (skips contracts expiring ≤1 day)
3. NIFTY NRML MARKET order; sized at 50% capital SPAN margin

**Coverage:** ✅ `test/test_futures_follow_service.py` (46 tests). Gap: expiry-roll edge (last-Tuesday NIFTY monthly)

---

**Flow 15 — EOD watchdog 15:14 flatten**

**Code path:**
1. `services/eod_watchdog_service.py:92` — `start_eod_watchdog()` at 15:14 IST
2. Reads `trade_journal` for open entries, places SELL/BUY to close
3. Hard cap: fires at `min(eod_exit_time, 15:14)` — sandbox rejects MIS after 15:15

**Coverage:** ✅ `test/test_eod_watchdog_service.py` (29 tests). Gap: `test/test_eod_three_layer_defense.py` (0 tests)

---

**Flow 16 — Sector_follow 15:25 exit (T+1 square-off)** *(P0-T3 above)*

**Code path:**
1. `services/sector_follow_service.py:1224` — `run_exit()` — reads `paper_book` + `sector_follow_trades`
2. Never gated by override (exits always run); places SELL MARKET orders

**Coverage:** ⚠️ Unit tests only; `test_critical_flows.py` cycle test is SKIPPED

---

**Flow 17 — EOD reconciliation 15:30**

**Code path:**
1. `services/engine_eod_reconciliation_service.py:150` — `reconcile_engine_journal(today)`
2. Reads `sandbox.db` read-only; stamps missing exit rows (`exit_reason='sandbox_eod_squareoff'`)
3. Idempotent

**Coverage:** ✅ `test/e2e/test_engine_eod_reconciliation.py` (8 tests)

---

**Flow 18 — Scanner-vs-Chartink 15:45 comparison**

**Code path:**
1. `services/scanner_comparison_eod_service.py:373` — `compute_comparison(date)` — Jaccard + recall
2. `database/scanner_comparison_db.py` — delete-then-insert (idempotent)
3. `notification_service.notify("scanner_comparison", ...)` → Telegram

**Coverage:** ✅ `test/test_scanner_comparison_eod_service.py` (3 tests), `test/test_scan_comparison.py` (8 tests)

---

#### POST-CLOSE (Flows 19–24)

---

**Flow 19 — EOD reports (sector_follow + futures_follow)**

**Code path:**
1. `services/sector_follow_service.py:1835` — `run_eod_summary()` — writes `eod_reports/YYYY-MM-DD.md`
2. Same at `services/futures_follow_service.py:1450`

**Coverage:** ⚠️ Tests in `test_critical_flows.py` SKIPPED (retired intent-DB arch)

---

**Flow 20 — Data freshness auto-pause (16:30)**

**Code path:**
1. `services/sector_follow_service.py:1841` — `run_data_health_check()` at 16:30 IST
2. `services/data_freshness_service.check_strategy_data_ready()` — reads `historify.duckdb`
3. On stale: `data_health_check` row written + Telegram + `strategy_runtime_override(type='pause', expires_at=tomorrow 15:30)`

**Coverage:** ⚠️ `test/test_data_freshness_service.py` (15 tests); auto-pause side-effect only in SKIPPED tests

---

**Flow 21 — WS proxy reconnect on re-login (ZMQ CACHE_INVALIDATE)**

**Code path:**
1. `database/auth_db.py:512` — `upsert_auth()` → ZMQ publish `CACHE_INVALIDATE_AUTH_<user_id>`
2. `websocket_proxy/server.py:1699` — ZMQ SUB listener → `_handle_cache_invalidation()`
3. Snapshots subscriptions → re-init token → reconnect → re-subscribe

**Coverage:** ✅ `test/test_broker_session_auto_reconnect.py` (7 tests). Gap: real proxy path in `test_ws_proxy_full_integration.py` (0 tests, Linux-only)

---

**Flow 22 — Aggregator-vs-historify source switch for today's data**

**Key seam:** `production_intraday_provider` → `ScannerService.get_today_ohlcv(symbol, date)` — reads in-process aggregator bars, falls back to historify. The race condition (Phase 1) is the failure mode of this fallback.

**Coverage:** ⚠️ `test/test_scanner_smoke_check.py` (13 tests)

---

**Flow 23 — Strategy mode persistence across restart** *(P0-T2 above)*

**Critical property:** `strategy_mode` rows survive app restart. An accidental revert to
scaffold would disable live trading silently.
**Coverage:** ❌ No test

---

**Flow 24 — Strategies dashboard API (mode + override real-time status)**

**Code path:** `GET /api/strategies/status` → reads all strategy modes + active overrides + kill-switch
**Coverage:** ✅ `test/test_strategies_dashboard_api.py` (20 tests). Layer B gap: no UI test

---

### Coverage Map

| Flow | Name | Layer-A | Layer-B | Status |
|------|------|---------|---------|--------|
| 1 | OpenAlgo login | ❌ | ❌ | ❌ No test |
| 2 | Zerodha OAuth | ⚠️ | ❌ | ⚠️ Partial |
| 3 | Background jobs registered | ❌ | ❌ | ❌ No test |
| 4 | Master contract load | ⚠️ | ❌ | ⚠️ Partial |
| 5 | Boot data convergence | ✅ | ❌ | ⚠️ Daemon path untested |
| 6 | Strategy mode resolution | ✅ | ❌ | ✅ Well covered |
| 7 | Live tick ingestion | ⚠️ | ❌ | ⚠️ ZMQ chain untested |
| 8 | Scanner evaluation | ✅ | ❌ | ✅ Well covered |
| 9 | Scanner UI update | ❌ | ❌ | ❌ No test |
| 10 | Chartink webhook → engine | ✅ | ❌ | ⚠️ HTTP route not tested |
| 11 | Sandbox order placement | ⚠️ | ❌ | ⚠️ Service wrapper untested |
| 12 | Positions visible in UI | ❌ | ❌ | ❌ No test |
| 13 | Sector_follow 15:18+15:20 | ⚠️ | ❌ | ⚠️ Full cycle = 0 tests |
| 14 | Futures_follow 15:20 | ✅ | ❌ | ⚠️ Expiry-roll edge |
| 15 | EOD watchdog 15:14 | ✅ | ❌ | ⚠️ 3-layer defense = 0 tests |
| 16 | Sector_follow 15:25 exit | ⚠️ | ❌ | ⚠️ E2E test SKIPPED |
| 17 | EOD reconciliation 15:30 | ✅ | ❌ | ✅ Well covered |
| 18 | Scanner comparison 15:45 | ✅ | ❌ | ✅ Well covered |
| 19 | EOD reports | ⚠️ | ❌ | ⚠️ Tests SKIPPED |
| 20 | Data freshness 16:30 | ⚠️ | ❌ | ⚠️ Auto-pause side-effect untested |
| 21 | WS proxy reconnect | ✅ | ❌ | ⚠️ Real proxy path: 0 tests |
| 22 | Aggregator source switch | ⚠️ | ❌ | ⚠️ Phase 1 is the fix |
| 23 | Mode persistence (restart) | ❌ | ❌ | ❌ No test |
| 24 | Strategy dashboard | ✅ | ❌ | ⚠️ UI layer gap |

**Breakdown:** ❌ No test: 5 flows. ⚠️ Partial: 13 flows. ✅ Well covered: 6 flows.
**Layer-B coverage: 0 of 24 flows.**

### Per-Flow Test Plan (Layer A, Phases 2-3)

**Flow 1 — Admin login (new: `test/test_login_flow.py`)**
- POST `/auth/login` correct creds → 302 + session cookie
- POST wrong creds → 401; no session
- Priority: P0

**Flow 2 — Zerodha OAuth callback (new: `test/test_zerodha_auth.py`)**
- Mock `auth_api.authenticate`; GET `/zerodha/callback?request_token=rt_abc`
- Assert: `auth_db.get_auth_token("admin")` returns decrypted token; ZMQ publish called once
- Priority: P0

**Flow 3 — Job registration (extend `test/test_boot_broker_session.py`)** — P0-T1 above

**Flow 4 — Master contract (new: `test/test_master_contract_flow.py`)**
- Mock broker instrument download; call `hook_into_master_contract_download`
- Assert: `sym_token` table has ≥1 row; `socketio.emit("cache_loaded")` called
- Priority: P1

**Flow 5 — Boot convergence daemon (extend `test/test_sector_follow_backfill_convergence.py`)**
- Call `init_sector_follow_backfill(app)` with monkeypatched thread; trigger synchronously
- Assert: `check_and_refresh_if_stale` called; stale symbols fetched
- Priority: P1

**Flow 6 — Mode persistence** — P0-T2 above

**Flow 7 — Tick→bar→scan chain (new: `test/test_tick_to_scan_integration.py`)**
- Seed definitions; inject 5 ticks; close 5m bar; call `_evaluate_definitions`
- Assert: `scan_results` row exists; `scan_hit` SocketIO event emitted
- Priority: P1

**Flow 8 — Scanner gates (extend `test/test_scanner_service.py`)**
- Post-close gate: call at 17:00 → 0 results; stale D-bar → WARNING logged
- Priority: P1

**Flow 9 — Scanner UI** — Layer B (Phase 4)

**Flow 10 — Webhook HTTP route (extend `test/test_chartink_webhook_audit.py`)**
- `client.post("/chartink/simplified-stock-engine/<id>", json={"stocks": "RELIANCE", ...})`
- Assert: 200; scan_cycle row; engine armed
- Priority: P0

**Flow 11 — Sandbox order service (extend `test/sandbox/test_sandbox_order_flow.py`)**
- `sandbox_service.sandbox_place_order(order_dict, api_key)` with LTP injected
- Assert: `SandboxOrders` row FILLED; `SandboxPositions` qty > 0; margin reduced
- Priority: P0

**Flow 12 — Positions API (new: `test/test_positions_api.py`)**
- Seed `SandboxPositions` row; `GET /api/v1/positions?apikey=k`
- Assert: response contains symbol with `qty > 0`
- Priority: P1

**Flow 13 + 16 — Sector_follow cycle** — P0-T3 above

**Flow 14 — Futures expiry roll (extend `test/test_futures_follow_service.py`)**
- Seed SymToken with NIFTY-JUN26 expiry=today; NIFTY-JUL26 expiry=30d out
- Assert: `resolve_futures_symbol("NIFTY", as_of=today)` returns JUL26, never JUN26
- Priority: P1

**Flow 15 — EOD three-layer defense (fill `test/test_eod_three_layer_defense.py`)**
- Seed open MIS position; clock=15:10: tick-driven layer skips; clock=15:14: watchdog closes
- Assert: after watchdog: exit row with `exit_reason='eod_watchdog'`; after reconcile: no duplicate
- Priority: P0

**Flow 17 — EOD reconciliation** — already covered ✅

**Flow 18 — Scanner comparison Telegram (extend `test/test_scanner_comparison_eod_service.py`)**
- Assert `notify` called with `event_type="scanner_comparison"` and non-empty body
- Priority: P2

**Flow 19 — EOD report (rewrite `test/e2e/test_critical_flows.py` for mode-only arch)**
- `fire_job("sector_follow_eod_summary")`; assert `eod_reports/YYYY-MM-DD.md` exists + sections
- Priority: P1

**Flow 20 — Data freshness auto-pause (extend `test/test_data_freshness_service.py`)**
- Monkeypatch stale NIFTY; `fire_job("sector_follow_data_health")`
- Assert: `strategy_runtime_override` row written with `type='pause'`, `expires_at=tomorrow 15:30`
- Priority: P0

**Flow 21 — WS proxy reconnect (fill `test/test_ws_proxy_full_integration.py`, Linux-only)**
- Start real WebSocketProxy with ZMQ SUB; call `upsert_auth("admin", "new_tok", "zerodha")`
- Assert: within 2s — subscriptions snapshotted; adapter re-initialized with new token
- Priority: P1 (Linux CI only)

**Flow 22 — Aggregator source switch (extend `test/test_scanner_smoke_check.py`)**
- Aggregator has bars → `intraday_source='aggregator'`, no fallback warning
- Aggregator empty → WARNING logged "aggregator had no today bars"
- Priority: P1 (superseded by Phase 1 fix)

**Flow 23 — Mode persistence** — P0-T2 above

**Flow 24 — Strategy dashboard (Layer B, Phase 4)**

**Total Layer-A tests: 22 new/extended**

---

## Phase 4 — Playwright (Layer B) + Synthetic Day (Layer C)

### Layer B — Playwright (5 tests)

**Setup:** `test/e2e_playwright/conftest.py` boots BootHarness backend on port 5000; starts Playwright browser.

| Test | Flow | What it verifies |
|------|------|-----------------|
| `test_login_to_dashboard.py` | 1 | Login form → redirect → nav bar shows username |
| `test_scanner_signalhit_updates_ui.py` | 9 | Emit `scan_hit` SocketIO → new `<tr>` row in scanner table within 2s, with today's date label |
| `test_positions_after_sandbox_fill.py` | 12 | Sandbox fill → `/positions` shows position card |
| `test_strategies_dashboard_badges.py` | 24 | strategy_mode=sandbox → sandbox badge visible; active override → PAUSED badge |
| `test_strategies_mode_change.py` | 6+24 | Update mode via API → dashboard badge updates without page reload |

### Layer C — Synthetic Trading Day

**File:** `test/e2e/test_synthetic_day.py`

Single test, <60s, exercises all phases in sequence via BootHarness:

```
# PRE-OPEN
harness = BootHarness.create(); harness.mock_broker_login()
harness.seed_historify(UNIVERSE, bars_20d); harness.seed_scanner_history(UNIVERSE, D_bars)
harness.fire_job("sector_follow_boot_convergence")
assert harness.no_errors()

# SCANNER WARMUP (Phase 1 regression)
harness.refresh_scanner_provider()    # must not sentinel-poison
harness.inject_aggregator_bars(UNIVERSE, today_bars)
assert harness.scanner_provider_loaded()

# 15:14 WATCHDOG
harness.seed_open_trade_journal("simplified_engine", "RELIANCE")
harness.set_clock(15, 14); harness.fire_job("simplified_engine_eod_watchdog")
harness.assert_journal_entry("RELIANCE", exit_reason__in=["eod_watchdog", "sandbox_eod_squareoff"])

# 15:18 SMOKE CHECK + 15:20 ENTRY
harness.set_clock(15, 18); harness.fire_job("sector_follow_smoke_check")
assert not harness.override_active("sector_follow")
harness.set_clock(15, 20); harness.fire_job("sector_follow_entry")
assert harness.n_journal_rows("sector_follow") == 5

# 15:25 EXIT
harness.set_clock(15, 25); harness.fire_job("sector_follow_exit")
assert harness.n_open_sector_follow() == 0

# 15:30 EOD + 15:45 COMPARISON
harness.fire_job("sector_follow_eod_summary"); assert harness.eod_report_written_today()
harness.fire_job("scanner_comparison_eod"); assert harness.scanner_comparison_row_exists()

# 16:30 DATA HEALTH
harness.fire_job("sector_follow_data_health"); assert harness.data_health_ok()

# FINAL
harness.assert_no_errors()
```

---

## Rollout Plan

| Phase | Work | Branch | Effort |
|-------|------|--------|--------|
| **0** | Fix duplicate decorator bug; PR `fix/124-duckdb-write-lock` → dev; 1 concurrency test | `fix/124-duckdb-write-lock` | 0.5 days |
| **1** | Fix C in `_get()` + Fix B invalidation hook + Jaccard alert + 5 regression tests | `fix/<N>-scanner-history-sentinel` off Phase 0 | 1.5 days |
| **2** | BootHarness + P0-T1, P0-T2, P0-T3 | `test/harness-and-p0-tests` off Phase 1 | 3–4 days |
| **3** | Complete Layer-A (22 tests) | `test/complete-layer-a` off Phase 2 | 1.5–2 weeks |
| **4** | Playwright (5 tests) + synthetic day (1 test) | `test/e2e-playwright-layer-b` off Phase 3 | 1–1.5 weeks |

**Total:** ~4.5–5 weeks

---

## Appendix — Empty/Scaffolded Test Files (Action Required)

| File | Tests | Action |
|------|-------|--------|
| `test/test_boot_broker_session.py` | 0 | P0-T1 (Flow 3) |
| `test/test_csrf.py` | 0 | Flow 1 sub-test |
| `test/test_sector_follow_full_cycle.py` | 0 | P0-T3 (Flows 13+16) |
| `test/test_eod_three_layer_defense.py` | 0 | Flow 15 |
| `test/test_ws_proxy_full_integration.py` | 0 | Flow 21 (Linux-only) |
| `test/test_connection_pool.py` | 0 | From #94 batch-2 |
| `test/test_connection_manager_predicate.py` | 0 | From #94 batch-2 |
| `test/e2e/test_critical_flows.py` | 11 (ALL SKIPPED) | Rewrite for mode-only arch |
| `test/e2e/test_fno_flows.py` | check actual count | Verify implemented |

---

*This document is a planning artifact only. No source or test code was modified.*
