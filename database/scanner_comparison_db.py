"""Persistence for the daily in-house-scanner-vs-Chartink comparison.

Additive table in the main database (``db/openalgo.db``) recording the verdict of
each ``services.scanner_comparison_eod_service`` run: for a given trading day, how
the in-house scanner's BUY/SELL hits compared against the Chartink lists that were
posted via webhook (recorded in ``scan_cycle``). One row per ``(date, screener_side)``.

This is the durable replacement for the old Cowork-side
``scanner-vs-chartink-daily-comparison`` scheduled task, which ran in a sandbox
without repo/folder access and silently failed (no comparison was ever persisted).
Moving it into an OpenAlgo APScheduler EOD job means the result is written here AND
sent to Telegram, every trading day, from inside the process that already owns the
data.

Read-only on every other module — this file owns only its own table. Writes are
idempotent per ``(date, screener_side)`` via delete-then-insert, so re-running the
EOD job (or the one-shot backfill) for the same day never duplicates rows.
"""

import json
import os
from datetime import datetime

import pytz
from sqlalchemy import (
    Column,
    Float,
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


class ScannerComparison(Base):
    """One row per ``(trading-day, screener_side)`` comparison verdict."""

    __tablename__ = "scanner_comparison"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_at = Column(String(40), nullable=False)  # IST ISO-8601 of the job run
    date = Column(String(10), nullable=False, index=True)  # 'YYYY-MM-DD' IST
    screener_side = Column(String(4), nullable=False)  # 'BUY' | 'SELL'
    inhouse_count = Column(Integer, nullable=False, default=0)
    chartink_count = Column(Integer, nullable=False, default=0)
    intersection_count = Column(Integer, nullable=False, default=0)
    # Jaccard = |inhouse ∩ chartink| / |inhouse ∪ chartink|. NULL when the
    # union is empty (undefined, e.g. a day with no hits on either side).
    jaccard = Column(Float, nullable=True)
    # ratio = recall against Chartink = intersection / chartink_count. NULL
    # when Chartink had no hits that day.
    ratio = Column(Float, nullable=True)
    false_positives_json = Column(Text, nullable=True)  # JSON: inhouse-only names
    false_negatives_json = Column(Text, nullable=True)  # JSON: chartink-only names
    tuning_suggestion = Column(Text, nullable=True)
    telegram_sent = Column(Integer, nullable=False, default=0)


# Composite index for the idempotent (date, side) lookup/delete path.
Index("idx_scanner_comparison_date_side", ScannerComparison.date, ScannerComparison.screener_side)


def init_db():
    """Create the ``scanner_comparison`` table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("scanner_comparison table ready")
    except Exception as e:
        logger.exception(f"Failed to init scanner_comparison table: {e}")


# Explicit alias for boot wiring that prefers the long name.
ensure_scanner_comparison_tables_exists = init_db


def _now_iso() -> str:
    return datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()


def _row_to_dict(row: ScannerComparison) -> dict:
    return {
        "id": row.id,
        "run_at": row.run_at,
        "date": row.date,
        "screener_side": row.screener_side,
        "inhouse_count": row.inhouse_count,
        "chartink_count": row.chartink_count,
        "intersection_count": row.intersection_count,
        "jaccard": row.jaccard,
        "ratio": row.ratio,
        "false_positives": json.loads(row.false_positives_json) if row.false_positives_json else [],
        "false_negatives": json.loads(row.false_negatives_json) if row.false_negatives_json else [],
        "tuning_suggestion": row.tuning_suggestion,
        "telegram_sent": bool(row.telegram_sent),
    }


def _session():
    """Resolve the live session from this module on each call.

    Mirrors the lazy-resolution pattern in ``services.scanner_service`` so tests
    can monkeypatch ``database.scanner_comparison_db.db_session`` cleanly.
    """
    return db_session


def upsert_comparison(
    date: str,
    screener_side: str,
    inhouse_count: int,
    chartink_count: int,
    intersection_count: int,
    jaccard: float | None,
    ratio: float | None,
    false_positives: list[str] | None = None,
    false_negatives: list[str] | None = None,
    tuning_suggestion: str | None = None,
    telegram_sent: bool | int = 0,
    run_at: str | None = None,
) -> int:
    """Insert one comparison row, replacing any existing row for the same
    ``(date, screener_side)``. Returns the new row id (0 on failure).

    Delete-then-insert keeps the EOD job and the one-shot backfill idempotent:
    re-running for the same day overwrites rather than appends.
    """
    side = (screener_side or "").upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"screener_side must be BUY or SELL, got {screener_side!r}")

    sess = _session()
    try:
        sess.query(ScannerComparison).filter(
            ScannerComparison.date == date,
            ScannerComparison.screener_side == side,
        ).delete(synchronize_session=False)

        row = ScannerComparison(
            run_at=run_at or _now_iso(),
            date=date,
            screener_side=side,
            inhouse_count=int(inhouse_count),
            chartink_count=int(chartink_count),
            intersection_count=int(intersection_count),
            jaccard=jaccard,
            ratio=ratio,
            false_positives_json=json.dumps(sorted(false_positives or [])),
            false_negatives_json=json.dumps(sorted(false_negatives or [])),
            tuning_suggestion=tuning_suggestion,
            telegram_sent=1 if telegram_sent else 0,
        )
        sess.add(row)
        sess.commit()
        return row.id
    except Exception:
        sess.rollback()
        logger.exception("failed to upsert scanner_comparison row")
        return 0
    finally:
        sess.remove()


def get_comparisons_for_date(date: str) -> list[dict]:
    """Return the BUY/SELL comparison rows for ``date`` (BUY first), newest run wins."""
    sess = _session()
    try:
        rows = (
            sess.query(ScannerComparison)
            .filter(ScannerComparison.date == date)
            .order_by(ScannerComparison.screener_side.asc())
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        sess.remove()


def get_recent_comparisons(limit: int = 20) -> list[dict]:
    """Up to ``limit`` most-recent comparison rows, newest first."""
    sess = _session()
    try:
        rows = (
            sess.query(ScannerComparison)
            .order_by(ScannerComparison.date.desc(), ScannerComparison.id.desc())
            .limit(limit)
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        sess.remove()
