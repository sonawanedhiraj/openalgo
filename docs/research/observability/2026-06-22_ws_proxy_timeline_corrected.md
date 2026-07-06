# WS Proxy (port 8765) Failure — 2026-06-22 Corrected Post-Mortem

**Supersedes:** `2026-06-22_ws_proxy_down_root_cause.md` (#72) and
`2026-06-22_ws_proxy_port8765_timeline.md` (#73) — both reached incorrect
conclusions about the cause. This document is the authoritative record.

**Tracking issue:** #76
**Date of incident:** 2026-06-22
**Author:** Claude Code (research session)

---

## TL;DR

Port 8765 was dead from **IST 10:23 to ~IST 12:08** (~105 min gap, confirmed
ConnRefused in log). The immediate crash was `OSError: too many file descriptors
in select()` — Windows asyncio's `select()` FD limit was exhausted by 500+
leaked `_run_event_loop` threads. The root cause of the thread leak was a
pre-existing bug in `websocket_proxy/connection_manager.py` that falsely
classified a successful Zerodha adapter init as a failure, causing the
`websocket_service` to retry every 10 seconds from boot. Each retry leaked one
asyncio thread. After ~2 hours (IST 08:20–10:23), ~520 leaked threads exhausted
the FD ceiling and killed the WS proxy thread. There is **no watchdog or
auto-restart** for that thread, so port 8765 stayed down until Dheeraj's manual
restart at ~IST 12:08.

**The Zerodha session was valid the entire day.** The broker token was not the
cause. The failure is 100% internal to OpenAlgo's WS proxy layer.

---

## Expected Lifecycle

```
OpenAlgo boot
  → app_integration.start_websocket_proxy()
    → Thread("run_websocket_server", daemon=False).start()
      → asyncio.new_event_loop()
      → WebSocketProxy(host, port).start()
        → websockets.serve() binds port 8765
        → ZMQ CACHE_INVALIDATE listener starts
  → Port 8765 UP: accepts connections
  → On each client auth: ConnectionPool.initialize() → adapter.initialize()
      → ✅ Zerodha connects, subscriptions flow
  → Runs indefinitely; ZMQ CACHE_INVALIDATE triggers reconnect on re-login
  → NO watchdog: if thread dies, port stays down until manual restart
```

Under the Windows dev server (no eventlet) the WS proxy is an in-process
`Thread`, not a subprocess. Under gunicorn+eventlet it is a child process
(`python -m websocket_proxy.server`). This incident is Windows dev server only.

---

## Today's Timeline

All timestamps are **IST** (log uses IST = UTC+5:30; health.db uses UTC,
converted below).

| IST | UTC (health.db) | Event | Source |
|-----|-----------------|-------|--------|
| 08:19:54 | — | App boot. "Dirty working tree detected" warning. | `openalgo_2026-06-22.log` |
| 08:20 | 02:50 | WS proxy thread starts; port 8765 opens. First `websocket_client` connect attempt. Zerodha adapter init **succeeds internally** but `connection_manager` misclassifies it as failure → returns "Not initialized". | log + `health.db` |
| 08:20–10:23 | 02:50–04:53 | **Continuous 10-second retry loop.** Each cycle: websocket_client connects → auth request sent → server returns "Not initialized" → connection closes → websocket_service retries after 10s. Each cycle leaks one `Thread-NN (_run_event_loop)`. Thread count climbs from ~20 baseline to 543. | log (grep shows steady connect/fail pattern) |
| 08:39–08:44 | 03:08–03:13 | Two quick manual/debug restarts visible in health.db (ws_thread_alive briefly False → True). Retry loop resumes. | `health.db` state transitions |
| 10:23:48 | 04:53 | **CRASH:** `ERROR in app_integration: Error in WebSocket server thread: too many file descriptors in select()`. Windows `select()` FD_SETSIZE exceeded by 500+ leaked threads. WS proxy thread dies. | `openalgo_2026-06-22.log:12402` |
| 10:23:48 | 04:53 | `WARNING in app_integration: Error closing event loop: too many file descriptors in select()` — even the finally-block cleanup fails. | log |
| ~10:24–11:02 | 04:54–05:32 | Port 8765 briefly lingers (TCP TIME_WAIT / OS buffer). "timed out during opening handshake" errors. | log |
| ~11:02 | ~05:32 | Port 8765 fully down. `[Errno 10061] Connect call failed ('127.0.0.1', 8765)` errors begin flooding log from scanner, sector_follow, futures_follow, preflight. | log |
| ~12:08 | 06:38 | Dheeraj's manual OpenAlgo restart. WS proxy thread restarts. Port 8765 UP. | `health.db` (ws_thread_alive → True) |
| 12:08–17:38 | 06:38–12:08 | WS proxy running. Connection_manager bug still present — `ws_connections_total = 0` throughout. Retry loop continues leaking threads, but at a slower accumulation rate post-restart. | `health.db` |
| ~17:38 | 12:08 | Second state transition in health.db (possible second manual restart or observed recovery). | `health.db` |

**Port 8765 was down for approximately 105 minutes (IST 10:23–12:08).**

---

## First Event That Took Port 8765 Down

**IST 10:23:48 — `OSError: too many file descriptors in select()`**

From `log/openalgo_2026-06-22.log` line 12402:

```
[2026-06-22 10:23:48,818] ERROR in app_integration: Error in WebSocket server thread: too many file descriptors in select()
[2026-06-22 10:23:48,861] WARNING in app_integration: Error closing event loop: too many file descriptors in select()
```

This is the proximate cause. The `run_websocket_server` thread function in
`app_integration.py:284-285` logs `ERROR ... Error in WebSocket server thread: {e}`
then falls into the `finally` block which also fails trying to close the event
loop. After both log lines, the thread function returns and the thread dies.

Port 8765 did **not** go down from a Zerodha disconnect, a market-close event,
a re-login failure, or any broker-side event. It died because the Python process
ran out of asyncio-selectable file descriptors.

---

## Event-Driven Chain Audit

### Layer 1 — connection_manager false-failure bug (initiating cause)

**File:** `websocket_proxy/connection_manager.py:438–449`

The Zerodha adapter's `initialize()` returns the Adapter format:
```python
{"status": "success", "message": "Adapter initialized successfully"}
```
(`broker/zerodha/streaming/zerodha_adapter.py:108`)

The connection_manager `is_error` check (introduced to handle two response
formats) contains a logic defect:

```python
is_error = (result and not result.get("success")) or (
    result and result.get("status") == "error"
)
```

For the Zerodha response:
- `result.get("success")` → `None` (key is absent; adapter uses `"status"` not `"success"`)
- `not None` → `True`
- First condition: `(result and True)` → **`True`**
- Second condition: `result.get("status") == "error"` → `False` (status is `"success"`)
- **`is_error = True`** — incorrect

So `error_msg = result.get("message", ...) = "Adapter initialized successfully"` and the
manager logs:
```
ERROR: Adapter initialization failed: Adapter initialized successfully
```
and returns `{"success": False, "error": "Adapter initialized successfully"}`.

The WS server then sends `{"error": "Not initialized"}` back to the client.

**This bug means the WS proxy has never successfully connected to Zerodha on
this install** — `ws_connections_total = 0` in health.db for the entire day, even
during the IST 08:20–10:23 window when the thread was alive.

### Layer 2 — 10-second retry loop (FD/thread leak mechanism)

`websocket_proxy/websocket_client.py` (or equivalent consumer) reconnects after
10 seconds on auth failure. Each reconnect attempt creates a new asyncio task
or thread (`Thread-NN (_run_event_loop)`) that is not properly cleaned up when
the auth handshake fails at the WS application layer (as opposed to a TCP-level
failure which the loop does handle). Over ~2 hours of 10-second cycles: ~720
retries → ~520 leaked threads (543 total at death, ~20 baseline normal).

### Layer 3 — Windows select() FD limit (kill shot)

`app_integration.py:37` sets `asyncio.WindowsSelectorEventLoopPolicy()` — the
Selector event loop uses `select()` which has a hard `FD_SETSIZE` limit (~512
sockets) on Windows. When the leaked threads each hold open socket FDs and the
cumulative count exceeds FD_SETSIZE, the asyncio select loop raises:
```
OSError: [WinError 10022] too many file descriptors in select()
```

### Layer 4 — No restart watchdog (makes downtime permanent until manual)

`app_integration.py:303-308` starts the thread with `daemon=False` but provides
zero watchdog logic. `_websocket_server_started = True` is set once and never
reset. When the thread dies, the module-level flag remains `True` so `start_websocket_server()`
would be a no-op even if called. There is no health-check loop, no supervisor,
no auto-respawn. Port 8765 stays dead until the Flask process restarts.

### Layer 5 — Cascade to all consumers

With port 8765 down, all in-process consumers receive `[Errno 10061] Connect call
failed ('127.0.0.1', 8765)` on every tick-subscription attempt:
- In-house scanner (`scanner_service.py`) → 0 tick events → 0 bar closes → 0 signals
- `sector_follow_service.py` → 0 live intraday data
- `futures_follow_service.py` → 0 live intraday data
- Preflight checks → WS health = degraded → possible abort-on-warning
- `errors.jsonl` floods with ConnRefused entries from all the above

The MEMORY entry confirms: "port 8765 not running → 0 ticks → 0 scan_results + errors.jsonl flood → all Cowork preflight aborts."

---

## Current State (as of 2026-06-22 EOD)

- Port 8765: **UP** (Dheeraj's manual restart recovered it)
- `ws_connections_total`: **0** (connection_manager bug unresolved; proxy never connects to Zerodha)
- Zerodha session: **valid** (was valid all day; not a factor)
- Thread leak: **ongoing** (retry loop continues; will exhaust FDs again on a long-running session)
- Watchdog: **absent** (next FD exhaustion = next unrecoverable crash until manual restart)

---

## Root Cause

**Primary:** `websocket_proxy/connection_manager.py:443` — incorrect `is_error`
predicate misclassifies the Zerodha adapter's `{"status": "success", ...}` response
as a failure because it checks `not result.get("success")` which is `not None = True`
when the `"success"` key is absent. The fix is:

```python
# Before (buggy):
is_error = (result and not result.get("success")) or (
    result and result.get("status") == "error"
)

# After (correct):
is_error = (result and result.get("success") is False) or (
    result and result.get("status") == "error"
)
```

**Compounding factor:** `asyncio.WindowsSelectorEventLoopPolicy()` is
explicitly set at line 37 of `app_integration.py`. The Windows Selector loop
uses `select()` with a hard FD_SETSIZE cap. Switching to the Proactor event
loop (`asyncio.WindowsProactorEventLoopPolicy()`) uses IOCP which has no
equivalent FD limit and is the recommended production loop on Windows.

**Structural gap:** no watchdog/auto-restart for the WS proxy thread. A single
unhandled exception in `loop.run_until_complete(proxy.start())` kills the thread
permanently.

---

## Recommended Fixes

Priority order:

1. **Fix the `is_error` predicate** (`connection_manager.py:443`) — the one-line
   change above. This is the root cause; fixing it stops the retry loop, stops
   the thread leak, and means Zerodha actually connects. Ship to `dev`; verify
   `ws_connections_total > 0` in health.db after restart.

2. **Add a thread watchdog in `app_integration.py`** — a daemon thread that
   checks `_websocket_thread.is_alive()` every 60s and calls
   `start_websocket_server()` (after resetting `_websocket_server_started = False`
   and clearing `_websocket_proxy_instance`) when it finds the thread dead.
   Alternatively: use a `while True` loop inside `run_websocket_server()` that
   respawns `proxy.start()` on exception (exponential backoff, max 60s).

3. **Switch to ProactorEventLoop on Windows** (`app_integration.py:37`) — replace
   `asyncio.WindowsSelectorEventLoopPolicy()` with
   `asyncio.WindowsProactorEventLoopPolicy()`. This removes the FD_SETSIZE ceiling
   entirely for Windows dev sessions and is the correct Windows asyncio default
   since Python 3.8+.

4. **Add a health alert when `ws_connections_total = 0` persists** for more than
   60s while `ws_thread_alive = True` — the current state (thread alive, no broker
   connection) is undetectable from health.db today. This is the silent degradation
   class the CLAUDE.md instrumentation narrative calls out.

---

## What Was WRONG in Prior Research (#72 and #73)

Both prior docs hypothesised causes that are factually contradicted by the log:

**Wrong hypothesis 1: "No broker/Zerodha session"**
The Zerodha session was valid the entire day. `upsert_auth()` completed at boot;
the encrypted token persists in `db/openalgo.db`. The only daily expiry is ~03:00
IST which Dheeraj handles with a single manual re-login each trading morning. The
log shows the Zerodha adapter initialising successfully (`✅ Zerodha adapter
initialized for user dheeraj.sonawane`) at IST 08:20. The failure is in the
`connection_manager` layer, one abstraction level above the adapter, not in
Zerodha's session.

**Wrong hypothesis 2: "Market closed → WS proxy idle/down"**
The WS proxy is designed to run 24/7 regardless of market state (Zerodha
heartbeats keep the upstream connection alive; the proxy serves local subscribers
independently of NSE trading hours). A closed market is not a valid explanation
for port 8765 being down. The proxy was actively retrying connections throughout
pre-market, market hours, and post-market until it crashed at IST 10:23.

**What actually happened:**
An internal `connection_manager` bug that has probably existed since the
dual-format adapter response handling was added caused the proxy to never
successfully connect to Zerodha. The resulting 10-second retry loop leaked asyncio
threads, which on Windows exhaust the `select()` FD limit. Two hours of retries
crashed the WS proxy thread at IST 10:23. No watchdog exists to restart it.
The Zerodha session was irrelevant to the failure.
