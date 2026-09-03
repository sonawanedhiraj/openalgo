"""Risk controls: day profit lock + trail, per-trade stop loss (issue #696).

The profit lock is DAY-wise (cumulative net P&L, real fills only) and the stop
loss is TRADE-wise (one open row's own MTM). Both are evaluated by the
background risk monitor on the same ``live_pnl()`` payload the /logs chart
reads. These tests pin:

- config resolution (env seed vs stored row, 0-threshold-disables, clamps),
- the lock/ratchet/trail state machine, including ordering (stops before the
  day rule, day rule deferred to the next refreshed snapshot after a stop),
- fail-open on unknown marks (``quotes_ok`` false fires nothing),
- the ``_enter`` gate (a locked day journals ``profit_target_locked`` skips
  and never reaches the order placer or the ``max_trades`` budget),
- the trail flatten exiting REAL rows only, through the shared exit path,
- config storage roundtrip and the ``armed``-event passthrough.
"""

import datetime as dt

import pytest
import pytz

import services.open15_pnl_curve as pnl_curve
from services.open15_breakout_service import (
    Open15BreakoutService,
    Open15Core,
    _resolve_risk_config,
    clamp_risk_rupees,
    resolve_day_config,
)

IST = pytz.timezone("Asia/Kolkata")
DATE = "2026-09-03"


