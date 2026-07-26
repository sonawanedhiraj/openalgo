"""Tests for the broker external-TOTP helper (issue #460).

Covers the ``/api/broker-totp`` blueprint + ``database/broker_totp_db``:

* save → status → current roundtrip (code matches pyotp for the same secret);
* input normalization (spaces / lowercase / dashes in the pasted base32 key);
* invalid / too-short secrets rejected with 400;
* ``/current`` without a stored secret → 404;
* delete clears the configured state;
* the secret is encrypted at rest and never echoed by any endpoint;
* all routes are session-gated.

Hermetic: the global ``test/conftest.py`` redirect points ``DATABASE_URL`` at a
throwaway temp DB before any ``database.*`` import binds its engine; the
fixture just calls ``init_db()`` on the redirected engine.
"""

from __future__ import annotations

import pyotp
import pytest
from flask import Flask

TEST_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # pragma: allowlist secret


@pytest.fixture
def client(monkeypatch):
    """Bare Flask app with broker_totp_bp mounted on the conftest-redirected DB."""
    import database.broker_totp_db as btdb
    from blueprints.broker_totp import broker_totp_bp

    btdb.init_db()

    monkeypatch.setattr("utils.session.is_session_valid", lambda: True)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(broker_totp_bp)

    yield app.test_client(), btdb

    # Clean the table so tests stay order-independent.
    try:
        btdb.db_session.query(btdb.BrokerTotpSecret).delete()
        btdb.db_session.commit()
    finally:
        btdb.db_session.remove()


def test_save_status_current_roundtrip(client):
    test_client, btdb = client

    resp = test_client.get("/api/broker-totp/status?broker=zerodha")
    assert resp.status_code == 200
    assert resp.get_json()["configured"] is False

    resp = test_client.post("/api/broker-totp", json={"broker": "zerodha", "secret": TEST_SECRET})
    assert resp.status_code == 200
    assert resp.get_json()["configured"] is True

    resp = test_client.get("/api/broker-totp/status?broker=zerodha")
    assert resp.get_json()["configured"] is True

    resp = test_client.get("/api/broker-totp/current?broker=zerodha")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["code"]) == 6
    assert 0 <= data["seconds_remaining"] <= data["interval"]
    # valid_window=1 tolerates a window rollover between the request and now.
    assert pyotp.TOTP(TEST_SECRET).verify(data["code"], valid_window=1)


def test_secret_input_is_normalized(client):
    test_client, _ = client

    messy = "jbsw y3dp-ehpk 3pxp JBSW Y3DP EHPK 3PXP"
    resp = test_client.post("/api/broker-totp", json={"secret": messy})
    assert resp.status_code == 200

    resp = test_client.get("/api/broker-totp/current")
    assert resp.status_code == 200
    assert pyotp.TOTP(TEST_SECRET).verify(resp.get_json()["code"], valid_window=1)


def test_invalid_secrets_rejected(client):
    test_client, _ = client

    for bad in ("", "SHORTKEY", "NOT-BASE32-!!!-DEFINITELY-INVALID1"):
        resp = test_client.post("/api/broker-totp", json={"secret": bad})
        assert resp.status_code == 400, bad

    resp = test_client.get("/api/broker-totp/status")
    assert resp.get_json()["configured"] is False


def test_current_without_secret_404(client):
    test_client, _ = client
    resp = test_client.get("/api/broker-totp/current?broker=zerodha")
    assert resp.status_code == 404


def test_delete_clears_secret(client):
    test_client, _ = client

    test_client.post("/api/broker-totp", json={"secret": TEST_SECRET})
    resp = test_client.delete("/api/broker-totp?broker=zerodha")
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] is True

    assert test_client.get("/api/broker-totp/status").get_json()["configured"] is False
    assert test_client.get("/api/broker-totp/current").status_code == 404


def test_secret_encrypted_at_rest_and_never_echoed(client):
    test_client, btdb = client

    resp = test_client.post("/api/broker-totp", json={"secret": TEST_SECRET})
    assert TEST_SECRET not in resp.get_data(as_text=True)

    row = btdb.db_session.query(btdb.BrokerTotpSecret).filter_by(broker="zerodha").first()
    assert row is not None
    assert TEST_SECRET not in row.totp_secret_encrypted

    for url in ("/api/broker-totp/status", "/api/broker-totp/current"):
        assert TEST_SECRET not in test_client.get(url).get_data(as_text=True)


def test_routes_are_session_gated(monkeypatch):
    import database.broker_totp_db as btdb  # noqa: F401  (ensure table/module import)
    from blueprints.broker_totp import broker_totp_bp

    monkeypatch.setattr("utils.session.is_session_valid", lambda: False)
    monkeypatch.setattr("utils.session.revoke_user_tokens", lambda: None)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"  # pragma: allowlist secret  (session.clear() needs one)
    app.register_blueprint(broker_totp_bp)
    test_client = app.test_client()

    for method, url in (
        ("get", "/api/broker-totp/status"),
        ("get", "/api/broker-totp/current"),
        ("post", "/api/broker-totp"),
        ("delete", "/api/broker-totp"),
    ):
        # JSON Accept header → the decorator answers 401 instead of a login redirect.
        resp = getattr(test_client, method)(url, headers={"Accept": "application/json"})
        assert resp.status_code == 401, (method, url, resp.status_code)
