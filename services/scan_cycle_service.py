"""Fail-safe scan_cycle audit writes.

Every public function here wraps its DB work in a try/except that logs but
never raises. The webhook handler must not 500 because audit writes failed —
audit loss is recoverable; a missed order isn't.

``start_cycle()`` returns ``-1`` on failure. Subsequent ``heartbeat`` /
``complete_cycle`` calls with ``cycle_id=-1`` no-op silently, so the caller
can use the returned id unconditionally.
"""

import json
from typing import Any

from database.scan_cycle_db import (
    CycleHeartbeat,
    ScanCycle,
    _cycle_to_dict,
    _heartbeat_to_dict,
    _now_iso,
)
from database.scan_cycle_db import db_session as _module_db_session  # noqa: F401
from utils.logging import get_logger

logger = get_logger(__name__)


def _session():
    """Resolve the live session from the DB module on each call.

    The DB module's ``db_session`` global is what tests monkeypatch, so we
    must look it up fresh rather than binding at import time.
    """
    from database import scan_cycle_db as scdb

    return scdb.db_session


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError) as e:
        logger.warning("scan_cycle: failed to JSON-encode audit field: %s", e)
        return None


# ---------------------------------------------------------------------------
# Write path — every function below MUST be fail-safe.
# ---------------------------------------------------------------------------


def start_cycle(cycle_kind: str, operator_intent: str | None = None) -> int:
    """Insert a new scan_cycle row and return its id, or -1 on DB failure."""
    sess = _session()
    try:
        row = ScanCycle(
            started_at=_now_iso(),
            cycle_kind=cycle_kind,
            post_status="pending",
            operator_intent=operator_intent,
        )
        sess.add(row)
        sess.commit()
        return row.id
    except Exception as e:
        logger.warning("scan_cycle.start_cycle audit write failed: %s", e)
        try:
            sess.rollback()
        except Exception:
            pass
        return -1
    finally:
        sess.remove()


def heartbeat(
    cycle_id: int,
    stage: str,
    status: str,
    detail: str | None = None,
) -> None:
    """Insert one heartbeat row. Silently no-op on cycle_id=-1 or DB failure."""
    if cycle_id is None or cycle_id < 0:
        return

    sess = _session()
    try:
        row = CycleHeartbeat(
            cycle_id=cycle_id,
            stage=stage,
            ts=_now_iso(),
            status=status,
            detail=detail,
        )
        sess.add(row)
        sess.commit()
    except Exception as e:
        logger.warning(
            "scan_cycle.heartbeat audit write failed (cycle=%s stage=%s): %s",
            cycle_id, stage, e,
        )
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        sess.remove()


def complete_cycle(
    cycle_id: int,
    post_status: str,
    screener_buy: list[str] | None = None,
    screener_sell: list[str] | None = None,
    engine_response: dict | None = None,
    error_payload: dict | None = None,
    effective_mode: str | None = None,
    operator_intent: str | None = None,
) -> None:
    """Finalise the cycle row. No-op on cycle_id=-1 or DB failure."""
    if cycle_id is None or cycle_id < 0:
        return

    sess = _session()
    try:
        row = sess.query(ScanCycle).filter_by(id=cycle_id).first()
        if row is None:
            logger.warning(
                "scan_cycle.complete_cycle: cycle_id=%s not found", cycle_id
            )
            return

        row.completed_at = _now_iso()
        row.post_status = post_status
        if screener_buy is not None:
            row.screener_buy = _json_or_none(screener_buy)
        if screener_sell is not None:
            row.screener_sell = _json_or_none(screener_sell)
        if engine_response is not None:
            row.engine_response = _json_or_none(engine_response)
        if error_payload is not None:
            row.error_payload = _json_or_none(error_payload)
        if effective_mode is not None:
            row.effective_mode = effective_mode
        if operator_intent is not None:
            row.operator_intent = operator_intent

        sess.commit()
    except Exception as e:
        logger.warning(
            "scan_cycle.complete_cycle audit write failed (cycle=%s): %s",
            cycle_id, e,
        )
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        sess.remove()


# ---------------------------------------------------------------------------
# Read path — used by preflight / status views. Raises on DB failure because
# read failures are bugs, not audit drops.
# ---------------------------------------------------------------------------


def get_recent_cycles(hours: int = 24) -> list[dict]:
    """Return all cycle rows started within the last ``hours``, newest first."""
    import datetime as dt

    import pytz

    cutoff = (
        dt.datetime.now(pytz.timezone("Asia/Kolkata"))
        - dt.timedelta(hours=hours)
    ).isoformat()

    sess = _session()
    try:
        rows = (
            sess.query(ScanCycle)
            .filter(ScanCycle.started_at >= cutoff)
            .order_by(ScanCycle.started_at.desc())
            .all()
        )
        return [_cycle_to_dict(r) for r in rows]
    finally:
        sess.remove()


def get_cycle_heartbeats(cycle_id: int) -> list[dict]:
    """Return heartbeats for one cycle, oldest first."""
    sess = _session()
    try:
        rows = (
            sess.query(CycleHeartbeat)
            .filter_by(cycle_id=cycle_id)
            .order_by(CycleHeartbeat.ts.asc(), CycleHeartbeat.id.asc())
            .all()
        )
        return [_heartbeat_to_dict(r) for r in rows]
    finally:
        sess.remove()


def cycles_since(iso_ts: str) -> int:
    """Count cycles started at or after ``iso_ts`` — for preflight staleness."""
    sess = _session()
    try:
        return (
            sess.query(ScanCycle)
            .filter(ScanCycle.started_at >= iso_ts)
            .count()
        )
    finally:
        sess.remove()


def preflight_heartbeats_since(iso_ts: str) -> int:
    """Count ``cycle_heartbeat`` rows where stage='preflight' and ts >= iso_ts.

    Used by the preflight freshness gate as a fallback liveness signal: an
    empty-screener cycle never POSTs to the engine webhook and so never
    writes a scan_cycle row, but preflight still fires every cycle. If
    recent preflight heartbeats exist, the scheduler is alive — abort
    would only mask a quiet market.

    Fail-safe to 0 on DB error — preflight must never block because we
    can't observe heartbeat state.
    """
    sess = _session()
    try:
        return (
            sess.query(CycleHeartbeat)
            .filter(CycleHeartbeat.stage == "preflight")
            .filter(CycleHeartbeat.ts >= iso_ts)
            .count()
        )
    except Exception as e:
        logger.warning("scan_cycle.preflight_heartbeats_since failed: %s", e)
        return 0
    finally:
        try:
            sess.remove()
        except Exception:
            pass
