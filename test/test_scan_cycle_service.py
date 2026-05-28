"""Tests for the fail-safe scan_cycle audit service.

The service must never raise into the order path. We assert this directly by
forcing a corrupt session state and confirming heartbeat / start_cycle /
complete_cycle still return cleanly.
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_cycle_db(monkeypatch):
    """Point scan_cycle_db at a fresh in-memory SQLite for one test."""
    from database import scan_cycle_db as scdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )

    monkeypatch.setattr(scdb, "engine", test_engine)
    monkeypatch.setattr(scdb, "db_session", test_session)
    scdb.Base.metadata.create_all(test_engine)

    yield scdb

    test_session.remove()
    test_engine.dispose()


# ---------------------------------------------------------------------------
# Happy-path inserts
# ---------------------------------------------------------------------------


def test_start_cycle_creates_row(fresh_cycle_db):
    from services import scan_cycle_service

    cid = scan_cycle_service.start_cycle("chartink", operator_intent="live")

    assert cid > 0
    row = fresh_cycle_db.db_session.query(fresh_cycle_db.ScanCycle).filter_by(id=cid).first()
    assert row is not None
    assert row.cycle_kind == "chartink"
    assert row.operator_intent == "live"
    assert row.post_status == "pending"
    assert row.completed_at is None


def test_heartbeat_creates_row(fresh_cycle_db):
    from services import scan_cycle_service

    cid = scan_cycle_service.start_cycle("chartink")
    scan_cycle_service.heartbeat(cid, "scan_buy", "ok", "5 syms")

    rows = (
        fresh_cycle_db.db_session.query(fresh_cycle_db.CycleHeartbeat)
        .filter_by(cycle_id=cid)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].stage == "scan_buy"
    assert rows[0].status == "ok"
    assert rows[0].detail == "5 syms"


def test_complete_cycle_updates_row(fresh_cycle_db):
    from services import scan_cycle_service

    cid = scan_cycle_service.start_cycle("chartink")
    scan_cycle_service.complete_cycle(
        cid,
        post_status="ok",
        screener_buy=["RELIANCE", "INFY"],
        engine_response={"status": "success", "armed": 2},
        effective_mode="sandbox",
    )

    row = fresh_cycle_db.db_session.query(fresh_cycle_db.ScanCycle).filter_by(id=cid).first()
    assert row.completed_at is not None
    assert row.post_status == "ok"
    assert row.effective_mode == "sandbox"
    assert json.loads(row.screener_buy) == ["RELIANCE", "INFY"]
    assert json.loads(row.engine_response) == {"status": "success", "armed": 2}


def test_full_cycle_lifecycle(fresh_cycle_db):
    """start → 3 heartbeats → complete leaves a consistent audit trail."""
    from services import scan_cycle_service

    cid = scan_cycle_service.start_cycle("chartink", operator_intent="live")
    scan_cycle_service.heartbeat(cid, "preflight", "ok")
    scan_cycle_service.heartbeat(cid, "scan_buy", "ok", "3 syms")
    scan_cycle_service.heartbeat(cid, "post", "ok")
    scan_cycle_service.complete_cycle(
        cid,
        post_status="ok",
        screener_buy=["A", "B", "C"],
        effective_mode="live",
    )

    cycles = scan_cycle_service.get_recent_cycles(hours=24)
    assert any(c["id"] == cid for c in cycles)

    heartbeats = scan_cycle_service.get_cycle_heartbeats(cid)
    assert len(heartbeats) == 3
    assert [h["stage"] for h in heartbeats] == ["preflight", "scan_buy", "post"]


# ---------------------------------------------------------------------------
# Fail-safety
# ---------------------------------------------------------------------------


def test_heartbeat_failure_does_not_raise(fresh_cycle_db, monkeypatch):
    """A broken session must NOT bubble out of heartbeat()."""
    from services import scan_cycle_service

    cid = scan_cycle_service.start_cycle("chartink")
    assert cid > 0

    # Replace the underlying engine with a disposed one — any DB op will throw.
    fresh_cycle_db.engine.dispose()
    fresh_cycle_db.db_session.remove()
    monkeypatch.setattr(fresh_cycle_db.db_session, "add", _raise_boom)

    # MUST NOT RAISE.
    scan_cycle_service.heartbeat(cid, "scan_buy", "ok")
    scan_cycle_service.heartbeat(cid, "post", "error", "boom")


def test_start_cycle_failure_returns_minus_one(monkeypatch):
    """If the DB session is broken at start, start_cycle returns -1 silently."""
    from database import scan_cycle_db as scdb
    from services import scan_cycle_service

    class _BrokenSession:
        def add(self, *a, **kw):
            raise RuntimeError("simulated DB outage")

        def commit(self):
            raise RuntimeError("simulated DB outage")

        def rollback(self):
            return None

        def remove(self):
            return None

    monkeypatch.setattr(scdb, "db_session", _BrokenSession())
    cid = scan_cycle_service.start_cycle("chartink")
    assert cid == -1


def test_heartbeat_with_minus_one_id_noops(fresh_cycle_db):
    """Passing cycle_id=-1 (start_cycle failure sentinel) must silently no-op."""
    from services import scan_cycle_service

    # Should not raise, should not insert.
    scan_cycle_service.heartbeat(-1, "scan_buy", "ok")
    rows = fresh_cycle_db.db_session.query(fresh_cycle_db.CycleHeartbeat).all()
    assert rows == []


def test_complete_cycle_with_minus_one_id_noops(fresh_cycle_db):
    from services import scan_cycle_service

    scan_cycle_service.complete_cycle(-1, post_status="ok")
    rows = fresh_cycle_db.db_session.query(fresh_cycle_db.ScanCycle).all()
    assert rows == []


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


def test_get_recent_cycles_returns_ordered(fresh_cycle_db):
    """Newest-first ordering for the dashboard / debugger views."""
    from services import scan_cycle_service

    c1 = scan_cycle_service.start_cycle("chartink")
    c2 = scan_cycle_service.start_cycle("inhouse")
    c3 = scan_cycle_service.start_cycle("manual")

    cycles = scan_cycle_service.get_recent_cycles(hours=24)
    ids = [c["id"] for c in cycles]
    # Insertion order is c1, c2, c3. started_at uses millisecond resolution
    # via isoformat so the relative ordering is preserved; sorted desc gives
    # us c3, c2, c1 (or at least c3 first and c1 last).
    assert ids.index(c3) < ids.index(c1)


def test_cycles_since_counts_correctly(fresh_cycle_db):
    """cycles_since() returns a count, used by preflight staleness checks."""
    import datetime as dt

    import pytz

    from services import scan_cycle_service

    before = dt.datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    scan_cycle_service.start_cycle("chartink")
    scan_cycle_service.start_cycle("chartink")

    assert scan_cycle_service.cycles_since(before) == 2

    # A future timestamp returns 0.
    future = (
        dt.datetime.now(pytz.timezone("Asia/Kolkata")) + dt.timedelta(hours=1)
    ).isoformat()
    assert scan_cycle_service.cycles_since(future) == 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _raise_boom(*args, **kwargs):
    raise RuntimeError("simulated DB outage")
