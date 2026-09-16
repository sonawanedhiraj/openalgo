"""Watched-break counterfactual on the risk monitor's batched quote poll (#730).

Pins:
- ``live_pnl(watched=...)``: the watched contracts ride the ONE batched quote
  call (idle and live branches), are reported under ``watched`` and never in
  ``trades`` / ``portfolio_mtm``; a call carrying them bypasses the cache read;
- ``_register_watched_breaks``: tracks a broken, never-entered watched name once,
  never a triggered one, and drops a name the poll it triggers;
- ``_track_watched``: first quote = entry mark, later quotes extend the path;
- ``_stamp_watched_live`` at the flatten: one journal row per tracked name,
  ``cf_source='live'``, bid/ask at both legs, the ``watched_counterfactual``
  event with ``source: live``, tracker cleared; a name without a quote is left
  to the bars pass, which then reports ``already`` for the live-priced one;
- ``_risk_tick`` hands the tracker to ``live_pnl`` and the stop rule never sees a
  watched mark;
- both row builders carry ``wcf_source``.
"""

from __future__ import annotations

import pytest

from database import open15_breakout_db as o15db
from services import open15_option_shadow as shadow
from services import open15_pnl_curve as curve
from services.open15_breakout_service import Open15BreakoutService

DATE = "2026-09-16"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    o15db.init_db()
    curve.clear_caches()
    monkeypatch.setattr(curve, "_today_ist", lambda: DATE)
    yield
    try:
        o15db.db_session.query(o15db.Open15Trade).delete()
        o15db.db_session.query(o15db.Open15DayLog).delete()
        o15db.db_session.commit()
    finally:
        o15db.db_session.remove()
    curve.clear_caches()


def _rows():
    try:
        return o15db.db_session.query(o15db.Open15Trade).order_by(o15db.Open15Trade.id).all()
    finally:
        o15db.db_session.remove()


class FakeCore:
    def __init__(self, snap: dict, selected: dict, entered: dict | None = None):
        self._snap = snap
        self.selected = selected
        self.entered = entered or {}
        self.gaps = {"INFY": 0.0456}
        self.watch_stats = {}

    def watch_snapshot(self):
        return self._snap


def _svc(snap, selected, entered=None) -> Open15BreakoutService:
    svc = Open15BreakoutService(order_placer=lambda mode, order: {"status": "success"})
    svc.core = FakeCore(snap, selected, entered)
    svc._log_date = DATE
    svc.day_status = "armed"
    svc.day_config = {
        **svc.day_config,
        "instrument": "atm_option",
        "watched_cf_enabled": True,
        "exit_time": "09:30",
    }
    return svc


SNAP = {
    "INFY": {
        "max_vol_ratio": 1.11,
        "max_vol_ratio_beyond": 1.11,
        "level_broken": True,
        "entered": False,
        "watch_source": "seed",
        "first_break_at": "09:17:12",
        "first_break_price": 1636.4,
    },
    "POWERGRID": {
        "max_vol_ratio": 0.9,
        "max_vol_ratio_beyond": None,
        "level_broken": False,
        "entered": False,
        "watch_source": "seed",
        "first_break_at": None,
        "first_break_price": None,
    },
    "TECHM": {
        "max_vol_ratio": 1.6,
        "max_vol_ratio_beyond": 1.6,
        "level_broken": True,
        "entered": True,
        "watch_source": "rolling",
        "first_break_at": "09:16:30",
        "first_break_price": 1558.9,
    },
}
SELECTED = {"INFY": "L", "POWERGRID": "S", "TECHM": "L"}


def _contract(symbol, side, spot, date):
    return {"symbol": f"{symbol}29SEP261640CE", "lotsize": 400, "strike": 1640, "ticksize": 0.05}


