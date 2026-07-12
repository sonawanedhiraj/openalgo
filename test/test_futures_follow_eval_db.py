"""Tests for database/futures_follow_eval_db.list_snapshots (issue #395).

Rebinds the module engine/session to a fresh in-memory SQLite DB per test so no
live DB is touched (the global conftest tripwire also guards this).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch):
    from database import futures_follow_eval_db as eval_db

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(eval_db, "engine", eng)
    monkeypatch.setattr(eval_db, "db_session", sess)
    eval_db.Base.query = sess.query_property()
    eval_db.Base.metadata.create_all(eng)
    yield eval_db
    sess.remove()
    eng.dispose()


def _seed(eval_db, dates, strategy="futures_follow_cap50"):
    for d in dates:
        assert eval_db.upsert_snapshot(strategy, d, {"n_signals": 0, "symbols": []})


def test_empty_history_returns_empty_list(_isolate_db):
    assert _isolate_db.list_snapshots("futures_follow_cap50") == []


def test_orders_newest_first(_isolate_db):
    _seed(_isolate_db, ["2026-07-07", "2026-07-09", "2026-07-08"])
    rows = _isolate_db.list_snapshots("futures_follow_cap50")
    assert [r["eval_date"] for r in rows] == ["2026-07-09", "2026-07-08", "2026-07-07"]


def test_limit_truncates_from_the_newest_end(_isolate_db):
    _seed(_isolate_db, ["2026-07-07", "2026-07-08", "2026-07-09"])
    rows = _isolate_db.list_snapshots("futures_follow_cap50", limit=2)
    assert [r["eval_date"] for r in rows] == ["2026-07-09", "2026-07-08"]


def test_before_is_exclusive_and_pages_backwards(_isolate_db):
    _seed(_isolate_db, ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"])
    page1 = _isolate_db.list_snapshots("futures_follow_cap50", limit=2)
    page2 = _isolate_db.list_snapshots(
        "futures_follow_cap50", limit=2, before=page1[-1]["eval_date"]
    )
    assert [r["eval_date"] for r in page1] == ["2026-07-09", "2026-07-08"]
    assert [r["eval_date"] for r in page2] == ["2026-07-07", "2026-07-06"]


def test_limit_clamped_to_max(_isolate_db):
    _seed(_isolate_db, [f"2026-07-{d:02d}" for d in range(1, 11)])
    rows = _isolate_db.list_snapshots("futures_follow_cap50", limit=10_000)
    assert len(rows) == 10  # clamped to MAX_HISTORY_LIMIT (90), bounded by rows present


def test_limit_below_one_is_clamped_to_one(_isolate_db):
    _seed(_isolate_db, ["2026-07-08", "2026-07-09"])
    assert len(_isolate_db.list_snapshots("futures_follow_cap50", limit=0)) == 1


def test_filters_by_strategy_name(_isolate_db):
    _seed(_isolate_db, ["2026-07-08"], strategy="futures_follow_cap50")
    _seed(_isolate_db, ["2026-07-09"], strategy="some_other_strategy")
    rows = _isolate_db.list_snapshots("futures_follow_cap50")
    assert [r["eval_date"] for r in rows] == ["2026-07-08"]


def test_rows_carry_the_full_payload(_isolate_db):
    assert _isolate_db.upsert_snapshot(
        "futures_follow_cap50", "2026-07-09", {"n_signals": 3, "symbols": [{"symbol": "INFY"}]}
    )
    row = _isolate_db.list_snapshots("futures_follow_cap50")[0]
    assert row["payload"]["n_signals"] == 3
    assert row["payload"]["symbols"] == [{"symbol": "INFY"}]


def test_before_accepts_a_date_object(_isolate_db):
    from datetime import date

    _seed(_isolate_db, ["2026-07-08", "2026-07-09"])
    rows = _isolate_db.list_snapshots("futures_follow_cap50", before=date(2026, 7, 9))
    assert [r["eval_date"] for r in rows] == ["2026-07-08"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
