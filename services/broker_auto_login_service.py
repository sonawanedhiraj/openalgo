"""Headless auto-login orchestration for Zerodha (primary + child accounts).

Ties the pure web-login flow (``services.zerodha_web_login.fetch_request_token``)
to the stored credentials and the token exchange + side effects, with **no Flask
request context** — safe to call from a boot daemon, the watcher loop, or an
HTTP handler.

The primary path reproduces exactly the **broker-side** subset of
``utils.auth_utils.handle_auth_success`` (the session/cookie parts are skipped —
there is no request):

    upsert_auth(...)  ->  init_broker_status(...)  ->  notify_broker_session_refreshed(...)
    ->  smart master-contract download

``upsert_auth`` is what publishes the ZMQ ``CACHE_INVALIDATE`` (WS-proxy adapter
reconnect + re-subscribe) and ``notify_broker_session_refreshed`` is what drives
``ws_recovery_service`` to backfill the bars missed while the token was dead — so
a headless re-login heals the live feed with no restart, the same as a manual
one. The child path simply reuses ``broker_accounts_service.complete_login``,
which already applies the child-scoped subset.

Every account is isolated: one account's failure never blocks the others, and
every outcome is returned (and, at the ``auto_login_all`` level, Telegrammed).
"""

import os
from threading import Thread

from utils.logging import get_logger

logger = get_logger(__name__)

BROKER = "zerodha"


def _result(scope: str, ok: bool, message: str) -> dict:
    return {"scope": scope, "ok": ok, "message": message}


def auto_login_primary() -> dict:
    """Log the PRIMARY Zerodha account in headlessly. Returns a result dict.

    Reads env ``BROKER_API_KEY`` / ``BROKER_API_SECRET`` fresh (not the module
    constant cached in ``blueprints/brlogin.py``) and the stored user-id /
    password / TOTP secret. Fail-soft: returns ``ok=False`` with a reason on any
    missing input or network/exchange error, never raises.
    """
    api_key = os.getenv("BROKER_API_KEY")
    api_secret = os.getenv("BROKER_API_SECRET")
    if not api_key or not api_secret:
        return _result(
            "primary", False, "BROKER_API_KEY / BROKER_API_SECRET not configured in env."
        )

    from database import broker_login_credentials_db as creds_db
    from database import broker_totp_db

    creds = creds_db.get_credentials(BROKER)
    if not creds:
        return _result("primary", False, "No stored login credentials for the primary account.")
    user_id, password = creds

    totp_secret = broker_totp_db.get_secret(BROKER)
    if not totp_secret:
        return _result("primary", False, "No stored TOTP secret for the primary account.")

    # 1. Kite web login -> request_token
    from services.zerodha_web_login import fetch_request_token

    request_token, error = fetch_request_token(user_id, password, totp_secret, api_key)
    if not request_token:
        logger.error(f"Primary auto-login failed at web-login step: {error}")
        return _result("primary", False, error or "Web login failed.")

    # 2. Exchange request_token -> access_token (existing checksum path, reads env)
    from broker.zerodha.api.auth_api import authenticate_broker

    access_token, error = authenticate_broker(request_token)
    if not access_token:
        logger.error(f"Primary auto-login failed at token exchange: {error}")
        return _result("primary", False, error or "Token exchange failed.")

    # 3. Apply the session-free broker-side side effects.
    try:
        _apply_primary_session(api_key, access_token)
    except Exception as e:
        logger.exception(f"Primary auto-login: token obtained but side-effects failed: {e}")
        return _result("primary", False, f"Logged in but post-login wiring failed: {e}")

    logger.info("Primary Zerodha account auto-login succeeded.")
    return _result("primary", True, "Primary account logged in.")


