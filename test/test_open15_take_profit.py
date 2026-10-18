"""Per-trade underlying take-profit (issue #770).

R74's follow-up replay (#769, ``backtest/open15_2y/followthrough.py``) found
that squaring a trade off once its UNDERLYING has moved +1.0..1.5% in the
trade's direction added return in both date halves — the one exit rule that
did. This ships it default OFF so the operator can switch it on and its
counterfactual is measured. These tests pin:

- resolution (env seed OFF, clamp 0.5..3.0, per-side NULL = common, 0 = off),
- the direction-aware move helper,
- the monitor: each side's threshold, real ``open`` rows only (never
  paper / sim / shadow), independent of option quotes, no core = no fire,
- the REAL ``_exit_open_row`` seam (order out, row closed ``take_profit``;
  a book-flat entry is papered and NOTHING is sent — the #626 rule),
- one Telegram per symbol/day,
- the counterfactual plumbing (``RISK_EXIT_REASONS``, ``net_of_row_under``,
  the scorecard undo toggle, the curve flag),
- config storage, the armed-event passthrough, ``risk_status`` and the API.
"""

import pytest

import services.open15_pnl_curve as pnl_curve
from services.open15_breakout_service import (
    Open15BreakoutService,
    Open15Core,
    _resolve_risk_config,
    clamp_take_profit_pct,
    resolve_day_config,
    take_profit_pct_for_side,
    underlying_move_pct,
)

DATE = "2026-10-16"


@pytest.fixture(autouse=True)
def _clean_journal():
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()
    yield
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()


def _live(trades=None, quotes_ok=True):
    trades = trades or []
    return {
        "status": "live",
        "date": DATE,
        "quotes_ok": quotes_ok,
        "trades": trades,
        "portfolio_mtm": sum(t.get("mtm") or 0.0 for t in trades),
        "poll_interval_s": 5,
    }


def _mk_service(risk_cfg, positions, monkeypatch, *, ltps=None, payload=None, stub_exit=True):
    orders = []

    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = Open15BreakoutService(order_placer=placer)
    svc.day_status = "armed"
    svc._log_date = DATE
    svc.day_config = resolve_day_config(risk_cfg, 0.0)
    svc.positions = positions
    svc.core = Open15Core(dict.fromkeys(ltps or {}, 100.0), vol_mult=1.5, top_n=1)
    for s, px in (ltps or {}).items():
        svc.core.last_price[s] = px
    svc._entry_never_filled = lambda _s, _p: False
    svc._reconcile_and_log = lambda **_kw: None
    svc._persist_day_log = lambda: None
    svc._today_realized_net = lambda: 0.0
    svc._alert_take_profit = lambda *_a: None
    monkeypatch.setattr(pnl_curve, "live_pnl", lambda: payload or _live())
    exited = []
    if stub_exit:
        svc._exit_open_row = lambda sym, _pos, reason: exited.append((sym, reason)) or True
    return svc, exited, orders


def _events(svc, kind):
    return [e for e in svc.day_log if e.get("event") == kind]


