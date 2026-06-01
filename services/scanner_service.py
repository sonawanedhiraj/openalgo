"""Scanner service — DB-facing helpers, rule registry, and (item 5) the
in-house scanner that builds bars from the tick bus and fires scan_hit
events.

Three concerns live here:

* CRUD over ``scan_definitions`` and append-only writes / reads over
  ``scan_results`` (Stage 1.5 item 3).
* A code-backed rule registry — ``@scan_rule`` decorator + helpers. Rules
  self-register at import time; ``services.scan_rules`` triggers the
  imports. (Stage 1.5 item 5, commit 1).
* ``ScannerService`` — the broker-agnostic core that subscribes to the
  ZMQ tick bus, drives ``MultiIntervalAggregator``, evaluates each
  enabled ``scan_definition`` at bar close, writes ``scan_results`` and
  emits a ``scan_hit`` event. (Stage 1.5 item 5, commit 2; see
  ``services.scanner_service.ScannerService``.)

Patterns follow ``services/signal_decision_service`` and ``scan_cycle_service``:
each function resolves the live module-level ``db_session`` lazily so tests
can monkeypatch it cleanly.
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy.exc import IntegrityError

from database.scanner_db import (
    ScanDefinition,
    ScanResult,
    _definition_to_dict,
    _now_iso,
    _result_to_dict,
)
from services import indicators as _indicators
from services.bar_aggregator import MultiIntervalAggregator
from utils.event_bus import Event
from utils.event_bus import bus as _default_bus
from utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Rule registry — code-backed scan rules self-register via @scan_rule.
# ---------------------------------------------------------------------------
#
# A rule is a Python callable with signature ``rule(bars, indicators) -> bool``:
#   * ``bars`` is a ``pandas.DataFrame`` of the recent OHLCV window
#     (most-recent bar is the last row).
#   * ``indicators`` is a dict of pre-computed indicator series keyed by
#     short name (e.g. ``ema_20``, ``atr_14``, ``rsi_14``, ``volume_avg_20``).
#   * Return ``True`` to flag the symbol at this bar close.
#
# Rules are referenced from ``scan_definitions.rule_module`` by their
# registered name (not by Python dotted path) so the DB row stays stable
# even when a rule moves to a different module.

_rule_registry: dict[str, Callable[..., bool]] = {}
_rule_metadata: dict[str, dict[str, str]] = {}


def scan_rule(
    name: str,
    screener_type: str,
    description: str = "",
) -> Callable[[Callable[..., bool]], Callable[..., bool]]:
    """Decorator: register a scan rule by name.

    The rule function should accept ``(bars, indicators)`` and return a
    boolean. ``screener_type`` mirrors the ``scan_definitions.screener_type``
    column (``"buy"`` or ``"sell"``) so the scanner can match a DB row to
    the right rule callable.

    Re-registering the same name is allowed (and useful in tests) — the
    new callable replaces the old one; a warning is logged so accidental
    collisions are visible.
    """
    if screener_type not in {"buy", "sell"}:
        raise ValueError(
            f"screener_type must be 'buy' or 'sell', got {screener_type!r}"
        )

    def decorator(fn: Callable[..., bool]) -> Callable[..., bool]:
        if name in _rule_registry:
            logger.warning("scan_rule %r being re-registered (was %s, now %s)",
                           name, _rule_registry[name], fn)
        _rule_registry[name] = fn
        _rule_metadata[name] = {
            "screener_type": screener_type,
            "description": description,
        }
        return fn

    return decorator


def get_rule(name: str) -> Callable[..., bool] | None:
    """Return the registered rule callable, or ``None`` if no such name."""
    return _rule_registry.get(name)


def all_rules() -> dict[str, dict[str, Any]]:
    """Snapshot of the registry: ``{name: {"fn": callable, **metadata}}``."""
    return {
        name: {"fn": fn, **_rule_metadata.get(name, {})}
        for name, fn in _rule_registry.items()
    }


def _clear_rule_registry_for_tests() -> None:
    """Drop all registered rules. Tests use this to reset the registry."""
    _rule_registry.clear()
    _rule_metadata.clear()


def _session():
    """Resolve the live session from the DB module on each call.

    The DB module's ``db_session`` global is what tests monkeypatch.
    """
    from database import scanner_db as sdb

    return sdb.db_session


def init_scanner_db() -> None:
    """Idempotent table creation. Thin wrapper around the DB module's init."""
    from database import scanner_db as sdb

    sdb.init_db()


