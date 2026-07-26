"""Child broker-account API (``/broker_accounts/api``) — multi-account Phase 1.

Issue #468. Serves the /accounts React page: account CRUD, per-account Kite
login URL, per-account TOTP code, strategy allow-list.

**Auth level is ``"user" in session``, NOT ``check_session_validity`` (issue
#462).** The morning use-case is precisely the window where the operator has
password-logged into OpenAlgo but the PRIMARY broker session may not be
established yet (``logged_in`` unset) — child logins must work there, and
``check_session_validity``'s failure path is destructive (revokes broker tokens
+ clears the session). The gate here only ever returns 401.

Secrets are write-only: api_key/api_secret/TOTP secret are accepted on POST/PUT
and never returned by any route.
"""

from functools import wraps

from flask import Blueprint, jsonify, request, session

from database import broker_accounts_db as accounts_db
from services import broker_accounts_service as accounts_service
from utils.logging import get_logger

logger = get_logger(__name__)

broker_accounts_bp = Blueprint("broker_accounts_bp", __name__, url_prefix="/broker_accounts/api")


def require_login(f):
    """Password-authenticated session required; never mutates the session (#462)."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return jsonify({"status": "error", "message": "Login required."}), 401
        return f(*args, **kwargs)

    return decorated_function


@broker_accounts_bp.route("", methods=["GET"])
@require_login
def list_accounts():
    """Full page payload: accounts + connection status + flag + known strategies."""
    return jsonify({"status": "success", **accounts_service.overview()})


@broker_accounts_bp.route("", methods=["POST"])
@require_login
def add_account():
    data = request.get_json(silent=True) or {}
    try:
        account = accounts_db.add_account(
            display_name=data.get("display_name", ""),
            api_key=data.get("api_key", ""),
            api_secret=data.get("api_secret", ""),
            capital_inr=data.get("capital_inr"),
            broker=data.get("broker", "zerodha"),
            broker_client_id=data.get("broker_client_id"),
        )
    except (ValueError, TypeError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        logger.exception(f"Failed to add child account: {e}")
        return jsonify({"status": "error", "message": "Failed to add account."}), 500
    return jsonify({"status": "success", "account": account}), 201


@broker_accounts_bp.route("/<int:account_id>", methods=["PUT"])
@require_login
def update_account(account_id: int):
    data = request.get_json(silent=True) or {}
    allowed = {
        k: data[k]
        for k in (
            "display_name",
            "broker_client_id",
            "capital_inr",
            "is_enabled",
            "api_key",
            "api_secret",
            "totp_secret",
        )
        if k in data
    }
    if allowed.get("totp_secret"):
        # Normalize + validate base32 before storing (mirrors broker_totp.py).
        import pyotp

        secret = str(allowed["totp_secret"]).replace(" ", "").replace("-", "").strip().upper()
        if len(secret) < 16:
            return jsonify(
                {"status": "error", "message": "TOTP secret looks too short — expected base32 key."}
            ), 400
        try:
            pyotp.TOTP(secret).now()
        except Exception:
            return jsonify(
                {"status": "error", "message": "Invalid TOTP secret — not a valid base32 key."}
            ), 400
        allowed["totp_secret"] = secret
    try:
        account = accounts_db.update_account(account_id, **allowed)
    except (ValueError, TypeError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        logger.exception(f"Failed to update child account {account_id}: {e}")
        return jsonify({"status": "error", "message": "Failed to update account."}), 500
    if not account:
        return jsonify({"status": "error", "message": "Account not found."}), 404
    return jsonify({"status": "success", "account": account})


@broker_accounts_bp.route("/<int:account_id>", methods=["DELETE"])
@require_login
def delete_account(account_id: int):
    deleted = accounts_service.delete_account(account_id)
    if not deleted:
        return jsonify({"status": "error", "message": "Account not found."}), 404
    return jsonify({"status": "success", "deleted": True})


@broker_accounts_bp.route("/<int:account_id>/login_url", methods=["GET"])
@require_login
def login_url(account_id: int):
    url = accounts_service.get_login_url(account_id)
    if not url:
        return jsonify({"status": "error", "message": "Account not found."}), 404
    return jsonify({"status": "success", "login_url": url})


@broker_accounts_bp.route("/<int:account_id>/disconnect", methods=["POST"])
@require_login
def disconnect(account_id: int):
    if not accounts_service.disconnect(account_id):
        return jsonify({"status": "error", "message": "Account not found."}), 404
    return jsonify({"status": "success"})


@broker_accounts_bp.route("/<int:account_id>/strategies", methods=["POST"])
@require_login
def set_strategies(account_id: int):
    if not accounts_db.get_account(account_id):
        return jsonify({"status": "error", "message": "Account not found."}), 404
    data = request.get_json(silent=True) or {}
    names = data.get("strategies")
    if not isinstance(names, list):
        return jsonify({"status": "error", "message": "'strategies' must be a list."}), 400
    unknown = [n for n in names if n not in accounts_service.KNOWN_STRATEGIES]
    if unknown:
        return jsonify({"status": "error", "message": f"Unknown strategies: {unknown}"}), 400
    saved = accounts_db.set_strategies(account_id, names)
    return jsonify({"status": "success", "strategies": saved})


@broker_accounts_bp.route("/<int:account_id>/totp", methods=["GET"])
@require_login
def totp_code(account_id: int):
    result = accounts_service.current_totp_code(account_id)
    if result is None:
        return jsonify({"status": "error", "message": "No TOTP secret enrolled."}), 404
    return jsonify({"status": "success", **result})


@broker_accounts_bp.route("/<int:account_id>/totp", methods=["DELETE"])
@require_login
def delete_totp(account_id: int):
    # nosec B106 — empty string is the delete sentinel, not a credential.
    account = accounts_db.update_account(account_id, totp_secret="")  # nosec B106
    if not account:
        return jsonify({"status": "error", "message": "Account not found."}), 404
    return jsonify({"status": "success", "deleted": True})
