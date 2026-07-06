# Root-cause analysis: 261 DuckDB lock errors + 2160 indicator warmup warnings per day

**No more iterative patches.** This is the evidence-first analysis the past six PRs should have done. Two related-but-distinct problem classes are diagnosed together because they share a single architectural fix.

---

## Problem class A — historify.duckdb lock errors

### Evidence (last 7 days)

**261 historify.duckdb lock errors** despite six attempted fixes:

| PR | Approach | What it actually changed | Why it didn't close it |
|---|---|---|---|
| #92 (#85) merged 06-22 | `_db_write_lock` on **one** `duckdb.connect()` call | One call site protected | 16+ other connect sites bypass |
| #109/#118 (#110) merged 06-23/24 | `connect_historify_readonly` fallback | Catches `ConnectionException("different configuration")` only, on **read** paths only | `IOException`, `BinderException`, and **all write** paths still raw |
| #125 (#124) merged 06-24 | `@_synchronized_write` RLock on 16 writes | Serialises **writes vs writes** in-process | Does NOT serialise **reads vs writes** — the actual config-mismatch source |
| #142 (#126) merged 06-26 | Broadened fallback to 3 exception classes | Read path tolerates IO + Binder | Architecturally identical — still per-op connect/close, still mixed configs |
| #143 (#139) merged 06-26 | Boot orphan-PID guard | Catches cross-process holder at startup | Only at boot; runtime contention untouched |
| #144 (#140) merged 06-26 | Boot scheduler serialisation | Lock between scheduler boot workers | Held lock for ~1.5s while downloads ran for ~25s (design gap #151) |
| #152 (#151) merged 06-26 | `wait_for_jobs` inside the lock | Designed correctly | Received empty job_ids — backfill helpers don't propagate (#154/#155) |
| #155 (#154) open | Propagate `job_id` | Makes #152 actually wait | **Even if merged, leaves 39 unsynchronised reads vs 18 writes — they will still collide on config** |

Top error file:line distribution over the period:

```
43 historify_db.py:112     get_connection retry exhaustion (write path)
28 historify_db.py:84      inside get_connection retry loop
25 historify_service.py:375 download_data wrapper
20 historify_db.py:2174    update_job_item_status (write)
18 historify_db.py:920     get_ohlcv (read)
15 historify_db.py:2134    update_job_progress (write)
14 historify_db.py:702     upsert_market_data (write)
14 historify_db.py:2044    write
13 historify_db.py:83      inside get_connection retry loop
```

**Write-path errors dominate (>120 of the top 161).** Every prior PR focused on the read fallback while the write path has zero fallback.

### Connection-site inventory (today, post-#152)

**17 direct `duckdb.connect()` call sites** in production code. The two wrappers:

| Wrapper | Location | Config | Lifetime | Threading |
|---|---|---|---|---|
| `get_connection()` | `database/historify_db.py:78-115` | default (writeable) | **Per-call open+close**, 3x retry | Any: Flask, APScheduler, ThreadPoolExecutor, boot |
| `connect_historify_readonly()` | `services/data_freshness_service.py:107-192` | `read_only=True` first, fallback default | **Per-call open+close**, fallback 3x retry | Same |

Plus `boot_db_probe.assert_historify_unlocked()` (one-shot at boot), 2 migration scripts, ~10 CLI/test scripts.

**Dozens of threads in a single process opening + closing connections to the same file with mixed configs concurrently.**

### Architectural root cause

DuckDB's documented constraint: within one process, the file's instance cache requires **one configuration**. Once any thread holds an open connection with `read_only=True`, any other thread that calls `duckdb.connect(path)` (default config = writeable) gets `ConnectionException("Can't open a connection to same database file with a different configuration than existing connections")`.

OpenAlgo violates this constraint every minute:
- 39 read functions call `get_connection()` (default config) but ALSO `connect_historify_readonly()` (read_only=True) is in flight from the freshness service / scanner
- 18 write functions open default config
- 5-worker `ThreadPoolExecutor` in `historify_service` runs concurrent writes from boot backfill
- `@_synchronized_write` serialises writes vs writes but does **nothing** for reads vs writes
- The retry loop in `get_connection()` just retries the same conflicting config, hence the 43+ "Failed to connect to DuckDB after 3 attempts"

Every prior PR patched a symptom of this pattern. **None replaced the pattern.**

---

## Problem class B — indicator warmup warnings (2160 per session)

### Evidence

After the 12:00:25 restart, the 12:10-12:20 window logged **2160 `verify_series` warnings** from `pandas_ta_classic`:

```
864 × Series has 3 rows but indicator requires at least 14   (RSI 14)
864 × Series has 2 rows but indicator requires at least 14   (RSI 14)
216 × Series has 3 rows but indicator requires at least 20   (SMA 20)
216 × Series has 2 rows but indicator requires at least 20   (SMA 20)
```

That is ~200 warnings per minute for ~10 minutes after every restart.

### Cause

`services/scan_rules/fno_intraday_*_chartink.py` call `sma(bars_5m, 20)`, `rsi(bars_5m, 14)`, `atr(...)`. `bars_5m` comes from the in-memory `MultiIntervalAggregator` which is **empty at restart** and fills one bar every 5 minutes from the live tick feed. RSI(14) needs 14 bars (~70 minutes of trading), SMA(20) needs 20 bars (~100 minutes). For the first ~100 minutes after every restart, the scanner runs rules on a too-short series and `pandas_ta_classic.utils._core.verify_series` logs a WARNING and returns None.

The scanner has full 5m history available in `historify.duckdb` but does not seed the aggregator from it at boot.

