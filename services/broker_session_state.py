"""Canonical "is the broker session fresh?" predicate — the single freshness gate.

Why this exists
---------------
On 2026-06-26 a forensic walk of the boot log showed that OpenAlgo's ``/login``
page DOES detect token staleness (live ``get_margin_data`` probe in
``blueprints/auth.py:_try_resume_broker_session``), but it returns without
clearing the stale row. Downstream subsystems then read
``auth_db.get_auth_token`` blindly and attempt Zerodha calls with yesterday's
token — the 15-second 403 retry cycle visible in
``log/openalgo_2026-06-26.log`` 08:53:53→08:54:40 and the post-login
master-contract race 08:54:51→08:56:07.

This module is the **single source of truth** every caller asks before making a
broker call from a background path: WS-proxy adapter creation, the
``websocket_service`` boot poll, ``ws_recovery_service``, and anywhere else a
freshness check is needed.

Semantics
---------
``is_broker_session_fresh(user)`` is True iff the stored auth row exists,
is not revoked, and decryption returns a non-empty token. Revoking the row
(``database.auth_db.invalidate_auth``) is the canonical way to mark a session
stale, so this predicate composes naturally with the existing ``is_revoked``
flag and the ``Auth`` table's existing read paths.

Gating
------
``BROKER_FRESHNESS_GATE_ENABLED`` (default ``false`` for the first deploy; flip
to ``true`` after one trading day of green logs). When the flag is off the
predicate is **permissive** (returns True for any non-empty user) so the legacy
behaviour is preserved.
"""

from __future__ import annotations

import os

from utils.logging import get_logger

logger = get_logger(__name__)


def gate_enabled() -> bool:
    """Master flag for the freshness gate. Default off for safe rollout."""
    return os.environ.get("BROKER_FRESHNESS_GATE_ENABLED", "false").lower() == "true"


def is_broker_session_fresh(user: str | None) -> bool:
    """Return True iff ``user`` has a live, non-revoked broker session.

    When the gate flag is off, returns True for any non-empty user (legacy
    permissive behaviour). When on, returns True only if the auth row exists,
    is not revoked, and decryption returns a non-empty token. A None/empty
    user always returns False.
    """
    if not user:
        return False
    if not gate_enabled():
        return True

    try:
        from database.auth_db import get_auth_token

        token = get_auth_token(user)
        return bool(token)
    except Exception:
        logger.exception("broker_session_state: get_auth_token raised for user=%s", user)
        return False
