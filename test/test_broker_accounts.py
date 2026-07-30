"""Tests for multi-account Phase 1 (issue #468).

Covers ``database/broker_accounts_db``, ``services/broker_accounts_service``
and the ``/broker_accounts/api`` blueprint:

* account CRUD — add starts disabled, credentials encrypted at rest and never
  echoed by any endpoint;
* strategy allow-list set/get + the Phase-2 ``accounts_for_strategy`` read
  (enabled + selected only);
* ``complete_login`` (exchange mocked) writes the ``acct:<id>`` auth row and
  stamps ``last_login_at``; failures leave no auth row;
* connection status requires BOTH an auth row and a same-IST-day login;
* the brlogin Zerodha callback routes to the child path ONLY when
  ``account_id`` is present (primary path regression-guarded);
* all API routes are session-gated with a plain 401 (issue #462 — no
  destructive session handling).

Hermetic: the global ``test/conftest.py`` redirect points ``DATABASE_URL`` at a
throwaway temp DB before any ``database.*`` import binds its engine.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from flask import Flask

TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # pragma: allowlist secret


@pytest.fixture
def accounts_db():
    import database.broker_accounts_db as adb

    adb.init_db()
    yield adb
    try:
        adb.db_session.query(adb.MultiAccountSettings).delete()
        adb.db_session.query(adb.AccountStrategy).delete()
        adb.db_session.query(adb.BrokerAccount).delete()
        adb.db_session.commit()
    finally:
        adb.db_session.remove()

    # SQLite reuses autoincrement ids after a full delete, so a leftover
    # ``acct:<id>`` auth row from one test would leak into the next.
    import database.auth_db as auth_db_module

    try:
        auth_db_module.db_session.query(auth_db_module.Auth).filter(
            auth_db_module.Auth.name.like(f"{adb.AUTH_NAME_PREFIX}%")
        ).delete(synchronize_session=False)
        auth_db_module.db_session.commit()
    finally:
        auth_db_module.db_session.remove()
    auth_db_module.auth_cache.clear()


@pytest.fixture
def client(accounts_db):
    """Bare Flask app with broker_accounts_bp mounted on the redirected DB."""
    from blueprints.broker_accounts import broker_accounts_bp

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"  # pragma: allowlist secret
    app.register_blueprint(broker_accounts_bp)

    test_client = app.test_client()
    with test_client.session_transaction() as sess:
        sess["user"] = "tester"
    return test_client


def _add(accounts_db, name="Dad — Zerodha", capital=250000.0):
    return accounts_db.add_account(
        display_name=name,
        api_key="child_api_key",  # pragma: allowlist secret
        api_secret="child_api_secret",  # pragma: allowlist secret
        capital_inr=capital,
        broker_client_id="AB1234",
    )


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def test_add_account_starts_disabled_and_encrypts_credentials(accounts_db):
    account = _add(accounts_db)
    assert account["is_enabled"] is False
    assert account["capital_inr"] == 250000.0
    # Public dict never carries credentials.
    assert "api_key" not in account and "api_secret" not in account

    # Encrypted at rest — the raw values never appear in the stored columns.
    row = accounts_db.db_session.query(accounts_db.BrokerAccount).first()
    assert "child_api_key" not in (row.api_key_encrypted or "")
    assert "child_api_secret" not in (row.api_secret_encrypted or "")
    accounts_db.db_session.remove()

    # But decrypt cleanly for the login exchange.
    api_key, api_secret, broker = accounts_db.get_credentials(account["id"])
    assert (api_key, api_secret, broker) == ("child_api_key", "child_api_secret", "zerodha")


def test_add_account_validates_input(accounts_db):
    with pytest.raises(ValueError):
        _add(accounts_db, name="")
    with pytest.raises(ValueError):
        _add(accounts_db, capital=0)
    with pytest.raises((ValueError, TypeError)):
        _add(accounts_db, capital=None)


# ---------------------------------------------------------------------------
# Credential visibility + autofill guard (issue #492)
# ---------------------------------------------------------------------------


def test_row_dict_exposes_masked_api_key_never_the_secret(accounts_db):
    account = _add(accounts_db)
    # api_key echoed masked (it already travels in the Kite login URL); the
    # secret is presence-only.
    assert account["api_key_masked"] == "chil" + "•" * 8 + "_key"  # pragma: allowlist secret
    assert account["has_api_secret"] is True
    assert "child_api_key" not in str(account)
    assert "child_api_secret" not in str(account)


def test_credential_shape_guard_rejects_autofilled_login(accounts_db):
    """A saved browser login (email + password with punctuation/spaces) is not a
    plausible Kite credential and must never overwrite a working one."""
    account = _add(accounts_db)
    before = accounts_db.get_credentials(account["id"])

    # NB: "" is not here — an empty field means "keep current" (asserted in
    # test_non_credential_update_preserves_stored_credentials), not "clear".
    for bad in ("someone@example.com", "my login password", "short", "a" * 65):
        with pytest.raises(ValueError):
            accounts_db.update_account(account["id"], api_key=bad)
        with pytest.raises(ValueError):
            accounts_db.update_account(account["id"], api_secret=bad)

    # Nothing was written by any rejected attempt.
    assert accounts_db.get_credentials(account["id"]) == before

    with pytest.raises(ValueError):
        accounts_db.add_account(
            display_name="Autofilled",
            api_key="someone@example.com",  # pragma: allowlist secret
            api_secret="hunter2 with spaces",  # pragma: allowlist secret
            capital_inr=100000,
        )


def test_non_credential_update_preserves_stored_credentials(accounts_db):
    """The regression that produced #492: a routine capital/name edit must leave
    the credential ciphertext byte-identical."""
    account = _add(accounts_db)
    row = accounts_db.db_session.query(accounts_db.BrokerAccount).first()
    key_before, secret_before = row.api_key_encrypted, row.api_secret_encrypted
    accounts_db.db_session.remove()

    accounts_db.update_account(account["id"], capital_inr=300000, display_name="Renamed")
    # An empty-string credential is treated as "keep", not "clear".
    accounts_db.update_account(account["id"], api_key="", api_secret="")

    row = accounts_db.db_session.query(accounts_db.BrokerAccount).first()
    assert row.api_key_encrypted == key_before
    assert row.api_secret_encrypted == secret_before
    accounts_db.db_session.remove()
    assert accounts_db.get_credentials(account["id"])[:2] == ("child_api_key", "child_api_secret")


def test_api_rejects_implausible_credential_with_400(client, accounts_db):
    account = _add(accounts_db)
    resp = client.put(
        f"/broker_accounts/api/{account['id']}",
        json={"capital_inr": 400000, "api_key": "someone@example.com"},  # pragma: allowlist secret
    )
    assert resp.status_code == 400
    assert "autofilled" in resp.get_json()["message"]
    # The rejected request wrote NOTHING — not even the valid capital field.
    assert accounts_db.get_account(account["id"])["capital_inr"] == 250000.0
    assert accounts_db.get_credentials(account["id"])[0] == "child_api_key"


def test_strategy_allowlist_and_fanout_read(accounts_db):
    acc1 = _add(accounts_db, name="A1")
    acc2 = _add(accounts_db, name="A2")

    accounts_db.set_strategies(acc1["id"], ["sector_follow_cap5_vol", "simplified_engine"])
    accounts_db.set_strategies(acc2["id"], ["sector_follow_cap5_vol"])
    assert accounts_db.get_strategies(acc1["id"]) == [
        "sector_follow_cap5_vol",
        "simplified_engine",
    ]

    # Fan-out read: only ENABLED accounts with the strategy selected.
    assert accounts_db.accounts_for_strategy("sector_follow_cap5_vol") == []
    accounts_db.update_account(acc1["id"], is_enabled=True)
    matched = accounts_db.accounts_for_strategy("sector_follow_cap5_vol")
    assert [a["id"] for a in matched] == [acc1["id"]]
    assert accounts_db.accounts_for_strategy("futures_follow_cap50") == []

    # Replace semantics.
    accounts_db.set_strategies(acc1["id"], [])
    assert accounts_db.get_strategies(acc1["id"]) == []


def test_delete_account_removes_strategy_rows(accounts_db):
    account = _add(accounts_db)
    accounts_db.set_strategies(account["id"], ["simplified_engine"])
    assert accounts_db.delete_account(account["id"]) is True
    assert accounts_db.get_account(account["id"]) is None
    assert accounts_db.get_strategies(account["id"]) == []


# ---------------------------------------------------------------------------
# Service layer — login + status
# ---------------------------------------------------------------------------


def test_complete_login_writes_child_auth_row(accounts_db, monkeypatch):
    from database.auth_db import get_auth_token
    from services import broker_accounts_service as svc

    account = _add(accounts_db)
    monkeypatch.setattr(svc, "_exchange_token", lambda k, s, rt: ("access123", None))

    ok, error = svc.complete_login(account["id"], "req_token")
    assert ok is True and error is None

    token = get_auth_token(accounts_db.auth_name(account["id"]), bypass_cache=True)
    assert token == "child_api_key:access123"
    refreshed = accounts_db.get_account(account["id"])
    assert refreshed["last_login_at"] is not None
    assert svc._is_connected(refreshed) is True


def test_complete_login_failure_paths(accounts_db, monkeypatch):
    from database.auth_db import get_auth_token
    from services import broker_accounts_service as svc

    account = _add(accounts_db)

    ok, error = svc.complete_login(account["id"], "")
    assert ok is False and "request_token" in error

    ok, error = svc.complete_login(99999, "req_token")
    assert ok is False and "Unknown" in error

    monkeypatch.setattr(svc, "_exchange_token", lambda k, s, rt: (None, "API error: boom"))
    ok, error = svc.complete_login(account["id"], "req_token")
    assert ok is False and "boom" in error
    assert get_auth_token(accounts_db.auth_name(account["id"]), bypass_cache=True) is None


def test_stale_login_date_counts_as_disconnected(accounts_db, monkeypatch):
    from services import broker_accounts_service as svc

    account = _add(accounts_db)
    monkeypatch.setattr(svc, "_exchange_token", lambda k, s, rt: ("access123", None))
    assert svc.complete_login(account["id"], "req_token")[0] is True

    # Backdate the login stamp to yesterday — Zerodha tokens die ~3 AM IST.
    accounts_db.update_account(account["id"], last_login_at=datetime.utcnow() - timedelta(days=2))
    refreshed = accounts_db.get_account(account["id"])
    assert svc._is_connected(refreshed) is False


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------


def test_api_requires_session():
    from blueprints.broker_accounts import broker_accounts_bp

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"  # pragma: allowlist secret
    app.register_blueprint(broker_accounts_bp)
    anonymous = app.test_client()

    resp = anonymous.get("/broker_accounts/api")
    assert resp.status_code == 401
    # Plain 401 only — never a redirect or session mutation (issue #462).
    assert resp.get_json()["status"] == "error"


def test_api_crud_roundtrip_never_echoes_secrets(client, monkeypatch):
    # Pin the seed OFF — the dev box's .env may carry MULTI_ACCOUNT_ENABLED=true
    # (issue #484: the DB row wins; env is only the first-read seed).
    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "false")
    resp = client.post(
        "/broker_accounts/api",
        json={
            "display_name": "Spouse — Zerodha",
            "broker_client_id": "CD5678",
            "api_key": "kx7f_child",  # pragma: allowlist secret
            "api_secret": "supersecret",  # pragma: allowlist secret
            "capital_inr": 500000,
        },
    )
    assert resp.status_code == 201
    body = resp.get_data(as_text=True)
    assert "kx7f_child" not in body and "supersecret" not in body
    account_id = resp.get_json()["account"]["id"]

    resp = client.get("/broker_accounts/api")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["multi_account_enabled"] is False  # env default — Phase 2 flag
    assert len(data["accounts"]) == 1
    assert data["accounts"][0]["connected"] is False
    body = resp.get_data(as_text=True)
    assert "kx7f_child" not in body and "supersecret" not in body

    resp = client.post(
        f"/broker_accounts/api/{account_id}/strategies",
        json={"strategies": ["sector_follow_cap5_vol"]},
    )
    assert resp.status_code == 200
    resp = client.post(
        f"/broker_accounts/api/{account_id}/strategies",
        json={"strategies": ["not_a_strategy"]},
    )
    assert resp.status_code == 400

    resp = client.put(f"/broker_accounts/api/{account_id}", json={"is_enabled": True})
    assert resp.status_code == 200
    assert resp.get_json()["account"]["is_enabled"] is True

    resp = client.delete(f"/broker_accounts/api/{account_id}")
    assert resp.status_code == 200


def test_api_totp_enroll_and_code(client, accounts_db):
    account = _add(accounts_db)
    account_id = account["id"]

    resp = client.get(f"/broker_accounts/api/{account_id}/totp")
    assert resp.status_code == 404

    resp = client.put(f"/broker_accounts/api/{account_id}", json={"totp_secret": "short"})
    assert resp.status_code == 400

    resp = client.put(f"/broker_accounts/api/{account_id}", json={"totp_secret": TEST_TOTP_SECRET})
    assert resp.status_code == 200
    assert resp.get_json()["account"]["has_totp_secret"] is True
    assert TEST_TOTP_SECRET not in resp.get_data(as_text=True)

    resp = client.get(f"/broker_accounts/api/{account_id}/totp")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["code"]) == 6
    assert TEST_TOTP_SECRET not in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# brlogin child-callback routing
# ---------------------------------------------------------------------------


@pytest.fixture
def brlogin_client(monkeypatch):
    """brlogin_bp on a bare app; auth functions stubbed for the primary path."""
    from flask import Blueprint

    from blueprints.brlogin import brlogin_bp
    from limiter import limiter

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"  # pragma: allowlist secret
    app.register_blueprint(brlogin_bp)
    limiter.init_app(app)

    # Stub target for the primary path's logged_in early-return redirect.
    dashboard_stub = Blueprint("dashboard_bp", __name__)
    dashboard_stub.add_url_rule("/dashboard", "dashboard", lambda: "ok")
    app.register_blueprint(dashboard_stub)

    # The primary path reads current_app.broker_auth_functions.
    app.broker_auth_functions = {"zerodha_auth": lambda code: (None, "stub: not exercised")}

    test_client = app.test_client()
    with test_client.session_transaction() as sess:
        sess["user"] = "tester"
        sess["logged_in"] = True  # normal morning state: primary already connected
    return test_client


def test_child_callback_routes_to_child_login(brlogin_client, monkeypatch):
    import services.broker_accounts_service as svc

    calls = {}

    def fake_complete_login(account_id, request_token):
        calls["args"] = (account_id, request_token)
        return True, None

    monkeypatch.setattr(svc, "complete_login", fake_complete_login)

    resp = brlogin_client.get("/zerodha/callback?account_id=7&request_token=rt123")
    assert resp.status_code == 302
    assert "/accounts?connected=7" in resp.headers["Location"]
    assert calls["args"] == (7, "rt123")


def test_child_callback_failure_redirects_with_error(brlogin_client, monkeypatch):
    import services.broker_accounts_service as svc

    monkeypatch.setattr(svc, "complete_login", lambda a, r: (False, "token rejected"))
    resp = brlogin_client.get("/zerodha/callback?account_id=7&request_token=bad")
    assert resp.status_code == 302
    assert "/accounts?error=" in resp.headers["Location"]

    resp = brlogin_client.get("/zerodha/callback?account_id=notanumber&request_token=x")
    assert resp.status_code == 302
    assert "/accounts?error=" in resp.headers["Location"]


def test_primary_callback_unchanged_without_account_id(brlogin_client):
    """No account_id → the pre-existing logged_in early-return fires (regression)."""
    resp = brlogin_client.get("/zerodha/callback?request_token=rt123")
    assert resp.status_code == 302
    assert "/accounts" not in resp.headers["Location"]
