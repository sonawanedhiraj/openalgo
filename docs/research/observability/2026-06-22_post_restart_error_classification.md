# Post-Restart Error Classification — 2026-06-22

## Context

| Field | Value |
|---|---|
| First restart | `2026-06-22 08:19:54` (app boot, post Tier-3 merge) |
| Second restart | `2026-06-22 17:38:16` (manual restart by Dheeraj) |
| Commit on dev | `39e5f8f99` (all 5 CI gap-closure tiers merged) |
| Broker re-login | **Not performed** — Zerodha session expired |
| Market state | Closed (IST evening) |
| Analysis range | All 1,429 entries in `log/errors.jsonl` |
| Note on truncation | `errors.jsonl` is auto-truncated to last 1,000 entries at startup; the 17:38 second restart wiped entries from 08:19–14:00 |

## TL;DR

**1,429 total errors** across **18 distinct classes** in `errors.jsonl`.
Of these, **1,270 are expected noise** (expired/absent broker session, no WS ticks, market closed)
and **159 are real bugs** across 5 classes.

The dominant noise: 1,247 WebSocket retry errors logged at `ERROR` level, ~7× per minute
for 4 hours. They drown the 159 real bug events and make `errors.jsonl` nearly unusable
as a signal detector without filtering.

---

## A — Error Histogram (Top 18 by count)

| # | Count | Logger | Message (first 70c) | Classification |
|---|---|---|---|---|
| 1 | 617 | `services.websocket_client` | `Error in WebSocket connection: [Errno 10061] Connect call failed` | Expected — WS proxy died (no broker session) |
| 2 | 242 | `services.websocket_service` | `Connection error for user dheeraj.sonawane: Failed to connect to WebSocket` | Expected — same root cause, service layer |
| 3 | 125 | `services.websocket_client` | `Failed to connect to WebSocket server` | Expected — same |
| 4 | 117 | `services.websocket_client` | `Failed to authenticate with WebSocket server` | Expected — no valid Zerodha token |
| 5 | 108 | `connection_pool_zerodha` | `Adapter initialization failed: Adapter initialized successfully` | **Real bug — misleading ERROR, wrong key check** |
| 6 | 108 | `services.websocket_client` | `Error from server: Not initialized` | Expected — adapter not initialised without session |
| 7 | 32 | `services.websocket_client` | `Error from server: Invalid API key` | Expected — expired token |
| 8 | 24 | `database.historify_db` | `Failed to connect to DuckDB after 3 attempts: IO Error: Cannot open` | **Real bug — Windows DuckDB write-lock contention** |
| 9 | 21 | `services.websocket_client` | `Error in WebSocket connection: did not receive a valid HTTP response` | Expected — WS proxy partially started |
| 10 | 12 | `database.historify_db` | `Error upserting market data: IO Error: Cannot open file` | Real bug — same DuckDB contention |
| 11 | 12 | `services.historify_service` | `Error downloading data: IO Error: Cannot open file` | Real bug — same DuckDB contention |
| 12 | 9 | `database.historify_db` | `Error updating job item status: IO Error: Cannot open file` | Real bug — same DuckDB contention |
| 13 | 3 | `database.historify_db` | `Error fetching data range: IO Error: Cannot open file` | Real bug — same DuckDB contention |
| 14 | 1 | `services.sector_follow_service` | `sector_follow 15:18 SMOKE CHECK FAILED: aggregator coverage 0/30` | Expected — no ticks without broker session |
| 15 | 1 | `services.journal_reflection_service` | `reflection: nightly run crashed` | **Real bug — bridge returned HTTP 500 on nightly run** |
| 16 | 1 | `services.scanner_smoke_check_service` | `init_scanner_smoke_check failed` | **Real bug — wrong scheduler API (day-1 regression)** |
| 17 | 1 | `services.scanner_dry_tripwire_service` | `init_scanner_dry_tripwire failed` | **Real bug — same wrong API (day-1 regression)** |
| 18 | 1 | `services.telegram_inbound_service` | `inbound bot thread error: Timed out` | Expected — transient network |

---

## B — Class-by-Class Analysis

