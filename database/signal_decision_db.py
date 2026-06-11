"""Persistence for Stage-1 LLM veto-layer decisions.

Every candidate signal that reaches the simplified engine is shipped to the
local Claude Bridge for a take/skip review, and the result lands here as one
row. In shadow mode the row is recorded but the decision is not enforced; in
active mode the engine short-circuits on ``decision='skip'``. ``actually_taken``
is updated after the order placement attempt so the audit table reflects what
the operator's broker actually saw.

Lives in the main ``openalgo.db`` next to ``daily_intent`` because both are
operational-floor primitives, and queries that correlate intent with veto
outcome are easier when they share a database.
"""

import json
import os
import threading
from datetime import datetime
from typing import Any

import pytz
from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
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

_tables_ensured_for_engine = None
_tables_ensured_lock = threading.Lock()


class SignalDecision(Base):
    __tablename__ = "signal_decision"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Wall-clock ISO timestamp of when the candidate signal arrived.
    candidate_at = Column(String(40), nullable=False)
    symbol = Column(String(32), nullable=False)
    # Free-form source tag (e.g. 'chartink_buy_fno_intraday').
    source = Column(String(64), nullable=False)
    # Side the engine actually armed: 'BUY' (long) | 'SELL' (short). Nullable
    # so the legacy rows written before this column existed read back as None.
    # The ``source`` string alone can't disambiguate the two chartink legs.
    direction = Column(String(8), nullable=True)
    # 'take' | 'skip' | 'review_failed'. review_failed is recorded so we can
    # tell apart "reviewer said take" from "reviewer was down and we defaulted".
    decision = Column(String(16), nullable=False)
    reasoning = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)
    # 'shadow' | 'active' | 'off' — what the engine was configured to do at
    # the time of decision. Lets us replay the audit log per mode.
    enforcement_mode = Column(String(16), nullable=False)
    # NULL until the engine reports the order outcome. 1 if the order was
    # placed (regardless of fill), 0 if the engine vetoed in active mode.
    actually_taken = Column(Integer, nullable=True)
    # JSON-serialised context (positions, pnl, NIFTY, etc.) at decision time.
    context_snapshot = Column(Text, nullable=True)
    bridge_latency_ms = Column(Integer, nullable=True)
    bridge_session_id = Column(String(128), nullable=True)
    raw_bridge_output = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_signal_decision_candidate_at", "candidate_at"),
        Index("idx_signal_decision_symbol", "symbol"),
    )


def init_db():
    """Create the signal_decision table if missing. Idempotent."""
    global _tables_ensured_for_engine
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Signal Decision DB", logger)
    _migrate_add_direction_column()
    with _tables_ensured_lock:
        _tables_ensured_for_engine = engine


def _migrate_add_direction_column() -> None:
    """Add the ``direction`` column to a pre-existing table. Idempotent.

    ``create_all`` only creates missing tables — it never alters an existing
    one — so a DB that predates this column needs an explicit ``ALTER TABLE``.
    Guarded by an inspector check so re-running is a no-op, and wrapped in
    try/except so a benign race (two threads adding at once) can't crash boot.
    """
    try:
        inspector = inspect(engine)
        if "signal_decision" not in inspector.get_table_names():
            return  # create_all will build it fresh with the column already present
        columns = {col["name"] for col in inspector.get_columns("signal_decision")}
        if "direction" in columns:
            return
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE signal_decision ADD COLUMN direction VARCHAR(8)"))
        logger.info("signal_decision: added 'direction' column via migration")
    except Exception:
        # Most likely a concurrent ALTER that already landed (duplicate column).
        logger.debug("signal_decision: direction-column migration skipped", exc_info=True)


def _ensure_tables() -> None:
    # The veto layer can be exercised before app.py's parallel db init has
    # reached this module — the scanner thread starts emitting signals as
    # soon as the master contract loads. Guarantee the table exists before
    # the first write, so a missed/late init can't turn into a hard error.
    # The flag tracks engine identity so tests that monkeypatch ``engine``
    # to a fresh in-memory SQLite re-trigger the create_all.
    global _tables_ensured_for_engine
    if _tables_ensured_for_engine is engine:
        return
    with _tables_ensured_lock:
        if _tables_ensured_for_engine is engine:
            return
        Base.metadata.create_all(bind=engine)
        _migrate_add_direction_column()
        _tables_ensured_for_engine = engine


def _now_iso() -> str:
    return datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()


def _row_to_dict(row: SignalDecision) -> dict[str, Any]:
    return {
        "id": row.id,
        "candidate_at": row.candidate_at,
        "symbol": row.symbol,
        "source": row.source,
        "direction": row.direction,
        "decision": row.decision,
        "reasoning": row.reasoning,
        "confidence": row.confidence,
        "enforcement_mode": row.enforcement_mode,
        "actually_taken": (
            None if row.actually_taken is None else bool(row.actually_taken)
        ),
        "context_snapshot": row.context_snapshot,
        "bridge_latency_ms": row.bridge_latency_ms,
        "bridge_session_id": row.bridge_session_id,
        "raw_bridge_output": row.raw_bridge_output,
    }


def insert_signal_decision(
    *,
    symbol: str,
    source: str,
    decision: str,
    direction: str | None = None,
    reasoning: str | None,
    confidence: float | None,
    enforcement_mode: str,
    context_snapshot: dict[str, Any] | None,
    bridge_latency_ms: int | None,
    bridge_session_id: str | None,
    raw_bridge_output: str | None,
    candidate_at: str | None = None,
) -> int:
    """Insert one decision row and return its id."""
    if candidate_at is None:
        candidate_at = _now_iso()
    _ensure_tables()
    try:
        row = SignalDecision(
            candidate_at=candidate_at,
            symbol=symbol,
            source=source,
            direction=direction,
            decision=decision,
            reasoning=reasoning,
            confidence=confidence,
            enforcement_mode=enforcement_mode,
            actually_taken=None,
            context_snapshot=json.dumps(context_snapshot) if context_snapshot else None,
            bridge_latency_ms=bridge_latency_ms,
            bridge_session_id=bridge_session_id,
            raw_bridge_output=raw_bridge_output,
        )
        db_session.add(row)
        db_session.commit()
        return row.id
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def get_signal_decision(decision_id: int) -> dict[str, Any] | None:
    _ensure_tables()
    try:
        row = db_session.query(SignalDecision).filter_by(id=decision_id).first()
        return _row_to_dict(row) if row else None
    finally:
        db_session.remove()


def mark_actually_taken(decision_id: int, taken: bool) -> None:
    """Update ``actually_taken`` for the given decision row.

    Silently no-ops if the id is unknown — the caller is in the order-placement
    path and shouldn't blow up on bookkeeping errors.
    """
    _ensure_tables()
    try:
        row = db_session.query(SignalDecision).filter_by(id=decision_id).first()
        if row is None:
            return
        row.actually_taken = 1 if taken else 0
        db_session.commit()
    except Exception:
        db_session.rollback()
        logger.exception(
            "signal_decision: mark_actually_taken failed for id=%s", decision_id
        )
    finally:
        db_session.remove()
