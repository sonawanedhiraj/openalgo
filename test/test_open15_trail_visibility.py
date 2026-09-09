"""Profit trail: bid marks, net evaluation, breach confirmation, and the trail
as data on the intra-hold chart (issue #716).

On 2026-09-09 the #696 trail locked at 09:21:22 and flattened at 09:21:28:
one MCX print at 113.40 (bid 109.80) lifted the peak by Rs1,600 and the next
poll tripped the floor by Rs120. The day log recorded nothing in between.
These tests pin the four rule changes and the logging that makes the trail
replayable:

- ``mark_for_row``: the touch (bid for a long premium, ask for a short stock),
  LTP only as a labelled fallback;
- ``live_pnl`` carries gross AND net (modelled exit charges) with the basis;
- the day rule reads NET, needs ``trail_confirm_polls`` consecutive breaches,
  resets the count on recovery, and names the mark that moved the peak;
- ``risk_path`` is persisted ONCE; ``risk_from_day_log`` rebuilds the overlay
  from it — or from the lock/exit events alone for a pre-#716 day.
"""

from __future__ import annotations

import pytest

from services import open15_pnl_curve as curve
from services.open15_breakout_service import (
    _resolve_risk_config,
    clamp_trail_confirm_polls,
    resolve_day_config,
)
from test.test_open15_risk_controls import (  # noqa: F401 — autouse journal fixture
    DATE,
    _clean_journal,
    _events,
    _mk_service,
)


class _Row:
    def __init__(self, **kw):
        self.symbol = kw.get("symbol", "MCX")
        self.side = kw.get("side", "L")
        self.instrument = kw.get("instrument", "option")
        self.opt_symbol = kw.get("opt_symbol", "MCX29SEP263400CE")
        self.entry_fill_price = kw.get("entry_fill_price")
        self.opt_entry_premium = kw.get("opt_entry_premium")
        self.trigger_price = kw.get("trigger_price")


# --------------------------------------------------------------------------- #
# marks
# --------------------------------------------------------------------------- #
class TestMarkForRow:
    def test_long_premium_marks_at_bid(self):
        px, basis = curve.mark_for_row(_Row(), {"ltp": 113.40, "bid": 109.80, "ask": 110.2})
        assert (px, basis) == (109.80, "bid")

    def test_ltp_fallback_is_labelled(self):
        px, basis = curve.mark_for_row(_Row(), {"ltp": 113.40, "bid": None, "ask": None})
        assert (px, basis) == (113.40, "ltp")

    def test_no_quote(self):
        assert curve.mark_for_row(_Row(), None) == (None, "none")

    def test_short_stock_marks_at_ask(self):
        row = _Row(instrument="stock", side="S", trigger_price=100.0)
        px, basis = curve.mark_for_row(row, {"ltp": 99.0, "bid": 98.9, "ask": 99.1})
        assert (px, basis) == (99.1, "ask")


class TestChargeEstimate:
    def test_option_uses_the_journal_helper(self):
        from services.open15_option_shadow import option_round_trip_charges

        row = _Row(entry_fill_price=104.75)
        est = curve.exit_charges_estimate(row, 104.75, 109.80, 450)
        assert est == option_round_trip_charges(104.75 * 450, 109.80 * 450)
        assert est and est > 0

    def test_stock_uses_mis_helper(self):
        from services.open15_breakout_service import mis_round_trip_charges

        row = _Row(instrument="stock", side="L", trigger_price=100.0)
        assert curve.exit_charges_estimate(row, 100.0, 101.0, 50) == mis_round_trip_charges(
            5000.0, 5050.0
        )

    def test_missing_inputs_are_none(self):
        assert curve.exit_charges_estimate(_Row(), None, 1.0, 1) is None
        assert curve.exit_charges_estimate(_Row(), 1.0, 1.0, 0) is None


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
class TestConfirmConfig:
    def test_default_is_two_polls(self, monkeypatch):
        monkeypatch.delenv("OPEN15_TRAIL_CONFIRM_POLLS", raising=False)
        assert resolve_day_config(None, 0.0)["trail_confirm_polls"] == 2

    def test_clamp(self):
        assert clamp_trail_confirm_polls(0) == 1
        assert clamp_trail_confirm_polls(9) == 5
        assert clamp_trail_confirm_polls("3") == 3
        assert clamp_trail_confirm_polls("x", 4) == 4

    def test_stored_row_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("OPEN15_TRAIL_CONFIRM_POLLS", "4")
        assert _resolve_risk_config({"trail_confirm_polls": 1})["trail_confirm_polls"] == 1
        assert _resolve_risk_config({})["trail_confirm_polls"] == 4


