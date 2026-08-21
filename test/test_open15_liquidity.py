"""Option-contract liquidity capture and derivations (issue #555).

The gap: #488 captured raw contract counts (``opt_entry_volume``/``opt_entry_oi``)
and then recorded that "every ex-ante metric ranked the two live trades
backwards". Lot sizes across this universe differ by ~30x, so a raw contract
count was never comparable between contracts — and the one liquidity fact that
actually costs money, the bid-ask spread a MARKET order crosses twice, was not
captured at all even though it arrives in the quote response the strategy
already fetches.

The numbers in ``test_raw_counts_rank_the_two_contracts_backwards`` are REAL,
measured from the broker on 2026-08-06 13:05 IST.
"""

import datetime as dt
import json

import pytest

from services.open15_liquidity import (
    derive,
    lots,
    parse_path,
    spread,
    spread_cost_inr,
    summarize_path,
)


# --------------------------------------------------------------------------- #
# spread — of the MID, and 0 is "absent", never a price
# --------------------------------------------------------------------------- #
def test_spread_is_measured_against_the_mid_not_the_ltp():
    """The LTP is whichever side last traded.

    Quoting the spread against it makes the same book look wider or narrower
    depending only on who traded last — a metric that moves without the market
    moving is worse than none.
    """
    s = spread(267.15, 268.95, tick_size=0.05)
    assert s["abs"] == pytest.approx(1.80)
    # mid = 268.05 -> 0.6715%. Against the LTP of 269.50 it would read 0.6679%.
    assert s["pct"] == pytest.approx(0.6715, abs=1e-3)
    assert s["ticks"] == pytest.approx(36.0)


def test_a_zero_bid_or_ask_is_absent_not_a_price():
    """OpenAlgo's broker mappers default missing quote fields to 0.

    Treated as a price, a 0 bid reports a 100%-wide spread on a contract that
    may be perfectly liquid — and it would rank as the single worst contract in
    every sort.
    """
    assert spread(0, 268.95)["pct"] is None
    assert spread(267.15, 0)["pct"] is None
    assert spread(None, None)["pct"] is None


def test_a_crossed_book_yields_no_spread_rather_than_a_negative_one():
    assert spread(269.0, 268.0)["pct"] is None


def test_spread_in_ticks_needs_the_contracts_own_tick_size():
    """Tick size is per contract — 0.05 and 0.01 were both observed on the same
    day, so a tick count computed against a constant would be wrong for one of
    them."""
    assert spread(4.69, 4.79, tick_size=0.01)["ticks"] == pytest.approx(10.0)
    assert spread(4.69, 4.79, tick_size=None)["ticks"] is None


# --------------------------------------------------------------------------- #
# the golden case: why normalization exists
# --------------------------------------------------------------------------- #
HAL = {
    "opt_lot_size": 150,
    "opt_tick_size": 0.05,
    "opt_entry_premium": 269.5,
    "opt_entry_bid": 267.15,
    "opt_entry_ask": 268.95,
    "opt_entry_volume": 689_850,
    "opt_entry_oi": 83_250,
    "quantity": 150,
}
SAIL = {
    "opt_lot_size": 4700,
    "opt_tick_size": 0.01,
    "opt_entry_premium": 4.74,
    "opt_entry_bid": 4.69,
    "opt_entry_ask": 4.79,
    "opt_entry_volume": 17_686_100,
    "opt_entry_oi": 6_471_900,
    "quantity": 4700,
}


