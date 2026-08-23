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
import os
import time

from utils.logging import get_logger

logger = get_logger(__name__)

# How long boot fetch workers wait for the master-contract download before
# proceeding anyway (fail-open). The download normally takes ~20-30s; the
# status DB auto-fails a download stuck >5min, which terminates the wait early.
_DEFAULT_MC_GATE_TIMEOUT_SEC = 240
_MC_GATE_POLL_SEC = 2.0


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


def probe_token(broker: str, auth_token: str) -> bool:
    """Return True only when the broker answers a live-session probe for this token.

    The probing core shared by the primary (:func:`is_live_broker_session`) and
    the auto-login watcher's child probe (issue #658) — child auth rows store the
    same ``"<api_key>:<access_token>"`` format the primary does, so one probe
    serves both and the two can never drift. Calls ``test_auth_token`` when the
    broker exposes one, else ``get_margin_data``. Never raises; any failure
    (missing/import-broken broker module, raising call, failure payload) is False.
    """
    try:
        if not broker or not auth_token:
            return False
        try:
            broker_module = importlib.import_module(f"broker.{broker}.api.funds")
        except ImportError:
            logger.exception(
                "broker_session_health: could not import broker.%s.api.funds",
                broker,
            )
            return False

        if hasattr(broker_module, "test_auth_token"):
            is_valid, error_message = broker_module.test_auth_token(auth_token)
            if not is_valid:
                logger.debug(
                    "broker_session_health: token probe failed for %s: %s",
                    broker,
                    error_message,
                )
            return bool(is_valid)

        funds_data = broker_module.get_margin_data(auth_token)
        failure_reason = _broker_response_indicates_failure(funds_data)
        if failure_reason:
            logger.debug(
                "broker_session_health: funds probe failed for %s: %s",
                broker,
                failure_reason,
            )
            return False
        return True
    except Exception:
        logger.exception("broker_session_health: token probe raised")
        return False


def is_live_broker_session() -> bool:
    """Return True only when a probe against the stored broker token succeeds.

    Probes the first non-revoked auth row via :func:`probe_token`. Returns
    False when:

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

        return probe_token(auth_obj.broker, auth_token)
    except Exception:
        logger.exception("broker_session_health: live-session probe raised")
        return False


def _active_broker() -> str | None:
    """Return the broker of the first non-revoked auth row, or None."""
    try:
        from database.auth_db import Auth

        auth_obj = Auth.query.filter_by(is_revoked=False).first()
        return auth_obj.broker if auth_obj and auth_obj.broker else None
    except Exception:
        logger.exception("broker_session_health: active-broker lookup raised")
        return None


def wait_for_master_contract_ready(
    deadline_sec: int | None = None,
    poll_sec: float = _MC_GATE_POLL_SEC,
    stop_event=None,
) -> bool:
    """Wait (bounded) for the active broker's master-contract download to finish.

    The daily download fires on broker login — the same instant boot-time
    backfill workers see a live session — and while it swaps the SymToken
    table a concurrent token lookup can race the refresh (issue #587). Boot
    fetch workers call this AFTER the session wait and proceed regardless of
    the outcome (fail-open): only ``pending``/``downloading`` are worth
    waiting out; on ``error``/``unknown``/timeout the table still holds the
    previous contract, which the fetches can use.

    Returns True when the status reached ``success``, False otherwise (the
    caller should proceed either way). Never raises. Gated by
    ``MASTER_CONTRACT_BOOT_GATE_ENABLED`` (default true).
    """
    if os.getenv("MASTER_CONTRACT_BOOT_GATE_ENABLED", "true").lower() != "true":
        return True

    if deadline_sec is None:
        try:
            deadline_sec = int(
                os.getenv(
                    "MASTER_CONTRACT_BOOT_GATE_TIMEOUT_SEC", str(_DEFAULT_MC_GATE_TIMEOUT_SEC)
                )
            )
        except ValueError:
            deadline_sec = _DEFAULT_MC_GATE_TIMEOUT_SEC

    broker = _active_broker()
    if not broker:
        return True

    try:
        from database.master_contract_status_db import get_status
    except Exception:
        logger.exception("broker_session_health: master_contract_status_db unavailable")
        return False

    start = time.monotonic()
    deadline = start + deadline_sec
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        try:
            status = str(get_status(broker).get("status", "unknown")).lower()
        except Exception:
            logger.exception("broker_session_health: master-contract status read raised")
            return False
        if status == "success":
            elapsed = time.monotonic() - start
            if elapsed > poll_sec:
                logger.info(
                    "broker_session_health: master contract ready for %s after %.1fs",
                    broker,
                    elapsed,
                )
            return True
        if status not in ("pending", "downloading"):
            logger.warning(
                "broker_session_health: master contract status=%s for %s — "
                "proceeding with the previous contract",
                status,
                broker,
            )
            return False
        if stop_event is not None:
            stop_event.wait(poll_sec)
        else:
            time.sleep(poll_sec)
    logger.warning(
        "broker_session_health: master contract not ready for %s after %ds — proceeding anyway",
        broker,
        deadline_sec,
    )
    return False
