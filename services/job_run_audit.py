"""Records every APScheduler job fire into the ``job_run`` table.

Attaches a listener to the shared Historify scheduler for
``EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED``
and writes one row per outcome via :mod:`database.job_run_db`.

Why a SUBMITTED handler too
---------------------------

APScheduler's ``JobExecutionEvent`` carries the *scheduled* run time but not the
wall-clock duration. ``EVENT_JOB_SUBMITTED`` fires when the job is handed to the
executor, so holding a small in-memory ``(job_id, scheduled_run_time) -> started``
map lets the EXECUTED/ERROR handler compute a real ``duration_ms``. The map is
bounded and self-cleaning: entries are popped on completion and any stragglers
(a job whose completion event never arrived) are evicted once the map exceeds
``_MAX_INFLIGHT``.

Design rules
------------

* **Never raises into the scheduler.** Every handler is wrapped; a bookkeeping
  failure must not affect the job that just ran, nor poison the listener.
* **Never blocks.** The write is a single small INSERT on a NullPool connection.
* **Fail-quiet, not fail-silent.** Failures are ``logger.exception``-logged and
  therefore land in ``errors.jsonl`` — which the post-market digest reads.
* Registration is idempotent: re-registering replaces the existing listener
  rather than stacking a second one.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Any

from database import job_run_db
from utils.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_ENABLED = "true"

# Bound on the in-flight start-time map. Far above any realistic concurrent job
# count (the shared scheduler runs a few dozen jobs a day); the cap exists only
# so a pathological stream of completion-less submissions cannot leak memory.
_MAX_INFLIGHT = 512

# (job_id, scheduled_run_time) -> (monotonic_start, job_name). The name is
# captured at submission because a one-shot ('date' trigger) job is removed from
# the scheduler before its completion event fires, so looking it up afterwards
# returns None.
_inflight: dict[tuple[str, str], tuple[float, str | None]] = {}
_inflight_lock = threading.Lock()

# job_id -> human name, populated on EVENT_JOB_ADDED. Guarded by the same lock.
_MAX_NAME_CACHE = 512
_name_cache: dict[str, str | None] = {}

_listener_registered = False
_registered_scheduler: Any = None


def _enabled() -> bool:
    """Per-fire gate so the audit can be switched off without re-registration."""
    return os.environ.get("JOB_RUN_AUDIT_ENABLED", _DEFAULT_ENABLED).strip().lower() == "true"


def _key(event: Any) -> tuple[str, str]:
    """Identify one fire as ``(job_id, scheduled_run_time)``.

    The two event classes spell this differently: ``JobSubmissionEvent`` carries
    ``scheduled_run_times`` (a *list* — a coalesced job can cover several missed
    fires), while ``JobExecutionEvent`` carries a single ``scheduled_run_time``.
    Reading only the singular form silently produced an empty key on submission,
    so no completion ever matched and every duration came back ``None``.
    """
    scheduled = getattr(event, "scheduled_run_time", None)
    if scheduled is None:
        times = getattr(event, "scheduled_run_times", None)
        if isinstance(times, (list, tuple)) and times:
            scheduled = times[0]
    return (str(getattr(event, "job_id", "") or ""), str(scheduled or ""))


def _on_job_added(event: Any) -> None:
    """Cache ``job_id -> name`` when a job is registered.

    Needed because a one-shot (``date`` trigger) job is removed from the
    jobstore *before* it is submitted, so ``get_job`` returns None for the whole
    lifetime of the fire. Cron jobs persist and would resolve either way; the
    cache just makes both cases uniform.
    """
    try:
        job = getattr(event, "job_id", None)
        if not job:
            return
        sched = _registered_scheduler
        name = None
        if sched is not None:
            try:
                found = sched.get_job(str(job))
                name = getattr(found, "name", None) if found else None
            except Exception:
                name = None
        with _inflight_lock:
            if len(_name_cache) >= _MAX_NAME_CACHE:
                _name_cache.clear()
            _name_cache[str(job)] = name
    except Exception:
        logger.exception("job_run_audit: failed to cache job name")


def _job_name(job_id: str) -> str | None:
    """Best-effort human name for ``job_id`` — cache first, live scheduler second."""
    if not job_id:
        return None
    with _inflight_lock:
        cached = _name_cache.get(job_id)
    if cached:
        return cached
    sched = _registered_scheduler
    if sched is None:
        return None
    try:
        job = sched.get_job(job_id)
    except Exception:
        return None
    return getattr(job, "name", None) if job else None


def _naive_utc(moment: Any) -> datetime | None:
    """Coerce an APScheduler tz-aware ``scheduled_run_time`` to naive UTC."""
    if not isinstance(moment, datetime):
        return None
    if moment.tzinfo is None:
        return moment
    import pytz

    return moment.astimezone(pytz.utc).replace(tzinfo=None)


def _on_submitted(event: Any) -> None:
    """Stamp the start time so the completion handler can compute a duration."""
    try:
        if not _enabled():
            return
        import time

        key = _key(event)
        name = _job_name(str(getattr(event, "job_id", "") or ""))
        with _inflight_lock:
            if len(_inflight) >= _MAX_INFLIGHT:
                # Evict the oldest straggler rather than growing without bound.
                oldest = min(_inflight, key=lambda k: _inflight[k][0])
                _inflight.pop(oldest, None)
                logger.warning("job_run_audit: in-flight map full, evicted %r", oldest)
            _inflight[key] = (time.monotonic(), name)
    except Exception:
        logger.exception("job_run_audit: failed to stamp job submission")


def _on_finished(event: Any) -> None:
    """Record an EXECUTED / ERROR outcome with its measured duration."""
    try:
        if not _enabled():
            return
        import time

        key = _key(event)
        with _inflight_lock:
            pending = _inflight.pop(key, None)
        started, submitted_name = pending if pending else (None, None)
        duration_ms = int((time.monotonic() - started) * 1000) if started is not None else None

        exception = getattr(event, "exception", None)
        if exception is not None:
            status = job_run_db.STATUS_ERROR
            # Prefer the formatted traceback the event carries; fall back to repr.
            detail = getattr(event, "traceback", None) or f"{type(exception).__name__}: {exception}"
        else:
            status = job_run_db.STATUS_OK
            detail = None

        job_id = str(getattr(event, "job_id", "") or "")
        job_run_db.record_run(
            job_id=job_id,
            status=status,
            job_name=submitted_name or _job_name(job_id),
            scheduled_at=_naive_utc(getattr(event, "scheduled_run_time", None)),
            duration_ms=duration_ms,
            error=detail,
        )
    except Exception:
        logger.exception("job_run_audit: failed to record job completion")


def _on_missed(event: Any) -> None:
    """Record a fire the scheduler skipped (past its misfire grace time)."""
    try:
        if not _enabled():
            return
        job_id = str(getattr(event, "job_id", "") or "")
        with _inflight_lock:
            _inflight.pop(_key(event), None)
        job_run_db.record_run(
            job_id=job_id,
            status=job_run_db.STATUS_MISSED,
            job_name=_job_name(job_id),
            scheduled_at=_naive_utc(getattr(event, "scheduled_run_time", None)),
            duration_ms=None,
            error="scheduler reported the fire as missed (past misfire_grace_time)",
        )
        logger.warning("job_run_audit: job %r missed its scheduled fire", job_id)
    except Exception:
        logger.exception("job_run_audit: failed to record missed job")


def register_listener(scheduler=None) -> bool:
    """Attach the audit listener to ``scheduler`` (default: the shared one).

    Idempotent — a second call removes the prior listener before adding a new
    one, so a re-init cannot double-record every fire.

    Returns:
        True if the listener is attached, False if it could not be.
    """
    global _listener_registered, _registered_scheduler

    sched = scheduler
    if sched is None:
        try:
            from services.historify_scheduler_service import get_historify_scheduler

            sched = get_historify_scheduler().scheduler
        except Exception:
            logger.exception("job_run_audit: could not resolve the shared scheduler")
            return False
    if sched is None:
        logger.warning("job_run_audit: no scheduler available — audit not registered")
        return False

    try:
        from apscheduler.events import (
            EVENT_JOB_ADDED,
            EVENT_JOB_ERROR,
            EVENT_JOB_EXECUTED,
            EVENT_JOB_MISSED,
            EVENT_JOB_SUBMITTED,
        )
    except Exception:
        logger.exception("job_run_audit: apscheduler events unavailable")
        return False

    try:
        if _listener_registered and _registered_scheduler is not None:
            for handler in (_on_job_added, _on_submitted, _on_finished, _on_missed):
                try:
                    _registered_scheduler.remove_listener(handler)
                except Exception:
                    # Not previously attached (or a different scheduler) — fine.
                    pass

        # `_registered_scheduler` must be live before EVENT_JOB_ADDED can fire,
        # since the handler resolves the name off the scheduler it came from.
        _registered_scheduler = sched
        sched.add_listener(_on_job_added, EVENT_JOB_ADDED)
        sched.add_listener(_on_submitted, EVENT_JOB_SUBMITTED)
        sched.add_listener(_on_finished, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        sched.add_listener(_on_missed, EVENT_JOB_MISSED)
    except Exception:
        logger.exception("job_run_audit: failed to attach listener")
        return False

    _listener_registered = True
    logger.info("job_run_audit: listener attached to the shared scheduler")
    return True


def init_job_run_audit(scheduler=None) -> bool:
    """Boot entry point. Safe to call when the audit is disabled."""
    if not _enabled():
        logger.info("job_run_audit: disabled via JOB_RUN_AUDIT_ENABLED — listener not attached")
        return False
    return register_listener(scheduler)


def reset_for_tests() -> None:
    """Clear module state so tests start from a clean listener registry."""
    global _listener_registered, _registered_scheduler
    with _inflight_lock:
        _inflight.clear()
        _name_cache.clear()
    _listener_registered = False
    _registered_scheduler = None
