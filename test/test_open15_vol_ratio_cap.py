"""Volume-ratio CEILING at the trigger (issue #721).

The entry gate fires the instant cum-volume-in-minute >= ``vol_mult`` x the
average minute volume while price is beyond the 09:15 level. The ratio at that
instant is therefore a fact about *which condition came last*: close to the gate
means price was already beyond the level and volume caught up (steady build);
far above it means the volume arrived while price sat at the level (absorption)
or in one climactic print. On the first 42 real fills (2026-08-18..09-10) the
latter was 2 winners of 8, on both sides.

These tests pin:

- config resolution (env seed 0 = off, stored row wins, clamp, a ceiling at or
  below ``vol_mult`` resolves OFF with a warning rather than refusing every
  trigger),
- the ``_enter`` gate: a capped trigger journals a sim-priced
  ``entry_skipped . vol_ratio_cap`` row, places nothing and consumes no
  ``max_trades`` slot; a trigger below the ceiling enters normally; ceiling 0
  changes nothing,
- the ceiling applies to the shadow side too (one rule for both cohorts),
- ``vol_ratio`` rides every ``entry_skipped`` event,
- config storage roundtrip, the ``armed``-event passthrough, and the /api/config
  clamp.
"""

import pytest

from services.open15_breakout_service import (
    Open15BreakoutService,
    clamp_max_vol_ratio,
    resolve_day_config,
    resolve_max_vol_ratio,
)

DATE = "2026-09-11"


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


def _mk_service(cfg=None):
    orders = []

    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = Open15BreakoutService(order_placer=placer)
    svc.day_status = "armed"
    svc._log_date = DATE
    svc.day_config = resolve_day_config(cfg or {}, 0.0)
    svc.positions = {}
    svc._entry_never_filled = lambda _s, _p: False
    svc._reconcile_and_log = lambda **_kw: None
    svc._persist_day_log = lambda: None
    return svc, orders


def _action(symbol, ratio, side="L", shadow=False):
    return {
        "symbol": symbol,
        "side": side,
        "price": 100.0,
        "gap_pct": 2.0 if side == "L" else -2.0,
        "level": 99.0 if side == "L" else 101.0,
        "baseline_vol": 1000.0,
        "cum_vol_at_trigger": 1000.0 * ratio,
        "trigger_minute": "09:18",
        "trigger_second": 5,
        "watch_source": "seed",
        "shadow": shadow,
    }


def _events(svc, kind):
    return [e for e in svc.day_log if e.get("event") == kind]


def _rows():
    from database.open15_breakout_db import Open15Trade, db_session

    rows = [
        {
            "symbol": r.symbol,
            "status": r.status,
            "reason": r.reason,
            "fill": r.fill,
            "quantity": r.quantity,
            "sim_quantity": r.sim_quantity,
        }
        for r in db_session.query(Open15Trade).all()
    ]
    db_session.remove()
    return rows