def test_raw_counts_rank_the_two_contracts_backwards():
    """The #488 defect, reproduced from real 2026-08-06 broker data.

    On raw contract counts SAIL looks ~26x more liquid than HAL. In LOTS the two
    are comparable — and on the metric that actually costs money, SAIL is three
    times WORSE. Any ranking built on the raw columns is inverted.
    """
    assert SAIL["opt_entry_volume"] / HAL["opt_entry_volume"] > 25  # the illusion

    hal, sail = derive(HAL), derive(SAIL)
    assert hal["entry_volume_lots"] == pytest.approx(4599.0, abs=1)
    assert sail["entry_volume_lots"] == pytest.approx(3763.0, abs=1)
    # same order of magnitude once normalized — SAIL is in fact the SMALLER book
    assert sail["entry_volume_lots"] < hal["entry_volume_lots"]

    # and the cost of trading it is 3x worse
    assert hal["entry_spread_pct"] == pytest.approx(0.67, abs=0.02)
    assert sail["entry_spread_pct"] == pytest.approx(2.11, abs=0.02)
    assert sail["entry_spread_pct"] > 3 * hal["entry_spread_pct"]


def test_turnover_compares_contracts_at_different_premiums():
    """A lot of a Rs4.74 option and a lot of a Rs269 option are not the same
    risk, so lots alone still mislead across price levels."""
    hal, sail = derive(HAL), derive(SAIL)
    assert hal["entry_turnover_inr"] == pytest.approx(689_850 * 269.5, rel=1e-6)
    assert sail["entry_turnover_inr"] == pytest.approx(17_686_100 * 4.74, rel=1e-6)
    # HAL Rs18.6cr vs SAIL Rs8.4cr — SAIL is the SMALLER book by 2.2x, the
    # opposite direction to the raw counts' 26x, and the same direction as lots
    ratio = sail["entry_turnover_inr"] / hal["entry_turnover_inr"]
    assert ratio < 1, "raw counts say SAIL is 26x bigger; in rupees it is smaller"
    assert 2 < 1 / ratio < 3


def test_lots_returns_none_rather_than_zero_when_the_lot_size_is_missing():
    """'Not captured' and 'zero liquidity' are different facts. Blurring them
    makes the research rows unusable, since 0 sorts as the worst contract."""
    assert lots(689_850, None) is None
    assert lots(689_850, 0) is None
    assert lots(None, 150) is None


# --------------------------------------------------------------------------- #
# round trip + spread cost
# --------------------------------------------------------------------------- #
def test_round_trip_spread_needs_both_legs():
    row = {**HAL, "opt_exit_bid": 300.0, "opt_exit_ask": 302.0}
    d = derive(row)
    assert d["round_trip_spread_pct"] == pytest.approx(d["entry_spread_pct"] + d["exit_spread_pct"])
    # entry-only row: no round trip is claimed
    assert derive(HAL)["round_trip_spread_pct"] is None


def test_spread_cost_is_half_the_width_per_leg():
    """A mid fill is the fair reference; a market order gives up half the width
    on each side of it."""
    row = {**HAL, "opt_exit_bid": 300.0, "opt_exit_ask": 302.0}
    # entry width 1.80, exit width 2.00 -> (1.80 + 2.00) / 2 * 150
    assert spread_cost_inr(row) == pytest.approx(285.0)


def test_spread_cost_uses_the_sim_size_when_nothing_was_ordered():
    """A sim row has quantity 0 by design — pricing its spread at 0 would report
    a free trade for the rows most likely to be illiquid."""
    row = {**SAIL, "quantity": 0, "sim_quantity": 4700, "opt_exit_bid": 5.0, "opt_exit_ask": 5.1}
    assert spread_cost_inr(row) == pytest.approx((0.10 + 0.10) / 2 * 4700, rel=1e-6)


def test_spread_cost_is_none_when_a_leg_was_never_captured():
    assert spread_cost_inr(HAL) is None  # no exit book


# --------------------------------------------------------------------------- #
# OI path
# --------------------------------------------------------------------------- #
def test_the_path_says_whether_oi_was_building_or_unwinding():
    """The direction is the whole point — two endpoint snapshots give a delta
    but cannot say it was monotonic, and a 15-minute series can."""
    path = [
        {"m": "09:21", "v": 3300, "oi": 72150},
        {"m": "09:22", "v": 1500, "oi": 74000},
        {"m": "09:23", "v": 900, "oi": 83250},
    ]
    s = summarize_path(json.dumps(path), lot_size=150)
    assert s["minutes"] == 3
    assert s["oi_open"] == 72150 and s["oi_close"] == 83250
    assert s["oi_change"] == 11100
    assert s["oi_change_lots"] == pytest.approx(74.0)
    # per-bar volume is INCREMENTAL, so summing is right here; the quote's
    # cumulative day figure must never be summed the same way
    assert s["volume_traded"] == 5700


