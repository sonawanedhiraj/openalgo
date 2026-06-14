"""Schema + journal insert/query tests for database/futures_follow_db.py.

Rebinds the module engine/session to a fresh in-memory SQLite DB per test so no
live DB is touched (the global conftest tripwire also guards this).
"""

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch):
    from database import futures_follow_db as ffdb

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(ffdb, "engine", eng)
    monkeypatch.setattr(ffdb, "db_session", sess)
    ffdb.Base.query = sess.query_property()
    ffdb.init_db()
    yield ffdb
    sess.remove()
    eng.dispose()


def test_init_db_creates_table(_isolate_db):
    cols = {c["name"] for c in inspect(_isolate_db.engine).get_columns("futures_follow_trades")}
    expected = {
        "id",
        "strategy_id",
        "mode",
        "side",
        "nifty_symbol",
        "exchange",
        "product",
        "lots",
        "quantity",
        "entry_price",
        "exit_price",
        "entry_date",
        "signal_id",
        "vol_ratio",
        "margin_inr",
        "gross_pnl",
        "charges_inr",
        "net_pnl",
        "order_id",
        "status",
        "error_message",
        "note",
        "created_at",
    }
    assert expected <= cols


def test_record_trade_inserts_entry_row(_isolate_db):
    rid = _isolate_db.record_trade(
        strategy_id=77,
        mode="sandbox",
        side="BUY",
        nifty_symbol="NIFTY26JUN24FUT",
        lots=1,
        quantity=75,
        entry_price=24000.0,
        entry_date="2026-06-10",
        signal_id="AAA",
        vol_ratio=2.0,
        margin_inr=250000.0,
        status="placed",
        order_id="OID-1",
    )
    assert rid is not None
    rows = _isolate_db.get_open_entries(strategy_id=77, entry_date="2026-06-10")
    assert len(rows) == 1
    r = rows[0]
    assert r.side == "BUY"
    assert r.nifty_symbol == "NIFTY26JUN24FUT"
    assert r.lots == 1
    assert r.quantity == 75
    assert r.product == "NRML"  # default
    assert r.exchange == "NFO"  # default


def test_record_trade_inserts_exit_row_with_pnl(_isolate_db):
    rid = _isolate_db.record_trade(
        strategy_id=77,
        mode="sandbox",
        side="SELL",
        nifty_symbol="NIFTY26JUN24FUT",
        lots=1,
        quantity=75,
        entry_price=24000.0,
        exit_price=24100.0,
        entry_date="2026-06-09",
        gross_pnl=7500.0,
        charges_inr=528.0,
        net_pnl=6972.0,
        status="placed",
        note="t+1_exit",
    )
    assert rid is not None
    # SELL rows are not "open entries".
    assert _isolate_db.get_open_entries(strategy_id=77, entry_date="2026-06-09") == []


def test_get_open_entries_filters_by_date_and_side(_isolate_db):
    for d in ("2026-06-09", "2026-06-10"):
        _isolate_db.record_trade(
            strategy_id=77,
            mode="sandbox",
            side="BUY",
            nifty_symbol="NIFTY26JUN24FUT",
            lots=1,
            quantity=75,
            entry_price=24000.0,
            entry_date=d,
            status="placed",
        )
    # A SELL on 06-10 must not show up as an open entry.
    _isolate_db.record_trade(
        strategy_id=77,
        mode="sandbox",
        side="SELL",
        nifty_symbol="NIFTY26JUN24FUT",
        lots=1,
        quantity=75,
        entry_price=24000.0,
        entry_date="2026-06-10",
        status="placed",
    )
    assert len(_isolate_db.get_open_entries(strategy_id=77, entry_date="2026-06-10")) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
