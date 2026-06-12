"""Ephemeral, self-expiring safety-guard table — the SYSTEM's emergency brake.

Distinct from the operator's persistent ``strategy_mode`` knob. Rows here are
written ONLY by automated safety guards or operator emergency actions, and they
**auto-clear after ``expires_at``**:

  * data-health auto-pause (stale feed) — expires after tomorrow's entry window.
  * sector_follow daily kill-switch (daily loss limit) — expires end of day.
  * sector_follow ``/api/pause`` emergency override — expires end of day.

There is intentionally NO operator daily prompt, NO Telegram path, and NO mode
axis here. ``mode`` (live/sandbox) lives in ``strategy_mode``; this table only
answers "should this strategy's *entries* be held right now?".

Semantics:
  * One active row per ``(strategy_name, override_type)`` — upsert.
  * ``override_type`` ∈ {``pause``, ``kill_switch``}. Both block new ENTRIES;
    neither blocks exits/EOD (a held position is riskier left unmanaged).
  * Expiry is LAZY: reads ignore rows whose ``expires_at <= now`` (a stale row
    is simply inert). ``clear_expired`` is offered for housekeeping but is not
    required for correctness.

Additive table in ``db/openalgo.db``. NullPool per the project's SQLite rule.
"""

import os
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
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

VALID_OVERRIDE_TYPES = ("pause", "kill_switch")


class StrategyRuntimeOverride(Base):
    """One active row per (strategy_name, override_type): a self-expiring hold."""

    __tablename__ = "strategy_runtime_override"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_name = Column(String(64), nullable=False, index=True)
    override_type = Column(String(16), nullable=False)
    expires_at = Column(DateTime, nullable=False)  # UTC; row is inert once passed
    reason = Column(String(256), nullable=True)
    set_by = Column(String(48), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("override_type IN ('pause','kill_switch')", name="ck_sro_override_type"),
    )


def init_db():
    """Create the strategy_runtime_override table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("strategy_runtime_override table ready")
    except Exception as e:
        logger.exception(f"Failed to init strategy_runtime_override table: {e}")


init_strategy_runtime_override_db = init_db


def _row_to_dict(row: StrategyRuntimeOverride) -> dict:
    return {
        "id": row.id,
        "strategy_name": row.strategy_name,
        "override_type": row.override_type,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "reason": row.reason,
        "set_by": row.set_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def set_override(
    strategy_name: str,
    override_type: str,
    expires_at: datetime,
    reason: str | None = None,
    set_by: str = "system",
) -> dict:
    """Upsert the active override for ``(strategy_name, override_type)``.

    ``expires_at`` is a UTC ``datetime``. Re-setting the same type replaces the
    existing row (e.g. extending a pause). Raises ``ValueError`` on bad input.
    """
    if override_type not in VALID_OVERRIDE_TYPES:
        raise ValueError(
            f"override_type must be one of {VALID_OVERRIDE_TYPES}, got {override_type!r}"
        )
    if not strategy_name:
        raise ValueError("strategy_name is required")
    if not isinstance(expires_at, datetime):
        raise ValueError("expires_at must be a datetime")
    if not set_by:
        raise ValueError("set_by is required")

    try:
        existing = (
            db_session.query(StrategyRuntimeOverride)
            .filter_by(strategy_name=strategy_name, override_type=override_type)
            .first()
        )
        if existing:
            existing.expires_at = expires_at
            existing.reason = reason
            existing.set_by = set_by
            existing.created_at = datetime.utcnow()
            row = existing
        else:
            row = StrategyRuntimeOverride(
                strategy_name=strategy_name,
                override_type=override_type,
                expires_at=expires_at,
                reason=reason,
                set_by=set_by,
                created_at=datetime.utcnow(),
            )
            db_session.add(row)
        db_session.commit()
        return _row_to_dict(row)
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def get_active_overrides(strategy_name: str, now: datetime | None = None) -> list[dict]:
    """Non-expired overrides for ``strategy_name`` (``expires_at > now``).

    ``now`` defaults to ``datetime.utcnow()``. Expired rows are ignored (lazy
    expiry) so a stale guard never blocks a strategy.
    """
    if now is None:
        now = datetime.utcnow()
    try:
        rows = (
            db_session.query(StrategyRuntimeOverride)
            .filter(
                StrategyRuntimeOverride.strategy_name == strategy_name,
                StrategyRuntimeOverride.expires_at > now,
            )
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        db_session.remove()


def is_entry_blocked(strategy_name: str, now: datetime | None = None) -> tuple[bool, dict | None]:
    """Is there an active pause/kill_switch holding this strategy's entries?

    Returns ``(blocked, override_dict_or_None)``. Fail-OPEN on any DB error —
    a runtime-override read must never crash a trading job; the worst case is a
    missed pause, which is caught by the persistent mode + other guards.
    """
    try:
        active = get_active_overrides(strategy_name, now=now)
    except Exception as e:
        logger.exception("runtime_override read failed for %s: %s", strategy_name, e)
        return False, None
    if active:
        # Earliest-expiring first is fine; any active override blocks entries.
        return True, active[0]
    return False, None


def clear_override(strategy_name: str, override_type: str | None = None) -> int:
    """Manually clear override(s) for a strategy (e.g. ``/api/resume``).

    ``override_type=None`` clears all types for the strategy. Returns the number
    of rows removed.
    """
    try:
        q = db_session.query(StrategyRuntimeOverride).filter_by(strategy_name=strategy_name)
        if override_type is not None:
            q = q.filter_by(override_type=override_type)
        removed = q.delete()
        db_session.commit()
        return int(removed)
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def clear_expired(now: datetime | None = None) -> int:
    """Delete rows whose ``expires_at <= now`` (housekeeping). Returns count.

    Not required for correctness (reads already ignore expired rows) but keeps
    the table from growing unbounded. Safe to call from a daily job.
    """
    if now is None:
        now = datetime.utcnow()
    try:
        removed = (
            db_session.query(StrategyRuntimeOverride)
            .filter(StrategyRuntimeOverride.expires_at <= now)
            .delete()
        )
        db_session.commit()
        return int(removed)
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def list_overrides(include_expired: bool = False, now: datetime | None = None) -> list[dict]:
    """All override rows, ordered by strategy then expiry. By default only
    active (non-expired) rows are returned."""
    if now is None:
        now = datetime.utcnow()
    try:
        q = db_session.query(StrategyRuntimeOverride)
        if not include_expired:
            q = q.filter(StrategyRuntimeOverride.expires_at > now)
        rows = q.order_by(
            StrategyRuntimeOverride.strategy_name, StrategyRuntimeOverride.expires_at
        ).all()
        return [_row_to_dict(r) for r in rows]
    finally:
        db_session.remove()
