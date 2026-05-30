"""Scanner service — DB-facing helpers for scan definitions and results.

This module will grow in Stage 1.5 item 5 to host the actual rule-evaluation
engine. For now it owns the persistence surface:

* CRUD over ``scan_definitions``.
* Append-only writes and time-window reads over ``scan_results``.
* A stub for ``get_scan_comparison()`` — the shadow-mode validation
  (Stage 1.5 item 7) will fill this in.

Patterns follow ``services/signal_decision_service`` and ``scan_cycle_service``:
each function resolves the live module-level ``db_session`` lazily so tests
can monkeypatch it cleanly.
"""

import json
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
