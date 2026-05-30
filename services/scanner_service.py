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

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy.exc import IntegrityError

from database.scanner_db import (
    ScanDefinition,
    ScanResult,
    _definition_to_dict,
    _now_iso,
    _result_to_dict,
)
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
