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


# ---------------------------------------------------------------------------
# Entry-evaluation history (issue #395)
# ---------------------------------------------------------------------------

_HISTORY_URL = "/futures_follow_cap50/api/entry_breakdown/history"


def _payload(n_signals=0, fails=None, symbols=None):
    return {
        "eval_at": "2026-07-07T15:20:03+05:30",
        "mode": "sandbox",
        "n_signals": n_signals,
        "intraday_source_counts": {"quotes": 30, "aggregator": 0, "historify": 0, "none": 0},
        "cap_skipped": 0,
        "vetoed": 0,
        "per_gate_fail_counts": fails or {"sector": 28, "stock": 24, "vol": 19, "missing_data": 0},
        "symbols": symbols if symbols is not None else [],
    }


def test_history_requires_auth(client, _eval_db):
    assert client.get(_HISTORY_URL).status_code == 401


def test_history_rejects_bad_key(client, _eval_db):
    assert client.get(_HISTORY_URL, headers={"X-API-KEY": "BAD"}).status_code == 401


def test_history_empty_is_success_not_error(client, _eval_db):
    resp = client.get(_HISTORY_URL, headers={"X-API-KEY": "GOOD"})
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["rows"] == []
    assert data["has_more"] is False


def test_history_returns_summaries_newest_first(client, _eval_db):
    for d in ("2026-07-07", "2026-07-08", "2026-07-09"):
        assert _eval_db.upsert_snapshot("futures_follow_cap50", d, _payload())
    rows = client.get(_HISTORY_URL, headers={"X-API-KEY": "GOOD"}).get_json()["data"]["rows"]
    assert [r["eval_date"] for r in rows] == ["2026-07-09", "2026-07-08", "2026-07-07"]
    # Summaries, not payloads: no per-symbol list is shipped.
    assert "symbols" not in rows[0]
    assert rows[0]["gates_passed"] == {"sector": 0, "stock": 0, "vol": 0}


def test_history_has_more_flag_and_limit(client, _eval_db):
    for d in ("2026-07-07", "2026-07-08", "2026-07-09"):
        assert _eval_db.upsert_snapshot("futures_follow_cap50", d, _payload())
    data = client.get(f"{_HISTORY_URL}?limit=2", headers={"X-API-KEY": "GOOD"}).get_json()["data"]
    assert [r["eval_date"] for r in data["rows"]] == ["2026-07-09", "2026-07-08"]
    assert data["has_more"] is True

    data = client.get(f"{_HISTORY_URL}?limit=3", headers={"X-API-KEY": "GOOD"}).get_json()["data"]
    assert data["has_more"] is False


def test_history_before_pages_backwards(client, _eval_db):
    for d in ("2026-07-07", "2026-07-08", "2026-07-09"):
        assert _eval_db.upsert_snapshot("futures_follow_cap50", d, _payload())
    data = client.get(
        f"{_HISTORY_URL}?before=2026-07-09&limit=2", headers={"X-API-KEY": "GOOD"}
    ).get_json()["data"]
    assert [r["eval_date"] for r in data["rows"]] == ["2026-07-08", "2026-07-07"]


@pytest.mark.parametrize("qs", ["limit=abc", "limit=0", "limit=-5", "before=not-a-date"])
def test_history_invalid_params_400(client, _eval_db, qs):
    assert client.get(f"{_HISTORY_URL}?{qs}", headers={"X-API-KEY": "GOOD"}).status_code == 400


def test_history_today_reports_pending_on_a_trading_day(client, _eval_db, monkeypatch):
    """No snapshot yet + a trading day -> the UI shows the pending row."""
    import blueprints.futures_follow as bp  # noqa: F401

    monkeypatch.setattr("services.data_freshness_service.is_trading_day", lambda d: True)
    today = client.get(_HISTORY_URL, headers={"X-API-KEY": "GOOD"}).get_json()["data"]["today"]
    assert today["is_trading_day"] is True
    assert today["snapshot_exists"] is False


def test_history_today_no_pending_row_on_a_holiday(client, _eval_db, monkeypatch):
    """A weekend / NSE holiday must not promise an evaluation that never comes."""
    monkeypatch.setattr("services.data_freshness_service.is_trading_day", lambda d: False)
    today = client.get(_HISTORY_URL, headers={"X-API-KEY": "GOOD"}).get_json()["data"]["today"]
    assert today["is_trading_day"] is False


def test_history_today_snapshot_exists_survives_paging(client, _eval_db, monkeypatch):
    """`snapshot_exists` is queried directly, never inferred from the page — a
    ?before= page never contains today's row."""
    from datetime import datetime, timedelta, timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    today_str = datetime.now(ist).date().isoformat()
    assert _eval_db.upsert_snapshot("futures_follow_cap50", today_str, _payload())
    assert _eval_db.upsert_snapshot("futures_follow_cap50", "2026-01-02", _payload())

    data = client.get(
        f"{_HISTORY_URL}?before={today_str}", headers={"X-API-KEY": "GOOD"}
    ).get_json()["data"]
    assert today_str not in [r["eval_date"] for r in data["rows"]]
    assert data["today"]["snapshot_exists"] is True


def test_history_session_auth_allows_read(client, _eval_db, monkeypatch):
    """The React page authenticates with a session cookie, not an API key."""
    monkeypatch.setattr("utils.session.is_session_valid", lambda: True)
    assert client.get(_HISTORY_URL).status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
