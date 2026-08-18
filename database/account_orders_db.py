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
"""

import os
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine
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
        logger.info("account_orders table ready")
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
    }


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
