"""Tests for headless auto-login orchestration (issue #654).

``services.broker_auto_login_service`` ties the web-login flow to stored
credentials + token exchange + side effects. These tests stub the pure/IO seams
(``fetch_request_token``, ``authenticate_broker``, ``complete_login``, the
primary side-effect application, and the accounts DB accessors) so the
orchestration logic — missing-input guards, per-account isolation, enabled-only
child selection — is exercised without network or DB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import services.broker_auto_login_service as svc

PW = "pw"  # pragma: allowlist secret  (dummy child password for the stubs below)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BROKER_API_KEY", "apikey")
    monkeypatch.setenv("BROKER_API_SECRET", "apisecret")
    monkeypatch.setenv("NOTIFY_BROKER_AUTO_LOGIN", "false")


def _patch_primary(
    monkeypatch,
    *,
    creds=("AB1234", "pw"),
    totp="SECRET",
    rt=("RT", None),
    access=("access_token", None),
    apply_ok=True,
):
    import broker.zerodha.api.auth_api as auth_api
    import database.broker_login_credentials_db as creds_db
    import database.broker_totp_db as totp_db
    import services.zerodha_web_login as web

    monkeypatch.setattr(creds_db, "get_credentials", lambda broker: creds)
    monkeypatch.setattr(totp_db, "get_secret", lambda broker: totp)
    monkeypatch.setattr(web, "fetch_request_token", lambda *a, **k: rt)
    monkeypatch.setattr(auth_api, "authenticate_broker", lambda token: access)

    def _apply(api_key, access_token):
        if not apply_ok:
            raise RuntimeError("wiring boom")

    monkeypatch.setattr(svc, "_apply_primary_session", _apply)


def test_primary_success(monkeypatch):
    _patch_primary(monkeypatch)
    result = svc.auto_login_primary()
    assert result["ok"] is True
    assert result["scope"] == "primary"


def test_primary_missing_credentials(monkeypatch):
    _patch_primary(monkeypatch, creds=None)
    result = svc.auto_login_primary()
    assert result["ok"] is False
    assert "credentials" in result["message"].lower()


def test_primary_missing_totp(monkeypatch):
    _patch_primary(monkeypatch, totp=None)
    result = svc.auto_login_primary()
    assert result["ok"] is False
    assert "totp" in result["message"].lower()


def test_primary_web_login_failure(monkeypatch):
    _patch_primary(monkeypatch, rt=(None, "Kite /api/twofa failed"))
    result = svc.auto_login_primary()
    assert result["ok"] is False
    assert "twofa" in result["message"].lower()


def test_primary_token_exchange_failure(monkeypatch):
    _patch_primary(monkeypatch, access=(None, "checksum error"))
    result = svc.auto_login_primary()
    assert result["ok"] is False


def test_primary_side_effects_failure_is_reported(monkeypatch):
    _patch_primary(monkeypatch, apply_ok=False)
    result = svc.auto_login_primary()
    assert result["ok"] is False
    assert "wiring" in result["message"].lower()


def test_primary_missing_env_keys(monkeypatch):
    monkeypatch.delenv("BROKER_API_KEY", raising=False)
    result = svc.auto_login_primary()
    assert result["ok"] is False
    assert "BROKER_API_KEY" in result["message"]


def _patch_children(monkeypatch, accounts, *, per_account, complete=(True, None), rt=("RT", None)):
    import database.broker_accounts_db as adb
    import services.broker_accounts_service as bas
    import services.zerodha_web_login as web

    monkeypatch.setattr(adb, "list_accounts", lambda: accounts)
    monkeypatch.setattr(adb, "get_password", lambda aid: per_account[aid]["password"])
    monkeypatch.setattr(adb, "get_credentials", lambda aid: per_account[aid]["creds"])
    monkeypatch.setattr(adb, "get_totp_secret", lambda aid: per_account[aid]["totp"])
    monkeypatch.setattr(adb, "get_user_id", lambda aid: per_account[aid]["user_id"])
    monkeypatch.setattr(web, "fetch_request_token", lambda *a, **k: rt)
    monkeypatch.setattr(bas, "complete_login", lambda aid, token: complete)


def test_children_enabled_only(monkeypatch):
    accounts = [
        {"id": 1, "display_name": "kid1", "is_enabled": True},
        {"id": 2, "display_name": "kid2", "is_enabled": False},
    ]
    per_account = {
        1: {"password": PW, "creds": ("k", "s", "zerodha"), "totp": "SEC", "user_id": "CD1"},
    }
    _patch_children(monkeypatch, accounts, per_account=per_account)
    results = svc.auto_login_children()
    assert len(results) == 1  # disabled account skipped
    assert results[0]["ok"] is True
    assert results[0]["scope"] == "child:kid1"


def test_child_without_password_skipped_loudly(monkeypatch):
    accounts = [{"id": 1, "display_name": "kid1", "is_enabled": True}]
    per_account = {
        1: {"password": None, "creds": ("k", "s", "zerodha"), "totp": "SEC", "user_id": "CD1"},
    }
    _patch_children(monkeypatch, accounts, per_account=per_account)
    results = svc.auto_login_children()
    assert results[0]["ok"] is False
    assert "password" in results[0]["message"].lower()


def test_all_isolation_primary_fails_child_succeeds(monkeypatch):
    _patch_primary(monkeypatch, creds=None)  # primary fails
    accounts = [{"id": 1, "display_name": "kid1", "is_enabled": True}]
    per_account = {
        1: {"password": PW, "creds": ("k", "s", "zerodha"), "totp": "SEC", "user_id": "CD1"},
    }
    _patch_children(monkeypatch, accounts, per_account=per_account)

    results = svc.auto_login_all(notify=False)
    assert results[0]["scope"] == "primary" and results[0]["ok"] is False
    assert any(r["scope"] == "child:kid1" and r["ok"] for r in results)


def test_notify_summary_reaches_notification_service(monkeypatch):
    """Regression (#688): ``_notify_summary`` imported a module-level ``notify``
    that ``services.notification_service`` never had, so the ImportError was
    swallowed by the best-effort try/except and every run summary was silently
    dropped. Exercise the REAL dispatch path — down to the
    ``get_notification_service()`` lookup — with only the service singleton
    replaced by a recorder."""
    monkeypatch.setenv("NOTIFY_BROKER_AUTO_LOGIN", "true")
    import services.notification_service as ns

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ns,
        "get_notification_service",
        lambda: SimpleNamespace(notify=lambda event, msg, **md: sent.append((event, msg))),
    )

    svc._notify_summary(
        [
            {"scope": "primary", "ok": True, "message": "logged in"},
            {"scope": "child:kid1", "ok": False, "message": "boom"},
        ]
    )

    assert sent, "summary never reached the notification service (the #688 silent drop)"
    event, msg = sent[0]
    assert event == "broker_auto_login"
    assert "1 ok, 1 failed" in msg
    assert "child:kid1" in msg
