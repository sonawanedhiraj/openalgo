"""Persistence for scheduled-job fires (``job_run``).

Additive table in the main database (``db/openalgo.db``) recording one row per
APScheduler job fire: which job, when it was scheduled, when it actually ran,
how long it took, and whether it succeeded, raised, or was missed entirely.

Why this exists
---------------

Nothing in this install recorded whether a scheduled job *fired*. The
``journal_reflection`` nightly LLM job (16:00 IST mon-fri) had run **three times
in two months** — it posts to a bridge on :5001 that is normally down, and failed
silently every other weekday. There was no cheap way to notice, because "did the
job run?" was only answerable by grepping a 5 MB daily text log.

With this table, "did the 15:20 entry job run at all?" is a one-row lookup. It is
the foundation the post-market review's expectation contracts assert against —
a contract that says "this strategy's entry job must fire on a trading day" is
only trustworthy if job fires are recorded independently of the job's own logging.

This module owns only its own table and is read-only on every other module. The
writer is :mod:`services.job_run_audit`.
"""

import os
from datetime import datetime, timedelta

import pytz
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

IST = pytz.timezone("Asia/Kolkata")

# Terminal statuses a fire can land in.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_MISSED = "missed"
VALID_STATUSES = (STATUS_OK, STATUS_ERROR, STATUS_MISSED)


class JobRun(Base):
    """One row per APScheduler job fire."""

    __tablename__ = "job_run"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # APScheduler job id (e.g. ``futures_follow_entry``) and its human name.
    job_id = Column(String(120), nullable=False)
    job_name = Column(String(200), nullable=True)
    # Naive UTC. ``scheduled_at`` is the trigger's intended time; ``fired_at`` is
    # when the listener saw the outcome. A large gap between them means the
    # scheduler was saturated or the process was busy.
    scheduled_at = Column(DateTime, nullable=True)
    fired_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # IST calendar date of the fire, denormalised so day queries stay trivial
    # (every other table in this repo pays for date filtering in string slicing).
    run_date = Column(String(10), nullable=False)
    status = Column(String(16), nullable=False)  # ok | error | missed
    duration_ms = Column(Integer, nullable=True)  # None for missed fires
    error = Column(Text, nullable=True)  # exception text, truncated


Index("idx_job_run_date", JobRun.run_date)
Index("idx_job_run_job", JobRun.job_id, JobRun.fired_at)


