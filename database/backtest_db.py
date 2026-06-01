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

    # Free-form tag identifying which backtest harness produced the run.
    # Examples: ``"all_symbol"`` (legacy run_backtest) or
    # ``"screener_filtered"`` (run_screener_filtered_backtest). NULL on
    # pre-migration rows.
    methodology = Column(String(32), nullable=True)

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

    # Same tag as the parent run; denormalised so the reflection bridge
    # can filter trades by methodology without joining backtest_runs.
    methodology = Column(String(32), nullable=True)
    # ISO timestamp of the bar close at which the scanner rule fired and the
    # symbol was added to the day's pick list. May be earlier than ``entry_at``
    # when the strategy uses a multi-bar confirmation window.
    scanner_hit_timestamp = Column(String(40), nullable=True)

    __table_args__ = (
        Index("idx_backtest_trades_run", "run_id"),
        Index("idx_backtest_trades_symbol", "symbol"),
    )


def init_db():
    """Create backtest_runs + backtest_trades tables if missing. Idempotent.

    Also runs lightweight column migrations for backwards-compatible
    additions (``methodology`` and ``scanner_hit_timestamp``) so that
    existing deployments don't need to drop the tables when the schema
    grows. Each ALTER TABLE is gated on ``information_schema`` so the
    migration is a no-op on a fresh DB and is safe to run repeatedly.
    """
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Backtest DB", logger)
    _migrate_add_methodology_columns()


def _migrate_add_methodology_columns() -> None:
    """Idempotent ALTER TABLE migration.

    Adds columns introduced after the initial schema landed:

    * ``backtest_runs.methodology``
    * ``backtest_trades.methodology``
    * ``backtest_trades.scanner_hit_timestamp``

    Uses PRAGMA-based introspection (works for SQLite — our only deployed
    backing store for backtest_*) and silently skips on engines that don't
    expose PRAGMA. Failures are logged but never raised so a stale schema
    can never break import.
    """
    try:
        with engine.connect() as conn:
            for table, column, ddl in [
                ("backtest_runs", "methodology", "VARCHAR(32)"),
                ("backtest_trades", "methodology", "VARCHAR(32)"),
                ("backtest_trades", "scanner_hit_timestamp", "VARCHAR(40)"),
            ]:
                if _column_exists(conn, table, column):
                    continue
                try:
                    from sqlalchemy import text as _text

                    conn.execute(_text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                    if hasattr(conn, "commit"):
                        conn.commit()
                    logger.info("backtest_db: migrated %s.%s", table, column)
                except Exception as e:
                    logger.warning(
                        "backtest_db: migration ALTER %s.%s failed: %s",
                        table, column, e,
                    )
    except Exception as e:
        logger.warning("backtest_db: migration introspection failed: %s", e)


def _column_exists(conn, table: str, column: str) -> bool:
    """Return True iff ``column`` already exists on ``table`` (SQLite-aware)."""
    from sqlalchemy import text as _text

    try:
        rows = conn.execute(_text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == column for r in rows)
    except Exception:
        return False


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
        "methodology": row.methodology,
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
        "methodology": row.methodology,
        "scanner_hit_timestamp": row.scanner_hit_timestamp,
    }
