# Test Gap Analysis — `connection_manager.py:443` Bug (closes #78)

**Date:** 2026-06-22
**Author:** Claude Code (research-only, no code changes)
**References:** Bug [#76](https://github.com/sonawanedhiraj/openalgo/issues/76) · Post-mortem PR [#77](https://github.com/sonawanedhiraj/openalgo/pulls/77) · CI gap umbrella [#58](https://github.com/sonawanedhiraj/openalgo/issues/58) · Fix commit `bad06f612`

---

## Background

On 2026-06-22, the WebSocket proxy (port 8765) crashed at ~10:23 IST and stayed
down for ~105 minutes. The root cause (documented in [#77](https://github.com/sonawanedhiraj/openalgo/pulls/77))
was a one-line predicate bug in `ConnectionPool.initialize()` that misread Zerodha's
success response as a failure — triggering a permanent 10-second retry loop that
leaked one asyncio thread per attempt until Windows `select()` `FD_SETSIZE` was
exhausted and the proxy thread crashed.

This document answers: **why did the bug escape Tiers 1–5 of the CI gap-closure
work from [#58](https://github.com/sonawanedhiraj/openalgo/issues/58)?**

---

## A — Buggy Code Path

**File:** `websocket_proxy/connection_manager.py`
**Class:** `ConnectionPool`
**Method:** `initialize(broker_name, user_id, auth_data, force)` (lines 387–460)
**Bug site:** line 443 (pre-fix)

```python
# BUGGY (pre-fix, commit before bad06f612):
is_error = (result and not result.get("success")) or (
    result and result.get("status") == "error"
)
```

`ZerodhaWebSocketAdapter.initialize()` (in `broker/zerodha/streaming/zerodha_adapter.py:64`)
returns **`{"status": "success", "message": "Adapter initialized successfully"}`** on success.
This dict has no `"success"` key.

Predicate evaluation for that return value:
- `result.get("success")` → `None` (key absent)
- `not None` → `True`
- `is_error = True` ← **wrong**

Every successful Zerodha adapter init was misclassified as a failure.
`ConnectionPool.initialize()` returned `{"success": False, "error": "Initialization failed"}`,
the proxy entered a 10 s retry loop, and each retry leaked one
`Thread-N (_run_event_loop)` asyncio thread. After ~720 retries (~2 hours),
Windows `select()` `FD_SETSIZE` was exhausted → `OSError: too many file
descriptors in select()` → proxy crash.

**Fix (commit `bad06f612`):** `result.get("success") is False` — uses identity
comparison so an absent key evaluates to `None is False` → `False` (not an error).

The comment immediately above the predicate (lines 440–442) explicitly documented
**two** formats; the actual Zerodha success format was a silent third:

```python
# Handle both response formats from adapters:
# - {"success": False, "error": "..."} (ConnectionPool format)
# - {"status": "error", "code": "...", "message": "..."} (Adapter format)
```

`{"status": "success"}` — the **only** success shape the Zerodha adapter
ever emits — was neither listed nor tested.

---

## B — Direct Test Coverage of `connection_manager.py`

```
grep -rn "from.*connection_manager|import connection_manager|ConnectionPool" test/
```

**Result:** `test/test_connection_manager_predicate.py` (line 12) — this is the
regression test **added by the fix commit**. It did not exist before `bad06f612`.

**No other test file in `test/` imported `connection_manager.py` before the fix.**

The module is imported by `websocket_proxy/broker_factory.py` (production path) and
referenced in comments inside `broker/*/streaming/` files, but zero test files
exercised the `ConnectionPool` class itself.

---

## C — Tier-by-Tier Audit

| Tier | File(s) | Could have caught? | Why it didn't |
|------|---------|-------------------|---------------|
| **T1** Playwright smoke | `frontend/e2e/smoke.spec.ts` | **No** | Loads 6 UI routes; checks `#root` has content + no JS errors. WS proxy is a separate subprocess — a dead proxy does not prevent any of the tested pages from loading. Port 8765 is never touched. |
| **T2** Boot session | `test/test_boot_broker_session.py` | **No** | Tests backfill schedulers (`_boot_worker`). Mocks `is_live_broker_session`. Never imports `connection_manager.py`. Orthogonal code path. |
| **T3** Mock broker E2E | `frontend/e2e/broker_happy_path.spec.ts` + `test/fixtures/mock_broker/app.py` | **No — even though it touches the trigger** | Closest call: `/_test/mock_auth` calls `handle_auth_success`, which publishes the ZMQ `CACHE_INVALIDATE` event that wakes `ConnectionPool.initialize()`. But the test never asserts WS proxy state. Its only assertion is `POST /api/v1/funds → 200 + balance ≈ ₹15L`, which is a REST response unaffected by a WS proxy in a retry loop. The bug could be live during the Tier 3 run and produce zero test failures. |
| **T4** Dist freshness | `.github/workflows/dist-freshness.yml` | **N/A** | Frontend-only: rebuilds `frontend/dist`, verifies it matches committed. Zero Python / WS proxy surface. |
| **T5** Vite chunk fix | Commit `c2943f5a5` (closes #57) | **N/A** | Frontend-only: removes `vendor-charts` from Vite `manualChunks`. Zero Python / WS proxy surface. |

### Tier 1 details — Playwright smoke

`smoke.spec.ts` (lines 6–11) hits `/login`, `/`, `/scanner`, `/strategies`,
`/analyzer`, `/tools`. All of these are Flask/React routes served by the Flask
app on port 5000. The WS proxy subprocess on port 8765 is started by `app.py` as
a separate thread/subprocess. Even with port 8765 completely dead, every smoked
route returns HTML (port 5000 is alive), React mounts, and `#root > *` has
children. No Playwright assertion probes port 8765 or checks WS connection state.

### Tier 2 details — Boot broker-session tests

`test_boot_broker_session.py` (lines 92–247) patches `services.broker_session_health.is_live_broker_session` and `run_boot_backfill_checks`, then calls `_boot_worker()`. This is the fix for the #55 class (backfill firing against a dead Zerodha token). The `ConnectionPool.initialize()` is not in any call stack these tests exercise.

### Tier 3 details — Mock broker happy-path

`broker_happy_path.spec.ts` flow: reset mock → set balance → POST `/setup` → POST `/auth/login` → GET `/_test/mock_auth` → GET `/apikey` → POST `/api/v1/funds`.

The mock broker (`test/fixtures/mock_broker/app.py:140–157`) returns:
```json
{"status": "success", "data": {"access_token": "mock_access_token_12345", ...}}
```
for `POST /session/token` — the correct Zerodha shape. The OAuth layer stores this
in `auth_db`. `handle_auth_success` then publishes the ZMQ `CACHE_INVALIDATE`
event. In a deployed container with the WS proxy running, this event **does**
trigger `ConnectionPool.initialize()`, which **would** hit the buggy predicate.

However: the mock server only implements Zerodha's **REST API** endpoints. It has
no WebSocket server. The `ZerodhaWebSocketAdapter.initialize()` (called from
`ConnectionPool.initialize()`) reads the auth token from DB and creates a
`ZerodhaWebSocket` Python client object — no network call — and returns
`{"status": "success"}`. The buggy predicate would misclassify this even in the
Tier 3 Docker container. But no test assertion checks:

- Is port 8765 accepting connections?
- Did `ConnectionPool.initialize()` mark `pool.initialized = True`?
- Is there a retry loop running?
- Is the thread count growing?

The test passes regardless because `POST /api/v1/funds` is served by the REST
layer, which is not gated on WS proxy state.

---

## D — Other Tests That Should Have Caught It

### `test_broker_session_auto_reconnect.py` — most relevant, still missed it

This is the most tightly related test. It uses a `FakeAdapter`:

```python
class FakeAdapter:
    def initialize(self, broker_name, user_id, auth_data=None):
        self.calls.append(("initialize", broker_name, user_id))
        return {"status": "success"}   # ← real Zerodha shape ✓
```

`FakeAdapter.initialize()` returns the correct Zerodha shape. **But the test
never routes this through `ConnectionPool`:**

```python
def _make_proxy(adapter, broker="zerodha"):
    proxy = WebSocketProxy.__new__(WebSocketProxy)
    proxy.broker_adapters = {USER_ID: adapter}   # ← direct injection
    ...
```

`broker_adapters` is populated directly with `FakeAdapter`. `ConnectionPool`
is never instantiated. `ConnectionPool.initialize()` and its predicate are never
called. The test correctly validates that the proxy re-initializes the adapter
on a ZMQ cache-invalidation event — but it validates it at the adapter level,
not the pool level.

### `test_websocket.py` — script only, not exercised in CI

Three `async def test_*` functions that connect to `ws://localhost:8765`. No
`pytest.skip()` guard; no pytest-asyncio marker. In CI, port 8765 is never bound
(no broker session), so these functions would time-out or fail with a connection
error if collected. They are effectively dead weight in the pytest run.

### `test_websocket_service.py` — explicitly skipped

Has `pytest.skip(..., allow_module_level=True)` at line 15. Not collected.

### `test_broker.py` — live integration script

Requires a real running OpenAlgo instance at `http://127.0.0.1:5000`. Not a
pytest suite in any meaningful sense. Not collected in CI.

---

## E — What Test WOULD Have Caught It

A direct unit test of `ConnectionPool.initialize()` with a mock adapter that
returns Zerodha's actual response shape:

```python
# test/test_connection_pool.py  (does NOT exist — should be created)

from unittest.mock import MagicMock, patch
from websocket_proxy.connection_manager import ConnectionPool


def _make_pool():
    adapter_cls = MagicMock()
    pool = ConnectionPool(adapter_cls, "zerodha", "testuser",
                          max_symbols_per_connection=10, max_connections=2)
    # Prevent ZMQ bind
    pool.shared_publisher.bind = MagicMock(return_value=5555)
    pool._create_adapter = lambda: adapter_cls.return_value
    return pool, adapter_cls


class TestConnectionPoolInitialize:

    def test_zerodha_success_shape_initializes_pool(self):
        """ConnectionPool must treat {"status": "success"} as success.

        This is the regression case from #76: the old predicate
        'not result.get("success")' treated a missing "success" key as True,
        misclassifying Zerodha's {"status": "success"} as failure.
        """
        pool, adapter_cls = _make_pool()
        adapter_cls.return_value.initialize.return_value = {"status": "success"}

        result = pool.initialize("zerodha", "testuser")

        assert result.get("success") is True, (
            "Zerodha's {'status': 'success'} must be treated as success"
        )
        assert pool.initialized is True

    def test_explicit_success_false_is_failure(self):
        pool, adapter_cls = _make_pool()
        adapter_cls.return_value.initialize.return_value = {
            "success": False, "error": "bad token"
        }
        result = pool.initialize("zerodha", "testuser")
        assert result.get("success") is False
        assert pool.initialized is False

    def test_status_error_is_failure(self):
        pool, adapter_cls = _make_pool()
        adapter_cls.return_value.initialize.return_value = {
            "status": "error", "message": "auth failed"
        }
        result = pool.initialize("zerodha", "testuser")
        assert result.get("success") is False
        assert pool.initialized is False
```

The simpler variant — a pure predicate test — was what the fix commit actually
added as `test/test_connection_manager_predicate.py`. That would also have caught it:

```python
def _check_is_error(result):
    # the OLD predicate:
    return (result and not result.get("success")) or (
        result and result.get("status") == "error"
    )

def test_zerodha_success_shape_not_error():
    result = {"status": "success"}
    assert not _check_is_error(result)  # ← FAILS with old predicate
```

**Why wasn't the predicate tested?** When `ConnectionPool` was first added
(upstream commit `85fefb97d`, "Websocket Pooling"), no test was written for its
init path. The class was added to the production code path (`broker_factory.py`)
without a corresponding test that exercised its response-format translation.

---

## F — Test Gap Category

**Untested integration-glue predicate with an undocumented third format.**

The `ConnectionPool` is a **thin wrapper** that sits between the WS proxy server
and the broker adapters. It is exactly the kind of component that is easy to test
in isolation — but wasn't. Specifically:

1. `ConnectionPool.initialize()` contains a **format-translation predicate** that
   decides whether the adapter's return value signals success or failure. The two
   documented formats in the comment were `{"success": False}` (pool format) and
   `{"status": "error"}` (adapter error format). The **actual success format**
   `{"status": "success"}` (what Zerodha always returns) was never listed and
   never tested.

2. Every existing WS-proxy test (`test_broker_session_auto_reconnect.py`) injects
   adapters **directly** into `proxy.broker_adapters`, bypassing `ConnectionPool`
   entirely. The production path in `broker_factory.py` goes through
   `ConnectionPool`, but no test does.

3. The predicate's correctness was only discoverable at runtime with a real Zerodha
   session — precisely the environment CI doesn't have.

**Sub-category:** Two-format contract without a contract test. The comment correctly
documented the two *failure* formats but omitted the *success* format, and the
implicit assumption (`not result.get("success")` = success-flag absent = failure)
was never verified against the real broker shape.

---

## G — Recommendations

### P0 — The fix's regression guard is now in place

`test/test_connection_manager_predicate.py` (6 cases, added by `bad06f612`) is the
regression guard. It directly tests the predicate logic for all relevant response
shapes including the critical Zerodha case (`{"status": "success"}`). This test
runs in CI (`ci-unit-tests` job) on every PR and push to dev/main. **No further
action needed here.** Do not remove or weaken these 6 cases.

### P1 — Add a `ConnectionPool` unit test suite

Create `test/test_connection_pool.py` with direct unit tests for:

- `initialize()` with Zerodha success shape → pool initialized, returns `{"success": True}`
- `initialize()` with explicit `{"success": False}` → pool not initialized
- `initialize()` with `{"status": "error"}` → pool not initialized
- `connect()` with `{"status": "success"}` → pool connected
- `connect()` with `{"status": "error"}` → returns failure dict
- `_attempt_auth_recovery()` predicate (lines 522–524 — verify it uses `is False`, not `not`)

This test suite would have caught the bug at the `ConnectionPool` level, not just
the predicate level.

### P1 — Add a WS proxy health check to Tier 3

In `broker_happy_path.spec.ts`, after the `/_test/mock_auth` step and before the
funds assertion, add:

```typescript
// Verify WS proxy is alive (not stuck in retry loop)
const wsStatus = await page.request.get(`${BASE_URL}/websocket/status`)
// or connect to port 8765 with a short timeout
```

If a probe endpoint exists (e.g. `GET /websocket/status`), assert it returns 200.
If port 8765 is checked directly, connect with a short timeout — a stuck proxy
would NOT accept connections even if port 8765 shows as "listening."

### P2 — Audit all test mocks for format inconsistency

Run this:

```bash
grep -rn '"success": True\|"success": False\|{"success"' test/ | grep -v "__pycache__"
```

Check each hit: does the mock return `{"success": True}` where the real broker
returns `{"status": "success"}`? The `FakeAdapter` in
`test_broker_session_auto_reconnect.py` already uses the correct Zerodha shape
(`{"status": "success"}`). But other mocks may not.

Also check: does any code downstream of the mock expect `{"success": True}` when
the real broker returns `{"status": "success"}`? This is the inverse drift.

### P3 — Create a real-shape fixture set

Add `test/fixtures/broker_response_shapes.py`:

```python
# Zerodha adapter response shapes — verified against zerodha_adapter.py
ZERODHA_SHAPES = {
    "initialize_success": {"status": "success", "message": "Adapter initialized successfully"},
    "initialize_error":   {"status": "error",   "message": "<reason>"},
    "connect_success":    {"status": "success", "message": "Connected successfully"},
    "connect_error":      {"status": "error",   "message": "<reason>"},
    "subscribe_success":  {"status": "success", "message": "<symbol>.NSE"},
    "subscribe_error":    {"status": "error",   "code": "<CODE>", "message": "<reason>"},
    # Pool-internal format (only used when ConnectionPool itself generates the result):
    "pool_success":       {"success": True,  "message": "<msg>"},
    "pool_failure":       {"success": False, "error":   "<reason>"},
}
```

Reference this fixture in `test_connection_pool.py` and
`test_connection_manager_predicate.py`. When upstream changes adapter response
shapes, a failing test immediately flags the drift.

---

## Summary for Dheeraj

The five CI tiers collectively covered: page loads (T1), backfill scheduler boot
(T2), REST API happy-path through mock broker (T3), frontend dist build (T4), and
a Vite config change (T5). None covered the `ConnectionPool` initialization
predicate because:

- `ConnectionPool` is a middle-tier component introduced upstream without tests
- All WS-proxy tests inject adapters **below** the pool layer
- The Tier 3 E2E is the closest — it triggers the code path — but asserts only
  on the REST API response, not WS proxy state

The fix adds `test/test_connection_manager_predicate.py` (P0, already in dev).
The next step for Dheeraj: open a follow-up issue to add `test/test_connection_pool.py`
(P1) so the full `ConnectionPool.initialize()` method is covered end-to-end, not
just the extracted predicate.
