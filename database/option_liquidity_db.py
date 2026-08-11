"""Persistence for the daily option-liquidity score (``option_liquidity_daily``).

Additive table in the main database (``db/openalgo.db``) recording, per trading day,
how tradeable each F&O underlying's near-the-money option book actually is. Written
by the 15:45 IST ``option_liquidity_eod`` job in
``services/option_liquidity_service.py``. Read-only on every other module — this file
owns only its own table.

**Keyed ``(as_of_date, symbol, side)`` — one row per SIDE, and that is load-bearing.**
``open15_vol_breakout`` buys a CE for a long and a PE for a short, and the two books
are not interchangeable. On 20-day medians over 2026-06-01..08-07, collapsing the
sides into one number misclassifies **17 of 208** names — 8 thin only on calls
(BLUESTARCO, GMRAIRPORT, IRFC, MAXHEALTH, OBEROIRLTY, OIL, PNBHOUSING, PRESTIGE) and
9 only on puts (360ONE, APLAPOLLO, CROMPTON, DALBHARAT, GODFRYPHLP, PIDILITIND,
RADICO, SUPREMEIND, UNOMINDA). UNOMINDA is the clean case: CE p28 against PE p10, so
it is perfectly tradeable long and should never be entered short. A consumer must
always read the score for the side it intends to trade.

``option_liquidity_pctile`` is the value consumers read; it is a **20-day median** of
the daily percentile, not today's. Single-day scoring is far too noisy to act on
(replayed over 2026-02..05: consecutive-day Jaccard 0.48 on the bottom quintile, ~30
names entering or leaving per day; the median lifts that to 0.91 / 3.4). ``NULL``
means "not enough history to rank this symbol honestly" — never "illiquid".
"""

import json
import os
from datetime import date as _date
from datetime import datetime, timedelta

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
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

#: The two option sides. ``CE`` backs a long entry, ``PE`` a short.
SIDES = ("CE", "PE")


class OptionLiquidityDaily(Base):
    """One row per (trading day, underlying, option side)."""

    __tablename__ = "option_liquidity_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    as_of_date = Column(Date, nullable=False)
    symbol = Column(String(32), nullable=False)
    side = Column(String(2), nullable=False)  # CE | PE

    # --- the primary metric -------------------------------------------------
    # Rupees of premium that changed hands across the measured band. This is the
    # quantity a fill scales against. NOT trade count: an early draft ranked on
    # trade count and put MANAPPURAM in the bottom quintile when it sits mid-pack
    # on turnover with a ~Rs 44,000 average ticket. Few large tickets is block
    # flow, not illiquidity.
    atm_premium_turnover = Column(Float, nullable=True)

    # --- the hard tell ------------------------------------------------------
    # Strikes in this side's band that traded nothing at all. A majority-dead
    # band forces the symbol below the floor regardless of turnover.
    atm_zero_vol_strikes = Column(Integer, nullable=True)
    band_strikes = Column(Integer, nullable=True)  # how many strikes were measured

    # --- diagnostics (never gate on these directly) -------------------------
    atm_spread_pct = Column(Float, nullable=True)  # median spread as % of MID
    atm_volume_lots = Column(Float, nullable=True)
    atm_oi_lots = Column(Float, nullable=True)
    atm_trades = Column(Integer, nullable=True)  # NULL on the broker path
    avg_ticket_inr = Column(Float, nullable=True)

    # --- the score consumers read ------------------------------------------
    # Rank percentile of ``atm_premium_turnover`` within this side, within this
    # day's universe, then rolled to a 20-day median. NULL = insufficient
    # history (see ``n_days_in_median``), which is NOT a low score.
    option_liquidity_pctile = Column(Float, nullable=True)
    daily_pctile = Column(Float, nullable=True)  # today's raw percentile, pre-median
    n_days_in_median = Column(Integer, nullable=True)

    expiry_used = Column(Date, nullable=True)

    # Which feed produced this row: ``broker_sweep`` (the runtime path) or
    # ``bhavcopy`` (the one-time history seed). Mixing them inside one median is
    # sound ONLY because the stored value is a rank percentile *within that day's
    # universe* — bhavcopy turnover is lots x lot_size x close while the broker's is
    # volume_units x ltp, so any systematic scale difference cancels in the ranking.
    # The column exists so a mixed median is visible rather than assumed.
    source = Column(String(16), nullable=True)

    details_json = Column(Text, nullable=True)
    computed_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("as_of_date", "symbol", "side", name="uq_option_liquidity_day_sym_side"),
    )


Index("idx_option_liquidity_date", OptionLiquidityDaily.as_of_date)
Index("idx_option_liquidity_sym_side", OptionLiquidityDaily.symbol, OptionLiquidityDaily.side)


#: Columns added after the table first shipped. ``create_all`` NEVER alters an
#: existing table, so any new column must be listed here or it will simply not exist
#: on an install that already ran init_db — which fails at INSERT time, not at boot.
#: (Learned the hard way: ``source`` was added a day later and the backfill died with
#: "table option_liquidity_daily has no column named source" after scoring 49 days.)
_WANTED_COLUMNS: dict[str, str] = {
    "source": "VARCHAR(16)",
}


