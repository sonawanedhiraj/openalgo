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
    # Phase 2 (#532) — expectation-contract verdicts. Added after the table
    # shipped in #511, so init_db migrates existing installs in place.
    violations_json = Column(Text, nullable=True)  # JSON array of Violation dicts
    n_violations = Column(Integer, nullable=True)
    contracts_json = Column(Text, nullable=True)  # counts + unknown-contract list
    # Phase 3 (#534) — LLM triage over the violations.
    triage_json = Column(Text, nullable=True)  # day assessment + per-violation triage
    llm_status = Column(String(32), nullable=True)  # ok | skipped_* | timeout | ...
    llm_latency_ms = Column(Integer, nullable=True)


Index("idx_postmarket_review_date", PostmarketReview.review_date)


# Columns added after the table first shipped, migrated in on boot.
_ADDED_COLUMNS = (
    ("violations_json", "TEXT"),
    ("n_violations", "INTEGER"),
    ("contracts_json", "TEXT"),
    ("triage_json", "TEXT"),
    ("llm_status", "TEXT"),
    ("llm_latency_ms", "INTEGER"),
)


def _ensure_columns():
    """Add post-#511 columns to an existing ``postmarket_review`` table.

    ``create_all`` only creates missing *tables*, never missing columns, so an
    install that already ran #511 would otherwise keep the old schema and every
    write of the new fields would fail.
    """
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            existing = {
                row[1] for row in conn.execute(text("PRAGMA table_info(postmarket_review)"))
            }
            for name, sql_type in _ADDED_COLUMNS:
                if name not in existing:
                    conn.execute(
                        text(f"ALTER TABLE postmarket_review ADD COLUMN {name} {sql_type}")
                    )
                    logger.info("postmarket_review: added column %s", name)
            conn.commit()
    except Exception:
        logger.exception("failed to migrate postmarket_review columns")


def init_db():
    """Create the ``postmarket_review`` table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        _ensure_columns()
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
        "violations": json.loads(row.violations_json) if row.violations_json else [],
        "n_violations": row.n_violations,
        "contracts": json.loads(row.contracts_json) if row.contracts_json else {},
        "triage": json.loads(row.triage_json) if row.triage_json else {},
        "llm_status": row.llm_status,
        "llm_latency_ms": row.llm_latency_ms,
    }


def upsert_review(
    review_date: str,
    digest: dict | None = None,
    summary_text: str | None = None,
    is_trading_day: bool | None = None,
    elapsed_ms: int | None = None,
    telegram_sent: bool | int = 0,
    contracts: dict | None = None,
    triage: dict | None = None,
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
            violations_json=json.dumps((contracts or {}).get("violations") or [], default=str),
            n_violations=len((contracts or {}).get("violations") or []),
            contracts_json=json.dumps(
                {
                    "counts": (contracts or {}).get("counts") or {},
                    "unknown_contracts": (contracts or {}).get("unknown_contracts") or [],
                    "evaluated": (contracts or {}).get("evaluated", False),
                },
                default=str,
            ),
            triage_json=json.dumps(triage or {}, default=str),
            llm_status=(triage or {}).get("status"),
            llm_latency_ms=(triage or {}).get("latency_ms"),
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


def get_recent_reviews(limit: int = 30) -> list[dict]:
    """Up to ``limit`` most-recent review rows, newest first.

    Backs the triage layer's fingerprint history, so "new vs recurring" is read
    off stored data rather than guessed by the model.
    """
    try:
        rows = (
            db_session.query(PostmarketReview)
            .order_by(PostmarketReview.review_date.desc(), PostmarketReview.id.desc())
            .limit(max(1, int(limit)))
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    except Exception:
        logger.exception("failed to read recent postmarket_review rows")
        return []
    finally:
        db_session.remove()
