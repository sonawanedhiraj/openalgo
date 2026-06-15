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
from collections import deque
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
from services.bar_aggregator import BarBuilder, MultiIntervalAggregator
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
        raise ValueError(f"screener_type must be 'buy' or 'sell', got {screener_type!r}")

    def decorator(fn: Callable[..., bool]) -> Callable[..., bool]:
        if name in _rule_registry:
            logger.warning(
                "scan_rule %r being re-registered (was %s, now %s)", name, _rule_registry[name], fn
            )
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
    return {name: {"fn": fn, **_rule_metadata.get(name, {})} for name, fn in _rule_registry.items()}


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
        raise ValueError(f"source must be one of chartink|inhouse|shadow|manual, got {source!r}")
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

    cutoff = (datetime.now(pytz.timezone("Asia/Kolkata")) - timedelta(hours=hours)).isoformat()

    sess = _session()
    try:
        q = sess.query(ScanResult).filter(ScanResult.run_at >= cutoff)
        if source is not None:
            q = q.filter(ScanResult.source == source)
        rows = q.order_by(ScanResult.run_at.desc()).all()
        return [_result_to_dict(r) for r in rows]
    finally:
        sess.remove()


def _symbol_set_from_json(blob: Any) -> set[str]:
    """Parse a JSON-encoded list of symbols into a clean set.

    Tolerant of ``None``, malformed JSON, and non-list payloads — returns an
    empty set rather than raising, so a single bad audit row can't sink the
    whole comparison.
    """
    if not blob:
        return set()
    try:
        items = json.loads(blob)
    except (ValueError, TypeError):
        return set()
    if not isinstance(items, list):
        return set()
    out: set[str] = set()
    for s in items:
        if s is None:
            continue
        sym = str(s).strip().upper()
        if sym:
            out.add(sym)
    return out


def get_scan_comparison(
    date: str | None = None,
    scan_name: str = "fno_intraday_buy_chartink",
) -> dict[str, Any]:
    """Return precision/recall + diff lists for in-house vs Chartink BUY for a single day.

    Shadow-validation harness (Stage 1.5 item 7). Compares the in-house
    scanner's BUY hits — ``scan_results`` rows with ``source='inhouse'`` whose
    ``scan_definition.name == scan_name`` — against the live Chartink BUY hits
    recorded in ``scan_cycle.screener_buy`` (rows with ``cycle_kind='chartink'``).
    Only the BUY leg is counted; the paired SELL leg (``screener_sell``) is
    ignored. Treating Chartink as ground truth, ``inhouse_only`` are false
    positives and ``chartink_only`` are false negatives.

    Both ``run_at`` and ``started_at`` are stored as IST ISO-8601 strings
    (``datetime.now(Asia/Kolkata).isoformat()``), so a ``[:10]`` date-prefix
    match is the correct IST day filter — no UTC conversion is applied.

    Args:
        date: ``'YYYY-MM-DD'`` IST. Defaults to today in IST.
        scan_name: in-house scan definition name to compare (default the
            Chartink BUY mirror).

    Returns:
        A dict with ``date``, ``scan_name``, the two side counts, the three
        diff lists (sorted), ``precision`` / ``recall`` / ``f1`` (``None`` when
        undefined), and an ``error`` key if a DB query failed.
    """
    if date is None:
        import pytz

        date = _dt.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")

    result: dict[str, Any] = {
        "date": date,
        "scan_name": scan_name,
        "inhouse_count": 0,
        "chartink_count": 0,
        "intersection": [],
        "intersection_count": 0,
        "inhouse_only": [],
        "chartink_only": [],
        "precision": None,
        "recall": None,
        "f1": None,
    }

    # --- In-house side: scan_results (source='inhouse') joined to its definition.
    inhouse_symbols: set[str] = set()
    try:
        sess = _session()
        try:
            rows = (
                sess.query(ScanResult)
                .join(ScanDefinition, ScanResult.scan_definition_id == ScanDefinition.id)
                .filter(ScanDefinition.name == scan_name)
                .filter(ScanResult.source == "inhouse")
                .all()
            )
            for row in rows:
                if (row.run_at or "")[:10] != date:
                    continue
                inhouse_symbols |= _symbol_set_from_json(row.symbols)
        finally:
            sess.remove()
    except Exception as exc:
        logger.exception("get_scan_comparison: in-house query failed")
        result["error"] = f"inhouse query failed: {exc}"
        return result

    # --- Chartink side: scan_cycle BUY leg (cycle_kind='chartink'). Any other
    # cycle_kind (incl. test/'trend-up' pollution) is excluded by the filter.
    chartink_symbols: set[str] = set()
    try:
        from database import scan_cycle_db as scdb

        csess = scdb.db_session
        try:
            rows = csess.query(scdb.ScanCycle).filter(scdb.ScanCycle.cycle_kind == "chartink").all()
            for row in rows:
                if (row.started_at or "")[:10] != date:
                    continue
                chartink_symbols |= _symbol_set_from_json(row.screener_buy)
        finally:
            csess.remove()
    except Exception as exc:
        logger.exception("get_scan_comparison: chartink query failed")
        result["error"] = f"chartink query failed: {exc}"
        return result

    intersection = inhouse_symbols & chartink_symbols
    n, m, k = len(inhouse_symbols), len(chartink_symbols), len(intersection)

    precision = k / n if n else None
    recall = k / m if m else None
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)

    result.update(
        {
            "inhouse_count": n,
            "chartink_count": m,
            "intersection": sorted(intersection),
            "intersection_count": k,
            "inhouse_only": sorted(inhouse_symbols - chartink_symbols),
            "chartink_only": sorted(chartink_symbols - inhouse_symbols),
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    )
    return result


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
_MULTI_SEGMENT_EXCHANGE_PREFIXES: frozenset[tuple[str, str]] = frozenset(
    {
        ("NSE", "INDEX"),
        ("BSE", "INDEX"),
        ("MCX", "INDEX"),
        ("GLOBAL", "INDEX"),
    }
)


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
                ts = _dt.datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value)
                break
        except Exception:
            continue

    return {"price": price, "cumulative_volume": cum_volume, "ts": ts}


