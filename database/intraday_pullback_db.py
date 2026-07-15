"""Journal DB for the intraday_pullback_top2 combined long+short strategy (issue #394).

One row per trade (insert on entry, update on exit) with an L/S ``side`` column, entry/exit
price+time, exit reason, per-trade charges/pnl, and a gate-snapshot JSON blob. Mirrors the
``futures_follow_db`` boilerplate (NullPool sqlite engine bound to DATABASE_URL, scoped_session,
``db_session.remove()`` in every finally).
"""

import json
import os
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine
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

TABLE_NAME = "intraday_pullback_trades"


class IntradayPullbackTrade(Base):
    """A single long or short intraday trade (open -> closed within the day)."""

    __tablename__ = TABLE_NAME

    id = Column(Integer, primary_key=True)
    strategy_id = Column(Integer, nullable=True)
    mode = Column(String(10), nullable=False)  # sandbox | live
    side = Column(String(1), nullable=False)  # 'L' (long) | 'S' (short)
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(12), nullable=False, default="NSE")
    product = Column(String(6), nullable=False, default="MIS")
    sector = Column(String(40), nullable=True)
    trade_date = Column(String(10), nullable=False)  # YYYY-MM-DD (IST)
    session = Column(String(8), nullable=True)  # MORNING | AFT

    entry_time = Column(DateTime, nullable=True)
    entry_price = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    quantity = Column(Integer, nullable=False, default=0)

    exit_time = Column(DateTime, nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_reason = Column(String(12), nullable=True)  # SL | EOD

    gross_pnl = Column(Float, nullable=True)
    charges_inr = Column(Float, nullable=True)
    net_pnl = Column(Float, nullable=True)

    entry_order_id = Column(String(64), nullable=True)
    exit_order_id = Column(String(64), nullable=True)

    gate_json = Column(Text, nullable=True)  # selection/gate snapshot (json)
    status = Column(
        String(12), nullable=False, default="open"
    )  # open|closed|rejected|exception|veto_skip
    error_message = Column(String(255), nullable=True)
    note = Column(String(120), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def _row_to_dict(row: IntradayPullbackTrade) -> dict:
    return {
        "id": row.id,
        "strategy_id": row.strategy_id,
        "mode": row.mode,
        "side": row.side,
        "symbol": row.symbol,
        "exchange": row.exchange,
        "product": row.product,
        "sector": row.sector,
        "trade_date": row.trade_date,
        "session": row.session,
        "entry_time": row.entry_time.isoformat() if row.entry_time else None,
        "entry_price": row.entry_price,
        "stop_price": row.stop_price,
        "quantity": row.quantity,
        "exit_time": row.exit_time.isoformat() if row.exit_time else None,
        "exit_price": row.exit_price,
        "exit_reason": row.exit_reason,
        "gross_pnl": row.gross_pnl,
        "charges_inr": row.charges_inr,
        "net_pnl": row.net_pnl,
        "entry_order_id": row.entry_order_id,
        "exit_order_id": row.exit_order_id,
        "gate": json.loads(row.gate_json) if row.gate_json else None,
        "status": row.status,
        "error_message": row.error_message,
        "note": row.note,
    }


def _ensure_columns():
    """Idempotent additive migration (create_all never ALTERs existing tables)."""
    wanted = {
        "sector": "VARCHAR(40)",
        "session": "VARCHAR(8)",
        "stop_price": "FLOAT",
        "exit_order_id": "VARCHAR(64)",
        "gate_json": "TEXT",
        "error_message": "VARCHAR(255)",
        "note": "VARCHAR(120)",
        "updated_at": "DATETIME",
    }
    try:
        with engine.connect() as conn:
            existing = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({TABLE_NAME})")}
            for col, ddl in wanted.items():
                if col not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {TABLE_NAME} ADD COLUMN {col} {ddl}")
                    logger.info("added column %s.%s", TABLE_NAME, col)
    except Exception as e:  # noqa: BLE001
        logger.exception("intraday_pullback _ensure_columns failed: %s", e)


def init_db():
    """Create the intraday_pullback_trades table if it does not exist (idempotent)."""
    try:
        Base.metadata.create_all(bind=engine)
        _ensure_columns()
        logger.info("%s table ready", TABLE_NAME)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to init %s table: %s", TABLE_NAME, e)


init_intraday_pullback_db = init_db  # long-name alias for app.py init list


def record_entry(
    *,
    strategy_id,
    mode,
    side,
    symbol,
    trade_date,
    quantity,
    entry_time=None,
    entry_price=None,
    stop_price=None,
    exchange="NSE",
    product="MIS",
    sector=None,
    session=None,
    entry_order_id=None,
    gate=None,
    status="open",
    error_message=None,
    note=None,
):
    """Insert a trade row on entry (or a terminal ``rejected``/``exception``/``veto_skip`` row).

    Returns the new row id, or None on failure.
    """
    try:
        row = IntradayPullbackTrade(
            strategy_id=strategy_id,
            mode=mode,
            side=side,
            symbol=symbol,
            exchange=exchange,
            product=product,
            sector=sector,
            trade_date=trade_date,
            session=session,
            entry_time=entry_time,
            entry_price=entry_price,
            stop_price=stop_price,
            quantity=quantity,
            entry_order_id=entry_order_id,
            gate_json=(json.dumps(gate, default=str) if gate is not None else None),
            status=status,
            error_message=error_message,
            note=note,
        )
        db_session.add(row)
        db_session.commit()
        return row.id
    except Exception as e:  # noqa: BLE001
        logger.exception("record_entry failed for %s: %s", symbol, e)
        db_session.rollback()
        return None
    finally:
        db_session.remove()


def close_trade(
    trade_id,
    *,
    exit_time,
    exit_price,
    exit_reason,
    gross_pnl=None,
    charges_inr=None,
    net_pnl=None,
    exit_order_id=None,
    status="closed",
):
    """Update an open trade row on exit. Returns True on success."""
    try:
        row = IntradayPullbackTrade.query.filter_by(id=trade_id).first()
        if row is None:
            logger.warning("close_trade: id %s not found", trade_id)
            return False
        row.exit_time = exit_time
        row.exit_price = exit_price
        row.exit_reason = exit_reason
        row.gross_pnl = gross_pnl
        row.charges_inr = charges_inr
        row.net_pnl = net_pnl
        if exit_order_id is not None:
            row.exit_order_id = exit_order_id
        row.status = status
        db_session.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception("close_trade failed for id %s: %s", trade_id, e)
        db_session.rollback()
        return False
    finally:
        db_session.remove()


def get_open_trades(strategy_id, trade_date):
    """Open (un-exited) trades for the strategy on a date."""
    try:
        rows = (
            IntradayPullbackTrade.query.filter_by(
                strategy_id=strategy_id, trade_date=trade_date, status="open"
            )
            .order_by(IntradayPullbackTrade.entry_time)
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.exception("get_open_trades failed: %s", e)
        return []
    finally:
        db_session.remove()


def get_trades(strategy_id, *, trade_date=None, date_from=None, date_to=None, side=None, mode=None):
    """Flexible trade query for status / performance panels. All filters optional."""
    try:
        q = IntradayPullbackTrade.query.filter_by(strategy_id=strategy_id)
        if trade_date is not None:
            q = q.filter(IntradayPullbackTrade.trade_date == trade_date)
        if date_from is not None:
            q = q.filter(IntradayPullbackTrade.trade_date >= date_from)
        if date_to is not None:
            q = q.filter(IntradayPullbackTrade.trade_date <= date_to)
        if side is not None:
            q = q.filter(IntradayPullbackTrade.side == side)
        if mode is not None:
            q = q.filter(IntradayPullbackTrade.mode == mode)
        rows = q.order_by(IntradayPullbackTrade.created_at).all()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.exception("get_trades failed: %s", e)
        return []
    finally:
        db_session.remove()


def performance_by_side(strategy_id, *, date_from=None, date_to=None, mode=None):
    """Split win-rate / PF / net for L, S, and combined. Only counts closed trades with a net_pnl."""
    trades = get_trades(strategy_id, date_from=date_from, date_to=date_to, mode=mode)
    closed = [t for t in trades if t["status"] == "closed" and t["net_pnl"] is not None]

    def agg(rows):
        n = len(rows)
        wins = [r["net_pnl"] for r in rows if r["net_pnl"] >= 0]
        losses = [r["net_pnl"] for r in rows if r["net_pnl"] < 0]
        gross_w = sum(wins)
        gross_l = abs(sum(losses))
        return {
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
            "profit_factor": (
                round(gross_w / gross_l, 2) if gross_l else (99.0 if gross_w else 0.0)
            ),
            "avg_win": round(gross_w / len(wins), 0) if wins else 0.0,
            "avg_loss": round(-gross_l / len(losses), 0) if losses else 0.0,
            "net_pnl": round(sum(r["net_pnl"] for r in rows), 0),
        }

    return {
        "long": agg([t for t in closed if t["side"] == "L"]),
        "short": agg([t for t in closed if t["side"] == "S"]),
        "combined": agg(closed),
    }
