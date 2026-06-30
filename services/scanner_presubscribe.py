"""Event-driven broker-WebSocket pre-subscribe for the scanner and the
regime classifier.

Background
----------
The scanner and the sector-rotation regime classifier both want a live tick
feed for a fixed set of symbols *before* any Chartink hit arrives, so bars
aggregate from market open. They used to do this with a one-shot daemon
thread at boot that polled ``get_websocket_connection`` for 30 s and then
gave up permanently if the broker WS wasn't ready.

That lost a race every morning: the Zerodha session token expires ~3 AM IST,
so until the operator logs in, every broker WS handshake returns 403. If
OpenAlgo booted before the login (the normal case), the 30 s deadline expired
and nothing ever re-armed — the scanner ended up with only the handful of
symbols the engine armed via Chartink hits, instead of the full universe.

This module replaces that pattern with three triggers working together (see
:func:`wire_pre_subscribe`):

1. A :class:`PreSubscriber` whose :meth:`~PreSubscriber.ensure` idempotently
   subscribes a symbol list to the broker WS, tracking what is already
   subscribed on the current connection so repeated calls are cheap no-ops.
2. Registration of :meth:`~PreSubscriber.ensure` as a *connect callback*
   (see :func:`services.websocket_service.register_connect_callback`) so it
   fires whenever the broker WS transitions to connected+authenticated — the
   first connect after the morning login and every mid-day reconnect.
3. An **event-bus subscription** to ``broker_session_refreshed`` so the chain
   is kicked off the instant the operator completes broker OAuth — no
   dependence on a daemon thread still being alive (issue #244).

A boot-retry daemon thread additionally polls every
``PRESUBSCRIBE_RETRY_SEC`` seconds for up to ``PRESUBSCRIBE_MAX_WAIT_SEC``
(default 7200) to cover the race where the operator's OAuth completes in
the same instant as boot — the api_key is re-fetched on **every** loop
iteration, so a session that lands after thread start is picked up rather
than the thread exiting early (the pre-#244 behaviour: the old
implementation fetched the api_key once before the loop and bailed when it
was missing, leaving nothing to retrigger on later login — operator had to
restart OpenAlgo manually).

Secondary fixes folded in here
------------------------------
* **Index exchange.** Zerodha resolves index instruments (NIFTY, BANKNIFTY,
  …) only under the ``NSE_INDEX`` exchange, not ``NSE``. The scanner symbol
  universe mixes 5 indices in with the equities; :func:`resolve_exchange_for_symbol`
  routes those to ``NSE_INDEX`` while everything else stays ``NSE``.
* **Response parsing.** The shared ``subscribe_to_symbols`` service wrapper
  collapses the proxy's per-symbol result into a single ``success``/``partial``
  status and maps ``partial`` to a hard failure — so the proxy's normal
  "Subscription processing complete" acknowledgement was being logged as a
  failure. :meth:`~PreSubscriber.ensure` instead reads the per-symbol
  ``subscriptions`` list straight from the client and counts each entry whose
  own status is ``success``, so the async-completion message is never misread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

# Symbols that Zerodha exposes only under the NSE_INDEX exchange. The scanner
# universe (SCANNER_SYMBOLS) interleaves these 5 indices with ~210 equities;
# subscribing them under plain "NSE" yields a "token not found" error and they
# never stream. Kept as a superset (SENSEX/BANKEX/INDIAVIX) so the helper is
# correct for any index a future config adds.
INDEX_SYMBOLS = {
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
    "INDIAVIX",
    "SENSEX",
    "BANKEX",
}


def resolve_exchange_for_symbol(symbol: str) -> str:
    """Return the exchange a symbol must be subscribed under.

    Indices (see :data:`INDEX_SYMBOLS`) resolve to ``NSE_INDEX``; everything
    else to ``NSE``.
    """
    return "NSE_INDEX" if symbol.upper() in INDEX_SYMBOLS else "NSE"


class PreSubscriber:
    """Idempotent broker-WebSocket subscriber for a named set of symbols.

    One instance per concern (scanner, regime). Tracks the symbols currently
    subscribed on the active connection so that:

    * repeated :meth:`ensure` calls on the *same* connection only subscribe
      symbols not yet covered (cheap no-op when everything is already up), and
    * a connect callback can pass ``reset=True`` to forget the previous
      connection's state and re-subscribe everything on a fresh connection
      (a reconnect drops the broker-side subscriptions, so they must be
      re-placed rather than assumed live).
    """

    def __init__(
        self,
        name: str,
        exchange_resolver: Callable[[str], str],
        mode: str = "Quote",
        connection_getter: Callable[[str], tuple] | None = None,
    ):
        """Args:
        name: Label for logging (e.g. ``"scanner"``).
        exchange_resolver: Maps a symbol to the exchange to subscribe it under.
        mode: Subscription mode passed to the WS client (default ``"Quote"``).
        connection_getter: Optional ``(user_id) -> (ok, client, err)`` used to
            obtain the WS client. Defaults to
            ``services.websocket_service.get_websocket_connection`` (imported
            lazily so this module stays import-light and testable without the
            DB-heavy service module). Injectable for unit tests.
        """
        self.name = name
        self._exchange_resolver = exchange_resolver
        self._mode = mode
        self._connection_getter = connection_getter
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()

    def _get_connection(self, user_id: str) -> tuple:
        if self._connection_getter is not None:
            return self._connection_getter(user_id)
        # Lazy import keeps this module free of the DB-heavy service import at
        # load time (and avoids blocking when the live app holds the DB lock).
        from services.websocket_service import get_websocket_connection

        return get_websocket_connection(user_id)

    @property
    def subscribed(self) -> set[str]:
        """Snapshot of symbols currently believed subscribed (test/introspection)."""
        with self._lock:
            return set(self._subscribed)

    def reset(self) -> None:
        """Forget all tracked subscriptions (e.g. on a fresh connection)."""
        with self._lock:
            self._subscribed.clear()

    def ensure(self, user_id: str, broker: str | None, symbols, *, reset: bool = False) -> int:
        """Subscribe any not-yet-subscribed ``symbols`` to the broker WS.

        Idempotent: symbols already tracked as subscribed on the current
        connection are skipped. Pass ``reset=True`` (the connect-callback path)
        to clear tracking first and re-subscribe everything, which is correct
        on a fresh connection where the broker-side subscriptions are gone.

        Args:
            user_id: OpenAlgo username (same value the proxy derives from the
                API key — see websocket_proxy auth).
            broker: Broker name (cosmetic here; routing is by ``user_id``).
            symbols: Iterable of OpenAlgo symbols to subscribe.
            reset: If True, clear tracking before subscribing (fresh connection).

        Returns:
            Number of symbols newly confirmed subscribed by this call.
        """
        symbols = list(symbols)
        with self._lock:
            if reset:
                self._subscribed.clear()
            pending = [s for s in symbols if s not in self._subscribed]
        if not pending:
            logger.debug(
                "%s pre-subscribe: nothing pending (%d already subscribed)",
                self.name,
                len(symbols),
            )
            return 0

        # Read the per-symbol response straight from the client so we count
        # actual broker-accepted subscriptions rather than relying on the
        # service wrapper's coarse success/partial flag (which misreads the
        # proxy's "Subscription processing complete" ack as a failure).
        ok_conn, client, err = self._get_connection(user_id)
        if not ok_conn or client is None:
            logger.warning("%s pre-subscribe: broker WS not available (%s)", self.name, err)
            return 0

        batch = [{"exchange": self._exchange_resolver(s), "symbol": s} for s in pending]
        try:
            result = client.subscribe(batch, mode=self._mode)
        except Exception:
            logger.exception("%s pre-subscribe: client.subscribe raised", self.name)
            return 0

        per_symbol = result.get("subscriptions", []) or []
        newly = {
            entry.get("symbol")
            for entry in per_symbol
            if entry.get("status") == "success" and entry.get("symbol")
        }
        failed = [
            entry
            for entry in per_symbol
            if entry.get("status") != "success" and entry.get("symbol")
        ]

        with self._lock:
            self._subscribed.update(newly)
            total = len(self._subscribed)

        logger.info(
            "%s pre-subscribed %d/%d symbols (status=%s, msg=%s, total tracked=%d)",
            self.name,
            len(newly),
            len(pending),
            result.get("status"),
            result.get("message"),
            total,
        )
        for entry in failed:
            logger.warning(
                "%s pre-subscribe failed for %s: %s",
                self.name,
                entry.get("symbol"),
                entry.get("message", "unknown"),
            )
        return len(newly)


# Module-level singletons wired up by app.py at boot.
#
# * scanner: equity universe with the 5 mixed-in indices routed to NSE_INDEX.
# * regime: sector indices — these are ALL indices, so force NSE_INDEX directly
#   (the named set in INDEX_SYMBOLS does not enumerate every sector index such
#   as NIFTYAUTO/NIFTYIT, but every REGIME_SECTOR_SYMBOLS entry is an index).
scanner_pre_subscriber = PreSubscriber("scanner", resolve_exchange_for_symbol)
regime_pre_subscriber = PreSubscriber("regime", lambda _s: "NSE_INDEX")


@dataclass
class PreSubscribeWiring:
    """Internals returned to tests when :func:`wire_pre_subscribe` is called with
    ``start_thread=False``. Production callers ignore this — the daemon thread
    runs on its own.

    Attributes:
        attempt: ``attempt(reason) -> bool`` — one-shot probe. Reads the api_key,
            calls the WS connection getter (which kicks the lazy client into
            its connect loop), and runs ``pre_subscriber.ensure`` if the WS is
            already up. Returns True iff the WS was up at this moment.
        on_session_refreshed: bus subscriber callback (takes the event arg).
        establish: the boot-retry loop body (no args). Tests can drive it
            synchronously by injecting a ``sleep_fn`` / ``time_fn``.
    """

    attempt: Callable[[str], bool]
    on_session_refreshed: Callable[[Any], None]
    establish: Callable[[], None]


def wire_pre_subscribe(
    callback_name: str,
    pre_subscriber: PreSubscriber,
    symbols: list,
    *,
    thread_name: str,
    # Injection points (production callers omit these and get the live defaults).
    api_key_provider: Callable[[], str | None] | None = None,
    user_id_verifier: Callable[[str], str | None] | None = None,
    broker_resolver: Callable[[str], str | None] | None = None,
    ws_connection_getter: Callable[[str], tuple] | None = None,
    register_callback: Callable[[str, Callable[[str, str], None]], None] | None = None,
    bus: Any = None,
    sleep_fn: Callable[[float], None] | None = None,
    time_fn: Callable[[], float] | None = None,
    retry_sec: int | None = None,
    max_wait_sec: int | None = None,
    start_thread: bool = True,
) -> PreSubscribeWiring | None:
    """Wire the three pre-subscribe triggers for ``symbols``.

    Three independent triggers all converge on the same idempotent
    :meth:`PreSubscriber.ensure` call:

    1. **Connect callback** via :func:`services.websocket_service.register_connect_callback`
       — fires every time the broker WS transitions to connected+authenticated,
       i.e. each first/re-connect of the day. Handles routine reconnects.
    2. **Event-bus subscription** to ``broker_session_refreshed`` (the in-process
       Event published by :func:`utils.auth_utils.notify_broker_session_refreshed`
       after successful OAuth) — fires the instant the operator's broker session
       lands, without waiting on the boot-retry poll cadence. Survives the
       boot-retry deadline.
    3. **Boot-retry daemon thread** that polls every ``PRESUBSCRIBE_RETRY_SEC``
       seconds for up to ``PRESUBSCRIBE_MAX_WAIT_SEC`` (default 7200 = 2h).
       The api_key is re-fetched on **every** iteration so an operator who
       completes Zerodha OAuth right after boot still gets caught here without
       depending on triggers (2) or (1) — defensive belt-and-braces for the
       race that produced issue #244 (operator OAuth landing milliseconds
       after the thread started, with the pre-#244 implementation bailing
       early on the first None api_key and never retrying).

    All triggers call the same internal ``_attempt`` helper. ``ensure()`` is
    idempotent (a keyed-set dedup over already-subscribed symbols), so the
    triggers can never double-subscribe or thrash.

    Args:
        callback_name: Stable string identifier (e.g. ``"scanner_pre_subscribe"``).
        pre_subscriber: The :class:`PreSubscriber` instance for this concern.
        symbols: Iterable of OpenAlgo symbols to subscribe.
        thread_name: ``threading.Thread`` name for the boot-retry daemon.
        api_key_provider, user_id_verifier, broker_resolver, ws_connection_getter,
        register_callback, bus, sleep_fn, time_fn, retry_sec, max_wait_sec:
            Test-injection points. Production callers omit these — the function
            falls back to live module-level imports (lazy, so this module stays
            import-light; CLAUDE.md notes ``database.auth_db`` is heavy).
        start_thread: If True (production default) the boot-retry runs in a daemon
            thread. If False, the thread is NOT started and a
            :class:`PreSubscribeWiring` is returned for test-driven execution.

    Returns:
        ``None`` when ``start_thread=True``; the internal callables wrapped in
        :class:`PreSubscribeWiring` when ``start_thread=False`` (for tests).
    """
    # Lazy default imports keep this module import-light. database.auth_db in
    # particular runs DB/crypto setup at import time and can stall in a fresh
    # process while the live app holds the SQLite lock.
    if api_key_provider is None:
        from database.auth_db import (
            get_first_available_api_key as api_key_provider,  # type: ignore[no-redef]
        )
    if user_id_verifier is None:
        from database.auth_db import verify_api_key as user_id_verifier  # type: ignore[no-redef]
    if broker_resolver is None:
        from database.auth_db import get_broker_name as broker_resolver  # type: ignore[no-redef]
    if ws_connection_getter is None:
        from services.websocket_service import (
            get_websocket_connection as ws_connection_getter,  # type: ignore[no-redef]
        )
    if register_callback is None:
        from services.websocket_service import (
            register_connect_callback as register_callback,  # type: ignore[no-redef]
        )
    if bus is None:
        from utils.event_bus import bus  # type: ignore[no-redef]
    if sleep_fn is None or time_fn is None:
        import time as _time

        if sleep_fn is None:
            sleep_fn = _time.sleep
        if time_fn is None:
            time_fn = _time.time
    if retry_sec is None or max_wait_sec is None:
        import os as _os

        if retry_sec is None:
            retry_sec = int(_os.environ.get("PRESUBSCRIBE_RETRY_SEC", "15"))
        if max_wait_sec is None:
            max_wait_sec = int(_os.environ.get("PRESUBSCRIBE_MAX_WAIT_SEC", "7200"))

    # Trigger 1: persistent connect callback for routine reconnects (each
    # connect+auth fires this; reset=True clears tracking since a broker-side
    # reconnect drops its subscriptions).
    register_callback(
        callback_name,
        lambda uid, brk: pre_subscriber.ensure(uid, brk, symbols, reset=True),
    )

    def _attempt(reason: str) -> bool:
        """Kick the WS-client connect chain once. Returns True iff the WS was
        up at the moment we checked (so an initial ensure() ran here).

        Calling ``ws_connection_getter`` is itself the trigger: on the first
        call the lazy :class:`WebSocketClient` starts its connect loop and
        the function returns False (not yet authenticated). When AUTH_SUCCESS
        arrives later, the connect-callback path (trigger 1 above) drives the
        ensure(). The local ensure() here covers the case where the WS is
        already up (e.g. mid-day re-login event)."""
        try:
            api_key = api_key_provider()
            user_id = user_id_verifier(api_key) if api_key else None
            if not user_id:
                return False
            broker = broker_resolver(api_key) or ""
            ok, _client, _err = ws_connection_getter(user_id)
            if ok:
                pre_subscriber.ensure(user_id, broker, symbols)
                logger.info(
                    "%s: broker WS up (%s) — initial subscribe done",
                    callback_name,
                    reason,
                )
                return True
            return False
        except Exception:
            logger.exception("%s: %s attempt raised", callback_name, reason)
            return False

    # Trigger 2: event-bus subscription. The published event lands moments
    # after upsert_auth() in handle_auth_success, so this fires the instant
    # the operator's OAuth completes — no 15s poll latency, and crucially it
    # works even after the boot-retry thread's deadline has expired.
    def _on_session_refreshed(_event: Any) -> None:
        _attempt("event-driven")

    try:
        bus.subscribe(
            # Topic literal matches ws_recovery_service._TOPIC ("broker_session_refreshed").
            # Kept as a string to avoid an import cycle with ws_recovery_service.
            "broker_session_refreshed",
            _on_session_refreshed,
            name=f"{callback_name}_session_refreshed",
        )
    except Exception:
        logger.exception(
            "%s: failed to subscribe to broker_session_refreshed event",
            callback_name,
        )

    # Trigger 3: boot-retry loop. Re-fetches api_key on EVERY iteration so a
    # session landing after thread start is caught. The two earlier triggers
    # alone would also catch it (event for normal OAuth, connect callback for
    # mid-day reconnect), so this is defensive — covers e.g. an OAuth that
    # somehow misses the bus emit, or a deployment where the bus is briefly
    # offline. Belt-and-braces, not single-point-of-failure.
    def _establish() -> None:
        deadline = time_fn() + max_wait_sec
        logged_no_key = False
        while time_fn() < deadline:
            if _attempt("boot"):
                return
            api_key = api_key_provider()
            user_id = user_id_verifier(api_key) if api_key else None
            if not user_id and not logged_no_key:
                logger.info(
                    "%s: no API key configured yet — retrying every %ds for "
                    "up to %ds (also armed via broker_session_refreshed event)",
                    callback_name,
                    retry_sec,
                    max_wait_sec,
                )
                logged_no_key = True
            sleep_fn(retry_sec)
        logger.warning(
            "%s: broker WS not up after %ds at boot; connect callback "
            "remains armed for the next connect, and event-bus subscription "
            "still active",
            callback_name,
            max_wait_sec,
        )

    if start_thread:
        threading.Thread(target=_establish, daemon=True, name=thread_name).start()
        return None
    return PreSubscribeWiring(
        attempt=_attempt,
        on_session_refreshed=_on_session_refreshed,
        establish=_establish,
    )
