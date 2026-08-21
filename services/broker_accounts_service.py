"""Child broker-account management — login, status, TOTP (multi-account Phase 1).

Issue #468 / docs/design/multi_account_plan.md. This service owns everything a
child account needs BEFORE it can mirror orders (Phase 2): per-account Kite
Connect login, session status, and the per-account TOTP helper code.

Design invariants (load-bearing — see plan §5.3):

- A child's daily access token is stored in the existing ``auth`` table under
  ``name = f"acct:{id}"`` via ``upsert_auth`` — token encryption, revocation
  and caching come for free, and the key can never collide with a username.
- ``complete_login`` performs NONE of the primary-login side effects in
  ``utils.auth_utils.handle_auth_success``: no Flask session mutation, no
  master-contract download, no WS notify. The ZMQ ``CACHE_INVALIDATE`` that
  ``upsert_auth`` always publishes is harmless for ``acct:N`` names (the WS
  proxy holds no adapter under that key).
- Feature flag ``MULTI_ACCOUNT_ENABLED`` (default false) does NOT gate this
  module — account setup/login is always allowed; the flag gates the Phase-2
  order fan-out. It is surfaced in ``overview()`` so the UI can banner it.
"""

import hashlib
import os
import time
from datetime import datetime

import pyotp

from database import broker_accounts_db as accounts_db
from database.auth_db import get_auth_token, upsert_auth
from utils.logging import get_logger

logger = get_logger(__name__)

BROKER_API_URL = os.getenv("BROKER_API_URL", "https://api.kite.trade")

# Canonical mode_keys a child may mirror. Deliberately a curated list (the
# strategies dashboard hardcodes per-engine builders the same way); extend when
# a new in-repo engine ships. open15 was initially excluded as a measurement
# strategy; the operator opted it in on 2026-07-27 (issue #482) — MIS equity
# scales fine at small child capital, and mirrors only fire if it ever runs
# LIVE anyway (R58 note: its honest backtest is negative; the mirror simply
# follows whatever the operator promotes).
KNOWN_STRATEGIES = (
    "simplified_engine",
    "sector_follow_cap5_vol",
    "futures_follow_cap50",
    "open15_vol_breakout",
)


def is_multi_account_enabled() -> bool:
    """Fan-out master switch — UI-configurable, DB-backed (issue #484).

    Reads the ``multi_account_settings`` row (seeded from the
    ``MULTI_ACCOUNT_ENABLED`` env var on first read). Consulted at fire time,
    so a UI flip applies immediately without a restart. Setup/login are never
    gated.
    """
    return accounts_db.get_multi_account_settings()["enabled"]


def primary_book_capital() -> float:
    """Mirror-sizing denominator — UI-configurable, DB-backed (issue #484)."""
    return accounts_db.get_multi_account_settings()["primary_book_capital"]


def _is_connected(account: dict) -> bool:
    """Fresh session = auth row exists AND last successful login was today (IST).

    The ``auth`` table has no timestamp, so freshness keys off the
    ``last_login_at`` we stamp in ``complete_login``. Zerodha tokens die ~3 AM
    IST daily, so "logged in on an earlier date" is always stale.
    """
    if not account.get("last_login_at"):
        return False
    token = get_auth_token(accounts_db.auth_name(account["id"]))
    if not token:
        return False
    try:
        last = datetime.fromisoformat(account["last_login_at"])
    except ValueError:
        return False
    # last_login_at is naive UTC (repo contract); IST = UTC+5:30.
    ist_now = datetime.utcnow().timestamp() + 5.5 * 3600
    ist_last = last.timestamp() + 5.5 * 3600
    return datetime.utcfromtimestamp(ist_now).date() == datetime.utcfromtimestamp(ist_last).date()


def _today_mirror_stats() -> dict[int, dict[str, int]]:
    """Per-account counts of today's mirror attempts: placed/skipped/failed.

    Fail-graceful: an unreadable journal yields empty stats, never an error —
    the /accounts page must render even if the Phase-2 table is unhappy.
    """
    try:
        from database.account_orders_db import list_orders

        today = datetime.utcnow().strftime("%Y-%m-%d")
        stats: dict[int, dict[str, int]] = {}
        for row in list_orders(date_utc=today):
            s = stats.setdefault(row["account_id"], {"placed": 0, "skipped": 0, "failed": 0})
            if row["status"] == "placed":
                s["placed"] += 1
            elif row["status"].startswith("skipped"):
                s["skipped"] += 1
            else:
                s["failed"] += 1
        return stats
    except Exception:
        logger.exception("today's mirror stats unavailable")
        return {}


