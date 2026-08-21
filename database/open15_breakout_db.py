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

    # ---- broker fill reconciliation (issue #555) ------------------------- #
    # What the broker ACTUALLY filled, as opposed to the quote/tick prices the
    # decision was made on. Every other price column on this row is a
    # measurement of a decision moment (``trigger_price`` = the tick that fired
    # the gate, ``opt_entry_premium`` = the quote at that instant); these two are
    # the only ones that say what was transacted. Kept SEPARATE rather than
    # overwriting the quote columns, because ``fill - quote`` is the slippage the
    # strategy exists to measure, and collapsing them destroys it.
    entry_fill_price = Column(Float, nullable=True)  # broker average_price, entry leg
    exit_fill_price = Column(Float, nullable=True)  # broker average_price, exit leg
    entry_fill_qty = Column(Integer, nullable=True)  # filled qty (catches partial fills)
    exit_fill_qty = Column(Integer, nullable=True)
    # pending / reconciled / unavailable / not_applicable — ``unavailable`` means
    # we asked the broker and it could not tell us, which is a different fact
    # from "we have not asked yet" and must stay distinguishable.
    fill_reconcile_status = Column(String(16), nullable=True)
    # Which prices produced ``pnl``: ``fill`` once reconciled, else ``quote``.
    # NULL on pre-#555 rows — those are all quote-derived.
    pnl_source = Column(String(8), nullable=True)
    # The broker's own realized P&L for this symbol, for the cross-check. NULL
    # when the position book did not report one; never inferred.
    broker_pnl = Column(Float, nullable=True)
    # Notional size a NON-TRADED row is priced on (issue #555): unaffordable and
    # cap-skipped triggers are simulated at 1 lot so "would it have paid?" is
    # answerable. ``quantity`` stays 0 on those rows — it means "what was
    # ordered", and nothing was.
    sim_quantity = Column(Integer, nullable=True)

    # ATM option shadow trade (issue #435) — research-only: what 1 lot of the
    # ATM CE (L) / PE (S) did over the same entry->exit window. No orders.
    opt_symbol = Column(String(48), nullable=True)  # e.g. BAJAJ-AUTO28JUL2610700CE
    opt_lot_size = Column(Integer, nullable=True)
    opt_entry_premium = Column(Float, nullable=True)  # open of minute after trigger
    opt_exit_premium = Column(Float, nullable=True)  # 09:30 bar open
    opt_charges_inr = Column(Float, nullable=True)  # modelled option round-trip, 1 lot
    opt_pnl = Column(Float, nullable=True)  # NET premium pnl for 1 lot

    # Entry-timing sensitivity for REPLAY rows only (issue #600). A replayed
    # session is rebuilt from 1m bars, which can only trigger at a minute's
    # CLOSE, while the live gate fires somewhere inside that minute at a better
    # price. This is the trigger minute's option OPEN — the early end of the
    # range the true fill sits in.
    #
    # It is a PRICE, deliberately, not a second P&L. Deriving the optimistic net
    # by running ``net_pnl_of_row`` against this entry price keeps ONE net
    # convention (issue #552); storing a second pnl+charges pair would create a
    # parallel convention that rots the moment the charge model changes.
    # NULL on every non-replay row.
    opt_entry_premium_early = Column(Float, nullable=True)

    # contract liquidity at the two decision moments (issue #488) — measurement
    # only, nothing gates on them. Recorded so exit slippage can eventually be
    # regressed against the contract's own flow instead of a guessed threshold:
    # on 2026-07-28 every ex-ante metric ranked the two live trades backwards,
    # so the raw inputs are captured until the data justifies a rule.
    opt_entry_volume = Column(Integer, nullable=True)  # cumulative day volume at trigger
    opt_entry_oi = Column(Integer, nullable=True)  # open interest at trigger
    opt_exit_volume = Column(Integer, nullable=True)  # cumulative day volume at exit
    opt_exit_oi = Column(Integer, nullable=True)  # open interest at exit

    # ---- contract liquidity, part 2 (issue #555) ------------------------- #
    # The #488 columns above are RAW CONTRACT COUNTS, and lot sizes across this
    # universe differ by ~30x (HAL 150 vs SAIL 4700) — so they were never
    # comparable between contracts, which is the simplest explanation for #488's
    # note that "every ex-ante metric ranked the two live trades backwards".
    # Everything derived from them is normalized to LOTS or RUPEES; see
    # ``services/open15_liquidity.py``.
    #
    # The bid/ask pair arrives in the SAME quote response the strategy already
    # fetches at both decision moments — it was simply being discarded. It is the
    # one liquidity fact that directly costs money: the strategy sends MARKET
    # orders, so it pays the spread on entry and again on exit.
    opt_entry_bid = Column(Float, nullable=True)
    opt_entry_ask = Column(Float, nullable=True)
    opt_exit_bid = Column(Float, nullable=True)
    opt_exit_ask = Column(Float, nullable=True)
    # from the master contract; varies per contract (0.05 vs 0.01 observed), so a
    # spread is only comparable across contracts once expressed in ticks
    opt_tick_size = Column(Float, nullable=True)
    # per-minute [{"m": "09:21", "v": 3300, "oi": 72150}, ...] over the hold,
    # from the 1m bars the option-shadow already fetches (volume and oi were
    # being discarded). Two endpoint snapshots cannot show whether OI was
    # BUILDING or UNWINDING while the position was held; this can.
    opt_liquidity_path = Column(Text, nullable=True)

    # Gate 2, issue #583 — what the MARKET order was projected to pay by walking the
    # 5 visible ask levels at the trigger, measured from the MID (never the LTP).
    # ``opt_depth_exhausted`` means those levels could not fill the order at all, in
    # which case ``opt_impact_pct`` is computed on a PARTIAL fill and UNDERSTATES the
    # true cost. Recorded on skipped rows too, so a blocked entry can be scored later
    # against the ones that were allowed through.
    opt_impact_pct = Column(Float, nullable=True)
    opt_depth_levels_used = Column(Integer, nullable=True)
    opt_depth_exhausted = Column(Integer, nullable=True)
    # how this symbol got onto the watch list (issue #529): ``seed`` = the 09:16
    # gap ranking, ``rolling`` = appended by an intraday re-rank. Load-bearing
    # for the measurement — without it the two cohorts cannot be scored apart.
    # NULL on rows written before #529 shipped; read as ``seed``.
    watch_source = Column(String(8), nullable=True)
    # What capital the size was derived from (issue #643):
    #   ``slot``     — the full configured ``margin_per_slot``.
    #   ``residual`` — the cash left after earlier fills, less than a slot.
    # A residual row is REAL money and stays in real P&L, but it is a
    # DIFFERENT SIZE from every other row, so research that compares
    # per-trade outcomes across days has to be able to filter it out.
    # NULL on every row written before #643 = ``slot``.
    sizing_basis = Column(String(8), nullable=True)
    status = Column(String(16), default="open")  # open / closed / error / observe / rejected
    # Did an order actually fill (issue #548)? Four classes, and the difference
    # between them is the difference between money that moved and money that did
    # not — never collapse them into one number:
    #   ``real``  — the broker accepted the order.
    #   ``paper`` — the broker REJECTED the entry; every price/P&L field is a
    #               sandbox-equivalent simulation of what the trade would have
    #               done. An order WAS attempted.
    #   ``sim``   — no order was ever attempted (unaffordable / cap-skipped,
    #               issue #555). Priced at 1 lot purely to answer "would it have
    #               paid?". Provenance differs from ``paper``: nothing was sent,
    #               so nothing can have half-reached the exchange.
    #   ``shadow``— the side is switched OFF by ``trade_side``, so no order was
    #               ever attempted (issue #581). Priced at the FULL slot size a
    #               real entry would have used, because the whole point is to
    #               compare the excluded cohort against the traded one. Kept
    #               apart from ``sim`` deliberately: ``sim`` asks "was the budget
    #               the constraint?", ``shadow`` asks "does the signal work on
    #               the other side?" — one blended number answers neither.
    #   ``none``  — beyond the paper cap; deliberately left unpriced.
    # NULL on rows written before #548 shipped; read as ``real``.
    #
    # Load-bearing: ``mode`` still records what the run genuinely was (``live``)
    # — a paper row must never be disguised as a sandbox run, and neither paper
    # nor sim P&L may be summed into realized P&L (see ``total_realized_pnl``).
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
    # shadow-log the side ``trade_side`` excludes (issue #581). NULL = env
    # default (OFF). Places no orders — it only decides whether the excluded
    # side is still watched and journaled as ``fill='shadow'`` rows.
    shadow_excluded_side = Column(Integer, nullable=True)  # 0/1 (NULL = env default)
    shadow_max_trades = Column(Integer, nullable=True)  # own daily cap, clamped 0..10
    # rolling additive watch list (issue #529). NULL = env default; the cadence
    # and top-N are the operator-facing knobs edited from /open15_vol_breakout/logs.
    rolling_watchlist_enabled = Column(Integer, nullable=True)  # 0/1 (NULL = env default)
    rolling_cadence_s = Column(Integer, nullable=True)  # re-rank period, clamped 10..300
    rolling_top_n = Column(Integer, nullable=True)  # adds per side per cycle, clamped 1..10
    # option-liquidity gates (issue #583). NULL = env default. Thresholds are
    # PERCENTILES within that day's universe, not absolute rupee floors, so the gate
    # does not silently widen or vanish when market-wide activity shifts.
    option_liquidity_gate_enabled = Column(Integer, nullable=True)  # 0/1 - Gate 1
    option_liquidity_min_pctile = Column(Float, nullable=True)  # exclude below this
    option_liquidity_reentry_pctile = Column(Float, nullable=True)  # hysteresis band top
    option_liquidity_reentry_days = Column(Integer, nullable=True)  # clean sessions to return
    option_liquidity_min_days = Column(Integer, nullable=True)  # history before a score counts
    option_liquidity_max_staleness_days = Column(Integer, nullable=True)  # then fail OPEN
    option_liquidity_backfill_rank = Column(Integer, nullable=True)  # 0/1 - seed path only
    option_impact_gate_enabled = Column(Integer, nullable=True)  # 0/1 - Gate 2
    option_impact_max_pct = Column(Float, nullable=True)  # SEBI LES shape, our slot size
    # broker OI floor in LOTS (issue #595) — mirrors Zerodha's per-contract MIS
    # block (OI < 500 lots). NULL = env default (500); 0 = off. Absolute, not a
    # percentile: the broker's rule is absolute, and no relative rank can
    # reproduce it (KALYANKJIL at p96 was blocked on 2026-08-13).
    option_min_oi_lots = Column(Integer, nullable=True)
    # ATM lot-cost coverage ladder target % (issue #591). NULL = env default (90);
    # clamped 50..100 by the service. Observational only — nothing gates on it.
    coverage_target_pct = Column(Integer, nullable=True)
    # Residual-cash sizing (issue #643). NULL = env default (OFF). When on, an
    # entry that cannot afford a full ``margin_per_slot`` is sized against the
    # cash actually left in the account instead of being dropped — on
    # 2026-08-19 Rs39,730 sat idle while the day's third signal was skipped.
    residual_sizing_enabled = Column(Integer, nullable=True)  # 0/1
    residual_reserve_pct = Column(Float, nullable=True)  # headroom for charges
    residual_min_lots = Column(Integer, nullable=True)  # floor, 0 = no entry
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
            # broker fill reconciliation (issue #555)
            "entry_fill_price": "FLOAT",
            "exit_fill_price": "FLOAT",
            "entry_fill_qty": "INTEGER",
            "exit_fill_qty": "INTEGER",
            "fill_reconcile_status": "VARCHAR(16)",
            "pnl_source": "VARCHAR(8)",
            "broker_pnl": "FLOAT",
            "sim_quantity": "INTEGER",
            # contract liquidity, part 2 (issue #555)
            "opt_entry_bid": "FLOAT",
            "opt_entry_ask": "FLOAT",
            "opt_exit_bid": "FLOAT",
            "opt_exit_ask": "FLOAT",
            "opt_tick_size": "FLOAT",
            "opt_liquidity_path": "TEXT",
            "opt_impact_pct": "FLOAT",
            "opt_depth_levels_used": "INTEGER",
            "opt_depth_exhausted": "INTEGER",
            # issue #600 — replay rows only; NULL everywhere else. This belongs
            # to the TRADES table: it shipped in the config block by mistake
            # (#602), which no test could catch because tests build their tables
            # from the ORM model and only a pre-existing install runs this ALTER.
            "opt_entry_premium_early": "FLOAT",
            # issue #643 — NULL on every existing row, which reads as ``slot``
            "sizing_basis": "VARCHAR(8)",
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
            "option_liquidity_gate_enabled": "INTEGER",
            "option_liquidity_min_pctile": "FLOAT",
            "option_liquidity_reentry_pctile": "FLOAT",
            "option_liquidity_reentry_days": "INTEGER",
            "option_liquidity_min_days": "INTEGER",
            "option_liquidity_max_staleness_days": "INTEGER",
            "option_liquidity_backfill_rank": "INTEGER",
            "option_impact_gate_enabled": "INTEGER",
            "option_impact_max_pct": "FLOAT",
            # issue #595 — NULL resolves to the env default (500 lots)
            "option_min_oi_lots": "INTEGER",
            # issue #581 — both NULL on an existing install, which resolves to
            # the env default (OFF), so the next arm behaves exactly as before
            "shadow_excluded_side": "INTEGER",
            "shadow_max_trades": "INTEGER",
            # issue #591 — NULL resolves to the env default (90)
            "coverage_target_pct": "INTEGER",
            # issue #643 — all three NULL on an existing install, which
            # resolves to the env default (residual sizing OFF), so the next
            # arm behaves exactly as it did before
            "residual_sizing_enabled": "INTEGER",
            "residual_reserve_pct": "FLOAT",
            "residual_min_lots": "INTEGER",
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
            # issue #581 — None stays None so env can supply the default (OFF)
            "shadow_excluded_side": (
                None if row.shadow_excluded_side is None else bool(row.shadow_excluded_side)
            ),
            "shadow_max_trades": row.shadow_max_trades,
            # issue #583 — None stays None so env supplies the default
            "option_liquidity_gate_enabled": (
                None
                if row.option_liquidity_gate_enabled is None
                else bool(row.option_liquidity_gate_enabled)
            ),
            "option_liquidity_min_pctile": row.option_liquidity_min_pctile,
            "option_liquidity_reentry_pctile": row.option_liquidity_reentry_pctile,
            "option_liquidity_reentry_days": row.option_liquidity_reentry_days,
            "option_liquidity_min_days": row.option_liquidity_min_days,
            "option_liquidity_max_staleness_days": row.option_liquidity_max_staleness_days,
            "option_liquidity_backfill_rank": (
                None
                if row.option_liquidity_backfill_rank is None
                else bool(row.option_liquidity_backfill_rank)
            ),
            "option_impact_gate_enabled": (
                None
                if row.option_impact_gate_enabled is None
                else bool(row.option_impact_gate_enabled)
            ),
            "option_impact_max_pct": row.option_impact_max_pct,
            # issue #595 — None stays None so env supplies the default (500)
            "option_min_oi_lots": row.option_min_oi_lots,
            # issue #591 — None stays None so env supplies the default
            "coverage_target_pct": row.coverage_target_pct,
            # issue #643 — None stays None so env supplies the default (OFF)
            "residual_sizing_enabled": (
                None if row.residual_sizing_enabled is None else bool(row.residual_sizing_enabled)
            ),
            "residual_reserve_pct": row.residual_reserve_pct,
            "residual_min_lots": row.residual_min_lots,
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
    shadow_excluded_side: bool | None = None,
    shadow_max_trades: int | None = None,
    option_liquidity_gate_enabled: bool | None = None,
    option_liquidity_min_pctile: float | None = None,
    option_liquidity_reentry_pctile: float | None = None,
    option_liquidity_reentry_days: int | None = None,
    option_liquidity_min_days: int | None = None,
    option_liquidity_max_staleness_days: int | None = None,
    option_liquidity_backfill_rank: bool | None = None,
    option_impact_gate_enabled: bool | None = None,
    option_impact_max_pct: float | None = None,
    option_min_oi_lots: int | None = None,
    coverage_target_pct: int | None = None,
    residual_sizing_enabled: bool | None = None,
    residual_reserve_pct: float | None = None,
    residual_min_lots: int | None = None,
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
        row.shadow_excluded_side = (
            None if shadow_excluded_side is None else int(bool(shadow_excluded_side))
        )
        row.shadow_max_trades = shadow_max_trades
        row.option_liquidity_gate_enabled = (
            None
            if option_liquidity_gate_enabled is None
            else int(bool(option_liquidity_gate_enabled))
        )
        row.option_liquidity_min_pctile = option_liquidity_min_pctile
        row.option_liquidity_reentry_pctile = option_liquidity_reentry_pctile
        row.option_liquidity_reentry_days = option_liquidity_reentry_days
        row.option_liquidity_min_days = option_liquidity_min_days
        row.option_liquidity_max_staleness_days = option_liquidity_max_staleness_days
        row.option_liquidity_backfill_rank = (
            None
            if option_liquidity_backfill_rank is None
            else int(bool(option_liquidity_backfill_rank))
        )
        row.option_impact_gate_enabled = (
            None if option_impact_gate_enabled is None else int(bool(option_impact_gate_enabled))
        )
        row.option_impact_max_pct = option_impact_max_pct
        row.option_min_oi_lots = option_min_oi_lots
        row.coverage_target_pct = coverage_target_pct
        row.residual_sizing_enabled = (
            None if residual_sizing_enabled is None else int(bool(residual_sizing_enabled))
        )
        row.residual_reserve_pct = residual_reserve_pct
        row.residual_min_lots = residual_min_lots
        row.updated_by = updated_by
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("open15: config save failed")
        return False
    finally:
        db_session.remove()


