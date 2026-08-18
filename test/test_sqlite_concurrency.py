"""SQLite concurrency contract for app engines (issue #633).

The 2026-08-18 pre-open outage: `openalgo.db` (100 MB, rollback-journal mode)
became unreadable for ~11 minutes on the daily master-contract boot. The swap
writes 114,268 symtoken rows, then the cache hook reads all of them back in a
single 38.88 s pass. In rollback-journal mode a writer that needs EXCLUSIVE
while that long read holds SHARED escalates to PENDING — and PENDING blocks
every NEW reader. Each died on the driver's 5 s timeout, which is exactly the
5-7 s failure cadence in the production log.

WAL removes the class: readers never block writers and writers never block
readers.

Every test here builds its OWN file-backed engine and never reads a
`database.*` module's ambient engine. That is not fastidiousness — an earlier
draft asserted on `market_intel_db.engine`, passed on Windows, and failed on
Linux CI with `assert 'memory' == 'wal'`: `test/test_action_center.py` sets
`os.environ["DATABASE_URL"] = "sqlite:///:memory:"` at MODULE level, so under
xdist any module imported after it binds to an in-memory database, where WAL is
impossible and every connection sees a different empty DB. The contract under
test is about file-backed databases, so the test must supply one.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

_CREATE = "CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, v TEXT)"


@pytest.fixture
def file_engine(tmp_path):
    """An engine built exactly as the app builds one, on a real file."""
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrency.db'}")
    with engine.connect() as conn:
        conn.execute(text(_CREATE))
        conn.commit()
    try:
        yield engine
    finally:
        engine.dispose()


def _pragma(engine, name: str):
    with engine.connect() as conn:
        return conn.execute(text(f"PRAGMA {name}")).scalar()


def test_file_backed_engines_run_in_wal_mode(file_engine):
    """Pre-fix this returns 'delete' — the mode that let a long read lock everyone out."""
    assert str(_pragma(file_engine, "journal_mode")).lower() == "wal"


def test_busy_timeout_is_set_explicitly(file_engine):
    """Regression guard, NOT a repro — pysqlite's default already supplies 5000 ms.

    It exists so a future change to `timeout=0` is caught.
    """
    assert int(_pragma(file_engine, "busy_timeout")) >= 1000


def test_a_long_reader_does_not_block_a_writer(file_engine):
    """The outage shape, reduced to two connections.

    A reader holds an open read transaction (the 38.88 s cache load); a writer
    commits (news ingest). Pre-fix the commit raises `database is locked`.
    """
    reader = file_engine.raw_connection()
    writer = file_engine.raw_connection()
    try:
        rc = reader.cursor()
        rc.execute("BEGIN")
        rc.execute("SELECT count(*) FROM probe").fetchone()  # holds the read open

        wc = writer.cursor()
        wc.execute("PRAGMA busy_timeout=500")  # fail fast rather than stall the suite
        wc.execute("INSERT INTO probe (v) VALUES ('written-under-a-long-read')")
        writer.commit()  # <-- pre-fix: sqlite3.OperationalError: database is locked

        assert _pragma(file_engine, "journal_mode").lower() == "wal"
    finally:
        for conn in (reader, writer):
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            conn.close()


def test_a_writer_does_not_block_new_readers(file_engine):
    """Regression guard, NOT a repro.

    It passes on both trees: a writer only blocks readers during the brief
    EXCLUSIVE phase of commit, which cannot be held open deterministically.
    Kept because the READER-blocked half is what actually took the app down,
    so a future regression there must not go unnoticed.
    """
    writer = file_engine.raw_connection()
    reader = file_engine.raw_connection()
    try:
        wc = writer.cursor()
        wc.execute("BEGIN IMMEDIATE")
        wc.execute("INSERT INTO probe (v) VALUES ('uncommitted')")

        rc = reader.cursor()
        rc.execute("PRAGMA busy_timeout=500")
        rc.execute("SELECT count(*) FROM probe").fetchone()
    finally:
        for conn in (writer, reader):
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            conn.close()


def test_listener_covers_engines_created_after_import(tmp_path):
    """The ~30 broker `master_contract_db` engines are the real target.

    They call `create_engine(DATABASE_URL)` with no knowledge of this listener,
    and the zerodha one performs the 114,268-row symtoken swap.
    """
    independent = create_engine(f"sqlite:///{tmp_path / 'broker_like.db'}")
    try:
        assert str(_pragma(independent, "journal_mode")).lower() == "wal"
        assert int(_pragma(independent, "busy_timeout")) >= 1000
    finally:
        independent.dispose()


def test_in_memory_databases_are_exempt_not_broken():
    """`:memory:` reports 'memory' and cannot be WAL — the listener must accept it.

    Several tests bind modules to `sqlite:///:memory:`, so treating that as a
    failure would spam a warning on every connect in the suite.
    """
    mem = create_engine("sqlite:///:memory:")
    try:
        assert str(_pragma(mem, "journal_mode")).lower() == "memory"
    finally:
        mem.dispose()


def test_journal_mode_is_overridable_for_rollback(monkeypatch, tmp_path):
    """`SQLITE_JOURNAL_MODE=DELETE` must restore the old behaviour."""
    from database import sqlite_tuning

    monkeypatch.setattr(sqlite_tuning, "JOURNAL_MODE", "DELETE")
    rolled_back = create_engine(f"sqlite:///{tmp_path / 'rollback.db'}")
    try:
        assert str(_pragma(rolled_back, "journal_mode")).lower() == "delete"
    finally:
        rolled_back.dispose()


def test_a_plain_file_copy_of_a_wal_db_is_not_a_valid_backup(tmp_path):
    """WAL's one real regression, pinned so nobody re-introduces `cp` backups.

    Under WAL, recent commits live in the `-wal` sidecar until a checkpoint, so
    copying only the `.db` file of a LIVE database silently loses them. Measured
    while adding WAL (#633): a plain copy of a 2-row database produced a copy in
    which the table did not exist at all.

    `services/futures_follow_t1_backfill.py` used `shutil.copy2` here, right
    before rewriting trade rows — a backup that looks fine and is missing data
    is worse than no backup. It now uses the sqlite backup API, which this test
    pins as the correct alternative.
    """
    import shutil

    src = tmp_path / "live.db"
    engine = create_engine(f"sqlite:///{src}")
    try:
        with engine.connect() as conn:
            conn.execute(text(_CREATE))
            conn.execute(text("INSERT INTO probe (v) VALUES ('committed')"))
            conn.commit()
        assert str(_pragma(engine, "journal_mode")).lower() == "wal"

        live = sqlite3.connect(str(src))  # hold it open, as the running app does
        try:
            naive = tmp_path / "naive.db"
            shutil.copy2(src, naive)
            with pytest.raises(sqlite3.Error):
                sqlite3.connect(str(naive)).execute("SELECT count(*) FROM probe").fetchone()

            proper = tmp_path / "proper.db"
            dst = sqlite3.connect(str(proper))
            try:
                live.backup(dst)
            finally:
                dst.close()
            got = sqlite3.connect(str(proper)).execute("SELECT count(*) FROM probe").fetchone()[0]
            assert got == 1
        finally:
            live.close()
    finally:
        engine.dispose()