# --------------------------------------------------------------------------- #
# config resolution
# --------------------------------------------------------------------------- #
class TestConfig:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("OPEN15_MAX_VOL_RATIO", raising=False)
        assert resolve_day_config({}, 0.0)["max_vol_ratio"] == 0.0

    def test_env_seed(self, monkeypatch):
        monkeypatch.setenv("OPEN15_MAX_VOL_RATIO", "1.7")
        assert resolve_day_config({}, 0.0)["max_vol_ratio"] == 1.7

    def test_stored_row_wins_including_zero(self, monkeypatch):
        monkeypatch.setenv("OPEN15_MAX_VOL_RATIO", "1.7")
        assert resolve_day_config({"max_vol_ratio": 2.2}, 0.0)["max_vol_ratio"] == 2.2
        # a stored 0 beats a non-zero env seed: "off" is a decision, not an absence
        assert resolve_day_config({"max_vol_ratio": 0}, 0.0)["max_vol_ratio"] == 0.0

    def test_clamp(self):
        assert clamp_max_vol_ratio("garbage", 0.0) == 0.0
        assert clamp_max_vol_ratio(-3, 0.0) == 0.0
        assert clamp_max_vol_ratio(99, 0.0) == 10.0
        assert clamp_max_vol_ratio("1.7", 0.0) == 1.7

    def test_ceiling_at_or_below_gate_resolves_off(self):
        # 1.5 gate, 1.5 ceiling -> every trigger would be refused; OFF instead
        assert resolve_max_vol_ratio({"max_vol_ratio": 1.5}, 1.5) == 0.0
        assert resolve_max_vol_ratio({"max_vol_ratio": 1.2}, 1.5) == 0.0
        assert resolve_max_vol_ratio({"max_vol_ratio": 1.6}, 1.5) == 1.6
        cfg = resolve_day_config({"vol_mult": 2.0, "max_vol_ratio": 1.7}, 0.0)
        assert cfg["max_vol_ratio"] == 0.0


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
class TestEntryGate:
    def test_capped_trigger_is_a_sim_skip_with_no_order(self, monkeypatch):
        monkeypatch.setenv("OPEN15_SIM_SKIPPED_ENABLED", "true")
        svc, orders = _mk_service({"max_vol_ratio": 1.7, "instrument": "stock"})
        svc._enter(_action("AAA", 2.6))
        assert orders == []
        skips = _events(svc, "entry_skipped")
        assert len(skips) == 1
        assert skips[0]["reason"] == "vol_ratio_cap"
        assert skips[0]["fill"] == "sim"
        assert skips[0]["vol_ratio"] == 2.6
        (row,) = _rows()
        assert row["status"] == "skipped" and row["reason"] == "vol_ratio_cap"
        assert row["fill"] == "sim" and row["quantity"] == 0 and row["sim_quantity"] > 0
        # registered as a SIM position so flatten prices it at the exit time
        assert svc.positions["AAA"]["status"] == "sim"
        assert svc.positions["AAA"]["no_order_attempted"] is True

    def test_exactly_at_ceiling_is_capped(self):
        svc, orders = _mk_service({"max_vol_ratio": 1.7, "instrument": "stock"})
        svc._enter(_action("AAA", 1.7))
        assert orders == [] and _events(svc, "entry_skipped")[0]["reason"] == "vol_ratio_cap"

    def test_below_ceiling_enters_normally(self):
        svc, orders = _mk_service({"max_vol_ratio": 1.7, "instrument": "stock"})
        svc._enter(_action("AAA", 1.55))
        assert len(orders) == 1 and orders[0]["action"] == "BUY"
        assert _events(svc, "entry_skipped") == []
        assert _events(svc, "entry")[0]["vol_ratio"] == 1.55

    def test_ceiling_off_changes_nothing(self):
        svc, orders = _mk_service({"max_vol_ratio": 0, "instrument": "stock"})
        svc._enter(_action("AAA", 4.0))
        assert len(orders) == 1

    def test_capped_trigger_does_not_consume_a_slot(self):
        """max_trades=1: the capped AAA must leave the slot for BBB."""
        svc, orders = _mk_service({"max_vol_ratio": 1.7, "instrument": "stock", "max_trades": 1})
        svc._enter(_action("AAA", 3.0))
        svc._enter(_action("BBB", 1.52))
        assert [o["symbol"] for o in orders] == ["BBB"]
        reasons = [e["reason"] for e in _events(svc, "entry_skipped")]
        assert reasons == ["vol_ratio_cap"]
        # and the cap itself still works after that
        svc._enter(_action("CCC", 1.52))
        assert [o["symbol"] for o in orders] == ["BBB"]
        assert [e["reason"] for e in _events(svc, "entry_skipped")] == [
            "vol_ratio_cap",
            "max_trades_cap",
        ]

    def test_shadow_side_is_capped_by_the_same_rule(self):
        svc, orders = _mk_service(
            {
                "max_vol_ratio": 1.7,
                "instrument": "stock",
                "trade_side": "long_only",
                "shadow_excluded_side": True,
            }
        )
        svc._enter(_action("SSS", 2.4, side="S", shadow=True))
        assert orders == []
        assert _events(svc, "entry_shadow") == []
        skips = _events(svc, "entry_skipped")
        assert len(skips) == 1 and skips[0]["reason"] == "vol_ratio_cap"
        # below the ceiling the shadow path is untouched
        svc._enter(_action("TTT", 1.6, side="S", shadow=True))
        assert orders == [] and len(_events(svc, "entry_shadow")) == 1

    def test_other_skips_carry_the_ratio_too(self, monkeypatch):
        monkeypatch.setenv("OPEN15_SIM_SKIPPED_ENABLED", "false")
        svc, _orders = _mk_service({"profit_lock_enabled": True, "profit_target_inr": 5000})
        svc._risk["locked"] = True
        svc._enter(_action("AAA", 2.0))
        (skip,) = _events(svc, "entry_skipped")
        assert skip["reason"] == "profit_target_locked" and skip["vol_ratio"] == 2.0


# --------------------------------------------------------------------------- #
# storage, armed passthrough, API clamp
# --------------------------------------------------------------------------- #
class TestPlumbing:
    def test_config_roundtrip(self):
        from database.open15_breakout_db import get_config, save_config

        assert save_config(30000, "fixed", 1.5, max_vol_ratio=1.7)
        assert get_config()["max_vol_ratio"] == 1.7
        assert save_config(30000, "fixed", 1.5)
        assert get_config()["max_vol_ratio"] is None

    def test_armed_event_passthrough(self):
        from services.open15_log_view import effective_from_armed

        out = effective_from_armed({"event": "armed", "vol_mult": 1.5, "max_vol_ratio": 1.7})
        assert out["max_vol_ratio"] == 1.7

    def test_digest_counts_the_capped_trigger_as_sim(self):
        from services.open15_log_view import summarize_day

        events = [
            {"event": "armed", "max_vol_ratio": 1.7},
            {
                "event": "entry_skipped",
                "symbol": "AAA",
                "reason": "vol_ratio_cap",
                "fill": "sim",
                "vol_ratio": 2.6,
            },
            {"event": "exit_sim", "symbol": "AAA", "pnl": -120.0, "fill": "sim"},
        ]
        digest = summarize_day(DATE, events)
        assert digest["sim"] == 1
        assert digest["entered"] == 0

    def test_api_config_clamps_and_stores(self, monkeypatch):
        """Blueprint-only app, same pattern as test_open15_time_window."""
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
            json={"margin_per_slot": 30000, "sizing_mode": "fixed", "max_vol_ratio": 99},
        )
        assert r.status_code == 200, r.data
        assert saved["max_vol_ratio"] == 10.0  # clamped, not rejected
        # an empty field means "leave it at the env seed", never 0
        r = client.post("/open15_vol_breakout/api/config", json={"max_vol_ratio": ""})
        assert r.status_code == 200
        assert saved["max_vol_ratio"] is None
        # GET serves the seed so the form can render it
        r = client.get("/open15_vol_breakout/api/config")
        assert r.status_code == 200
        assert "max_vol_ratio" in r.get_json()["env_defaults"]
