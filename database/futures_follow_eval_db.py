"""Persistence for the futures_follow_cap50 15:20 entry-evaluation breakdown
(``futures_follow_eval_snapshots``), issue #352.

Additive table in the main database (``db/openalgo.db``) recording, once per
trading day, the per-symbol decision inputs the sector_follow_cap5_vol evaluator
computed at the 15:20 IST entry evaluation — sector_ret / stock_ret / vol_ratio /
intraday_source / gate outcome for every universe stock, plus batch-level
observability (intraday-source counts, cap-skips, LLM vetoes). This is what lets
the futures_follow_cap50 strategy page answer "why zero signals today" without
reading logs.

Read-only on every other module — this file owns only its own table. Mirrors the
small-table pattern in ``database/data_health_db.py``. Written by
``services/futures_follow_service.py`` (``FuturesFollowService.run_entry``), AFTER
placement decisions, wrapped fail-graceful — a persist failure must never affect
entry placement.
"""

import json
import os
from datetime import date, datetime

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")

# Upper bound on a single ``list_snapshots`` page (issue #395). ~9 KB payload per
# day, so 90 days is the most one request should ever load off disk.
MAX_HISTORY_LIMIT = 90

if DATABASE_URL and "sqlite" in DATABASE_URL:
    engine = create_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL, pool_size=10, max_overflow=20, pool_timeout=10)

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class FuturesFollowEvalSnapshot(Base):
    """One row per (strategy_name, eval_date) — idempotent overwrite on re-run."""

    __tablename__ = "futures_follow_eval_snapshots"
    __table_args__ = (
        UniqueConstraint("strategy_name", "eval_date", name="uq_futures_follow_eval_snapshot"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_name = Column(String(64), nullable=False)
    eval_date = Column(String(10), nullable=False)  # YYYY-MM-DD (IST trading date)
    eval_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    payload_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


def init_db():
    """Create the ``futures_follow_eval_snapshots`` table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("futures_follow_eval_snapshots table ready")
    except Exception:
        logger.exception("Failed to init futures_follow_eval_snapshots table")


def _row_to_dict(row: FuturesFollowEvalSnapshot) -> dict:
    return {
        "id": row.id,
        "strategy_name": row.strategy_name,
        "eval_date": row.eval_date,
        "eval_at": row.eval_at.isoformat() if row.eval_at else None,
        "payload": json.loads(row.payload_json) if row.payload_json else {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def upsert_snapshot(
    strategy_name: str,
    eval_date: str | date,
    payload: dict,
    eval_at: datetime | None = None,
) -> bool:
    """Idempotent upsert of the (strategy_name, eval_date) breakdown snapshot.

    A re-run for the same day overwrites the row (last-write-wins — the 15:20
    job runs once/day, but a manual replay or a scheduler retry must not create
    duplicate rows). Returns ``True`` on success, ``False`` on any failure (never
    raises — the caller wraps this fail-graceful too, but this function is safe
    to call directly).
    """
    eval_date_str = eval_date.isoformat() if isinstance(eval_date, date) else str(eval_date)
    try:
        row = (
            db_session.query(FuturesFollowEvalSnapshot)
            .filter_by(strategy_name=strategy_name, eval_date=eval_date_str)
            .first()
        )
        now = datetime.utcnow()
        payload_json = json.dumps(payload, default=str)
        if row is None:
            row = FuturesFollowEvalSnapshot(
                strategy_name=strategy_name,
                eval_date=eval_date_str,
                eval_at=eval_at or now,
                payload_json=payload_json,
                created_at=now,
            )
            db_session.add(row)
        else:
            row.eval_at = eval_at or now
            row.payload_json = payload_json
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception(
            "failed to upsert futures_follow_eval_snapshots row (strategy=%s date=%s)",
            strategy_name,
            eval_date_str,
        )
        return False
    finally:
        db_session.remove()


def list_snapshots(
    strategy_name: str,
    limit: int = 30,
    before: str | date | None = None,
) -> list[dict]:
    """Snapshots for ``strategy_name``, newest ``eval_date`` first (issue #395).

    ``before`` (exclusive) pages backwards through the history: pass the oldest
    ``eval_date`` already rendered to fetch the next page. ``limit`` is clamped to
    ``[1, MAX_HISTORY_LIMIT]``.

    Returns the same dicts as ``get_snapshot`` — each carries the full payload.
    Callers that only need the per-day summary should run each payload through
    ``services.futures_follow_eval_view.summarize_payload`` rather than shipping
    ~9 KB/day to the browser.
    """
    limit = max(1, min(int(limit), MAX_HISTORY_LIMIT))
    try:
        query = db_session.query(FuturesFollowEvalSnapshot).filter_by(strategy_name=strategy_name)
        if before is not None:
            before_str = before.isoformat() if isinstance(before, date) else str(before)
            query = query.filter(FuturesFollowEvalSnapshot.eval_date < before_str)
        rows = query.order_by(FuturesFollowEvalSnapshot.eval_date.desc()).limit(limit).all()
        return [_row_to_dict(r) for r in rows]
    finally:
        db_session.remove()


def get_snapshot(strategy_name: str, eval_date: str | date) -> dict | None:
    """The breakdown snapshot for ``(strategy_name, eval_date)``, or ``None``."""
    eval_date_str = eval_date.isoformat() if isinstance(eval_date, date) else str(eval_date)
    try:
        row = (
            db_session.query(FuturesFollowEvalSnapshot)
            .filter_by(strategy_name=strategy_name, eval_date=eval_date_str)
            .first()
        )
        return _row_to_dict(row) if row else None
    finally:
        db_session.remove()
