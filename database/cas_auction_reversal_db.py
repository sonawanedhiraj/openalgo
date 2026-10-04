"""Persistence for the ``cas_auction_reversal`` strategy (issue #752).

Four additive tables in the main database (``openalgo.db``). This module owns
ONLY these tables and touches nothing else:

* ``cas_ar_config``     — the single-row (id=1) UI-editable config (issue #755);
  NULL fields fall through to the ``CAS_AR_*`` env seed, then the code default.
* ``cas_ar_trades``     — one row per position: the CNC BUY taken in the
  closing auction (status ``placed``/``open``/``rejected``/``error``) and its
  T+1 morning SELL (``closed`` with realized P&L).
* ``cas_ar_polls``      — every monitor poll for every universe symbol across
  15:14:30–15:33 IST: last price, the auction's indicative close
  (``indicative_close_price``) and imbalance. This is the pre-registered
  20-session measurement (R71b): it says how well the 15:20–15:25 indicative
  price predicts the final auction close. Written whatever the trade outcome.
* ``cas_ar_candidates`` — one row per ``(trade_date, symbol)`` that crossed the
  threshold at the decision: reference price, the indicative close the
  decision read, whether it was selected (or why not), and — filled by the EOD
  job — the final auction close and the T+1 counterfactual. Numbers only.

P&L convention (#552 rule): ``net_pnl_of_row`` is the ONLY place a trade's net
P&L is derived from its stored fields; ``real_closed_rows`` is the row set
every P&L consumer must read.
"""

from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
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

# ``real`` is money. ``paper`` = the broker REFUSED the entry (placement or
# post-ACK, the #548/#626 shape) — nothing was held, never joins real P&L.
REAL_FILL = "real"
NON_REAL_FILLS = ("paper",)
TRADE_STATUSES = ("placed", "open", "closed", "rejected", "error")


class CasArTrade(Base):
    """One CNC position: auction BUY on T, SELL on T+1 morning."""

    __tablename__ = "cas_ar_trades"

    id = Column(Integer, primary_key=True)
    mode = Column(String(10), nullable=False)  # sandbox | live
    trade_date = Column(String(10), nullable=False, index=True)  # entry day, YYYY-MM-DD IST
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(10), nullable=False, default="NSE")
    product = Column(String(10), nullable=False, default="CNC")
    quantity = Column(Integer, nullable=False)
    # decision-time observations (what we SAW), apart from fills (what was
    # TRANSACTED), so the IEP-vs-close gap and slippage stay measurable
    ref_price = Column(Float, nullable=True)  # last continuous print (frozen LTP after 15:15)
    iep_at_decision = Column(Float, nullable=True)  # indicative_close_price read at 15:23:30
    dislocation_pct = Column(Float, nullable=True)  # (iep / ref - 1) * 100
    imbalance_qty = Column(Integer, nullable=True)
    entry_order_id = Column(String(64), nullable=True)
    entry_price = Column(Float, nullable=True)  # broker average_price
    entry_qty = Column(Integer, nullable=True)  # broker filled quantity
    entry_at = Column(DateTime, nullable=True)  # naive UTC
    auction_close = Column(Float, nullable=True)  # official close (EOD quote)
    exit_order_id = Column(String(64), nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_at = Column(DateTime, nullable=True)
    exit_reason = Column(String(24), nullable=True)  # t1_exit | retry | manual | error
    gross_pnl = Column(Float, nullable=True)
    charges_inr = Column(Float, nullable=True)  # modelled CNC delivery charges
    net_pnl = Column(Float, nullable=True)  # derived in ONE place
    status = Column(String(12), nullable=False, default="placed")
    fill = Column(String(8), nullable=False, default=REAL_FILL)
    error_message = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CasArPoll(Base):
    """One quote observation for one universe symbol."""

    __tablename__ = "cas_ar_polls"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, nullable=False, index=True)  # naive UTC
    trade_date = Column(String(10), nullable=False, index=True)
    symbol = Column(String(50), nullable=False)
    ltp = Column(Float, nullable=True)
    iep = Column(Float, nullable=True)  # indicative_close_price (None outside CAS)
    imbalance_qty = Column(Integer, nullable=True)
    volume = Column(Integer, nullable=True)


