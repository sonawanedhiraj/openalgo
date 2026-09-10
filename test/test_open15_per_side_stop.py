"""Per-side stop loss (issue #722).

Replaying the first 41 live open15 fills through the #696 risk rules (#711),
the same Rs2,500 per-trade stop RAISED short P&L (short losers run) and
LOWERED long P&L (long losers do not run, so the stop mostly clips winners on
their dip). One number for both sides is the wrong shape, so the stop now
resolves per side:

- ``stop_loss_inr`` stays the common value and the fallback;
- ``stop_loss_inr_long`` / ``stop_loss_inr_short`` override it for that side
  when set; NULL = same as common; 0 = stop OFF for that side only;
- ``stop_loss_enabled`` is true when ANY side has a positive threshold.

These tests pin the resolution, the monitor picking the SIDE's threshold, the
"unset side follows the common value" contract (so an install that never set a
side keeps the exact pre-#722 behaviour), storage roundtrip, the armed-event
passthrough, risk_status and the config API.
"""

import pytest

import services.open15_pnl_curve as pnl_curve
from services.open15_breakout_service import (
    Open15BreakoutService,
    _resolve_risk_config,
    resolve_day_config,
    stop_loss_for_side,
)

DATE = "2026-09-11"


@pytest.fixture(autouse=True)
def _single_poll_trail(monkeypatch):
    monkeypatch.setenv("OPEN15_TRAIL_CONFIRM_POLLS", "1")


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


def _mk_service(risk_cfg, positions, payload, monkeypatch):
    orders = []

    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = Open15BreakoutService(order_placer=placer)
    svc.day_status = "armed"
    svc._log_date = DATE
    svc.day_config = resolve_day_config(risk_cfg, 0.0)
    svc.positions = positions
    svc._entry_never_filled = lambda _s, _p: False
    svc._reconcile_and_log = lambda **_kw: None
    svc._persist_day_log = lambda: None
    svc._today_realized_net = lambda: 0.0
    svc._alert_stop_loss = lambda *_a: None
    monkeypatch.setattr(pnl_curve, "live_pnl", lambda: payload)
    exited = []
    svc._exit_open_row = lambda sym, _pos, reason: exited.append((sym, reason)) or True
    return svc, exited


def _live(trades):
    return {
        "status": "live",
        "date": DATE,
        "quotes_ok": True,
        "trades": trades,
        "portfolio_mtm": sum(t.get("mtm") or 0.0 for t in trades),
        "poll_interval_s": 5,
    }


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
class TestResolution:
    def test_unset_sides_follow_the_common_value(self):
        cfg = _resolve_risk_config({"stop_loss_enabled": True, "stop_loss_inr": 2500})
        assert cfg["stop_loss_inr"] == 2500
        assert cfg["stop_loss_inr_long"] == 2500
        assert cfg["stop_loss_inr_short"] == 2500
        assert cfg["stop_loss_enabled"] is True

    def test_side_override_and_side_off(self):
        cfg = _resolve_risk_config(
            {
                "stop_loss_enabled": True,
                "stop_loss_inr": 2500,
                "stop_loss_inr_long": 4000,
                "stop_loss_inr_short": 0,
            }
        )
        assert cfg["stop_loss_inr_long"] == 4000
        assert cfg["stop_loss_inr_short"] == 0.0
        assert cfg["stop_loss_enabled"] is True

    def test_enabled_when_only_one_side_is_positive(self):
        cfg = _resolve_risk_config(
            {"stop_loss_enabled": True, "stop_loss_inr": 0, "stop_loss_inr_long": 4000}
        )
        assert cfg["stop_loss_enabled"] is True
        assert cfg["stop_loss_inr_short"] == 0.0  # follows the common 0

    def test_all_zero_disables(self):
        cfg = _resolve_risk_config(
            {
                "stop_loss_enabled": True,
                "stop_loss_inr": 0,
                "stop_loss_inr_long": 0,
                "stop_loss_inr_short": 0,
            }
        )
        assert cfg["stop_loss_enabled"] is False

    def test_side_garbage_falls_back_to_common(self):
        cfg = _resolve_risk_config(
            {"stop_loss_enabled": True, "stop_loss_inr": 2500, "stop_loss_inr_long": "x"}
        )
        assert cfg["stop_loss_inr_long"] == 2500

    def test_stop_loss_for_side_helper(self):
        cfg = {"stop_loss_inr": 2500, "stop_loss_inr_long": 4000, "stop_loss_inr_short": 0}
        assert stop_loss_for_side(cfg, "L") == 4000
        assert stop_loss_for_side(cfg, "long") == 4000
        assert stop_loss_for_side(cfg, "S") == 0.0
        assert stop_loss_for_side(cfg, "SELL") == 0.0
        # older day_config shape without side keys -> common
        assert stop_loss_for_side({"stop_loss_inr": 2500}, "S") == 2500
        assert stop_loss_for_side({}, "L") == 0.0


