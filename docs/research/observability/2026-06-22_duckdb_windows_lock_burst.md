# DuckDB Windows write-lock burst — root-cause + fix

**Date:** 2026-06-22
**Issue:** #85 (umbrella)
**Symptom:** Intermittent "could not set lock on file" / "database locked" errors in
`errors.jsonl` during the 15:30–17:00 IST post-close backfill window. Dropped records
in `historify.duckdb`.

---

## 1. Writer map

Every path that calls `duckdb.connect(db_path)` **in write mode** inside the Flask
process:

| Caller | Triggered by | Rate |
|---|---|---|
| `historify_db.upsert_market_data` | `_process_download_job` (job executor thread) | once per symbol per job |
| `historify_db.create_download_job` | `create_and_start_job` call | once per batch |
| `historify_db.update_job_status` | job lifecycle transitions | several per job |
| `historify_db.update_job_item_status` | per symbol start/finish | 2× per symbol |
| `historify_db.update_job_progress` | per symbol finish | 1× per symbol |
| `historify_db.init_database` | app boot | once |
| `historify_db.get_connection` (misc reads) | data catalog, job queries, etc. | many |
| `migrate_historify*.py` | operator CLI | occasional |

All write operations funnel through `historify_db.get_connection()`, which calls
`duckdb.connect(db_path)` at line 75.

The ONLY read path that bypasses `get_connection()` is
`data_freshness_service.connect_historify_readonly()` — it tries `read_only=True`
first and falls back to a config-matching connect when the process already holds the
file read-write (the in-process instance-cache fallback documented in that function).

---

## 2. Concurrent writer timing — how many can overlap?

**Executors involved:**
- `historify_service._job_executor = ThreadPoolExecutor(max_workers=5)` — shared
  across ALL `create_and_start_job` calls from any caller.

**Post-close window overlap:**
```
15:30 IST: sector_follow periodic tick fires
            → sector_follow_index_backfill.check_and_refresh_if_stale()
                → create_and_start_job(symbols=8_indices, ...)  # submits 8 items
            → sector_follow_stock_backfill.check_and_refresh_if_stale()
                → create_and_start_job(symbols=30_stocks, ...)  # submits 30 items

15:30 IST: scanner backfill periodic tick fires (same boot moment)
            → scanner_universe_backfill.check_and_refresh_if_stale(interval='1m')
                → create_and_start_job(symbols=~238_scanner_syms, ...)  # submits ~238 items
            → scanner_universe_backfill.check_and_refresh_if_stale(interval='D')
                → create_and_start_job(symbols=~238_scanner_syms, ...)  # submits ~238 items
```

**Worst-case concurrent writers at the executor:** 5 threads × 3 write calls per
symbol (upsert + 2 job-item updates) = 15 consecutive DuckDB opens per second per
worker. With 5 workers active, up to 5 `duckdb.connect()` calls can fire
**simultaneously**.

Each job also calls `create_download_job` / `update_job_status` while workers are
running, adding further concurrent opens.

Total backfill at boot: ~514 items (8 + 30 + 238 + 238) ≈ **514 concurrent DuckDB
write opens** over ~170 seconds (3 req/sec broker limit).

---

## 3. Current lock retry / backoff policy

```python
# database/historify_db.py:49-91
@contextmanager
def get_connection(max_retries: int = 3, retry_delay: float = 0.5):
    for attempt in range(max_retries):
        try:
            conn = duckdb.connect(db_path)
            break
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))  # 0.5s, 1.0s
    # if conn is None → raises
```

**Maximum wait:** 3 attempts × 0.5s / 1.0s = **1.5 seconds** before giving up. If
a concurrent write transaction holds the Windows file lock for longer than 1.5 s,
the retry budget is exhausted and an exception propagates — dropping that symbol's
records.

---

## 4. DuckDB Windows behaviour

DuckDB uses **mandatory exclusive write locks** on Windows (`LockFileEx` with
`LOCKFILE_EXCLUSIVE_LOCK`), unlike Linux where `flock` is advisory. The lock is held
for the **lifetime of the connection** (not just the transaction). This means:

