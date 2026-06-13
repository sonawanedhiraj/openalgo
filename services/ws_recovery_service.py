"""Historical-API replay on WS reconnect — eliminates the tick-starvation gap.

When the Zerodha session is refreshed (daily ~3 AM IST token rotation, or any
mid-day re-login), the WS proxy subprocess reconnects its broker adapter off the
ZMQ ``CACHE_INVALIDATE`` event (see ``WebSocketProxy._reconnect_broker_adapter``)
and the Flask side emits a ``broker_session_refreshed`` SocketIO event for the UI
(``utils.auth_utils.notify_broker_session_refreshed``). The feed resumes, but the
in-memory bar aggregators that drive the in-house scanner have a **gap**: every
1m/5m bar that closed while the socket was down was never seen, so the scanner
silently warms up from scratch — the class of failure behind the 2026-06-11/12
"in-house scanner collapsed to 7 hits/day" tick-starvation incidents.

This service closes that gap. It subscribes (in-process, via the event bus) to
the same ``broker_session_refreshed`` signal, then for every tracked symbol it
fetches the last ``WS_RECOVERY_LOOKBACK_MIN`` minutes of 1m bars from the broker
historical API and folds them into the live scanner aggregator via
``MultiIntervalAggregator.replay_bars`` — replaying the missed bar closes so the
scanner's rolling state is immediately current.

Honest limitations (handled gracefully, never block or crash):

* **Zerodha current-day historical delay (~5-15 min).** If the WS reconnects
  inside that window the most-recent 1m bars are not available yet. The service
  fetches what IS there, reports how many symbols came back empty, and notes the
  remaining minutes will catch up on the next refresh.
* **Rate limit (~3 req/sec).** ``history_service.get_history`` already enforces a
  3/sec limiter, so ~250 symbols take ~85s. We rely on it (no extra sleep).
* **Per-symbol failure** (network/auth/broker) is logged via ``logger.exception``
  and skipped — never all-or-nothing. If >20% of symbols fail the Telegram alert
  is escalated to a warning.

No feature flag: the service always registers; its tests carry the guarantee.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from utils.event_bus import Event
from utils.event_bus import bus as _default_bus
from utils.logging import get_logger

logger = get_logger(__name__)

# Default lookback window (minutes of 1m bars to fetch per symbol on reconnect).
# Overridable via the WS_RECOVERY_LOOKBACK_MIN env var. See docs/PARAMETER_LOG.md.
_DEFAULT_LOOKBACK_MIN = 20

_TOPIC = "broker_session_refreshed"


@dataclass
class BrokerSessionRefreshedEvent(Event):
    """Published on the in-process event bus after a broker re-login.

    The SocketIO event of the same name is browser-only; this is the in-process
    counterpart the recovery service subscribes to.
    """

    username: str = ""
    broker: str = ""
    topic: str = _TOPIC


def _normalize_ts(value: Any) -> dt.datetime | None:
    """Coerce a broker history ``timestamp`` field to a naive ``datetime``.

    Accepts epoch seconds (int/float), ISO strings, and anything with a
    ``to_pydatetime`` (pandas Timestamp). Returns ``None`` if it cannot parse —
    the caller drops the bar rather than raising.
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime()
        except Exception:
            return None
    if isinstance(value, int | float):
        try:
            return dt.datetime.fromtimestamp(float(value))
        except Exception:
            return None
    if isinstance(value, str):
        for parser in (
            lambda s: dt.datetime.fromisoformat(s),
            lambda s: dt.datetime.fromtimestamp(float(s)),
        ):
            try:
                return parser(value)
            except Exception:
                continue
    return None


def _default_universe() -> list[tuple[str, str]]:
    """Enumerate every symbol the recovery path should catch up.

    Union of three canonical sources, de-duplicated by symbol:

    * scanner universe (``SCANNER_SYMBOLS`` env), with the 5 mixed-in indices
      routed to ``NSE_INDEX`` via the scanner's own resolver;
    * sector_follow universe (the locked-static-30 stocks) on ``NSE``;
    * sector_follow mapped sector indices on ``NSE_INDEX``.

    Returns a list of ``(symbol, exchange)`` tuples. Any source that fails to
    import/resolve is logged and skipped — a partial universe still helps.
    """
    universe: dict[str, str] = {}

    # Scanner universe (env-driven, indices routed to NSE_INDEX).
    try:
        from services.scanner_presubscribe import resolve_exchange_for_symbol

        raw = os.getenv("SCANNER_SYMBOLS", "")
        for s in (x.strip() for x in raw.split(",") if x.strip()):
            universe.setdefault(s.upper(), resolve_exchange_for_symbol(s))
    except Exception:
        logger.exception("WS recovery: failed to enumerate scanner universe")

    # sector_follow universe stocks (NSE).
    try:
        from services.sector_follow_stock_backfill import sector_follow_stock_symbols

        for s in sector_follow_stock_symbols():
            universe.setdefault(s.upper(), "NSE")
    except Exception:
        logger.exception("WS recovery: failed to enumerate sector_follow stock universe")

    # sector_follow mapped sector indices (NSE_INDEX).
    try:
        from services.sector_follow_index_backfill import sector_index_symbols

        for s in sector_index_symbols():
            universe.setdefault(s.upper(), "NSE_INDEX")
    except Exception:
        logger.exception("WS recovery: failed to enumerate sector_follow index universe")

    return list(universe.items())


