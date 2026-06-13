"""Persistence for Stage 2 trade journal — one row per round-trip.

The trade journal is the substrate the nightly reflection loop reads from
when it asks "what worked today, what didn't, and why?". Every engine entry
writes a row at order placement; the matching exit closes it out with P&L,
hold duration, and the broker-side fill numbers. Soft-links via
``signal_decision_id`` and ``scan_cycle_id`` let reflection join back to the
Stage-1 veto audit and the Stage-0 scan cycle that produced the candidate.

Lives in the main ``openalgo.db`` next to ``signal_decision`` and
``daily_intent`` so cross-table joins (intent → cycle → veto → trade) stay
in a single database file.
"""

import os
from datetime import datetime

import pytz
from sqlalchemy import Column, Float, Index, Integer, String, Text, create_engine
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


class TradeJournal(Base):
    __tablename__ = "trade_journal"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Identification
    placed_at = Column(String(40), nullable=False)
    symbol = Column(String(32), nullable=False)
    direction = Column(String(8), nullable=False)  # 'LONG' | 'SHORT'
    quantity = Column(Integer, nullable=False)
    strategy_name = Column(String(64), nullable=False)
    signal_source = Column(String(32), nullable=False)  # 'chartink' | 'inhouse' | 'manual'
    # Soft FK — no DB-enforced constraint, just an int we can join on. The
    # Stage 1 audit row and Stage 0 cycle row live in separate metadata trees
    # and we want to keep the trade-journal write path cheap even when the
    # upstream row is missing (e.g. the engine fired before Stage 1 was on).
    signal_decision_id = Column(Integer, nullable=True)
    scan_cycle_id = Column(Integer, nullable=True)

    # Entry details
    entry_price = Column(Float, nullable=True)
    entry_order_id = Column(String(64), nullable=True)
    entry_fill_at = Column(String(40), nullable=True)
    # LTP the engine observed at signal time (the decision price). Lets the
    # nightly loop compute realized slippage = (fill_price - ltp_at_signal) /
    # ltp_at_signal once live fills accumulate. Nullable on purpose: pre-existing
    # rows predate the column, and signal-less exits (EOD flatten) have no LTP.
    ltp_at_signal = Column(Float, nullable=True)

    # Context at entry — Stage 1.7 will fill these richer. nifty_pct + vix
    # are kept as top-level columns so the reflection loop can group/filter
    # cheaply without parsing JSON; the regime_snapshot blob carries the
    # full structured context for forensic queries.
    regime_snapshot = Column(Text, nullable=True)
    nifty_pct_at_entry = Column(Float, nullable=True)
    india_vix_at_entry = Column(Float, nullable=True)

    # Exit details
    exited_at = Column(String(40), nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_order_id = Column(String(64), nullable=True)
    # 'stop_loss' | 'target' | 'manual' | 'eod_squareoff' | 'circuit_breaker' | 'other'
    exit_reason = Column(String(32), nullable=True)

    # Outcome
    pnl = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    hold_duration_seconds = Column(Integer, nullable=True)

    # Audit
    notes = Column(Text, nullable=True)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)

    __table_args__ = (
        Index("idx_trade_journal_placed_at", "placed_at"),
        Index("idx_trade_journal_symbol", "symbol"),
        Index("idx_trade_journal_strategy", "strategy_name"),
        Index("idx_trade_journal_exit_reason", "exit_reason"),
        Index("idx_trade_journal_signal_decision", "signal_decision_id"),
    )


def init_db():
    """Create the trade_journal table if missing, then add any columns that
    post-date the original schema. Idempotent.

    ``create_all`` only creates missing tables -- it never adds columns to a
    table that already exists. New nullable columns are evolved here with a
    guarded ``ALTER TABLE ... ADD COLUMN`` (mirrors upgrade/add_feed_token.py),
    so an existing db/openalgo.db picks them up on the next boot.
    """
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Trade Journal DB", logger)
    _ensure_columns()


def _ensure_columns():
    """Add nullable columns introduced after the initial schema, if absent."""
    from sqlalchemy import inspect, text

    # ALTER TABLE ADD COLUMN clause keyed by column name. SQLite has no
    # native bool/decimal types; REAL maps to the SQLAlchemy Float column.
    pending = {"ltp_at_signal": "REAL"}
    try:
        inspector = inspect(engine)
        existing = {col["name"] for col in inspector.get_columns("trade_journal")}
    except Exception as e:
        # Table may not exist yet on a brand-new db; create_all above handles
        # that case, so a failure here is non-fatal.
        logger.debug("trade_journal column inspection skipped: %s", e)
        return

    for name, sql_type in pending.items():
        if name in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE trade_journal ADD COLUMN {name} {sql_type}"))
            logger.info("trade_journal: added column %s %s", name, sql_type)
        except Exception as e:
            logger.warning("trade_journal: failed adding column %s: %s", name, e)


def _now_iso() -> str:
    return datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()


def _row_to_dict(row: TradeJournal) -> dict:
    return {
        "id": row.id,
        "placed_at": row.placed_at,
        "symbol": row.symbol,
        "direction": row.direction,
        "quantity": row.quantity,
        "strategy_name": row.strategy_name,
        "signal_source": row.signal_source,
        "signal_decision_id": row.signal_decision_id,
        "scan_cycle_id": row.scan_cycle_id,
        "entry_price": row.entry_price,
        "entry_order_id": row.entry_order_id,
        "entry_fill_at": row.entry_fill_at,
        "ltp_at_signal": row.ltp_at_signal,
        "regime_snapshot": row.regime_snapshot,
        "nifty_pct_at_entry": row.nifty_pct_at_entry,
        "india_vix_at_entry": row.india_vix_at_entry,
        "exited_at": row.exited_at,
        "exit_price": row.exit_price,
        "exit_order_id": row.exit_order_id,
        "exit_reason": row.exit_reason,
        "pnl": row.pnl,
        "pnl_pct": row.pnl_pct,
        "hold_duration_seconds": row.hold_duration_seconds,
        "notes": row.notes,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