def _ensure_columns() -> None:
    """Add any missing column to an existing table. Idempotent, fail-graceful."""
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            have = {
                r[1]
                for r in conn.execute(text("PRAGMA table_info(option_liquidity_daily)")).fetchall()
            }
            if not have:
                return  # table doesn't exist yet; create_all will make it complete
            for col, ddl in _WANTED_COLUMNS.items():
                if col not in have:
                    conn.execute(text(f"ALTER TABLE option_liquidity_daily ADD COLUMN {col} {ddl}"))
                    logger.info("option_liquidity_daily: added missing column %s", col)
    except Exception:
        logger.exception("option_liquidity_daily: column migration failed")


def init_db():
    """Create the ``option_liquidity_daily`` table if missing, and migrate columns.

    Idempotent. ``create_all`` handles a fresh install; ``_ensure_columns`` handles an
    install that predates a later column.
    """
    try:
        Base.metadata.create_all(bind=engine)
        _ensure_columns()
        logger.info("option_liquidity_daily table ready")
    except Exception as e:
        logger.exception(f"Failed to init option_liquidity_daily table: {e}")


# Explicit alias for boot wiring that prefers the long name.
init_option_liquidity_db = init_db


def _row_to_dict(row: OptionLiquidityDaily) -> dict:
    return {
        "as_of_date": row.as_of_date.isoformat() if row.as_of_date else None,
        "symbol": row.symbol,
        "side": row.side,
        "atm_premium_turnover": row.atm_premium_turnover,
        "atm_zero_vol_strikes": row.atm_zero_vol_strikes,
        "band_strikes": row.band_strikes,
        "atm_spread_pct": row.atm_spread_pct,
        "atm_volume_lots": row.atm_volume_lots,
        "atm_oi_lots": row.atm_oi_lots,
        "atm_trades": row.atm_trades,
        "avg_ticket_inr": row.avg_ticket_inr,
        "option_liquidity_pctile": row.option_liquidity_pctile,
        "daily_pctile": row.daily_pctile,
        "n_days_in_median": row.n_days_in_median,
        "expiry_used": row.expiry_used.isoformat() if row.expiry_used else None,
        "source": row.source,
        "details": json.loads(row.details_json) if row.details_json else {},
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
    }


def upsert_scores(as_of_date: _date, rows: list[dict]) -> int:
    """Replace the whole day's scores in one transaction. Returns rows written.

    Delete-then-insert per date, so a re-run overwrites cleanly rather than
    accumulating duplicates — the same idempotency contract as
    ``scanner_comparison``. Partial days are not merged: a sweep either produced a
    universe or it did not, and half of yesterday blended with half of today would
    make the percentile meaningless (it is a rank *within that day's universe*).
    """
    if not rows:
        return 0
    try:
        db_session.query(OptionLiquidityDaily).filter(
            OptionLiquidityDaily.as_of_date == as_of_date
        ).delete(synchronize_session=False)
        for r in rows:
            db_session.add(
                OptionLiquidityDaily(
                    as_of_date=as_of_date,
                    symbol=r["symbol"],
                    side=r["side"],
                    atm_premium_turnover=r.get("atm_premium_turnover"),
                    atm_zero_vol_strikes=r.get("atm_zero_vol_strikes"),
                    band_strikes=r.get("band_strikes"),
                    atm_spread_pct=r.get("atm_spread_pct"),
                    atm_volume_lots=r.get("atm_volume_lots"),
                    atm_oi_lots=r.get("atm_oi_lots"),
                    atm_trades=r.get("atm_trades"),
                    avg_ticket_inr=r.get("avg_ticket_inr"),
                    option_liquidity_pctile=r.get("option_liquidity_pctile"),
                    daily_pctile=r.get("daily_pctile"),
                    n_days_in_median=r.get("n_days_in_median"),
                    expiry_used=r.get("expiry_used"),
                    source=r.get("source"),
                    details_json=json.dumps(r.get("details") or {}, default=str),
                    computed_at=datetime.utcnow(),
                )
            )
        db_session.commit()
        return len(rows)
    except Exception:
        db_session.rollback()
        logger.exception("failed to upsert option_liquidity_daily rows")
        return 0
    finally:
        db_session.remove()


def has_scores_for(as_of_date: _date) -> bool:
    """True when at least one row exists for ``as_of_date``.

    The convergence loop's probe (issue #589): cheap enough to run every tick.
    Fails towards ``False`` — a re-sweep is harmless (``upsert_scores`` replaces
    the day), whereas skipping one leaves a permanent hole.
    """
    try:
        return (
            db_session.query(OptionLiquidityDaily.id)
            .filter(OptionLiquidityDaily.as_of_date == as_of_date)
            .first()
            is not None
        )
    except Exception:
        logger.exception("option_liquidity: has_scores_for probe failed")
        return False
    finally:
        db_session.remove()


