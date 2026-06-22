"""Live-broker-session probe shared by boot-time backfill schedulers.

The difference this module captures: ``database.auth_db.get_first_available_api_key()``
returns truthy as long as an OpenAlgo API key row exists for a user with a
non-revoked auth row. It does NOT tell you whether the underlying *daily*
broker access token still works. Indian broker tokens expire daily at ~3 AM
IST, and the auth row is not auto-revoked on expiry — the encrypted broker
token stays in the row but is dead until the user re-logs in.

Boot-time history backfill schedulers gating only on
``get_first_available_api_key()`` therefore fire hundreds of fetches against a
dead token after every overnight restart, producing the morning 401 spam in
``errors.jsonl``. This helper performs the same lightweight probe that
``blueprints/auth.py:_try_resume_broker_session`` does at login — a single
``get_margin_data`` call (or ``test_auth_token`` when the broker exposes one)
— so callers can gate on "the broker would actually answer right now" instead
of "an API key row exists somewhere".

Keep the surface minimal — this is a quick-fix module, not a general broker
health framework. A future enhancement should wire boot backfill to the
existing ``BrokerSessionRefreshedEvent`` bus instead of polling.
"""

from __future__ import annotations

import importlib

from utils.logging import get_logger

logger = get_logger(__name__)


def _broker_response_indicates_failure(funds_data) -> str | None:
    """Return a reason when a broker funds payload signals auth failure, else None.

    Mirrors ``blueprints/auth.py:_broker_validation_failure_reason``. We do not
    import that helper directly to keep this module free of blueprint coupling
    — the boot path is a long way from the request path and a circular import
    here is silently catastrophic.
    """
    if not funds_data:
        return "empty funds response"
    if not isinstance(funds_data, dict):
        return None

    status = str(funds_data.get("status", "")).lower()
    if status in {"error", "failed", "failure"}:
        return str(
            funds_data.get("message")
            or funds_data.get("errorMessage")
            or funds_data.get("errors")
            or funds_data.get("error")
            or "broker returned error status"
        )

    for key in (
        "errorType",
        "errorCode",
        "errorMessage",
        "errors",
        "error",
        "errcode",
        "error_code",
    ):
        value = funds_data.get(key)
        if value:
            return str(value)
    return None


def is_live_broker_session() -> bool:
    """Return True only when a probe against the stored broker token succeeds.

    Probes the first non-revoked auth row by calling ``test_auth_token`` (if
    the broker exposes one) or ``get_margin_data``. Returns False when:

    - No non-revoked auth row exists.
    - The stored auth token is empty / undecryptable.
    - The broker module cannot be imported.
    - The probe raises or returns a payload classified as failure.

    Never raises. Designed for boot-time polling: cheap, idempotent, safe to
    call every few seconds.
    """
    try:
        from database.auth_db import Auth, decrypt_token

        auth_obj = Auth.query.filter_by(is_revoked=False).first()
        if not auth_obj or not auth_obj.broker:
            return False

        auth_token = decrypt_token(auth_obj.auth)
        if not auth_token:
            return False

        try:
            broker_module = importlib.import_module(f"broker.{auth_obj.broker}.api.funds")
        except ImportError:
            logger.exception(
                "broker_session_health: could not import broker.%s.api.funds",
                auth_obj.broker,
            )
            return False

        if hasattr(broker_module, "test_auth_token"):
            is_valid, error_message = broker_module.test_auth_token(auth_token)
            if not is_valid:
                logger.debug(
                    "broker_session_health: token probe failed for %s: %s",
                    auth_obj.broker,
                    error_message,
                )
                return False
            return True

        funds_data = broker_module.get_margin_data(auth_token)
        failure_reason = _broker_response_indicates_failure(funds_data)
        if failure_reason:
            logger.debug(
                "broker_session_health: funds probe failed for %s: %s",
                auth_obj.broker,
                failure_reason,
            )
            return False
        return True
    except Exception:
        logger.exception("broker_session_health: live-session probe raised")
        return False
