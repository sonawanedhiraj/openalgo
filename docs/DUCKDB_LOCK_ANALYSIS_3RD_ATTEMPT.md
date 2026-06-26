# DuckDB Lock Contention — 3rd Attempt Root Cause Analysis

**Date:** 2026-06-24 09:30 IST
**Status:** Errors STILL occurring after PR #118 and PR #125
**Author:** Claude Code forensic analysis

## Evidence

### Process inventory (PowerShell)
- PID 19992: `python.exe app.py` (276MB) — started 09:15:01 IST — **the live OpenAlgo, has PR #125's fix**
- PID 36784: `python.exe app.py` (4MB) — launcher process for PID 19992
- PID 20296: `python.exe bridge/server.py` (0MB) — started 08:16:49 — bridge (no DuckDB access)
- PID 10956: `python.exe bridge/server.py` (2MB) — bridge (no DuckDB access)

### Errors continuing after PR #125 (merged 09:05 IST, OpenAlgo restarted 09:15)
19 DuckDB errors recorded between 09:15:34 and 09:15:55 IST — i.e. **after** the running process picked up the fix.

### Three distinct error categories in log/errors.jsonl

| # | Error class | Message | Occurrences (last 500 lines) |
|---|-------------|---------|------------------------------|
| A | `_duckdb.IOException` | `IO Error: Cannot open file ... being used by another process` | 40 |
| B | `_duckdb.ConnectionException` | `Can't open a connection ... different configuration` | 81 |
| C | `_duckdb.BinderException` | `Unique file handle conflict: Cannot attach "historify"` | 3 |