def get_scores_for_date(as_of_date: _date) -> list[dict]:
    """Every (symbol, side) row for one date."""
    try:
        rows = (
            db_session.query(OptionLiquidityDaily)
            .filter(OptionLiquidityDaily.as_of_date == as_of_date)
            .order_by(OptionLiquidityDaily.symbol, OptionLiquidityDaily.side)
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        db_session.remove()


def sessions_behind(as_of: _date, ref: _date, cap: int = 40) -> int:
    """Trading days in ``(as_of, ref]`` — how many sessions the score has missed.

    A weekend or an NSE holiday contributes **zero**: Friday's scores are 0
    sessions behind on Saturday and Sunday, 1 on Monday, and still 1 on the
    Tuesday after a Monday holiday (issue #589 — calendar-day staleness went
    dark on 2026-08-11 with the scores a single session old, and would do so
    again every long weekend, e.g. Tue 2026-09-15 after Ganesh Chaturthi).

    Delegates to ``data_freshness_service.is_trading_day`` (weekday AND not a
    ``market_holidays`` row, fail-open per #253). If that import or call ever
    raises, this degrades to weekday-only counting rather than failing — a
    broken calendar must not dark the gate. The walk is capped at ``cap``
    calendar days: anything that far back is stale under every convention.
    """
    try:
        from services.data_freshness_service import is_trading_day
    except Exception:  # circular-import guard — weekday-only fallback
        logger.exception("option_liquidity: is_trading_day import failed — weekday fallback")

        def is_trading_day(d, exchange=None):
            return d.weekday() < 5

    n = 0
    d = as_of + timedelta(days=1)
    end = min(ref, as_of + timedelta(days=cap))
    while d <= end:
        try:
            if is_trading_day(d):
                n += 1
        except Exception:
            if d.weekday() < 5:
                n += 1
        d += timedelta(days=1)
    if ref > as_of + timedelta(days=cap):
        n += 1  # walk capped: force "behind" past the horizon regardless
    return n


def get_latest_scores(max_age_days: int | None = None, today: _date | None = None) -> dict:
    """Latest scored day, as ``{(symbol, side): row}``.

    Returns ``{}`` when nothing has been scored, or when the newest scored day
    is more than ``max_age_days`` **trading sessions** behind ``today`` (issue
    #589 — sessions, not calendar days, so weekends and NSE holidays never
    burn the budget). Consumers MUST treat an empty result as *"we have no
    data"* and fail OPEN — never as *"everything is illiquid"*. Darkening a
    universe because a collection job failed is the failure shape issue #390
    was filed for.
    """
    try:
        newest = (
            db_session.query(OptionLiquidityDaily.as_of_date)
            .order_by(OptionLiquidityDaily.as_of_date.desc())
            .first()
        )
        if not newest:
            return {}
        as_of = newest[0]
        if max_age_days is not None:
            ref = today or datetime.now().date()
            behind = sessions_behind(as_of, ref)
            if behind > max_age_days:
                logger.warning(
                    "option_liquidity: newest score is %s, %d trading sessions behind "
                    "%s (max %d) — callers must fail OPEN",
                    as_of,
                    behind,
                    ref,
                    max_age_days,
                )
                return {}
        rows = (
            db_session.query(OptionLiquidityDaily)
            .filter(OptionLiquidityDaily.as_of_date == as_of)
            .all()
        )
        return {(r.symbol, r.side): _row_to_dict(r) for r in rows}
    finally:
        db_session.remove()


def get_history(symbol: str, side: str, limit: int = 40) -> list[dict]:
    """Most recent ``limit`` daily rows for one (symbol, side), newest first."""
    try:
        rows = (
            db_session.query(OptionLiquidityDaily)
            .filter(OptionLiquidityDaily.symbol == symbol, OptionLiquidityDaily.side == side)
            .order_by(OptionLiquidityDaily.as_of_date.desc())
            .limit(limit)
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        db_session.remove()


def get_daily_pctile_history(lookback_days: int, before: _date) -> dict:
    """``{(symbol, side): [daily_pctile, ...]}`` over the last ``lookback_days`` scored
    days strictly BEFORE ``before`` — the input to the rolling median.

    Ordered newest-first per key. Rows whose ``daily_pctile`` is NULL are skipped
    rather than counted as zero: a day we failed to measure is not a day the book
    was dead.
    """
    try:
        dates = [
            d[0]
            for d in db_session.query(OptionLiquidityDaily.as_of_date)
            .filter(OptionLiquidityDaily.as_of_date < before)
            .distinct()
            .order_by(OptionLiquidityDaily.as_of_date.desc())
            .limit(lookback_days)
            .all()
        ]
        if not dates:
            return {}
        rows = (
            db_session.query(OptionLiquidityDaily)
            .filter(OptionLiquidityDaily.as_of_date.in_(dates))
            .order_by(OptionLiquidityDaily.as_of_date.desc())
            .all()
        )
        out: dict = {}
        for r in rows:
            if r.daily_pctile is None:
                continue
            out.setdefault((r.symbol, r.side), []).append(r.daily_pctile)
        return out
    finally:
        db_session.remove()
