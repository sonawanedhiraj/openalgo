"""Persistence for the MVP backtester.

Two tables, kept deliberately separate from ``trade_journal``:

* ``backtest_runs`` — one row per ``run_backtest`` invocation. Carries the
  config the run was launched with so historical runs remain interpretable
  even after live config drifts.
* ``backtest_trades`` — one row per simulated round-trip, linked to its
  parent run via ``run_id``. Mirrors the same fields the live engine writes
  to ``trade_journal`` (entry, exit, SL/target, ATR at entry) so backtest
  results can be eyeballed against live trades side-by-side.

Lives in the main ``openalgo.db`` next to the other Stage-2 audit tables.
``trade_journal`` is the live ledger and MUST NOT be touched by the
simulator — these tables exist precisely so backtests run in parallel
without polluting the production journal.
"""

import os
from datetime import datetime
from typing import Any

import pytz
from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
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


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    started_at = Column(String(40), nullable=False)
    completed_at = Column(String(40), nullable=True)

    strategy_name = Column(String(64), nullable=False)
    rule_names = Column(Text, nullable=False)  # JSON array
    symbols = Column(Text, nullable=False)  # JSON array
    from_date = Column(String(16), nullable=False)  # 'YYYY-MM-DD'
    to_date = Column(String(16), nullable=False)
    interval = Column(String(8), nullable=False)
    config = Column(Text, nullable=False)  # JSON

    status = Column(String(16), nullable=False)  # 'running' | 'completed' | 'error'
    error_message = Column(Text, nullable=True)

    # Summary metrics — populated by finalize_run.
    total_trades = Column(Integer, nullable=True)
    winners = Column(Integer, nullable=True)
    losers = Column(Integer, nullable=True)
    gross_pnl = Column(Float, nullable=True)
    win_rate = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_backtest_runs_started", "started_at"),
    )


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("backtest_runs.id"), nullable=False)

    symbol = Column(String(32), nullable=False)
    direction = Column(String(8), nullable=False)  # 'LONG' | 'SHORT'

    entry_at = Column(String(40), nullable=False)
    entry_price = Column(Float, nullable=False)
    entry_reason = Column(String(64), nullable=False)  # rule name that fired

    exit_at = Column(String(40), nullable=True)
    exit_price = Column(Float, nullable=True)
    # 'stop_loss' | 'target' | 'eod_squareoff'
    exit_reason = Column(String(32), nullable=True)

    quantity = Column(Integer, nullable=False)
    pnl = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    hold_duration_seconds = Column(Integer, nullable=True)

    atr_at_entry = Column(Float, nullable=True)
    sl_price = Column(Float, nullable=True)
    # Optional — a trailing-stop strategy may not pre-compute a fixed target.
    target_price = Column(Float, nullable=True)

    __table_args__ = (
        Index("idx_backtest_trades_run", "run_id"),
        Index("idx_backtest_trades_symbol", "symbol"),
    )


def init_db():
    """Create backtest_runs + backtest_trades tables if missing. Idempotent."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Backtest DB", logger)


def _now_iso() -> str:
    return datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()


def _run_to_dict(row: BacktestRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "strategy_name": row.strategy_name,
        "rule_names": row.rule_names,
        "symbols": row.symbols,
        "from_date": row.from_date,
        "to_date": row.to_date,
        "interval": row.interval,
        "config": row.config,
        "status": row.status,
        "error_message": row.error_message,
        "total_trades": row.total_trades,
        "winners": row.winners,
        "losers": row.losers,
        "gross_pnl": row.gross_pnl,
        "win_rate": row.win_rate,
        "max_drawdown": row.max_drawdown,
        "notes": row.notes,
    }


def _trade_to_dict(row: BacktestTrade) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "symbol": row.symbol,
        "direction": row.direction,
        "entry_at": row.entry_at,
        "entry_price": row.entry_price,
        "entry_reason": row.entry_reason,
        "exit_at": row.exit_at,
        "exit_price": row.exit_price,
        "exit_reason": row.exit_reason,
        "quantity": row.quantity,
        "pnl": row.pnl,
        "pnl_pct": row.pnl_pct,
        "hold_duration_seconds": row.hold_duration_seconds,
        "atr_at_entry": row.atr_at_entry,
        "sl_price": row.sl_price,
        "target_price": row.target_price,
    }