def init_db():
    """Create the ``job_run`` table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("job_run table ready")
    except Exception as e:
        logger.exception(f"Failed to init job_run table: {e}")


init_job_run_db = init_db


_MAX_ERROR_CHARS = 2000


def _row_to_dict(row: JobRun) -> dict:
    return {
        "id": row.id,
        "job_id": row.job_id,
        "job_name": row.job_name,
        "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
        "fired_at": row.fired_at.isoformat() if row.fired_at else None,
        "run_date": row.run_date,
        "status": row.status,
        "duration_ms": row.duration_ms,
        "error": row.error,
    }


def _ist_date_str(moment: datetime | None = None) -> str:
    """IST calendar date of ``moment`` (naive UTC) as ``YYYY-MM-DD``."""
    if moment is None:
        return datetime.now(IST).strftime("%Y-%m-%d")
    aware = moment if moment.tzinfo else pytz.utc.localize(moment)
    return aware.astimezone(IST).strftime("%Y-%m-%d")


def record_run(
    job_id: str,
    status: str,
    job_name: str | None = None,
    scheduled_at: datetime | None = None,
    fired_at: datetime | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> int:
    """Insert one job-fire row. Returns the new row id (0 on failure).

    Never raises: this is called from an APScheduler event listener, and a
    bookkeeping failure must never take down the job that just ran.
    """
    if status not in VALID_STATUSES:
        logger.warning("job_run: unknown status %r for job %r — recording anyway", status, job_id)
    try:
        moment = fired_at or datetime.utcnow()
        row = JobRun(
            job_id=job_id,
            job_name=job_name,
            scheduled_at=scheduled_at,
            fired_at=moment,
            run_date=_ist_date_str(moment),
            status=status,
            duration_ms=duration_ms,
            error=(error or None) and str(error)[:_MAX_ERROR_CHARS],
        )
        db_session.add(row)
        db_session.commit()
        return row.id
    except Exception:
        db_session.rollback()
        logger.exception("failed to insert job_run row for job %r", job_id)
        return 0
    finally:
        db_session.remove()


def get_runs_for_date(run_date: str, job_id: str | None = None) -> list[dict]:
    """All fires on IST calendar date ``run_date`` (``YYYY-MM-DD``), oldest first."""
    try:
        q = db_session.query(JobRun).filter(JobRun.run_date == run_date)
        if job_id:
            q = q.filter(JobRun.job_id == job_id)
        rows = q.order_by(JobRun.fired_at.asc(), JobRun.id.asc()).all()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        logger.exception("failed to read job_run rows for %s", run_date)
        return []
    finally:
        db_session.remove()


def earliest_run_date() -> str | None:
    """Earliest IST date the audit ever recorded, or None if the table is empty.

    Used to tell "the audit was live and recorded nothing" apart from "this date
    predates the audit entirely" — without it, replaying any pre-audit day
    reports a phantom "no jobs fired" violation.
    """
    try:
        row = db_session.query(JobRun.run_date).order_by(JobRun.run_date.asc()).first()
        return row[0] if row else None
    except Exception:
        logger.exception("failed to read earliest job_run date")
        return None
    finally:
        db_session.remove()


def get_last_run(job_id: str) -> dict | None:
    """Most recent fire of ``job_id``, or None if it has never been recorded."""
    try:
        row = (
            db_session.query(JobRun)
            .filter(JobRun.job_id == job_id)
            .order_by(JobRun.fired_at.desc(), JobRun.id.desc())
            .first()
        )
        return _row_to_dict(row) if row else None
    except Exception:
        logger.exception("failed to read last job_run for %r", job_id)
        return None
    finally:
        db_session.remove()


def summarize_date(run_date: str) -> dict[str, dict]:
    """Per-job digest for one IST date: ``{job_id: {ran, ok, error, missed, ...}}``.

    This is the shape the post-market review consumes — compact enough to embed
    in a day digest without carrying every individual fire.
    """
    summary: dict[str, dict] = {}
    for row in get_runs_for_date(run_date):
        entry = summary.setdefault(
            row["job_id"],
            {
                "job_name": row["job_name"],
                "ran": 0,
                "ok": 0,
                "error": 0,
                "missed": 0,
                "last_fired_at": None,
                "last_status": None,
                "max_duration_ms": None,
                "first_error": None,
            },
        )
        entry["ran"] += 1
        if row["status"] in (STATUS_OK, STATUS_ERROR, STATUS_MISSED):
            entry[row["status"]] += 1
        entry["last_fired_at"] = row["fired_at"]
        entry["last_status"] = row["status"]
        if row["duration_ms"] is not None:
            prior = entry["max_duration_ms"]
            entry["max_duration_ms"] = (
                row["duration_ms"] if prior is None else max(prior, row["duration_ms"])
            )
        if row["error"] and not entry["first_error"]:
            entry["first_error"] = row["error"][:300]
    return summary


def prune_older_than(days: int = 90) -> int:
    """Delete fires older than ``days`` IST days. Returns rows deleted.

    Job fires accumulate on every scheduler tick; without pruning the table
    grows without bound. Called from the post-market review's own job.
    """
    if days <= 0:
        return 0
    try:
        cutoff_date = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")
        deleted = (
            db_session.query(JobRun)
            .filter(JobRun.run_date < cutoff_date)
            .delete(synchronize_session=False)
        )
        db_session.commit()
        if deleted:
            logger.info("job_run: pruned %d rows older than %s", deleted, cutoff_date)
        return int(deleted or 0)
    except Exception:
        db_session.rollback()
        logger.exception("failed to prune job_run rows")
        return 0
    finally:
        db_session.remove()