# --------------------------------------------------------------------------- #
# live_pnl carries the watched contracts on the one batch
# --------------------------------------------------------------------------- #
def test_live_pnl_marks_watched_on_the_same_batch_and_never_as_a_trade(monkeypatch):
    calls: list[list] = []

    def fake_batch(contracts):
        calls.append(list(contracts))
        return {
            "INFY29SEP261640CE": {"ltp": 24.3, "bid": 24.2, "ask": 24.4, "volume": 5, "oi": 9},
        }

    monkeypatch.setattr(curve, "_batched_quotes", fake_batch)
    # idle day (no real rows) + nothing watched: no broker call at all
    p = curve.live_pnl()
    assert p["status"] == "idle" and calls == []
    # idle day + a watched contract: ONE call, marks under `watched`, no trades
    w = [
        {
            "symbol": "INFY",
            "contract": "INFY29SEP261640CE",
            "lot": 400,
            "side": "L",
            "break_at": "09:17:12",
        }
    ]
    p = curve.live_pnl(watched=w)
    assert p["status"] == "idle" and len(calls) == 1
    assert calls[0] == [("INFY29SEP261640CE", "NFO")]
    assert p["watched"][0]["ltp"] == 24.3 and p["watched"][0]["bid"] == 24.2
    assert p["watched"][0]["volume"] == 5 and p["watched"][0]["oi"] == 9
    assert p["watched_quotes_ok"] is True
    assert "trades" not in p and "portfolio_mtm" not in p
    # a call carrying watched contracts bypasses the cache READ but refreshes it
    p2 = curve.live_pnl(watched=w)
    assert len(calls) == 2 and p2["watched"][0]["ltp"] == 24.3
    assert curve.live_pnl()["watched"][0]["ltp"] == 24.3  # page poll reads the cache
    assert len(calls) == 2


def test_live_pnl_keeps_watched_out_of_portfolio_mtm_with_open_trades(monkeypatch):
    r = o15db.Open15Trade(
        trade_date=DATE,
        symbol="MCX",
        side="S",
        mode="live",
        instrument="option",
        opt_symbol="MCX29SEP263100PE",
        opt_lot_size=225,
        quantity=225,
        entry_fill_price=79.0,
        entry_fill_qty=225,
        trigger_minute="09:20",
        trigger_second=5,
        trigger_price=3117.4,
        opt_entry_premium=79.0,
        status="open",
        fill="real",
    )
    o15db.db_session.add(r)
    o15db.db_session.commit()
    o15db.db_session.remove()
    calls: list[list] = []

    def fake_batch(contracts):
        calls.append(list(contracts))
        return {
            "MCX29SEP263100PE": {"ltp": 81.0, "bid": 80.9, "ask": 81.1, "volume": 1, "oi": 1},
            "INFY29SEP261640CE": {"ltp": 30.0, "bid": 29.9, "ask": 30.1, "volume": 1, "oi": 1},
        }

    monkeypatch.setattr(curve, "_batched_quotes", fake_batch)
    w = [
        {
            "symbol": "INFY",
            "contract": "INFY29SEP261640CE",
            "lot": 400,
            "side": "L",
            "break_at": "09:17:12",
        }
    ]
    p = curve.live_pnl(watched=w)
    assert p["status"] == "live" and len(calls) == 1
    assert ("INFY29SEP261640CE", "NFO") in calls[0] and ("MCX29SEP263100PE", "NFO") in calls[0]
    assert [t["symbol"] for t in p["trades"]] == ["MCX"]
    assert p["portfolio_mtm"] == round((80.9 - 79.0) * 225, 2)  # the bid mark of MCX only
    assert p["watched"][0]["symbol"] == "INFY" and p["watched"][0]["ltp"] == 30.0


# --------------------------------------------------------------------------- #
# registration, tracking, stamping
# --------------------------------------------------------------------------- #
def test_register_tracks_broken_never_entered_names_once(monkeypatch):
    monkeypatch.setattr(shadow, "resolve_atm_option", _contract)
    svc = _svc(SNAP, SELECTED, entered={"TECHM": {}})
    w = svc._register_watched_breaks()
    assert [x["symbol"] for x in w] == ["INFY"]  # POWERGRID never broke, TECHM triggered
    assert w[0]["contract"] == "INFY29SEP261640CE" and w[0]["lot"] == 400
    assert svc._register_watched_breaks() == w  # idempotent
    rec = svc._risk["watched_track"]["INFY"]
    assert rec["break_at"] == "09:17:12" and rec["gap_pct"] == 4.56 and rec["entry"] is None
    # INFY triggers later: dropped the next poll
    svc.core.entered["INFY"] = {}
    assert svc._register_watched_breaks() == []
    assert "INFY" not in svc._risk["watched_track"]


def test_register_respects_the_knob_and_the_instrument(monkeypatch):
    monkeypatch.setattr(shadow, "resolve_atm_option", _contract)
    svc = _svc(SNAP, SELECTED)
    svc.day_config["watched_cf_enabled"] = False
    assert svc._register_watched_breaks() == []
    svc.day_config["watched_cf_enabled"] = True
    svc.day_config["instrument"] = "stock"
    assert svc._register_watched_breaks() == []


