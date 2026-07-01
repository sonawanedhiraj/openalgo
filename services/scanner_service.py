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
import os
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import time as _dtime
from typing import Any

import pandas as pd
import pytz
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
# Market-hours gate (Tier-1 Fix #1 — kills the post-close AUROPHARMA-SELL class).
#
# The scanner is purely tick-driven: a straggler/backfill tick that closes a bar
# after the session ends still drives ``_evaluate_definitions``. Combined with
# the rule's wall-clock ``_SETTLE_CUTOFF`` flip and a stale daily-D bar, that
# produced 17 spurious post-close AUROPHARMA SELL fires on 2026-06-15 (see
# docs/research/strategy/screener/2026-06-15_inhouse_deep_analysis.md, FM-6/DP-5).
# This gate skips evaluation entirely outside [09:15, 15:30] IST. It is paired
# with a rule-side D-bar-date verify (in the scan_rules modules) so a future
# change to either one cannot silently re-open the post-close path on its own.
# ---------------------------------------------------------------------------
_IST = pytz.timezone("Asia/Kolkata")
_MARKET_OPEN_IST = _dtime(9, 15)
_MARKET_CLOSE_IST = _dtime(15, 30)


def _postclose_gate_enabled() -> bool:
    """``SCANNER_POSTCLOSE_GATE_ENABLED`` env flag (default true). When false the
    market-hours gate is a no-op — evaluation runs at any wall-clock time (the
    pre-Tier-1 behavior)."""
    return os.environ.get("SCANNER_POSTCLOSE_GATE_ENABLED", "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def _now_ist() -> _dt.datetime:
    """Current IST time. Indirected through a module function so tests can pin it."""
    return _dt.datetime.now(_IST)


def _within_market_hours(now_ist: _dt.datetime) -> bool:
    """True iff ``now_ist`` falls inside the [09:15, 15:30] IST trading window.

    ``datetime.time()`` returns the naive wall-clock component for both naive and
    tz-aware datetimes, so the comparison is against IST clock time directly."""
    t = now_ist.time()
    return _MARKET_OPEN_IST <= t <= _MARKET_CLOSE_IST


# ---------------------------------------------------------------------------
# Decision-input completeness metric (Tier-1 Fix #3 — ends the "0 hits == no
# data == failure" ambiguity, mirroring sector_follow Fix 1b).
#
# The scanner evaluates per-symbol-per-bar (tick-driven), so there is no natural
# "cycle". We accumulate the set of symbols that produced a live bar within a
# rolling wall-clock window and, when the window rolls, emit
# ``n_live / total_subscribed`` — <50% WARNING, <20% CRITICAL via Telegram, with
# a per-severity once-a-day dedup so a persistently-degraded feed does not spam.
# Limitation: a TOTAL feed outage produces no bar closes at all, so this path
# never fires — that case is the 15:18 smoke check's job (Tier 2). This metric
# catches PARTIAL degradation (some symbols live, most not) and reports coverage.
# ---------------------------------------------------------------------------


def _completeness_enabled() -> bool:
    """``SCANNER_COMPLETENESS_ENABLED`` env flag (default true)."""
    return os.environ.get("SCANNER_COMPLETENESS_ENABLED", "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def _completeness_window_min() -> int:
    """``SCANNER_COMPLETENESS_WINDOW_MIN`` (default 5) — the rolling accumulation
    window in minutes, ~one 5m bar cycle."""
    try:
        return max(1, int(os.environ.get("SCANNER_COMPLETENESS_WINDOW_MIN", "5")))
    except ValueError:
        return 5


def _completeness_warn_pct() -> float:
    """``SCANNER_COMPLETENESS_WARN_PCT`` (default 50)."""
    try:
        return float(os.environ.get("SCANNER_COMPLETENESS_WARN_PCT", "50"))
    except ValueError:
        return 50.0


def _completeness_crit_pct() -> float:
    """``SCANNER_COMPLETENESS_CRIT_PCT`` (default 20)."""
    try:
        return float(os.environ.get("SCANNER_COMPLETENESS_CRIT_PCT", "20"))
    except ValueError:
        return 20.0


def _default_completeness_notifier(message: str) -> None:
    """Route a completeness alert to Telegram via the shared notification service.
    Lazily imported so the scanner module stays cheap to import; never raises."""
    try:
        from services.notification_service import get_notification_service  # noqa: PLC0415

        get_notification_service().notify("scanner_completeness", message)
    except Exception:
        logger.debug("scanner completeness notifier unavailable", exc_info=True)


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


def _resolve_eval_snapshot(rule_fn: Callable[..., bool]) -> dict | None:
    """Look up the rule module's ``get_last_eval_snapshot`` (Issue #205).

    The rule modules optionally expose a thread-local ``get_last_eval_snapshot``
    helper that returns the gate values that drove the last successful
    evaluation. The scanner folds those values into the PASS log line so a
    future ``derive_today_and_yest``-class regression can be reproduced from
    logs alone, without a re-instrument-and-restart cycle.

    Returns ``None`` for un-instrumented rules; the caller falls back to the
    prior ``close=...`` log shape.
    """
    module_name = getattr(rule_fn, "__module__", None)
    if not module_name:
        return None
    module = sys.modules.get(module_name)
    if module is None:
        return None
    helper = getattr(module, "get_last_eval_snapshot", None)
    if helper is None or not callable(helper):
        return None
    try:
        snapshot = helper()
    except Exception:
        # An un-trusted helper must not break the log site.
        return None
    return snapshot if isinstance(snapshot, dict) and snapshot else None


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
    parameters_json: str | dict | None = None,
    parent_definition_id: int | None = None,
) -> int:
    """Insert a scan definition and return its id.

    ``expression_json`` may be passed as a Python object (dict/list) for
    convenience — it will be JSON-encoded. Pass ``None`` for code-backed
    rules that live entirely in ``rule_module``; the column stores an
    empty JSON object in that case so the NOT NULL constraint is satisfied
    without callers needing to know.

    ``parameters_json`` — optional Tier-3 override dict (or JSON string).
    ``parent_definition_id`` — optional Tier-3 FK to the source definition.

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

    if parameters_json is None:
        encoded_params = None
    elif isinstance(parameters_json, str):
        encoded_params = parameters_json
    else:
        encoded_params = json.dumps(parameters_json)

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
            parameters_json=encoded_params,
            parent_definition_id=parent_definition_id,
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
        except Exception:  # nosec B112 — intentional: try the next timestamp key on parse failure
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

    def seed_bars(self, bars: list[dict[str, Any]]) -> int:
        """Append pre-built 15m bars directly to the rolling deque (issue #201).

        Bypasses the tick-simulation path so a boot-time seeder can pre-warm
        the per-symbol 15m history from historical data without having to
        replay every constituent 1m tick. ``bars`` are appended in order;
        anything beyond ``maxlen`` is dropped by the deque.

        Each bar dict must carry ``ts/open/high/low/close/volume``. The
        ``elapsed_pct`` check from the live ``on_bar`` callback is skipped —
        the caller is asserting these are already-closed bars.

        Idempotent enough for retries: a duplicate ts replaces the prior
        entry of the same timestamp before append, so a partial-then-full
        seed converges without double-counting.

        Returns the number of bars actually folded in (after de-dup).
        """
        if not bars:
            return 0
        seen_ts = {row.get("ts") for row in self._closed}
        added = 0
        for b in bars:
            ts = b.get("ts")
            row = {
                "ts": ts,
                "open": b.get("open"),
                "high": b.get("high"),
                "low": b.get("low"),
                "close": b.get("close"),
                "volume": b.get("volume"),
            }
            if ts is not None and ts in seen_ts:
                # Replace the earlier bar with the new one (e.g. a
                # partial-then-full re-seed). Maintain deque order.
                self._closed = deque(
                    (r if r.get("ts") != ts else row for r in self._closed),
                    maxlen=self._closed.maxlen,
                )
            else:
                self._closed.append(row)
                seen_ts.add(ts)
                added += 1
        return added


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
        notifier: Callable[[str], None] | None = None,
    ) -> None:
        self.symbols: set[str] = {s.strip() for s in symbols if s and s.strip()}
        self.intervals: list[str] = list(intervals) if intervals else ["5m"]
        self.bus = bus if bus is not None else _default_bus
        self.zmq_endpoint = zmq_endpoint
        self.history_size = int(history_size)
        # Tier-1 Fix #3: Telegram notifier for completeness alerts (injectable for
        # tests; defaults to the shared notification service).
        self._notifier = notifier or _default_completeness_notifier
        # Rolling decision-input completeness window state.
        self._completeness_window_syms: set[str] = set()
        self._completeness_window_start: _dt.datetime | None = None
        self._completeness_alert_day: _dt.date | None = None
        self._completeness_alert_severities: set[str] = set()

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
                # Tier-1 Fix #2: name the reason instead of a silent (None, None).
                # DEBUG (not WARNING) because this is the EXPECTED return for an
                # untracked symbol (e.g. an index the scanner doesn't stream); the
                # caller (sector_follow) escalates loudly when it actually matters.
                logger.debug(
                    "ScannerService.get_today_ohlcv: no live bars for %s on %s "
                    "(seen=%s) — caller should fall back to historify",
                    symbol,
                    as_of_date,
                    seen,
                )
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

        Replayed bars (``bar["is_replay"]``) — folded in by the boot seeder
        (``scanner_aggregator_seeder``) or the WS-reconnect recovery
        (``ws_recovery_service``) via ``MultiIntervalAggregator.replay_bars`` —
        still warm the rolling history window (``_append_bar``) so RSI/SMA are
        ready when live ticks resume, but they are NEVER evaluated: a historical
        bar must not fire a scan hit, and a mid-session restart replays bars
        DURING market hours where the ``_evaluate_definitions`` market-hours gate
        would not skip them. Only genuine live bar closes are evaluated.
        """
        try:
            bars = self._append_bar(symbol, interval, bar)
            if bar.get("is_replay"):
                return
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

        # Resolve the symbol's exchange so rules can quickly skip non-stock
        # universes (e.g. NSE_INDEX) without firing missing-input warnings
        # against indices that were never meant to be evaluated. Issue #158 D2.
        try:
            from services.scanner_presubscribe import resolve_exchange_for_symbol

            exchange = resolve_exchange_for_symbol(symbol)
        except Exception:
            # Resolver failure is non-fatal — rules that care will treat
            # missing exchange as "unknown" and proceed as before.
            exchange = None

        return {
            # The symbol is threaded into the bundle so rules can name it in
            # their loud-failure / D-bar-date-verify logs (Tier-1 Fix #1/#2).
            "symbol": symbol,
            "exchange": exchange,
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

        Tier-1 Fix #1: a market-hours gate skips evaluation entirely outside
        [09:15, 15:30] IST so a straggler/backfill tick that closes a bar
        post-close cannot fire a (stale-bar) signal.

        Tier-1 Fix #3: each in-hours bar close records the symbol as "live this
        window" for the decision-input completeness metric.
        """
        now_ist = _now_ist()
        if _postclose_gate_enabled() and not _within_market_hours(now_ist):
            phase = "post-close" if now_ist.time() > _MARKET_CLOSE_IST else "pre-open"
            # DEBUG, not INFO: this fires per bar per symbol, so on a restart the
            # market-hours gate would flood the log with tens of thousands of
            # identical lines. The phase is obvious from the wall clock; the
            # backstop itself is unchanged.
            logger.debug(
                "scanner evaluation skipped: %s (now=%s IST) for %s/%s",
                phase,
                now_ist.strftime("%H:%M"),
                symbol,
                interval,
            )
            return

        if _completeness_enabled():
            self._record_completeness(symbol, now_ist)

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
                raw_params = definition.get("parameters_json")
                if raw_params:
                    try:
                        params = (
                            json.loads(raw_params) if isinstance(raw_params, str) else raw_params
                        )
                    except (ValueError, TypeError):
                        params = {}
                else:
                    params = {}
                eff_indicators = (
                    {**indicators_dict, "parameters": params} if params else indicators_dict
                )
                matched = bool(rule_fn(bars, eff_indicators))
            except Exception:
                logger.exception(
                    "ScannerService: rule %r raised for %s/%s",
                    rule_name,
                    symbol,
                    interval,
                )
                continue
            # Tier-1 Fix #2: loud per-symbol PASS / quiet FAIL. PASS is rare (only
            # on a match) so it is INFO; FAIL fires for ~every symbol on ~every bar
            # so it stays DEBUG to avoid flooding the log. The specific missing-input
            # reason (None daily-D etc.) is logged at WARNING inside the rule itself,
            # and the per-cycle completeness metric (Fix #3) surfaces aggregate gaps.
            if matched:
                # Issue #205: enrich the PASS line with the gate values that drove
                # the match. We pull the snapshot from a thread-local exposed by
                # the rule module (``get_last_eval_snapshot``). When a rule hasn't
                # been instrumented yet, we fall back to the prior ``close=...``
                # shape so the log site never crashes on an un-instrumented rule.
                snapshot = _resolve_eval_snapshot(rule_fn)
                if snapshot:
                    kv = " ".join(
                        f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}"
                        for k, v in snapshot.items()
                    )
                    logger.info(
                        "scanner PASS %s rule=%s interval=%s close=%s %s",
                        symbol,
                        rule_name,
                        interval,
                        bar.get("close"),
                        kv,
                    )
                else:
                    logger.info(
                        "scanner PASS %s rule=%s interval=%s close=%s",
                        symbol,
                        rule_name,
                        interval,
                        bar.get("close"),
                    )
            else:
                logger.debug("scanner FAIL %s rule=%s interval=%s", symbol, rule_name, interval)
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

    # -- decision-input completeness (Tier-1 Fix #3) ------------------------

    def _record_completeness(self, symbol: str, now_ist: _dt.datetime) -> None:
        """Note that ``symbol`` produced a live bar this window; emit + reset the
        metric when the rolling window has elapsed."""
        if self._completeness_window_start is None:
            self._completeness_window_start = now_ist
        elapsed = (now_ist - self._completeness_window_start).total_seconds()
        if elapsed >= _completeness_window_min() * 60:
            # The window elapsed — emit the PRIOR window's coverage, then start a
            # fresh window that this triggering bar belongs to.
            self._emit_completeness(now_ist)
            self._completeness_window_syms = set()
            self._completeness_window_start = now_ist
        self._completeness_window_syms.add(symbol)

    def _emit_completeness(self, now_ist: _dt.datetime) -> None:
        """Log ``n_live/total`` for the window and Telegram-alert when the live
        fraction falls below the WARNING / CRITICAL thresholds. Per-severity
        once-a-day dedup so a persistently-degraded feed alerts at most once each
        per day. Never raises."""
        total = len(self.symbols)
        if total <= 0:
            return
        n_live = len(self._completeness_window_syms)
        frac = n_live / total
        window_min = _completeness_window_min()
        logger.info(
            "scanner decision-input completeness: %d/%d (%.0f%%) symbols produced "
            "live bars in the last %d min",
            n_live,
            total,
            frac * 100,
            window_min,
        )
        crit = _completeness_crit_pct() / 100.0
        warn = _completeness_warn_pct() / 100.0
        if frac < crit:
            severity = "critical"
        elif frac < warn:
            severity = "warning"
        else:
            return

        # Per-severity once-a-day dedup.
        day = now_ist.date()
        if self._completeness_alert_day != day:
            self._completeness_alert_day = day
            self._completeness_alert_severities = set()
        if severity in self._completeness_alert_severities:
            return
        self._completeness_alert_severities.add(severity)

        try:
            if severity == "critical":
                msg = (
                    f"🔴 CRITICAL in-house scanner {day.isoformat()}: only "
                    f"{n_live}/{total} ({frac * 100:.0f}%) symbols produced live bars in the "
                    f"last {window_min} min — the feed is largely starved, signals are "
                    "effectively fail-closed."
                )
            else:
                msg = (
                    f"🟠 WARNING in-house scanner {day.isoformat()}: only "
                    f"{n_live}/{total} ({frac * 100:.0f}%) symbols produced live bars in the "
                    f"last {window_min} min (partial feed degradation)."
                )
            self._notifier(msg)
        except Exception:
            logger.exception("scanner completeness alert failed")


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