# ---------------------------------------------------------------------------
# scan_definitions
# ---------------------------------------------------------------------------


def create_scan_definition(
    name: str,
    screener_type: str,
    expression_json: str | dict | list | None = None,
    rule_module: str | None = None,
    enabled: bool = True,
) -> int:
    """Insert a scan definition and return its id.

    ``expression_json`` may be passed as a Python object (dict/list) for
    convenience — it will be JSON-encoded. Pass ``None`` for code-backed
    rules that live entirely in ``rule_module``; the column stores an
    empty JSON object in that case so the NOT NULL constraint is satisfied
    without callers needing to know.

    Raises ``IntegrityError`` on duplicate name.
    """
    if screener_type not in {"buy", "sell"}:
        raise ValueError(f"screener_type must be 'buy' or 'sell', got {screener_type!r}")

    if expression_json is None:
        encoded_expression = "{}"
    elif isinstance(expression_json, str):
        encoded_expression = expression_json
    else:
        encoded_expression = json.dumps(expression_json)

    now = _now_iso()
    sess = _session()
    try:
        row = ScanDefinition(
            name=name,
            screener_type=screener_type,
            expression_json=encoded_expression,
            rule_module=rule_module,
            enabled=1 if enabled else 0,
            created_at=now,
            updated_at=now,
        )
        sess.add(row)
        sess.commit()
        return row.id
    except IntegrityError:
        sess.rollback()
        raise
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.remove()


def get_scan_definitions(enabled_only: bool = True) -> list[dict[str, Any]]:
    """Return all scan definitions, optionally filtered to enabled only."""
    sess = _session()
    try:
        q = sess.query(ScanDefinition)
        if enabled_only:
            q = q.filter(ScanDefinition.enabled == 1)
        rows = q.order_by(ScanDefinition.id.asc()).all()
        return [_definition_to_dict(r) for r in rows]
    finally:
        sess.remove()


# ---------------------------------------------------------------------------
# scan_results
# ---------------------------------------------------------------------------


def record_scan_result(
    scan_definition_id: int,
    symbols: list[str],
    source: str,
    posted_to_engine: bool = False,
    notes: str | None = None,
) -> int:
    """Append a scan result row and return its id."""
    if source not in {"chartink", "inhouse", "shadow", "manual"}:
        raise ValueError(
            f"source must be one of chartink|inhouse|shadow|manual, got {source!r}"
        )
    sess = _session()
    try:
        row = ScanResult(
            scan_definition_id=scan_definition_id,
            run_at=_now_iso(),
            symbols=json.dumps(symbols),
            source=source,
            posted_to_engine=1 if posted_to_engine else 0,
            notes=notes,
        )
        sess.add(row)
        sess.commit()
        return row.id
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.remove()


def get_scan_results(hours: int = 24, source: str | None = None) -> list[dict[str, Any]]:
    """Return scan results from the last ``hours`` hours, optionally filtered by source.

    Ordered by ``run_at DESC`` (most recent first).
    """
    from datetime import datetime, timedelta

    import pytz

    cutoff = (
        datetime.now(pytz.timezone("Asia/Kolkata")) - timedelta(hours=hours)
    ).isoformat()

    sess = _session()
    try:
        q = sess.query(ScanResult).filter(ScanResult.run_at >= cutoff)
        if source is not None:
            q = q.filter(ScanResult.source == source)
        rows = q.order_by(ScanResult.run_at.desc()).all()
        return [_result_to_dict(r) for r in rows]
    finally:
        sess.remove()


