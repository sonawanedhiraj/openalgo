"""API tests for blueprints/cas_straddle.py (issue #740).

Minimal Flask app with only this blueprint; auth + service lookups are
monkeypatched; the config table is an in-memory engine.
"""

from __future__ import annotations

import os

os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "0" * 64)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import scoped_session, sessionmaker  # noqa: E402

from database import cas_straddle_db as db  # noqa: E402


class _FakeService:
    def __init__(self):
        self.paused = self.resumed = self.closed = False
        self.day = {"config": None, "u": {"NIFTY": {"lotsize": 65}, "SENSEX": {"lotsize": 20}}}

    def get_status(self):
        return {"strategy": "cas_320_expiry_straddle", "mode": "sandbox", "armed": False}

    def trade_date(self):
        return "2026-09-22"

    def pause(self):
        self.paused = True
        return {"status": "success", "manual_pause": True}

    def resume(self):
        self.resumed = True
        return {"status": "success", "manual_pause": False}

    def close_all_positions(self):
        self.closed = True
        return [{"symbol": "NIFTY22SEP2623200CE", "exit_price": 120.0}]


@pytest.fixture
def svc():
    return _FakeService()


@pytest.fixture
def client(monkeypatch, svc):
    import blueprints.cas_straddle as bp

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "db_session", sess)
    db.Base.query = sess.query_property()
    db.Base.metadata.create_all(eng)

    monkeypatch.setattr(bp, "verify_api_key", lambda k: k == "GOOD")
    monkeypatch.setattr(bp, "get_service", lambda: svc)
    monkeypatch.setattr(bp, "_session_ok", lambda: False)
    app = Flask(__name__)
    app.register_blueprint(bp.cas_straddle_bp)
    app.config["TESTING"] = True
    yield app.test_client()
    sess.remove()
    eng.dispose()


def test_status_requires_auth(client):
    assert client.get("/cas_320_expiry_straddle/api/status").status_code == 401
    r = client.get("/cas_320_expiry_straddle/api/status", headers={"X-API-KEY": "GOOD"})
    assert r.status_code == 200
    assert r.get_json()["data"]["mode"] == "sandbox"


def test_status_accepts_browser_session(client, monkeypatch):
    import blueprints.cas_straddle as bp

    monkeypatch.setattr(bp, "_session_ok", lambda: True)
    assert client.get("/cas_320_expiry_straddle/api/status").status_code == 200


def test_config_get_defaults_when_unset(client):
    r = client.get("/cas_320_expiry_straddle/api/config", headers={"X-API-KEY": "GOOD"})
    body = r.get_json()["data"]
    assert body["override"] is None
    assert body["effective"]["lots_nifty"] == 1 and body["effective"]["trade_sensex"] is True
    assert body["lotsizes"] == {"NIFTY": 65, "SENSEX": 20}


def test_config_post_validates_and_persists(client):
    r = client.post(
        "/cas_320_expiry_straddle/api/config",
        json={"trade_sensex": False, "lots_nifty": 2, "hard_exit_time": "15:29"},
        headers={"X-API-KEY": "GOOD"},
    )
    assert r.status_code == 200, r.get_json()
    eff = r.get_json()["data"]["effective"]
    assert eff["trade_sensex"] is False and eff["lots_nifty"] == 2
    assert eff["hard_exit_time"] == "15:29:00"
    # bounds are refused, not clamped
    r = client.post(
        "/cas_320_expiry_straddle/api/config",
        json={"lots_sensex": 11},
        headers={"X-API-KEY": "GOOD"},
    )
    assert r.status_code == 400 and "lots_sensex" in r.get_json()["message"]
    r = client.post(
        "/cas_320_expiry_straddle/api/config",
        json={"hard_exit_time": "15:39:00"},
        headers={"X-API-KEY": "GOOD"},
    )
    assert r.status_code == 400
    # an explicit null clears the override
    r = client.post(
        "/cas_320_expiry_straddle/api/config",
        json={"lots_nifty": None},
        headers={"X-API-KEY": "GOOD"},
    )
    assert r.get_json()["data"]["effective"]["lots_nifty"] == 1


def test_config_post_empty_body_rejected(client):
    r = client.post("/cas_320_expiry_straddle/api/config", json={}, headers={"X-API-KEY": "GOOD"})
    assert r.status_code == 400


def test_pause_resume_close_all(client, svc):
    assert client.post("/cas_320_expiry_straddle/api/pause").status_code == 401
    assert (
        client.post("/cas_320_expiry_straddle/api/pause", headers={"X-API-KEY": "GOOD"}).status_code
        == 200
    )
    assert svc.paused
    assert (
        client.post(
            "/cas_320_expiry_straddle/api/resume", headers={"X-API-KEY": "GOOD"}
        ).status_code
        == 200
    )
    assert svc.resumed
    r = client.post(
        "/cas_320_expiry_straddle/api/close_all", json={}, headers={"X-API-KEY": "GOOD"}
    )
    assert r.status_code == 400 and not svc.closed
    r = client.post(
        "/cas_320_expiry_straddle/api/close_all",
        json={"confirm": "yes"},
        headers={"X-API-KEY": "GOOD"},
    )
    assert r.status_code == 200 and r.get_json()["count"] == 1 and svc.closed


def test_positions_sessions_polls_read(client):
    db.record_trade(
        mode="sandbox", trade_date="2026-09-22", underlying="NIFTY", exchange="NFO",
        symbol="NIFTY22SEP2623200CE", strike=23200.0, expiry="22-SEP-26", side="CE",
        lots=1, quantity=65, status="open", fill="real", entry_price=100.0, entry_qty=65,
    )  # fmt: skip
    db.upsert_session("2026-09-22", "NIFTY", atm_strike=23200.0)
    h = {"X-API-KEY": "GOOD"}
    r = client.get("/cas_320_expiry_straddle/api/positions", headers=h)
    assert r.get_json()["data"]["trades"][0]["symbol"] == "NIFTY22SEP2623200CE"
    r = client.get("/cas_320_expiry_straddle/api/sessions?limit=5", headers=h)
    assert r.get_json()["data"]["sessions"][0]["atm_strike"] == 23200.0
    r = client.get("/cas_320_expiry_straddle/api/polls?date=2026-09-22", headers=h)
    assert r.status_code == 200 and r.get_json()["data"]["polls"] == []
    assert (
        client.get("/cas_320_expiry_straddle/api/polls?date=x&limit=abc", headers=h).status_code
        == 400
    )
