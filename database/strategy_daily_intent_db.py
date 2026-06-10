"""Unified per-strategy daily intent table — the single {mode, intent} surface.

One row per ``(strategy_name, intent_date)`` records, for that strategy on that
IST day, HOW orders route (``mode`` ∈ live/sandbox/skip) and WHETHER to act
(``intent`` ∈ run/pause/halt), plus an optional ``daily_capital_cap`` override.

This is an additive table in the main database (``db/openalgo.db``). It does not
modify any existing model. It supersedes BOTH the legacy simplified-engine
``daily_intent`` table (still readable; migrated forward at boot) and
sector_follow's in-memory pause flag. Design:
``docs/design/strategy_daily_intent.md``.

``intent_date`` is stored as a ``YYYY-MM-DD`` string (IST date), mirroring the
legacy ``daily_intent`` table, so the two are trivially comparable during
migration. Read-only on every other module — this file owns only its own table.
"""

import os
from datetime import datetime

import pytz
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
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

VALID_MODES = ("live", "sandbox", "skip")
VALID_INTENTS = ("run", "pause", "halt")


class StrategyDailyIntent(Base):
    """One row per strategy per IST day: the {mode, intent} control record."""

    __tablename__ = "strategy_daily_intent"

    strategy_name = Column(String(64), primary_key=True, nullable=False)
    intent_date = Column(String(10), primary_key=True, nullable=False)  # IST YYYY-MM-DD
    mode = Column(String(16), nullable=False)
    intent = Column(String(16), nullable=False, default="run")
    daily_capital_cap = Column(Float, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_by = Column(String(32), nullable=False)
    notes = Column(String(512), nullable=True)

    __table_args__ = (
        CheckConstraint("mode IN ('live','sandbox','skip')", name="ck_sdi_mode"),
        CheckConstraint("intent IN ('run','pause','halt')", name="ck_sdi_intent"),
    )


def init_db():
    """Create the strategy_daily_intent table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("strategy_daily_intent table ready")
    except Exception as e:
        logger.exception(f"Failed to init strategy_daily_intent table: {e}")


# Explicit alias for callers that prefer the long name (e.g. boot wiring).
init_strategy_daily_intent_db = init_db


def _today_ist_str() -> str:
    return datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")


def _row_to_dict(row: StrategyDailyIntent, source: str = "unified") -> dict:
    return {
        "strategy_name": row.strategy_name,
        "intent_date": row.intent_date,
        "mode": row.mode,
        "intent": row.intent,
        "daily_capital_cap": row.daily_capital_cap,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": row.updated_by,
        "notes": row.notes,
        "source": source,
    }


def get_intent(strategy_name: str, date=None) -> dict | None:
    """Return the unified intent row for ``(strategy_name, date)`` or None.

    ``date`` defaults to today IST. The returned dict carries ``source='unified'``
    so callers can attribute the decision. Returns None when no row exists — the
    resolver in ``services.mode_service`` is responsible for the fall-through.
    """
    if date is None:
        date = _today_ist_str()
    try:
        row = (
            db_session.query(StrategyDailyIntent)
            .filter_by(strategy_name=strategy_name, intent_date=date)
            .first()
        )
        return _row_to_dict(row) if row else None
    finally:
        db_session.remove()


def set_intent(
    strategy_name: str,
    date,
    mode: str,
    intent: str = "run",
    daily_capital_cap: float | None = None,
    updated_by: str = "operator",
    notes: str | None = None,
) -> dict:
    """Upsert one ``(strategy_name, date)`` intent row. Returns the row dict.

    Validation errors raise ``ValueError`` (programming/operator-input errors).
    """
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
    if intent not in VALID_INTENTS:
        raise ValueError(f"intent must be one of {VALID_INTENTS}, got {intent!r}")
    if not strategy_name:
        raise ValueError("strategy_name is required")
    if not updated_by:
        raise ValueError("updated_by is required")
    if date is None:
        date = _today_ist_str()

    try:
        existing = (
            db_session.query(StrategyDailyIntent)
            .filter_by(strategy_name=strategy_name, intent_date=date)
            .first()
        )
        if existing:
            existing.mode = mode
            existing.intent = intent
            existing.daily_capital_cap = daily_capital_cap
            existing.updated_at = datetime.utcnow()
            existing.updated_by = updated_by
            existing.notes = notes
            row = existing
        else:
            row = StrategyDailyIntent(
                strategy_name=strategy_name,
                intent_date=date,
                mode=mode,
                intent=intent,
                daily_capital_cap=daily_capital_cap,
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


def delete_intent(strategy_name: str, date=None) -> bool:
    """Delete one intent row. Returns True if a row was removed. Used by the
    post-deploy smoke test and by single-strategy rollback (delete → fall-through)."""
    if date is None:
        date = _today_ist_str()
    try:
        deleted = (
            db_session.query(StrategyDailyIntent)
            .filter_by(strategy_name=strategy_name, intent_date=date)
            .delete()
        )
        db_session.commit()
        return bool(deleted)
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def list_intents(date=None) -> list[dict]:
    """All rows for ``date`` (default today IST), or every row when ``date`` is
    the sentinel string ``'all'``."""
    try:
        q = db_session.query(StrategyDailyIntent)
        if date != "all":
            q = q.filter_by(intent_date=date or _today_ist_str())
        return [_row_to_dict(r) for r in q.order_by(StrategyDailyIntent.strategy_name).all()]
    finally:
        db_session.remove()


def migrate_legacy_daily_intent() -> int:
    """Backfill the legacy ``daily_intent`` table into the unified table.

    For each legacy row, upsert a ``strategy_daily_intent`` row with
    ``strategy_name='simplified_engine'``, ``mode=<legacy intent>``,
    ``intent='run'``, ``updated_by='migration'``. Idempotent: an existing
    ``(simplified_engine, date)`` row is left untouched (operator edits win over
    the backfill). Returns the number of rows newly inserted.

    The legacy intent vocabulary (live/sandbox/skip) maps 1:1 onto the unified
    ``mode`` axis, so the migration is a straight copy with ``intent='run'``.
    """
    inserted = 0
    try:
        from database.daily_intent_db import DailyIntent

        legacy_rows = db_session.query(DailyIntent).all()
        for legacy in legacy_rows:
            exists = (
                db_session.query(StrategyDailyIntent)
                .filter_by(strategy_name="simplified_engine", intent_date=legacy.date)
                .first()
            )
            if exists:
                continue
            mode = legacy.intent if legacy.intent in VALID_MODES else "skip"
            db_session.add(
                StrategyDailyIntent(
                    strategy_name="simplified_engine",
                    intent_date=legacy.date,
                    mode=mode,
                    intent="run",
                    updated_at=datetime.utcnow(),
                    updated_by="migration",
                    notes=f"migrated from legacy daily_intent (set_by={legacy.set_by})",
                )
            )
            inserted += 1
        if inserted:
            db_session.commit()
        logger.info("strategy_daily_intent migration: %d legacy row(s) backfilled", inserted)
        return inserted
    except Exception as e:
        db_session.rollback()
        logger.exception(f"Failed to migrate legacy daily_intent: {e}")
        return 0
    finally:
        db_session.remove()
