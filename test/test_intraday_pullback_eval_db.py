"""Tests for database/intraday_pullback_eval_db.list_snapshots (issue #422).

Rebinds the module engine/session to a fresh in-memory SQLite DB per test so no live DB is
touched (the global conftest tripwire also guards this). Mirrors test_futures_follow_eval_db.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

STRAT = "intraday_pullback_top2"


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch):
    from database import intraday_pullback_eval_db as eval_db

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(eval_db, "engine", eng)
    monkeypatch.setattr(eval_db, "db_session", sess)
    eval_db.Base.query = sess.query_property()
    eval_db.Base.metadata.create_all(eng)
    yield eval_db
    sess.remove()
    eng.dispose()


def _seed(eval_db, dates, strategy=STRAT):
    for d in dates:
        assert eval_db.upsert_snapshot(strategy, d, {"picks": [], "evaluation": []})


def test_empty_history_returns_empty_list(_isolate_db):
    assert _isolate_db.list_snapshots(STRAT) == []


def test_orders_newest_first(_isolate_db):
    _seed(_isolate_db, ["2026-07-14", "2026-07-16", "2026-07-15"])
    rows = _isolate_db.list_snapshots(STRAT)
    assert [r["eval_date"] for r in rows] == ["2026-07-16", "2026-07-15", "2026-07-14"]


def test_limit_truncates_from_the_newest_end(_isolate_db):
    _seed(_isolate_db, ["2026-07-14", "2026-07-15", "2026-07-16"])
    rows = _isolate_db.list_snapshots(STRAT, limit=2)
    assert [r["eval_date"] for r in rows] == ["2026-07-16", "2026-07-15"]


def test_limit_is_clamped_to_bounds(_isolate_db):
    _seed(_isolate_db, [f"2026-07-{d:02d}" for d in range(1, 6)])
    assert len(_isolate_db.list_snapshots(STRAT, limit=0)) == 1  # clamped up to 1
    assert len(_isolate_db.list_snapshots(STRAT, limit=10_000)) == 5  # clamped to MAX, then all


def test_before_pages_backwards_exclusively(_isolate_db):
    _seed(_isolate_db, ["2026-07-14", "2026-07-15", "2026-07-16"])
    rows = _isolate_db.list_snapshots(STRAT, before="2026-07-16")
    assert [r["eval_date"] for r in rows] == ["2026-07-15", "2026-07-14"]


def test_only_returns_the_requested_strategy(_isolate_db):
    _seed(_isolate_db, ["2026-07-15"])
    _seed(_isolate_db, ["2026-07-16"], strategy="some_other_strategy")
    assert [r["eval_date"] for r in _isolate_db.list_snapshots(STRAT)] == ["2026-07-15"]


def test_rows_carry_the_payload_and_eval_at(_isolate_db):
    _isolate_db.upsert_snapshot(STRAT, "2026-07-15", {"picks": ["AAA"], "n_trades_today": 1})
    (row,) = _isolate_db.list_snapshots(STRAT)
    assert row["picks"] == ["AAA"]
    assert row["n_trades_today"] == 1
    assert row["eval_at"] is not None


def test_repeat_upsert_overwrites_the_same_row(_isolate_db):
    """The per-tick write (issue #422) hits the same (strategy, date) row ~75x/day."""
    _isolate_db.upsert_snapshot(STRAT, "2026-07-15", {"n_trades_today": 0})
    _isolate_db.upsert_snapshot(STRAT, "2026-07-15", {"n_trades_today": 2})
    rows = _isolate_db.list_snapshots(STRAT)
    assert len(rows) == 1
    assert rows[0]["n_trades_today"] == 2