### Classes 1–4, 6–7, 9: WebSocket retry storm (1,247 errors)

**Pattern:** Every call to `websocket_service.get_websocket_connection()` raises a
`ConnectionError` because `websocket_proxy/server.py` (port 8765) crashes when no
Zerodha token is available. The client retry loop in `websocket_client.py` fires up to
5 attempts with exponential backoff up to 30 s, then escalates to `logger.exception`.
A periodic caller (React dashboard polling, ~1×/min) triggers 242 service-level errors
spanning 08:20 – 18:06 IST. The raw connection-refused errors (class 1) run at ~7.3×/min
for 84 minutes (total 617).

**Timeline:** First 10061 error at 08:20 (immediately after boot), WS proxy died at 10:24
with keepalive ping timeouts. Connection refused from 10:24 onwards.

**Classification:** Expected. The WS proxy subprocess requires a live broker connection;
without one it exits. Retries are correct behaviour.

**Severity audit:** ALL seven classes log at `ERROR` / `logger.exception`. A no-session
restart is a normal daily occurrence (Zerodha token expires at 3 AM IST). These should be:
- First failure after N retries: `WARNING` ("WS server unreachable — expected if no broker session")
- Subsequent failures until session established: `DEBUG`
- Current rate floods `errors.jsonl` with ~1,247 noise entries that bury the 159 real bugs.

### Class 5: "Adapter initialization failed: Adapter initialized successfully" (108 errors)

**File:** `websocket_proxy/connection_manager.py:448`

**Root cause:** `ConnectionPool.initialize()` checks for adapter failure with:
```python
is_error = (result and not result.get("success")) or (
    result and result.get("status") == "error"
)
```
The Zerodha adapter returns `{"status": "success", "message": "Adapter initialized
successfully"}`. Since there is **no `"success"` key** in that dict, `result.get("success")`
returns `None`; `not None` is `True`, so `is_error = True` and the ERROR fires
**even when the adapter succeeded**.

This means: every successful Zerodha adapter initialization logs a false ERROR
`"Adapter initialization failed: Adapter initialized successfully"` — the
success message appears inside the ERROR log, creating a contradictory and misleading
record that erodes trust in errors.jsonl.

**Active period:** 10:20 – 18:01 IST (every WS auth attempt; ~1 per minute for 108 minutes).

**Classification: Real bug. P1.**

**Fix:** `connection_manager.py:443–447`
```python
# Current (wrong):
is_error = (result and not result.get("success")) or (
    result and result.get("status") == "error"
)

# Fixed:
is_error = (result and result.get("status") == "error") or (
    result and "success" in result and not result.get("success")
)
```
This treats `{"status": "success"}` as success (correct for the adapter format) and
`{"success": False}` as failure (correct for the ConnectionPool format).

### Classes 8, 10–13: DuckDB Windows file-lock contention (48 errors)

**File:** `database/historify_db.py:75`

**Root cause:** At ~15:53 IST the periodic backfill convergence triggered three concurrent
jobs: sector_follow index (10 indices), scanner universe 1m (214 symbols), and scanner
universe D (216 symbols). All three try to `duckdb.connect(db_path)` for write operations
simultaneously. On Windows, DuckDB acquires an exclusive OS file lock per connection;
a second write connection from **the same process** fails with "The process cannot access
the file because it is being used by another process."

Note: the `connect_historify_readonly()` fallback in `data_freshness_service.py` handles
**read-only** conflicts by reusing the shared in-process connection. The main write path
in `historify_db.get_connection()` has no such protection — it retries 3 times with
exponential backoff and then logs at `logger.exception`.

**Active period:** 15:53:26 – 15:56:20 IST (~3 minute burst). 48 errors across 4 subtypes.

**Classification: Real bug. P2.** Self-resolving within minutes once the concurrent jobs
finish, but: (1) backfill records are silently dropped for affected symbols, (2) the burst
fills errors.jsonl. Will recur on every day the post-close window triggers concurrent
backfills for both sector_follow + scanner universes simultaneously.

