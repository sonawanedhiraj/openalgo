"""Regression tests for the per-process DuckDB singleton (issue #191 / #156 Phase 1).

Each test would have FAILED against the pre-#191 ``get_connection`` that opened
a brand-new ``duckdb.connect(db_path)`` on every call. They pass with the
singleton because every cursor in the process shares one underlying database
with one configuration, so the ``ConnectionException`` / ``IOException`` class
that bursts at boot becomes mathematically impossible to reproduce.

The global ``test/conftest.py`` ``_isolate_databases`` fixture has already
redirected ``HISTORIFY_DATABASE_PATH`` to a per-pytest-process tmpdir before
this module imports, so every test here writes to a throwaway DB.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest


@pytest.fixture(autouse=True)
def _reset_singleton_each_test():
    """Drop the singleton before AND after every test so they cannot leak
    cursors / connection state into each other."""
    from database.historify_db import _reset_for_tests

    _reset_for_tests()
    yield
    _reset_for_tests()


def _ensure_market_data_schema() -> None:
    """Create the minimal ``market_data`` table the stress tests write into."""
    from database.historify_db import get_connection

    with get_connection() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS market_data (
                symbol VARCHAR NOT NULL,
                exchange VARCHAR NOT NULL,
                interval VARCHAR NOT NULL,
                timestamp BIGINT NOT NULL,
                open DOUBLE NOT NULL,
                high DOUBLE NOT NULL,
                low DOUBLE NOT NULL,
                close DOUBLE NOT NULL,
                volume BIGINT NOT NULL,
                PRIMARY KEY (symbol, exchange, interval, timestamp)
            )
            """
        )


# --------------------------------------------------------------------------- #
# Singleton identity / lifecycle invariants
# --------------------------------------------------------------------------- #


def test_singleton_returns_same_instance_across_calls():
    """Two calls from the same thread return the *same* underlying connection."""
    from database.historify_db import _get_shared_conn

    a = _get_shared_conn()
    b = _get_shared_conn()
    assert a is b, "expected the same shared connection object on repeat calls"


def test_singleton_double_checked_locking_returns_one_instance_under_thread_race():
    """50 threads racing on first init MUST all get the same instance.

    A broken impl (no lock, or single-checked) would create one
    DuckDBPyConnection per thread, leaking handles and re-introducing the
    config-mismatch race this fix was meant to kill.
    """
    from database.historify_db import _get_shared_conn

    barrier = threading.Barrier(50)
    instances: list = []
    lock = threading.Lock()

    def race():
        barrier.wait()
        c = _get_shared_conn()
        with lock:
            instances.append(c)

    with ThreadPoolExecutor(max_workers=50) as pool:
        for fut in as_completed(pool.submit(race) for _ in range(50)):
            fut.result()

    assert len(instances) == 50
    unique = {id(c) for c in instances}
    assert len(unique) == 1, (
        f"expected exactly 1 shared connection, got {len(unique)} — "
        "double-checked locking is not protecting first init"
    )


def test_reset_for_tests_drops_singleton_and_next_call_re_opens():
    """``_reset_for_tests`` is the only sanctioned way to swap the file path
    mid-process. After calling it the cached instance must be gone."""
    import database.historify_db as h
    from database.historify_db import _get_shared_conn, _reset_for_tests

    first = _get_shared_conn()
    assert h._shared_conn is first
    _reset_for_tests()
    assert h._shared_conn is None, "_reset_for_tests should clear the cache"
    second = _get_shared_conn()
    assert second is not first, "next call should open a fresh connection"


# --------------------------------------------------------------------------- #
# Cursor lifecycle — closing a cursor must NOT close the underlying DB
# --------------------------------------------------------------------------- #


def test_get_connection_yields_cursor_not_the_shared_connection():
    """``get_connection`` MUST yield a cursor, not the bare singleton —
    otherwise the ``with`` block's __exit__ would close the singleton."""
    from database.historify_db import _get_shared_conn, get_connection

    shared = _get_shared_conn()
    with get_connection() as c:
        assert c is not shared, (
            "expected a cursor distinct from the singleton; got the singleton "
            "itself — its __exit__ would close the shared connection"
        )


def test_cursor_close_does_not_close_shared_connection():
    """After the ``with get_connection()`` block exits, the next caller MUST
    still get a working cursor from the same singleton."""
    from database.historify_db import _get_shared_conn, get_connection

    with get_connection() as c:
        c.execute("SELECT 1").fetchone()

    # If cursor close had killed the singleton, this would raise
    # ConnectionException("database is closed") or similar.
    n = _get_shared_conn().execute("SELECT 42").fetchone()[0]
    assert n == 42