# Every non-real fill class (issue #555 widened this from just ``paper``). A row
# is REAL unless it is explicitly marked as one of these — a NULL-tolerant
# predicate, so every row created before the column existed counts as real.
#
# The exclusion is an explicit LIST rather than ``!= 'paper'`` on purpose: that
# form silently reclassified every future class as REAL, so adding ``sim``
# (issue #555) would have folded simulated money straight into realized P&L and
# into tomorrow's compound position size. A new fill class must be added here.
# ``replay`` (issue #600) is a session the strategy never ran, rebuilt from 1m
# bars after the fact. Its P&L carries an entry-timing band wide enough to flip
# sign, so it must never reach compound sizing or any published figure.
NON_REAL_FILLS = ("paper", "sim", "none", "shadow", "replay")

_REAL_FILL = (Open15Trade.fill.is_(None)) | (Open15Trade.fill.notin_(NON_REAL_FILLS))


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


def _pnl_by_date(fill_class: str) -> dict[str, float]:
    """NET journal P&L per trade_date for one fill class (issues #548, #552, #555).

    ``fill_class`` is ``real`` (the NULL-tolerant real predicate) or an exact
    value from :data:`NON_REAL_FILLS`.
    """
    try:
        from sqlalchemy import func

        pred = _REAL_FILL if fill_class == "real" else (Open15Trade.fill == fill_class)
        rows = (
            db_session.query(Open15Trade.trade_date, func.sum(net_pnl_expr()))
            .filter(Open15Trade.pnl.isnot(None), pred)
            .group_by(Open15Trade.trade_date)
            .all()
        )
        return {d: round(float(v), 2) for d, v in rows if v is not None}
    except Exception:
        logger.exception("open15: per-date pnl aggregation failed (fill=%s)", fill_class)
        return {}
    finally:
        db_session.remove()


