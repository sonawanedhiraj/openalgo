"""Persistence for the daily post-market review (``postmarket_review``).

One row per IST calendar date holding the day digest and the rendered operator
summary. Additive table in the main database (``db/openalgo.db``); this module
owns only its own table and is read-only on everything else.

Writes are **idempotent per date** — re-running the review for a date replaces
that date's row rather than appending, so a manual replay is safe and the table
stays one-row-per-day.
"""

import json
import os
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
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


class PostmarketReview(Base):
    """One row per reviewed IST date."""

    __tablename__ = "postmarket_review"

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_date = Column(String(10), nullable=False, unique=True)  # YYYY-MM-DD (IST)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)  # naive UTC
    is_trading_day = Column(Integer, nullable=True)  # 0/1, None when unknown
    digest_json = Column(Text, nullable=True)  # the full day digest
    sources_failed = Column(Text, nullable=True)  # JSON array of degraded sections
    summary_text = Column(Text, nullable=True)  # rendered operator summary
    elapsed_ms = Column(Integer, nullable=True)
    telegram_sent = Column(Integer, default=0)


Index("idx_postmarket_review_date", PostmarketReview.review_date)


def init_db():
    """Create the ``postmarket_review`` table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("postmarket_review table ready")
    except Exception as e:
        logger.exception(f"Failed to init postmarket_review table: {e}")


init_postmarket_review_db = init_db


def _row_to_dict(row: PostmarketReview) -> dict:
    return {
        "id": row.id,
        "review_date": row.review_date,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "is_trading_day": None if row.is_trading_day is None else bool(row.is_trading_day),
        "digest": json.loads(row.digest_json) if row.digest_json else {},
        "sources_failed": json.loads(row.sources_failed) if row.sources_failed else [],
        "summary_text": row.summary_text,
        "elapsed_ms": row.elapsed_ms,
        "telegram_sent": bool(row.telegram_sent),
    }


def upsert_review(
    review_date: str,
    digest: dict | None = None,
    summary_text: str | None = None,
    is_trading_day: bool | None = None,
    elapsed_ms: int | None = None,
    telegram_sent: bool | int = 0,
) -> int:
    """Replace the row for ``review_date``. Returns the row id (0 on failure).

    Delete-then-insert rather than an UPDATE so a replay always produces a row
    consistent with the digest it just built, with no stale columns surviving
    from an earlier partial run.
    """
    try:
        db_session.query(PostmarketReview).filter(
            PostmarketReview.review_date == review_date
        ).delete(synchronize_session=False)
        row = PostmarketReview(
            review_date=review_date,
            created_at=datetime.utcnow(),
            is_trading_day=None if is_trading_day is None else (1 if is_trading_day else 0),
            digest_json=json.dumps(digest or {}, default=str),
            sources_failed=json.dumps((digest or {}).get("sources_failed") or []),
            summary_text=summary_text,
            elapsed_ms=elapsed_ms,
            telegram_sent=1 if telegram_sent else 0,
        )
        db_session.add(row)
        db_session.commit()
        return row.id
    except Exception:
        db_session.rollback()
        logger.exception("failed to upsert postmarket_review row for %s", review_date)
        return 0
    finally:
        db_session.remove()


def get_review(review_date: str) -> dict | None:
    """The review row for ``review_date``, or None."""
    try:
        row = (
            db_session.query(PostmarketReview)
            .filter(PostmarketReview.review_date == review_date)
            .first()
        )
        return _row_to_dict(row) if row else None
    except Exception:
        logger.exception("failed to read postmarket_review row for %s", review_date)
        return None
    finally:
        db_session.remove()


def get_latest_review() -> dict | None:
    """The most recent review row, or None."""
    try:
        row = (
            db_session.query(PostmarketReview)
            .order_by(PostmarketReview.review_date.desc(), PostmarketReview.id.desc())
            .first()
        )
        return _row_to_dict(row) if row else None
    except Exception:
        logger.exception("failed to read latest postmarket_review row")
        return None
    finally:
        db_session.remove()
