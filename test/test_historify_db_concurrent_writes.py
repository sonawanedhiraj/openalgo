"""Tests for the Windows write-lock serialisation fix in ``database.historify_db``.

Context
-------
On Windows, DuckDB holds a mandatory exclusive write lock for the lifetime of
each connection. With ``historify_service._job_executor`` using up to 5 worker
threads, and both the sector_follow (38 symbols) and scanner (238+ symbols)
backfill schedulers feeding it simultaneously, up to 5 concurrent
``duckdb.connect()`` calls race for the OS lock — producing "could not set lock"
bursts and dropped records.

The fix adds ``_db_write_lock`` (``threading.Lock``) to ``historify_db`` and
wraps the ``duckdb.connect()`` call inside ``get_connection()`` with it, so only
one connection is in flight at a time from this process.

Tests
-----
* ``test_lock_is_present`` — guard rail: ``_db_write_lock`` exists and is a Lock.
* ``test_concurrent_writes_do_not_drop_records`` — 5 threads each upsert a
  disjoint set of 20 rows; after all finish, every row is present (no silent
  drop under concurrent access).
"""

from __future__ import annotations

import threading

import pandas as pd
import pytest

import database.historify_db as hdb


def _reset_db() -> None:
    """Drop relevant tables and re-init so each test starts clean."""
    with hdb.get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS market_data")
        conn.execute("DROP TABLE IF EXISTS data_catalog")
    hdb.init_database()


# ---------------------------------------------------------------------------
# Guard-rail: the lock must exist
# ---------------------------------------------------------------------------


def test_lock_is_present():
    """``_db_write_lock`` must be a threading.Lock so the guard is in place."""
    assert hasattr(hdb, "_db_write_lock"), "_db_write_lock missing from historify_db"
    # threading.Lock() returns a _thread.lock; the public ABC is threading.Lock
    # but isinstance checks against the concrete type work reliably.
    assert isinstance(hdb._db_write_lock, type(threading.Lock())), (
        "_db_write_lock is not a threading.Lock"
    )


# ---------------------------------------------------------------------------
# Concurrent-write correctness
# ---------------------------------------------------------------------------


def _make_symbol_df(symbol: str, n_rows: int = 20) -> pd.DataFrame:
    """Return a minimal DataFrame for ``upsert_market_data``."""
    base_ts = 1_700_000_000  # arbitrary past epoch
    return pd.DataFrame(
        {
            "timestamp": [base_ts + i * 60 for i in range(n_rows)],
            "open": [100.0] * n_rows,
            "high": [101.0] * n_rows,
            "low": [99.0] * n_rows,
            "close": [100.5] * n_rows,
            "volume": [1000] * n_rows,
            "oi": [0] * n_rows,
        }
    )


def test_concurrent_writes_do_not_drop_records():
    """5 threads writing disjoint symbol batches must all land without drops."""
    _reset_db()

    n_threads = 5
    rows_per_thread = 20
    exchange = "NSE"
    interval = "1m"

    errors: list[Exception] = []
    symbols = [f"TESTSYM{i}" for i in range(n_threads)]

    def _worker(sym: str) -> None:
        try:
            df = _make_symbol_df(sym, rows_per_thread)
            hdb.upsert_market_data(df, sym, exchange, interval)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(s,)) for s in symbols]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"Worker threads raised: {errors}"

    # Verify all rows landed
    with hdb.get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM market_data WHERE exchange = ? AND interval = ?",
            [exchange, interval],
        ).fetchone()[0]

    expected = n_threads * rows_per_thread
    assert total == expected, (
        f"Expected {expected} rows after concurrent writes, got {total} "
        "(some writes were dropped — lock not working)"
    )
