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
    instrument = Column(String(8), nullable=True)  # stock / option (issue #437; NULL = stock)

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
    charges_inr = Column(Float, nullable=True)  # modelled MIS round-trip charges (issue #433)

    # ATM option shadow trade (issue #435) — research-only: what 1 lot of the
    # ATM CE (L) / PE (S) did over the same entry->exit window. No orders.
    opt_symbol = Column(String(48), nullable=True)  # e.g. BAJAJ-AUTO28JUL2610700CE
    opt_lot_size = Column(Integer, nullable=True)
    opt_entry_premium = Column(Float, nullable=True)  # open of minute after trigger
    opt_exit_premium = Column(Float, nullable=True)  # 09:30 bar open
    opt_charges_inr = Column(Float, nullable=True)  # modelled option round-trip, 1 lot
    opt_pnl = Column(Float, nullable=True)  # NET premium pnl for 1 lot

    # contract liquidity at the two decision moments (issue #488) — measurement
    # only, nothing gates on them. Recorded so exit slippage can eventually be
    # regressed against the contract's own flow instead of a guessed threshold:
    # on 2026-07-28 every ex-ante metric ranked the two live trades backwards,
    # so the raw inputs are captured until the data justifies a rule.
    opt_entry_volume = Column(Integer, nullable=True)  # cumulative day volume at trigger
    opt_entry_oi = Column(Integer, nullable=True)  # open interest at trigger
    opt_exit_volume = Column(Integer, nullable=True)  # cumulative day volume at exit
    opt_exit_oi = Column(Integer, nullable=True)  # open interest at exit
    # how this symbol got onto the watch list (issue #529): ``seed`` = the 09:16
    # gap ranking, ``rolling`` = appended by an intraday re-rank. Load-bearing
    # for the measurement — without it the two cohorts cannot be scored apart.
    # NULL on rows written before #529 shipped; read as ``seed``.
    watch_source = Column(String(8), nullable=True)
    status = Column(String(16), default="open")  # open / closed / error / observe / rejected
    # Did an order actually fill (issue #548)? ``real`` = the broker accepted it;
    # ``paper`` = the broker REJECTED the entry and every price/P&L field on this
    # row is a sandbox-equivalent simulation of what the trade would have done.
    # NULL on rows written before #548 shipped; read as ``real``.
    #
    # Load-bearing: ``mode`` still records what the run genuinely was (``live``)
    # — a paper row must never be disguised as a sandbox run, and paper P&L must
    # never be summed into realized P&L (see ``total_realized_pnl``).
    fill = Column(String(8), nullable=True)
    reason = Column(String(64), nullable=True)
    # full broker rejection text — ``reason`` stays a short machine-readable code
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Open15DayLog(Base):
    """One row per trading day: the full decision log as a JSON blob.

    Upserted on every logged event (issue #444 — was 09:30/09:35 only), so
    the UI can show past days' decision timelines after restarts and a
    mid-window crash loses nothing.
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
    instrument = Column(String(16), nullable=True)  # stock | atm_option (issue #437)
    max_trades = Column(Integer, nullable=True)  # daily entry cap, both sides (issue #437)
    no_entry_after = Column(String(5), nullable=True)  # "HH:MM" IST entry cutoff (issue #451)
    exit_time = Column(String(5), nullable=True)  # "HH:MM" IST hard flatten (issue #451)
    trade_side = Column(String(16), nullable=True)  # both | long_only | short_only (issue #503)
    # rolling additive watch list (issue #529). NULL = env default; the cadence
    # and top-N are the operator-facing knobs edited from /open15_vol_breakout/logs.
    rolling_watchlist_enabled = Column(Integer, nullable=True)  # 0/1 (NULL = env default)
    rolling_cadence_s = Column(Integer, nullable=True)  # re-rank period, clamped 10..300
    rolling_top_n = Column(Integer, nullable=True)  # adds per side per cycle, clamped 1..10
    updated_by = Column(String(64), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def _ensure_columns():
    """Idempotently add columns introduced after the table's first creation.

    ``Base.metadata.create_all`` only creates *new* tables — it never alters an
    existing one, so post-ship columns need an explicit ``ALTER TABLE``. No-op
    when the column already exists.
    """
    from sqlalchemy import text

    wanted_by_table = {
        "open15_trades": {
            "charges_inr": "FLOAT",
            "instrument": "VARCHAR(8)",
            "opt_symbol": "VARCHAR(48)",
            "opt_lot_size": "INTEGER",
            "opt_entry_premium": "FLOAT",
            "opt_exit_premium": "FLOAT",
            "opt_charges_inr": "FLOAT",
            "opt_pnl": "FLOAT",
            "opt_entry_volume": "INTEGER",
            "opt_entry_oi": "INTEGER",
            "opt_exit_volume": "INTEGER",
            "opt_exit_oi": "INTEGER",
            "watch_source": "VARCHAR(8)",
            "fill": "VARCHAR(8)",
            "error_message": "TEXT",
        },
        "open15_config": {
            "instrument": "VARCHAR(16)",
            "max_trades": "INTEGER",
            "no_entry_after": "VARCHAR(5)",
            "exit_time": "VARCHAR(5)",
            "trade_side": "VARCHAR(16)",
            "rolling_watchlist_enabled": "INTEGER",
            "rolling_cadence_s": "INTEGER",
            "rolling_top_n": "INTEGER",
        },
    }
    try:
        with engine.connect() as conn:
            for table, wanted in wanted_by_table.items():
                existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
                for col, ddl in wanted.items():
                    if col not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                        logger.info("%s: added column %s", table, col)
            conn.commit()
    except Exception:
        logger.exception("Failed to ensure open15 columns")


def init_db():
    """Create the tables if missing. Idempotent; never touches other tables."""
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
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
            "instrument": row.instrument,
            "max_trades": row.max_trades,
            "no_entry_after": row.no_entry_after,
            "exit_time": row.exit_time,
            "trade_side": row.trade_side,
            # None stays None so ``resolve_day_config`` can fall through to env
            "rolling_watchlist_enabled": (
                None
                if row.rolling_watchlist_enabled is None
                else bool(row.rolling_watchlist_enabled)
            ),
            "rolling_cadence_s": row.rolling_cadence_s,
            "rolling_top_n": row.rolling_top_n,
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
    instrument: str | None = None,
    max_trades: int | None = None,
    no_entry_after: str | None = None,
    exit_time: str | None = None,
    trade_side: str | None = None,
    rolling_watchlist_enabled: bool | None = None,
    rolling_cadence_s: int | None = None,
    rolling_top_n: int | None = None,
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
        row.instrument = instrument
        row.max_trades = max_trades
        row.no_entry_after = no_entry_after
        row.exit_time = exit_time
        row.trade_side = trade_side
        row.rolling_watchlist_enabled = (
            None if rolling_watchlist_enabled is None else int(bool(rolling_watchlist_enabled))
        )
        row.rolling_cadence_s = rolling_cadence_s
        row.rolling_top_n = rolling_top_n
        row.updated_by = updated_by
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("open15: config save failed")
        return False
    finally:
        db_session.remove()


# A row is a REAL fill unless it is explicitly marked paper (issue #548). Written
# as a NULL-tolerant predicate so every row created before the column existed —
# and every row a future writer forgets to stamp — counts as real, which is the
# safe direction: a mislabelled paper row would inflate realized P&L, whereas a
# mislabelled real row only understates it.
_REAL_FILL = (Open15Trade.fill.is_(None)) | (Open15Trade.fill != "paper")


def net_pnl_expr():
    """SQL expression for a row's NET P&L — the ONE P&L convention (issue #552).

    ``pnl`` is gross (entry -> exit) and the modelled round-trip charges live
    separately in ``charges_inr`` (issue #433), so every consumer that reports
    "the P&L" has to deduct them. Before #552 four sites re-derived this
    independently and three disagreed: the day digest and ``total_realized_pnl``
    reported gross, the strategies dashboard reported net, the ``exit`` event
    reported gross and the ``exit_paper`` event reported net. On 2026-08-05 that
    put **+₹2109 gross** in the logs-page chip above rows totalling **+₹1383.81
    net** (charges were 34% of gross); on 2026-07-23 it flipped the sign.

    Charges are real money, so net is the honest number and the only one that
    should ever be shown or compounded. Route every new consumer through here
    (or ``net_pnl_of_row``) rather than writing ``sum(pnl)`` again.
    """
    from sqlalchemy import func

    return Open15Trade.pnl - func.coalesce(Open15Trade.charges_inr, 0.0)


def net_pnl_of_row(row) -> float:
    """Python-side twin of :func:`net_pnl_expr` for an ORM row / mapping."""
    get = row.get if isinstance(row, dict) else lambda k: getattr(row, k, None)
    return float(get("pnl") or 0.0) - float(get("charges_inr") or 0.0)


def total_realized_pnl() -> float:
    """Sum of NET research P&L across closed trades (drives compound sizing).

    Net of modelled charges (issue #552) — compounding off gross would size
    tomorrow's real orders against money the charges already took.

    **Paper rows are excluded** (issue #548). A broker-rejected entry is priced
    as a sandbox-equivalent simulation so the day stays measurable, but that
    money was never made — compounding tomorrow's position size off it would
    size real orders against fictional capital.
    """
    try:
        from sqlalchemy import func

        val = (
            db_session.query(func.sum(net_pnl_expr()))
            .filter(Open15Trade.pnl.isnot(None), _REAL_FILL)
            .scalar()
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


def list_day_logs() -> list[tuple[str, list]]:
    """All persisted day logs as ``[(trade_date, events), ...]``, newest first.

    Fail-graceful (returns ``[]``); a row whose JSON fails to parse is skipped
    with an exception log rather than sinking the whole listing.
    """
    import json

    out: list[tuple[str, list]] = []
    try:
        rows = db_session.query(Open15DayLog).order_by(Open15DayLog.trade_date.desc()).all()
        for row in rows:
            try:
                out.append((row.trade_date, json.loads(row.log_json) if row.log_json else []))
            except (ValueError, TypeError):
                logger.exception(
                    "open15: day-log JSON unparseable for %s — skipped", row.trade_date
                )
        return out
    except Exception:
        logger.exception("open15: day-log listing failed")
        return []
    finally:
        db_session.remove()


def _pnl_by_date(paper: bool) -> dict[str, float]:
    """NET journal P&L per trade_date for one fill class (issues #548, #552)."""
    try:
        from sqlalchemy import func

        pred = (Open15Trade.fill == "paper") if paper else _REAL_FILL
        rows = (
            db_session.query(Open15Trade.trade_date, func.sum(net_pnl_expr()))
            .filter(Open15Trade.pnl.isnot(None), pred)
            .group_by(Open15Trade.trade_date)
            .all()
        )
        return {d: round(float(v), 2) for d, v in rows if v is not None}
    except Exception:
        logger.exception("open15: per-date pnl aggregation failed (paper=%s)", paper)
        return {}
    finally:
        db_session.remove()


def trades_pnl_by_date() -> dict[str, float]:
    """NET REAL journal P&L per trade_date (charges deducted — issue #552).

    Paper rows are excluded and reported separately by ``paper_pnl_by_date`` —
    the history sidebar must never blend simulated money into a day's P&L.
    """
    return _pnl_by_date(paper=False)


def paper_pnl_by_date() -> dict[str, float]:
    """NET PAPER journal P&L per trade_date (broker-rejected entries, #548)."""
    return _pnl_by_date(paper=True)


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