def get_scan_comparison(date_iso: str) -> dict[str, Any]:
    """Shadow validation stub — Stage 1.5 item 7 will implement this.

    Intended to diff Chartink-sourced symbols against in-house scanner output
    for ``date_iso`` so we can quantify drift before promoting any in-house
    rule from shadow to enforced.
    """
    return {
        "date": date_iso,
        "implemented": False,
        "note": "stub — Stage 1.5 item 7 will populate chartink vs inhouse diff",
    }


# ---------------------------------------------------------------------------
# ScannerService — the in-house scanner core (Stage 1.5 item 5, commit 2).
# ---------------------------------------------------------------------------
#
# Flow:
#
#   ZMQ SUB  ──▶  _ingest_message  ──▶  MultiIntervalAggregator.on_tick
#                                          │
#                                          ▼ (bar close only)
#                                _on_bar_close(symbol, interval, bar)
#                                          │
#                  ┌───────────────────────┼─────────────────────────┐
#                  ▼                       ▼                         ▼
#       update _bar_history       evaluate enabled rules    on match:
#                                                          - record_scan_result
#                                                          - bus.publish(ScanHitEvent)
#
# A scan_hit consumer is NOT wired in this commit — Stage 1.5 item 6 will
# subscribe the webhook poster to ``topic="scan_hit"``. Until then the
# events fire but go to no subscriber, which is the intended idle state.

@dataclass
class ScanHitEvent(Event):
    """Emitted on the event bus when a rule fires at bar close.

    Item 6 (webhook poster) will subscribe to ``topic="scan_hit"`` to
    forward the symbol into the engine. Other consumers (e.g. the shadow
    validator in item 7) can subscribe to the same topic without affecting
    the live engine path.
    """

    scan_definition_id: int = 0
    scan_name: str = ""
    screener_type: str = ""
    symbol: str = ""
    interval: str = ""
    bar: dict[str, Any] = field(default_factory=dict)
    # Row id of the freshly-written ``scan_results`` row (set by the scanner
    # right after ``record_scan_result`` returns). 0 means the audit insert
    # failed — consumers should treat that as "no row to update".
    scan_result_id: int = 0
    topic: str = "scan_hit"


# Topic prefixes the scanner subscribes to.
# All broker adapters publish ``EXCHANGE_SYMBOL_MODE`` — see
# ``websocket_proxy/server.py:zmq_listener``. We subscribe with an empty
# filter and route per-tick because the per-symbol cost is trivial and
# the topic format may grow new multi-segment exchanges over time.
_DEFAULT_ZMQ_ENDPOINT = "tcp://127.0.0.1:5555"

# Two-segment exchange prefixes — keep in sync with the proxy's table.
# If a new index/multi-segment exchange ships, add it both here and in
# ``websocket_proxy/server.py:zmq_listener``.
_MULTI_SEGMENT_EXCHANGE_PREFIXES: frozenset[tuple[str, str]] = frozenset({
    ("NSE", "INDEX"),
    ("BSE", "INDEX"),
    ("MCX", "INDEX"),
    ("GLOBAL", "INDEX"),
})


def _parse_topic(topic: str) -> tuple[str, str, str] | None:
    """Split a ``EXCHANGE_SYMBOL_MODE`` topic into its three parts.

    Returns ``None`` for cache-invalidation, account-event, or otherwise
    malformed topics. This mirrors the parsing in
    ``websocket_proxy/server.py:zmq_listener`` — keep the two in sync if
    the topic format ever changes.
    """
    if not topic or topic.startswith("CACHE_INVALIDATE"):
        return None
    if topic.endswith(("_orders", "_positions", "_margins")):
        return None
    parts = topic.split("_")
    if len(parts) < 3:
        return None

    mode_str = parts[-1]
    remaining = parts[:-1]

    if len(remaining) >= 2 and (remaining[0], remaining[1]) in _MULTI_SEGMENT_EXCHANGE_PREFIXES:
        exchange = f"{remaining[0]}_{remaining[1]}"
        symbol = "_".join(remaining[2:])
    else:
        exchange = remaining[0]
        symbol = "_".join(remaining[1:])

    if not symbol:
        return None
    return exchange, symbol, mode_str


