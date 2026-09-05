"""Mirror-order journal (``account_orders``) — multi-account Phase 2 (issue #474).

One row per ATTEMPTED child mirror order, whatever the outcome. This is the
audit trail for the fan-out in ``services/account_fanout_service.py`` and the
data source for the Phase-3 orderbook card / EOD summary. Child FILLS live in
the child's broker books — OpenAlgo does not track child positions.

``status`` values:
- ``placed`` — broker accepted; ``broker_orderid`` set
- ``rejected`` — broker refused; ``error_text`` carries the broker message
- ``skipped_no_session`` — child had no fresh ``acct:<id>`` auth token
- ``skipped_zero_qty`` — capital scaling rounded to 0 shares/lots
- ``error`` — unexpected exception in the mirror attempt

Additive table in the main database. NullPool per the project's SQLite rule.

Issue #700 adds two things on the same engine:

- **Fill columns** on ``account_orders`` (``fill_price`` / ``fill_qty`` /
  ``fill_at`` / ``charges_inr`` / ``charges_source``) — what the broker
  actually transacted, written ONLY from the child's own tradebook (or a
  one-time Console tradebook import), never inferred from ``sizing_price``.
- **``account_daily_pnl``** — one row per (account, IST trade date, strategy):
  the realized net P&L paired from those fills. Kite serves the tradebook for
  TODAY only, so this table is the only durable record of a child's P&L.
"""

import os
from datetime import date, datetime, time, timedelta

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker
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

VALID_STATUSES = (
    "placed",
    "rejected",
    "skipped_no_session",
    "skipped_zero_qty",
    "skipped_no_position",
    "skipped_no_capital",
    "skipped_no_quote",
    # the child cannot pay for the sized order (issue #637) — distinct from
    # skipped_zero_qty, which means the per-trade CAP could not buy one unit
    "skipped_insufficient_funds",
    "error",
)


class AccountOrder(Base):
    """One attempted child mirror order."""

    __tablename__ = "account_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False, index=True)
    strategy_name = Column(String(64), nullable=False)
    symbol = Column(String(64), nullable=False)
    exchange = Column(String(16), nullable=False)
    action = Column(String(8), nullable=False)
    product = Column(String(8), nullable=True)
    parent_qty = Column(Integer, nullable=False)
    child_qty = Column(Integer, nullable=False, default=0)
    factor = Column(Float, nullable=True)  # retired ratio audit (pre-#496 rows)
    sizing_price = Column(Float, nullable=True)  # issue #496: price used to size
    parent_orderid = Column(String(64), nullable=True)
    status = Column(String(24), nullable=False, index=True)
    broker_orderid = Column(String(64), nullable=True)
    error_text = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    # issue #700 — what the broker actually transacted (#626 vocabulary).
    # fill_qty is read BY PRESENCE: 0 is the answer on an unfilled order.
    fill_price = Column(Float, nullable=True)  # volume-weighted across partials (#641)
    fill_qty = Column(Integer, nullable=True)
    fill_at = Column(DateTime, nullable=True)  # broker fill timestamp, naive UTC
    charges_inr = Column(Float, nullable=True)  # this leg's charges
    charges_source = Column(String(16), nullable=True)  # 'broker' | 'modelled'


CHARGES_SOURCES = ("broker", "modelled", "mixed")
CAPTURE_SOURCES = ("tradebook", "console_csv")