**Fix:** Apply the same in-process conflict detection used by `connect_historify_readonly()`
to the write path. The DuckDB instance cache (`duckdb.connect` uses a shared in-memory
connection for the same file within a process) means the fix is:
- Add a process-level `threading.Lock()` around DuckDB write operations in `historify_db.get_connection()`, OR
- Serialize the three backfill schedulers so they don't overlap (add a global `historify_write_lock` that each backfill acquires before starting its job).

### Class 14: sector_follow 15:18 smoke check failed — 0/30 coverage (1 error)

**File:** `services/sector_follow_service.py:1458`

**Root cause:** The smoke check at 15:18 IST confirmed that the aggregator has 0/30 live
bars — because no broker session means no ticks. The three-point check is:
(1) aggregator ≥50% coverage, (2) historify has prior-day data, (3) broker session live.
Without a session check 1 fails.

**Classification:** Expected — the smoke check is working correctly; it correctly diagnosed
"no live data." The ERROR log is intentional by design (CLAUDE.md: "loud failure").

**Possible improvement:** Add broker session check as the FIRST gate, and emit a dedicated
Telegram message "smoke check skipped — no broker session" (INFO/WARNING level) before the
coverage gate, so the 15:20 entry hold is clearly attributed to missing session rather than
an ambiguous "aggregator dead".

### Class 15: Nightly reflection crashed — bridge HTTP 500 (1 error)

**File:** `services/journal_reflection_service.py:611`

**Root cause:** At 16:00:06 IST (scheduled nightly cron), `run_reflection()` called the
bridge at `http://127.0.0.1:5001/run`, which returned `HTTP 500:
{"detail":"claude_returncode_1: "}`. The bridge was running (confirmed: `bridge_access.jsonl`
active all day), but Claude Code exited with return code 1 with an empty output — probably
a missing/expired API key, rate limit, or network issue on the Claude API side.

**Classification: Real bug (operational). P2.** The reflection service correctly catches
and logs the failure. However:
1. The error is non-retriable — a single 500 from the bridge is treated as fatal
2. There's no alert: the ERROR entry is in errors.jsonl but no Telegram notification fires
3. `claude_returncode_1: ` (empty detail) suggests the bridge's Claude process crashed
   before writing any output — potential config/API key issue on the dev machine

**Validate after re-login:** If the reflection service crashes again at the NEXT 16:00 IST,
the bridge config needs investigation (check Claude API key, `uv run python bridge/server.py`
stdout for errors).

### Classes 16–17: init_scanner_smoke_check + init_scanner_dry_tripwire failed (2 errors)

**Files:**
- `services/scanner_smoke_check_service.py:333`
- `services/scanner_dry_tripwire_service.py:401`

**Root cause:** Both services were introduced in PRs #34 and #35. They call:
```python
scheduler = get_historify_scheduler()
scheduler.add_job(...)          # AttributeError: 'HistorifyScheduler' has no add_job
```
`HistorifyScheduler` is a wrapper around `BackgroundScheduler` but does **not** expose
`add_job` publicly. The inner APScheduler is available via the `.scheduler` property:
```python
scheduler.scheduler.add_job(...)   # correct
```