def test_an_empty_or_unreadable_path_degrades_quietly():
    for bad in (None, "", "not json", "{}", "[]"):
        s = summarize_path(bad)
        assert s["minutes"] == 0 and s["oi_change"] is None
    assert parse_path(None) == []


# --------------------------------------------------------------------------- #
# path extraction from the bars the option-shadow already fetches
# --------------------------------------------------------------------------- #
def _bar(h, m, volume, oi):
    import pytz

    ts = pytz.timezone("Asia/Kolkata").localize(dt.datetime(2026, 8, 6, h, m))
    return {"timestamp": int(ts.timestamp()), "open": 1, "close": 1, "volume": volume, "oi": oi}


def test_path_covers_only_the_hold_window():
    """Bars before the trigger and after the exit describe someone else's trade."""
    from services.open15_option_shadow import liquidity_path_from_bars

    bars = [
        _bar(9, 16, 100, 1000),  # before the trigger
        _bar(9, 21, 3300, 72150),
        _bar(9, 22, 1500, 74000),
        _bar(9, 30, 900, 83250),
        _bar(9, 45, 5000, 90000),  # after the exit
    ]
    path = liquidity_path_from_bars(bars, "09:21", "09:30")
    assert [p["m"] for p in path] == ["09:21", "09:22", "09:30"]
    assert path[0] == {"m": "09:21", "v": 3300, "oi": 72150}


def test_path_survives_bars_missing_oi_and_bad_timestamps():
    """Fail-graceful: this runs inside a scheduler job that must not raise."""
    from services.open15_option_shadow import liquidity_path_from_bars

    bars = [
        {"timestamp": "garbage", "volume": 1, "oi": 1},
        {**_bar(9, 21, 3300, 72150), "oi": None},
        _bar(9, 22, 1500, 74000),
    ]
    path = liquidity_path_from_bars(bars, "09:21", "09:30")
    assert len(path) == 2
    assert "oi" not in path[0] and path[1]["oi"] == 74000
    assert liquidity_path_from_bars(bars, "nonsense", "09:30") == []


# --------------------------------------------------------------------------- #
# capture — bid/ask ride in a response the strategy ALREADY fetches
# --------------------------------------------------------------------------- #
def test_the_quote_snapshot_passes_bid_and_ask_through(monkeypatch):
    """They were always in this payload; open15 simply dropped them.

    A zero from the mapper means "absent" and must not become a price.
    """
    import sys

    import services.open15_breakout_service as svc

    payload = {"data": {"ltp": 269.5, "volume": 689850, "oi": 83250, "bid": 267.15, "ask": 268.95}}
    monkeypatch.setitem(
        sys.modules,
        "services.quotes_service",
        type("M", (), {"get_quotes": staticmethod(lambda *a, **k: (True, payload, 200))}),
    )
    monkeypatch.setitem(
        sys.modules,
        "database.auth_db",
        type("M", (), {"get_first_available_api_key": staticmethod(lambda: "k")}),
    )
    snap = svc.production_quote_snapshot("HAL25AUG264750CE", "NFO")
    assert snap["bid"] == 267.15 and snap["ask"] == 268.95

    payload["data"]["bid"] = 0
    snap = svc.production_quote_snapshot("HAL25AUG264750CE", "NFO")
    assert snap["bid"] is None, "0 is the mapper's absent, not a price"


