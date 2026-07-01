"""Per-strategy LLM control table — the single persistent operator knob for the
Stage-1 LLM reviewer (issue #266 Phase 2).

One row per ``strategy_name`` records HOW the LLM participates in that
strategy's order path: ``llm_mode`` ∈ {``off``, ``veto``, ``delegate``}.

* ``off``      — no reviewer is called; orders proceed unreviewed.
* ``veto``     — the reviewer runs and a ``skip`` verdict *blocks* the order
                 (maps to the internal ``active`` enforcement mode).
* ``delegate`` — reserved for the future "LLM decides buy/sell" path. Stored,
                 but the resolver treats it as ``veto``/``active`` for now
                 (the LLM-decides engine path does not exist yet).

This axis is intentionally **decoupled** from the trading-mode axis
(``strategy_mode``: live/sandbox). The two knobs answer different questions —
``strategy_mode`` = "route orders to broker or sandbox?"; ``strategy_llm_config``
= "should the LLM review/gate those orders?". Keeping them in separate tables
avoids overloading ``strategy_mode``'s CHECK constraint and lets the operator
reason about each independently.

There is **no** ``shadow`` value here — shadow (observe-only) remains an
env-only internal option (``VETO_LAYER_MODE=shadow``) and is not
operator-selectable from the UI.

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

VALID_LLM_MODES = ("off", "veto", "delegate")
DEFAULT_LLM_MODE = "off"


class StrategyLLMConfig(Base):
    """One row per strategy: the persistent {llm_mode} control record."""

    __tablename__ = "strategy_llm_config"

    strategy_name = Column(String(64), primary_key=True, nullable=False)
    llm_mode = Column(String(16), nullable=False, default=DEFAULT_LLM_MODE)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_by = Column(String(64), nullable=False)
    notes = Column(String(512), nullable=True)

    __table_args__ = (
        CheckConstraint("llm_mode IN ('off','veto','delegate')", name="ck_slc_llm_mode"),
    )


def init_db():
    """Create the strategy_llm_config table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("strategy_llm_config table ready")
    except Exception as e:
        logger.exception(f"Failed to init strategy_llm_config table: {e}")


# Explicit alias for callers that prefer the long name (e.g. boot wiring).
init_strategy_llm_config_db = init_db


def _row_to_dict(row: StrategyLLMConfig) -> dict:
    return {
        "strategy_name": row.strategy_name,
        "llm_mode": row.llm_mode,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": row.updated_by,
        "notes": row.notes,
    }


def get_llm_mode(strategy_name: str) -> dict | None:
    """Return the persistent LLM-config row for ``strategy_name`` or None.

    Returns None when no row exists — the resolver in
    ``services.signal_review_service`` is responsible for the env/default
    fall-through.
    """
    try:
        row = db_session.query(StrategyLLMConfig).filter_by(strategy_name=strategy_name).first()
        return _row_to_dict(row) if row else None
    finally:
        db_session.remove()


def _set_llm_mode_unchecked(
    strategy_name: str,
    llm_mode: str,
    updated_by: str = "operator",
    notes: str | None = None,
) -> dict:
    """UNCHECKED — bypasses the guarded-writer audit/event. Upsert the row.

    The ONLY sanctioned caller is
    ``services.strategy_llm_config_service.flip_llm_mode`` (the guarded path).
    Do NOT call this from tests/harness/app code — use ``flip_llm_mode``, which
    validates, audits, and publishes ``StrategyLLMModeChangedEvent`` before the
    row is written. Calling this directly bypasses that trail.

    Returns the row dict. Validation errors raise ``ValueError``.
    """
    if llm_mode not in VALID_LLM_MODES:
        raise ValueError(f"llm_mode must be one of {VALID_LLM_MODES}, got {llm_mode!r}")
    if not strategy_name:
        raise ValueError("strategy_name is required")
    if not updated_by:
        raise ValueError("updated_by is required")

    try:
        existing = (
            db_session.query(StrategyLLMConfig).filter_by(strategy_name=strategy_name).first()
        )
        if existing:
            existing.llm_mode = llm_mode
            existing.updated_at = datetime.utcnow()
            existing.updated_by = updated_by
            existing.notes = notes
            row = existing
        else:
            row = StrategyLLMConfig(
                strategy_name=strategy_name,
                llm_mode=llm_mode,
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


def delete_llm_mode(strategy_name: str) -> bool:
    """Delete one strategy's LLM-config row → instant env/default fall-through.

    Returns True if a row was removed. Used by rollback and tests.
    """
    try:
        deleted = (
            db_session.query(StrategyLLMConfig).filter_by(strategy_name=strategy_name).delete()
        )
        db_session.commit()
        return bool(deleted)
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def list_llm_modes() -> list[dict]:
    """All persistent LLM-config rows, ordered by strategy_name."""
    try:
        return [
            _row_to_dict(r)
            for r in db_session.query(StrategyLLMConfig)
            .order_by(StrategyLLMConfig.strategy_name)
            .all()
        ]
    finally:
        db_session.remove()