@pytest.fixture(autouse=True)
def _clean_journal():
    """Empty the journal between tests (same-date rows leak across tests)."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()
    yield
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()


def _mk_service(orders=None, *, risk_cfg=None, positions=None):
    """Armed service with a capturing order placer and no broker access."""
    orders = orders if orders is not None else []

    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = Open15BreakoutService(order_placer=placer)
    svc.day_status = "armed"
    svc._log_date = DATE
    svc.day_config = resolve_day_config(risk_cfg or {}, 0.0)
    svc.positions = positions or {}
    # never touch the broker book or the fill reconciler from a unit test
    svc._entry_never_filled = lambda _s, _p: False
    svc._reconcile_and_log = lambda **_kw: None
    # persistence seam: keep events in memory only
    svc._persist_day_log = lambda: None
    return svc, orders


def _live(trades, mtm=None, quotes_ok=True, status="live"):
    total = sum(t.get("mtm") or 0.0 for t in trades) if mtm is None else mtm
    return {
        "status": status,
        "date": DATE,
        "quotes_ok": quotes_ok,
        "trades": trades,
        "portfolio_mtm": total,
        "poll_interval_s": 5,
    }


def _events(svc, kind):
    return [e for e in svc.day_log if e.get("event") == kind]


# --------------------------------------------------------------------------- #
# config resolution
# --------------------------------------------------------------------------- #
class TestRiskConfig:
    def test_defaults_are_off(self, monkeypatch):
        for var in (
            "OPEN15_PROFIT_LOCK",
            "OPEN15_PROFIT_TARGET_INR",
            "OPEN15_TRAIL_GIVEBACK_INR",
            "OPEN15_STOP_LOSS",
            "OPEN15_STOP_LOSS_INR",
        ):
            monkeypatch.delenv(var, raising=False)
        cfg = resolve_day_config(None, 0.0)
        assert cfg["profit_lock_enabled"] is False
        assert cfg["stop_loss_enabled"] is False
        assert cfg["profit_target_inr"] == 5000.0
        assert cfg["trail_giveback_inr"] == 1500.0
        assert cfg["stop_loss_inr"] == 1200.0

    def test_stored_row_wins(self):
        cfg = resolve_day_config(
            {
                "profit_lock_enabled": True,
                "profit_target_inr": 8000,
                "trail_giveback_inr": 2000,
                "stop_loss_enabled": True,
                "stop_loss_inr": 900,
            },
            0.0,
        )
        assert cfg["profit_lock_enabled"] is True
        assert cfg["profit_target_inr"] == 8000.0
        assert cfg["stop_loss_enabled"] is True
        assert cfg["stop_loss_inr"] == 900.0

    def test_stored_false_beats_env_true(self, monkeypatch):
        monkeypatch.setenv("OPEN15_PROFIT_LOCK", "true")
        monkeypatch.setenv("OPEN15_STOP_LOSS", "true")
        cfg = resolve_day_config({"profit_lock_enabled": False, "stop_loss_enabled": False}, 0.0)
        assert cfg["profit_lock_enabled"] is False
        assert cfg["stop_loss_enabled"] is False

    def test_zero_threshold_disables(self):
        cfg = _resolve_risk_config(
            {
                "profit_lock_enabled": True,
                "profit_target_inr": 0,
                "stop_loss_enabled": True,
                "stop_loss_inr": 0,
            }
        )
        assert cfg["profit_lock_enabled"] is False
        assert cfg["stop_loss_enabled"] is False

    def test_clamp_rejects_garbage_and_negatives(self):
        assert clamp_risk_rupees("junk", 5000.0) == 5000.0
        assert clamp_risk_rupees(-100, 5000.0) == 0.0
        assert clamp_risk_rupees(99_999_999_999, 5000.0) == 10_000_000.0

    def test_giveback_zero_is_legal(self):
        cfg = _resolve_risk_config(
            {"profit_lock_enabled": True, "profit_target_inr": 5000, "trail_giveback_inr": 0}
        )
        assert cfg["profit_lock_enabled"] is True
        assert cfg["trail_giveback_inr"] == 0.0


# --------------------------------------------------------------------------- #
# lock / ratchet / trail state machine
# --------------------------------------------------------------------------- #
class TestProfitLock:
    def _svc(self, monkeypatch, payload, realized=0.0, **risk):
        risk_cfg = {
            "profit_lock_enabled": True,
            "profit_target_inr": 5000,
            "trail_giveback_inr": 1500,
            **risk,
        }
        svc, orders = _mk_service(risk_cfg=risk_cfg)
        monkeypatch.setattr(pnl_curve, "live_pnl", lambda: payload)
        svc._today_realized_net = lambda: realized
        return svc, orders

    def test_below_target_does_nothing(self, monkeypatch):
        svc, _ = self._svc(monkeypatch, _live([{"symbol": "AAA", "mtm": 2000.0}]))
        svc._risk_tick()
        assert not svc._risk["locked"]
        assert not _events(svc, "profit_target_locked")

    def test_lock_fires_once_at_target(self, monkeypatch):
        svc, _ = self._svc(monkeypatch, _live([{"symbol": "AAA", "mtm": 5400.0}]), realized=100.0)
        svc._risk_tick()
        assert svc._risk["locked"]
        assert svc._risk["peak"] == 5500.0
        assert svc._risk["floor"] == 4000.0
        events = _events(svc, "profit_target_locked")
        assert len(events) == 1
        assert events[0]["day_pnl"] == 5500.0
        # second tick at the same level: no duplicate lock event
        svc._risk_tick()
        assert len(_events(svc, "profit_target_locked")) == 1

    def test_peak_ratchets_and_floor_follows(self, monkeypatch):
        svc, _ = self._svc(monkeypatch, _live([{"symbol": "AAA", "mtm": 5200.0}]))
        svc._risk_tick()
        monkeypatch.setattr(
            pnl_curve, "live_pnl", lambda: _live([{"symbol": "AAA", "mtm": 6100.0}])
        )
        svc._risk_tick()
        assert svc._risk["peak"] == 6100.0
        assert svc._risk["floor"] == 4600.0
        assert not svc._risk["trail_done"]

    def test_trail_flattens_on_giveback_breach(self, monkeypatch):
        svc, _ = self._svc(monkeypatch, _live([{"symbol": "AAA", "mtm": 6100.0}]))
        flattened = []
        svc._profit_trail_exits = lambda reason: flattened.append(reason)
        svc._risk_tick()  # locks, peak 6100, floor 4600
        monkeypatch.setattr(
            pnl_curve, "live_pnl", lambda: _live([{"symbol": "AAA", "mtm": 4580.0}])
        )
        svc._risk_tick()
        assert svc._risk["trail_done"]
        assert flattened == ["profit_trail"]
        events = _events(svc, "profit_trail_exit")
        assert len(events) == 1
        assert events[0]["peak"] == 6100.0
        assert events[0]["floor"] == 4600.0

    def test_no_trail_without_open_trades(self, monkeypatch):
        # realized-only P&L cannot retrace; a locked flat day must not "flatten"
        svc, _ = self._svc(monkeypatch, _live([{"symbol": "AAA", "mtm": 5200.0}]))
        svc._risk_tick()
        monkeypatch.setattr(pnl_curve, "live_pnl", lambda: _live([], status="closed"))
        svc._today_realized_net = lambda: 0.0  # below the floor, but nothing open
        svc._risk_tick()
        assert not svc._risk["trail_done"]

    def test_unknown_marks_fire_nothing(self, monkeypatch):
        svc, _ = self._svc(monkeypatch, _live([{"symbol": "AAA", "mtm": 9999.0}], quotes_ok=False))
        svc._risk_tick()
        assert not svc._risk["locked"]

    def test_giveback_zero_books_at_target(self, monkeypatch):
        svc, _ = self._svc(
            monkeypatch,
            _live([{"symbol": "AAA", "mtm": 5100.0}]),
            trail_giveback_inr=0,
        )
        flattened = []
        svc._profit_trail_exits = lambda reason: flattened.append(reason)
        svc._risk_tick()
        assert svc._risk["locked"] and svc._risk["trail_done"]
        assert flattened == ["profit_trail"]

    def test_disabled_rules_do_nothing(self, monkeypatch):
        svc, _ = _mk_service(risk_cfg={"profit_lock_enabled": False, "stop_loss_enabled": False})
        called = []
        monkeypatch.setattr(pnl_curve, "live_pnl", lambda: called.append(1) or _live([]))
        svc._today_realized_net = lambda: 99_999.0
        svc._risk_tick()
        # live_pnl still refreshes the chart cache, but no rule evaluates
        assert called and not svc._risk["locked"]


# --------------------------------------------------------------------------- #
# per-trade stop loss
# --------------------------------------------------------------------------- #
class TestStopLoss:
    def _svc(self, monkeypatch, payload, positions):
        risk_cfg = {
            "stop_loss_enabled": True,
            "stop_loss_inr": 1200,
            "profit_lock_enabled": True,
            "profit_target_inr": 5000,
        }
        svc, orders = _mk_service(risk_cfg=risk_cfg, positions=positions)
        monkeypatch.setattr(pnl_curve, "live_pnl", lambda: payload)
        svc._today_realized_net = lambda: 0.0
        svc._alert_stop_loss = lambda *_a: None
        return svc, orders

    def test_only_the_breaching_trade_is_stopped(self, monkeypatch):
        positions = {
            "AAA": {"status": "open", "fill": "real"},
            "BBB": {"status": "open", "fill": "real"},
        }
        payload = _live([{"symbol": "AAA", "mtm": -1300.0}, {"symbol": "BBB", "mtm": 500.0}])
        svc, _ = self._svc(monkeypatch, payload, positions)
        exited = []
        svc._exit_open_row = lambda sym, _pos, reason: exited.append((sym, reason)) or True
        svc._risk_tick()
        assert exited == [("AAA", "stop_loss")]
        # a fired stop defers the day rule to the next refreshed snapshot
        assert not svc._risk["locked"]

    def test_within_limit_is_left_alone(self, monkeypatch):
        positions = {"AAA": {"status": "open", "fill": "real"}}
        svc, _ = self._svc(monkeypatch, _live([{"symbol": "AAA", "mtm": -1100.0}]), positions)
        exited = []
        svc._exit_open_row = lambda sym, _pos, reason: exited.append((sym, reason)) or True
        svc._risk_tick()
        assert exited == []

    def test_unpriced_mtm_never_stops(self, monkeypatch):
        positions = {"AAA": {"status": "open", "fill": "real"}}
        svc, _ = self._svc(monkeypatch, _live([{"symbol": "AAA", "mtm": None}]), positions)
        exited = []
        svc._exit_open_row = lambda sym, _pos, reason: exited.append(sym) or True
        svc._risk_tick()
        assert exited == []

    def test_stop_loss_exits_through_the_real_path(self, monkeypatch):
        """End-to-end through ``_exit_open_row``: order sent, row closed,
        ``exit`` event carries reason=stop_loss (the mirrored parent order is
        exactly what the child fan-out echoes)."""
        from database.open15_breakout_db import Open15Trade, db_session, insert_trade

        row_id = insert_trade(
            trade_date=DATE,
            symbol="AAA",
            side="L",
            mode="sandbox",
            trigger_price=100.0,
            quantity=10,
            status="open",
            fill="real",
        )
        positions = {
            "AAA": {
                "status": "open",
                "fill": "real",
                "side": "L",
                "quantity": 10,
                "trigger_price": 100.0,
                "row_id": row_id,
                "instrument": "stock",
            }
        }
        payload = _live([{"symbol": "AAA", "mtm": -1250.0}])
        svc, orders = self._svc(monkeypatch, payload, positions)
        svc.core = Open15Core({"AAA": 100.0}, vol_mult=1.5, top_n=1)
        svc.core.last_price["AAA"] = 87.5
        svc._risk_tick()
        assert orders and orders[0]["action"] == "SELL" and orders[0]["symbol"] == "AAA"
        assert positions["AAA"]["status"] == "closed"
        exits = _events(svc, "exit")
        assert len(exits) == 1 and exits[0]["reason"] == "stop_loss"
        row = db_session.query(Open15Trade).filter(Open15Trade.id == row_id).one()
        assert row.status == "closed" and row.reason == "stop_loss"
        db_session.remove()


# --------------------------------------------------------------------------- #
# entry gate + trail flatten scope
# --------------------------------------------------------------------------- #
class TestEntryGateAndTrailScope:
    def test_locked_day_journals_skip_and_places_nothing(self, monkeypatch):
        monkeypatch.setenv("OPEN15_SIM_SKIPPED_ENABLED", "false")
        svc, orders = _mk_service(risk_cfg={"profit_lock_enabled": True, "profit_target_inr": 5000})
        svc._risk["locked"] = True
        action = {
            "symbol": "AAA",
            "side": "L",
            "price": 100.0,
            "gap_pct": 2.0,
            "level": 99.0,
            "baseline_vol": 1000,
            "cum_vol_at_trigger": 2000,
            "trigger_minute": "09:18",
            "trigger_second": 5,
        }
        svc._enter(action)
        assert orders == []
        skips = _events(svc, "entry_skipped")
        assert len(skips) == 1 and skips[0]["reason"] == "profit_target_locked"
        from database.open15_breakout_db import Open15Trade, db_session

        row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").one()
        assert row.status == "skipped" and row.reason == "profit_target_locked"
        db_session.remove()

    def test_unlocked_day_enters_normally(self):
        svc, orders = _mk_service(risk_cfg={"profit_lock_enabled": True, "profit_target_inr": 5000})
        action = {
            "symbol": "AAA",
            "side": "L",
            "price": 100.0,
            "gap_pct": 2.0,
            "level": 99.0,
            "baseline_vol": 1000,
            "cum_vol_at_trigger": 2000,
            "trigger_minute": "09:18",
            "trigger_second": 5,
        }
        svc._enter(action)
        assert len(orders) == 1 and orders[0]["action"] == "BUY"

    def test_trail_flatten_exits_real_rows_only(self):
        """Paper/sim/shadow rows are measurements priced to the configured
        exit — an early risk exit must not truncate them."""
        positions = {
            "AAA": {"status": "open", "fill": "real"},
            "BBB": {"status": "paper", "fill": "paper"},
            "CCC": {"status": "sim", "fill": "sim"},
            "DDD": {"status": "shadow", "fill": "shadow"},
        }
        svc, _ = _mk_service(
            risk_cfg={"profit_lock_enabled": True, "profit_target_inr": 5000},
            positions=positions,
        )
        exited = []
        svc._exit_open_row = lambda sym, _pos, reason: exited.append((sym, reason)) or True
        svc._profit_trail_flatten(4580.0)
        assert exited == [("AAA", "profit_trail")]
        assert svc._risk["trail_done"]


# --------------------------------------------------------------------------- #
# storage + event plumbing
# --------------------------------------------------------------------------- #
class TestPlumbing:
    def test_config_roundtrip(self):
        from database.open15_breakout_db import get_config, save_config

        assert save_config(
            30000,
            "fixed",
            1.5,
            profit_lock_enabled=True,
            profit_target_inr=7000.0,
            trail_giveback_inr=2500.0,
            stop_loss_enabled=True,
            stop_loss_inr=800.0,
        )
        cfg = get_config()
        assert cfg["profit_lock_enabled"] is True
        assert cfg["profit_target_inr"] == 7000.0
        assert cfg["trail_giveback_inr"] == 2500.0
        assert cfg["stop_loss_enabled"] is True
        assert cfg["stop_loss_inr"] == 800.0
        # NULLs survive a save that omits them (env fall-through intact)
        assert save_config(30000, "fixed", 1.5)
        cfg = get_config()
        assert cfg["profit_lock_enabled"] is None and cfg["stop_loss_inr"] is None

    def test_armed_event_passthrough(self):
        from services.open15_log_view import effective_from_armed

        out = effective_from_armed(
            {
                "event": "armed",
                "profit_lock_enabled": True,
                "profit_target_inr": 5000,
                "trail_giveback_inr": 1500,
                "stop_loss_enabled": True,
                "stop_loss_inr": 1200,
            }
        )
        assert out["profit_lock_enabled"] is True
        assert out["stop_loss_inr"] == 1200

    def test_new_events_do_not_break_the_digest(self):
        from services.open15_log_view import summarize_day

        events = [
            {"event": "armed"},
            {"event": "profit_target_locked", "day_pnl": 5400.0},
            {"event": "profit_trail_exit", "day_pnl": 4580.0},
            {"event": "exit", "symbol": "AAA", "pnl": 4580.0, "reason": "profit_trail"},
        ]
        digest = summarize_day(DATE, events)
        assert digest["status"] == "armed"
        assert digest["pnl"] == 4580.0

    def test_risk_status_shape(self):
        svc, _ = _mk_service(
            risk_cfg={
                "profit_lock_enabled": True,
                "profit_target_inr": 5000,
                "stop_loss_enabled": True,
                "stop_loss_inr": 1200,
            }
        )
        svc._today_realized_net = lambda: 0.0
        rs = svc.risk_status()
        assert rs["profit_lock_enabled"] and rs["stop_loss_enabled"]
        assert rs["locked"] is False and rs["trail_done"] is False
        assert rs["day_status"] == "armed"
