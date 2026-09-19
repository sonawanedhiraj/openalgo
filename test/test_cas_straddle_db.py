"""Journal / config / poll / session persistence for cas_320_expiry_straddle (#740).

Hermetic: the module engine is rebound to an in-memory SQLite per test.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from database import cas_straddle_db as db


@pytest.fixture(autouse=True)
def _mem_db(monkeypatch):
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "db_session", sess)
    db.Base.query = sess.query_property()
    db.Base.metadata.create_all(eng)
    yield
    sess.remove()
    eng.dispose()


def _leg(**over):
    base = {
        "mode": "sandbox",
        "trade_date": "2026-09-22",
        "underlying": "NIFTY",
        "exchange": "NFO",
        "symbol": "NIFTY22SEP2623200CE",
        "strike": 23200.0,
        "expiry": "22-SEP-26",
        "side": "CE",
        "lots": 1,
        "quantity": 65,
        "entry_ref_price": 100.0,
        "entry_order_id": "o1",
        "status": "placed",
        "fill": "real",
    }
    base.update(over)
    return db.record_trade(**base)


def test_config_roundtrip_null_means_unset():
    assert db.get_config() is None
    stored = db.save_config({"trade_sensex": False, "lots_nifty": 2, "hard_exit_time": None})
    assert stored["trade_sensex"] is False
    assert stored["lots_nifty"] == 2
    assert stored["hard_exit_time"] is None
    assert stored["trade_nifty"] is None  # never set → NULL → code default
    # second save only touches the given keys
    again = db.save_config({"target_mult": 2.5})
    assert again["lots_nifty"] == 2 and again["target_mult"] == 2.5


def test_net_pnl_single_definition_and_fill_classes():
    rid = _leg()
    assert rid
    assert db.net_pnl_of_row(db.get_trade(rid)) is None  # open → unknown, never 0
    db.update_trade(rid, status="open", entry_price=100.0, entry_qty=65)
    db.update_trade(rid, status="closed", exit_price=250.0, charges_inr=60.0)
    row = db.get_trade(rid)
    assert row.gross_pnl == pytest.approx(150.0 * 65)
    assert row.net_pnl == pytest.approx(150.0 * 65 - 60.0)
    assert db.net_pnl_of_row(row) == pytest.approx(150.0 * 65 - 60.0)
    # a paper leg is priced for measurement but never joins real P&L
    pid = _leg(side="PE", symbol="NIFTY22SEP2623200PE", status="rejected", fill="paper")
    db.update_trade(
        pid, status="closed", entry_price=90.0, entry_qty=65, exit_price=0.0, charges_inr=0
    )
    assert db.net_pnl_of_row(db.get_trade(pid)) is None
    assert [r.id for r in db.real_closed_rows()] == [rid]
    assert [r.id for r in db.real_closed_rows(mode="live")] == []


def test_open_trades_excludes_paper_and_closed():
    a = _leg(status="open", entry_price=100.0, entry_qty=65)
    _leg(side="PE", symbol="X", status="rejected", fill="paper")
    c = _leg(side="PE", symbol="Y", status="placed")
    assert sorted(r.id for r in db.open_trades("2026-09-22")) == sorted([a, c])
    assert db.open_trades("2026-09-23") == []


def test_polls_and_sessions():
    now = datetime(2026, 9, 22, 9, 50, 0)
    n = db.record_polls(
        [
            {"ts": now, "trade_date": "2026-09-22", "underlying": "NIFTY", "kind": "spot", "symbol": "NIFTY",
                 "exchange": "NSE_INDEX", "strike": None, "side": None, "ltp": 23180.0, "bid": None, "ask": None,
                 "volume": None, "oi": None},
            {"ts": now, "trade_date": "2026-09-22", "underlying": "NIFTY", "kind": "option",
                 "symbol": "NIFTY22SEP2623200CE", "exchange": "NFO", "strike": 23200.0, "side": "CE",
                 "ltp": 100.0, "bid": 99.0, "ask": 101.0, "volume": 10, "oi": 500},
        ]
    )  # fmt: skip
    assert n == 2
    rows = db.polls_for("2026-09-22", "NIFTY")
    assert [db.poll_to_dict(p)["kind"] for p in rows] == ["spot", "option"]
    assert db.polls_for("2026-09-22", "SENSEX") == []

    sid = db.upsert_session("2026-09-22", "NIFTY", atm_strike=23200.0, config={"lots_nifty": 1})
    sid2 = db.upsert_session("2026-09-22", "NIFTY", close_1515=23172.35, traded=1)
    assert sid == sid2  # same (date, underlying) → update in place
    s = db.session_to_dict(db.sessions()[0])
    assert s["atm_strike"] == 23200.0 and s["close_1515"] == 23172.35
    assert s["traded"] is True and s["config"] == {"lots_nifty": 1}


def test_trade_to_dict_carries_net_from_the_helper():
    rid = _leg(status="closed", fill="real", entry_price=100.0, entry_qty=65, exit_price=110.0,
               charges_inr=50.0)  # fmt: skip
    d = db.trade_to_dict(db.get_trade(rid))
    assert d["net_pnl"] == pytest.approx(10.0 * 65 - 50.0)
