"""Persistence for the ``cas_320_expiry_straddle`` strategy (issue #740).

Four additive tables in the main database (``openalgo.db``). This module owns
ONLY these tables and touches nothing else:

* ``cas_straddle_config``   — the single-row (id=1) UI-editable config. Every
  field is nullable; NULL = fall through to the code default constant in
  ``services/cas_straddle_service.py``. There are no env seeds for this
  strategy — the UI row is the only knob (operator rule, 2026-09-19).
* ``cas_straddle_trades``   — one row per option LEG (the CE and the PE of a
  straddle are two rows sharing ``trade_date``/``underlying``/``strike``).
  Written at entry (status ``placed``/``rejected``/``error``), promoted to
  ``open`` once the fill is confirmed, and ``closed`` at exit with realized P&L.
* ``cas_straddle_polls``    — every monitor poll for every watched contract
  (spot + the ATM ±2 ladder, both sides) across the 15:14–15:41 window.
  This is the research dataset the R69 backtest reads; it is written whether
  or not the underlying is toggled to trade.
* ``cas_straddle_sessions`` — one row per ``(trade_date, underlying)``: the
  session digest (15:15 close, ATM, first IIV print, IIV path extremes,
  straddle cost at 15:20, peak combined bid, settlement) plus the effective
  config the arm ran with.

P&L convention (#552 rule): ``net_pnl_of_row`` is the ONLY place a leg's net
P&L is derived from its stored fields. Never write ``sum(net_pnl)`` against
``fill != 'real'`` rows — ``real_closed_rows`` is the row set every P&L
consumer must read.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
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

# Fill classes. ``real`` is the only bucket that is money; ``paper`` is a leg
# the broker REFUSED at placement or post-ACK (#548/#626 shape) — nothing was
# ever held, so it is priced for measurement only and never joins real P&L.
REAL_FILL = "real"
NON_REAL_FILLS = ("paper",)

# Terminal exit reasons a leg can carry.
EXIT_REASONS = (
    "target",  # combined bid reached target_mult × cost
    "hard_exit",  # the configured hard exit time
    "fallback",  # the 15:38 protected flatten found the leg still open
    "expired_worthless",  # bid 0 at the hard exit — left to cash-settle at 0
    "manual",  # operator close_all
    "error",  # exit order rejected / raised; position may still be open
)

TRADE_STATUSES = ("placed", "open", "closed", "rejected", "error")


class CasStraddleConfig(Base):
    """Single-row (id=1) UI-editable strategy config. NULL = code default."""

    __tablename__ = "cas_straddle_config"

    id = Column(Integer, primary_key=True)
    trade_nifty = Column(Integer, nullable=True)  # 0/1
    trade_sensex = Column(Integer, nullable=True)  # 0/1
    lots_nifty = Column(Integer, nullable=True)  # 1..10
    lots_sensex = Column(Integer, nullable=True)  # 1..10
    max_premium_inr = Column(Float, nullable=True)  # per underlying per expiry
    target_mult = Column(Float, nullable=True)  # combined-bid exit multiple
    hard_exit_time = Column(String(8), nullable=True)  # "HH:MM:SS" IST
    poll_interval_s = Column(Integer, nullable=True)  # monitor cadence
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(64), nullable=True)


class CasStraddleTrade(Base):
    """One option leg of a straddle, in sandbox or live mode."""

    __tablename__ = "cas_straddle_trades"

    id = Column(Integer, primary_key=True)
    mode = Column(String(10), nullable=False)  # sandbox | live
    trade_date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD (IST)
    underlying = Column(String(16), nullable=False)  # NIFTY | SENSEX
    exchange = Column(String(10), nullable=False)  # NFO | BFO
    symbol = Column(String(50), nullable=False)  # option contract symbol
    strike = Column(Float, nullable=False)
    expiry = Column(String(12), nullable=False)  # DD-MMM-YY
    side = Column(String(2), nullable=False)  # CE | PE
    product = Column(String(10), nullable=False, default="NRML")
    lots = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False)  # lots × lotsize
    # decision-time references (what we SAW), kept apart from fills (what
    # was TRANSACTED) so slippage stays measurable
    entry_ref_price = Column(Float, nullable=True)  # ask at the 15:20 decision
    entry_ref_bid = Column(Float, nullable=True)
    entry_order_id = Column(String(64), nullable=True)
    entry_price = Column(Float, nullable=True)  # broker average_price
    entry_qty = Column(Integer, nullable=True)  # broker filled_quantity
    entry_at = Column(DateTime, nullable=True)  # naive UTC
    exit_order_id = Column(String(64), nullable=True)
    exit_ref_price = Column(Float, nullable=True)  # bid at the exit decision
    exit_price = Column(Float, nullable=True)  # broker average_price
    exit_at = Column(DateTime, nullable=True)
    exit_reason = Column(String(24), nullable=True)
    gross_pnl = Column(Float, nullable=True)
    charges_inr = Column(Float, nullable=True)  # modelled
    net_pnl = Column(Float, nullable=True)  # derived in ONE place (net_pnl_of_row)
    # counterfactual: intrinsic at the official settlement, numbers only
    settlement_cf_pnl = Column(Float, nullable=True)
    status = Column(String(12), nullable=False, default="placed")
    fill = Column(String(8), nullable=False, default=REAL_FILL)  # real | paper
    error_message = Column(String(255), nullable=True)
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CasStraddlePoll(Base):
    """One quote observation for one watched instrument."""

    __tablename__ = "cas_straddle_polls"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, nullable=False, index=True)  # naive UTC
    trade_date = Column(String(10), nullable=False, index=True)
    underlying = Column(String(16), nullable=False)
    kind = Column(String(8), nullable=False)  # spot | option
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(10), nullable=False)
    strike = Column(Float, nullable=True)
    side = Column(String(2), nullable=True)  # CE | PE | None for spot
    ltp = Column(Float, nullable=True)
    bid = Column(Float, nullable=True)
    ask = Column(Float, nullable=True)
    volume = Column(Integer, nullable=True)
    oi = Column(Integer, nullable=True)


class CasStraddleSession(Base):
    """Per-(trade_date, underlying) session digest — the backtest input row."""

    __tablename__ = "cas_straddle_sessions"
    __table_args__ = (UniqueConstraint("trade_date", "underlying", name="uq_cas_session"),)

    id = Column(Integer, primary_key=True)
    trade_date = Column(String(10), nullable=False, index=True)
    underlying = Column(String(16), nullable=False)
    exchange = Column(String(10), nullable=True)
    expiry = Column(String(12), nullable=True)
    atm_strike = Column(Float, nullable=True)
    close_1515 = Column(Float, nullable=True)  # last continuous spot print
    first_print = Column(Float, nullable=True)  # first spot print at/after 15:20:00
    first_print_at = Column(String(8), nullable=True)  # HH:MM:SS IST
    iiv_low = Column(Float, nullable=True)
    iiv_low_at = Column(String(8), nullable=True)
    iiv_high = Column(Float, nullable=True)
    iiv_high_at = Column(String(8), nullable=True)
    iiv_close = Column(Float, nullable=True)  # last spot print of the window
    straddle_ask_1520 = Column(Float, nullable=True)  # CE ask + PE ask at entry poll
    straddle_bid_1520 = Column(Float, nullable=True)
    max_combined_bid = Column(Float, nullable=True)
    max_combined_bid_at = Column(String(8), nullable=True)
    t_first_target = Column(String(8), nullable=True)  # first poll ≥ target
    combined_bid_1525 = Column(Float, nullable=True)
    combined_bid_1528 = Column(Float, nullable=True)
    combined_bid_1530 = Column(Float, nullable=True)
    combined_bid_1535 = Column(Float, nullable=True)
    spread_pct_1520 = Column(Float, nullable=True)  # (ask-bid)/mid, combined
    settle = Column(Float, nullable=True)  # last spot print (proxy for the close)
    settle_intrinsic = Column(Float, nullable=True)  # |settle − K|
    traded = Column(Integer, nullable=False, default=0)  # 0/1 orders were placed
    n_polls = Column(Integer, nullable=True)
    config_json = Column(Text, nullable=True)  # effective config at the arm
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


_TRADE_COLUMNS_ADDED_LATER: dict[str, str] = {}


def _ensure_columns() -> None:
    """Idempotently add columns introduced after the tables first shipped.

    ``create_all`` never alters an existing table (the futures_follow /
    open15 precedent), so any column added later lands here as an
    ``ALTER TABLE ADD COLUMN``. Empty today; the hook exists so the first
    follow-up does not have to invent the mechanism.
    """
    if not _TRADE_COLUMNS_ADDED_LATER:
        return
    try:
        with engine.connect() as conn:
            existing = {
                row[1] for row in conn.execute(text("PRAGMA table_info(cas_straddle_trades)"))
            }
            for col, ddl in _TRADE_COLUMNS_ADDED_LATER.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE cas_straddle_trades ADD COLUMN {col} {ddl}"))
                    logger.info("cas_straddle_trades: added column %s", col)
            conn.commit()
    except Exception as e:
        logger.exception(f"Failed to ensure cas_straddle_trades columns: {e}")


def init_db() -> None:
    """Create the four tables if missing (idempotent)."""
    try:
        Base.metadata.create_all(bind=engine)
        _ensure_columns()
        logger.info("cas_straddle_* tables ready")
    except Exception as e:
        logger.exception(f"Failed to init cas_straddle tables: {e}")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
CONFIG_FIELDS = (
    "trade_nifty",
    "trade_sensex",
    "lots_nifty",
    "lots_sensex",
    "max_premium_inr",
    "target_mult",
    "hard_exit_time",
    "poll_interval_s",
)

_BOOL_FIELDS = ("trade_nifty", "trade_sensex")


def get_config() -> dict | None:
    """The UI config row as a dict (None if never saved). NULL fields stay None
    so the service can fall through to its code defaults."""
    try:
        row = db_session.query(CasStraddleConfig).filter(CasStraddleConfig.id == 1).first()
        if row is None:
            return None
        out: dict = {}
        for f in CONFIG_FIELDS:
            v = getattr(row, f)
            if f in _BOOL_FIELDS and v is not None:
                v = bool(v)
            out[f] = v
        out["updated_at"] = row.updated_at.isoformat() if row.updated_at else None
        out["updated_by"] = row.updated_by
        return out
    except Exception:
        logger.exception("cas_straddle: get_config failed")
        return None
    finally:
        db_session.remove()


def save_config(values: dict, updated_by: str = "ui") -> dict | None:
    """Upsert the single config row. Only keys in ``CONFIG_FIELDS`` are written;
    a key set to None clears the override (falls back to the code default).
    Returns the stored row (via ``get_config``) or None on failure."""
    try:
        row = db_session.query(CasStraddleConfig).filter(CasStraddleConfig.id == 1).first()
        if row is None:
            row = CasStraddleConfig(id=1)
            db_session.add(row)
        for f in CONFIG_FIELDS:
            if f not in values:
                continue
            v = values[f]
            if f in _BOOL_FIELDS and v is not None:
                v = 1 if v else 0
            setattr(row, f, v)
        row.updated_by = updated_by
        row.updated_at = datetime.utcnow()
        db_session.commit()
    except Exception:
        logger.exception("cas_straddle: save_config failed")
        db_session.rollback()
        return None
    finally:
        db_session.remove()
    return get_config()


# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #
def record_trade(**fields) -> int | None:
    """Insert one leg row. Returns the row id, or None on failure."""
    try:
        row = CasStraddleTrade(**fields)
        db_session.add(row)
        db_session.commit()
        return row.id
    except Exception as e:
        logger.exception(f"Failed to record cas_straddle trade: {e}")
        db_session.rollback()
        return None
    finally:
        db_session.remove()


def update_trade(row_id: int, **fields) -> bool:
    """Update columns on one leg row. ``net_pnl`` is recomputed from the stored
    prices/charges whenever any of them changes (single derivation point)."""
    try:
        row = db_session.query(CasStraddleTrade).filter(CasStraddleTrade.id == row_id).first()
        if row is None:
            return False
        for k, v in fields.items():
            setattr(row, k, v)
        if any(k in fields for k in ("entry_price", "exit_price", "charges_inr", "entry_qty")):
            gross, net = _pnl_from_fields(row)
            row.gross_pnl = gross
            row.net_pnl = net
        db_session.commit()
        return True
    except Exception as e:
        logger.exception(f"Failed to update cas_straddle trade {row_id}: {e}")
        db_session.rollback()
        return False
    finally:
        db_session.remove()


def _pnl_from_fields(row) -> tuple[float | None, float | None]:
    if row.entry_price is None or row.exit_price is None:
        return None, None
    qty = int(row.entry_qty if row.entry_qty is not None else row.quantity or 0)
    gross = (float(row.exit_price) - float(row.entry_price)) * qty
    net = gross - float(row.charges_inr or 0.0)
    return round(gross, 2), round(net, 2)


def net_pnl_of_row(row) -> float | None:
    """The ONE definition of a leg's net P&L. ``None`` for a paper leg, an
    unpriced leg, or an open leg — never 0 for "unknown"."""
    if getattr(row, "fill", REAL_FILL) != REAL_FILL:
        return None
    if getattr(row, "status", None) != "closed":
        return None
    _gross, net = _pnl_from_fields(row)
    return net


def get_trade(row_id: int):
    try:
        return db_session.query(CasStraddleTrade).filter(CasStraddleTrade.id == row_id).first()
    except Exception:
        logger.exception("cas_straddle: get_trade failed")
        return None
    finally:
        db_session.remove()


def trades_for_date(trade_date: str) -> list:
    try:
        return (
            db_session.query(CasStraddleTrade)
            .filter(CasStraddleTrade.trade_date == trade_date)
            .order_by(CasStraddleTrade.id)
            .all()
        )
    except Exception:
        logger.exception("cas_straddle: trades_for_date failed")
        return []
    finally:
        db_session.remove()


def open_trades(trade_date: str | None = None) -> list:
    """Legs believed to hold a position (``placed`` or ``open``), real fills only."""
    try:
        q = db_session.query(CasStraddleTrade).filter(
            CasStraddleTrade.status.in_(("placed", "open")),
            CasStraddleTrade.fill == REAL_FILL,
        )
        if trade_date:
            q = q.filter(CasStraddleTrade.trade_date == trade_date)
        return q.order_by(CasStraddleTrade.id).all()
    except Exception:
        logger.exception("cas_straddle: open_trades failed")
        return []
    finally:
        db_session.remove()


def recent_trades(limit: int = 50) -> list:
    try:
        return (
            db_session.query(CasStraddleTrade)
            .order_by(CasStraddleTrade.created_at.desc(), CasStraddleTrade.id.desc())
            .limit(limit)
            .all()
        )
    except Exception:
        logger.exception("cas_straddle: recent_trades failed")
        return []
    finally:
        db_session.remove()


def real_closed_rows(mode: str | None = None) -> list:
    """Every closed REAL leg — the only row set a P&L consumer may sum."""
    try:
        q = db_session.query(CasStraddleTrade).filter(
            CasStraddleTrade.status == "closed",
            CasStraddleTrade.fill == REAL_FILL,
            CasStraddleTrade.exit_price.isnot(None),
            CasStraddleTrade.entry_price.isnot(None),
        )
        if mode:
            q = q.filter(CasStraddleTrade.mode == mode)
        return q.order_by(CasStraddleTrade.exit_at, CasStraddleTrade.id).all()
    except Exception:
        logger.exception("cas_straddle: real_closed_rows failed")
        return []
    finally:
        db_session.remove()


def trade_to_dict(r) -> dict:
    return {
        "id": r.id,
        "mode": r.mode,
        "trade_date": r.trade_date,
        "underlying": r.underlying,
        "exchange": r.exchange,
        "symbol": r.symbol,
        "strike": r.strike,
        "expiry": r.expiry,
        "side": r.side,
        "product": r.product,
        "lots": r.lots,
        "quantity": r.quantity,
        "entry_ref_price": r.entry_ref_price,
        "entry_ref_bid": r.entry_ref_bid,
        "entry_order_id": r.entry_order_id,
        "entry_price": r.entry_price,
        "entry_qty": r.entry_qty,
        "entry_at": r.entry_at.isoformat() if r.entry_at else None,
        "exit_order_id": r.exit_order_id,
        "exit_ref_price": r.exit_ref_price,
        "exit_price": r.exit_price,
        "exit_at": r.exit_at.isoformat() if r.exit_at else None,
        "exit_reason": r.exit_reason,
        "gross_pnl": r.gross_pnl,
        "charges_inr": r.charges_inr,
        "net_pnl": net_pnl_of_row(r),
        "settlement_cf_pnl": r.settlement_cf_pnl,
        "status": r.status,
        "fill": r.fill,
        "error_message": r.error_message,
        "note": r.note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# --------------------------------------------------------------------------- #
# Polls
# --------------------------------------------------------------------------- #
def record_polls(rows: list[dict]) -> int:
    """Bulk-insert poll observations. Returns the number written (0 on failure)."""
    if not rows:
        return 0
    try:
        db_session.bulk_insert_mappings(CasStraddlePoll, rows)
        db_session.commit()
        return len(rows)
    except Exception as e:
        logger.exception(f"Failed to record cas_straddle polls: {e}")
        db_session.rollback()
        return 0
    finally:
        db_session.remove()


def polls_for(trade_date: str, underlying: str | None = None, limit: int | None = None) -> list:
    try:
        q = db_session.query(CasStraddlePoll).filter(CasStraddlePoll.trade_date == trade_date)
        if underlying:
            q = q.filter(CasStraddlePoll.underlying == underlying)
        q = q.order_by(CasStraddlePoll.ts, CasStraddlePoll.id)
        if limit:
            q = q.limit(limit)
        return q.all()
    except Exception:
        logger.exception("cas_straddle: polls_for failed")
        return []
    finally:
        db_session.remove()


def poll_to_dict(p) -> dict:
    return {
        "ts": p.ts.isoformat() if p.ts else None,
        "trade_date": p.trade_date,
        "underlying": p.underlying,
        "kind": p.kind,
        "symbol": p.symbol,
        "exchange": p.exchange,
        "strike": p.strike,
        "side": p.side,
        "ltp": p.ltp,
        "bid": p.bid,
        "ask": p.ask,
        "volume": p.volume,
        "oi": p.oi,
    }


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def upsert_session(trade_date: str, underlying: str, **fields) -> int | None:
    """Insert or update the ``(trade_date, underlying)`` digest row."""
    try:
        row = (
            db_session.query(CasStraddleSession)
            .filter(
                CasStraddleSession.trade_date == trade_date,
                CasStraddleSession.underlying == underlying,
            )
            .first()
        )
        if row is None:
            row = CasStraddleSession(trade_date=trade_date, underlying=underlying)
            db_session.add(row)
        for k, v in fields.items():
            if k == "config" and v is not None:
                row.config_json = json.dumps(v, default=str)
            else:
                setattr(row, k, v)
        db_session.commit()
        return row.id
    except Exception as e:
        logger.exception(f"Failed to upsert cas_straddle session: {e}")
        db_session.rollback()
        return None
    finally:
        db_session.remove()


def sessions(limit: int = 60) -> list:
    try:
        return (
            db_session.query(CasStraddleSession)
            .order_by(CasStraddleSession.trade_date.desc(), CasStraddleSession.underlying)
            .limit(limit)
            .all()
        )
    except Exception:
        logger.exception("cas_straddle: sessions failed")
        return []
    finally:
        db_session.remove()


def session_to_dict(s) -> dict:
    cfg = None
    if s.config_json:
        try:
            cfg = json.loads(s.config_json)
        except Exception:
            cfg = None
    return {
        "trade_date": s.trade_date,
        "underlying": s.underlying,
        "exchange": s.exchange,
        "expiry": s.expiry,
        "atm_strike": s.atm_strike,
        "close_1515": s.close_1515,
        "first_print": s.first_print,
        "first_print_at": s.first_print_at,
        "iiv_low": s.iiv_low,
        "iiv_low_at": s.iiv_low_at,
        "iiv_high": s.iiv_high,
        "iiv_high_at": s.iiv_high_at,
        "iiv_close": s.iiv_close,
        "straddle_ask_1520": s.straddle_ask_1520,
        "straddle_bid_1520": s.straddle_bid_1520,
        "max_combined_bid": s.max_combined_bid,
        "max_combined_bid_at": s.max_combined_bid_at,
        "t_first_target": s.t_first_target,
        "combined_bid_1525": s.combined_bid_1525,
        "combined_bid_1528": s.combined_bid_1528,
        "combined_bid_1530": s.combined_bid_1530,
        "combined_bid_1535": s.combined_bid_1535,
        "spread_pct_1520": s.spread_pct_1520,
        "settle": s.settle,
        "settle_intrinsic": s.settle_intrinsic,
        "traded": bool(s.traded),
        "n_polls": s.n_polls,
        "config": cfg,
        "note": s.note,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }
