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


class Open15DayLog(Base):
    """One row per trading day: the full decision log as a JSON blob.

    Written at 09:30 flatten and 09:35 summary (idempotent upsert by date) so
    the UI can show past days' decision timelines after restarts.
    """

    __tablename__ = "open15_day_logs"

    id = Column(Integer, primary_key=True)
    trade_date = Column(String(10), unique=True, index=True)
    log_json = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Open15Config(Base):
    """Single-row (id=1) UI-editable strategy config (issue #425 follow-up).

    NULL field = fall through to the OPEN15_* env default. Changes apply at the
    next 09:10 arm (the service snapshots an effective day-config then).
    """

    __tablename__ = "open15_config"

    id = Column(Integer, primary_key=True)
    margin_per_slot = Column(Float, nullable=True)  # Rs capital per trade slot
    sizing_mode = Column(String(16), nullable=True)  # fixed | compound
    vol_mult = Column(Float, nullable=True)  # volume-surge multiplier
    updated_by = Column(String(64), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    """Create the tables if missing. Idempotent; never touches other tables."""
    Base.metadata.create_all(bind=engine)
    logger.info("open15_trades + open15_day_logs + open15_config tables ready")


def get_config() -> dict | None:
    """Return the UI config row as a dict (None if never saved)."""
    try:
        row = db_session.query(Open15Config).filter(Open15Config.id == 1).first()
        if row is None:
            return None
        return {
            "margin_per_slot": row.margin_per_slot,
            "sizing_mode": row.sizing_mode,
            "vol_mult": row.vol_mult,
            "updated_by": row.updated_by,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    except Exception:
        logger.exception("open15: config read failed")
        return None
    finally:
        db_session.remove()


def save_config(
    margin_per_slot: float | None,
    sizing_mode: str | None,
    vol_mult: float | None,
    updated_by: str = "ui",
) -> bool:
    """Upsert the single config row. Fail-graceful."""
    try:
        row = db_session.query(Open15Config).filter(Open15Config.id == 1).first()
        if row is None:
            row = Open15Config(id=1)
            db_session.add(row)
        row.margin_per_slot = margin_per_slot
        row.sizing_mode = sizing_mode
        row.vol_mult = vol_mult
        row.updated_by = updated_by
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("open15: config save failed")
        return False
    finally:
        db_session.remove()


def total_realized_pnl() -> float:
    """Sum of research P&L across closed trades (drives compound sizing)."""
    try:
        from sqlalchemy import func

        val = (
            db_session.query(func.sum(Open15Trade.pnl)).filter(Open15Trade.pnl.isnot(None)).scalar()
        )
        return float(val or 0.0)
    except Exception:
        logger.exception("open15: realized-pnl sum failed")
        return 0.0
    finally:
        db_session.remove()


def save_day_log(trade_date: str, events: list) -> bool:
    """Upsert the day's decision log (JSON). Fail-graceful."""
    import json

    try:
        row = db_session.query(Open15DayLog).filter(Open15DayLog.trade_date == trade_date).first()
        if row is None:
            row = Open15DayLog(trade_date=trade_date)
            db_session.add(row)
        row.log_json = json.dumps(events)
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("open15: day-log save failed for %s", trade_date)
        return False
    finally:
        db_session.remove()


def get_day_log(trade_date: str) -> list | None:
    """Return the persisted decision log for a date, or None."""
    import json

    try:
        row = db_session.query(Open15DayLog).filter(Open15DayLog.trade_date == trade_date).first()
        return json.loads(row.log_json) if row and row.log_json else None
    except Exception:
        logger.exception("open15: day-log read failed for %s", trade_date)
        return None
    finally:
        db_session.remove()


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