def test_freshness_service_alias_shares_the_singleton():
    """``connect_historify_readonly`` MUST return a cursor on the same
    singleton — otherwise the pre-#191 config-mismatch race could re-emerge."""
    from database.historify_db import _get_shared_conn, get_db_path
    from services.data_freshness_service import connect_historify_readonly

    shared = _get_shared_conn()
    # Pass the singleton's path so we exercise the production happy-path
    # (caller's path matches → cursor on singleton), not the test-ergonomic
    # fallthrough that opens a separate file.
    cur = connect_historify_readonly(get_db_path())
    try:
        assert cur is not shared
        # A cursor's parent connection is reachable via .execute working
        # against the shared underlying DB. Prove they share state:
        shared.execute("CREATE OR REPLACE TABLE _share_probe (k INT)").fetchone()
        # The cursor sees the table the singleton just created — same DB.
        rows = cur.execute("SELECT COUNT(*) FROM _share_probe").fetchone()
        assert rows[0] == 0
    finally:
        try:
            cur.close()
        except Exception:
            pass

    # After cursor close, the singleton must still be usable.
    again = _get_shared_conn()
    assert again is shared


def test_freshness_service_alias_returned_cursor_supports_with_block():
    """Direct-assign callers (``con = connect_historify_readonly(...)``) AND
    context-manager callers (``with connect_historify_readonly(...) as c:``)
    both exist in the codebase; both must work without closing the singleton.
    """
    from database.historify_db import _get_shared_conn, get_db_path
    from services.data_freshness_service import connect_historify_readonly

    shared = _get_shared_conn()
    with connect_historify_readonly(get_db_path()) as c:
        c.execute("SELECT 1").fetchone()

    # Singleton survives the __exit__:
    assert _get_shared_conn() is shared
    _get_shared_conn().execute("SELECT 1").fetchone()


# --------------------------------------------------------------------------- #
# Stress test — concurrent readers + writers across many ops produce ZERO
# ConnectionException / IOException. This is the regression that blocks the
# class of error this fix kills.
# --------------------------------------------------------------------------- #


def test_concurrent_readers_and_writers_no_lock_exceptions():
    """50 reader threads + 10 writer threads, ~20 ops each = 1200 ops total.

    Against the pre-#191 ``duckdb.connect()`` per call this stresser would
    burst ``"Can't open a connection to same database file with a different
    configuration"`` whenever a reader and a writer raced on the first
    connect. With the singleton, every op uses a cursor on the same
    connection — DuckDB serialises ops on a connection internally, and there
    is no config to mismatch. Zero lock exceptions expected.
    """
    _ensure_market_data_schema()
    from database.historify_db import _get_shared_conn, get_connection

    # Seed a row so readers have something to scan. Use an explicit column
    # list — another test in the same pytest-process may have already created
    # ``market_data`` with the production 11-column schema (``oi``,
    # ``created_at``), in which case ``INSERT INTO market_data VALUES (...)``
    # with 9 positional values fails ``Binder Error: has N columns but 9 values
    # were supplied``. The column-list form is schema-version-tolerant.
    seed = _get_shared_conn()
    seed.execute(
        "INSERT OR REPLACE INTO market_data "
        "(symbol, exchange, interval, timestamp, open, high, low, close, volume) "
        "VALUES ('SEED', 'NSE', '1m', 0, 1.0, 1.0, 1.0, 1.0, 0)"
    )

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    barrier = threading.Barrier(60)

    def read_worker(_i: int) -> None:
        try:
            barrier.wait()
            for _ in range(20):
                with get_connection() as c:
                    c.execute("SELECT COUNT(*) FROM market_data WHERE symbol = 'SEED'").fetchone()
        except Exception as e:  # noqa: BLE001
            with errors_lock:
                errors.append(e)

    def write_worker(i: int) -> None:
        try:
            barrier.wait()
            for k in range(20):
                with get_connection() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO market_data "
                        "(symbol, exchange, interval, timestamp, open, high, low, close, volume) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [f"W{i}", "NSE", "1m", k, 1.0, 1.0, 1.0, 1.0, 1],
                    )
        except Exception as e:  # noqa: BLE001
            with errors_lock:
                errors.append(e)

    with ThreadPoolExecutor(max_workers=60) as pool:
        futs = []
        futs.extend(pool.submit(read_worker, i) for i in range(50))
        futs.extend(pool.submit(write_worker, i) for i in range(10))
        for f in as_completed(futs):
            f.result()

    lock_errors = [
        e
        for e in errors
        if any(
            s in str(e)
            for s in (
                "different configuration",
                "being used by another process",
                "Failed to connect to DuckDB",
                "Could not set lock",
            )
        )
    ]
    assert lock_errors == [], (
        f"singleton did NOT prevent lock-class errors — got {len(lock_errors)} "
        f"out of {len(errors)} total; first: {lock_errors[0]!r}"
    )
    # Any other unexpected exceptions also fail the test loud — the singleton
    # is the only thing this test is supposed to certify, but a regression
    # anywhere downstream shouldn't pass silently.
    assert errors == [], f"unexpected non-lock errors under concurrency: {errors[:3]!r}"

    # Sanity: writers actually wrote, readers actually read.
    final_count = _get_shared_conn().execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    assert final_count >= 1 + 10 * 20, (
        f"expected at least {1 + 10 * 20} rows after writers, got {final_count}"
    )
