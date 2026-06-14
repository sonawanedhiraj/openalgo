"""Persistence for the futures_follow_cap50 strategy trade journal.

A self-contained, additive table in the main database (``openalgo.db``). It does
NOT modify any existing model or table — it adds ``futures_follow_trades`` so every
NIFTY-futures entry/exit the strategy makes is attributable to its ``strategy_id``.
Used in all three modes (scaffold/sandbox/live) so the schema is exercised
end-to-end before any real order flows.

Mirrors ``database/sector_follow_db.py`` exactly, with futures-specific columns
(``nifty_symbol``, ``lots``, ``entry_price``, ``exit_price``, ``gross_pnl``,
``charges_inr``, ``net_pnl``). One row per order leg (entry or exit) — an entry
row is written at 15:20 (status ``placed``/``rejected``/``exception``/``scaffold``),
its matching exit row at the T+1 15:25 square-off with realized P&L stamped.

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


class FuturesFollowTrade(Base):
    """One row per strategy order leg (entry or exit) across all modes."""

    __tablename__ = "futures_follow_trades"

    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, nullable=True)  # FK -> strategies.id (nullable until seeded)
    mode = Column(String(10), nullable=False)  # scaffold | sandbox | live
    side = Column(String(4), nullable=False)  # BUY | SELL
    nifty_symbol = Column(String(50), nullable=False)  # resolved NIFTY future contract symbol
    exchange = Column(String(10), nullable=False, default="NFO")
    product = Column(String(10), nullable=False, default="NRML")
    lots = Column(Integer, nullable=False)  # number of NIFTY future lots
    quantity = Column(Integer, nullable=False)  # lots * lot_size (units)
    entry_price = Column(Float, nullable=True)  # reference price at the BUY decision
    exit_price = Column(Float, nullable=True)  # fill/reference price at the SELL leg
    # For an entry row, entry_date == the entry session; for an exit row it carries
    # the original entry session (so an exit reconciles to its T+1 entry).
    entry_date = Column(String(10), nullable=False)  # YYYY-MM-DD of the entry session
    signal_id = Column(String(64), nullable=True)  # opaque id of the sector_follow signal
    vol_ratio = Column(Float, nullable=True)  # tiebreaker volume ratio of the source signal
    margin_inr = Column(Float, nullable=True)  # estimated overnight SPAN margin for the lot(s)
    gross_pnl = Column(Float, nullable=True)  # (exit-entry)*qty on the exit leg
    charges_inr = Column(Float, nullable=True)  # modelled round-trip charges on the exit leg
    net_pnl = Column(Float, nullable=True)  # gross_pnl - charges_inr on the exit leg
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


def init_db():
    """Create the futures_follow_trades table if it does not exist (idempotent)."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("futures_follow_trades table ready")
    except Exception as e:
        logger.exception(f"Failed to init futures_follow_trades table: {e}")


def record_trade(
    *,
    strategy_id,
    mode,
    side,
    nifty_symbol,
    lots,
    quantity,
    entry_date,
    exchange="NFO",
    product="NRML",
    entry_price=None,
    exit_price=None,
    signal_id=None,
    vol_ratio=None,
    margin_inr=None,
    gross_pnl=None,
    charges_inr=None,
    net_pnl=None,
    order_id=None,
    status="placed",
    error_message=None,
    note=None,
):
    """Insert one trade-journal row. Returns the row id, or None on failure."""
    try:
        row = FuturesFollowTrade(
            strategy_id=strategy_id,
            mode=mode,
            side=side,
            nifty_symbol=nifty_symbol,
            exchange=exchange,
            product=product,
            lots=lots,
            quantity=quantity,
            entry_price=entry_price,
            exit_price=exit_price,
            entry_date=entry_date,
            signal_id=signal_id,
            vol_ratio=vol_ratio,
            margin_inr=margin_inr,
            gross_pnl=gross_pnl,
            charges_inr=charges_inr,
            net_pnl=net_pnl,
            order_id=order_id,
            status=status,
            error_message=error_message,
            note=note,
        )
        db_session.add(row)
        db_session.commit()
        return row.id
    except Exception as e:
        logger.exception(f"Failed to record futures_follow trade: {e}")
        db_session.rollback()
        return None
    finally:
        db_session.remove()


def get_open_entries(strategy_id, entry_date):
    """Return BUY rows recorded on ``entry_date`` for the strategy (for T+1 exit)."""
    try:
        return FuturesFollowTrade.query.filter_by(
            strategy_id=strategy_id, side="BUY", entry_date=entry_date
        ).all()
    except Exception as e:
        logger.exception(f"Failed to query open futures_follow entries: {e}")
        return []
    finally:
        db_session.remove()
