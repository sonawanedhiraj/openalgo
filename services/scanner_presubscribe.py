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

This module replaces that pattern with two pieces working together:

1. A :class:`PreSubscriber` whose :meth:`~PreSubscriber.ensure` idempotently
   subscribes a symbol list to the broker WS, tracking what is already
   subscribed on the current connection so repeated calls are cheap no-ops.
2. Registration of :meth:`~PreSubscriber.ensure` as a *connect callback*
   (see :func:`services.websocket_service.register_connect_callback`) so it
   fires whenever the broker WS transitions to connected+authenticated — the
   first connect after the morning login and every mid-day reconnect.

A short boot-time retry thread (in ``app.py``) pokes the connection until it
comes up so the very first subscribe happens as early as possible; the connect
callback then keeps the subscriptions fresh across reconnects.

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
