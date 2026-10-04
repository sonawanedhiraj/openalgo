"""cas_auction_reversal Settings config (issue #755): precedence UI row → env seed →
code default, refusing validation, the /api/config roundtrip + auth, and the arm
picking up a saved value."""

from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

import database.cas_auction_reversal_db as cdb
import services.cas_auction_reversal_service as svc_mod
from services.cas_auction_reversal_service import (
    DEFAULTS,
    CasAuctionReversalService,
    config_sources,
    production_config_reader,
    resolve_config,
    validate_config,
)

IST = timezone(timedelta(hours=5, minutes=30))
ENV_KEYS = (
    "CAS_AR_THRESHOLD_PCT",
    "CAS_AR_MAX_POSITIONS",
    "CAS_AR_CAPITAL_PER_TRADE_INR",
    "CAS_AR_POLL_INTERVAL_S",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    cdb.init_db()
    cdb.db_session.query(cdb.CasArConfig).delete()
    cdb.db_session.commit()
    cdb.db_session.remove()
    yield


# --------------------------------------------------------------------------- precedence
def test_defaults_when_nothing_is_set():
    assert resolve_config(None) == {
        "threshold_pct": 0.5,
        "max_positions": 10,
        "capital_per_trade_inr": 50_000.0,
        "poll_interval_s": 15,
    }
    assert set(config_sources(None).values()) == {"default"}


def test_env_seed_beats_default_and_ui_beats_env(monkeypatch):
    monkeypatch.setenv("CAS_AR_THRESHOLD_PCT", "0.75")
    monkeypatch.setenv("CAS_AR_MAX_POSITIONS", "6")
    row = {"threshold_pct": 1.0, "max_positions": None}
    eff = resolve_config(row)
    assert eff["threshold_pct"] == 1.0  # UI wins
    assert eff["max_positions"] == 6  # NULL in the row → env
    assert eff["capital_per_trade_inr"] == DEFAULTS["capital_per_trade_inr"]
    assert config_sources(row) == {
        "threshold_pct": "ui",
        "max_positions": "env",
        "capital_per_trade_inr": "default",
        "poll_interval_s": "default",
    }


def test_out_of_range_env_seed_is_clamped_not_trusted(monkeypatch):
    monkeypatch.setenv("CAS_AR_MAX_POSITIONS", "500")
    monkeypatch.setenv("CAS_AR_THRESHOLD_PCT", "garbage")
    eff = resolve_config(None)
    assert eff["max_positions"] == 30
    assert eff["threshold_pct"] == 0.5  # unparseable env → default


# --------------------------------------------------------------------------- validation
def test_validate_refuses_out_of_bounds_and_non_integers():
    values, errors = validate_config(
        {"threshold_pct": 0.1, "max_positions": 2.5, "capital_per_trade_inr": "abc"}
    )
    assert values == {}
    assert any("threshold_pct must be between 0.2 and 5" in e for e in errors)
    assert any("max_positions must be a whole number" in e for e in errors)
    assert any("capital_per_trade_inr must be a number" in e for e in errors)


def test_validate_accepts_and_types_values_and_null_clears():
    values, errors = validate_config(
        {"threshold_pct": "0.75", "max_positions": 8, "poll_interval_s": None}
    )
    assert errors == []
    assert values == {"threshold_pct": 0.75, "max_positions": 8, "poll_interval_s": None}
    assert isinstance(values["max_positions"], int)


# --------------------------------------------------------------------------- DB + reader
def test_save_then_reader_returns_effective_config():
    stored = cdb.save_config({"threshold_pct": 0.75, "capital_per_trade_inr": 75_000.0})
    assert stored["threshold_pct"] == 0.75 and stored["max_positions"] is None
    eff = production_config_reader()
    assert eff["threshold_pct"] == 0.75 and eff["capital_per_trade_inr"] == 75_000.0
    assert eff["max_positions"] == 10
    cdb.save_config({"threshold_pct": None})  # clearing falls back
    assert production_config_reader()["threshold_pct"] == 0.5


def test_arm_runs_with_the_saved_config():
    cdb.save_config({"threshold_pct": 1.0, "max_positions": 3})
    now = datetime(2026, 10, 5, 15, 14, 30, tzinfo=IST)
    svc = CasAuctionReversalService(
        universe_provider=lambda: ["AAA"],
        quote_batch=lambda syms: {},
        order_placer=lambda o: {},
        fill_reader=lambda oid: None,
        book_reader=lambda: (None, None),
        mode_resolver=lambda: "sandbox",
        notifier=lambda m: None,
        trading_day_checker=lambda d: True,
        now=lambda: now,
        sleep=lambda s: None,
    )
    svc._ensure_monitor_thread = lambda: None
    svc.arm()
    assert svc.day["config"]["threshold_pct"] == 1.0
    assert svc.day["config"]["max_positions"] == 3


# --------------------------------------------------------------------------- blueprint
@pytest.fixture
def client(monkeypatch):
    import blueprints.cas_auction_reversal as bp_mod

    state = {"key_ok": False, "session_ok": False}
    monkeypatch.setattr(bp_mod, "verify_api_key", lambda k: state["key_ok"] and k == "good")
    monkeypatch.setattr(bp_mod, "_session_ok", lambda: state["session_ok"])
    monkeypatch.setattr(svc_mod, "_SINGLETON", None)
    app = Flask(__name__)
    app.register_blueprint(bp_mod.cas_auction_reversal_bp)
    return app.test_client(), state


def test_config_requires_auth(client):
    c, _state = client
    assert c.get("/cas_auction_reversal/api/config").status_code == 401
    assert c.post("/cas_auction_reversal/api/config", json={"threshold_pct": 1}).status_code == 401


def test_config_roundtrip_with_session(client):
    c, state = client
    state["session_ok"] = True
    r = c.get("/cas_auction_reversal/api/config")
    d = r.get_json()["data"]
    assert r.status_code == 200 and d["override"] is None
    assert d["effective"]["threshold_pct"] == 0.5 and d["sources"]["threshold_pct"] == "default"
    assert d["bounds"]["max_positions"] == {"min": 1, "max": 30, "integer": True}
    assert d["applies_at"] == "next 15:14:30 IST arm"

    r = c.post("/cas_auction_reversal/api/config", json={"threshold_pct": 0.75})
    d = r.get_json()["data"]
    assert r.status_code == 200
    assert d["effective"]["threshold_pct"] == 0.75 and d["sources"]["threshold_pct"] == "ui"
    assert d["override"]["updated_by"] == "ui"


def test_config_post_refuses_bad_values_and_stores_nothing(client):
    c, state = client
    state["session_ok"] = True
    r = c.post("/cas_auction_reversal/api/config", json={"max_positions": 99})
    assert r.status_code == 400
    assert "max_positions must be between 1 and 30" in r.get_json()["message"]
    assert cdb.get_config() is None
    assert c.post("/cas_auction_reversal/api/config", json={}).status_code == 400


def test_config_post_with_api_key_is_attributed_to_api(client):
    c, state = client
    state["key_ok"] = True
    r = c.post(
        "/cas_auction_reversal/api/config",
        json={"max_positions": 5},
        headers={"X-API-KEY": "good"},
    )
    assert r.status_code == 200
    assert r.get_json()["data"]["override"]["updated_by"] == "api"