### Why this matters now

1. **2160 warnings/restart is noise** — drowns real WARN/ERROR signal.
2. **It's the symptom of a real bug** — for the first ~100 minutes after restart, the scanner produces no signals because every rule returns None. This is the same class of issue PR #147 (subscribe-aware tripwire) partially papered over for the scanner_dry tripwire.
3. **Seeding the aggregator from historify will add MORE reads to historify.duckdb at boot** — which would fail today against the existing connection pattern. So problem B's fix depends on problem A's fix.

---

## The single structural fix (covers both classes)

**Replace the per-operation `duckdb.connect()` pattern with one shared, long-lived, writeable connection per process. All reads and writes use cursors derived from it.**

DuckDB's Python API supports this directly. A single `DuckDBPyConnection` is opened once at module import; `conn.cursor()` returns a new cursor sharing the same underlying database. Cursors are safe for concurrent SELECT. Writes through cursors are serialised internally by DuckDB. **There is no config mismatch because there is only one config in the process.**

### Implementation outline

1. `database/historify_db.py` gains a module-level `_shared_conn = duckdb.connect(get_db_path())` (lazy, thread-safe init via lock).
2. `get_connection()` returns a context-manager wrapping `_shared_conn.cursor()`. Cursors are cheap; closing a cursor does not close the underlying DB.
3. `connect_historify_readonly()` becomes a thin alias for `_shared_conn.cursor()` — no more `read_only=True` opens, no more fallback retry, no more "different configuration" errors possible.
4. Keep `@_synchronized_write` for now as defence-in-depth (DuckDB serialises writes internally, but explicit lock makes write ordering observable in logs).
5. Test conftest resets the singleton between tests (the existing `_isolate_databases` fixture already redirects `HISTORIFY_DATABASE_PATH` to a tmpdir, so per-process singleton is naturally per-test-process; just expose a `_reset_for_tests()` helper).
6. Migration scripts use their own short-lived connections (they run pre-app, before the singleton would exist).
7. `boot_db_probe` is **kept** — still the correct cross-process orphan guard.
8. **Problem B fix becomes feasible**: add a startup hook that seeds the `MultiIntervalAggregator` for every scanner symbol from `historify.duckdb` (last ~100 5m bars per symbol). This is now safe because reads go through the singleton's cursor pool, not new connections.

### What disappears

- `read_only=True` opens in production code
- The `connect_historify_readonly` fallback ladder
- ALL `"different configuration"` errors (mathematically impossible with one config)
- ALL `Failed to connect to DuckDB after 3 attempts` errors from `get_connection` (the retry was working around the config mismatch — it's not a real lock)
- The 5-worker thrash → workers share the singleton's cursor pool
- 2160 indicator warmup warnings per restart (aggregator pre-seeded → indicators have history)

### What remains

- Cross-process file locks (caught by `boot_db_probe` at boot; a separate runtime probe is a follow-up if needed)
- DuckDB's internal write serialisation (already there, no change)
- The `@_synchronized_write` RLock (kept for log observability)

---

## Why I'm confident this time

- **Empirical root cause**: 261 errors over a week, file:line distribution shows write paths dominate, error message *names* the config-mismatch issue explicitly.
- **Architectural fix, not a patch**: the connect+close-with-mixed-configs pattern is the cause; replacing it with a singleton makes the error mathematically unreachable.
- **Documented DuckDB pattern**: this is the standard recommendation for multi-threaded Python access to a DuckDB file.
- **Closes every symptom class at once**: `different configuration`, `Failed to connect after 3 attempts`, in-process `IO Error: being used by another process`, AND the 2160 indicator warmup warnings.
- **Unblocks Problem B**: the aggregator-seeding fix needs many concurrent reads at boot — feasible only after this.

## Tests

- `test/test_historify_singleton.py` (new): single-connection invariant, 50 concurrent reader threads, 10 concurrent writer threads, no exceptions raised.
- `test/test_historify_singleton_isolation.py`: confirms test isolation (each pytest worker gets its own singleton against its own tmpdir DB).
- Regression: full pytest suite must remain green.
- Stress test: 100 cursors × 1000 ops, mixed read/write — no `"different configuration"` errors.

## Acceptance

After this PR ships + a restart at any subsequent time:

| Metric | Pre-fix (today) | Post-fix expected |
|---|---|---|
| `Can't open a connection to same database file with a different configuration` per day | ~30–50 | **0** |
| `Failed to connect to DuckDB after 3 attempts` per day | ~20–40 | **0** |
| `IO Error: ... being used by another process` (in-process subset) per day | ~5–10 | **0** |
| `pandas_ta_classic verify_series` warnings per restart | ~2160 | **0** (after the aggregator-seeding hook, problem B) |

Measured over a full 24h cycle.

## Out of scope (separate issues if needed)

- Cross-process file locks (orphan python.exe holders) — already handled by #143's `boot_db_probe` at boot. A runtime probe is a separate, smaller follow-up if needed.
- DuckDB internal performance — singleton is at minimum equal to per-op connect+close, likely faster.
- Migration scripts — they run pre-app and use their own connections; no change needed.

## Supersedes

- **Issue #151** (boot_convergence wait gap) — vanishes with the singleton; the lock is no longer load-bearing.
- **Issue #154** (job_id propagation) — vanishes with the singleton; the wait_for_jobs path becomes obsolete.
- **PR #155 (open)** — keep it as defence-in-depth (proper job_id propagation is correct hygiene), but the errors it was supposed to fix will disappear regardless.
- The implicit architectural intent behind PRs #110, #124, #126, #140 — all subsumed.