class CasArCandidate(Base):
    """Every symbol that crossed the threshold at the decision — the
    measurement row (selected or not)."""

    __tablename__ = "cas_ar_candidates"
    __table_args__ = (UniqueConstraint("trade_date", "symbol", name="uq_cas_ar_candidate"),)

    id = Column(Integer, primary_key=True)
    trade_date = Column(String(10), nullable=False, index=True)
    symbol = Column(String(50), nullable=False)
    ref_price = Column(Float, nullable=True)
    iep_at_decision = Column(Float, nullable=True)
    dislocation_pct = Column(Float, nullable=True)
    imbalance_qty = Column(Integer, nullable=True)
    rank = Column(Integer, nullable=True)
    selected = Column(Integer, nullable=False, default=0)  # 0/1
    skip_reason = Column(String(48), nullable=True)  # max_positions | qty_zero | paused | ...
    auction_close = Column(Float, nullable=True)  # filled by the EOD job
    close_dislocation_pct = Column(Float, nullable=True)  # (close / ref - 1) * 100
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CasArConfig(Base):
    """Single-row (id=1) UI-editable config (issue #755). Every field nullable:
    NULL = fall through to the ``CAS_AR_*`` env seed, then the code default."""

    __tablename__ = "cas_ar_config"

    id = Column(Integer, primary_key=True)
    threshold_pct = Column(Float, nullable=True)
    max_positions = Column(Integer, nullable=True)
    capital_per_trade_inr = Column(Float, nullable=True)
    poll_interval_s = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(64), nullable=True)


CONFIG_FIELDS = ("threshold_pct", "max_positions", "capital_per_trade_inr", "poll_interval_s")


