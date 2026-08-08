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

    # Which book the order actually routed to — 'sandbox' | 'live' (issue #568).
    # Load-bearing for the strategies dashboard: without it the whole journal is
    # attributed to whatever mode the strategy is in *today*, so flipping a
    # strategy live silently re-labels its entire sandbox history as live
    # performance. Nullable because rows predating the column exist; readers
    # MUST treat NULL as 'sandbox' via ``mode_of_row`` rather than as 'unknown'
    # — every pre-#568 row was written while the engine defaulted to sandbox.
    mode = Column(String(16), nullable=True)

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
    #
    # ``pnl`` is GROSS — (exit - entry) * qty — and ``charges_inr`` carries the
    # modelled round-trip cost separately (issue #579), exactly as
    # ``open15_trades`` splits them. Never report ``pnl`` as "the P&L": on the
    # 231 sandbox trades to 2026-08-07 gross read +Rs8,740 while charges were
    # Rs17,385, so the honest net was -Rs8,645 — the dashboard showed a
    # profitable strategy that was losing money. Route every consumer through
    # ``net_pnl_expr`` / ``net_pnl_of_row``.
    pnl = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    charges_inr = Column(Float, nullable=True)
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
        Index("idx_trade_journal_mode", "mode"),
    )


#: Books an order can route to. Mirrors ``services.mode_service`` vocabulary.
MODE_SANDBOX = "sandbox"
MODE_LIVE = "live"

#: Rows written before the ``mode`` column existed (issue #568) carry NULL. The
#: simplified engine ran ``SIMPLIFIED_ENGINE_MODE=sandbox`` for the whole of that
#: history, so NULL means sandbox — never "unknown". Resolving it anywhere else
#: would re-introduce the leak the column exists to prevent.
DEFAULT_MODE = MODE_SANDBOX


def net_pnl_expr():
    """SQL expression for a row's NET P&L — the ONE P&L convention (issue #579).

    ``pnl`` is gross and the modelled MIS round-trip charges live separately in
    ``charges_inr``, so every consumer reporting "the P&L" must deduct them.
    This is the same split ``open15_trades`` uses, and the same lesson: before
    #552 four open15 consumers derived net independently and three disagreed.
    Here the failure was worse — nothing deducted charges at all, so the
    strategies dashboard reported **+Rs8,740** on 231 sandbox trades whose true
    net was **-Rs8,645** (charges 199% of gross). The sign was wrong, not just
    the magnitude.

    A NULL ``charges_inr`` coalesces to 0, i.e. net degrades to gross rather
    than to NULL — an un-stamped row must not blank out a whole aggregate.
    Such rows are the pre-#579 backlog and are backfilled at migration time.

    Never write ``sum(pnl)`` against this journal again — route through here or
    :func:`net_pnl_of_row`.
    """
    from sqlalchemy import func

    return TradeJournal.pnl - func.coalesce(TradeJournal.charges_inr, 0.0)


def net_pnl_of_row(row) -> float | None:
    """Python-side twin of :func:`net_pnl_expr` for an ORM row / mapping.

    Returns ``None`` when the row has no gross P&L (still open, or an unpriced
    exit) so callers can skip it rather than count an open position as a
    Rs0 scratch.
    """
    get = row.get if isinstance(row, dict) else lambda k, d=None: getattr(row, k, d)
    gross = get("pnl")
    if gross is None:
        return None
    return float(gross) - float(get("charges_inr") or 0.0)


def compute_charges_for_row(row) -> float | None:
    """Modelled MIS round-trip charges for a closed row, or None if not derivable.

    Delegates to the engine's own ``compute_zerodha_intraday_charges`` — the
    model already calibrated against Kite's ``/charges/orders`` endpoint (it
    carries the NBCC per-leg-brokerage-cap correction). Deliberately NOT a second
    implementation: a journal that disagreed with the engine about cost would be
    the #552 divergence all over again.
    """
    get = row.get if isinstance(row, dict) else lambda k, d=None: getattr(row, k, d)
    entry, exit_px, qty = get("entry_price"), get("exit_price"), get("quantity")
    if entry is None or exit_px is None or not qty:
        return None
    try:
        from services.simplified_stock_engine_core import compute_zerodha_intraday_charges

        q = abs(int(qty))
        buy_value = float(entry) * q
        sell_value = float(exit_px) * q
        return round(compute_zerodha_intraday_charges(buy_value, sell_value).total, 2)
    except Exception:
        logger.exception("trade_journal: charge computation failed")
        return None