def test_track_first_quote_is_the_entry_then_the_path_grows(monkeypatch):
    monkeypatch.setattr(shadow, "resolve_atm_option", _contract)
    svc = _svc(SNAP, SELECTED)
    svc._register_watched_breaks()
    mark = {"symbol": "INFY", "contract": "INFY29SEP261640CE", "ltp": None}
    svc._track_watched({"asof": "09:17:14", "watched": [mark]})  # no quote yet
    assert svc._risk["watched_track"]["INFY"]["entry"] is None
    svc._track_watched(
        {
            "asof": "09:17:16",
            "watched": [{**mark, "ltp": 24.3, "bid": 24.2, "ask": 24.4, "volume": 3, "oi": 7}],
        }
    )
    svc._track_watched(
        {"asof": "09:20:00", "watched": [{**mark, "ltp": 23.0, "bid": 22.9, "ask": 23.1}]}
    )
    svc._track_watched(
        {
            "asof": "09:29:58",
            "watched": [{**mark, "ltp": 25.85, "bid": 25.8, "ask": 25.9, "volume": 9, "oi": 8}],
        }
    )
    rec = svc._risk["watched_track"]["INFY"]
    assert rec["entry"] == {
        "ltp": 24.3,
        "bid": 24.2,
        "ask": 24.4,
        "volume": 3,
        "oi": 7,
        "at": "09:17:16",
    }
    assert rec["path"] == [("09:17:16", 24.3), ("09:20:00", 23.0), ("09:29:58", 25.85)]
    assert rec["last"]["ltp"] == 25.85 and rec["last"]["at"] == "09:29:58"


def test_stamp_at_the_exit_journals_and_prices_from_live_marks(monkeypatch):
    monkeypatch.setattr(shadow, "resolve_atm_option", _contract)
    svc = _svc(SNAP, SELECTED)
    svc._register_watched_breaks()
    mark = {"symbol": "INFY", "contract": "INFY29SEP261640CE"}
    for at, ltp in (("09:17:16", 24.3), ("09:20:00", 23.0), ("09:29:58", 25.85)):
        svc._track_watched(
            {
                "asof": at,
                "watched": [
                    {**mark, "ltp": ltp, "bid": ltp - 0.1, "ask": ltp + 0.1, "volume": 1, "oi": 2}
                ],
            }
        )
    svc._stamp_watched_live()
    rows = _rows()
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "INFY" and r.fill == "watched" and r.reason == "no_trigger"
    assert r.break_at == "09:17:12" and r.break_price == 1636.4 and r.gap_pct == 4.56
    assert r.opt_symbol == "INFY29SEP261640CE" and r.sim_quantity == 400 and r.quantity == 0
    assert (r.opt_entry_premium, r.opt_exit_premium) == (24.3, 25.85)
    assert r.opt_entry_bid == pytest.approx(24.2) and r.opt_exit_ask == pytest.approx(25.95)
    charges = shadow.option_round_trip_charges(24.3 * 400, 25.85 * 400)
    assert r.opt_charges_inr == charges
    assert r.opt_pnl == round((25.85 - 24.3) * 400 - charges, 2)
    assert r.cf_mae == round((23.0 - 24.3) * 400, 2) and r.cf_mae_minute == "09:20"
    assert r.cf_mfe == round((25.85 - 24.3) * 400, 2) and r.cf_mfe_minute == "09:29"
    assert r.cf_source == "live" and r.cf_exit_minute == "09:30"
    assert r.pnl is None  # never money
    assert o15db.total_realized_pnl() == 0.0
    ev = [e for e in svc.day_log if e["event"] == "watched_counterfactual"]
    assert len(ev) == 1 and ev[0]["source"] == "live" and ev[0]["pnl"] == r.opt_pnl
    assert ev[0]["entry_minute"] == "09:17:16" and ev[0]["exit_minute"] == "09:29:58"
    assert ev[0]["polls"] == 3 and ev[0]["entry_bid"] == 24.2
    assert svc._risk["watched_track"] == {}  # cleared: a second flatten stamps nothing
    svc._stamp_watched_live()
    assert len(_rows()) == 1
    # the bars pass now finds the row priced and leaves it alone
    monkeypatch.setattr(shadow, "_fetch_1m_bars", lambda *a, **k: pytest.fail("no bars call"))
    events = [
        {"event": "armed", "instrument": "atm_option", "exit_time": "09:30", "mode": "live"},
        {
            "event": "no_entry",
            "symbol": "INFY",
            "side": "L",
            "watch_source": "seed",
            "level_broken": True,
            "first_break_at": "09:17:12",
            "first_break_price": 1636.4,
        },
    ]
    res = shadow.enrich_watched(DATE, events)
    assert res["already"] == 1 and res["priced"] == 0 and len(_rows()) == 1


