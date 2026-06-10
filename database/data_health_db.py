"""Persistence for daily market-data freshness checks (``data_health_check``).

Additive table in the main database (``db/openalgo.db``) recording the verdict of
each ``services.data_freshness_service.check_strategy_data_ready`` run: when it
ran, for which strategy, whether the feed was fresh, which symbols were stale, and
whether an alert was dispatched. Read-only on every other module — this file owns
only its own table. See the daily 16:30 IST job in
``services/sector_follow_service.py`` (``run_data_health_check``).
"""

import json
import os
from datetime import datetime

from sqlalchemy import (
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


class DataHealthCheck(Base):
    """One row per freshness check (strategy × run-time)."""

    __tablename__ = "data_health_check"

    id = Column(Integer, primary_key=True, autoincrement=True)
    check_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    strategy_name = Column(String(64), nullable=False)
    overall_ok = Column(Integer, nullable=False)  # 0 or 1
    stale_symbols = Column(Text, nullable=True)  # JSON array of symbols flagged stale
    details_json = Column(Text, nullable=True)  # full per-symbol details
    alert_sent = Column(Integer, default=0)


# Index on check_at for "recent checks" queries (matches the SQL spec).
Index("idx_data_health_at", DataHealthCheck.check_at)


def init_db():
    """Create the ``data_health_check`` table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("data_health_check table ready")
    except Exception as e:
        logger.exception(f"Failed to init data_health_check table: {e}")


# Explicit alias for boot wiring that prefers the long name.
init_data_health_db = init_db


def _row_to_dict(row: DataHealthCheck) -> dict:
    return {
        "id": row.id,
        "check_at": row.check_at.isoformat() if row.check_at else None,
        "strategy_name": row.strategy_name,
        "overall_ok": bool(row.overall_ok),
        "stale_symbols": json.loads(row.stale_symbols) if row.stale_symbols else [],
        "details": json.loads(row.details_json) if row.details_json else {},
        "alert_sent": bool(row.alert_sent),
    }


def insert_check(
    strategy_name: str,
    overall_ok: bool,
    stale_symbols: list[str] | None = None,
    details: dict | None = None,
    alert_sent: bool | int = 0,
    check_at: datetime | None = None,
) -> int:
    """Insert one freshness-check row. Returns the new row id (0 on failure)."""
    try:
        row = DataHealthCheck(
            check_at=check_at or datetime.utcnow(),
            strategy_name=strategy_name,
            overall_ok=1 if overall_ok else 0,
            stale_symbols=json.dumps(sorted(stale_symbols or [])),
            details_json=json.dumps(details or {}, default=str),
            alert_sent=1 if alert_sent else 0,
        )
        db_session.add(row)
        db_session.commit()
        return row.id
    except Exception:
        db_session.rollback()
        logger.exception("failed to insert data_health_check row")
        return 0
    finally:
        db_session.remove()


def get_latest_check(strategy_name: str) -> dict | None:
    """Most recent check for ``strategy_name``, or None."""
    try:
        row = (
            db_session.query(DataHealthCheck)
            .filter_by(strategy_name=strategy_name)
            .order_by(DataHealthCheck.check_at.desc(), DataHealthCheck.id.desc())
            .first()
        )
        return _row_to_dict(row) if row else None
    finally:
        db_session.remove()


def get_recent_checks(strategy_name: str, limit: int = 20) -> list[dict]:
    """Up to ``limit`` most-recent checks for ``strategy_name``, newest first."""
    try:
        rows = (
            db_session.query(DataHealthCheck)
            .filter_by(strategy_name=strategy_name)
            .order_by(DataHealthCheck.check_at.desc(), DataHealthCheck.id.desc())
            .limit(limit)
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        db_session.remove()