Failing call sites (after current OpenAlgo restart):
- `historify_db.py:920` — `get_ohlcv()` (read)
- `historify_db.py:112` — `get_connection()` (write path setup)
- `historify_db.py:1193` — `_get_daily_aggregated_ohlcv()` (read)
- `data_freshness_service.py:128` — `connect_historify_readonly()` (the PR #118 fallback itself)

## What PR #118 fixed (and missed)

`services/data_freshness_service.py:107-136`:

```python
def connect_historify_readonly(duckdb_path: str):
    import duckdb
    try:
        return duckdb.connect(duckdb_path, read_only=True)
    except duckdb.ConnectionException as e:           # <-- ONLY ConnectionException
        if "different configuration" not in str(e):    # <-- ONLY this message
            raise
        logger.info(...)
        return duckdb.connect(duckdb_path)             # fallback to default config
```

**The gap:** PR #118's fallback ONLY catches `duckdb.ConnectionException` with the text "different configuration".

It does **NOT** fall back when:
- `_duckdb.IOException` is raised ("being used by another process") — Category A errors
- `_duckdb.BinderException` is raised ("Unique file handle conflict") — Category C errors

Ironically, `is_transient_lock_error()` in the **same file** (line 90) lists ALL FOUR patterns the project considers transient — but the connect helper only recovers from one of them.

## What PR #125 fixed (and missed)

PR #125 added `@_synchronized_write` decorator (using `threading.RLock`) to all 16 write functions in `historify_db.py`.

**What it correctly fixes:** Concurrent WRITES from multiple threads inside the SAME Python process (ThreadPoolExecutor workers + Flask request threads racing to call `update_job_*`/`upsert_*`).

**What it does NOT fix:**
1. **READ paths** — the lock is only on write functions. `get_ohlcv()` is a read function and is not decorated.
2. **CROSS-PROCESS contention** — `threading.RLock` is process-local. Two Python processes cannot coordinate via this lock. (Bridges don't touch DuckDB, so this is moot for now, but it's a structural limitation.)
3. **Connection-open contention** — the lock serializes the `func()` call, but the contention occurs inside `duckdb.connect()` itself. If `get_connection()` opens a fresh connection while another thread's open is mid-flight, Windows can still surface IOException at the OS file-open layer before our lock matters. (Less likely with RLock serialization, but possible because connection open is brief.)

## Why errors persist after BOTH fixes

The errors at 09:15:34–09:15:55 IST trace back to:

1. **READ path** `get_ohlcv()` line 914 → `connect_historify_readonly()` line 128 → tries `read_only=True` → raises `IOException` ("being used by another process") → **PR #118's except clause does not catch IOException** → re-raises → top-level handler logs `Error fetching OHLCV data: IO Error`.

2. **WRITE path** `update_job_progress()` line 2167 (decorated) → acquires `_write_lock` → calls `get_connection()` line 112 → `duckdb.connect(db_path)` line 76 → raises `ConnectionException` ("different configuration") because **another read on a separate thread is currently holding `read_only=True`** (from the PR #118 fallback's first attempt). This is the reverse direction: a read holds read-only, so writes can't open default config.

3. **BACKFILL stale-check** `scanner_universe_backfill.py:243` → `compute_stale_symbols()` → `connect_historify_readonly()` → on retry path uses `ATTACH ... AS historify` SQL → raises `BinderException` ("already attached") → **PR #118 does not handle this either**.

In short: the two fixes are correct as far as they go, but they fix only one slice each of the lock problem. **The connection-open helper is the linchpin — until it catches all three exception classes and centralizes connection management, the read/write paths will keep racing.**

## Recommended fix (3rd attempt)

**One change, two layers:**

### Layer 1 — Broaden `connect_historify_readonly()` to catch all transient lock errors

Replace the narrow `except duckdb.ConnectionException + "different configuration"` check with the `is_transient_lock_error()` helper that already exists in the same file:

```python
def connect_historify_readonly(duckdb_path: str):
    import duckdb
    try:
        return duckdb.connect(duckdb_path, read_only=True)
    except (duckdb.ConnectionException, duckdb.IOException, duckdb.BinderException) as e:
        if not is_transient_lock_error(e):
            raise
        logger.info(
            "historify open conflict (%s); reusing shared connection for read-only query",
            type(e).__name__,
        )
        return duckdb.connect(duckdb_path)
```

### Layer 2 — Add retry-with-backoff inside the fallback

The fallback `duckdb.connect(duckdb_path)` can ALSO fail under heavy contention. Wrap it in a 2-3 attempt retry with short sleep so the next write/read window opens:

```python
def connect_historify_readonly(duckdb_path: str, max_retries: int = 3):
    import duckdb, time
    last_err = None
    for attempt in range(max_retries):
        try:
            return duckdb.connect(duckdb_path, read_only=True)
        except (duckdb.ConnectionException, duckdb.IOException, duckdb.BinderException) as e:
            if not is_transient_lock_error(e):
                raise
            last_err = e
            # Fall back to shared instance
            try:
                logger.info("historify open conflict (%s); reusing shared connection", type(e).__name__)
                return duckdb.connect(duckdb_path)
            except (duckdb.ConnectionException, duckdb.IOException, duckdb.BinderException) as e2:
                last_err = e2
                if attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))  # 100ms, 200ms, 300ms
    raise last_err
```

### Why this is enough (and not over-engineered)

- **NO restart needed** — the fix is in the helper, picked up on next OpenAlgo restart (which the user controls).
- **NO new locks** — RLock from PR #125 stays.
- **NO multi-process coordination** — not needed because bridges don't touch DuckDB; only one OpenAlgo process at a time owns the file.
- **NO behavior change for the happy path** — `read_only=True` still tried first.
- **Backward compatible** — same function signature, same return type.

## Why NOT do these (rejected alternatives)

- **Single global connection** — DuckDB is not thread-safe with one shared connection across threads for writes; would require a queue.
- **PostgreSQL migration** — out of scope; would solve everything but is a multi-week project.
- **Process-level file lock (msvcrt.locking)** — not needed; the bridges don't touch DuckDB. Cross-process is a hypothetical, not the actual bug.
- **Connection pool** — adds complexity; we tried this before (the 81 "different config" errors are partly because connection pooling fights DuckDB's single-instance model).

## Test plan

1. Apply fix to `services/data_freshness_service.py`.
2. Add unit test: simulate `IOException` on first attempt → verify fallback catches it.
3. Add unit test: simulate `BinderException` on first attempt → verify fallback catches it.
4. Add unit test: simulate persistent failure → verify retries happen 3× then raises.
5. Manual: restart OpenAlgo (operator's call, NOT during market hours), grep errors.jsonl for the next 24 hours. Expect: zero `Error fetching OHLCV data: IO Error` and zero `Failed to connect to DuckDB after 3 attempts`.