TP_ON = {"take_profit_enabled": True, "take_profit_underlying_pct": 1.0}


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
class TestResolution:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("OPEN15_TAKE_PROFIT", raising=False)
        monkeypatch.delenv("OPEN15_TAKE_PROFIT_UNDERLYING_PCT", raising=False)
        cfg = _resolve_risk_config({})
        assert cfg["take_profit_enabled"] is False
        assert cfg["take_profit_underlying_pct"] == 1.0

    def test_env_seed_on(self, monkeypatch):
        monkeypatch.setenv("OPEN15_TAKE_PROFIT", "true")
        monkeypatch.setenv("OPEN15_TAKE_PROFIT_UNDERLYING_PCT", "1.25")
        cfg = _resolve_risk_config({})
        assert cfg["take_profit_enabled"] is True
        assert cfg["take_profit_underlying_pct"] == 1.25

    def test_stored_false_beats_env_true(self, monkeypatch):
        monkeypatch.setenv("OPEN15_TAKE_PROFIT", "true")
        assert _resolve_risk_config({"take_profit_enabled": False})["take_profit_enabled"] is False

    def test_clamp(self):
        assert clamp_take_profit_pct(0.1, 1.0) == 0.5
        assert clamp_take_profit_pct(9, 1.0) == 3.0
        assert clamp_take_profit_pct("x", 1.0) == 1.0
        assert clamp_take_profit_pct(float("nan"), 1.0) == 1.0
        # the common value is never 0 — "off" is the enabled flag's job
        assert clamp_take_profit_pct(0, 1.0) == 0.5
        assert clamp_take_profit_pct(0, 1.0, allow_zero=True) == 0.0

    def test_unset_sides_follow_common(self):
        cfg = _resolve_risk_config(TP_ON)
        assert cfg["take_profit_pct_long"] == 1.0 and cfg["take_profit_pct_short"] == 1.0

    def test_side_override_and_side_off(self):
        cfg = _resolve_risk_config(
            {**TP_ON, "take_profit_pct_long": 0, "take_profit_pct_short": 1.5}
        )
        assert cfg["take_profit_pct_long"] == 0.0
        assert cfg["take_profit_pct_short"] == 1.5
        assert cfg["take_profit_enabled"] is True

    def test_both_sides_off_disables(self):
        cfg = _resolve_risk_config({**TP_ON, "take_profit_pct_long": 0, "take_profit_pct_short": 0})
        assert cfg["take_profit_enabled"] is False

    def test_for_side_helper(self):
        cfg = {
            "take_profit_underlying_pct": 1.0,
            "take_profit_pct_long": 1.5,
            "take_profit_pct_short": 0.0,
        }
        assert take_profit_pct_for_side(cfg, "L") == 1.5
        assert take_profit_pct_for_side(cfg, "SELL") == 0.0
        assert take_profit_pct_for_side({"take_profit_underlying_pct": 1.0}, "S") == 1.0
        assert take_profit_pct_for_side({}, "L") == 0.0

    def test_move_is_direction_aware(self):
        assert underlying_move_pct("L", 100.0, 101.0) == pytest.approx(1.0)
        assert underlying_move_pct("L", 100.0, 99.0) == pytest.approx(-1.0)
        assert underlying_move_pct("S", 100.0, 99.0) == pytest.approx(1.0)
        assert underlying_move_pct("S", 100.0, 101.0) == pytest.approx(-1.0)
        assert underlying_move_pct("L", 100.0, None) is None
        assert underlying_move_pct("L", 0, 101.0) is None


