"""Profit-trail exits keep being marked to the scheduled exit, exactly like a
stop-loss exit (issue #713 — extends #704).

The chart on ``/open15_vol_breakout/logs`` used to stop dead at a profit-trail
exit while a stop-loss row continued dashed to the scheduled exit: every gate
in the pipeline read ``reason == 'stop_loss'`` and the trail flatten journals
``reason='profit_trail'``. Pins:

- ``risk_exit_rows`` covers BOTH reasons; ``stop_loss_rows`` (the scorecard's
  population, with its pre-registered decision rule) stays stop-only,
- the curve payload: a ``profit_trail`` row gets a ghost series + the
  ``portfolio_no_stop`` twin, flagged ``profit_trail``/``risk_exit`` and NOT
  ``stop_loss`` so the page can draw a different marker,
- ``live_pnl`` ghosts ride the batch for a trail row and say which rule fired,
- the risk monitor keeps recording ghost marks AFTER ``trail_done`` — the
  early return the trail sets must not switch the counterfactual off,
- the live stamp at ``flatten`` and the bars backfill price a trail row on the
  same convention, with the ``stop_counterfactual`` event carrying ``reason``.
"""

from __future__ import annotations

import pytest

from database import open15_breakout_db as o15db
from services import open15_pnl_curve as curve
from services import open15_sl_counterfactual as slcf
from test.test_open15_sl_counterfactual import (  # noqa: F401 — autouse fixture
    CLOSES,
    DATE,
    LATER,
    _fresh,
    _get,
    _patch_bars,
    _patch_live,
    _row,
)


def _trail_row(**kw) -> int:
    """A REAL long-CE row the profit trail flattened at 09:22 for +3000 gross."""
    defaults = {
        "symbol": "IDEA",
        "side": "L",
        "opt_symbol": "IDEA29SEP2615CE",
        "pnl": 3000.0,
        "reason": "profit_trail",
    }
    defaults.update(kw)
    return _row(**defaults)


class TestRows:
    def test_risk_exit_rows_covers_both_and_stop_loss_rows_stays_stop_only(self):
        _row()  # stop_loss
        _trail_row()
        _row(symbol="HELD", reason="eod_0930")
        _trail_row(symbol="PAPER", fill="paper")
        assert {r.symbol for r in o15db.risk_exit_rows()} == {"BAJAJ-AUTO", "IDEA"}
        assert [r.symbol for r in o15db.risk_exit_rows(unpriced_only=True)] == [
            "BAJAJ-AUTO",
            "IDEA",
        ]
        assert [r.symbol for r in o15db.stop_loss_rows()] == ["BAJAJ-AUTO"]
        assert o15db.RISK_EXIT_REASONS == ("stop_loss", "profit_trail")

    def test_scorecard_never_counts_a_trail_exit(self):
        _trail_row(cf_pnl=-5250.0, cf_charges_inr=130.0, cf_source="bars")
        j = slcf.scorecard()
        assert j["n_events"] == 0 and j["events"] == []

    def test_saved_reads_the_same_for_a_trail_row(self):
        # booked net = 3000 - 122 = 2878 ; held net = -5250 - 130 = -5380
        row = _get(_trail_row(cf_pnl=-5250.0, cf_charges_inr=130.0))
        assert o15db.stop_saved_of_row(row) == pytest.approx(2878.0 + 5380.0)


class TestCurve:
    def test_trail_row_continues_dashed_to_the_scheduled_exit(self, monkeypatch):
        _trail_row()
        monkeypatch.setattr(curve, "_today_ist", lambda: LATER)
        _patch_bars(monkeypatch, {"IDEA29SEP2615CE": CLOSES})
        j = curve.build_pnl_curve(DATE)
        assert j["status"] == "ok" and j["n_stopped"] == 1
        (t,) = j["trades"]
        assert t["profit_trail"] is True and t["risk_exit"] is True
        assert t["stop_loss"] is False  # the page draws a different marker
        # real marks end before the exit; the ghost runs from the exit
        # minute's bar to the last bar before the scheduled exit (long sign)
        assert dict(t["series"]) == {"09:21": pytest.approx(-1500.0)}
        g = dict(t["ghost"])
        assert list(g)[0] == "09:22" and list(g)[-1] == "09:29"
        assert g["09:24"] == pytest.approx(-6000.0) and "09:30" not in g
        assert t["ghost_final"] == ["09:30", pytest.approx(-5250.0)]
        assert t["cf_source"] == "curve" and t["stop_saved"] is None
        # the held-portfolio twin exists on a trail day too
        assert dict(j["portfolio_no_stop"])["09:24"] == pytest.approx(-6000.0)
        assert j["portfolio_final"] == ["09:22", pytest.approx(3000.0)]  # the real exit
        assert j["portfolio_no_stop_final"] == ["09:30", pytest.approx(-5250.0)]

    def test_scheduled_flatten_row_still_has_no_ghost(self, monkeypatch):
        _trail_row(reason="eod_0930", exit_ts=f"{DATE}T09:30:01+05:30")
        monkeypatch.setattr(curve, "_today_ist", lambda: LATER)
        _patch_bars(monkeypatch, {"IDEA29SEP2615CE": CLOSES})
        j = curve.build_pnl_curve(DATE)
        (t,) = j["trades"]
        assert t["risk_exit"] is False and t["ghost"] == [] and j["n_stopped"] == 0