def test_stamp_skips_names_without_a_quote_or_that_triggered(monkeypatch):
    monkeypatch.setattr(shadow, "resolve_atm_option", _contract)
    svc = _svc(SNAP, SELECTED)
    svc._register_watched_breaks()
    # never quoted: left to the bars pass
    svc._stamp_watched_live()
    assert _rows() == [] and svc._risk["watched_track"] == {}
    # quoted, then triggered before the exit: never journaled as watched
    svc._register_watched_breaks()
    svc._track_watched(
        {
            "asof": "09:17:16",
            "watched": [{"symbol": "INFY", "contract": "INFY29SEP261640CE", "ltp": 24.3}],
        }
    )
    svc.core.entered["INFY"] = {}
    svc._stamp_watched_live()
    assert _rows() == []


def test_risk_tick_hands_the_tracker_to_live_pnl_and_stops_ignore_it(monkeypatch):
    monkeypatch.setattr(shadow, "resolve_atm_option", _contract)
    seen = {}

    def fake_live_pnl(watched=None):
        seen["watched"] = watched
        return {
            "status": "idle",
            "date": DATE,
            "asof": "09:17:16",
            "watched": [{"symbol": "INFY", "contract": "INFY29SEP261640CE", "ltp": 24.3}],
            "watched_quotes_ok": True,
        }

    monkeypatch.setattr(curve, "live_pnl", fake_live_pnl)
    svc = _svc(SNAP, SELECTED)
    svc.day_config["stop_loss_enabled"] = True
    svc.day_config["stop_loss_inr"] = 1.0  # any real mark would fire; a watched one must not
    fired = []
    monkeypatch.setattr(svc, "_exit_open_row", lambda *a, **k: fired.append(a) or True)
    svc._risk_tick()
    assert [w["symbol"] for w in seen["watched"]] == ["INFY"]
    assert svc._risk["watched_track"]["INFY"]["entry"]["ltp"] == 24.3
    assert fired == []


def test_builders_carry_the_source():
    from services.open15_log_view import CSV_COLUMNS, selection_outcomes
    from test.test_open15_log_view import _run_render_sel

    events = [
        {
            "ts": "09:16:01.000",
            "event": "selection",
            "selected": {"INFY": "L"},
            "gaps_pct": {"INFY": 4.56},
        },
        {
            "ts": "09:30:00.400",
            "event": "no_entry",
            "symbol": "INFY",
            "side": "L",
            "watch_source": "seed",
            "level_broken": True,
            "max_vol_ratio": 1.11,
            "max_vol_ratio_while_beyond": 1.11,
            "needed": 1.5,
            "first_break_at": "09:17:12",
            "first_break_price": 1636.4,
        },
        {
            "ts": "09:30:00.900",
            "event": "watched_counterfactual",
            "symbol": "INFY",
            "side": "L",
            "watch_source": "seed",
            "break_at": "09:17:12",
            "break_price": 1636.4,
            "contract": "INFY29SEP261640CE",
            "lot_size": 400,
            "entry_minute": "09:17:16",
            "entry_premium": 24.3,
            "exit_minute": "09:29:58",
            "exit_premium": 25.85,
            "gross": 620.0,
            "charges": 78.8,
            "pnl": 541.2,
            "mae": -520.0,
            "mfe": 620.0,
            "source": "live",
            "status": "priced",
        },
    ]
    py = {r["symbol"]: r for r in selection_outcomes(DATE, events)}
    assert py["INFY"]["wcf_source"] == "live" and py["INFY"]["wcf_net"] == 541.2
    assert "wcf_source" in CSV_COLUMNS
    js = _run_render_sel(events)
    assert js["INFY"]["wcfSource"] == "live" and js["INFY"]["wcfNet"] == 541.2
    journal = [
        {
            "symbol": "INFY",
            "fill": "watched",
            "reason": "no_trigger",
            "instrument": "option",
            "opt_symbol": "INFY29SEP261640CE",
            "opt_entry_premium": 24.3,
            "opt_exit_premium": 25.85,
            "opt_pnl": 541.2,
            "opt_charges_inr": 78.8,
            "cf_mae": -520.0,
            "cf_mfe": 620.0,
            "cf_source": "bars",
            "break_at": "09:17:12",
            "break_price": 1636.4,
            "quantity": 0,
            "sim_quantity": 400,
            "pnl": None,
        }
    ]
    py2 = {r["symbol"]: r for r in selection_outcomes(DATE, events[:2], journal=journal)}
    js2 = _run_render_sel(events[:2], journal)
    assert py2["INFY"]["wcf_source"] == "bars" and js2["INFY"]["wcfSource"] == "bars"