class AccountDailyPnl(Base):
    """Realized net P&L of one child account, one strategy, one IST trading day.

    ``realized_gross`` is FIFO-paired from the child's own fills on THIS
    strategy's mirror rows only. ``book_realised`` is the broker's whole-account
    ``realised`` for the same symbols (manual trades included) — a cross-check
    that is NEVER summed into the strategy figure. Absence of a row means
    "not captured", which the UI must never render as ₹0.
    """

    __tablename__ = "account_daily_pnl"
    __table_args__ = (
        UniqueConstraint("account_id", "trade_date", "strategy_name", name="uq_account_day_strat"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    strategy_name = Column(String(64), nullable=False, index=True)
    realized_gross = Column(Float, nullable=False, default=0.0)
    charges_inr = Column(Float, nullable=False, default=0.0)
    charges_source = Column(String(16), nullable=True)
    realized_net = Column(Float, nullable=False, default=0.0)
    n_round_trips = Column(Integer, nullable=False, default=0)
    n_fills = Column(Integer, nullable=False, default=0)
    n_open_legs = Column(Integer, nullable=False, default=0)
    book_realised = Column(Float, nullable=True)
    capture_source = Column(String(16), nullable=False, default="tradebook")
    finalized = Column(Boolean, nullable=False, default=False)
    captured_at = Column(DateTime, nullable=False, default=datetime.utcnow)


def _migrate_add_fill_columns() -> None:
    """Idempotent boot ALTER for installs created before issue #700."""
    from sqlalchemy import text

    wanted = {
        "fill_price": "FLOAT",
        "fill_qty": "INTEGER",
        "fill_at": "DATETIME",
        "charges_inr": "FLOAT",
        "charges_source": "VARCHAR(16)",
    }
    try:
        with engine.connect() as conn:
            cols = [
                row[1] for row in conn.execute(text("PRAGMA table_info(account_orders)")).fetchall()
            ]
            if not cols:
                return
            added = []
            for name, ddl in wanted.items():
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE account_orders ADD COLUMN {name} {ddl}"))
                    added.append(name)
            if added:
                conn.commit()
                logger.info("account_orders fill columns added (issue #700): %s", added)
    except Exception:
        logger.exception("account_orders fill-column migration failed")


def _migrate_add_sizing_price() -> None:
    """Idempotent boot ALTER for installs created before issue #496."""
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            cols = [
                row[1] for row in conn.execute(text("PRAGMA table_info(account_orders)")).fetchall()
            ]
            if cols and "sizing_price" not in cols:
                conn.execute(text("ALTER TABLE account_orders ADD COLUMN sizing_price FLOAT"))
                conn.commit()
                logger.info("account_orders.sizing_price column added (issue #496)")
    except Exception:
        logger.exception("sizing_price migration failed")


def init_db():
    """Create the account_orders table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        _migrate_add_sizing_price()
        _migrate_add_fill_columns()
        logger.info("account_orders + account_daily_pnl tables ready")
    except Exception as e:
        logger.exception(f"Failed to init account_orders table: {e}")


ensure_account_orders_tables_exists = init_db


def _row_to_dict(row: AccountOrder) -> dict:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "strategy_name": row.strategy_name,
        "symbol": row.symbol,
        "exchange": row.exchange,
        "action": row.action,
        "product": row.product,
        "parent_qty": row.parent_qty,
        "child_qty": row.child_qty,
        "factor": row.factor,
        "sizing_price": row.sizing_price,
        "parent_orderid": row.parent_orderid,
        "status": row.status,
        "broker_orderid": row.broker_orderid,
        "error_text": row.error_text,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "fill_price": row.fill_price,
        "fill_qty": row.fill_qty,
        "fill_at": row.fill_at.isoformat() if row.fill_at else None,
        "charges_inr": row.charges_inr,
        "charges_source": row.charges_source,
    }


def ist_day_utc_window(day: date) -> tuple[datetime, datetime]:
    """``[start, end)`` in naive UTC for one IST calendar day.

    ``created_at``/``fill_at`` are naive UTC (repo contract); the IST day
    [00:00, 24:00) is the UTC window [D-1 18:30, D 18:30).
    """
    start = datetime.combine(day, time(0, 0)) - timedelta(hours=5, minutes=30)
    return start, start + timedelta(days=1)


def ist_date_of_utc(ts: datetime | None) -> date | None:
    """IST calendar date of a naive-UTC timestamp."""
    if ts is None:
        return None
    return (ts + timedelta(hours=5, minutes=30)).date()


def set_fill(
    row_id: int,
    *,
    fill_price: float,
    fill_qty: int,
    fill_at: datetime | None,
    charges_inr: float | None = None,
    charges_source: str | None = None,
) -> bool:
    """Record what the broker transacted on one mirror row (issue #700).

    Only ever called with evidence from the child's own tradebook (or the
    Console export). A journal write failure is an ERROR, not a warning — a
    lost fill silently under-reports the child's realized P&L.
    """
    try:
        row = db_session.query(AccountOrder).filter(AccountOrder.id == row_id).first()
        if row is None:
            return False
        row.fill_price = float(fill_price)
        row.fill_qty = int(fill_qty)
        row.fill_at = fill_at
        if charges_inr is not None:
            row.charges_inr = float(charges_inr)
            row.charges_source = charges_source
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("account_orders: fill update failed for row %s", row_id)
        return False
    finally:
        db_session.remove()


def placed_rows_in_window(
    account_id: int,
    start_utc: datetime,
    end_utc: datetime,
    strategy_name: str | None = None,
) -> list[dict]:
    """``placed`` mirror rows (the only ones that can have filled) created in
    ``[start_utc, end_utc)``, oldest first. Fail-open to ``[]``."""
    try:
        q = db_session.query(AccountOrder).filter(
            AccountOrder.account_id == account_id,
            AccountOrder.status == "placed",
            AccountOrder.created_at >= start_utc,
            AccountOrder.created_at < end_utc,
        )
        if strategy_name is not None:
            q = q.filter(AccountOrder.strategy_name == strategy_name)
        return [_row_to_dict(r) for r in q.order_by(AccountOrder.id).all()]
    except Exception:
        logger.exception("placed_rows_in_window read failed — failing open to []")
        return []
    finally:
        db_session.remove()


def rows_by_broker_orderids(account_id: int, orderids: list[str]) -> dict[str, dict]:
    """``{broker_orderid: row}`` for the given ids on one account (any status).

    The Console-import join: a family member's own trades have order ids that
    match nothing here and are therefore ignored.
    """
    if not orderids:
        return {}
    try:
        rows = (
            db_session.query(AccountOrder)
            .filter(
                AccountOrder.account_id == account_id,
                AccountOrder.broker_orderid.in_([str(o) for o in orderids]),
            )
            .all()
        )
        return {str(r.broker_orderid): _row_to_dict(r) for r in rows}
    finally:
        db_session.remove()


def _daily_to_dict(row: AccountDailyPnl) -> dict:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "trade_date": row.trade_date.isoformat() if row.trade_date else None,
        "strategy_name": row.strategy_name,
        "realized_gross": row.realized_gross,
        "charges_inr": row.charges_inr,
        "charges_source": row.charges_source,
        "realized_net": row.realized_net,
        "n_round_trips": row.n_round_trips,
        "n_fills": row.n_fills,
        "n_open_legs": row.n_open_legs,
        "book_realised": row.book_realised,
        "capture_source": row.capture_source,
        "finalized": bool(row.finalized),
        "captured_at": row.captured_at.isoformat() if row.captured_at else None,
    }


def upsert_daily_pnl(
    account_id: int,
    trade_date: date,
    strategy_name: str,
    *,
    realized_gross: float,
    charges_inr: float,
    charges_source: str | None,
    n_round_trips: int,
    n_fills: int,
    n_open_legs: int,
    book_realised: float | None,
    capture_source: str,
    finalized: bool,
) -> dict | None:
    """Insert or replace the day row. Idempotent: re-capturing the same day
    reaches the same values. ``realized_net`` is derived HERE and only here
    (the #552 one-definition rule)."""
    if capture_source not in CAPTURE_SOURCES:
        capture_source = "tradebook"
    try:
        row = (
            db_session.query(AccountDailyPnl)
            .filter(
                AccountDailyPnl.account_id == account_id,
                AccountDailyPnl.trade_date == trade_date,
                AccountDailyPnl.strategy_name == strategy_name,
            )
            .first()
        )
        if row is None:
            row = AccountDailyPnl(
                account_id=account_id, trade_date=trade_date, strategy_name=strategy_name
            )
            db_session.add(row)
        # A finalized row is never demoted to provisional by a later
        # provisional pass (e.g. a page-open recapture after 15:35).
        row.finalized = bool(finalized) or bool(row.finalized)
        row.realized_gross = round(float(realized_gross), 2)
        row.charges_inr = round(float(charges_inr), 2)
        row.charges_source = charges_source
        row.realized_net = round(float(realized_gross) - float(charges_inr), 2)
        row.n_round_trips = int(n_round_trips)
        row.n_fills = int(n_fills)
        row.n_open_legs = int(n_open_legs)
        row.book_realised = book_realised
        row.capture_source = capture_source
        row.captured_at = datetime.utcnow()
        db_session.commit()
        return _daily_to_dict(row)
    except Exception:
        db_session.rollback()
        logger.exception(
            "account_daily_pnl upsert failed (account=%s date=%s strategy=%s)",
            account_id,
            trade_date,
            strategy_name,
        )
        return None
    finally:
        db_session.remove()


def list_daily_pnl(
    strategy_name: str,
    account_id: int | None = None,
    since: date | None = None,
    until: date | None = None,
) -> list[dict]:
    """Day rows for one strategy, oldest first. Fail-open to ``[]``."""
    try:
        q = db_session.query(AccountDailyPnl).filter(AccountDailyPnl.strategy_name == strategy_name)
        if account_id is not None:
            q = q.filter(AccountDailyPnl.account_id == account_id)
        if since is not None:
            q = q.filter(AccountDailyPnl.trade_date >= since)
        if until is not None:
            q = q.filter(AccountDailyPnl.trade_date <= until)
        return [
            _daily_to_dict(r)
            for r in q.order_by(AccountDailyPnl.trade_date, AccountDailyPnl.account_id).all()
        ]
    except Exception:
        logger.exception("list_daily_pnl read failed — failing open to []")
        return []
    finally:
        db_session.remove()


def get_daily_pnl(account_id: int, trade_date: date, strategy_name: str) -> dict | None:
    try:
        row = (
            db_session.query(AccountDailyPnl)
            .filter(
                AccountDailyPnl.account_id == account_id,
                AccountDailyPnl.trade_date == trade_date,
                AccountDailyPnl.strategy_name == strategy_name,
            )
            .first()
        )
        return _daily_to_dict(row) if row else None
    finally:
        db_session.remove()


def record_mirror_attempt(
    account_id: int,
    strategy_name: str,
    symbol: str,
    exchange: str,
    action: str,
    parent_qty: int,
    child_qty: int,
    status: str,
    product: str | None = None,
    factor: float | None = None,
    sizing_price: float | None = None,
    parent_orderid: str | None = None,
    broker_orderid: str | None = None,
    error_text: str | None = None,
) -> dict | None:
    """Journal one mirror attempt. Never raises — a journal failure must not
    disturb the fan-out (the attempt itself already happened)."""
    if status not in VALID_STATUSES:
        status = "error"
    try:
        row = AccountOrder(
            account_id=account_id,
            strategy_name=strategy_name,
            symbol=symbol,
            exchange=exchange,
            action=action,
            product=product,
            parent_qty=int(parent_qty),
            child_qty=int(child_qty),
            factor=factor,
            sizing_price=sizing_price,
            parent_orderid=parent_orderid,
            status=status,
            broker_orderid=broker_orderid,
            error_text=(error_text or None),
        )
        db_session.add(row)
        db_session.commit()
        return _row_to_dict(row)
    except Exception:
        db_session.rollback()
        # ERROR (not warning): a lost mirror-audit row is a real observability
        # gap on a live-order path (journal-failure-warning-only rule).
        logger.exception(
            f"Failed to journal mirror attempt (account={account_id}, {symbol} {action})"
        )
        return None
    finally:
        db_session.remove()


def last_opposite_attempt_status(
    account_id: int,
    symbol: str,
    exchange: str,
    strategy_name: str,
    action: str,
    lookback_days: int = 7,
) -> str | None:
    """Status of the child's most recent OPPOSITE-action mirror attempt.

    The rejected-entry exit guard (issue #478): before scaling an order for a
    FLAT child, the fan-out asks whether the opposite side (the would-be entry)
    was recently attempted and failed — if so, this "exit" has nothing to exit
    and must be skipped, not scaled into a fresh naked position. Returns None
    when no opposite attempt exists in the lookback window (a genuine opening
    order). Fail-open (None) on any read error.
    """
    opposite = "SELL" if (action or "").upper() == "BUY" else "BUY"
    try:
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(days=lookback_days)
        row = (
            db_session.query(AccountOrder)
            .filter(
                AccountOrder.account_id == account_id,
                AccountOrder.symbol == symbol,
                AccountOrder.exchange == exchange,
                AccountOrder.strategy_name == strategy_name,
                AccountOrder.action == opposite,
                AccountOrder.created_at >= cutoff,
            )
            .order_by(AccountOrder.id.desc())
            .first()
        )
        return row.status if row else None
    except Exception:
        logger.exception("last_opposite_attempt_status read failed — failing open")
        return None
    finally:
        db_session.remove()


def update_status(row_id: int, *, status: str, error_text: str | None = None) -> bool:
    """Correct one mirror attempt's outcome (issue #637).

    Used by the fill reconciliation to turn a `placed` row the broker actually
    refused into `rejected` with the broker's own reason. An unknown status is
    REFUSED here rather than silently coerced: `record_mirror_attempt` maps a
    bad status to `error`, which is right when journalling a live attempt but
    wrong when correcting a record — it would replace one wrong answer with
    another.
    """
    if status not in VALID_STATUSES:
        logger.error("account_orders: refusing to set unknown status %r on row %s", status, row_id)
        return False
    try:
        row = db_session.query(AccountOrder).filter(AccountOrder.id == row_id).first()
        if row is None:
            return False
        row.status = status
        if error_text is not None:
            row.error_text = error_text
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("account_orders: status update failed for row %s", row_id)
        return False
    finally:
        db_session.remove()


def todays_placed_rows(
    strategy_name: str,
    account_id: int | None = None,
    symbol: str | None = None,
) -> list[dict]:
    """Today's (IST trading day) ``placed`` mirror rows for one strategy.

    Feed for the orphan-flatten sweep and the duplicate-exit guard (issue
    #659). ``created_at`` is naive UTC; the IST day [00:00, 24:00) maps to the
    UTC window [D-1 18:30, D 18:30). Only ``placed`` rows count — a skipped or
    rejected mirror opened nothing, so it must not enter the net. Fail-open to
    ``[]``: a read failure must never manufacture a phantom flatten.
    """
    from datetime import time, timedelta

    try:
        ist_now = datetime.utcnow() + timedelta(hours=5, minutes=30)
        day_start_utc = datetime.combine(ist_now.date(), time(0, 0)) - timedelta(
            hours=5, minutes=30
        )
        q = db_session.query(AccountOrder).filter(
            AccountOrder.strategy_name == strategy_name,
            AccountOrder.status == "placed",
            AccountOrder.created_at >= day_start_utc,
            AccountOrder.created_at < day_start_utc + timedelta(days=1),
        )
        if account_id is not None:
            q = q.filter(AccountOrder.account_id == account_id)
        if symbol is not None:
            q = q.filter(AccountOrder.symbol == symbol)
        return [_row_to_dict(r) for r in q.order_by(AccountOrder.id).all()]
    except Exception:
        logger.exception("todays_placed_rows read failed — failing open to []")
        return []
    finally:
        db_session.remove()


def list_orders(date_utc: str | None = None, account_id: int | None = None) -> list[dict]:
    """Mirror attempts, newest first. ``date_utc`` filters by YYYY-MM-DD prefix."""
    try:
        q = db_session.query(AccountOrder)
        if account_id is not None:
            q = q.filter(AccountOrder.account_id == account_id)
        if date_utc:
            day_start = datetime.fromisoformat(f"{date_utc}T00:00:00")
            day_end = datetime.fromisoformat(f"{date_utc}T23:59:59.999999")
            q = q.filter(AccountOrder.created_at >= day_start, AccountOrder.created_at <= day_end)
        return [_row_to_dict(r) for r in q.order_by(AccountOrder.id.desc()).limit(500)]
    finally:
        db_session.remove()