def mode_of_row(row) -> str:
    """Resolve a journal row's book, treating NULL/blank as ``sandbox``.

    The single definition of that fallback — callers must not re-derive it, for
    the same reason ``net_pnl_expr``/``net_pnl_of_row`` are centralized in
    ``open15_breakout_db`` (issue #552): three call sites deriving one convention
    independently is how the dashboard and the logs page ended up disagreeing.
    """
    return (getattr(row, "mode", None) or DEFAULT_MODE).strip().lower() or DEFAULT_MODE


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
    pending = {"ltp_at_signal": "REAL", "mode": "VARCHAR(16)", "charges_inr": "REAL"}
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
            continue
        if name == "mode":
            _backfill_mode()

    # Costing runs on EVERY boot, not only in the branch that just added the
    # column. Tying it to column-creation is fragile: on 2026-08-08 the dev
    # server auto-reloaded midway through this change, applied the ALTER, and
    # left every row unstamped forever — the `continue` above meant the backfill
    # could never fire again. It is idempotent and cheap (one indexed filter on
    # `charges_inr IS NULL`), so an unconditional call is strictly safer and
    # also repairs rows imported by any other path.
    backfill_charges()


def _backfill_mode() -> None:
    """One-shot: stamp pre-#568 rows as ``sandbox``.

    Runs only in the branch that just *added* the column, so it can never
    re-stamp a row a later live session wrote. Verified before shipping: the
    ``strategy_mode`` row for ``simplified_engine`` has read ``sandbox`` since
    2026-06-12 and was never flipped, and it is the only strategy with journal
    rows — so every existing row genuinely is a sandbox trade.

    ``mode_of_row`` already resolves NULL to sandbox, so this is belt-and-braces;
    its real value is making raw SQL (``GROUP BY mode``) agree with the ORM
    readers instead of silently bucketing history under NULL.
    """
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            res = conn.execute(
                text(f"UPDATE trade_journal SET mode = '{MODE_SANDBOX}' WHERE mode IS NULL")
            )
        logger.info("trade_journal: backfilled mode=sandbox on %s legacy row(s)", res.rowcount)
    except Exception as e:
        logger.warning("trade_journal: mode backfill failed (readers fall back): %s", e)


def backfill_charges() -> int:
    """Stamp ``charges_inr`` on every closed row that lacks it. Returns the count.

    Deterministic — charges are a pure function of entry price, exit price and
    quantity — so this is safe to re-run and is idempotent by the
    ``charges_inr IS NULL`` predicate. Called once when the column is first
    added; also exposed for an operator re-run after an import.

    Rows that are still open, or whose exit was never priced (the #350
    watchdog-stamped class), are skipped rather than stamped 0 — a fabricated
    zero cost would be indistinguishable from a genuinely free trade.
    """
    stamped = 0
    sess = db_session()
    try:
        rows = (
            sess.query(TradeJournal)
            .filter(
                TradeJournal.charges_inr.is_(None),
                TradeJournal.pnl.isnot(None),
                TradeJournal.entry_price.isnot(None),
                TradeJournal.exit_price.isnot(None),
            )
            .all()
        )
        for row in rows:
            c = compute_charges_for_row(row)
            if c is not None:
                row.charges_inr = c
                stamped += 1
        sess.commit()
        logger.info("trade_journal: backfilled charges_inr on %s closed row(s)", stamped)
    except Exception as e:
        logger.warning("trade_journal: charges backfill failed: %s", e)
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        try:
            sess.remove()
        except Exception:
            pass
    return stamped


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
        "mode": mode_of_row(row),
        "regime_snapshot": row.regime_snapshot,
        "nifty_pct_at_entry": row.nifty_pct_at_entry,
        "india_vix_at_entry": row.india_vix_at_entry,
        "exited_at": row.exited_at,
        "exit_price": row.exit_price,
        "exit_order_id": row.exit_order_id,
        "exit_reason": row.exit_reason,
        "pnl": row.pnl,
        "pnl_pct": row.pnl_pct,
        # gross (`pnl`), modelled cost, and the ONE net definition — consumers
        # must read `net_pnl`, never `pnl` (issue #579).
        "charges_inr": row.charges_inr,
        "net_pnl": net_pnl_of_row(row),
        "hold_duration_seconds": row.hold_duration_seconds,
        "notes": row.notes,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
