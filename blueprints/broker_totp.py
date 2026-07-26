"""Broker external-TOTP helper API (``/api/broker-totp``).

Serves the current 6-digit TOTP code for the configured broker's external-2FA
secret so the operator can complete the broker's own login page (e.g. Zerodha
Kite's 2FA step) without reaching for the phone authenticator. The secret is
stored Fernet-encrypted in ``broker_totp_secrets`` (``database/broker_totp_db``)
and is write-only through this API: it can be saved, replaced, or deleted, but
never read back — only the derived code is ever returned.

All routes are session-gated (``check_session_validity``) and CSRF-protected;
there is deliberately NO API-key auth path.
"""

import time

import pyotp
from flask import Blueprint, jsonify, request

from database.broker_totp_db import delete_secret, get_secret, has_secret, set_secret
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

broker_totp_bp = Blueprint("broker_totp_bp", __name__, url_prefix="/api/broker-totp")

DEFAULT_BROKER = "zerodha"


def _normalize_secret(raw: str) -> str:
    """Strip whitespace/dashes and uppercase — the base32 form pyotp expects."""
    return raw.replace(" ", "").replace("-", "").strip().upper()


def _broker_from_request() -> str:
    return (request.args.get("broker") or DEFAULT_BROKER).strip().lower()


@broker_totp_bp.route("/status", methods=["GET"])
@check_session_validity
def totp_status():
    """Whether a TOTP secret is configured for the broker."""
    broker = _broker_from_request()
    return jsonify({"status": "success", "broker": broker, "configured": has_secret(broker)})


@broker_totp_bp.route("/current", methods=["GET"])
@check_session_validity
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
@check_session_validity
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
@check_session_validity
def remove_secret():
    """Delete the broker's stored TOTP secret."""
    broker = _broker_from_request()
    deleted = delete_secret(broker)
    return jsonify({"status": "success", "broker": broker, "deleted": deleted})
