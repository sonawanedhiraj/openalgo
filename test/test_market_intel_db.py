"""Session-lifecycle regression tests for ``database/market_intel_db.py`` (issue #627).

These reproduce the 2026-08-18 outage: ``insert_intel`` committed without a
``try/except``, so a ``database is locked`` error left the scoped session in an
aborted transaction that was never rolled back and never closed. It runs from an
APScheduler thread — no Flask app context — so ``teardown_appcontext`` never
reclaimed it either. The connection held the SQLite rollback journal for 11
minutes and every other reader in the process timed out.

The tests below hold a REAL ``BEGIN EXCLUSIVE`` from a second connection rather
than mocking ``commit()``. That distinction is load-bearing: a monkeypatched
``commit`` raises before SQLAlchemy marks its transaction inactive, so the
session is never actually poisoned and the test would pass on the broken tree.
Only a genuine driver-level lock reproduces the failure.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from database import market_intel_db as mid

pytestmark = pytest.mark.integration


@pytest.fixture
def intel_db(tmp_path):
    """A dedicated SQLite file with the session rebound to it.

    Deriving the path from ``mid.engine.url`` (an earlier draft) broke on CI:
    the table landed somewhere the insert never looked, so every assertion met
    ``no such table`` instead of ``database is locked`` — the tests failed on
    Linux and, worse, would have passed for the wrong reason had the raises()
    been loose. A dedicated ``tmp_path`` file removes the guesswork.

    The 0.2s busy timeout (vs pysqlite's 5s default) keeps the lock assertions
    fast; same file, same NullPool contract otherwise.
    """
    db_file = tmp_path / "intel.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 0.2},
    )
    mid.Base.metadata.create_all(engine)

    # Fail loudly if the harness itself is broken, rather than letting a
    # missing table masquerade as the lock error we are trying to assert.
    with sqlite3.connect(str(db_file)) as check:
        names = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "market_intel" in names, f"harness failed to create the table: {names}"

    original = mid.engine
    mid.db_session.remove()
    mid.db_session.configure(bind=engine)
    try:
        yield str(db_file)
    finally:
        mid.db_session.remove()
        mid.db_session.configure(bind=original)
        engine.dispose()


@contextmanager
def _external_writer_lock(db_file: str):
    """Hold a genuine EXCLUSIVE lock from outside SQLAlchemy."""
    conn = sqlite3.connect(db_file, timeout=0.2, isolation_level=None)
    conn.execute("BEGIN EXCLUSIVE")
    try:
        yield
    finally:
        conn.rollback()
        conn.close()


def test_insert_intel_recovers_after_a_locked_commit(intel_db):
    """The bug, stated as the invariant it broke.

    Pre-fix this fails on the SECOND insert with
    ``PendingRollbackError: Can't reconnect until invalid transaction is rolled
    back`` — the exact message the production log carried ~100 times.
    """
    with _external_writer_lock(intel_db):
        with pytest.raises(OperationalError, match="database is locked"):
            mid.insert_intel(kind="news", payload_json='{"t": "locked"}')

    # Lock released. The very next call must succeed — a single contended
    # write may not poison every subsequent write in the process.
    row_id = mid.insert_intel(kind="news", payload_json='{"t": "after"}')
    assert row_id > 0


def test_failed_insert_does_not_hold_the_database(intel_db):
    """The outage itself: a failed write must not leave the DB locked.

    Pre-fix the aborted session keeps its connection (and the rollback
    journal), so an external writer cannot take EXCLUSIVE afterwards.
    """
    with _external_writer_lock(intel_db):
        with pytest.raises(OperationalError, match="database is locked"):
            mid.insert_intel(kind="news", payload_json='{"t": "locked"}')

    # Nothing of ours may still be holding the file.
    probe = sqlite3.connect(intel_db, timeout=1.0)
    try:
        probe.execute("BEGIN EXCLUSIVE")
        probe.rollback()
    finally:
        probe.close()


def test_reads_release_their_session_too(intel_db):
    """``latest_intel`` / ``latest_intel_by_kind`` had no ``remove()`` either.

    A reader that never returns its connection is the same leak with a slower
    fuse — on a non-request thread nothing reclaims it.
    """
    mid.insert_intel(kind="news", payload_json='{"t": "read-me"}')
    assert mid.latest_intel("news") is not None
    assert isinstance(mid.latest_intel_by_kind("news", limit=5), list)

    probe = sqlite3.connect(intel_db, timeout=1.0)
    try:
        probe.execute("BEGIN EXCLUSIVE")
        probe.rollback()
    finally:
        probe.close()


def test_locked_insert_fails_fast_and_does_not_wedge_the_thread(intel_db):
    """A contended write raises promptly instead of hanging the ingest loop."""
    with _external_writer_lock(intel_db):
        started = time.time()
        with pytest.raises(OperationalError, match="database is locked"):
            mid.insert_intel(kind="news", payload_json='{"t": "slow"}')
        assert time.time() - started < 5.0