class _Rolling15mBars:
    """Per-symbol 15-minute bar accumulator backed by a ``BarBuilder``.

    ``BarBuilder`` (in ``services.bar_aggregator``) is single-symbol /
    single-interval and only fires a callback per tick — it keeps no
    history. We wrap one here, capture each *closed* 15m bar (elapsed_pct
    >= 1.0) into a ``deque`` capped at ``maxlen`` rows (~50 is enough for
    RSI(14) warm-up plus buffer), and expose ``get_recent_bars(n)`` for
    rule evaluation. Memory is bounded by the deque maxlen, per symbol.
    """

    def __init__(self, symbol: str, maxlen: int = 50) -> None:
        self.symbol = symbol
        self._closed: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._builder = BarBuilder(symbol, "15m", on_bar=self._on_bar)

    def _on_bar(self, bar: dict[str, Any]) -> None:
        if bar.get("elapsed_pct", 0.0) >= 1.0:
            self._closed.append(
                {
                    "ts": bar.get("ts"),
                    "open": bar.get("open"),
                    "high": bar.get("high"),
                    "low": bar.get("low"),
                    "close": bar.get("close"),
                    "volume": bar.get("volume"),
                }
            )

    def on_tick(self, tick: dict[str, Any]) -> None:
        self._builder.on_tick(tick)

    def get_recent_bars(self, n: int = 50) -> pd.DataFrame:
        """Return the most recent ``n`` closed 15m bars as a DataFrame.

        Empty DataFrame (not None) when no 15m bar has closed yet.
        """
        rows = list(self._closed)
        if n:
            rows = rows[-n:]
        return pd.DataFrame(rows)


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

        # Per-symbol rolling 15-minute bar accumulators. Fed the same ticks
        # as the aggregator (see _ingest_message); memory bounded per symbol
        # by the deque inside each _Rolling15mBars (50 bars).
        self._bar_15m_history: dict[str, _Rolling15mBars] = {
            sym: _Rolling15mBars(sym) for sym in self.symbols
        }

        # Daily/weekly OHLCV cache (Task 2/3) — singleton warmed at boot and
        # refreshed at 16:00 IST. Constructed once here, not per tick.
        try:
            from services.scanner_history_provider import get_provider  # noqa: PLC0415

            self._history_provider = get_provider()
        except Exception:
            logger.exception("ScannerService: failed to obtain history provider")
            self._history_provider = None

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
            len(self.symbols),
            self.intervals,
            self.zmq_endpoint,
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

    # -- intraday read-out (consumed by sector_follow Fix 1b) ---------------

    def get_today_ohlcv(
        self, symbol: str, as_of_date: _dt.date
    ) -> tuple[float | None, float | None]:
        """Aggregate TODAY's ``(close, volume)`` for ``symbol`` from the live feed.

        Reads the rolling closed-bar history for ``symbol`` at the primary
        interval plus the in-progress bar, filtered to ``as_of_date`` (IST). The
        close is the latest bar's close; the volume is the sum of today's bar
        volumes (each bar's volume is the delta-from-cumulative the BarBuilder
        already computed, so the sum is today's cumulative traded volume).

        Returns ``(None, None)`` when the scanner has no bars for ``symbol`` today
        (e.g. an index the scanner doesn't track, or a feed that never started) so
        the caller can fall back to historify. Thread-safe; never raises.

        Bar timestamps are naive datetimes derived from the broker's
        ``exchange_timestamp`` on an IST box, so ``ts.date()`` is the IST trade
        date — the same assumption the aggregator's bucketing already relies on.
        """
        try:
            interval = self.intervals[0] if self.intervals else "5m"
            with self._history_lock:
                frame = self._bar_history.get((symbol, interval))
                rows = frame.to_dict("records") if frame is not None and not frame.empty else []
            total_vol = 0.0
            last_close: float | None = None
            seen = False
            for r in rows:
                ts = r.get("ts")
                if ts is None or getattr(ts, "date", lambda: None)() != as_of_date:
                    continue
                seen = True
                total_vol += float(r.get("volume") or 0)
                last_close = float(r.get("close"))
            # Fold in the in-progress (not-yet-closed) bar — strictly after the
            # last closed bar's bucket, so no double counting.
            try:
                cur = self.aggregator.current_bar(symbol, interval)
            except Exception:
                cur = None
            if cur is not None:
                ts = cur.get("ts")
                if ts is not None and getattr(ts, "date", lambda: None)() == as_of_date:
                    seen = True
                    total_vol += float(cur.get("volume") or 0)
                    last_close = float(cur.get("close"))
            if not seen or last_close is None:
                return None, None
            return last_close, total_vol
        except Exception:
            logger.exception("ScannerService.get_today_ohlcv failed for %s", symbol)
            return None, None

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
            logger.exception("ScannerService: failed to open ZMQ SUB at %s", self.zmq_endpoint)
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
        builder_15m = self._bar_15m_history.get(symbol)
        if builder_15m is not None:
            builder_15m.on_tick(tick)

    # -- per-bar evaluation -------------------------------------------------

    def _on_bar_close(self, symbol: str, interval: str, bar: dict[str, Any]) -> None:
        """Aggregator callback: append to history, evaluate every enabled rule.

        Any exception raised here is caught and logged but does NOT propagate
        back into the aggregator — a single rule blowing up must not kill
        future ticks for other symbols.
        """
        try:
            bars = self._append_bar(symbol, interval, bar)
            indicators_dict = self._build_indicators(symbol, bars)
            self._evaluate_definitions(symbol, interval, bars, indicators_dict, bar)
        except Exception:
            logger.exception("ScannerService: _on_bar_close failed for %s/%s", symbol, interval)

    def _append_bar(self, symbol: str, interval: str, bar: dict[str, Any]) -> pd.DataFrame:
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

    def _build_indicators(self, symbol: str, bars: pd.DataFrame) -> dict[str, Any]:
        """Pre-compute the per-symbol indicator bundle rules can reach for.

        Backward-compatible keys (5m-derived series, NaN during warm-up):
        ``ema_20``, ``atr_14``, ``rsi_14``, ``volume_avg_20``. NaN-handling
        is the rule's responsibility.

        Multi-timeframe frames (Task 4):
        * ``bars_5m`` — the 5m frame passed in (same object as ``bars``).
        * ``bars_15m`` — rolling 15m frame for ``symbol`` (empty DataFrame
          until the first 15m bar closes; ``None`` if the symbol is not
          tracked).
        * ``bars_daily`` / ``bars_weekly`` — from ``ScannerHistoryProvider``;
          ``None`` when unavailable.
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

        builder_15m = self._bar_15m_history.get(symbol)
        bars_15m = builder_15m.get_recent_bars(50) if builder_15m is not None else None

        bars_daily = None
        bars_weekly = None
        if self._history_provider is not None:
            try:
                bars_daily = self._history_provider.get_daily(symbol)
                bars_weekly = self._history_provider.get_weekly(symbol)
            except Exception:
                logger.exception("ScannerService: history provider lookup failed for %s", symbol)

        return {
            "ema_20": ema_20,
            "atr_14": atr_14,
            "rsi_14": rsi_14,
            "volume_avg_20": volume_avg_20,
            "bars_5m": bars,
            "bars_15m": bars_15m,
            "bars_daily": bars_daily,
            "bars_weekly": bars_weekly,
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
                    rule_name,
                    definition.get("id"),
                )
                continue
            try:
                matched = bool(rule_fn(bars, indicators_dict))
            except Exception:
                logger.exception(
                    "ScannerService: rule %r raised for %s/%s",
                    rule_name,
                    symbol,
                    interval,
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
                    symbol,
                    interval,
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
                    symbol,
                    interval,
                )


# ---------------------------------------------------------------------------
# Live singleton accessor (set by app.py at boot; read by sector_follow Fix 1b).
# ---------------------------------------------------------------------------
_SCANNER_SINGLETON: ScannerService | None = None


def set_scanner_service(svc: ScannerService | None) -> None:
    """Record the live ScannerService so other in-process services (sector_follow)
    can read today's aggregated bars without an import cycle. Called by app.py."""
    global _SCANNER_SINGLETON
    _SCANNER_SINGLETON = svc


def get_scanner_service() -> ScannerService | None:
    """Return the live ScannerService, or None when the scanner is disabled."""
    return _SCANNER_SINGLETON