def overview() -> dict:
    """Everything the /accounts page needs in one call."""
    mirror_stats = _today_mirror_stats()
    accounts = []
    for acc in accounts_db.list_accounts():
        acc["connected"] = _is_connected(acc)
        acc["strategies"] = accounts_db.get_strategies(acc["id"])
        acc["strategy_settings"] = accounts_db.get_strategy_settings(acc["id"])
        acc["redirect_url_hint"] = f"/zerodha/callback?account_id={acc['id']}"
        acc["today_mirrors"] = mirror_stats.get(acc["id"], {"placed": 0, "skipped": 0, "failed": 0})
        accounts.append(acc)
    settings = accounts_db.get_multi_account_settings()
    return {
        "multi_account_enabled": settings["enabled"],
        "primary_book_capital": settings["primary_book_capital"],
        "known_strategies": list(KNOWN_STRATEGIES),
        "accounts": accounts,
    }


def get_login_url(account_id: int) -> str | None:
    """The Kite login URL for this account's own app, or None if unknown."""
    creds = accounts_db.get_credentials(account_id)
    if not creds:
        return None
    api_key, _secret, broker = creds
    if broker != "zerodha":
        return None
    return f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"


def _exchange_token(
    api_key: str, api_secret: str, request_token: str
) -> tuple[str | None, str | None]:
    """Zerodha session/token checksum exchange → (access_token, error).

    Mirrors ``broker.zerodha.api.auth_api.authenticate_broker`` but with
    per-account credentials instead of the env pair. Kept separate so the
    primary's auth path is untouched.
    """
    checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    try:
        from utils.httpx_client import get_httpx_client

        client = get_httpx_client()
        response = client.post(
            f"{BROKER_API_URL}/session/token",
            headers={"X-Kite-Version": "3"},
            data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
        )
        response.raise_for_status()
        data = response.json()
        access_token = (data.get("data") or {}).get("access_token")
        if access_token:
            return access_token, None
        return None, "Authentication succeeded but no access token was returned."
    except Exception as e:
        message = str(e)
        try:
            if hasattr(e, "response") and e.response is not None:
                message = e.response.json().get("message", message)
        except Exception:
            pass
        return None, f"API error: {message}"


def complete_login(account_id: int, request_token: str) -> tuple[bool, str | None]:
    """Exchange the callback's request_token and store the child auth row.

    Returns (ok, error). Deliberately free of every primary-login side effect —
    see the module docstring.
    """
    if not request_token:
        return False, "Missing request_token in callback."
    creds = accounts_db.get_credentials(account_id)
    if not creds:
        return False, f"Unknown child account id {account_id}."
    api_key, api_secret, broker = creds

    access_token, error = _exchange_token(api_key, api_secret, request_token)
    if not access_token:
        logger.error(f"Child account {account_id} login exchange failed: {error}")
        return False, error

    # Same "<api_key>:<access_token>" format the Zerodha order API expects
    # (brlogin.py does this for the primary).
    upsert_auth(accounts_db.auth_name(account_id), f"{api_key}:{access_token}", broker)
    accounts_db.update_account(account_id, last_login_at=datetime.utcnow())
    logger.info(f"Child account {account_id} connected (broker={broker})")
    return True, None


def disconnect(account_id: int) -> bool:
    """Revoke the child's auth row (e.g. before deleting the account)."""
    account = accounts_db.get_account(account_id)
    if not account:
        return False
    upsert_auth(accounts_db.auth_name(account_id), "", account["broker"], revoke=True)
    accounts_db.update_account(account_id, last_login_at=None)
    logger.info(f"Child account {account_id} disconnected")
    return True


def delete_account(account_id: int) -> bool:
    """Disconnect + remove the account and its strategy rows."""
    disconnect(account_id)
    return accounts_db.delete_account(account_id)


def current_totp_code(account_id: int) -> dict | None:
    """Rolling 6-digit code for the account's enrolled TOTP secret, or None."""
    secret = accounts_db.get_totp_secret(account_id)
    if not secret:
        return None
    try:
        totp = pyotp.TOTP(secret)
        return {
            "code": totp.now(),
            "seconds_remaining": int(totp.interval - time.time() % totp.interval),
            "interval": int(totp.interval),
        }
    except Exception:
        logger.exception(f"TOTP code generation failed for child account {account_id}")
        return None
