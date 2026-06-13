"""Integration test for the ``GET /preflight`` Flask route.

The route MUST return 200 in both go and abort cases — non-200 is reserved
for actual route errors (orchestrator blew up). The caller distinguishes
proceed-vs-abort by reading ``go_decision``, not the HTTP status.
"""

import datetime as dt

import pytest
import pytz
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture
def app_with_preflight(monkeypatch, tmp_path):
    """Bare Flask app with the preflight blueprint mounted + in-memory DBs."""
    from blueprints.preflight import preflight_bp
    from database import daily_intent_db as dim
    from database import scan_cycle_db as scdb

    di_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    di_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=di_engine))
    monkeypatch.setattr(dim, "engine", di_engine)
    monkeypatch.setattr(dim, "db_session", di_session)
    dim.Base.metadata.create_all(di_engine)

    sc_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sc_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=sc_engine))
    monkeypatch.setattr(scdb, "engine", sc_engine)
    monkeypatch.setattr(scdb, "db_session", sc_session)
    scdb.Base.metadata.create_all(sc_engine)

    # Mocked clock — Thursday 2026-05-28 11:30 IST, in market.
    fake_now = IST.localize(dt.datetime(2026, 5, 28, 11, 30, 0))
    monkeypatch.setattr("services.preflight_service._now_ist", lambda: fake_now)
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    # ``get_recent_cycles`` uses real wallclock for its 24h cutoff. Anchor it
    # to fake_now so cycles inserted at the fixture date stay visible to the
    # recent_cycles gate regardless of when this test runs.
    def _fake_get_recent_cycles(hours: int = 24):
        cutoff = (fake_now - dt.timedelta(hours=hours)).isoformat()
        sess = scdb.db_session
        try:
            rows = (
                sess.query(scdb.ScanCycle)
                .filter(scdb.ScanCycle.started_at >= cutoff)
                .order_by(scdb.ScanCycle.started_at.desc())
                .all()
            )
            return [scdb._cycle_to_dict(r) for r in rows]
        finally:
            sess.remove()

    monkeypatch.setattr("services.scan_cycle_service.get_recent_cycles", _fake_get_recent_cycles)
    monkeypatch.setattr(
        "services.preflight_service._check_broker_session",
        lambda: {
            "ok": True,
            "broker": "zerodha",
            "user": "VU3790",
            "reason": None,
        },
    )

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(preflight_bp)

    yield app, dim, scdb, fake_now

    di_session.remove()
    sc_session.remove()
    di_engine.dispose()
    sc_engine.dispose()


def _insert_cycle(scdb, started_at_iso: str) -> None:
    row = scdb.ScanCycle(started_at=started_at_iso, cycle_kind="chartink", post_status="ok")
    scdb.db_session.add(row)
    scdb.db_session.commit()
    scdb.db_session.remove()


def _set_intent(intent: str, date_str: str = "2026-05-28"):
    from services.mode_service import set_daily_intent_safe

    return set_daily_intent_safe(intent, set_by="operator", date_str=date_str)


def test_preflight_route_returns_200_with_go(app_with_preflight):
    """Happy path — every check passes, route returns 200 with go_decision='go'."""
    app, dim, scdb, _ = app_with_preflight
    _set_intent("live")
    recent = IST.localize(dt.datetime(2026, 5, 28, 11, 25, 0))
    _insert_cycle(scdb, recent.isoformat())

    client = app.test_client()
    resp = client.get("/preflight")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["go_decision"] == "go"
    assert data["reasons"] == []
    assert data["checks"]["intent"]["value"] == "live"
    assert "checked_at" in data


def test_preflight_route_returns_200_with_abort(app_with_preflight):
    """Failed checks must NOT produce a 4xx — the route is informational.

    No daily_intent on record, so intent fails → go_decision='abort', but
    the HTTP response is still 200.
    """
    app, dim, scdb, _ = app_with_preflight
    # No _set_intent() and no cycles inserted.

    client = app.test_client()
    resp = client.get("/preflight")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert data["go_decision"] == "abort"
    assert len(data["reasons"]) >= 1
    assert any("no daily_intent" in r for r in data["reasons"])
