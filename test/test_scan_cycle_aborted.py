"""Tests for the aborted-cycle trace (Round 2 dev-stability).

Covers both the service helper ``record_aborted_cycle`` and the HTTP endpoint
``POST /chartink/cycle/aborted``. The point is the silent-gap fix: a
triggered-but-aborted run must leave a scan_cycle row with
``post_status='aborted_<stage>'`` and the abort reason recoverable from the
audit, instead of nothing.

All DB work runs against a fresh in-memory SQLite, monkeypatched onto the
scan_cycle_db module — same isolation pattern as test_scan_cycle_service.py and
test_chartink_webhook_audit.py. The live DB is never touched.
"""

import json

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_cycle_db(monkeypatch):
    """Point scan_cycle_db at a fresh in-memory SQLite for one test."""
    from database import scan_cycle_db as scdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))

    monkeypatch.setattr(scdb, "engine", test_engine)
    monkeypatch.setattr(scdb, "db_session", test_session)
    scdb.Base.metadata.create_all(test_engine)

    yield scdb

    test_session.remove()
    test_engine.dispose()


@pytest.fixture
def app_with_chartink(fresh_cycle_db):
    """Bare Flask app with the chartink blueprint mounted, limiter bound."""
    from blueprints.chartink import chartink_bp
    from limiter import limiter

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    limiter.init_app(app)
    app.register_blueprint(chartink_bp)
    return app


# ---------------------------------------------------------------------------
# Service helper
# ---------------------------------------------------------------------------


def test_record_aborted_cycle_writes_row(fresh_cycle_db):
    from services import scan_cycle_service

    result = scan_cycle_service.record_aborted_cycle(
        abort_reason="preflight: daily_intent not set",
        abort_stage="preflight",
    )

    assert result["id"] > 0
    assert result["post_status"] == "aborted_preflight"
    assert result["started_at"] is not None

    row = (
        fresh_cycle_db.db_session.query(fresh_cycle_db.ScanCycle).filter_by(id=result["id"]).first()
    )
    assert row is not None
    assert row.post_status == "aborted_preflight"
    assert row.cycle_kind == "chartink"
    # Aborted cycles are terminal — completed_at is stamped at insert.
    assert row.completed_at is not None
    assert row.completed_at == row.started_at

    payload = json.loads(row.error_payload)
    assert payload["abort_reason"] == "preflight: daily_intent not set"
    assert payload["abort_stage"] == "preflight"
    assert payload["scan_name"] == "fno-scan-cycle"


def test_record_aborted_cycle_stores_operator_intent_and_metadata(fresh_cycle_db):
    from services import scan_cycle_service

    result = scan_cycle_service.record_aborted_cycle(
        scan_name="fno-scan-cycle",
        cycle_kind="manual",
        abort_reason="broker session dead",
        abort_stage="other",
        operator_intent="live",
        metadata={"errors_over_threshold": 7},
    )

    row = (
        fresh_cycle_db.db_session.query(fresh_cycle_db.ScanCycle).filter_by(id=result["id"]).first()
    )
    assert row.cycle_kind == "manual"
    assert row.operator_intent == "live"
    payload = json.loads(row.error_payload)
    assert payload["metadata"] == {"errors_over_threshold": 7}


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("preflight", "aborted_preflight"),
        ("scrape", "aborted_scrape"),
        ("post", "aborted_post"),
        ("market_closed", "aborted_market_closed"),
        ("other", "aborted_other"),
    ],
)
def test_record_aborted_cycle_post_status_for_each_stage(fresh_cycle_db, stage, expected):
    from services import scan_cycle_service

    result = scan_cycle_service.record_aborted_cycle(
        abort_reason="x",
        abort_stage=stage,
    )
    assert result["post_status"] == expected

    row = (
        fresh_cycle_db.db_session.query(fresh_cycle_db.ScanCycle).filter_by(id=result["id"]).first()
    )
    assert row.post_status == expected


def test_record_aborted_cycle_failsafe_on_db_outage(monkeypatch):
    """A broken session must not raise — returns id=-1 instead."""
    from database import scan_cycle_db as scdb
    from services import scan_cycle_service

    class _BrokenSession:
        def add(self, *a, **kw):
            raise RuntimeError("simulated DB outage")

        def commit(self):
            raise RuntimeError("simulated DB outage")

        def rollback(self):
            return None

        def remove(self):
            return None

    monkeypatch.setattr(scdb, "db_session", _BrokenSession())
    result = scan_cycle_service.record_aborted_cycle(abort_reason="x", abort_stage="preflight")
    assert result["id"] == -1
    assert result["post_status"] == "aborted_preflight"


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------


def test_endpoint_valid_payload_returns_200_and_row_lands(app_with_chartink, fresh_cycle_db):
    client = app_with_chartink.test_client()
    resp = client.post(
        "/chartink/cycle/aborted",
        data=json.dumps(
            {
                "abort_reason": "preflight: errors over threshold (12)",
                "abort_stage": "preflight",
                "scan_name": "fno-scan-cycle",
            }
        ),
        content_type="application/json",
    )

    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body["post_status"] == "aborted_preflight"
    assert body["id"] > 0

    row = fresh_cycle_db.db_session.query(fresh_cycle_db.ScanCycle).filter_by(id=body["id"]).first()
    assert row is not None
    assert row.post_status == "aborted_preflight"
    payload = json.loads(row.error_payload)
    assert "errors over threshold" in payload["abort_reason"]


def test_endpoint_missing_abort_reason_returns_400(app_with_chartink, fresh_cycle_db):
    client = app_with_chartink.test_client()
    resp = client.post(
        "/chartink/cycle/aborted",
        data=json.dumps({"abort_stage": "preflight"}),
        content_type="application/json",
    )

    assert resp.status_code == 400
    assert "abort_reason" in resp.get_json()["error"]

    # No row should have been written.
    rows = fresh_cycle_db.db_session.query(fresh_cycle_db.ScanCycle).all()
    assert rows == []


def test_endpoint_blank_abort_reason_returns_400(app_with_chartink, fresh_cycle_db):
    client = app_with_chartink.test_client()
    resp = client.post(
        "/chartink/cycle/aborted",
        data=json.dumps({"abort_reason": "   ", "abort_stage": "preflight"}),
        content_type="application/json",
    )
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("preflight", "aborted_preflight"),
        ("scrape", "aborted_scrape"),
        ("post", "aborted_post"),
        ("market_closed", "aborted_market_closed"),
    ],
)
def test_endpoint_various_stages(app_with_chartink, fresh_cycle_db, stage, expected):
    client = app_with_chartink.test_client()
    resp = client.post(
        "/chartink/cycle/aborted",
        data=json.dumps({"abort_reason": "x", "abort_stage": stage}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["post_status"] == expected


def test_endpoint_defaults_stage_to_other(app_with_chartink, fresh_cycle_db):
    """abort_stage omitted → defaults to 'other'."""
    client = app_with_chartink.test_client()
    resp = client.post(
        "/chartink/cycle/aborted",
        data=json.dumps({"abort_reason": "unknown failure"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["post_status"] == "aborted_other"
