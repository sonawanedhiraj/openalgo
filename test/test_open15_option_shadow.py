"""Tests for the ATM option shadow-trade pricing (issue #435)."""

import datetime as dt

import pytz

from services.open15_option_shadow import (
    option_round_trip_charges,
    pick_contract,
    premiums_from_bars,
)

IST = pytz.timezone("Asia/Kolkata")


def _bar(h, m, o, c, day=22):
    ts = IST.localize(dt.datetime(2026, 7, day, h, m)).timestamp()
    return {"timestamp": int(ts), "open": o, "high": max(o, c), "low": min(o, c), "close": c}


def test_pick_contract_nearest_strike_and_expiry():
    cands = [
        {"symbol": "AAA28JUL2610700CE", "strike": 10700.0, "expiry": "28-JUL-26", "lotsize": 75},
        {"symbol": "AAA28JUL2610800CE", "strike": 10800.0, "expiry": "28-JUL-26", "lotsize": 75},
        {"symbol": "AAA25AUG2610700CE", "strike": 10700.0, "expiry": "25-AUG-26", "lotsize": 75},
        {"symbol": "AAA30JUN2610700CE", "strike": 10700.0, "expiry": "30-JUN-26", "lotsize": 75},
    ]
    # expired 30-JUN is dropped; nearest alive expiry is 28-JUL; nearest strike 10700
    got = pick_contract(cands, spot=10699.5, trade_date="2026-07-22")
    assert got["symbol"] == "AAA28JUL2610700CE"
    # spot near 10790 -> 10800 strike wins
    got = pick_contract(cands, spot=10790.0, trade_date="2026-07-22")
    assert got["symbol"] == "AAA28JUL2610800CE"
    # all expired -> None
    assert pick_contract(cands, spot=10700.0, trade_date="2026-09-30") is None


def test_option_charges_model():
    # BAJAJ-AUTO 22-Jul real numbers: buy 152x75=11400, sell 170x75=12750
    c = option_round_trip_charges(11_400.0, 12_750.0)
    # brokerage 40 + txn 0.3503% x 24150 = 84.60 + STT 7.97 + GST 22.43 + stamp 0.34 -> ~155
    assert c is not None and 150.0 < c < 160.0
    assert option_round_trip_charges(0.0, 12_750.0) is None


def test_premiums_entry_next_minute_open_exit_0930_open():
    bars = [
        _bar(9, 20, 129.55, 150.85),
        _bar(9, 21, 152.00, 186.00),
        _bar(9, 29, 186.30, 170.00),
        _bar(9, 30, 170.00, 154.20),
    ]
    entry, exit_p = premiums_from_bars(bars, "09:20")
    assert entry == 152.00  # open of the minute AFTER the trigger minute
    assert exit_p == 170.00  # 09:30 bar open


def test_premiums_fallbacks_and_missing():
    # no next-minute bar -> trigger-minute close; no 09:30 bar -> last close before
    bars = [_bar(9, 20, 129.55, 150.85), _bar(9, 29, 186.30, 170.00)]
    entry, exit_p = premiums_from_bars(bars, "09:20")
    assert entry == 150.85 and exit_p == 170.00
    # empty bars / bad trigger -> None
    assert premiums_from_bars([], "09:20") == (None, None)
    assert premiums_from_bars(bars, None) == (None, None)


def test_enrich_missing_prices_closed_rows(monkeypatch):
    from database.open15_breakout_db import Open15Trade, db_session, init_db, insert_trade

    init_db()
    row_id = insert_trade(
        trade_date="2026-07-22",
        symbol="BAJAJ-AUTO",
        side="L",
        mode="sandbox",
        trigger_minute="09:20",
        trigger_second=59,
        trigger_price=10699.5,
        quantity=14,
        status="closed",
    )
    assert row_id is not None

    import services.open15_option_shadow as shadow

    monkeypatch.setattr(
        shadow,
        "resolve_atm_option",
        lambda underlying, side, spot, trade_date: {
            "symbol": "BAJAJ-AUTO28JUL2610700CE",
            "strike": 10700.0,
            "expiry": "28-JUL-26",
            "lotsize": 75,
        },
    )
    monkeypatch.setattr(
        shadow,
        "_fetch_1m_bars",
        lambda symbol, trade_date: [
            _bar(9, 20, 129.55, 150.85),
            _bar(9, 21, 152.00, 186.00),
            _bar(9, 30, 170.00, 154.20),
        ],
    )
    res = shadow.enrich_missing()
    assert res["status"] == "ok" and res["priced"] >= 1

    row = db_session.query(Open15Trade).filter(Open15Trade.id == row_id).first()
    assert row.opt_symbol == "BAJAJ-AUTO28JUL2610700CE"
    assert row.opt_entry_premium == 152.00 and row.opt_exit_premium == 170.00
    assert row.opt_lot_size == 75
    # gross (170-152)*75 = 1350, minus ~155 charges -> ~1195 net
    assert 1180.0 < row.opt_pnl < 1210.0
    db_session.remove()

    # idempotent: second run finds nothing to price
    res2 = shadow.enrich_missing()
    assert res2["priced"] == 0
