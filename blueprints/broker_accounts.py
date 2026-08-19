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


@broker_accounts_bp.route("/settings", methods=["PUT"])
@require_login
def update_settings():
    """UI-configurable multi-account settings (issue #484): mirror-trading
    master switch + primary book capital. DB-backed; applies immediately
    (fan-out and jobs consult at fire time — no restart)."""
    data = request.get_json(silent=True) or {}
    kwargs = {}
    if "enabled" in data:
        kwargs["enabled"] = bool(data["enabled"])
    if "primary_book_capital" in data:
        try:
            kwargs["primary_book_capital"] = float(data["primary_book_capital"])
        except (TypeError, ValueError):
            return jsonify(
                {"status": "error", "message": "primary_book_capital must be a number"}
            ), 400
    if not kwargs:
        return jsonify({"status": "error", "message": "Nothing to update."}), 400
    try:
        settings = accounts_db.set_multi_account_settings(
            **kwargs, updated_by=f"ui:{session.get('user', '?')}"
        )
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        logger.exception(f"Failed to update multi-account settings: {e}")
        return jsonify({"status": "error", "message": "Failed to update settings."}), 500
    return jsonify({"status": "success", "settings": settings})


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
            "password",  # issue #654: Kite login password for headless auto-login
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


@broker_accounts_bp.route("/<int:account_id>/auto_login", methods=["POST"])
@require_login
def auto_login(account_id: int):
    """Run the headless auto-login for one child now (issue #654).

    Requires the child to have a stored password (+ TOTP secret + user-id). The
    result mirrors the primary endpoint's shape; the password is never echoed.
    """
    account = accounts_db.get_account(account_id)
    if not account:
        return jsonify({"status": "error", "message": "Account not found."}), 404

    from services.broker_auto_login_service import _auto_login_child

    name = account.get("display_name") or f"account:{account_id}"
    result = _auto_login_child(account_id, name)
    code = 200 if result.get("ok") else 502
    return jsonify(
        {
            "status": "success" if result.get("ok") else "error",
            "ok": result.get("ok", False),
            "message": result.get("message", ""),
        }
    ), code


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
    # Per-SELECTED-strategy capital-per-trade (issue #496) — the ONE sizing
    # knob: {"capital_per_trade": {"open15_vol_breakout": 15000}}. Entries for
    # non-selected strategies are ignored; null/absent leaves it unset (that
    # strategy's mirrors are then skipped loudly — default deny).
    raw_per_trade = data.get("capital_per_trade") or {}
    if not isinstance(raw_per_trade, dict):
        return jsonify(
            {"status": "error", "message": "'capital_per_trade' must be an object."}
        ), 400
    per_trade: dict[str, float] = {}
    for key, value in raw_per_trade.items():
        if key not in names or value in (None, ""):
            continue
        try:
            per_trade[key] = float(value)
        except (TypeError, ValueError):
            return jsonify(
                {"status": "error", "message": f"capital per trade for {key} must be a number"}
            ), 400
    try:
        saved = accounts_db.set_strategies(account_id, names, capital_per_trade=per_trade)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    return jsonify(
        {
            "status": "success",
            "strategies": saved,
            "strategy_settings": accounts_db.get_strategy_settings(account_id),
        }
    )


@broker_accounts_bp.route("/mirror_orders", methods=["GET"])
@require_login
def mirror_orders():
    """Today's (or ``?date=YYYY-MM-DD``) mirror attempts with account names.

    Read-only view over ``account_orders`` (Phase 2's journal) for the
    orderbook Mirror Orders card. Dates are UTC-day prefixes (repo journal
    contract).
    """
    from datetime import datetime

    from database.account_orders_db import list_orders

    date_utc = (request.args.get("date") or datetime.utcnow().strftime("%Y-%m-%d")).strip()
    try:
        datetime.fromisoformat(date_utc)
    except ValueError:
        return jsonify({"status": "error", "message": "date must be YYYY-MM-DD"}), 400

    names = {a["id"]: a["display_name"] for a in accounts_db.list_accounts()}
    rows = list_orders(date_utc=date_utc)
    for row in rows:
        row["account_name"] = names.get(row["account_id"], f"account {row['account_id']}")
    return jsonify({"status": "success", "date": date_utc, "orders": rows})


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