# --------------------------------------------------------------------------- #
# the day rule
# --------------------------------------------------------------------------- #
def _payload(trades, asof="09:21:22", quotes_ok=True):
    """A #716-shaped live payload: gross + net + basis per trade."""
    gross = 0.0
    net = 0.0
    chg = 0.0
    out = []
    for t in trades:
        mtm = float(t["mtm"])
        c = float(t.get("charges_est", 0.0))
        gross += mtm
        net += mtm - c
        chg += c
        out.append(
            {
                "symbol": t["symbol"],
                "mark": t.get("mark"),
                "mark_basis": t.get("mark_basis", "bid"),
                "ltp": t.get("ltp", t.get("mark")),
                "bid": t.get("mark"),
                "ask": None,
                "mtm": mtm,
                "charges_est": c,
                "mtm_net": mtm - c,
            }
        )
    return {
        "status": "live",
        "date": DATE,
        "asof": asof,
        "quotes_ok": quotes_ok,
        "trades": out,
        "portfolio_mtm": round(gross, 2),
        "portfolio_mtm_net": round(net, 2),
        "charges_est_total": round(chg, 2),
        "mark_basis": "bid",
        "poll_interval_s": 2,
    }


def _svc(monkeypatch, payload, confirm=2, **risk):
    risk_cfg = {
        "profit_lock_enabled": True,
        "profit_target_inr": 5000,
        "trail_giveback_inr": 1500,
        "trail_confirm_polls": confirm,
        **risk,
    }
    svc, orders = _mk_service(risk_cfg=risk_cfg)
    monkeypatch.setattr(curve, "live_pnl", lambda: payload)
    svc._today_realized_net = lambda: 0.0
    flattened: list[str] = []
    svc._profit_trail_exits = lambda reason: flattened.append(reason)
    return svc, flattened


def _set(monkeypatch, payload):
    monkeypatch.setattr(curve, "live_pnl", lambda: payload)


class TestBreachConfirmation:
    def test_breach_must_hold_for_confirm_polls(self, monkeypatch):
        svc, flattened = _svc(monkeypatch, _payload([{"symbol": "A", "mtm": 6100}]))
        svc._risk_tick()  # lock: peak 6100, floor 4600
        assert svc._risk["locked"] and svc._risk["floor"] == 4600.0
        _set(monkeypatch, _payload([{"symbol": "A", "mtm": 4580}], asof="09:21:25"))
        svc._risk_tick()  # breach 1/2 — no exit yet
        assert not svc._risk["trail_done"]
        assert flattened == []
        assert svc._risk["breach_polls"] == 1
        (br,) = _events(svc, "profit_trail_breach")
        assert br["polls_required"] == 2 and br["shortfall"] == 20.0
        _set(monkeypatch, _payload([{"symbol": "A", "mtm": 4500}], asof="09:21:28"))
        svc._risk_tick()  # breach 2/2 — flatten
        assert svc._risk["trail_done"]
        assert flattened == ["profit_trail"]
        (ex,) = _events(svc, "profit_trail_exit")
        assert ex["breach_polls"] == 2 and ex["polls_required"] == 2
        assert ex["polls_since_lock"] == 2
        assert ex["mark_basis"] == "bid"
        assert ex["net"] == 4500.0
        # the breach event is logged once, not per confirming poll
        assert len(_events(svc, "profit_trail_breach")) == 1

    def test_recovery_resets_the_count(self, monkeypatch):
        svc, flattened = _svc(monkeypatch, _payload([{"symbol": "A", "mtm": 6100}]))
        svc._risk_tick()
        _set(monkeypatch, _payload([{"symbol": "A", "mtm": 4580}], asof="09:21:25"))
        svc._risk_tick()
        assert svc._risk["breach_polls"] == 1
        _set(monkeypatch, _payload([{"symbol": "A", "mtm": 5000}], asof="09:21:28"))
        svc._risk_tick()  # back above the floor
        assert svc._risk["breach_polls"] == 0
        assert [p["action"] for p in svc._risk["path"]][-1] == "recovered"
        _set(monkeypatch, _payload([{"symbol": "A", "mtm": 4580}], asof="09:21:31"))
        svc._risk_tick()  # a fresh breach starts from 1 again
        assert svc._risk["breach_polls"] == 1
        assert not svc._risk["trail_done"] and flattened == []

    def test_confirm_one_is_the_old_single_poll_behaviour(self, monkeypatch):
        svc, flattened = _svc(monkeypatch, _payload([{"symbol": "A", "mtm": 6100}]), confirm=1)
        svc._risk_tick()
        _set(monkeypatch, _payload([{"symbol": "A", "mtm": 4580}], asof="09:21:25"))
        svc._risk_tick()
        assert svc._risk["trail_done"] and flattened == ["profit_trail"]


