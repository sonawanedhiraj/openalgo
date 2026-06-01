"""Persistence for Stage 2 part 2 — nightly journal reflection rows.

One row per reflection_date: an LLM-generated synthesis of what happened
that day across the three substrates the platform already keeps —

* ``trade_journal`` — live engine round-trips (what actually fired).
* ``scan_results`` — what the screener proposed (the candidate pool).
* ``backtest_trades`` — what an offline simulator says the strategy
  would have done historically.

The reflection's purpose is forensic / learning, NOT order-routing — it
runs at EOD, persists a summary + structured patterns + open questions,
and surfaces them to the operator via the existing EOD surfaces.

Lives in the main ``openalgo.db`` so cross-table joins against the same
day's ``trade_journal``, ``scan_results``, and ``backtest_trades`` stay
in a single database file.
"""

import os
from datetime import datetime
from typing import Any

import pytz
from sqlalchemy import Column, Date, Index, Integer, String, Text, UniqueConstraint, create_engine
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


class JournalReflection(Base):
    __tablename__ = "journal_reflection"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # The trading day being reflected on. UNIQUE so the nightly cron is
    # naturally idempotent — a second run on the same date replaces, doesn't
    # duplicate.
    reflection_date = Column(Date, nullable=False)
    created_at = Column(String(40), nullable=False)
    # Window the inputs were pulled over. 7 today; we may widen or narrow as
    # patterns crystallise. Storing it on the row keeps reflections
    # interpretable after the default changes.
    data_window_days = Column(Integer, nullable=False)
    n_journal_trades = Column(Integer, nullable=False)
    n_screener_hits = Column(Integer, nullable=False)
    n_backtest_trades = Column(Integer, nullable=False)
    # Methodology note that gets included in the prompt; stored so an analyst
    # reading an old row knows what caveat the LLM was warned about.
    backtest_caveat = Column(Text, nullable=True)
    summary = Column(Text, nullable=False)
    patterns_json = Column(Text, nullable=False)
    questions_json = Column(Text, nullable=True)
    llm_model = Column(String(64), nullable=True)
    llm_latency_ms = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("reflection_date", name="uq_journal_reflection_date"),
        Index("idx_journal_reflection_date", "reflection_date"),
    )


def init_db():
    """Create the journal_reflection table if missing. Idempotent."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Journal Reflection DB", logger)


def _now_iso() -> str:
    return datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()


def _row_to_dict(row: JournalReflection) -> dict[str, Any]:
    return {
        "id": row.id,
        "reflection_date": row.reflection_date.isoformat()
        if row.reflection_date
        else None,
        "created_at": row.created_at,
        "data_window_days": row.data_window_days,
        "n_journal_trades": row.n_journal_trades,
        "n_screener_hits": row.n_screener_hits,
        "n_backtest_trades": row.n_backtest_trades,
        "backtest_caveat": row.backtest_caveat,
        "summary": row.summary,
        "patterns_json": row.patterns_json,
        "questions_json": row.questions_json,
        "llm_model": row.llm_model,
        "llm_latency_ms": row.llm_latency_ms,
    }
