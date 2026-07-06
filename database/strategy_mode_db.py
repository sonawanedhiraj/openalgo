"""Mode-only per-strategy control table — the single persistent operator knob.

One row per ``strategy_name`` records HOW that strategy routes orders:
``mode`` ∈ {``live``, ``sandbox``}. There is **no** intent axis, no daily date
key, and no capital cap here — this is the durable, operator-set control. The
default everywhere is ``sandbox``; ``live`` is an explicit operator opt-in.

This supersedes the ``strategy_daily_intent`` table's ``mode`` column (the
intent/cap axes are retired). Automated, *ephemeral* safety guards (data-health
auto-pause, daily kill-switch, the sector_follow ``/api/pause`` emergency
override) live in the separate ``strategy_runtime_override`` table — they are
system-set and self-expiring, never an operator daily input.

Additive table in the main database (``db/openalgo.db``). Read-only on every
other module — this file owns only its own table. NullPool per the project's
SQLite connection-pooling rule.
"""

import os
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    String,
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

VALID_MODES = ("live", "sandbox")
DEFAULT_MODE = "sandbox"


class StrategyMode(Base):
    """One row per strategy: the persistent {mode} control record."""

    __tablename__ = "strategy_mode"

    strategy_name = Column(String(64), primary_key=True, nullable=False)
    mode = Column(String(10), nullable=False, default=DEFAULT_MODE)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_by = Column(String(32), nullable=False)
    notes = Column(String(512), nullable=True)

    __table_args__ = (CheckConstraint("mode IN ('live','sandbox')", name="ck_sm_mode"),)


def init_db():
    """Create the strategy_mode table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("strategy_mode table ready")
    except Exception as e:
        logger.exception(f"Failed to init strategy_mode table: {e}")


# Explicit alias for callers that prefer the long name (e.g. boot wiring).
init_strategy_mode_db = init_db


def _row_to_dict(row: StrategyMode) -> dict:
    return {
        "strategy_name": row.strategy_name,
        "mode": row.mode,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": row.updated_by,
        "notes": row.notes,
    }


def get_mode(strategy_name: str) -> dict | None:
    """Return the persistent mode row for ``strategy_name`` or None.

    Returns None when no row exists — the resolver in ``services.mode_service``
    is responsible for the env/default fall-through.
    """
    try:
        row = db_session.query(StrategyMode).filter_by(strategy_name=strategy_name).first()
        return _row_to_dict(row) if row else None
    finally:
        db_session.remove()


def _set_mode_unchecked(
    strategy_name: str,
    mode: str,
    updated_by: str = "operator",
    notes: str | None = None,
) -> dict:
    """UNCHECKED — bypasses preflight/audit. Upsert the persistent mode row.

    The ONLY sanctioned caller is ``services.strategy_mode_service.flip_mode``
    (and the one-shot migration script). Do NOT call this from tests/harness/app
    code — use ``flip_mode``, which runs the preflight gate + audit trail +
    publishes ``StrategyModeChangedEvent`` before the row is written. Calling
    this directly is how the 2026-06-24 silent ``live`` flip happened.

    Returns the row dict. Validation errors raise ``ValueError``
    (programming/operator-input errors).
    """
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
    if not strategy_name:
        raise ValueError("strategy_name is required")
    if not updated_by:
        raise ValueError("updated_by is required")

    try:
        existing = db_session.query(StrategyMode).filter_by(strategy_name=strategy_name).first()
        if existing:
            existing.mode = mode
            existing.updated_at = datetime.utcnow()
            existing.updated_by = updated_by
            existing.notes = notes
            row = existing
        else:
            row = StrategyMode(
                strategy_name=strategy_name,
                mode=mode,
                updated_at=datetime.utcnow(),
                updated_by=updated_by,
                notes=notes,
            )
            db_session.add(row)
        db_session.commit()
        return _row_to_dict(row)
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def delete_mode(strategy_name: str) -> bool:
    """Delete one strategy's mode row → instant env/default fall-through.

    Returns True if a row was removed. Used by rollback and tests.
    """
    try:
        deleted = db_session.query(StrategyMode).filter_by(strategy_name=strategy_name).delete()
        db_session.commit()
        return bool(deleted)
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def list_modes() -> list[dict]:
    """All persistent mode rows, ordered by strategy_name."""
    try:
        return [
            _row_to_dict(r)
            for r in db_session.query(StrategyMode).order_by(StrategyMode.strategy_name).all()
        ]
    finally:
        db_session.remove()
