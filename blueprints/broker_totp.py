"""Broker external-TOTP helper API (``/api/broker-totp``).

Serves the current 6-digit TOTP code for the configured broker's external-2FA
secret so the operator can complete the broker's own login page (e.g. Zerodha
Kite's 2FA step) without reaching for the phone authenticator. The secret is
stored Fernet-encrypted in ``broker_totp_secrets`` (``database/broker_totp_db``)
and is write-only through this API: it can be saved, replaced, or deleted, but
never read back — only the derived code is ever returned.

All routes require a logged-in OpenAlgo session and are CSRF-protected; there is
deliberately NO API-key auth path.

**Auth level is ``"user" in session``, NOT ``check_session_validity`` — this is
load-bearing (issue #462).** OpenAlgo's session is two-stage: the password login
sets ``session["user"]``, and ``session["logged_in"]`` is only set later by
``handle_auth_success`` once BROKER auth completes. This blueprint is consumed by
the broker-connect page, i.e. precisely the window between those two stages, where
``is_session_valid()`` is False by design. ``check_session_validity``'s failure path
is destructive — it calls ``revoke_user_tokens()`` (revoking broker tokens in the DB
and clearing active sessions) and ``session.clear()`` — so decorating these routes
with it logged the operator out on arrival at the broker page. We gate on the same
condition ``/auth/broker-config`` uses to serve that very page, and the failure path
here only ever returns 401.
"""

import time
from functools import wraps

import pyotp
from flask import Blueprint, jsonify, request, session

from database.broker_totp_db import delete_secret, get_secret, has_secret, set_secret
from utils.logging import get_logger

logger = get_logger(__name__)

broker_totp_bp = Blueprint("broker_totp_bp", __name__, url_prefix="/api/broker-totp")

DEFAULT_BROKER = "zerodha"


def require_login(f):
    """Require a password-authenticated session; never mutate it.

    Deliberately non-destructive: an unauthenticated call gets a plain 401 and
    the session is left exactly as it was. See the module docstring for why
    ``check_session_validity`` must not be used here (issue #462).
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return jsonify({"status": "error", "message": "Login required."}), 401
        return f(*args, **kwargs)

    return decorated_function


def _normalize_secret(raw: str) -> str:
    """Strip whitespace/dashes and uppercase — the base32 form pyotp expects."""
    return raw.replace(" ", "").replace("-", "").strip().upper()


def _broker_from_request() -> str:
    return (request.args.get("broker") or DEFAULT_BROKER).strip().lower()


@broker_totp_bp.route("/status", methods=["GET"])
@require_login
def totp_status():
    """Whether a TOTP secret is configured for the broker."""
    broker = _broker_from_request()
    return jsonify({"status": "success", "broker": broker, "configured": has_secret(broker)})


@broker_totp_bp.route("/current", methods=["GET"])
@require_login
def current_code():
    """Current 6-digit TOTP code + seconds left in this 30s window."""
    broker = _broker_from_request()
    secret = get_secret(broker)
    if not secret:
        return jsonify(
            {"status": "error", "message": f"No TOTP secret configured for {broker}."}
        ), 404
    try:
        totp = pyotp.TOTP(secret)
        code = totp.now()
        seconds_remaining = int(totp.interval - time.time() % totp.interval)
    except Exception:
        logger.exception(f"TOTP code generation failed for broker '{broker}'")
        return jsonify(
            {"status": "error", "message": "Stored TOTP secret is invalid — re-save it."}
        ), 500
    return jsonify(
        {
            "status": "success",
            "broker": broker,
            "code": code,
            "seconds_remaining": seconds_remaining,
            "interval": int(totp.interval),
        }
    )


@broker_totp_bp.route("", methods=["POST"])
@require_login
def save_secret():
    """Save or replace the broker's TOTP secret (base32). Never echoed back."""
    data = request.get_json(silent=True) or {}
    broker = (data.get("broker") or DEFAULT_BROKER).strip().lower()
    secret = _normalize_secret(str(data.get("secret") or ""))

    if not secret:
        return jsonify({"status": "error", "message": "TOTP secret is required."}), 400
    if len(secret) < 16:
        return jsonify(
            {"status": "error", "message": "TOTP secret looks too short — expected base32 key."}
        ), 400

    # Validate by generating a code; rejects non-base32 input.
    try:
        pyotp.TOTP(secret).now()
    except Exception:
        return jsonify(
            {"status": "error", "message": "Invalid TOTP secret — not a valid base32 key."}
        ), 400

    if not set_secret(broker, secret):
        return jsonify({"status": "error", "message": "Failed to store TOTP secret."}), 500

    return jsonify({"status": "success", "broker": broker, "configured": True})


@broker_totp_bp.route("", methods=["DELETE"])
@require_login
def remove_secret():
    """Delete the broker's stored TOTP secret."""
    broker = _broker_from_request()
    deleted = delete_secret(broker)
    return jsonify({"status": "success", "broker": broker, "deleted": deleted})