# --------------------------------------------------------------------------- #
# the monitor
# --------------------------------------------------------------------------- #
class TestMonitor:
    @staticmethod
    def _pos(side, trig=100.0, status="open", fill="real"):
        return {"status": status, "fill": fill, "side": side, "trigger_price": trig}

    def test_long_and_short_fire_at_their_move(self, monkeypatch):
        positions = {"LLL": self._pos("L"), "SSS": self._pos("S"), "QQQ": self._pos("L")}
        ltps = {"LLL": 101.0, "SSS": 98.9, "QQQ": 100.9}
        svc, exited, _ = _mk_service(TP_ON, positions, monkeypatch, ltps=ltps)
        svc._risk_tick()
        assert sorted(exited) == [("LLL", "take_profit"), ("SSS", "take_profit")]

    def test_adverse_move_never_takes_profit(self, monkeypatch):
        positions = {"LLL": self._pos("L"), "SSS": self._pos("S")}
        svc, exited, _ = _mk_service(
            TP_ON, positions, monkeypatch, ltps={"LLL": 97.0, "SSS": 103.0}
        )
        svc._risk_tick()
        assert exited == []

    def test_each_side_uses_its_own_threshold(self, monkeypatch):
        cfg = {**TP_ON, "take_profit_pct_long": 0, "take_profit_pct_short": 1.5}
        positions = {"LLL": self._pos("L"), "SSS": self._pos("S"), "SS2": self._pos("S")}
        ltps = {"LLL": 105.0, "SSS": 98.4, "SS2": 98.6}
        svc, exited, _ = _mk_service(cfg, positions, monkeypatch, ltps=ltps)
        svc._risk_tick()
        assert exited == [("SSS", "take_profit")]

    def test_unordered_rows_are_never_squared_off(self, monkeypatch):
        positions = {
            "PAP": self._pos("L", status="paper", fill="paper"),
            "SIM": self._pos("L", status="sim", fill="sim"),
            "SHD": self._pos("L", status="shadow", fill="shadow"),
            "CLS": self._pos("L", status="closed"),
            "ODD": self._pos("L", fill="paper"),  # belt and braces
        }
        ltps = dict.fromkeys(positions, 110.0)
        svc, exited, _ = _mk_service(TP_ON, positions, monkeypatch, ltps=ltps)
        svc._risk_tick()
        assert exited == []

    def test_off_does_nothing(self, monkeypatch):
        svc, exited, _ = _mk_service(
            {"take_profit_enabled": False},
            {"LLL": self._pos("L")},
            monkeypatch,
            ltps={"LLL": 110.0},
        )
        svc._risk_tick()
        assert exited == []

    def test_independent_of_option_quotes(self, monkeypatch):
        """The rule reads the stock LTP, so an option-quote failure (which
        silences the stop and the day rule) does not silence it."""
        svc, exited, _ = _mk_service(
            TP_ON,
            {"LLL": self._pos("L")},
            monkeypatch,
            ltps={"LLL": 101.2},
            payload=_live(quotes_ok=False),
        )
        svc._risk_tick()
        assert exited == [("LLL", "take_profit")]

    def test_unknown_ltp_or_no_core_fires_nothing(self, monkeypatch):
        svc, exited, _ = _mk_service(TP_ON, {"LLL": self._pos("L")}, monkeypatch, ltps={})
        svc._risk_tick()
        svc.core = None
        svc._risk_tick()
        assert exited == []

    def test_fired_take_profit_ends_the_cycle(self, monkeypatch):
        """Like a fired stop: reconcile, then the stop/day rules wait for the
        next refreshed snapshot."""
        cfg = {**TP_ON, "stop_loss_enabled": True, "stop_loss_inr": 100}
        positions = {"LLL": self._pos("L"), "BBB": self._pos("L")}
        payload = _live([{"symbol": "BBB", "mtm": -5000.0}])
        svc, exited, _ = _mk_service(
            cfg, positions, monkeypatch, ltps={"LLL": 101.5, "BBB": 95.0}, payload=payload
        )
        reconciled = []
        svc._reconcile_and_log = lambda **kw: reconciled.append(kw)

        def _exit(sym, pos, reason):
            pos["status"] = "closed"
            exited.append((sym, reason))
            return True

        svc._exit_open_row = _exit
        svc._risk_tick()
        assert exited == [("LLL", "take_profit")]
        assert reconciled
        svc._risk_tick()  # next cycle: the stop gets its turn
        assert exited == [("LLL", "take_profit"), ("BBB", "stop_loss")]

    def test_failed_exit_retries_at_most_once_a_minute(self, monkeypatch):
        """A rejected exit keeps the row open; it must not become a market
        order every poll, nor starve the stop on another row."""
        cfg = {**TP_ON, "stop_loss_enabled": True, "stop_loss_inr": 100}
        positions = {"LLL": self._pos("L"), "BBB": self._pos("L")}
        payload = _live([{"symbol": "BBB", "mtm": -5000.0}])
        svc, exited, _ = _mk_service(
            cfg, positions, monkeypatch, ltps={"LLL": 101.5, "BBB": 95.0}, payload=payload
        )
        svc._alert_stop_loss = lambda *_a: None
        svc._risk_tick()  # TP sent; the stub leaves LLL open (a rejected exit)
        svc._risk_tick()  # inside the minute: no resend, the stop runs
        assert exited == [("LLL", "take_profit"), ("BBB", "stop_loss")]
        svc._risk["tp_last_try"]["LLL"] -= 61.0
        svc._risk_tick()
        assert exited.count(("LLL", "take_profit")) == 2

    def test_alert_once_per_symbol(self, monkeypatch):
        sent = []

        class _NS:
            def notify(self, kind, msg):
                sent.append((kind, msg))

        import services.notification_service as ns

        monkeypatch.setattr(ns, "get_notification_service", lambda: _NS())
        svc, _, _ = _mk_service(TP_ON, {}, monkeypatch)
        del svc._alert_take_profit  # restore the real method
        svc._alert_take_profit("LLL", 1.1, 1.0)
        svc._alert_take_profit("LLL", 1.3, 1.0)
        svc._alert_take_profit("SSS", 1.0, 1.0)
        assert len(sent) == 2
        assert all(k == "open15_breakout" for k, _ in sent)
        assert "TAKE PROFIT" in sent[0][1] and "LLL" in sent[0][1]


