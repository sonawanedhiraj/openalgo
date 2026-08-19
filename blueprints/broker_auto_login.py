"""Broker headless auto-login API (``/api/broker-auto-login``) — issue #654.

Stores the primary account's Kite user-id + login password (write-only,
Fernet-encrypted in ``broker_login_credentials``) and exposes a manual
"Auto login" trigger that runs the headless flow server-side. The scheduled
boot hook + continuous watcher (``services/broker_auto_login_watcher``) call the
same service; this blueprint is the operator's on-demand button + setup form.

Auth is ``"user" in session`` ONLY — never ``check_session_validity`` — for the
exact reason ``broker_totp`` documents (issue #462): this is consumed by the
broker-connect page, the window between password-login and broker-login where
``check_session_validity`` would destructively log the operator out. The password
is never echoed by any response.
"""

import re
from functools import wraps

from flask import Blueprint, jsonify, request, session

from database.broker_login_credentials_db import (
    delete_credentials,
    get_user_id,
    has_credentials,
    set_credentials,
)
from database.broker_totp_db import has_secret
from utils.logging import get_logger

logger = get_logger(__name__)

broker_auto_login_bp = Blueprint(
    "broker_auto_login_bp", __name__, url_prefix="/api/broker-auto-login"
)

DEFAULT_BROKER = "zerodha"
# Kite user ids are short alphanumerics (e.g. AB1234). The pattern is an autofill
# guard (issue #492 shape), not a Zerodha spec — kept liberal.
_USER_ID_RE = re.compile(r"^[A-Za-z0-9]{4,20}$")


def require_login(f):
    """Require a password-authenticated session; never mutate it (issue #462)."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return jsonify({"status": "error", "message": "Login required."}), 401
        return f(*args, **kwargs)

    return decorated_function


def _broker_from_request() -> str:
    return (request.args.get("broker") or DEFAULT_BROKER).strip().lower()


@broker_auto_login_bp.route("/status", methods=["GET"])
@require_login
def status():
    """What is configured, and is the session currently live.

    Never returns the password. ``user_id`` is not a secret (it appears in the
    Kite login URL) so it is surfaced to confirm what is stored.
    """
    broker = _broker_from_request()
    try:
        from services.broker_auto_login_watcher import watcher_enabled

        enabled = watcher_enabled()
    except Exception:
        enabled = False
    try:
        from services.broker_session_health import is_live_broker_session

        live = is_live_broker_session()
    except Exception:
        logger.exception("auto-login status: live probe failed")
        live = False

    return jsonify(
        {
            "status": "success",
            "broker": broker,
            "enabled": enabled,
            "has_credentials": has_credentials(broker),
            "has_totp": has_secret(broker),
            "user_id": get_user_id(broker),
            "live": live,
        }
    )


@broker_auto_login_bp.route("/credentials", methods=["POST"])
@require_login
def save_credentials():
    """Save or replace the primary account's Kite user-id + password (write-only)."""
    data = request.get_json(silent=True) or {}
    broker = (data.get("broker") or DEFAULT_BROKER).strip().lower()
    user_id = str(data.get("user_id") or "").strip()
    password = str(data.get("password") or "")

    if not _USER_ID_RE.match(user_id):
        return jsonify({"status": "error", "message": "Invalid Kite user-id."}), 400
    if not password.strip():
        return jsonify({"status": "error", "message": "Password is required."}), 400

    if not set_credentials(broker, user_id, password):
        return jsonify({"status": "error", "message": "Failed to store credentials."}), 500

    return jsonify({"status": "success", "broker": broker, "has_credentials": True})


@broker_auto_login_bp.route("/credentials", methods=["DELETE"])
@require_login
def remove_credentials():
    """Delete the stored login credentials for the broker."""
    broker = _broker_from_request()
    deleted = delete_credentials(broker)
    return jsonify({"status": "success", "broker": broker, "deleted": deleted})


@broker_auto_login_bp.route("/login", methods=["POST"])
@require_login
def login_now():
    """Run the headless primary auto-login now and return the result.

    Requires credentials + a TOTP secret already stored; the response says which
    is missing. This is the manual counterpart to the boot/watcher triggers.
    """
    broker = _broker_from_request()
    if broker != DEFAULT_BROKER:
        return jsonify({"status": "error", "message": "Auto-login supports Zerodha only."}), 400
    if not has_credentials(broker):
        return jsonify({"status": "error", "message": "No login credentials stored."}), 400
    if not has_secret(broker):
        return jsonify({"status": "error", "message": "No TOTP secret stored."}), 400

    from services.broker_auto_login_service import auto_login_primary

    result = auto_login_primary()
    code = 200 if result.get("ok") else 502
    return jsonify(
        {
            "status": "success" if result.get("ok") else "error",
            "ok": result.get("ok", False),
            "message": result.get("message", ""),
        }
    ), code
