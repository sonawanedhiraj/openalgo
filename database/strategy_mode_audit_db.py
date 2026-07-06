"""Audit trail for every strategy mode flip attempt (issue #162).

Why this exists
---------------
Before issue #162, the only public path to change a strategy's mode was a raw
SQL UPDATE on ``strategy_mode``. There was no record of WHO flipped, WHEN,
why the flip was attempted, or — crucially — when a flip was REFUSED. On
2026-06-26 15:20 IST the operator flipped sector_follow_cap5_vol to LIVE
via that raw path; the strategy emitted 0 orders because the data pipeline
wasn't ready; nothing in the system logged that the operator's expectation
diverged from the system's behaviour. This table is the forensic record that
would have caught that the moment it happened.

What this records
-----------------
One row per ``services.strategy_mode_service.flip_mode`` invocation —
including BLOCKED attempts. A blocked attempt is just as auditable as an
accepted one; both are written here with the full preflight snapshot so an
operator (or a future incident review) can answer "what did the system know
at flip time?".

Sandbox flips are also audited even though they always pass — the audit trail
shows the full mode history regardless.

Read-only on every other module. NullPool per the SQLite connection-pooling
rule in CLAUDE.md.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")

if DATABASE_URL and "sqlite" in DATABASE_URL:
    engine = create_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL, pool_size=10, max_overflow=20, pool_timeout=10)

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class StrategyModeAudit(Base):
    """One row per ``flip_mode`` attempt — accepted OR blocked."""

    __tablename__ = "strategy_mode_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_name = Column(String(64), nullable=False, index=True)
    target_mode = Column(String(10), nullable=False)
    previous_mode = Column(String(10), nullable=True)
    accepted = Column(Boolean, nullable=False)
    # JSON-serialised lists/dicts from PreflightResult — kept as TEXT so we
    # don't fight SQLite about typed JSON. Empty list/object serialise to
    # "[]" / "{}" rather than NULL for clean read paths.
    blockers_json = Column(Text, nullable=False, default="[]")
    warnings_json = Column(Text, nullable=False, default="[]")
    snapshot_json = Column(Text, nullable=False, default="{}")
    flipped_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    flipped_by = Column(String(64), nullable=False)
    error_message = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_sma_strategy_flipped", "strategy_name", "flipped_at"),
        Index("idx_sma_accepted", "accepted"),
    )


def init_db() -> None:
    """Create the strategy_mode_audit table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("strategy_mode_audit table ready")
    except Exception:
        logger.exception("Failed to init strategy_mode_audit table")


# Explicit alias for boot wiring callers.
init_strategy_mode_audit_db = init_db


def _row_to_dict(row: StrategyModeAudit) -> dict:
    return {
        "id": row.id,
        "strategy_name": row.strategy_name,
        "target_mode": row.target_mode,
        "previous_mode": row.previous_mode,
        "accepted": row.accepted,
        "blockers": _parse_json(row.blockers_json, default=[]),
        "warnings": _parse_json(row.warnings_json, default=[]),
        "snapshot": _parse_json(row.snapshot_json, default={}),
        "flipped_at": row.flipped_at.isoformat() if row.flipped_at else None,
        "flipped_by": row.flipped_by,
        "error_message": row.error_message,
    }


def _parse_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _json_or_empty(value: Any, fallback: str) -> str:
    """Serialise to JSON; on any failure return ``fallback`` (the empty-shape
    default) so a malformed snapshot never poisons the audit write."""
    try:
        return json.dumps(value or _PARSE_FALLBACK_OBJECT[fallback])
    except (TypeError, ValueError):
        logger.exception("strategy_mode_audit: failed to JSON-serialise %s", fallback)
        return fallback


_PARSE_FALLBACK_OBJECT = {"[]": [], "{}": {}}


def record_attempt(
    *,
    strategy_name: str,
    target_mode: str,
    previous_mode: str | None,
    accepted: bool,
    blockers: list[str] | None = None,
    warnings: list[str] | None = None,
    snapshot: dict[str, Any] | None = None,
    flipped_by: str = "unknown",
    error_message: str | None = None,
) -> dict:
    """Insert one audit row. Returns the row dict.

    Never raises — audit writes must not break the flip path. On DB failure
    the row is logged at ERROR and an empty dict is returned (caller's flip
    decision is unaffected).
    """
    if not strategy_name:
        logger.error("strategy_mode_audit.record_attempt: empty strategy_name — skipping")
        return {}

    row = StrategyModeAudit(
        strategy_name=strategy_name,
        target_mode=target_mode or "",
        previous_mode=previous_mode,
        accepted=bool(accepted),
        blockers_json=_json_or_empty(blockers, "[]"),
        warnings_json=_json_or_empty(warnings, "[]"),
        snapshot_json=_json_or_empty(snapshot, "{}"),
        flipped_at=datetime.utcnow(),
        flipped_by=flipped_by or "unknown",
        error_message=error_message,
    )

    try:
        db_session.add(row)
        db_session.commit()
        return _row_to_dict(row)
    except Exception:
        logger.exception(
            "strategy_mode_audit: insert failed for %s target=%s accepted=%s",
            strategy_name,
            target_mode,
            accepted,
        )
        try:
            db_session.rollback()
        except Exception:
            logger.exception("strategy_mode_audit: rollback failed")
        return {}
    finally:
        db_session.remove()


def list_attempts(
    strategy_name: str | None = None,
    limit: int = 50,
    accepted_only: bool = False,
) -> list[dict]:
    """Return recent audit rows, newest first.

    Args:
        strategy_name: Filter by strategy (None = all strategies).
        limit: Max rows to return.
        accepted_only: If True, exclude blocked attempts.
    """
    try:
        q = db_session.query(StrategyModeAudit)
        if strategy_name:
            q = q.filter(StrategyModeAudit.strategy_name == strategy_name)
        if accepted_only:
            q = q.filter(StrategyModeAudit.accepted.is_(True))
        rows = q.order_by(StrategyModeAudit.flipped_at.desc()).limit(max(1, int(limit))).all()
        return [_row_to_dict(r) for r in rows]
    finally:
        db_session.remove()
