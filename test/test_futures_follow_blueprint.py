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


# --------------------------------------------------------------------------- #
# Entry-evaluation breakdown (issue #352)
# --------------------------------------------------------------------------- #
@pytest.fixture
def _eval_db(monkeypatch):
    """Rebind database.futures_follow_eval_db to a fresh in-memory DB per test."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker

    from database import futures_follow_eval_db as eval_db

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(eval_db, "engine", eng)
    monkeypatch.setattr(eval_db, "db_session", sess)
    eval_db.Base.query = sess.query_property()
    eval_db.Base.metadata.create_all(eng)
    yield eval_db
    sess.remove()
    eng.dispose()


def test_entry_breakdown_requires_auth(client, _eval_db):
    assert client.get("/futures_follow_cap50/api/entry_breakdown").status_code == 401


def test_entry_breakdown_rejects_bad_key(client, _eval_db):
    resp = client.get("/futures_follow_cap50/api/entry_breakdown", headers={"X-API-KEY": "BAD"})
    assert resp.status_code == 401


def test_entry_breakdown_null_when_no_snapshot(client, _eval_db):
    """No evaluation recorded yet -> {status: success, data: null} (the UI shows
    'no evaluation recorded yet', not an error)."""
    resp = client.get("/futures_follow_cap50/api/entry_breakdown", headers={"X-API-KEY": "GOOD"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert body["data"] is None


def test_entry_breakdown_returns_payload_for_date(client, _eval_db):
    payload = {
        "eval_at": "2026-07-06T15:20:00+05:30",
        "mode": "sandbox",
        "n_signals": 0,
        "intraday_source_counts": {"quotes": 30, "aggregator": 0, "historify": 0, "none": 0},
        "cap_skipped": 0,
        "vetoed": 0,
        "per_gate_fail_counts": {"sector": 25, "stock": 3, "vol": 8, "missing_data": 0},
        "symbols": [],
    }
    assert _eval_db.upsert_snapshot("futures_follow_cap50", "2026-07-06", payload)
    resp = client.get(
        "/futures_follow_cap50/api/entry_breakdown?date=2026-07-06",
        headers={"X-API-KEY": "GOOD"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert body["data"]["eval_date"] == "2026-07-06"
    assert body["data"]["payload"]["intraday_source_counts"]["quotes"] == 30


def test_entry_breakdown_invalid_date_400(client, _eval_db):
    resp = client.get(
        "/futures_follow_cap50/api/entry_breakdown?date=not-a-date",
        headers={"X-API-KEY": "GOOD"},
    )
    assert resp.status_code == 400


def test_entry_breakdown_session_auth_allows_read(client, _eval_db, monkeypatch):
    """A valid logged-in browser session (no API key) can read the breakdown —
    that's how the React strategy page calls it."""
    import blueprints.futures_follow as bp  # noqa: F401 (imported for parity)

    monkeypatch.setattr("utils.session.is_session_valid", lambda: True)
    resp = client.get("/futures_follow_cap50/api/entry_breakdown")
    assert resp.status_code == 200
    assert resp.get_json()["data"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
