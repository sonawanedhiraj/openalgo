"""Tests for the ATM option shadow-trade pricing (issue #435)."""

import datetime as dt

import pytz

from services.open15_option_shadow import (
    is_expiry_blocked,
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


def test_is_expiry_blocked_window():
    """Zerodha's physical-delivery block (issue #669): the expiry day and the
    trading day before it, nothing else — and an already-expired contract is
    NOT this check's job (aliveness is filtered separately)."""
    exp = dt.date(2026, 8, 25)  # Tuesday — the Aug 2026 stock-F&O expiry
    assert is_expiry_blocked(exp, dt.date(2026, 8, 24))  # Monday before
    assert is_expiry_blocked(exp, dt.date(2026, 8, 25))  # expiry day
    assert not is_expiry_blocked(exp, dt.date(2026, 8, 21))  # Friday before
    assert not is_expiry_blocked(exp, dt.date(2026, 8, 19))  # mid-cycle
    assert not is_expiry_blocked(exp, dt.date(2026, 8, 26))  # expired != blocked


def test_is_expiry_blocked_holiday_shifts_window(monkeypatch):
    """With the Monday a market holiday, the trading day before a Tuesday
    expiry is the Friday — the window must follow the calendar, not weekdays."""
    import services.data_freshness_service as dfs

    monkeypatch.setattr(
        dfs,
        "is_trading_day",
        lambda d, exchange=None: d.weekday() < 5 and d != dt.date(2026, 8, 24),
    )
    exp = dt.date(2026, 8, 25)
    assert is_expiry_blocked(exp, dt.date(2026, 8, 21))  # Friday is now blocked
    assert not is_expiry_blocked(exp, dt.date(2026, 8, 24))  # holiday — closed anyway


def test_pick_contract_rolls_in_expiry_block_window():
    """Golden incident 2026-08-24 (issue #669): Monday before the Tuesday
    expiry — Zerodha rejected all 4 live entries on the front month. The pick
    must roll to September and say so."""
    cands = [
        {"symbol": "BIOCON25AUG26410PE", "strike": 410.0, "expiry": "25-AUG-26", "lotsize": 2500},
        {"symbol": "BIOCON25AUG26415PE", "strike": 415.0, "expiry": "25-AUG-26", "lotsize": 2500},
        {"symbol": "BIOCON29SEP26410PE", "strike": 410.0, "expiry": "29-SEP-26", "lotsize": 2500},
    ]
    got = pick_contract(cands, spot=410.5, trade_date="2026-08-24")
    assert got["symbol"] == "BIOCON29SEP26410PE"
    assert got["expiry_rolled"] is True and got["rolled_from"] == "2026-08-25"
    # expiry day itself is blocked too
    got = pick_contract(cands, spot=410.5, trade_date="2026-08-25")
    assert got["symbol"] == "BIOCON29SEP26410PE"
    # the Friday before is OUTSIDE the window — front month, no roll marker
    got = pick_contract(cands, spot=410.5, trade_date="2026-08-21")
    assert got["symbol"] == "BIOCON25AUG26410PE"
    assert "expiry_rolled" not in got


def test_pick_contract_fails_open_when_every_expiry_is_blocked():
    """Master contract with no later month: return the front month un-rolled —
    a rejected attempt lands in the #548 paper path, which beats resolving
    nothing at all."""
    cands = [
        {"symbol": "AAA25AUG26100CE", "strike": 100.0, "expiry": "25-AUG-26", "lotsize": 100},
    ]
    got = pick_contract(cands, spot=100.0, trade_date="2026-08-24")
    assert got["symbol"] == "AAA25AUG26100CE"
    assert "expiry_rolled" not in got


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