class TestNetEvaluation:
    def test_lock_reads_net_not_gross(self, monkeypatch):
        # 2026-09-09 09:21:22: Rs6,395 gross, ~Rs1,020 of modelled exit charges
        svc, _ = _svc(
            monkeypatch,
            _payload(
                [
                    {"symbol": "MCX", "mtm": 2295, "charges_est": 477},
                    {"symbol": "BIOCON", "mtm": 4100, "charges_est": 543},
                ]
            ),
            profit_target_inr=5500,
        )
        svc._risk_tick()
        assert not svc._risk["locked"]
        assert svc._risk["last_day_pnl"] == 5375.0
        assert svc._risk["last_day_pnl_gross"] == 6395.0
        assert _events(svc, "profit_target_locked") == []
        # the below-target poll is still on the path (how close it came)
        assert svc._risk["path"][-1]["action"] == "below_target"

    def test_lock_event_carries_both_numbers_and_marks(self, monkeypatch):
        svc, _ = _svc(
            monkeypatch,
            _payload([{"symbol": "A", "mtm": 7000, "charges_est": 500, "mark": 12.0, "ltp": 12.3}]),
        )
        svc._risk_tick()
        (ev,) = _events(svc, "profit_target_locked")
        assert ev["net"] == 6500.0 and ev["gross"] == 7000.0
        assert ev["floor"] == 5000.0 and ev["floor_gross"] == 5500.0
        assert ev["confirm_polls"] == 2 and ev["mark_basis"] == "bid"
        assert ev["marks"]["A"]["mark"] == 12.0 and ev["marks"]["A"]["basis"] == "bid"

    def test_payload_without_net_field_falls_back_to_gross(self, monkeypatch):
        from test.test_open15_risk_controls import _live

        svc, _ = _svc(monkeypatch, _live([{"symbol": "A", "mtm": 5200.0}]))
        svc._risk_tick()
        assert svc._risk["locked"] and svc._risk["peak"] == 5200.0


class TestPeakEvent:
    def test_peak_raise_names_the_mover(self, monkeypatch):
        svc, _ = _svc(
            monkeypatch,
            _payload(
                [
                    {"symbol": "MCX", "mtm": 2295, "mark": 109.85},
                    {"symbol": "BIOCON", "mtm": 4100, "mark": 11.70},
                ]
            ),
        )
        svc._risk_tick()  # lock at 6395
        _set(
            monkeypatch,
            _payload(
                [
                    {
                        "symbol": "MCX",
                        "mtm": 3892.5,
                        "mark": 113.40,
                        "mark_basis": "ltp",
                        "ltp": 113.40,
                    },
                    {"symbol": "BIOCON", "mtm": 3350, "mark": 11.55},
                ],
                asof="09:21:25",
            ),
        )
        svc._risk_tick()
        (pk,) = _events(svc, "profit_peak")
        assert pk["peak"] == 7242.5 and pk["prev_peak"] == 6395.0
        assert pk["floor"] == 5742.5
        assert pk["moved_by"]["symbol"] == "MCX"
        assert pk["moved_by"]["delta"] == 1597.5
        assert pk["moved_by"]["basis"] == "ltp"
        assert svc._risk["path"][-1]["action"] == "peak"


