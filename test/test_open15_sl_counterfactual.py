"""Stop-loss counterfactual: keep marking a stopped contract to the scheduled
exit so the per-trade stop (issue #696) can be judged on data (issue #704).

Pins:
- the pure derivation from 1m closes (option long premium, stock short sign,
  MAE/MFE over the post-stop window, modelled charges, exit-mark convention),
- ``stop_saved_of_row`` as the ONE definition of "stop saved" (#552 shape),
- the curve payload: ghost series + ``portfolio_no_stop`` on a stop day, and a
  byte-identical no-stop day (no ghost keys populated),
- ``live_pnl`` ghosts: on the SAME batch, excluded from ``portfolio_mtm``, and
  invisible to ``_risk_tick`` (a ghost below the stop fires nothing),
- the live stamp at the head of ``flatten`` and the bars backfill (idempotent,
  pending rows stay NULL),
- the scorecard aggregates + the pre-registered decision rule.
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytz

from database import open15_breakout_db as o15db
from services import open15_pnl_curve as curve
from services import open15_sl_counterfactual as slcf

IST = pytz.timezone("Asia/Kolkata")
DATE = "2026-09-07"
LATER = "2026-09-08"
EXIT_MIN = 9 * 60 + 30


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    o15db.init_db()
    curve.clear_caches()
    # the day's scheduled exit — the armed event is what production reads
    monkeypatch.setattr(slcf, "scheduled_exit_min", lambda _d: EXIT_MIN)
    yield
    try:
        o15db.db_session.query(o15db.Open15Trade).delete()
        o15db.db_session.query(o15db.Open15DayLog).delete()
        o15db.db_session.commit()
    finally:
        o15db.db_session.remove()
    curve.clear_caches()


def _row(**kw) -> int:
    """A REAL option row; default = stopped at 09:22 with journal gross -3270."""
    defaults = {
        "trade_date": DATE,
        "symbol": "BAJAJ-AUTO",
        "side": "S",
        "mode": "live",
        "instrument": "option",
        "opt_symbol": "BAJAJ-AUTO29SEP2611700PE",
        "opt_lot_size": 75,
        "quantity": 150,
        "entry_fill_price": 200.0,
        "entry_fill_qty": 150,
        "trigger_minute": "09:20",
        "trigger_second": 50,
        "trigger_price": 11650.0,
        "opt_entry_premium": 199.0,
        "exit_ts": f"{DATE}T09:22:14+05:30",
        "status": "closed",
        "pnl": -3270.0,
        "charges_inr": 122.0,
        "fill": "real",
        "reason": "stop_loss",
    }
    defaults.update(kw)
    r = o15db.Open15Trade(**defaults)
    o15db.db_session.add(r)
    o15db.db_session.commit()
    rid = r.id
    o15db.db_session.remove()
    return rid


def _get(rid: int):
    try:
        return o15db.db_session.query(o15db.Open15Trade).get(rid)
    finally:
        o15db.db_session.remove()


def _bars(closes: dict[str, float], date: str = DATE) -> list[dict]:
    y, m, d = (int(x) for x in date.split("-"))
    out = []
    for hhmm, close in closes.items():
        h, mi = (int(x) for x in hhmm.split(":"))
        ts = IST.localize(dt.datetime(y, m, d, h, mi))
        out.append({"timestamp": int(ts.timestamp()), "close": close})
    return out


# entry fill 200 x 150: 09:21 close 190 -> -1500 ... stop fired 09:22:14
CLOSES = {
    "09:21": 190.0,
    "09:22": 178.0,  # the stop minute's own bar closes after the stop
    "09:23": 172.0,
    "09:24": 160.0,  # worst: (160-200)*150 = -6000
    "09:25": 175.0,
    "09:26": 181.0,  # best after stop: -2850
    "09:27": 170.0,
    "09:28": 168.0,
    "09:29": 165.0,  # held-to-09:30 mark: (165-200)*150 = -5250
    "09:30": 199.0,  # AFTER the scheduled exit — must never be read
    "09:31": 210.0,
}


def _patch_bars(monkeypatch, by_contract):
    calls = []

    def fake(symbol, trade_date, exchange="NFO"):
        calls.append((symbol, trade_date, exchange))
        v = by_contract.get(symbol)
        return None if v is None else _bars(v, trade_date)

    monkeypatch.setattr(curve, "_fetch_bars", fake)
    return calls


# --------------------------------------------------------------------------- #
# derivation
# --------------------------------------------------------------------------- #


class TestDerive:
    def test_option_row_from_closes(self):
        row = _get(_row())
        cf = slcf.derive_from_closes(row, CLOSES, EXIT_MIN)
        assert cf["cf_exit_minute"] == "09:30"
        # priced at the last full bar before the scheduled exit (09:29), the
        # same convention the intra-hold curve ends a real 09:30 flatten on
        assert cf["cf_exit_price"] == 165.0
        assert cf["cf_pnl"] == pytest.approx(-5250.0)
        assert cf["cf_mae"] == pytest.approx(-6000.0) and cf["cf_mae_minute"] == "09:24"
        assert cf["cf_mfe"] == pytest.approx(-2850.0) and cf["cf_mfe_minute"] == "09:26"
        # charges are the option round trip on the FULL quantity, like the exit path
        from services.open15_option_shadow import option_round_trip_charges

        assert cf["cf_charges_inr"] == pytest.approx(
            option_round_trip_charges(200.0 * 150, 165.0 * 150)
        )

    def test_stock_short_sign_and_charges(self):
        row = _get(
            _row(
                instrument=None,
                opt_symbol=None,
                side="S",
                entry_fill_price=100.0,
                quantity=300,
                entry_fill_qty=300,
            )
        )
        cf = slcf.derive_from_closes(row, {"09:22": 101.0, "09:29": 97.0}, EXIT_MIN)
        # a short gains when price falls
        assert cf["cf_pnl"] == pytest.approx((100.0 - 97.0) * 300)
        assert cf["cf_mae"] == pytest.approx(-300.0) and cf["cf_mfe"] == pytest.approx(900.0)
        from services.open15_breakout_service import mis_round_trip_charges

        # short: buy leg is the counterfactual exit, sell leg is the entry
        assert cf["cf_charges_inr"] == pytest.approx(
            mis_round_trip_charges(97.0 * 300, 100.0 * 300)
        )

    def test_exit_mark_missing_is_none(self):
        row = _get(_row())
        assert slcf.derive_from_closes(row, {"09:22": 178.0}, EXIT_MIN) is None

    def test_stopped_at_or_after_exit_is_none(self):
        row = _get(_row(exit_ts=f"{DATE}T09:30:02+05:30"))
        assert slcf.derive_from_closes(row, CLOSES, EXIT_MIN) is None

    def test_live_derivation(self):
        row = _get(_row())
        path = [("09:23:05", -3300.0), ("09:24:10", -6150.0), ("09:26:00", -2700.0)]
        cf = slcf.derive_from_live(row, EXIT_MIN, 166.0, path)
        assert cf["cf_pnl"] == pytest.approx((166.0 - 200.0) * 150)
        assert cf["cf_mae"] == pytest.approx(-6150.0) and cf["cf_mae_minute"] == "09:24"
        assert cf["cf_mfe"] == pytest.approx(-2700.0) and cf["cf_mfe_minute"] == "09:26"


class TestStopSaved:
    def test_one_definition(self):
        rid = _row(cf_pnl=-5250.0, cf_charges_inr=130.0)
        row = _get(rid)
        # stop net = -3270 - 122 = -3392 ; held net = -5250 - 130 = -5380
        assert o15db.cf_net_of_row(row) == pytest.approx(-5380.0)
        assert o15db.stop_saved_of_row(row) == pytest.approx(1988.0)
        # unpriced -> None, never 0 (a 0 would read as "the stop was neutral")
        assert o15db.stop_saved_of_row(_get(_row(symbol="X"))) is None

    def test_stop_loss_rows_filters(self):
        _row()
        _row(symbol="HELD", reason="eod_0930")
        _row(symbol="PAPER", fill="paper")
        _row(symbol="PRICED", cf_pnl=-1.0)
        assert {r.symbol for r in o15db.stop_loss_rows()} == {"BAJAJ-AUTO", "PRICED"}
        assert [r.symbol for r in o15db.stop_loss_rows(unpriced_only=True)] == ["BAJAJ-AUTO"]


# --------------------------------------------------------------------------- #
# curve payload
# --------------------------------------------------------------------------- #


class TestCurve:
    def test_ghost_series_and_no_stop_portfolio(self, monkeypatch):
        _row()
        _row(
            symbol="IDEA",
            side="L",
            opt_symbol="IDEA29SEP2615CE",
            entry_fill_price=1.0,
            quantity=1000,
            entry_fill_qty=1000,
            trigger_minute="09:22",
            trigger_second=0,
            exit_ts=f"{DATE}T09:30:01+05:30",
            pnl=800.0,
            charges_inr=40.0,
            reason="eod_0930",
        )
        monkeypatch.setattr(curve, "_today_ist", lambda: LATER)
        _patch_bars(
            monkeypatch,
            {
                "BAJAJ-AUTO29SEP2611700PE": CLOSES,
                "IDEA29SEP2615CE": dict.fromkeys(("09:23", "09:24", "09:25", "09:29"), 1.5),
            },
        )
        j = curve.build_pnl_curve(DATE)
        assert j["status"] == "ok" and j["n_stopped"] == 1
        stopped = next(t for t in j["trades"] if t["symbol"] == "BAJAJ-AUTO")
        held = next(t for t in j["trades"] if t["symbol"] == "IDEA")
        assert stopped["stop_loss"] is True and held["stop_loss"] is False
        # real marks stop before the exit; ghost runs from the stop minute's
        # bar to the last bar before the scheduled exit, never past it
        assert dict(stopped["series"]) == {"09:21": pytest.approx(-1500.0)}
        g = dict(stopped["ghost"])
        assert list(g) == ["09:22", "09:23", "09:24", "09:25", "09:26", "09:27", "09:28", "09:29"]
        assert g["09:24"] == pytest.approx(-6000.0)
        assert "09:30" not in g and "09:31" not in g
        # unstamped: the last ghost mark stands in, labelled as such
        assert stopped["ghost_final"] == ["09:30", pytest.approx(-5250.0)]
        assert stopped["cf_source"] == "curve" and stopped["stop_saved"] is None
        assert held["ghost"] == [] and held["ghost_final"] is None
        # the real portfolio excludes the stopped row after 09:22; the no-stop
        # portfolio adds its ghost marks back
        port = dict(j["portfolio"])
        ns = dict(j["portfolio_no_stop"])
        assert port["09:24"] == pytest.approx(500.0)  # IDEA only
        assert ns["09:24"] == pytest.approx(500.0 - 6000.0)
        assert j["portfolio_final"] == ["09:30", pytest.approx(800.0 - 3270.0)]
        assert j["portfolio_no_stop_final"] == ["09:30", pytest.approx(800.0 - 5250.0)]
        assert j["stop_saved_total"] is None  # nothing stamped yet

    def test_stamped_row_uses_journal_counterfactual(self, monkeypatch):
        _row(cf_pnl=-5100.0, cf_charges_inr=130.0, cf_source="live")
        monkeypatch.setattr(curve, "_today_ist", lambda: LATER)
        _patch_bars(monkeypatch, {"BAJAJ-AUTO29SEP2611700PE": CLOSES})
        j = curve.build_pnl_curve(DATE)
        (t,) = j["trades"]
        # the stamp wins over the bar mark (a live quote at the exit, not a close)
        assert t["ghost_final"] == ["09:30", -5100.0] and t["cf_source"] == "live"
        assert t["stop_saved"] == pytest.approx((-3270.0 - 122.0) - (-5100.0 - 130.0))
        assert j["stop_saved_total"] == t["stop_saved"]

    def test_no_stop_day_payload_unchanged(self, monkeypatch):
        _row(reason="eod_0930", exit_ts=f"{DATE}T09:30:01+05:30")
        monkeypatch.setattr(curve, "_today_ist", lambda: LATER)
        _patch_bars(monkeypatch, {"BAJAJ-AUTO29SEP2611700PE": CLOSES})
        j = curve.build_pnl_curve(DATE)
        assert j["n_stopped"] == 0
        assert j["portfolio_no_stop"] == [] and j["portfolio_no_stop_final"] is None
        assert j["stop_saved_total"] is None
        (t,) = j["trades"]
        assert t["stop_loss"] is False and t["ghost"] == []


# --------------------------------------------------------------------------- #
# live ghosts: same batch, never money, never a risk input
# --------------------------------------------------------------------------- #


def _patch_live(monkeypatch, ltps: dict[str, float], now_hhmm: str = "09:25"):
    calls = []

    def fake_batch(contracts):
        calls.append([c for c, _ in contracts])
        return {c: {"ltp": ltps[c], "bid": ltps[c], "ask": ltps[c]} for c, _ in contracts if c in ltps}

    monkeypatch.setattr(curve, "_batched_quotes", fake_batch)
    monkeypatch.setattr(curve, "_today_ist", lambda: DATE)
    h, m = (int(x) for x in now_hhmm.split(":"))
    now = IST.localize(dt.datetime(2026, 9, 7, h, m, 30))
    monkeypatch.setattr(curve, "_now_ist", lambda: now)
    monkeypatch.setattr(curve, "resolve_live_poll_interval", lambda: 5)
    return calls


class TestLiveGhosts:
    def test_ghost_rides_the_batch_and_stays_out_of_portfolio(self, monkeypatch):
        _row()  # stopped
        _row(
            symbol="IDEA",
            opt_symbol="IDEA29SEP2615CE",
            status="open",
            exit_ts=None,
            pnl=None,
            charges_inr=None,
            reason=None,
            entry_fill_price=1.0,
            quantity=1000,
            entry_fill_qty=1000,
        )
        calls = _patch_live(
            monkeypatch, {"BAJAJ-AUTO29SEP2611700PE": 170.0, "IDEA29SEP2615CE": 1.4}
        )
        j = curve.live_pnl()
        assert j["status"] == "live"
        assert calls == [["IDEA29SEP2615CE", "BAJAJ-AUTO29SEP2611700PE"]]  # ONE call
        assert [t["symbol"] for t in j["trades"]] == ["IDEA"]
        assert j["portfolio_mtm"] == pytest.approx(400.0)  # the ghost is NOT in it
        (g,) = j["ghost"]
        assert g["symbol"] == "BAJAJ-AUTO" and g["mtm"] == pytest.approx((170.0 - 200.0) * 150)
        assert g["row_id"] == o15db.stop_loss_rows()[0].id

    def test_ghost_after_all_rows_closed(self, monkeypatch):
        _row()
        calls = _patch_live(monkeypatch, {"BAJAJ-AUTO29SEP2611700PE": 170.0})
        j = curve.live_pnl()
        assert j["status"] == "closed" and len(j["ghost"]) == 1 and j["asof"]
        assert len(calls) == 1

    def test_ghost_leaves_the_batch_at_the_scheduled_exit_or_once_stamped(self, monkeypatch):
        _row()
        calls = _patch_live(monkeypatch, {"BAJAJ-AUTO29SEP2611700PE": 170.0}, now_hhmm="09:30")
        j = curve.live_pnl()
        assert j["status"] == "closed" and "ghost" not in j and calls == []
        curve.clear_caches()
        _patch_live(monkeypatch, {"BAJAJ-AUTO29SEP2611700PE": 170.0}, now_hhmm="09:25")
        o15db.update_trade(o15db.stop_loss_rows()[0].id, cf_pnl=-1.0)
        j = curve.live_pnl()
        assert "ghost" not in j

    def test_risk_tick_ignores_ghosts(self, monkeypatch):
        """A ghost mark below -stop_loss_inr must fire NOTHING: the row is
        closed, the money is gone, and only ``trades`` feed the rules."""
        from test.test_open15_risk_controls import _live, _mk_service

        svc, orders = _mk_service(risk_cfg={"stop_loss_enabled": True, "stop_loss_inr": 1000})
        svc._log_date = DATE
        payload = _live([])
        payload["status"] = "closed"
        payload["asof"] = "09:25:10"
        payload["ghost"] = [
            {"row_id": 7, "symbol": "BAJAJ-AUTO", "contract": "X", "ltp": 150.0, "mtm": -7500.0}
        ]
        import services.open15_breakout_service as svc_mod

        monkeypatch.setattr(curve, "live_pnl", lambda: payload)
        monkeypatch.setattr(svc_mod, "live_pnl", lambda: payload, raising=False)
        svc._risk_tick()
        assert orders == []
        # ...but the mark was recorded for the live counterfactual
        assert svc._risk["ghost_path"][7] == [("09:25:10", -7500.0)]
        assert svc._risk["ghost_last"][7] == 150.0


# --------------------------------------------------------------------------- #
# stamping: live at the flatten, bars as the backstop
# --------------------------------------------------------------------------- #


class TestStamp:
    def test_flatten_stamps_tracked_ghosts_live(self, monkeypatch):
        from test.test_open15_risk_controls import _mk_service

        rid = _row()
        svc, _orders = _mk_service()
        svc._log_date = DATE
        monkeypatch.setattr(svc, "_trade_date", lambda: DATE)
        svc._risk["ghost_path"][rid] = [("09:24:05", -6100.0), ("09:28:00", -4000.0)]
        svc._risk["ghost_last"][rid] = 167.0
        svc._stamp_ghost_counterfactuals()
        row = _get(rid)
        assert row.cf_source == "live" and row.cf_exit_minute == "09:30"
        assert row.cf_pnl == pytest.approx((167.0 - 200.0) * 150)
        assert row.cf_mae == pytest.approx(-6100.0) and row.cf_mfe == pytest.approx(-4000.0)
        ev = [e for e in svc.day_log if e["event"] == "stop_counterfactual"]
        assert len(ev) == 1 and ev[0]["symbol"] == "BAJAJ-AUTO"
        assert ev[0]["stop_saved"] == pytest.approx(o15db.stop_saved_of_row(row))
        # idempotent: a second call finds nothing unpriced
        svc._stamp_ghost_counterfactuals()
        assert len([e for e in svc.day_log if e["event"] == "stop_counterfactual"]) == 1

    def test_untracked_row_stays_null_for_the_backfill(self, monkeypatch):
        from test.test_open15_risk_controls import _mk_service

        rid = _row()
        svc, _orders = _mk_service()
        monkeypatch.setattr(svc, "_trade_date", lambda: DATE)
        svc._stamp_ghost_counterfactuals()
        assert _get(rid).cf_pnl is None

    def test_backfill_from_bars_idempotent_and_pending(self, monkeypatch):
        rid = _row()
        rid2 = _row(symbol="MCX", opt_symbol="MCX29SEP263350CE", exit_ts=f"{DATE}T09:23:41+05:30")
        _row(symbol="DONE", opt_symbol="DONE29SEP26CE", cf_pnl=-9.0, cf_source="live")
        calls = _patch_bars(
            monkeypatch, {"BAJAJ-AUTO29SEP2611700PE": CLOSES, "MCX29SEP263350CE": None}
        )
        dry = slcf.backfill_missing(DATE, apply=False)
        assert (dry["checked"], dry["priced"], dry["pending"]) == (2, 1, 1)
        assert _get(rid).cf_pnl is None  # dry run wrote nothing
        res = slcf.backfill_missing(DATE)
        assert (res["checked"], res["priced"], res["pending"]) == (2, 1, 1)
        row = _get(rid)
        assert row.cf_source == "bars" and row.cf_pnl == pytest.approx(-5250.0)
        assert _get(rid2).cf_pnl is None
        pend = next(r for r in res["rows"] if r["symbol"] == "MCX")
        assert pend["pending"]
        # already-priced rows are never touched; a re-run only re-fetches the
        # still-pending contract
        n = len(calls)
        res2 = slcf.backfill_missing(DATE)
        assert res2["checked"] == 1 and len(calls) == n + 1


# --------------------------------------------------------------------------- #
# scorecard
# --------------------------------------------------------------------------- #


class TestScorecard:
    def test_empty(self):
        j = slcf.scorecard()
        assert j["n_events"] == 0 and j["verdict"] == "insufficient_sample" and j["since"] is None
        assert j["rule"]["min_events"] == slcf.DECISION_MIN_EVENTS

    def test_aggregates_and_pending(self):
        # right: held would have lost more (saved +1988)
        _row(cf_pnl=-5250.0, cf_charges_inr=130.0, cf_source="bars", cf_mfe=-2850.0)
        # wrong: held would have recovered (saved = -3392 - (+1800-140) = -5052)
        _row(
            trade_date="2026-09-04",
            symbol="MCX",
            cf_pnl=1800.0,
            cf_charges_inr=140.0,
            cf_source="live",
            cf_mfe=2600.0,
        )
        _row(trade_date="2026-09-03", symbol="PEND")  # unpriced
        j = slcf.scorecard()
        assert j["since"] == "2026-09-03" and j["n_events"] == 3 and j["n_days"] == 3
        assert j["n_priced"] == 2 and j["n_pending"] == 1
        assert (j["right"], j["wrong"]) == (1, 1) and j["right_rate"] == 0.5
        assert j["net_saved"] == pytest.approx(1988.0 - 5052.0)
        assert j["median_saved"] == pytest.approx((1988.0 - 5052.0) / 2)
        assert j["worst_saved"] == pytest.approx(-5052.0) and j["best_saved"] == pytest.approx(
            1988.0
        )
        assert j["avg_mfe_after_stop"] == pytest.approx((-2850.0 + 2600.0) / 2)
        assert j["verdict"] == "insufficient_sample"
        by = {e["symbol"]: e for e in j["events"]}
        assert by["PEND"]["pending"] is True and by["PEND"]["verdict"] is None
        assert by["MCX"]["verdict"] == "wrong" and by["MCX"]["source"] == "live"
        assert by["BAJAJ-AUTO"]["stop_at"] == "09:22:14"

    def test_decision_rule(self, monkeypatch):
        monkeypatch.setattr(slcf, "DECISION_MIN_EVENTS", 2)
        _row(cf_pnl=-5250.0, cf_charges_inr=130.0)
        _row(symbol="B", cf_pnl=-4000.0, cf_charges_inr=100.0)
        assert slcf.scorecard()["verdict"] == "keep"
        _row(symbol="C", cf_pnl=9000.0, cf_charges_inr=100.0)  # a big miss flips the sum
        j = slcf.scorecard()
        assert j["net_saved"] < 0 and j["verdict"] == "disable"