def _default_api_key() -> str | None:
    from database.auth_db import get_first_available_api_key

    return get_first_available_api_key()


def _default_history_fetcher(
    symbol: str, exchange: str, api_key: str, lookback_min: int
) -> list[dict]:
    """Fetch recent 1m bars for one symbol from the broker historical API.

    Returns a list of ``{open, high, low, close, volume, ts}`` dicts (ts as a
    ``datetime``), trimmed to the last ``lookback_min`` bars. Raises on a hard
    failure so the caller can record + skip the symbol. ``get_history`` enforces
    the 3 req/sec broker rate limit internally.
    """
    from services.history_service import get_history

    today = dt.datetime.now().strftime("%Y-%m-%d")
    success, payload, _code = get_history(
        symbol=symbol,
        exchange=exchange,
        interval="1m",
        start_date=today,
        end_date=today,
        api_key=api_key,
    )
    if not success:
        raise RuntimeError((payload or {}).get("message", "history fetch failed"))

    rows = (payload or {}).get("data") or []
    bars: list[dict] = []
    for row in rows:
        ts = _normalize_ts(row.get("timestamp"))
        if ts is None:
            continue
        bars.append(
            {
                "ts": ts,
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume") or 0,
            }
        )
    bars.sort(key=lambda b: b["ts"])
    return bars[-lookback_min:] if lookback_min else bars


def _default_notifier(message: str) -> None:
    """Send an operator alert via the direct Telegram Bot API.

    Uses ``bot_config`` directly (token + allowlisted chat ids) rather than
    ``notification_service.notify`` — the latter no-ops for event types it does
    not know and needs a live in-process bot loop. Best-effort: any failure is
    logged and swallowed.
    """
    try:
        import requests

        from database.telegram_db import get_bot_config

        cfg = get_bot_config()
        if not cfg or not cfg.get("bot_token") or not cfg.get("telegram_chat_ids"):
            logger.info("WS recovery: no Telegram config; skipping alert")
            return
        chat_ids = [c.strip() for c in cfg["telegram_chat_ids"].split(",") if c.strip()]
        for cid in chat_ids:
            requests.post(
                f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                json={"chat_id": int(cid), "text": message},
                timeout=10,
            )
    except Exception:
        logger.exception("WS recovery: failed to send Telegram alert")