class TestReplay20260909:
    """The three decisive polls of 2026-09-09, re-run under the new rules with
    the marks the log recorded (bid where the log had it, else the print)."""

    def _polls(self):
        # net = gross - modelled charges (~Rs477 MCX, ~Rs543 BIOCON)
        return [
            (
                "09:21:22",
                [
                    {"symbol": "MCX", "mtm": 2295, "charges_est": 477},
                    {"symbol": "BIOCON", "mtm": 4100, "charges_est": 543},
                ],
            ),
            (
                "09:21:25",
                [
                    {"symbol": "MCX", "mtm": 3892.5, "charges_est": 480},
                    {"symbol": "BIOCON", "mtm": 3350, "charges_est": 541},
                ],
            ),
            (
                "09:21:28",
                [
                    {"symbol": "MCX", "mtm": 2272.5, "charges_est": 477},
                    {"symbol": "BIOCON", "mtm": 2850, "charges_est": 540},
                ],
            ),
        ]

    def test_no_lock_on_the_first_poll_and_no_exit_on_the_third(self, monkeypatch):
        polls = self._polls()
        svc, flattened = _svc(
            monkeypatch,
            _payload(polls[0][1], asof=polls[0][0]),
            profit_target_inr=5500,
            trail_giveback_inr=2000,
        )
        svc._risk_tick()
        assert not svc._risk["locked"]  # 5,375 net < 5,500
        _set(monkeypatch, _payload(polls[1][1], asof=polls[1][0]))
        svc._risk_tick()
        assert svc._risk["locked"]  # 6,221.5 net
        assert svc._risk["floor"] == 4221.5
        _set(monkeypatch, _payload(polls[2][1], asof=polls[2][0]))
        svc._risk_tick()
        # 4,105.5 net <= 4,221.5: first breach, awaiting confirmation — no SELL
        assert svc._risk["breach_polls"] == 1
        assert not svc._risk["trail_done"] and flattened == []
        assert len(svc._risk["path"]) == 3


class TestPathPersistence:
    def test_risk_path_written_once_on_trail_exit(self, monkeypatch):
        svc, _ = _svc(monkeypatch, _payload([{"symbol": "A", "mtm": 6100}]), confirm=1)
        svc._risk_tick()
        _set(monkeypatch, _payload([{"symbol": "A", "mtm": 4580}], asof="09:21:25"))
        svc._risk_tick()
        (rp,) = _events(svc, "risk_path")
        assert rp["n"] == 2
        assert rp["polls"][0][6] == "locked" and rp["polls"][1][6] == "exit"
        assert rp["polls"][1][7]["A"][1] == "bid"
        # flatten / summary backstops must not write it again
        svc._log_risk_path()
        svc._log_risk_path()
        assert len(_events(svc, "risk_path")) == 1

    def test_never_locked_day_still_records_its_path(self, monkeypatch):
        svc, _ = _svc(monkeypatch, _payload([{"symbol": "A", "mtm": 1000}]))
        svc._risk_tick()
        svc._risk_tick()
        assert not svc._risk["locked"]
        svc._log_risk_path()
        (rp,) = _events(svc, "risk_path")
        assert rp["n"] == 2 and rp["polls"][0][3] is None  # no peak yet

    def test_risk_status_exposes_the_trail(self, monkeypatch):
        svc, _ = _svc(monkeypatch, _payload([{"symbol": "A", "mtm": 6100}]))
        svc._risk_tick()
        _set(monkeypatch, _payload([{"symbol": "A", "mtm": 5000}], asof="09:21:25"))
        svc._risk_tick()
        st = svc.risk_status()
        assert st["trail_confirm_polls"] == 2
        assert st["breach_polls"] == 0 and st["polls_since_lock"] == 1
        assert st["mark_basis"] == "bid"
        assert st["day_pnl_net"] == 5000.0 and st["headroom"] == 400.0
        assert st["n_polls"] == 2 and len(st["path"]) == 2
        assert st["path"][-1]["action"] == "trailing"


