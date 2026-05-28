"""Persistence for the operator's per-day trading intent.

A single row keyed by IST date records whether the operator wants the simplified
engine to trade live, run in sandbox, or skip the day entirely. The row is
created at most once per day and can be locked (``locked=1``) once trading has
started so the intent cannot be flipped mid-session.

This is the foundation of the Stage-0 operational floor: the legacy
``settings.analyze_mode`` flag and the env-level ``SIMPLIFIED_ENGINE_MODE`` are
both still load-bearing for the rest of OpenAlgo, but the resolved
``EffectiveMode`` returned by ``services.mode_service.resolve_effective_mode``
combines all three with a most-conservative-wins rule. A missing row resolves
to ``DISABLED`` — we refuse to trade with no declared intent.
"""

import os
from datetime import datetime

import pytz
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and "sqlite" in DATABASE_URL:
    engine = create_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL, pool_size=50, max_overflow=100, pool_timeout=10)

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class DailyIntent(Base):
    __tablename__ = "daily_intent"

    # IST 'YYYY-MM-DD'. One row per trading day.
    date = Column(String(10), primary_key=True)
    # 'live' | 'sandbox' | 'skip' — validated at the service layer.
    intent = Column(String(16), nullable=False)
    # 'operator' (manual flip), 'agent' (Cowork), 'auto' (scheduled fallback).
    set_by = Column(String(16), nullable=False)
    # ISO 8601 timestamp captured at write time.
    set_at = Column(String(32), nullable=False)
    # 0 = open to further changes, 1 = locked (writes refused).
    locked = Column(Integer, nullable=False, default=0)
    notes = Column(String(512), nullable=True)


def init_db():
    """Create the daily_intent table if missing. Idempotent."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Daily Intent DB", logger)


def _today_ist_str() -> str:
    return datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()


def _row_to_dict(row: DailyIntent) -> dict:
    return {
        "date": row.date,
        "intent": row.intent,
        "set_by": row.set_by,
        "set_at": row.set_at,
        "locked": bool(row.locked),
        "notes": row.notes,
    }


def get_daily_intent(date_str: str | None = None) -> dict | None:
    """Return the intent row for ``date_str`` (defaults to today IST), or None."""
    if date_str is None:
        date_str = _today_ist_str()
    try:
        row = db_session.query(DailyIntent).filter_by(date=date_str).first()
        return _row_to_dict(row) if row else None
    finally:
        db_session.remove()


def set_daily_intent(
    intent: str,
    set_by: str,
    notes: str | None = None,
    date_str: str | None = None,
    locked: bool = False,
) -> dict:
    """Upsert the daily intent row.

    Returns ``{"status": "ok", "row": {...}}`` on success or
    ``{"status": "locked", "row": {...existing...}}`` if a locked row already
    exists for that date and would be overwritten. Validation errors raise
    ``ValueError`` — these are programming errors, not operator errors.
    """
    if intent not in ("live", "sandbox", "skip"):
        raise ValueError(f"intent must be one of live|sandbox|skip, got {intent!r}")
    if not set_by:
        raise ValueError("set_by is required")
    if date_str is None:
        date_str = _today_ist_str()

    try:
        existing = db_session.query(DailyIntent).filter_by(date=date_str).first()
        if existing and existing.locked:
            return {"status": "locked", "row": _row_to_dict(existing)}

        if existing:
            existing.intent = intent
            existing.set_by = set_by
            existing.set_at = _now_iso()
            existing.notes = notes
            if locked:
                existing.locked = 1
            row = existing
        else:
            row = DailyIntent(
                date=date_str,
                intent=intent,
                set_by=set_by,
                set_at=_now_iso(),
                locked=1 if locked else 0,
                notes=notes,
            )
            db_session.add(row)

        db_session.commit()
        return {"status": "ok", "row": _row_to_dict(row)}
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()
