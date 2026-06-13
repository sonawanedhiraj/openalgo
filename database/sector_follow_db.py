"""Persistence for the sector_follow_cap5_vol strategy trade journal.

A self-contained, additive table in the main database (``openalgo.db``). It does
NOT modify any existing model or table — it adds ``sector_follow_trades`` so every
entry/exit the strategy makes is attributable to its ``strategy_id`` (see
``strategies/sector_follow_cap5_vol/strategy_id_design.md``). Used in all three
modes (scaffold/sandbox/live) so the schema is exercised end-to-end before any
real order flows.

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


class SectorFollowTrade(Base):
    """One row per strategy order (entry or exit) across all modes."""

    __tablename__ = "sector_follow_trades"

    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, nullable=True)  # FK -> strategies.id (nullable until seeded)
    mode = Column(String(10), nullable=False)  # scaffold | sandbox | live
    side = Column(String(4), nullable=False)  # BUY | SELL
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(10), nullable=False, default="NSE")
    product = Column(String(10), nullable=False, default="CNC")
    quantity = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)  # reference price at decision time
    entry_date = Column(String(10), nullable=False)  # YYYY-MM-DD of the entry session
    vol_ratio = Column(Float, nullable=True)
    stock_ret = Column(Float, nullable=True)
    sector_ret = Column(Float, nullable=True)
    order_id = Column(String(64), nullable=True)  # broker/sandbox order id if placed
    # Order outcome so a failed/rejected attempt is never silently dropped:
    #   placed   — order accepted (orderid present or broker status=success)
    #   rejected — broker/sandbox returned an error response
    #   exception — order placement raised
    #   scaffold — scaffold mode, no order routed
    status = Column(String(12), nullable=False, default="placed")
    error_message = Column(String(255), nullable=True)  # broker/exception message on failure
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def _ensure_columns():
    """Idempotently add columns introduced after the table's first creation.

    ``Base.metadata.create_all`` only creates *new* tables — it never alters an
    existing one. ``status``/``error_message`` were added after the table shipped,
    so back-fill them on existing DBs via SQLite ``ALTER TABLE ADD COLUMN`` (a
    constant DEFAULT is allowed). No-op when the columns already exist.
    """
    from sqlalchemy import text

    wanted = {
        "status": "VARCHAR(12) NOT NULL DEFAULT 'placed'",
        "error_message": "VARCHAR(255)",
    }
    try:
        with engine.connect() as conn:
            existing = {
                row[1] for row in conn.execute(text("PRAGMA table_info(sector_follow_trades)"))
            }
            for col, ddl in wanted.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE sector_follow_trades ADD COLUMN {col} {ddl}"))
                    logger.info("sector_follow_trades: added column %s", col)
            conn.commit()
    except Exception as e:
        logger.exception(f"Failed to ensure sector_follow_trades columns: {e}")


def init_db():
    """Create the sector_follow_trades table if it does not exist (idempotent)."""
    try:
        Base.metadata.create_all(bind=engine)
        _ensure_columns()
        logger.info("sector_follow_trades table ready")
    except Exception as e:
        logger.exception(f"Failed to init sector_follow_trades table: {e}")


def record_trade(
    *,
    strategy_id,
    mode,
    side,
    symbol,
    quantity,
    price,
    entry_date,
    exchange="NSE",
    product="CNC",
    vol_ratio=None,
    stock_ret=None,
    sector_ret=None,
    order_id=None,
    status="placed",
    error_message=None,
    note=None,
):
    """Insert one trade-journal row. Returns the row id, or None on failure."""
    try:
        row = SectorFollowTrade(
            strategy_id=strategy_id,
            mode=mode,
            side=side,
            symbol=symbol,
            exchange=exchange,
            product=product,
            quantity=quantity,
            price=price,
            entry_date=entry_date,
            vol_ratio=vol_ratio,
            stock_ret=stock_ret,
            sector_ret=sector_ret,
            order_id=order_id,
            status=status,
            error_message=error_message,
            note=note,
        )
        db_session.add(row)
        db_session.commit()
        return row.id
    except Exception as e:
        logger.exception(f"Failed to record sector_follow trade: {e}")
        db_session.rollback()
        return None
    finally:
        db_session.remove()


def get_open_entries(strategy_id, entry_date):
    """Return BUY rows recorded on ``entry_date`` for the strategy (for T+1 exit)."""
    try:
        return SectorFollowTrade.query.filter_by(
            strategy_id=strategy_id, side="BUY", entry_date=entry_date
        ).all()
    except Exception as e:
        logger.exception(f"Failed to query open sector_follow entries: {e}")
        return []
    finally:
        db_session.remove()