class WSRecoveryService:
    """Fetches missed 1m bars on WS reconnect and seeds the scanner aggregator.

    All collaborators are injectable for testing; the defaults wire to the real
    broker history API, the live scanner aggregator, and the direct Telegram
    Bot API. Construction is side-effect free — call :meth:`register` to start
    listening on the event bus.
    """

    def __init__(
        self,
        aggregator_provider: Callable[[], Any] | None = None,
        universe_provider: Callable[[], list[tuple[str, str]]] | None = None,
        history_fetcher: Callable[[str, str, str, int], list[dict]] | None = None,
        api_key_provider: Callable[[], str | None] | None = None,
        notifier: Callable[[str], None] | None = None,
        lookback_min: int | None = None,
        bus: Any = None,
    ):
        self._aggregator_provider = aggregator_provider
        self._universe_provider = universe_provider or _default_universe
        self._history_fetcher = history_fetcher or _default_history_fetcher
        self._api_key_provider = api_key_provider or _default_api_key
        self._notifier = notifier or _default_notifier
        self._bus = bus if bus is not None else _default_bus
        if lookback_min is not None:
            self.lookback_min = int(lookback_min)
        else:
            self.lookback_min = int(os.getenv("WS_RECOVERY_LOOKBACK_MIN", _DEFAULT_LOOKBACK_MIN))
        self._registered = False

    # -- lifecycle ----------------------------------------------------------

    def register(self) -> None:
        """Subscribe to ``broker_session_refreshed`` on the event bus (idempotent)."""
        if self._registered:
            return
        self._bus.subscribe(_TOPIC, self.on_broker_session_refreshed, name="ws_recovery")
        self._registered = True
        logger.info("WS recovery service registered (lookback=%d min)", self.lookback_min)

    # -- event entry --------------------------------------------------------

    def on_broker_session_refreshed(self, event: Any) -> dict:
        """Bus callback: run recovery for the refreshed session. Never raises."""
        username = getattr(event, "username", "")
        broker = getattr(event, "broker", "")
        try:
            return self.recover(username=username, broker=broker)
        except Exception:
            # Recovery is best-effort: the WS reconnect already succeeded; a
            # failure here must not propagate back into the login/bus path.
            logger.exception("WS recovery run failed for %s (%s)", username, broker)
            return {"status": "error"}

    # -- core ---------------------------------------------------------------

    def _resolve_aggregator(self) -> Any | None:
        if self._aggregator_provider is not None:
            return self._aggregator_provider()
        # Default: the live scanner aggregator, if the scanner is running.
        try:
            from flask import current_app

            scanner = getattr(current_app, "scanner_service", None)
            return scanner.aggregator if scanner is not None else None
        except Exception:
            return None

    def recover(self, username: str = "", broker: str = "") -> dict:
        """Fetch + replay missed bars for the whole tracked universe.

        Returns a summary dict for observability/tests. Always sends one
        Telegram alert summarizing the run.
        """
        start = time.monotonic()
        aggregator = self._resolve_aggregator()
        if aggregator is None:
            logger.info(
                "WS recovery: no scanner aggregator available (scanner disabled?) — nothing to seed"
            )
            return {"status": "skipped", "reason": "no_aggregator", "symbols": 0}

        universe = self._universe_provider()
        api_key = self._api_key_provider()
        if not api_key:
            logger.warning("WS recovery: no API key available — cannot fetch history")
            self._notifier("WS recovery: aborted — no broker API key available to fetch history")
            return {"status": "error", "reason": "no_api_key", "symbols": len(universe)}

        total = len(universe)
        resynced = 0
        empty = 0
        failed = 0
        bars_replayed = 0
        newest_ts: dt.datetime | None = None

        for symbol, exchange in universe:
            try:
                bars = self._history_fetcher(symbol, exchange, api_key, self.lookback_min)
            except Exception as e:
                failed += 1
                logger.exception("WS recovery historical fetch failed for %s: %s", symbol, e)
                continue

            if not bars:
                empty += 1
                continue

            try:
                n = aggregator.replay_bars(symbol, bars)
            except Exception as e:
                failed += 1
                logger.exception("WS recovery replay failed for %s: %s", symbol, e)
                continue

            if n > 0:
                resynced += 1
                bars_replayed += n
            latest = bars[-1]["ts"]
            if newest_ts is None or latest > newest_ts:
                newest_ts = latest

        elapsed = time.monotonic() - start
        gap_min: int | None = None
        if newest_ts is not None:
            gap_min = max(int((dt.datetime.now() - newest_ts).total_seconds() // 60), 0)

        summary = {
            "status": "ok",
            "symbols": total,
            "resynced": resynced,
            "empty": empty,
            "failed": failed,
            "bars_replayed": bars_replayed,
            "elapsed_sec": round(elapsed, 1),
            "gap_minutes": gap_min,
        }
        self._notifier(self._format_alert(summary))
        logger.info("WS recovery complete: %s", summary)
        return summary

    @staticmethod
    def _format_alert(s: dict) -> str:
        total = s["symbols"]
        gap = s["gap_minutes"]
        gap_str = f"~{gap}min" if gap is not None else "unknown"
        msg = (
            f"WS recovery: {s['resynced']}/{total} symbols re-synced in "
            f"{s['elapsed_sec']}s, gap was {gap_str}, {s['bars_replayed']} bars replayed"
        )
        if s["empty"]:
            msg += (
                f"; {s['empty']} symbols had no recent bars yet "
                f"(current-day API delay — will catch up on next refresh)"
            )
        if total and s["failed"] / total > 0.20:
            msg = f"⚠️ {msg}; {s['failed']} symbols FAILED (>20%) — check broker/auth"
        return msg


# --------------------------------------------------------------------- singleton

_service: WSRecoveryService | None = None


def init_ws_recovery_service(app: Any = None) -> WSRecoveryService:
    """Construct + register the singleton recovery service (boot wiring).

    Mirrors the Task 2 reconnect-hook lifecycle: built once at boot, listens on
    the event bus for the life of the process. ``app`` is captured so the default
    aggregator provider can read ``app.scanner_service`` without an app context.
    """
    global _service
    if _service is None:
        aggregator_provider = None
        if app is not None:

            def aggregator_provider() -> Any | None:  # noqa: E306
                scanner = getattr(app, "scanner_service", None)
                return scanner.aggregator if scanner is not None else None

        _service = WSRecoveryService(aggregator_provider=aggregator_provider)
        _service.register()
    return _service


__all__ = [
    "BrokerSessionRefreshedEvent",
    "WSRecoveryService",
    "init_ws_recovery_service",
]
