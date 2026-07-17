"""Daily entry-evaluation snapshots for intraday_pullback_top2 (issue #394 observability).

One JSON row per trade date: the per-pick breakdown of why entries did/didn't fire (the same
payload the /api/entry_breakdown endpoint serves live). Written on every eval tick and again at
the 15:30 EOD summary (issue #422) — the unique (strategy_name, eval_date) constraint makes each
write an idempotent overwrite of the same row, so a restart mid-session no longer loses the day's
diagnostics. Mirrors futures_follow_eval_db.
"""

import json
import os
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint, create_engine
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

TABLE_NAME = "intraday_pullback_eval_snapshots"

# Upper bound on a single ``list_snapshots`` page (issue #422). Mirrors
# futures_follow_eval_db: the payload is a few KB per day, so 90 days is the most
# one request should ever load off disk.
MAX_HISTORY_LIMIT = 90


class IntradayPullbackEvalSnapshot(Base):
    __tablename__ = TABLE_NAME
    __table_args__ = (UniqueConstraint("strategy_name", "eval_date", name="uq_ip_eval_date"),)

    id = Column(Integer, primary_key=True)
    strategy_name = Column(String(64), nullable=False)
    eval_date = Column(String(10), nullable=False)  # YYYY-MM-DD (IST)
    payload_json = Column(Text, nullable=False)
    eval_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def _row_to_dict(row: IntradayPullbackEvalSnapshot) -> dict:
    try:
        payload = json.loads(row.payload_json)
    except Exception:  # noqa: BLE001
        payload = {}
    return {
        "eval_date": row.eval_date,
        "eval_at": row.eval_at.isoformat() if row.eval_at else None,
        **payload,
    }


def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("%s table ready", TABLE_NAME)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to init %s table: %s", TABLE_NAME, e)


init_intraday_pullback_eval_db = init_db  # long-name alias for app.py init list


def upsert_snapshot(strategy_name: str, eval_date: str, payload: dict) -> bool:
    try:
        row = IntradayPullbackEvalSnapshot.query.filter_by(
            strategy_name=strategy_name, eval_date=eval_date
        ).first()
        if row is None:
            row = IntradayPullbackEvalSnapshot(strategy_name=strategy_name, eval_date=eval_date)
            db_session.add(row)
        row.payload_json = json.dumps(payload, default=str)
        db_session.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception("upsert_snapshot failed: %s", e)
        db_session.rollback()
        return False
    finally:
        db_session.remove()


def list_snapshots(strategy_name: str, limit: int = 30, before: str | None = None) -> list[dict]:
    """Snapshots for ``strategy_name``, newest ``eval_date`` first (issue #422).

    ``before`` (exclusive ``YYYY-MM-DD``) pages backwards: pass the oldest date already rendered
    to fetch the next page. ``limit`` is clamped to ``[1, MAX_HISTORY_LIMIT]``.

    Returns the same dicts as ``get_snapshot`` — each carries the day's full payload. Callers
    that only need a per-day digest should run each row through
    ``services.intraday_pullback_eval_view.summarize_payload`` rather than shipping every
    per-pick row to the browser.
    """
    limit = max(1, min(int(limit), MAX_HISTORY_LIMIT))
    try:
        query = IntradayPullbackEvalSnapshot.query.filter_by(strategy_name=strategy_name)
        if before:
            query = query.filter(IntradayPullbackEvalSnapshot.eval_date < str(before))
        rows = query.order_by(IntradayPullbackEvalSnapshot.eval_date.desc()).limit(limit).all()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.exception("list_snapshots failed: %s", e)
        return []
    finally:
        db_session.remove()


def get_snapshot(strategy_name: str, eval_date: str) -> dict | None:
    try:
        row = IntradayPullbackEvalSnapshot.query.filter_by(
            strategy_name=strategy_name, eval_date=eval_date
        ).first()
        return _row_to_dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        logger.exception("get_snapshot failed: %s", e)
        return None
    finally:
        db_session.remove()
