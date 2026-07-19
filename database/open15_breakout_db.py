"""Persistence for the open15_vol_breakout strategy (issue #425).

A self-contained, additive table in the main database (``openalgo.db``): one row
per attempted/entered trade. Beyond order bookkeeping the row carries the
RESEARCH fields this deployment exists to measure (Round 58 salvage question):
``level`` (the first-candle breakout level), ``trigger_second`` /
``trigger_price`` (the legal mid-bar entry moment), and
``entry_minute_close`` (stamped when the entry minute completes) — together they
measure how much of the ~0.54% intra-bar burst a real-time entry captures.

Read-only on every other module — this file only owns its own table.
"""

import os
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
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


class Open15Trade(Base):
    """One row per open15_vol_breakout trade attempt (entry + its exit)."""

    __tablename__ = "open15_trades"

    id = Column(Integer, primary_key=True)
    trade_date = Column(String(10), index=True)  # YYYY-MM-DD (IST)
    symbol = Column(String(32), index=True)
    side = Column(String(1))  # L / S
    mode = Column(String(16))  # sandbox / observe

    # research fields (the measurement)
    gap_pct = Column(Float)  # 09:15 open vs prev daily close
    level = Column(Float)  # first-candle high (L) / low (S)
    baseline_vol = Column(Float)  # running-avg full-minute volume at trigger
    cum_vol_at_trigger = Column(Float)  # volume accumulated within the entry minute
    trigger_minute = Column(String(5))  # "09:21"
    trigger_second = Column(Integer)  # seconds into the minute (0-59)
    trigger_price = Column(Float)  # ltp at the legal trigger moment
    entry_minute_close = Column(Float, nullable=True)  # stamped at minute end

    # order bookkeeping
    quantity = Column(Integer)
    entry_order_id = Column(String(64), nullable=True)
    entry_status = Column(String(16), nullable=True)
    exit_ts = Column(String(32), nullable=True)
    exit_price = Column(Float, nullable=True)  # last tick at flatten (research)
    exit_order_id = Column(String(64), nullable=True)
    exit_status = Column(String(16), nullable=True)

    pnl = Column(Float, nullable=True)  # research pnl: trigger_price -> exit_price
    status = Column(String(16), default="open")  # open / closed / error / observe
    reason = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    """Create the table if missing. Idempotent; never touches other tables."""
    Base.metadata.create_all(bind=engine)
    logger.info("open15_trades table ready")


def insert_trade(**kw) -> int | None:
    """Insert a trade row; returns id (None on failure — fail-graceful)."""
    try:
        row = Open15Trade(**kw)
        db_session.add(row)
        db_session.commit()
        return row.id
    except Exception:
        db_session.rollback()
        logger.exception("open15: journal insert failed")
        return None
    finally:
        db_session.remove()


def update_trade(row_id: int, **kw) -> bool:
    """Update fields on a trade row by id. Fail-graceful."""
    try:
        row = db_session.query(Open15Trade).filter(Open15Trade.id == row_id).first()
        if row is None:
            return False
        for k, v in kw.items():
            setattr(row, k, v)
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("open15: journal update failed for id=%s", row_id)
        return False
    finally:
        db_session.remove()
