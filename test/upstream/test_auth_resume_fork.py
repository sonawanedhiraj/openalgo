"""Fork-specific regression tests for broker-session resume validation.

Complements the adopted upstream ``test_auth_resume.py`` by pinning the
fork-only additions: our ``_broker_validation_failure_reason`` rejects the
snake/lower error-code key variants (``errcode`` / ``error_code``) that some
brokers emit and that upstream's key set does not cover. Mocked end-to-end at
the ``_try_resume_broker_session`` boundary per the project's test discipline.
"""

import importlib
from types import SimpleNamespace

import pytest
from flask import Flask, session

import blueprints.auth as auth_bp_module
import database.auth_db as auth_db
import utils.auth_utils as auth_utils

pytestmark = pytest.mark.unit


@pytest.fixture()
def app_context():
    app = Flask(__name__)
    app.secret_key = "test-secret"  # pragma: allowlist secret
    with app.test_request_context("/auth/login", method="POST"):
        yield


def _auth_record(broker="angel"):
    return SimpleNamespace(
        auth="encrypted-token",
        feed_token=None,
        broker=broker,
        user_id="client-id",
        is_revoked=False,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "error", "message": "token expired"},
        {"errcode": "AB1004", "errmsg": "session expired"},
        {"error_code": "401", "message": "unauthorized"},
        {"errorType": "AuthError"},
        {},
    ],
)
def test_failure_reason_flags_error_payloads(payload):
    """Every broker error shape (incl. snake-case errcode) is flagged invalid."""
    assert auth_bp_module._broker_validation_failure_reason(payload) is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"availablecash": "100.00"},
        {"status": "success", "data": {"availablecash": "5000"}},
    ],
)
def test_failure_reason_passes_valid_payloads(payload):
    """A genuine funds payload returns no failure reason."""
    assert auth_bp_module._broker_validation_failure_reason(payload) is None


def test_resume_rejects_snake_case_errcode(monkeypatch, app_context):
    """An errcode-style structured error must NOT resume the session."""
    session["user"] = "rajandran"

    fake_funds = SimpleNamespace(
        get_margin_data=lambda token: {"errcode": "AB1004", "errmsg": "session expired"}
    )

    monkeypatch.setattr(auth_db, "get_auth_token_dbquery", lambda username: _auth_record())
    monkeypatch.setattr(auth_db, "decrypt_token", lambda token: "plain-token")
    monkeypatch.setattr(importlib, "import_module", lambda module_path: fake_funds)
    monkeypatch.setattr(
        auth_utils,
        "handle_auth_success",
        lambda **kwargs: pytest.fail("errcode error payload must not resume session"),
    )

    assert auth_bp_module._try_resume_broker_session("rajandran") is None
    assert session.get("logged_in") is not True