# --------------------------------------------------------------------------- #
# replay from the day log
# --------------------------------------------------------------------------- #
class TestRiskFromDayLog:
    def test_none_without_a_lock(self):
        assert curve.risk_from_day_log([{"event": "armed"}, {"event": "entry"}]) is None
        assert curve.risk_from_day_log(None) is None

    def test_pre_716_day_from_lock_and_exit_events_only(self):
        # exactly what 2026-09-09's day log holds
        events = [
            {
                "ts": "09:21:22.226",
                "event": "profit_target_locked",
                "day_pnl": 6395.0,
                "target": 5500.0,
                "trail_giveback": 2000.0,
                "floor": 4395.0,
                "open_trades": 2,
            },
            {
                "ts": "09:21:28.269",
                "event": "profit_trail_exit",
                "day_pnl": 5122.5,
                "peak": 7242.5,
                "floor": 5242.5,
            },
        ]
        rk = curve.risk_from_day_log(events)
        assert rk["source"] == "events" and rk["net_available"] is False
        assert rk["locked_at"] == "09:21:22.226" and rk["lock_pnl"] == 6395.0
        assert rk["trail_at"] == "09:21:28.269" and rk["exit_pnl"] == 5122.5
        assert rk["peak"] == 7242.5 and rk["peak_gross"] == 7242.5
        assert rk["floor"] == 5242.5 and rk["floor_gross"] == 5242.5
        assert rk["trail_giveback_inr"] == 2000.0 and rk["path"] == []
        # nothing dates the peak on an events-only day; the shortfall is derivable
        assert rk["peak_at"] is None and rk["exit_shortfall"] == 120.0

    def test_716_day_uses_the_path_and_peaks(self):
        events = [
            {
                "ts": "09:21:25.0",
                "event": "profit_target_locked",
                "day_pnl": 6221.5,
                "net": 6221.5,
                "gross": 7242.5,
                "target": 5500.0,
                "trail_giveback": 2000.0,
                "confirm_polls": 2,
                "floor": 4221.5,
                "floor_gross": 5242.5,
                "peak_gross": 7242.5,
                "mark_basis": "bid",
            },
            {
                "ts": "09:21:31.0",
                "event": "profit_peak",
                "peak": 6500.0,
                "peak_gross": 7520.0,
                "prev_peak": 6221.5,
                "floor": 4500.0,
                "floor_gross": 5520.0,
                "moved_by": {"symbol": "MCX"},
            },
            {
                "ts": "09:21:34.0",
                "event": "profit_trail_breach",
                "day_pnl": 4400.0,
                "floor": 4500.0,
                "shortfall": 100.0,
                "polls_required": 2,
            },
            {
                "ts": "09:21:37.0",
                "event": "profit_trail_exit",
                "day_pnl": 4300.0,
                "net": 4300.0,
                "gross": 5320.0,
                "peak": 6500.0,
                "peak_gross": 7520.0,
                "floor": 4500.0,
                "floor_gross": 5520.0,
                "shortfall": 200.0,
                "breach_polls": 2,
                "polls_since_lock": 4,
                "mark_basis": "bid",
            },
            {
                "ts": "09:21:37.1",
                "event": "risk_path",
                "n": 2,
                "polls": [
                    ["09:21:25", 7242.5, 6221.5, 7242.5, 5242.5, 0, "locked", {}],
                    ["09:21:37", 5320.0, 4300.0, 7520.0, 5520.0, 2, "exit", {}],
                ],
            },
        ]
        rk = curve.risk_from_day_log(events)
        assert rk["source"] == "risk_path" and rk["net_available"] is True
        assert rk["lock_pnl"] == 7242.5 and rk["lock_pnl_net"] == 6221.5
        assert rk["peak"] == 6500.0 and rk["peak_at"] == "09:21:31.0"
        assert rk["peaks"][0]["moved_by"] == {"symbol": "MCX"}
        assert rk["breach_at"] == "09:21:34.0"
        assert (
            rk["exit_shortfall"] == 200.0
            and rk["breach_polls"] == 2
            and rk["polls_since_lock"] == 4
        )
        assert rk["trail_confirm_polls"] == 2 and rk["mark_basis"] == "bid"
        assert len(rk["path"]) == 2 and rk["n_polls"] == 2


class TestCurvePayloadCarriesRisk:
    def test_build_pnl_curve_replays_the_day_log(self, monkeypatch):
        from database import open15_breakout_db as o15db

        monkeypatch.setattr(
            o15db,
            "get_day_log",
            lambda d: [
                {
                    "ts": "09:21:22.226",
                    "event": "profit_target_locked",
                    "day_pnl": 6395.0,
                    "target": 5500.0,
                    "trail_giveback": 2000.0,
                    "floor": 4395.0,
                },
                {
                    "ts": "09:21:28.269",
                    "event": "profit_trail_exit",
                    "day_pnl": 5122.5,
                    "peak": 7242.5,
                    "floor": 5242.5,
                },
            ],
        )
        curve.clear_caches()
        j = curve.build_pnl_curve("2026-01-05")  # no rows: still a payload
        assert j["status"] == "ok"
        assert j["risk"]["locked"] and j["risk"]["source"] == "events"


