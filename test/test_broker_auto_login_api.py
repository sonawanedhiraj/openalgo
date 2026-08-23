"""Tests for the headless auto-login API (issue #654).

Covers ``/api/broker-auto-login``: save→status roundtrip, password never echoed,
the manual login trigger's prerequisite checks + result passthrough, delete, and
the ``"user" in session`` gate (issue #462 — no ``check_session_validity``).

Hermetic: the global ``test/conftest.py`` redirect points every DB env var at a
throwaway temp DB before any ``database.*`` import binds its engine.
"""

from __future__ import annotations

import pytest
from flask import Flask

USER_ID = "AB1234"
PASSWORD = "s3cret-login-pw"  # pragma: allowlist secret
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # pragma: allowlist secret


@pytest.fixture
def client():
    import database.broker_login_credentials_db as creds_db
    import database.broker_totp_db as totp_db
    from blueprints.broker_auto_login import broker_auto_login_bp

    creds_db.init_db()
    totp_db.init_db()

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"  # pragma: allowlist secret
    app.register_blueprint(broker_auto_login_bp)

    tc = app.test_client()
    with tc.session_transaction() as sess:
        sess["user"] = "tester"  # broker-connect page state (no logged_in)

    yield tc, creds_db, totp_db

    for db, model in (
        (creds_db, creds_db.BrokerLoginCredential),
        (totp_db, totp_db.BrokerTotpSecret),
    ):
        try:
            db.db_session.query(model).delete()
            db.db_session.commit()
        finally:
            db.db_session.remove()


def test_status_before_and_after_save(client):
    tc, _, _ = client

    resp = tc.get("/api/broker-auto-login/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["has_credentials"] is False
    assert body["user_id"] is None

    resp = tc.post(
        "/api/broker-auto-login/credentials",
        json={"user_id": USER_ID, "password": PASSWORD},
    )
    assert resp.status_code == 200
    assert resp.get_json()["has_credentials"] is True

    body = tc.get("/api/broker-auto-login/status").get_json()
    assert body["has_credentials"] is True
    assert body["user_id"] == USER_ID


def test_password_never_echoed(client):
    tc, creds_db, _ = client
    resp = tc.post(
        "/api/broker-auto-login/credentials",
        json={"user_id": USER_ID, "password": PASSWORD},
    )
    assert PASSWORD not in resp.get_data(as_text=True)
    assert PASSWORD not in tc.get("/api/broker-auto-login/status").get_data(as_text=True)

    row = (
        creds_db.db_session.query(creds_db.BrokerLoginCredential)
        .filter_by(broker="zerodha")
        .first()
    )
    assert row is not None and PASSWORD not in row.password_encrypted
    creds_db.db_session.remove()


def test_invalid_inputs_rejected(client):
    tc, _, _ = client
    # bad user-id
    assert (
        tc.post(
            "/api/broker-auto-login/credentials", json={"user_id": "!!", "password": PASSWORD}
        ).status_code
        == 400
    )
    # empty password
    assert (
        tc.post(
            "/api/broker-auto-login/credentials", json={"user_id": USER_ID, "password": ""}
        ).status_code
        == 400
    )


def test_login_requires_credentials_and_totp(client):
    tc, creds_db, totp_db = client

    # no creds yet
    assert tc.post("/api/broker-auto-login/login").status_code == 400

    creds_db.set_credentials("zerodha", USER_ID, PASSWORD)
    # creds but no TOTP
    resp = tc.post("/api/broker-auto-login/login")
    assert resp.status_code == 400
    assert "totp" in resp.get_json()["message"].lower()


def test_login_success_passthrough(client, monkeypatch):
    tc, creds_db, totp_db = client
    creds_db.set_credentials("zerodha", USER_ID, PASSWORD)
    totp_db.set_secret("zerodha", TOTP_SECRET)

    import services.broker_auto_login_service as bals

    monkeypatch.setattr(
        bals, "auto_login_primary", lambda: {"scope": "primary", "ok": True, "message": "done"}
    )
    resp = tc.post("/api/broker-auto-login/login")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_login_failure_returns_502(client, monkeypatch):
    tc, creds_db, totp_db = client
    creds_db.set_credentials("zerodha", USER_ID, PASSWORD)
    totp_db.set_secret("zerodha", TOTP_SECRET)

    import services.broker_auto_login_service as bals

    monkeypatch.setattr(
        bals,
        "auto_login_primary",
        lambda: {"scope": "primary", "ok": False, "message": "twofa failed"},
    )
    resp = tc.post("/api/broker-auto-login/login")
    assert resp.status_code == 502
    assert resp.get_json()["ok"] is False


def test_delete_clears(client):
    tc, _, _ = client
    tc.post("/api/broker-auto-login/credentials", json={"user_id": USER_ID, "password": PASSWORD})
    assert tc.delete("/api/broker-auto-login/credentials").get_json()["deleted"] is True
    assert tc.get("/api/broker-auto-login/status").get_json()["has_credentials"] is False


def test_anonymous_rejected():
    from blueprints.broker_auto_login import broker_auto_login_bp

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"  # pragma: allowlist secret
    app.register_blueprint(broker_auto_login_bp)
    tc = app.test_client()

    for method, url in (
        ("get", "/api/broker-auto-login/status"),
        ("post", "/api/broker-auto-login/credentials"),
        ("delete", "/api/broker-auto-login/credentials"),
        ("post", "/api/broker-auto-login/login"),
    ):
        resp = getattr(tc, method)(url, headers={"Accept": "application/json"})
        assert resp.status_code == 401, (method, url)