def _normalize_tick(market_data: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce a broker-published tick dict to the shape ``BarBuilder`` expects.

    Returns ``{"price", "cumulative_volume", "ts"}`` or ``None`` when the
    payload is missing a usable price (LTP modes without volume still
    yield a usable tick — volume just defaults to 0, which the bar
    builder treats as no delta).
    """
    price = None
    for key in ("ltp", "last_price", "last_traded_price", "price"):
        value = market_data.get(key)
        if value not in (None, ""):
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            break
    if price is None:
        return None

    cum_volume = 0
    for key in ("volume", "volume_traded", "cum_volume"):
        value = market_data.get(key)
        if value not in (None, ""):
            try:
                cum_volume = int(float(value))
                break
            except (TypeError, ValueError):
                continue

    ts = _dt.datetime.now()
    for key in ("exchange_timestamp", "timestamp", "ltt"):
        value = market_data.get(key)
        if value in (None, ""):
            continue
        try:
            if isinstance(value, _dt.datetime):
                ts = value.replace(tzinfo=None)
                break
            if isinstance(value, (int, float)):
                # Treat values > 10^10 as milliseconds (epoch_s ceiling).
                ts = _dt.datetime.fromtimestamp(
                    value / 1000 if value > 10_000_000_000 else value
                )
                break
        except Exception:
            continue

    return {"price": price, "cumulative_volume": cum_volume, "ts": ts}


class ScannerService:
    """Subscribes to the ZMQ tick bus, builds bars per (symbol, interval),
    evaluates every enabled scan_definition at bar close, and emits
    ``ScanHitEvent`` for each match.

    Lifecycle:
      * ``start()`` spawns a daemon thread that opens the ZMQ SUB socket
        and routes ticks into the aggregator. Construction is cheap and
        side-effect free — no socket is opened until ``start()``.
      * ``stop()`` flips an event and closes the socket so the subscriber
        thread exits cleanly.

    Per-symbol bar history is kept as a small rolling ``pandas.DataFrame``
    capped at ``history_size`` rows (default 100). On every bar close the
    new bar is appended, indicators are recomputed from the trimmed window,
    and each enabled rule is evaluated. Persistence is one ``scan_results``
    row per match, with ``source='inhouse'``.
    """

    def __init__(
        self,
        symbols: Iterable[str],
        intervals: list[str] | None = None,
        bus: Any = None,
        zmq_endpoint: str = _DEFAULT_ZMQ_ENDPOINT,
        history_size: int = 100,
    ) -> None:
        self.symbols: set[str] = {s.strip() for s in symbols if s and s.strip()}
        self.intervals: list[str] = list(intervals) if intervals else ["5m"]
        self.bus = bus if bus is not None else _default_bus
        self.zmq_endpoint = zmq_endpoint
        self.history_size = int(history_size)

        # Ensure the example rule modules are imported so they self-register
        # before any bar close lands. Import here (not at module top) to keep
        # ``services.scanner_service`` cheap to import for unrelated callers
        # (e.g. the DB-only test fixtures).
        import services.scan_rules  # noqa: F401, PLC0415

        self.aggregator = MultiIntervalAggregator(
            symbols=list(self.symbols),
            intervals=self.intervals,
            on_bar_close=self._on_bar_close,
        )

        # (symbol, interval) → rolling OHLCV frame, capped at history_size.
        self._bar_history: dict[tuple[str, str], pd.DataFrame] = {}
        self._history_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._subscriber_thread: threading.Thread | None = None
        self._zmq_context: Any = None
        self._zmq_socket: Any = None
        self._running = False

    # -- public lifecycle ---------------------------------------------------

    def start(self) -> None:
        """Open the ZMQ subscriber thread. Safe to call once; re-calling is a no-op."""
        if self._running:
            logger.debug("ScannerService.start() called while already running")
            return
        self._stop_event.clear()
        self._subscriber_thread = threading.Thread(
            target=self._run_subscriber,
            daemon=True,
            name="ScannerZMQSubscriber",
        )
        self._subscriber_thread.start()
        self._running = True
        logger.info(
            "ScannerService started: %d symbols on intervals=%s, zmq=%s",
            len(self.symbols), self.intervals, self.zmq_endpoint,
        )

    def stop(self) -> None:
        """Signal the subscriber thread to exit and tear down the ZMQ socket."""
        if not self._running:
            return
        self._stop_event.set()
        sock = self._zmq_socket
        if sock is not None:
            try:
                sock.close(linger=0)
            except Exception:
                logger.exception("ScannerService failed to close ZMQ socket cleanly")
        if self._subscriber_thread is not None:
            self._subscriber_thread.join(timeout=5.0)
        self._running = False
        logger.info("ScannerService stopped")

    def running(self) -> bool:
        return self._running

    # -- ZMQ subscriber -----------------------------------------------------

    def _run_subscriber(self) -> None:
        """Background thread: connect ZMQ SUB, route ticks to the aggregator."""
        import zmq  # noqa: PLC0415 — keep pyzmq optional at import time

        try:
            self._zmq_context = zmq.Context.instance()
            self._zmq_socket = self._zmq_context.socket(zmq.SUB)
            self._zmq_socket.setsockopt(zmq.SUBSCRIBE, b"")
            self._zmq_socket.connect(self.zmq_endpoint)
        except Exception:
            logger.exception(
                "ScannerService: failed to open ZMQ SUB at %s", self.zmq_endpoint
            )
            return

        poller = zmq.Poller()
        poller.register(self._zmq_socket, zmq.POLLIN)

        while not self._stop_event.is_set():
            try:
                events = dict(poller.poll(timeout=300))
                if self._zmq_socket not in events:
                    continue
                topic_b, data_b = self._zmq_socket.recv_multipart()
            except zmq.ZMQError:
                if self._stop_event.is_set():
                    return
                logger.exception("ScannerService: ZMQ recv failed")
                continue
            except Exception:
                logger.exception("ScannerService: subscriber loop hiccup")
                continue

            try:
                topic_str = topic_b.decode("utf-8")
                data_str = data_b.decode("utf-8")
                self._ingest_message(topic_str, data_str)
            except Exception:
                logger.exception("ScannerService: failed to process ZMQ message")

    def _ingest_message(self, topic: str, data_str: str) -> None:
        """Parse a ZMQ frame and forward the tick to the aggregator.

        Visibility: package-private so tests can drive the parser without
        spinning up real sockets. Returns silently for messages that don't
        match any subscribed symbol.
        """
        parsed = _parse_topic(topic)
        if parsed is None:
            return
        _exchange, symbol, _mode = parsed
        if symbol not in self.symbols:
            return
        try:
            market_data = json.loads(data_str)
        except Exception:
            logger.debug("ScannerService: bad JSON in topic %s", topic)
            return
        tick = _normalize_tick(market_data)
        if tick is None:
            return
        self.aggregator.on_tick(symbol, tick)

    # -- per-bar evaluation -------------------------------------------------

    def _on_bar_close(self, symbol: str, interval: str, bar: dict[str, Any]) -> None:
        """Aggregator callback: append to history, evaluate every enabled rule.

        Any exception raised here is caught and logged but does NOT propagate
        back into the aggregator — a single rule blowing up must not kill
        future ticks for other symbols.
        """
        try:
            bars = self._append_bar(symbol, interval, bar)
            indicators_dict = self._build_indicators(bars)
            self._evaluate_definitions(symbol, interval, bars, indicators_dict, bar)
        except Exception:
            logger.exception(
                "ScannerService: _on_bar_close failed for %s/%s", symbol, interval
            )

    def _append_bar(
        self, symbol: str, interval: str, bar: dict[str, Any]
    ) -> pd.DataFrame:
        """Push the closed bar onto the rolling window and return the frame."""
        import pandas as pd  # noqa: PLC0415

        row = {
            "ts": bar.get("ts"),
            "open": float(bar.get("open", 0.0)),
            "high": float(bar.get("high", 0.0)),
            "low": float(bar.get("low", 0.0)),
            "close": float(bar.get("close", 0.0)),
            "volume": float(bar.get("volume", 0) or 0),
        }
        with self._history_lock:
            key = (symbol, interval)
            existing = self._bar_history.get(key)
            new_frame = pd.DataFrame([row])
            if existing is None or existing.empty:
                combined = new_frame
            else:
                combined = pd.concat([existing, new_frame], ignore_index=True)
            if len(combined) > self.history_size:
                combined = combined.iloc[-self.history_size :].reset_index(drop=True)
            self._bar_history[key] = combined
            return combined

    def _build_indicators(self, bars: pd.DataFrame) -> dict[str, Any]:
        """Pre-compute the standard indicator series rules can reach for.

        Returns a dict with ``ema_20``, ``atr_14``, ``rsi_14``, ``volume_avg_20``.
        Each value is a ``pandas.Series`` aligned with ``bars`` (NaN during
        warm-up). NaN-handling is the rule's responsibility.
        """
        try:
            ema_20 = _indicators.ema(bars["close"], period=20)
        except Exception:
            ema_20 = None
        try:
            atr_14 = _indicators.atr(bars, period=14) if len(bars) >= 2 else None
        except Exception:
            atr_14 = None
        try:
            rsi_14 = _indicators.rsi(bars["close"], period=14)
        except Exception:
            rsi_14 = None
        try:
            volume_avg_20 = _indicators.volume_average(bars["volume"], period=20)
        except Exception:
            volume_avg_20 = None
        return {
            "ema_20": ema_20,
            "atr_14": atr_14,
            "rsi_14": rsi_14,
            "volume_avg_20": volume_avg_20,
        }

    def _evaluate_definitions(
        self,
        symbol: str,
        interval: str,
        bars: pd.DataFrame,
        indicators_dict: dict[str, Any],
        bar: dict[str, Any],
    ) -> None:
        """For each enabled scan_definition: look up its rule and evaluate.

        On match: append a ``scan_results`` row (source='inhouse', one
        symbol per row) and publish a ``ScanHitEvent``. We intentionally
        write one row per (definition, symbol, bar) hit — the consumer in
        item 6 will dedupe / debounce if needed.
        """
        for definition in get_scan_definitions(enabled_only=True):
            rule_name = definition.get("rule_module") or definition.get("name")
            if not rule_name:
                continue
            rule_fn = get_rule(rule_name)
            if rule_fn is None:
                logger.debug(
                    "ScannerService: no registered rule named %r (definition id=%s)",
                    rule_name, definition.get("id"),
                )
                continue
            try:
                matched = bool(rule_fn(bars, indicators_dict))
            except Exception:
                logger.exception(
                    "ScannerService: rule %r raised for %s/%s",
                    rule_name, symbol, interval,
                )
                continue
            if not matched:
                continue

            scan_result_id = 0
            try:
                scan_result_id = record_scan_result(
                    scan_definition_id=int(definition["id"]),
                    symbols=[symbol],
                    source="inhouse",
                    posted_to_engine=False,
                    notes=f"interval={interval}",
                )
            except Exception:
                logger.exception(
                    "ScannerService: record_scan_result failed for %s/%s",
                    symbol, interval,
                )

            try:
                self.bus.publish(
                    ScanHitEvent(
                        scan_definition_id=int(definition["id"]),
                        scan_name=str(definition.get("name", "")),
                        screener_type=str(definition.get("screener_type", "")),
                        symbol=symbol,
                        interval=interval,
                        bar=dict(bar),
                        scan_result_id=int(scan_result_id or 0),
                    )
                )
            except Exception:
                logger.exception(
                    "ScannerService: failed to publish scan_hit for %s/%s",
                    symbol, interval,
                )