# --------------------------------------------------------------------------- #
# the REAL exit seam
# --------------------------------------------------------------------------- #
class TestRealExitPath:
    def _row(self, side):
        from database.open15_breakout_db import insert_trade

        return insert_trade(
            trade_date=DATE,
            symbol="AAA",
            side=side,
            mode="sandbox",
            trigger_price=100.0,
            quantity=10,
            status="open",
            fill="real",
        )

    def _positions(self, side, row_id):
        return {
            "AAA": {
                "status": "open",
                "fill": "real",
                "side": side,
                "quantity": 10,
                "trigger_price": 100.0,
                "row_id": row_id,
                "instrument": "stock",
            }
        }

    @pytest.mark.parametrize(("side", "ltp", "action"), [("L", 101.1, "SELL"), ("S", 98.9, "BUY")])
    def test_exits_through_exit_open_row(self, monkeypatch, side, ltp, action):
        from database.open15_breakout_db import Open15Trade, db_session

        row_id = self._row(side)
        positions = self._positions(side, row_id)
        svc, _, orders = _mk_service(
            TP_ON, positions, monkeypatch, ltps={"AAA": ltp}, stub_exit=False
        )
        svc._risk_tick()
        assert len(orders) == 1
        assert orders[0]["action"] == action and orders[0]["symbol"] == "AAA"
        assert orders[0]["quantity"] == 10
        assert positions["AAA"]["status"] == "closed"
        exits = _events(svc, "exit")
        assert len(exits) == 1 and exits[0]["reason"] == "take_profit"
        row = db_session.query(Open15Trade).filter(Open15Trade.id == row_id).one()
        assert row.status == "closed" and row.reason == "take_profit"
        assert row.pnl == pytest.approx(11.0)  # (101.1 - 100) x 10, or the short mirror
        db_session.remove()
        # a second cycle sends nothing more: the row is closed
        svc._risk_tick()
        assert len(orders) == 1

    def test_book_flat_entry_is_papered_and_nothing_is_sent(self, monkeypatch):
        """#626: an ACK is not a fill. A take-profit on a row whose entry the
        broker never filled must not send a naked exit."""
        row_id = self._row("L")
        positions = self._positions("L", row_id)
        svc, _, orders = _mk_service(
            TP_ON, positions, monkeypatch, ltps={"AAA": 102.0}, stub_exit=False
        )
        svc._entry_never_filled = lambda _s, _p: True
        mirrors = []
        svc._flatten_child_mirrors_for = lambda *a, **kw: mirrors.append(a)
        papered = []
        svc._flatten_paper = lambda sym, pos, reason: papered.append((sym, reason))
        svc._risk_tick()
        assert orders == []
        assert papered == [("AAA", "entry_rejected")]
        assert positions["AAA"]["fill"] == "paper"
        assert mirrors  # the child mirror is still told to flatten


