"""SQLite concurrency contract for every app engine (issue #633).

The 2026-08-18 pre-open outage: `openalgo.db` (100 MB, rollback-journal mode)
became unreadable for ~11 minutes on the daily master-contract boot. The swap
writes 114,268 symtoken rows, then the cache hook reads all of them back in a
single 38.88 s pass. In rollback-journal mode a writer that needs EXCLUSIVE
while that long read holds SHARED escalates to PENDING — and PENDING blocks
every NEW reader. Each one then died on pysqlite's 5 s default timeout, which
is exactly the 5-7 s failure cadence in the log.

WAL removes the class: readers never block writers and writers never block
readers. These tests pin that as a contract on the engines the app actually
uses, not on a hand-rolled connection.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text

from database import market_intel_db as mid

pytestmark = pytest.mark.integration


def _pragma(engine, name: str):
    with engine.connect() as conn:
        return conn.execute(text(f"PRAGMA {name}")).scalar()


def test_app_sqlite_engines_run_in_wal_mode():
    """Every engine built by a `database.*` module must be WAL.

    Pre-fix this returns 'delete' — the rollback journal that made a long
    read able to wall off the whole database.
    """
    assert str(_pragma(mid.engine, "journal_mode")).lower() == "wal"


def test_busy_timeout_is_set_explicitly():
    """A 0 / driver-default timeout is what turned contention into hard errors."""
    assert int(_pragma(mid.engine, "busy_timeout")) >= 1000


def test_a_long_reader_does_not_block_a_writer():
    """The outage shape, reduced to two connections.

    A reader holds an open read transaction (the cache load); a writer commits
    (news ingest). Pre-fix the commit raises `database is locked`.
    """
    mid.Base.metadata.create_all(mid.engine)

    reader = mid.engine.raw_connection()
    writer = mid.engine.raw_connection()
    try:
        rc = reader.cursor()
        rc.execute("BEGIN")
        rc.execute("SELECT count(*) FROM market_intel").fetchone()  # holds the read open

        wc = writer.cursor()
        wc.execute("PRAGMA busy_timeout=500")  # fail fast rather than stall the suite
        wc.execute(
            "INSERT INTO market_intel (captured_at, kind, payload_json) VALUES (?, ?, ?)",
            ("2026-08-18T09:00:00+05:30", "news", "{}"),
        )
        writer.commit()  # <-- pre-fix: sqlite3.OperationalError: database is locked
    finally:
        try:
            reader.rollback()
        except sqlite3.Error:
            pass
        reader.close()
        writer.close()


def test_a_pending_writer_does_not_block_new_readers():
    """The second half: PENDING is what made every *reader* fail, not the write."""
    mid.Base.metadata.create_all(mid.engine)

    writer = mid.engine.raw_connection()
    reader = mid.engine.raw_connection()
    try:
        wc = writer.cursor()
        wc.execute("BEGIN IMMEDIATE")
        wc.execute(
            "INSERT INTO market_intel (captured_at, kind, payload_json) VALUES (?, ?, ?)",
            ("2026-08-18T09:01:00+05:30", "news", "{}"),
        )

        rc = reader.cursor()
        rc.execute("PRAGMA busy_timeout=500")
        rc.execute("SELECT count(*) FROM market_intel").fetchone()
    finally:
        try:
            writer.rollback()
        except sqlite3.Error:
            pass
        writer.close()
        reader.close()


def test_listener_covers_engines_created_anywhere(tmp_path):
    """The ~30 broker `master_contract_db` engines are the real target.

    They build their own `create_engine(DATABASE_URL)` with no knowledge of
    this listener, and the zerodha one performs the 114,268-row symtoken swap.
    A class-level listener must reach an engine constructed independently and
    AFTER import — that is the whole reason this is not a per-module change.
    """
    from sqlalchemy import create_engine

    independent = create_engine(f"sqlite:///{tmp_path / 'broker_like.db'}")
    try:
        assert str(_pragma(independent, "journal_mode")).lower() == "wal"
        assert int(_pragma(independent, "busy_timeout")) >= 1000
    finally:
        independent.dispose()


def test_journal_mode_is_overridable_for_rollback(monkeypatch, tmp_path):
    """`SQLITE_JOURNAL_MODE=DELETE` must restore the old behaviour.

    A change this broad needs a switch that does not require a code edit.
    """
    from sqlalchemy import create_engine

    from database import sqlite_tuning

    monkeypatch.setattr(sqlite_tuning, "JOURNAL_MODE", "DELETE")
    rolled_back = create_engine(f"sqlite:///{tmp_path / 'rollback.db'}")
    try:
        assert str(_pragma(rolled_back, "journal_mode")).lower() == "delete"
    finally:
        rolled_back.dispose()