class TestLive:
    def test_trail_ghost_rides_the_batch_and_names_its_rule(self, monkeypatch):
        _trail_row()
        calls = _patch_live(monkeypatch, {"IDEA29SEP2615CE": 210.0})
        j = curve.live_pnl()
        assert j["status"] == "closed" and calls == [["IDEA29SEP2615CE"]]
        (g,) = j["ghost"]
        assert g["reason"] == "profit_trail" and g["row_id"] == o15db.risk_exit_rows()[0].id
        assert g["mtm"] == pytest.approx((210.0 - 200.0) * 150)

    def test_ghost_leaves_once_stamped(self, monkeypatch):
        rid = _trail_row(cf_pnl=-1.0)
        _patch_live(monkeypatch, {"IDEA29SEP2615CE": 210.0})
        assert "ghost" not in curve.live_pnl()
        assert _get(rid).cf_pnl == -1.0

    def test_risk_tick_keeps_tracking_ghosts_after_trail_done(self, monkeypatch):
        """``trail_done`` short-circuits the rules — it must not stop the
        counterfactual: the marks are recorded before that early return."""
        from test.test_open15_risk_controls import _live, _mk_service

        svc, orders = _mk_service(
            risk_cfg={
                "profit_lock_enabled": True,
                "profit_target_inr": 2000,
                "trail_giveback_inr": 500,
            }
        )
        svc._log_date = DATE
        svc._risk["trail_done"] = True
        payload = _live([])
        payload["status"] = "closed"
        payload["asof"] = "09:25:10"
        payload["ghost"] = [
            {
                "row_id": 9,
                "symbol": "IDEA",
                "contract": "X",
                "ltp": 215.0,
                "mtm": 2250.0,
                "reason": "profit_trail",
            }
        ]
        import services.open15_breakout_service as svc_mod

        monkeypatch.setattr(curve, "live_pnl", lambda: payload)
        monkeypatch.setattr(svc_mod, "live_pnl", lambda: payload, raising=False)
        svc._risk_tick()
        assert orders == []
        assert svc._risk["ghost_path"][9] == [("09:25:10", 2250.0)]
        assert svc._risk["ghost_last"][9] == 215.0


class TestStamp:
    def test_flatten_stamps_a_trail_row_live_with_its_reason(self, monkeypatch):
        from test.test_open15_risk_controls import _mk_service

        rid = _trail_row()
        svc, _orders = _mk_service()
        svc._log_date = DATE
        monkeypatch.setattr(svc, "_trade_date", lambda: DATE)
        svc._risk["ghost_path"][rid] = [("09:24:05", 2400.0), ("09:28:00", 900.0)]
        svc._risk["ghost_last"][rid] = 204.0
        svc._stamp_ghost_counterfactuals()
        row = _get(rid)
        assert row.cf_source == "live" and row.cf_exit_minute == "09:30"
        assert row.cf_pnl == pytest.approx((204.0 - 200.0) * 150)
        assert row.cf_mfe == pytest.approx(2400.0) and row.cf_mae == pytest.approx(600.0)
        (ev,) = [e for e in svc.day_log if e["event"] == "stop_counterfactual"]
        assert ev["symbol"] == "IDEA" and ev["reason"] == "profit_trail"
        # booked +2878 net vs held +600 - charges: the trail saved money
        assert (
            ev["stop_saved"] == pytest.approx(o15db.stop_saved_of_row(row)) and ev["stop_saved"] > 0
        )

    def test_backfill_prices_a_trail_row_from_bars(self, monkeypatch):
        rid = _trail_row()
        _patch_bars(monkeypatch, {"IDEA29SEP2615CE": CLOSES})
        res = slcf.backfill_missing(DATE)
        assert (res["checked"], res["priced"], res["pending"]) == (1, 1, 0)
        row = _get(rid)
        assert row.cf_source == "bars" and row.cf_pnl == pytest.approx(-5250.0)
        assert row.cf_mae == pytest.approx(-6000.0) and row.cf_mae_minute == "09:24"