# --------------------------------------------------------------------------- #
# counterfactual plumbing
# --------------------------------------------------------------------------- #
class TestCounterfactual:
    def test_take_profit_is_a_risk_exit(self):
        from database.open15_breakout_db import RISK_EXIT_REASONS, TAKE_PROFIT_REASON

        assert TAKE_PROFIT_REASON == "take_profit"
        assert TAKE_PROFIT_REASON in RISK_EXIT_REASONS

    def test_net_of_row_under_undoes_only_take_profit(self):
        from database.open15_breakout_db import net_of_row_under

        tp = {
            "reason": "take_profit",
            "pnl": 1000.0,
            "charges_inr": 50.0,
            "cf_pnl": 400.0,
            "cf_charges_inr": 40.0,
        }
        sl = {**tp, "reason": "stop_loss"}
        assert net_of_row_under(tp) == 950.0
        assert net_of_row_under(tp, apply_tp=False) == 360.0
        assert net_of_row_under(tp, apply_sl=False, apply_trail=False) == 950.0
        assert net_of_row_under(sl, apply_tp=False) == 950.0
        assert net_of_row_under({**tp, "cf_pnl": None}, apply_tp=False) is None

    def test_scorecard_toggle(self):
        from services.open15_grade_scorecard import build_scorecard

        rows = [
            {
                "cohort": "real",
                "trade_date": DATE,
                "side": "S",
                "rating": "A",
                "reason": "take_profit",
                "pnl": 1000.0,
                "charges_inr": 0.0,
                "cf_pnl": 200.0,
                "cf_charges_inr": 0.0,
            },
        ]
        on = build_scorecard(rows)
        off = build_scorecard(rows, apply_tp=False)
        assert on["apply_tp"] is True and off["apply_tp"] is False
        assert on["cohorts"]["real"]["all"] != off["cohorts"]["real"]["all"]

    def test_stop_saved_reads_the_same_for_take_profit(self):
        from database.open15_breakout_db import stop_saved_of_row

        row = {
            "reason": "take_profit",
            "pnl": 1000.0,
            "charges_inr": 50.0,
            "cf_pnl": 1500.0,
            "cf_charges_inr": 60.0,
        }
        # held would have paid more -> the take-profit COST money
        assert stop_saved_of_row(row) == -490.0

    def test_risk_exit_rows_include_take_profit(self):
        from database.open15_breakout_db import insert_trade, risk_exit_rows

        insert_trade(
            trade_date=DATE,
            symbol="AAA",
            side="L",
            mode="sandbox",
            trigger_price=100.0,
            quantity=1,
            status="closed",
            fill="real",
            reason="take_profit",
        )
        rows = risk_exit_rows(trade_date=DATE, unpriced_only=True)
        assert [r.reason for r in rows] == ["take_profit"]


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #
class TestPlumbing:
    def test_config_roundtrip(self):
        from database.open15_breakout_db import get_config, save_config

        assert save_config(
            30000,
            "fixed",
            1.5,
            take_profit_enabled=True,
            take_profit_underlying_pct=1.25,
            take_profit_pct_long=0.0,
            take_profit_pct_short=1.5,
        )
        cfg = get_config()
        assert cfg["take_profit_enabled"] is True
        assert cfg["take_profit_underlying_pct"] == 1.25
        assert cfg["take_profit_pct_long"] == 0.0 and cfg["take_profit_pct_short"] == 1.5
        assert save_config(30000, "fixed", 1.5)
        cfg = get_config()
        assert cfg["take_profit_enabled"] is None
        assert cfg["take_profit_pct_long"] is None

    def test_armed_event_passthrough(self):
        from services.open15_log_view import effective_from_armed

        out = effective_from_armed(
            {
                "event": "armed",
                "take_profit_enabled": True,
                "take_profit_underlying_pct": 1.0,
                "take_profit_pct_long": 0.0,
                "take_profit_pct_short": 1.5,
            }
        )
        assert out["take_profit_enabled"] is True
        assert out["take_profit_pct_short"] == 1.5 and out["take_profit_pct_long"] == 0.0

    def test_risk_status(self, monkeypatch):
        svc, _, _ = _mk_service({**TP_ON, "take_profit_pct_short": 1.5}, {}, monkeypatch)
        st = svc.risk_status()
        assert st["take_profit_enabled"] is True
        assert st["take_profit_pct_long"] == 1.0 and st["take_profit_pct_short"] == 1.5

    def test_api_config(self, monkeypatch):
        from flask import Flask

        import blueprints.open15_breakout as bp
        import database.open15_breakout_db as db
        import utils.session as sess

        monkeypatch.setattr(sess, "is_session_valid", lambda: True)
        saved = {}

        def fake_save(*_args, **kwargs):
            saved.update(kwargs)
            return True

        monkeypatch.setattr(db, "get_config", lambda: None)
        monkeypatch.setattr(db, "save_config", fake_save)
        app = Flask(__name__)
        app.register_blueprint(bp.open15_bp)
        app.config["TESTING"] = True
        client = app.test_client()
        r = client.post(
            "/open15_vol_breakout/api/config",
            json={
                "take_profit_enabled": True,
                "take_profit_underlying_pct": 9,
                "take_profit_pct_long": "",
                "take_profit_pct_short": 0,
            },
        )
        assert r.status_code == 200, r.data
        assert saved["take_profit_enabled"] is True
        assert saved["take_profit_underlying_pct"] == 3.0  # clamped, not rejected
        assert saved["take_profit_pct_long"] is None
        assert saved["take_profit_pct_short"] == 0.0
        d = client.get("/open15_vol_breakout/api/config").get_json()["env_defaults"]
        assert d["take_profit_enabled"] is False
        assert d["take_profit_underlying_pct"] == 1.0
        assert d["take_profit_pct_long"] is None

    def test_curve_flags_take_profit_rows(self, monkeypatch):
        from types import SimpleNamespace

        row = SimpleNamespace(
            id=1,
            symbol="AAA",
            side="L",
            instrument="stock",
            status="closed",
            reason="take_profit",
            trigger_minute=None,
            trigger_second=0,
            pnl=10.0,
            charges_inr=1.0,
            cf_pnl=None,
            cf_charges_inr=None,
            cf_source=None,
            entry_fill_qty=None,
            quantity=0,
            opt_symbol=None,
            entry_fill_price=None,
            exit_fill_price=None,
            exit_ts=None,
            trigger_price=100.0,
            opt_entry_premium=None,
        )
        out = pnl_curve._trade_curve(row, DATE)
        assert out["take_profit"] is True and out["risk_exit"] is True
        assert out["stop_loss"] is False and out["profit_trail"] is False