class TestLivePnlBidMarks:
    def test_open_row_marked_at_bid_with_net(self, monkeypatch):
        from test.test_open15_pnl_curve import DATE as CURVE_DATE
        from test.test_open15_pnl_curve import _row

        monkeypatch.setattr(curve, "_today_ist", lambda: CURVE_DATE)
        curve.clear_caches()
        _row(status="open", exit_ts=None, pnl=None, charges_inr=None, side="L")
        calls = []

        def fake(contracts):
            calls.append(list(contracts))
            return {"DIVISLAB29SEP269200PE": {"ltp": 190.0, "bid": 188.0, "ask": 190.5}}

        monkeypatch.setattr(curve, "_batched_quotes", fake)
        j = curve.live_pnl()
        assert j["status"] == "live" and len(calls) == 1
        (t,) = j["trades"]
        assert t["ltp"] == 190.0 and t["mark"] == 188.0 and t["mark_basis"] == "bid"
        # MTM is on the mark, not the print: (188.0 - 183.52) * 300
        assert t["mtm"] == pytest.approx(1344.0)
        assert t["charges_est"] and t["mtm_net"] == pytest.approx(t["mtm"] - t["charges_est"])
        assert j["portfolio_mtm"] == pytest.approx(1344.0)
        assert j["portfolio_mtm_net"] == pytest.approx(1344.0 - t["charges_est"])
        assert j["charges_est_total"] == pytest.approx(t["charges_est"])
        assert j["mark_basis"] == "bid"

    def test_missing_bid_falls_back_to_ltp_and_says_so(self, monkeypatch):
        from test.test_open15_pnl_curve import DATE as CURVE_DATE
        from test.test_open15_pnl_curve import _row

        monkeypatch.setattr(curve, "_today_ist", lambda: CURVE_DATE)
        curve.clear_caches()
        _row(status="open", exit_ts=None, pnl=None, charges_inr=None)
        monkeypatch.setattr(
            curve,
            "_batched_quotes",
            lambda c: {"DIVISLAB29SEP269200PE": {"ltp": 190.0, "bid": None, "ask": None}},
        )
        j = curve.live_pnl()
        (t,) = j["trades"]
        assert t["mark"] == 190.0 and t["mark_basis"] == "ltp"
        assert j["mark_basis"] == "ltp"


class TestConfigColumnMigration:
    def test_pre_716_table_gains_the_column_on_init(self):
        """An install whose open15_config predates #716 must read its stored
        row after ``init_db()`` (the branch-preview against the live DB, which
        skips init_db, failed exactly here: 'no such column ... trail_confirm_polls')."""
        from sqlalchemy import text

        from database import open15_breakout_db as o15db

        with o15db.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS open15_config"))
            conn.execute(
                text(
                    "CREATE TABLE open15_config (id INTEGER PRIMARY KEY, margin_per_slot FLOAT, "
                    "sizing_mode VARCHAR(16), vol_mult FLOAT, updated_by VARCHAR(32), updated_at DATETIME, "
                    "profit_lock_enabled INTEGER, profit_target_inr FLOAT, trail_giveback_inr FLOAT, "
                    "stop_loss_enabled INTEGER, stop_loss_inr FLOAT)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO open15_config (id, profit_lock_enabled, profit_target_inr, trail_giveback_inr) "
                    "VALUES (1, 1, 5500, 2000)"
                )
            )
        o15db.init_db()
        cfg = o15db.get_config()
        assert cfg is not None, "get_config must not fall back to env defaults after init_db"
        assert cfg["profit_target_inr"] == 5500 and cfg["trail_confirm_polls"] is None
        assert o15db.save_config(
            60000.0, "fixed", 1.5, "test", trail_confirm_polls=3, profit_lock_enabled=True
        )
        assert o15db.get_config()["trail_confirm_polls"] == 3