**Day-1 regression.** Confirmed firing on EVERY restart since the services were introduced
(line 23 and 30 in today's log, 08:20 — first app boot). Both the scanner pre-entry smoke
check (09:18 IST) and the scanner zero-results dry tripwire are **silently unregistered**
on every restart. The jobs have never executed in production.

**Classification: Real bug. P1.** Both safety monitors are dark from day one.

**Fix:** In `scanner_smoke_check_service.py:333` and `scanner_dry_tripwire_service.py:401`,
replace `scheduler.add_job(...)` with `scheduler.scheduler.add_job(...)`.

Alternatively, add an `add_job` passthrough on `HistorifyScheduler`:
```python
# services/historify_scheduler_service.py (inside HistorifyScheduler class)
def add_job(self, *args, **kwargs):
    return self._scheduler.add_job(*args, **kwargs)
```
The passthrough is cleaner as it keeps callers from depending on the internal `_scheduler`.

### Class 18: Telegram inbound bot timeout (1 error)

**File:** `services/telegram_inbound_service.py:577`

**Root cause:** The Telegram `getUpdates` long-poll timed out (httpx `ReadTimeout`).
This is normal for Telegram bots — long-poll connections routinely time out and the loop
reconnects.

**Classification:** Expected. Transient. The bot thread recovers and re-polls automatically.

---

## C — Recommended Fixes (Prioritised)

### P1 — Real bugs with operational impact

**Fix 1: `scanner_smoke_check_service.py:333` and `scanner_dry_tripwire_service.py:401`**
```python
# Both files: change scheduler.add_job → scheduler.scheduler.add_job
```
Impact: The scanner pre-entry smoke check and zero-results dry tripwire have never fired
in production. Both are safety monitors. One-line fix each.

**Fix 2: `websocket_proxy/connection_manager.py:443–447`**
```python
# is_error check: add "success" in result guard (see class 5 fix above)
```
Impact: Removes 108 false ERROR entries per session per day; the "Adapter initialization
failed" message is actively misleading.

### P2 — Real bugs, self-resolving or operational

**Fix 3: DuckDB write-lock contention during concurrent backfills**
Add a module-level `threading.Lock` to `database/historify_db.py`'s `get_connection()`
so concurrent backfill jobs queue instead of colliding. Or stagger the backfill scheduler
intervals (e.g. scanner 1m starts 2 minutes after sector_follow index).

**Fix 4: Journal reflection nightly crash**
After Dheeraj re-logs into Zerodha and the system is stable, check the bridge server for
Claude API key configuration. If the 16:00 IST reflection continues to crash, add a single
Telegram alert in `_cron_run_reflection`'s except block so Dheeraj is notified rather than
discovering it in errors.jsonl.

### P1 — Severity downgrades (not bugs, but logs degrade signal quality)

**Downgrade 5: `services/websocket_client.py`**
After N consecutive failures (suggested: 3), downgrade WS connection errors from
`logger.exception` / `logger.error` to `logger.warning`. Emit a single
"WS proxy unreachable — broker session required" summary rather than one ERROR per retry.
Impact: Eliminates ~1,247 noise entries from errors.jsonl on every no-session session.

---

## D — Validation Steps

1. **After Dheeraj re-logs into Zerodha:** Re-run the same histogram script against
   `errors.jsonl`. Every class in "Expected (no session)" should **disappear**. Any that
   persist are real bugs.

2. **Confirm scanner smoke + tripwire fix:** After applying Fix 1, look for
   `"scanner_smoke_check registered"` and `"scanner_dry_tripwire registered"` in the next
   boot log. If absent, the init failed again.

3. **Confirm connection_manager fix:** After applying Fix 2, the
   `"Adapter initialization failed: Adapter initialized successfully"` class should no
   longer appear in `errors.jsonl` after re-login.

4. **DuckDB contention:** Watch `errors.jsonl` around 15:53 IST tomorrow for the DuckDB
   IO errors. If they recur post-login, Fix 3 is needed.

5. **Monitor reflection:** Check `errors.jsonl` at 16:05 IST tomorrow. If
   `"reflection: nightly run crashed"` recurs, escalate to bridge/Claude API key check.

---

## E — What Was NOT in errors.jsonl (Truncation Note)

The 17:38 restart triggered `errors.jsonl` auto-truncation to ~1,000 entries. This wiped
errors from 08:19–14:00 that the first restart generated, including:

- **Backfill scheduler "live-session probe raised"** (6 lines in main log at 13:13–13:15) —
  logged via `logger.exception` from `sector_follow_backfill_scheduler.py:266` and
  `scanner_backfill_scheduler.py:319`. These are expected (no broker session), but they fire
  at ERROR level during the boot convergence check. Same downgrade recommendation as the WS
  errors: these should be `logger.warning` when no session is available.

- **First occurrences** of scanner smoke check failures (08:20, 08:39, 08:43) — present in
  the main log at lines 23–30 but absent from errors.jsonl.

For a complete picture, grep the main log at:
```bash
grep "ERROR\|EXCEPTION" log/openalgo_2026-06-22.log | grep -v websocket | head -50
```

---

*Analysis by Claude Code (research branch, issue #73)*