def init_db() -> None:
    """Create the four tables if missing (idempotent)."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("cas_ar_* tables ready")
    except Exception as e:
        logger.exception(f"Failed to init cas_ar tables: {e}")


# --------------------------------------------------------------------------- #
# Trades
# --------------------------------------------------------------------------- #
def record_trade(**fields) -> int | None:
    try:
        row = CasArTrade(**fields)
        db_session.add(row)
        db_session.commit()
        return row.id
    except Exception as e:
        logger.exception(f"Failed to record cas_ar trade: {e}")
        db_session.rollback()
        return None
    finally:
        db_session.remove()


def update_trade(row_id: int, **fields) -> bool:
    """Update one row. ``gross_pnl``/``net_pnl`` are recomputed from stored
    prices/charges whenever any of them changes (single derivation point)."""
    try:
        row = db_session.query(CasArTrade).filter(CasArTrade.id == row_id).first()
        if row is None:
            return False
        for k, v in fields.items():
            setattr(row, k, v)
        if any(k in fields for k in ("entry_price", "exit_price", "charges_inr", "entry_qty")):
            row.gross_pnl, row.net_pnl = _pnl_from_fields(row)
        db_session.commit()
        return True
    except Exception as e:
        logger.exception(f"Failed to update cas_ar trade {row_id}: {e}")
        db_session.rollback()
        return False
    finally:
        db_session.remove()


def _pnl_from_fields(row) -> tuple[float | None, float | None]:
    if row.entry_price is None or row.exit_price is None:
        return None, None
    qty = int(row.entry_qty if row.entry_qty is not None else row.quantity or 0)
    gross = (float(row.exit_price) - float(row.entry_price)) * qty
    return round(gross, 2), round(gross - float(row.charges_inr or 0.0), 2)


def net_pnl_of_row(row) -> float | None:
    """The ONE definition of a trade's net P&L. ``None`` for paper, open or
    unpriced rows — never 0 for "unknown"."""
    if getattr(row, "fill", REAL_FILL) != REAL_FILL or getattr(row, "status", None) != "closed":
        return None
    return _pnl_from_fields(row)[1]


def _query(fn, label: str, default):
    try:
        return fn()
    except Exception:
        logger.exception("cas_ar: %s failed", label)
        return default
    finally:
        db_session.remove()


def trades_for_date(trade_date: str) -> list:
    return _query(
        lambda: db_session.query(CasArTrade)
        .filter(CasArTrade.trade_date == trade_date)
        .order_by(CasArTrade.id)
        .all(),
        "trades_for_date",
        [],
    )


def open_positions(before_date: str | None = None) -> list:
    """Real entries believed to hold shares (``placed``/``open``). With
    ``before_date`` only entries made BEFORE that day — the T+1 exit set."""

    def _q():
        q = db_session.query(CasArTrade).filter(
            CasArTrade.status.in_(("placed", "open")), CasArTrade.fill == REAL_FILL
        )
        if before_date:
            q = q.filter(CasArTrade.trade_date < before_date)
        return q.order_by(CasArTrade.id).all()

    return _query(_q, "open_positions", [])


def recent_trades(limit: int = 50) -> list:
    return _query(
        lambda: db_session.query(CasArTrade)
        .order_by(CasArTrade.created_at.desc(), CasArTrade.id.desc())
        .limit(limit)
        .all(),
        "recent_trades",
        [],
    )


def real_closed_rows(mode: str | None = None) -> list:
    """Every closed REAL trade — the only row set a P&L consumer may sum."""

    def _q():
        q = db_session.query(CasArTrade).filter(
            CasArTrade.status == "closed",
            CasArTrade.fill == REAL_FILL,
            CasArTrade.exit_price.isnot(None),
            CasArTrade.entry_price.isnot(None),
        )
        if mode:
            q = q.filter(CasArTrade.mode == mode)
        return q.order_by(CasArTrade.exit_at, CasArTrade.id).all()

    return _query(_q, "real_closed_rows", [])


def trade_to_dict(r) -> dict:
    out = {c.name: getattr(r, c.name) for c in CasArTrade.__table__.columns}
    for k in ("entry_at", "exit_at", "created_at"):
        out[k] = out[k].isoformat() if out[k] else None
    out["net_pnl"] = net_pnl_of_row(r)
    return out


# --------------------------------------------------------------------------- #
# Polls
# --------------------------------------------------------------------------- #
def record_polls(rows: list[dict]) -> int:
    if not rows:
        return 0
    try:
        db_session.bulk_insert_mappings(CasArPoll, rows)
        db_session.commit()
        return len(rows)
    except Exception as e:
        logger.exception(f"Failed to record cas_ar polls: {e}")
        db_session.rollback()
        return 0
    finally:
        db_session.remove()


def poll_count(trade_date: str) -> int:
    return _query(
        lambda: db_session.query(CasArPoll).filter(CasArPoll.trade_date == trade_date).count(),
        "poll_count",
        0,
    )


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #
def upsert_candidate(trade_date: str, symbol: str, **fields) -> int | None:
    try:
        row = (
            db_session.query(CasArCandidate)
            .filter(CasArCandidate.trade_date == trade_date, CasArCandidate.symbol == symbol)
            .first()
        )
        if row is None:
            row = CasArCandidate(trade_date=trade_date, symbol=symbol)
            db_session.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        row.updated_at = datetime.utcnow()
        db_session.commit()
        return row.id
    except Exception as e:
        logger.exception(f"Failed to upsert cas_ar candidate {trade_date}/{symbol}: {e}")
        db_session.rollback()
        return None
    finally:
        db_session.remove()


def candidates_for_date(trade_date: str) -> list:
    return _query(
        lambda: db_session.query(CasArCandidate)
        .filter(CasArCandidate.trade_date == trade_date)
        .order_by(CasArCandidate.rank, CasArCandidate.id)
        .all(),
        "candidates_for_date",
        [],
    )


def candidate_to_dict(c) -> dict:
    out = {col.name: getattr(c, col.name) for col in CasArCandidate.__table__.columns}
    for k in ("created_at", "updated_at"):
        out[k] = out[k].isoformat() if out[k] else None
    out["selected"] = bool(out["selected"])
    return out


# --------------------------------------------------------------------------- #
# Config (issue #755)
# --------------------------------------------------------------------------- #
def get_config() -> dict | None:
    """The UI config row as a dict (None if never saved). NULL fields stay None
    so the service can fall through to the env seed / code default."""
    try:
        row = db_session.query(CasArConfig).filter(CasArConfig.id == 1).first()
        if row is None:
            return None
        out = {f: getattr(row, f) for f in CONFIG_FIELDS}
        out["updated_at"] = row.updated_at.isoformat() if row.updated_at else None
        out["updated_by"] = row.updated_by
        return out
    except Exception:
        logger.exception("cas_ar: get_config failed")
        return None
    finally:
        db_session.remove()


def save_config(values: dict, updated_by: str = "ui") -> dict | None:
    """Upsert the single config row. Only keys in ``CONFIG_FIELDS`` are written;
    a key set to None clears that override. Returns the stored row or None."""
    try:
        row = db_session.query(CasArConfig).filter(CasArConfig.id == 1).first()
        if row is None:
            row = CasArConfig(id=1)
            db_session.add(row)
        for f in CONFIG_FIELDS:
            if f in values:
                setattr(row, f, values[f])
        row.updated_by = updated_by
        row.updated_at = datetime.utcnow()
        db_session.commit()
    except Exception:
        logger.exception("cas_ar: save_config failed")
        db_session.rollback()
        return None
    finally:
        db_session.remove()
    return get_config()
