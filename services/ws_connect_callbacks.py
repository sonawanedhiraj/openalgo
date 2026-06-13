"""Connect-callback registry for the broker WebSocket (event-driven re-subscribe).

Deliberately kept free of heavy imports — no ``database`` modules, no broker
plugins — so it can be imported and unit-tested in isolation, and so importing
it never blocks the way ``database.auth_db`` does (that module runs DB/crypto
setup at import time and can stall in a fresh process while the live app holds
the SQLite lock). ``services.websocket_service`` re-exports these names for
backward compatibility, and ``services.websocket_client`` fires them.

Why this exists
---------------
The broker WebSocket only becomes usable once the proxy has authenticated the
API key AND connected the broker adapter (which needs a fresh broker session
token). On a typical Indian trading day the Zerodha token expires ~3 AM IST and
is only refreshed when the operator logs in for the morning. If OpenAlgo boots
before that login, every broker WS handshake returns 403 until the operator
logs in.

Components that want a live tick feed (the scanner pre-subscribe, the regime
classifier index subscriptions) used to poll for the WS once at boot with a
fixed 30 s deadline and then give up permanently — losing the race against the
morning login. A registered connect callback instead fires every time the
broker WS transitions to connected+authenticated (the FIRST connect after the
morning login, and every mid-day reconnect), so subscriptions are
(re)established whenever the feed is actually available.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from utils.logging import get_logger

logger = get_logger(__name__)

_connect_callbacks: dict[str, Callable[[str, str], None]] = {}
_callback_lock = threading.Lock()


def register_connect_callback(name: str, callback: Callable[[str, str], None]) -> None:
    """Register a callback fired when the broker WS becomes connected+authenticated.

    The callback is invoked as ``callback(user_id, broker)`` each time the
    internal WebSocket client receives an auth-success response from the proxy
    (i.e. the broker adapter is up). Registering twice with the same ``name``
    replaces the prior registration (idempotent), so re-running boot code does
    not stack duplicate callbacks.

    Args:
        name: Stable identifier for this callback (e.g. ``"scanner_pre_subscribe"``).
        callback: Callable accepting ``(user_id, broker)``.
    """
    with _callback_lock:
        _connect_callbacks[name] = callback
    logger.info("Registered WS connect callback %r", name)


def unregister_connect_callback(name: str) -> None:
    """Remove a previously registered connect callback (no-op if absent)."""
    with _callback_lock:
        _connect_callbacks.pop(name, None)


def _fire_connect_callbacks(user_id: str, broker: str) -> None:
    """Fire all registered connect callbacks (called by the WS client on auth).

    Each callback runs in its own daemon thread so a slow or blocking callback
    never stalls the WebSocket event loop, and a raising callback never blocks
    the others (fail-safe — exceptions are logged, not propagated).
    """
    with _callback_lock:
        callbacks = list(_connect_callbacks.items())
    if not callbacks:
        return
    logger.info(
        "Broker WS connected+authenticated (user=%s, broker=%s) — firing %d connect callback(s)",
        user_id,
        broker,
        len(callbacks),
    )
    for name, cb in callbacks:

        def _run(n=name, c=cb):
            try:
                c(user_id, broker)
            except Exception:
                logger.exception("Connect callback %r raised", n)

        threading.Thread(target=_run, name=f"connect-cb-{name}", daemon=True).start()