# --------------------------------------------------------------------------- #
# the monitor
# --------------------------------------------------------------------------- #
class TestMonitor:
    POS = {
        "LLL": {"status": "open", "fill": "real", "side": "L"},
        "SSS": {"status": "open", "fill": "real", "side": "S"},
    }

    def test_each_side_uses_its_own_threshold(self, monkeypatch):
        cfg = {
            "stop_loss_enabled": True,
            "stop_loss_inr": 2500,
            "stop_loss_inr_long": 4000,
            "stop_loss_inr_short": 2500,
        }
        payload = _live([{"symbol": "LLL", "mtm": -3000.0}, {"symbol": "SSS", "mtm": -3000.0}])
        svc, exited = _mk_service(cfg, dict(self.POS), payload, monkeypatch)
        svc._risk_tick()
        assert exited == [("SSS", "stop_loss")]

    def test_long_stops_at_its_own_number(self, monkeypatch):
        cfg = {"stop_loss_enabled": True, "stop_loss_inr": 2500, "stop_loss_inr_long": 4000}
        payload = _live([{"symbol": "LLL", "mtm": -4100.0}, {"symbol": "SSS", "mtm": -2000.0}])
        svc, exited = _mk_service(cfg, dict(self.POS), payload, monkeypatch)
        svc._risk_tick()
        assert exited == [("LLL", "stop_loss")]

    def test_side_off_never_stops_that_side(self, monkeypatch):
        cfg = {"stop_loss_enabled": True, "stop_loss_inr": 2500, "stop_loss_inr_long": 0}
        payload = _live([{"symbol": "LLL", "mtm": -9000.0}, {"symbol": "SSS", "mtm": -2600.0}])
        svc, exited = _mk_service(cfg, dict(self.POS), payload, monkeypatch)
        svc._risk_tick()
        assert exited == [("SSS", "stop_loss")]

    def test_unset_sides_behave_exactly_as_before(self, monkeypatch):
        cfg = {"stop_loss_enabled": True, "stop_loss_inr": 1200}
        payload = _live([{"symbol": "LLL", "mtm": -1300.0}, {"symbol": "SSS", "mtm": -1250.0}])
        svc, exited = _mk_service(cfg, dict(self.POS), payload, monkeypatch)
        svc._risk_tick()
        assert sorted(exited) == [("LLL", "stop_loss"), ("SSS", "stop_loss")]

    def test_payload_side_is_the_fallback_when_the_position_has_none(self, monkeypatch):
        cfg = {"stop_loss_enabled": True, "stop_loss_inr": 2500, "stop_loss_inr_short": 1000}
        positions = {"XXX": {"status": "open", "fill": "real"}}  # no side on the position
        payload = _live([{"symbol": "XXX", "side": "S", "mtm": -1100.0}])
        svc, exited = _mk_service(cfg, positions, payload, monkeypatch)
        svc._risk_tick()
        assert exited == [("XXX", "stop_loss")]

    def test_alert_and_log_carry_the_side_threshold(self, monkeypatch):
        cfg = {"stop_loss_enabled": True, "stop_loss_inr": 2500, "stop_loss_inr_short": 1000}
        payload = _live([{"symbol": "SSS", "mtm": -1100.0}])
        svc, _exited = _mk_service(cfg, {"SSS": dict(self.POS["SSS"])}, payload, monkeypatch)
        seen = []
        svc._alert_stop_loss = lambda sym, mtm, inr: seen.append((sym, mtm, inr))
        svc._risk_tick()
        assert seen == [("SSS", -1100.0, 1000.0)]


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
            stop_loss_enabled=True,
            stop_loss_inr=2500.0,
            stop_loss_inr_long=4000.0,
            stop_loss_inr_short=0.0,
        )
        cfg = get_config()
        assert cfg["stop_loss_inr_long"] == 4000.0
        assert cfg["stop_loss_inr_short"] == 0.0
        assert save_config(30000, "fixed", 1.5, stop_loss_inr=2500.0)
        cfg = get_config()
        assert cfg["stop_loss_inr_long"] is None and cfg["stop_loss_inr_short"] is None

    def test_armed_event_passthrough(self):
        from services.open15_log_view import effective_from_armed

        out = effective_from_armed(
            {
                "event": "armed",
                "stop_loss_enabled": True,
                "stop_loss_inr": 2500,
                "stop_loss_inr_long": 4000,
                "stop_loss_inr_short": 0,
            }
        )
        assert out["stop_loss_inr_long"] == 4000 and out["stop_loss_inr_short"] == 0

    def test_risk_status_carries_both_sides(self, monkeypatch):
        cfg = {"stop_loss_enabled": True, "stop_loss_inr": 2500, "stop_loss_inr_long": 4000}
        svc, _ = _mk_service(cfg, {}, _live([]), monkeypatch)
        st = svc.risk_status()
        assert st["stop_loss_inr_long"] == 4000 and st["stop_loss_inr_short"] == 2500

    def test_api_config_stores_blank_as_null_and_zero_as_zero(self, monkeypatch):
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
            json={"stop_loss_inr": 2500, "stop_loss_inr_long": 4000, "stop_loss_inr_short": ""},
        )
        assert r.status_code == 200, r.data
        assert saved["stop_loss_inr_long"] == 4000.0
        assert saved["stop_loss_inr_short"] is None
        r = client.post("/open15_vol_breakout/api/config", json={"stop_loss_inr_short": 0})
        assert r.status_code == 200
        assert saved["stop_loss_inr_short"] == 0.0
        r = client.get("/open15_vol_breakout/api/config")
        d = r.get_json()["env_defaults"]
        assert d["stop_loss_inr_long"] is None and d["stop_loss_inr_short"] is None