def _apply_primary_session(api_key: str, access_token: str) -> None:
    """Mirror ``handle_auth_success`` minus the request/session parts (see module docstring)."""
    from database.auth_db import upsert_auth
    from database.user_db import find_user_by_username

    admin = find_user_by_username()
    if not admin:
        raise RuntimeError("No admin user found to attach the broker session to.")
    username = admin.username

    # Same "<api_key>:<access_token>" composite the Zerodha API modules expect.
    auth_token = f"{api_key}:{access_token}"
    inserted_id = upsert_auth(username, auth_token, BROKER)
    if not inserted_id:
        raise RuntimeError("upsert_auth did not return a row id.")

    from database.master_contract_status_db import init_broker_status
    from utils.auth_utils import (
        async_master_contract_download,
        load_existing_master_contract,
        notify_broker_session_refreshed,
        should_download_master_contract,
    )

    init_broker_status(BROKER)
    # Publishes SocketIO + event-bus BrokerSessionRefreshedEvent -> WS reconnect + gap replay.
    notify_broker_session_refreshed(username, BROKER)

    should_download, reason = should_download_master_contract(BROKER)
    target = async_master_contract_download if should_download else load_existing_master_contract
    logger.info(f"Auto-login master-contract: should_download={should_download} ({reason})")
    Thread(target=target, args=(BROKER,), daemon=True).start()


def auto_login_children() -> list[dict]:
    """Log every ENABLED child account (with a stored password) in headlessly.

    Reuses ``broker_accounts_service.complete_login`` for the token exchange +
    child-scoped side effects. Accounts without a stored password are skipped
    (they keep the manual flow). Each account isolated.
    """
    results: list[dict] = []
    try:
        from database import broker_accounts_db as accounts_db
    except Exception as e:
        logger.exception(f"auto_login_children: cannot load accounts db: {e}")
        return results

    try:
        accounts = accounts_db.list_accounts()
    except Exception as e:
        logger.exception(f"auto_login_children: cannot list accounts: {e}")
        return results

    for account in accounts:
        account_id = account.get("id")
        name = account.get("display_name") or f"account:{account_id}"
        if not account.get("is_enabled"):
            continue
        results.append(_auto_login_child(account_id, name))
    return results


def _auto_login_child(account_id: int, name: str) -> dict:
    """Log one child account in headlessly. Returns a result dict; never raises."""
    scope = f"child:{name}"
    try:
        from database import broker_accounts_db as accounts_db

        password = accounts_db.get_password(account_id)
        creds = accounts_db.get_credentials(account_id)  # (api_key, api_secret, broker)
        totp_secret = accounts_db.get_totp_secret(account_id)
        user_id = accounts_db.get_user_id(account_id)
    except Exception as e:
        logger.exception(f"{scope}: could not load credentials: {e}")
        return _result(scope, False, f"Credential load failed: {e}")

    if not creds or (creds[2] != BROKER):
        return _result(scope, False, "Not a Zerodha account or missing api credentials.")
    if not password:
        return _result(scope, False, "No stored password — skipped (manual login only).")
    if not totp_secret:
        return _result(scope, False, "No stored TOTP secret.")
    if not user_id:
        return _result(scope, False, "No stored Kite user-id (broker_client_id).")

    api_key, api_secret, _broker = creds
    from services.zerodha_web_login import fetch_request_token

    request_token, error = fetch_request_token(user_id, password, totp_secret, api_key)
    if not request_token:
        logger.error(f"{scope}: web-login failed: {error}")
        return _result(scope, False, error or "Web login failed.")

    from services.broker_accounts_service import complete_login

    ok, error = complete_login(account_id, request_token)
    if not ok:
        logger.error(f"{scope}: token exchange failed: {error}")
        return _result(scope, False, error or "Token exchange failed.")

    logger.info(f"{scope}: auto-login succeeded.")
    return _result(scope, True, "Child account logged in.")


def auto_login_all(*, notify: bool = True) -> list[dict]:
    """Auto-login the primary then every enabled child; return all results.

    Telegrams a one-line summary when ``notify`` is set and the notifier is
    configured. Never raises.
    """
    results = [auto_login_primary()]
    results.extend(auto_login_children())

    if notify:
        _notify_summary(results)
    return results


def _notify_summary(results: list[dict]) -> None:
    """Best-effort Telegram summary of an auto-login run. Never raises."""
    try:
        if os.getenv("NOTIFY_BROKER_AUTO_LOGIN", "true").lower() != "true":
            return
        ok = [r for r in results if r["ok"]]
        bad = [r for r in results if not r["ok"]]
        lines = [f"🔐 Auto-login: {len(ok)} ok, {len(bad)} failed"]
        for r in results:
            mark = "✅" if r["ok"] else "❌"
            lines.append(f"{mark} {r['scope']}: {r['message']}")
        from services.notification_service import notify

        notify("broker_auto_login", "\n".join(lines))
    except Exception:
        logger.exception("Failed to send auto-login summary notification")
