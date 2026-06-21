"""Tests for the in-house scanner browser API (blueprints/scanner_api.py).

Tier 1 read-only endpoints:
  GET /scanner/api/definitions
  GET /scanner/api/definitions/<id>/signals

Uses a bare Flask app (not create_app) to avoid the singleton guard, with an
isolated in-memory SQLite for scanner_db.  Flask session is mocked so tests
don't need a real login flow.
"""

from __future__ import annotations

import json

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _rebind_scanner_db(monkeypatch, tmp_path):
    """Point scanner_db at a fresh SQLite file for one test."""
    import database.scanner_db as sdb

    db_file = str(tmp_path / "scanner_test.db")
    eng = create_engine(
        f"sqlite:///{db_file}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(sdb, "engine", eng, raising=False)
    monkeypatch.setattr(sdb, "db_session", sess, raising=False)
    sdb.Base.metadata.create_all(bind=eng)
    return sdb, sess


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Bare Flask app with scanner_api_bp mounted + isolated scanner DB."""
    sdb, sess = _rebind_scanner_db(monkeypatch, tmp_path)

    # Bypass session decorator — tests run outside a real logged-in session
    monkeypatch.setattr("utils.session.is_session_valid", lambda: True)

    from blueprints.scanner_api import scanner_api_bp

    # Patch the LOCAL binding in the blueprint module (not just database.scanner_db)
    # because `from database.scanner_db import db_session` creates a local alias.
    monkeypatch.setattr("blueprints.scanner_api.db_session", sess)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-key"  # pragma: allowlist secret
    app.register_blueprint(scanner_api_bp)

    with app.test_client() as tc:
        # Inject a fake session user so the endpoint's session.get("user") check passes
        with tc.session_transaction() as flask_sess:
            flask_sess["user"] = "test_user"
        yield tc, sdb

    sess.remove()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _add_definition(sdb, name="test_def", screener_type="buy", rule_module=None, enabled=1):
    from database.scanner_db import ScanDefinition

    sess = sdb.db_session()
    d = ScanDefinition(
        name=name,
        screener_type=screener_type,
        expression_json="{}",
        rule_module=rule_module,
        enabled=enabled,
        created_at="2026-06-21T09:00:00+05:30",
        updated_at="2026-06-21T09:00:00+05:30",
    )
    sess.add(d)
    sess.commit()
    return d.id


def _add_result(
    sdb, definition_id, symbols=None, source="inhouse", posted=0, run_at="2026-06-21T14:00:00+05:30"
):
    from database.scanner_db import ScanResult

    sess = sdb.db_session()
    r = ScanResult(
        scan_definition_id=definition_id,
        run_at=run_at,
        symbols=json.dumps(symbols or ["RELIANCE", "INFY"]),
        source=source,
        posted_to_engine=posted,
    )
    sess.add(r)
    sess.commit()
    return r.id


# ---------------------------------------------------------------------------
# GET /scanner/api/definitions
# ---------------------------------------------------------------------------


def test_list_definitions_empty(client):
    tc, _sdb = client
    res = tc.get("/scanner/api/definitions")
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    assert body["data"] == []


def test_list_definitions_returns_enabled_only(client):
    tc, sdb = client
    _add_definition(sdb, "buy_rule", "buy", enabled=1)
    _add_definition(sdb, "disabled_rule", "sell", enabled=0)

    res = tc.get("/scanner/api/definitions")
    assert res.status_code == 200
    body = res.get_json()
    names = [d["name"] for d in body["data"]]
    assert "buy_rule" in names
    assert "disabled_rule" not in names


def test_list_definitions_includes_latest_signals(client):
    tc, sdb = client
    did = _add_definition(sdb, "sig_def", "buy")
    _add_result(sdb, did, ["HDFCBANK", "ICICIBANK"])

    res = tc.get("/scanner/api/definitions")
    assert res.status_code == 200
    body = res.get_json()
    defs = body["data"]
    assert len(defs) == 1
    d = defs[0]
    assert isinstance(d["today_hit_count"], int)
    assert len(d["latest_signals"]) == 1
    assert "HDFCBANK" in d["latest_signals"][0]["symbols"]


def test_list_definitions_latest_signals_capped_at_5(client):
    tc, sdb = client
    did = _add_definition(sdb, "busy_def", "buy")
    for i in range(8):
        _add_result(sdb, did, [f"STOCK{i}"])

    res = tc.get("/scanner/api/definitions")
    assert res.status_code == 200
    body = res.get_json()
    assert len(body["data"][0]["latest_signals"]) <= 5


# ---------------------------------------------------------------------------
# GET /scanner/api/definitions/<id>/signals
# ---------------------------------------------------------------------------


def test_get_signals_not_found(client):
    tc, _sdb = client
    res = tc.get("/scanner/api/definitions/9999/signals")
    assert res.status_code == 404
    assert res.get_json()["status"] == "error"


def test_get_signals_returns_rows(client):
    tc, sdb = client
    did = _add_definition(sdb, "sell_def", "sell")
    _add_result(sdb, did, ["WIPRO"])

    res = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={"since": "2026-06-01T00:00:00+05:30"},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    data = body["data"]
    assert data["count"] == 1
    assert data["signals"][0]["symbols"] == ["WIPRO"]
    assert data["definition"]["id"] == did


def test_get_signals_limit_respected(client):
    tc, sdb = client
    did = _add_definition(sdb, "multi_sig", "buy")
    for i in range(10):
        _add_result(sdb, did, [f"S{i}"])

    res = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={"since": "2026-06-01T00:00:00+05:30", "limit": "3"},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["data"]["count"] <= 3


def test_get_signals_max_limit_capped(client):
    tc, sdb = client
    did = _add_definition(sdb, "cap_def", "buy")

    res = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={"since": "2026-06-01T00:00:00+05:30", "limit": "9999"},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["data"]["limit"] <= 500


def test_get_signals_response_shape(client):
    tc, sdb = client
    did = _add_definition(sdb, "shape_def", "buy", rule_module="services.scan_rules.fno_buy")
    _add_result(sdb, did, ["TCS"], source="inhouse", posted=1)

    res = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={"since": "2026-06-01T00:00:00+05:30"},
    )
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert data["definition"]["rule_module"] == "services.scan_rules.fno_buy"
    assert data["signals"][0]["posted_to_engine"] is True
    assert data["signals"][0]["source"] == "inhouse"
