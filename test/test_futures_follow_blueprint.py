"""API endpoint tests for blueprints/futures_follow.py.

Builds a minimal Flask app with only the futures_follow blueprint registered, and
monkeypatches the auth + service lookups so no live broker/DB is touched.
"""

import os

# blueprints.futures_follow imports database.auth_db, which requires a pepper at
# import time. Set a throwaway one before the blueprint is imported (the conftest
# tripwire already redirects DATABASE_URL to a temp dir).
os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "0" * 64)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    import blueprints.futures_follow as bp

    # Auth: accept the key "GOOD" only.
    monkeypatch.setattr(bp, "verify_api_key", lambda k: k == "GOOD")

    app = Flask(__name__)
    app.register_blueprint(bp.futures_follow_bp)
    app.config["TESTING"] = True
    return app.test_client()


class _FakeService:
    def __init__(self):
        self.paused = False
        self.resumed = False
        self.closed = False

    def get_status(self):
        return {"mode": "sandbox", "lots_held": 0, "margin_used_inr": 0.0}

    def open_positions_view(self):
        return []

    @property
    def today_entries(self):
        return []

    @property
    def today_exits(self):
        return []

    def lots_held(self):
        return 0

    def margin_used(self):
        return 0.0

    def pause(self):
        self.paused = True
        return {"status": "success", "manual_pause": True}

    def resume(self):
        self.resumed = True
        return {"status": "success", "manual_pause": False}

    def close_all_positions(self):
        self.closed = True
        return [{"nifty_symbol": "NIFTY26JUN24FUT", "status": "success", "order_id": "X"}]


def _install_service(monkeypatch, svc):
    import blueprints.futures_follow as bp

    monkeypatch.setattr(bp, "get_service", lambda: svc)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def test_status_requires_api_key(client):
    assert client.get("/futures_follow_cap50/api/status").status_code == 401


def test_status_rejects_bad_key(client):
    resp = client.get("/futures_follow_cap50/api/status", headers={"X-API-KEY": "BAD"})
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Service-unavailable
# --------------------------------------------------------------------------- #
def test_status_503_when_service_missing(client, monkeypatch):
    _install_service(monkeypatch, None)
    resp = client.get("/futures_follow_cap50/api/status", headers={"X-API-KEY": "GOOD"})
    assert resp.status_code == 503


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #
def test_status_ok(client, monkeypatch):
    _install_service(monkeypatch, _FakeService())
    resp = client.get("/futures_follow_cap50/api/status", headers={"X-API-KEY": "GOOD"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert body["data"]["mode"] == "sandbox"


def test_positions_ok(client, monkeypatch):
    _install_service(monkeypatch, _FakeService())
    resp = client.get("/futures_follow_cap50/api/positions", headers={"X-API-KEY": "GOOD"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["data"]["lots_held"] == 0
    assert body["data"]["open_positions"] == []


def test_pause_invokes_service(client, monkeypatch):
    svc = _FakeService()
    _install_service(monkeypatch, svc)
    resp = client.post("/futures_follow_cap50/api/pause", headers={"X-API-KEY": "GOOD"})
    assert resp.status_code == 200
    assert svc.paused is True


def test_resume_invokes_service(client, monkeypatch):
    svc = _FakeService()
    _install_service(monkeypatch, svc)
    resp = client.post("/futures_follow_cap50/api/resume", headers={"X-API-KEY": "GOOD"})
    assert resp.status_code == 200
    assert svc.resumed is True


def test_close_all_requires_confirm(client, monkeypatch):
    svc = _FakeService()
    _install_service(monkeypatch, svc)
    resp = client.post("/futures_follow_cap50/api/close_all", headers={"X-API-KEY": "GOOD"})
    assert resp.status_code == 400
    assert svc.closed is False


def test_close_all_with_confirm(client, monkeypatch):
    svc = _FakeService()
    _install_service(monkeypatch, svc)
    resp = client.post(
        "/futures_follow_cap50/api/close_all",
        headers={"X-API-KEY": "GOOD"},
        json={"confirm": "yes"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 1
    assert svc.closed is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