def trades_pnl_by_date() -> dict[str, float]:
    """NET REAL journal P&L per trade_date (charges deducted — issue #552).

    Paper and sim rows are excluded and reported separately by
    ``paper_pnl_by_date`` / ``sim_pnl_by_date`` — the history sidebar must never
    blend simulated money into a day's P&L.
    """
    return _pnl_by_date("real")


def paper_pnl_by_date() -> dict[str, float]:
    """NET PAPER journal P&L per trade_date (broker-rejected entries, #548)."""
    return _pnl_by_date("paper")


def sim_pnl_by_date() -> dict[str, float]:
    """NET SIM journal P&L per trade_date (issue #555).

    Triggers we never sent an order for — unaffordable or past the daily cap —
    priced at 1 lot. A THIRD bucket, not folded into ``paper``: "the broker
    blocked us" and "we could not afford it" are different claims about a day,
    and a single blended figure answers neither.
    """
    return _pnl_by_date("sim")


def shadow_pnl_by_date() -> dict[str, float]:
    """NET SHADOW journal P&L per trade_date (issue #581).

    Triggers on the side ``trade_side`` switched off, priced at the FULL slot
    size a real entry would have used. A FOURTH bucket for the same reason
    ``sim`` is a third one: "we could not afford it" and "we deliberately do not
    trade that side" are different claims, and the second is the one this
    strategy is collecting data to answer.

    These rows are counterfactual — no order was ever placed — so the figure is
    quote-priced and optimistic by roughly the round-trip spread. It must never
    be added to the real number.
    """
    return _pnl_by_date("shadow")


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
