"""Tests for the APScheduler job-run audit (issue #511).

The audit exists because nothing recorded whether a scheduled job *fired* —
``journal_reflection`` ran three times in two months against a daily schedule and
nobody noticed. These tests pin the behaviours that make the table trustworthy
enough for Phase 2's expectation contracts to assert against it.

Coverage:

* a successful fire records ``status='ok'`` with a measured ``duration_ms``,
* a raising job records ``status='error'`` and captures the exception text,
* a fire past its misfire grace records ``status='missed'`` with no duration,
* ``job_name`` survives even for one-shot jobs, which APScheduler removes from
  the jobstore *before* submitting them (the reason names are cached on
  EVENT_JOB_ADDED rather than looked up at completion),
* the SUBMITTED event's ``scheduled_run_times`` (plural) is keyed the same way
  as the EXECUTED event's ``scheduled_run_time`` (singular) — the mismatch that
  silently produced ``duration_ms=None`` for every job,
* ``summarize_date`` folds fires into the per-job shape the digest consumes,
* the per-fire ``JOB_RUN_AUDIT_ENABLED`` flag suppresses recording,
* ``register_listener`` is idempotent — re-registering must not double-record,
* a DB failure inside the listener never escapes into the scheduler.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

IST = pytz.timezone("Asia/Kolkata")

# Wall-clock budget for a probe job to fire and its event to land.
_SETTLE_SECONDS = 3.0


def _mk(module, db_path):
    """Rebind ``module`` to a fresh temp-file engine and create its tables.

    A **file** DB, not ``sqlite:///:memory:``: the audit listener writes from
    APScheduler's executor thread, and each new connection to an in-memory
    SQLite gets its own empty database — so the table created on the main
    thread would be invisible to the writer. NullPool matches production.
    """
    eng = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    module.Base.metadata.create_all(bind=eng)
    return eng, sess


@pytest.fixture
def job_run_table(monkeypatch, tmp_path):
    """Isolated ``job_run`` table plus a clean audit module."""
    from database import job_run_db
    from services import job_run_audit

    eng, sess = _mk(job_run_db, tmp_path / "job_run_test.db")
    monkeypatch.setattr(job_run_db, "engine", eng)
    monkeypatch.setattr(job_run_db, "db_session", sess)
    job_run_audit.reset_for_tests()
    yield job_run_db
    job_run_audit.reset_for_tests()


@pytest.fixture
def scheduler():
    """A real BackgroundScheduler on IST, torn down after the test.

    Real rather than mocked: the whole point of these tests is that our listener
    agrees with APScheduler's actual event objects, which is exactly where the
    ``scheduled_run_times`` bug lived.
    """
    sched = BackgroundScheduler(timezone=IST)
    sched.start()
    yield sched
    try:
        sched.shutdown(wait=False)
    except Exception:
        pass


def _ok_job():
    time.sleep(0.25)


def _bad_job():
    raise ValueError("deliberate probe failure")


def _rows(job_run_db, job_id: str):
    return [
        r for r in job_run_db.get_runs_for_date(job_run_db._ist_date_str()) if r["job_id"] == job_id
    ]


def test_successful_fire_records_ok_with_duration(job_run_table, scheduler):
    from services import job_run_audit

    assert job_run_audit.register_listener(scheduler) is True

    scheduler.add_job(
        _ok_job,
        "date",
        run_date=datetime.now(IST) + timedelta(seconds=1),
        id="probe_ok",
        name="Probe OK",
    )
    time.sleep(_SETTLE_SECONDS)

    rows = _rows(job_run_table, "probe_ok")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok"
    assert row["error"] is None
    # The job sleeps 250ms; a measured duration proves SUBMITTED and EXECUTED
    # were keyed to the same fire.
    assert row["duration_ms"] is not None
    assert row["duration_ms"] >= 200
    assert row["job_name"] == "Probe OK"
    assert row["scheduled_at"] is not None


def test_raising_job_records_error_and_exception_text(job_run_table, scheduler):
    from services import job_run_audit

    job_run_audit.register_listener(scheduler)

    scheduler.add_job(
        _bad_job,
        "date",
        run_date=datetime.now(IST) + timedelta(seconds=1),
        id="probe_err",
        name="Probe Err",
    )
    time.sleep(_SETTLE_SECONDS)

    rows = _rows(job_run_table, "probe_err")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert "deliberate probe failure" in (rows[0]["error"] or "")


def test_missed_fire_records_missed_without_duration(job_run_table, scheduler):
    from services import job_run_audit

    job_run_audit.register_listener(scheduler)

    scheduler.add_job(
        _ok_job,
        "date",
        run_date=datetime.now(IST) - timedelta(seconds=120),
        id="probe_missed",
        name="Probe Missed",
        misfire_grace_time=1,
    )
    time.sleep(_SETTLE_SECONDS)

    rows = _rows(job_run_table, "probe_missed")
    assert len(rows) == 1
    assert rows[0]["status"] == "missed"
    # A missed fire never ran, so claiming a duration would be a lie.
    assert rows[0]["duration_ms"] is None
    assert rows[0]["job_name"] == "Probe Missed"


def test_summarize_date_folds_fires_per_job(job_run_table):
    today = job_run_table._ist_date_str()
    job_run_table.record_run("j1", "ok", job_name="J1", duration_ms=100)
    job_run_table.record_run("j1", "ok", job_name="J1", duration_ms=300)
    job_run_table.record_run("j1", "error", job_name="J1", error="kaboom")
    job_run_table.record_run("j2", "missed", job_name="J2")

    summary = job_run_table.summarize_date(today)

    assert summary["j1"]["ran"] == 3
    assert summary["j1"]["ok"] == 2
    assert summary["j1"]["error"] == 1
    assert summary["j1"]["max_duration_ms"] == 300
    assert "kaboom" in summary["j1"]["first_error"]
    assert summary["j2"]["missed"] == 1
    assert summary["j2"]["ran"] == 1


def test_disabled_flag_suppresses_recording(job_run_table, scheduler, monkeypatch):
    from services import job_run_audit

    job_run_audit.register_listener(scheduler)
    monkeypatch.setenv("JOB_RUN_AUDIT_ENABLED", "false")

    scheduler.add_job(
        _ok_job,
        "date",
        run_date=datetime.now(IST) + timedelta(seconds=1),
        id="probe_disabled",
        name="Probe Disabled",
    )
    time.sleep(_SETTLE_SECONDS)

    assert _rows(job_run_table, "probe_disabled") == []


def test_re_registering_does_not_double_record(job_run_table, scheduler):
    from services import job_run_audit

    job_run_audit.register_listener(scheduler)
    job_run_audit.register_listener(scheduler)
    job_run_audit.register_listener(scheduler)

    scheduler.add_job(
        _ok_job,
        "date",
        run_date=datetime.now(IST) + timedelta(seconds=1),
        id="probe_once",
        name="Probe Once",
    )
    time.sleep(_SETTLE_SECONDS)

    # Three registrations, one fire — still exactly one row.
    assert len(_rows(job_run_table, "probe_once")) == 1


def test_listener_never_raises_into_scheduler(job_run_table, scheduler):
    """A DB failure inside the listener must not surface to APScheduler."""
    from services import job_run_audit

    job_run_audit.register_listener(scheduler)

    with patch.object(job_run_audit.job_run_db, "record_run", side_effect=RuntimeError("db down")):
        scheduler.add_job(
            _ok_job,
            "date",
            run_date=datetime.now(IST) + timedelta(seconds=1),
            id="probe_dbfail",
            name="Probe DB Fail",
        )
        time.sleep(_SETTLE_SECONDS)

    # Nothing recorded, but the scheduler is still alive and usable.
    assert _rows(job_run_table, "probe_dbfail") == []
    assert scheduler.running


def test_prune_older_than_drops_stale_rows(job_run_table):
    from datetime import datetime as _dt

    old = _dt.utcnow() - timedelta(days=120)
    job_run_table.record_run("old_job", "ok", fired_at=old)
    job_run_table.record_run("new_job", "ok")

    deleted = job_run_table.prune_older_than(days=90)

    assert deleted == 1
    assert job_run_table.get_last_run("old_job") is None
    assert job_run_table.get_last_run("new_job") is not None


def test_key_handles_both_scheduled_run_time_spellings():
    """SUBMITTED uses ``scheduled_run_times`` (list); EXECUTED uses the singular.

    Keying only off the singular form produced an empty key on submission, so no
    completion ever matched it and every ``duration_ms`` came back None.
    """
    from services.job_run_audit import _key

    moment = datetime(2026, 7, 30, 15, 20, tzinfo=IST)

    class _Submitted:
        job_id = "entry"
        scheduled_run_times = [moment]

    class _Executed:
        job_id = "entry"
        scheduled_run_time = moment

    assert _key(_Submitted()) == _key(_Executed())