def test_option_liquidity_returns_a_dict_so_new_fields_cannot_mis_bind():
    """It used to return a 3-tuple. Adding a field to a positional unpack
    silently rebinds values at any call site that was not updated — with prices
    and quantities in the same tuple, that is a P&L-corrupting failure."""
    from services.open15_breakout_service import Open15BreakoutService

    s = Open15BreakoutService(
        quote_fn=lambda sym, exch: 12.5,
        quote_snapshot_fn=lambda sym, exch: {
            "ltp": 12.5,
            "volume": 100,
            "oi": 200,
            "bid": 12.4,
            "ask": 12.6,
        },
    )
    assert s._option_liquidity("X") == {
        "ltp": 12.5,
        "volume": 100,
        "oi": 200,
        "bid": 12.4,
        "ask": 12.6,
    }
    # snapshot unavailable -> price-only fallback, liquidity simply unknown
    s.quote_snapshot_fn = None
    assert s._option_liquidity("X") == {
        "ltp": 12.5,
        "volume": None,
        "oi": None,
        "bid": None,
        "ask": None,
    }


def test_the_book_is_captured_at_BOTH_ends_of_a_sim_row(monkeypatch):
    """End-to-end through the real tick pipeline.

    The sim rows are the ones most likely to be illiquid — they exist because
    the contract was unaffordable — so capturing the book only at entry would
    miss half the cost on exactly the population the data is for. Exercised on
    the sim path specifically because it flattens through ``_flatten_paper``,
    a different function from the real ``flatten``.
    """
    from database.open15_breakout_db import Open15Trade, db_session, init_db
    from test.test_open15_fill_reconcile import (
        _mk_option_service,
        _run_to_selection,
        _trigger,
    )

    init_db()
    db_session.query(Open15Trade).delete()
    db_session.commit()

    monkeypatch.setattr(
        "services.open15_option_shadow.resolve_atm_option",
        lambda *a, **k: {
            "symbol": "AAA25AUG26100CE",
            "strike": 100,
            "lotsize": 650,
            "ticksize": 0.05,
        },
    )
    books = [
        {"ltp": 44.0, "volume": 299000, "oi": 564200, "bid": 43.75, "ask": 44.25},
        {"ltp": 45.8, "volume": 412000, "oi": 601000, "bid": 45.55, "ask": 46.05},
    ]
    orders = []
    svc = _mk_option_service(orders, premiums=[])
    svc.quote_snapshot_fn = lambda sym, exch: books.pop(0) if books else None
    _run_to_selection(svc)
    _trigger(svc)
    svc.flatten()

    assert orders == [], "a sim row must never place an order"
    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row.fill == "sim" and row.reason == "unaffordable"
    assert (row.opt_entry_bid, row.opt_entry_ask) == (43.75, 44.25)
    assert (row.opt_exit_bid, row.opt_exit_ask) == (45.55, 46.05)
    assert row.opt_tick_size == 0.05
    assert (row.opt_exit_volume, row.opt_exit_oi) == (412000, 601000)

    d = derive(row)
    assert d["entry_spread_pct"] == pytest.approx(0.5 / 44.0 * 100, abs=0.01)
    assert d["round_trip_spread_pct"] is not None
    # OI built while the trade would have been held
    assert d["oi_change"] == 601000 - 564200
    # priced on the sim size, since `quantity` is 0 by design on this row
    assert spread_cost_inr(row) == pytest.approx((0.5 + 0.5) / 2 * 650)
    db_session.remove()


def test_a_pre_555_row_derives_all_none_not_zero():
    """Every row written before this shipped has no bid/ask. They must read as
    'not captured', or the whole history reports perfectly tight spreads."""
    old = {"opt_lot_size": 150, "opt_entry_volume": 689_850, "opt_entry_oi": 83_250}
    d = derive(old)
    assert d["entry_spread_pct"] is None and d["round_trip_spread_pct"] is None
    # the columns that DID exist still derive
    assert d["entry_volume_lots"] == pytest.approx(4599.0, abs=1)
    assert d["volume_to_oi"] == pytest.approx(8.29, abs=0.01)