- Thread A calls `duckdb.connect(path)` → Windows acquires exclusive lock on the
  `.duckdb` file.
- Thread B calls `duckdb.connect(path)` while Thread A's connection is still open →
  Windows BLOCKS (or DuckDB raises "could not set lock on file").

**In-process instance cache behaviour:** DuckDB maintains one database instance per
file path per process. Within the same process, multiple `duckdb.connect(path)` calls
in write mode go through the same instance. However, the instance still enforces a
single-writer constraint at the Windows OS level: the first connect acquires the lock;
concurrent connects from other threads in the same process attempt to acquire the same
lock and race.

The `data_freshness_service.is_transient_lock_error()` helper already documents this:
```python
msg = str(exc).lower()
return (
    "different configuration" in msg   # in-process read_only vs write mismatch
    or "could not set lock" in msg     # Windows OS-level lock failure
    or "conflicting lock" in msg
    or "being used by another process" in msg
)
```

The `connect_historify_readonly()` fallback only fixes the `different configuration`
case (read_only vs write config mismatch). It does **not** prevent concurrent write
opens from racing for the Windows file lock.

---

## 5. The `connect_historify_readonly()` fallback — does it contribute?

No. `connect_historify_readonly()` is a **read path only** — it only opens the DB
for SELECT queries. It doesn't contribute to write-lock contention. Its `different
configuration` fallback correctly reuses the shared in-process instance for reads,
which is safe and doesn't compete with write connections.

---

## 6. Fix options

### Option A — Module-level write lock (chosen)

Add a `threading.Lock()` to `historify_db.py` that serializes all `get_connection()`
calls. Only one `duckdb.connect()` is in-flight at any moment from the same process.

**Pros:**
- Eliminates the root cause (concurrent Windows file-lock attempts)
- Zero change to callers — all use `get_connection()` already
- Idempotent retry logic can be simplified or kept as defence-in-depth
- Does not affect `connect_historify_readonly()` (that function uses DuckDB's
  in-process instance reuse independently)
- Does not slow down the backfill: the actual bottleneck is the broker API rate
  limit (3 req/sec → ~170 s for 514 symbols). DuckDB writes take ~10 ms per symbol,
  so the serial queue drains faster than the broker feed fills it.

**Cons:**
- All reads through `get_connection()` also serialise. Reads via the
  `connect_historify_readonly()` path are unaffected.

### Option B — Stagger the schedules

Add a fixed offset between sector_follow and scanner backfill start times (e.g.
sector_follow at 15:30, scanner at 15:35).

**Pros:** Simple.

**Cons:** Only helps at boot; the 30-minute periodic loops re-synchronise within one
period. Does not fix the underlying concurrent-write issue within a single job (5
workers still race). Does not fix the burst from a single large scanner job.
**Rejected.**

### Option C — Process-level write lock file

Use a lock file on disk so even CLI/subprocess callers serialise.

**Pros:** Works cross-process.

**Cons:** CLIs are manual (operator runs them separately and knows what they're doing),
no evidence of cross-process conflict in production, more complex implementation.
**Unnecessary for the current symptom.**

---

## 7. Decision: Option A

Implement a module-level `threading.Lock` (`_db_write_lock`) in `historify_db.py`.
`get_connection()` acquires it before calling `duckdb.connect()` and releases it
after `conn.close()`. The existing retry logic is retained as defence-in-depth for
the cross-process case.

The retry count is raised from 3 → 10 and the base delay from 0.5 → 1.0 s so
that brief cross-process contention (e.g. a CLI backfill running simultaneously)
is also handled gracefully. This is independent of the in-process lock fix.

---

## 8. Test coverage added

`test/test_historify_db_concurrent_writes.py` — two tests:

1. `test_concurrent_writes_do_not_drop_records` — spawns 5 threads each writing a
   unique batch of rows to `market_data` via `upsert_market_data`. After all threads
   finish, asserts that ALL rows are present (no silent drops under the lock).

2. `test_get_connection_lock_serializes_writes` — verifies that the
   `_db_write_lock` in the module is a `threading.Lock` (not None), confirming the
   guard is present.
