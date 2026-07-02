"""Boot-time + periodic state-convergence backfill for sector_follow_cap5_vol.

This module replaces the two after-close APScheduler **cron** jobs (16:05 IST
index + 16:10 IST stock, registered by ``HistorifyScheduler`` until commit
``5c2a06eff`` and earlier) with a *state-convergence* pattern: instead of firing
at a fixed wall-clock minute, the system **checks the last backfill timestamp and
only fetches what is actually behind** — once at boot and then periodically
during the post-close publish window.

Why the change
--------------
A cron job is blind: it fires whether or not the feed is already fresh, and it
does nothing if OpenAlgo happened to be down at 16:05/16:10 (the 2026-06-12
all-entries-held incident — a missed catch-up). The convergence pattern is
self-healing: every boot (e.g. after the daily ~3 AM IST Zerodha token expiry +
operator re-login + restart) the system reads ``MAX(timestamp)`` per symbol from
``historify.duckdb`` and catches up exactly the stale tail. A short periodic
re-check then closes the after-close gap on a day the app was already running.

Two universes, one pattern. Each backfill service exposes
``check_and_refresh_if_stale(today)`` (read MAX(timestamp) → fetch only the stale
subset → idempotent no-op when fresh → fail-graceful on a dead broker session).
This module orchestrates both:

  * ``run_boot_backfill_checks`` — one-shot boot convergence (index then stock).
  * ``start_periodic_backfill_check`` — a daemon thread that re-checks every
    ``SECTOR_FOLLOW_PERIODIC_INTERVAL_MIN`` minutes inside the
    ``15:30``..``SECTOR_FOLLOW_PERIODIC_END_TIME`` IST window on trading days,
    backing off until the next day once both universes report fresh.
  * ``init_sector_follow_backfill`` — the app.py boot entry: waits for a broker
    session to appear (so the catch-up actually fetches), runs the boot check on
    a daemon thread (never blocks boot), then starts the periodic loop.

The per-window CLI backfills (``python -m services.sector_follow_*_backfill
--from --to``) remain for manual historical catch-up; only the cron registration
is gone.
"""

from __future__ import annotations

import os
import threading
import time as _time
from datetime import datetime, time, timedelta, timezone

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Periodic re-check window: starts at the market close, ends at a configurable
# time while Zerodha is still publishing the day's post-close 1m bars.
_WINDOW_START = time(15, 30)
_DEFAULT_END_TIME = "17:00"
_DEFAULT_INTERVAL_MIN = 30

# How long the boot worker waits for a broker session (API key) to appear before
# running the convergence check. Mirrors the scanner pre-subscribe boot retry.
_BOOT_WAIT_MAX_SEC = 7200
_BOOT_WAIT_POLL_SEC = 15

_stop_event = threading.Event()
_periodic_thread: threading.Thread | None = None


# --------------------------------------------------------------------------- #
# Env-configurable knobs
# --------------------------------------------------------------------------- #
def _periodic_enabled() -> bool:
    return os.getenv("SECTOR_FOLLOW_PERIODIC_CHECK_ENABLED", "true").lower() == "true"


def _interval_seconds() -> int:
    try:
        return max(
            60,
            int(os.getenv("SECTOR_FOLLOW_PERIODIC_INTERVAL_MIN", str(_DEFAULT_INTERVAL_MIN))) * 60,
        )
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_MIN * 60


def _end_time() -> time:
    raw = os.getenv("SECTOR_FOLLOW_PERIODIC_END_TIME", _DEFAULT_END_TIME)
    try:
        hh, mm = (int(x) for x in raw.split(":", 1))
        return time(hh, mm)
    except (TypeError, ValueError):
        return time(17, 0)


# Pre-entry refresh (#237): a single convergence fetch just before the 15:20
# entry, since the boot check runs hours earlier and the periodic loop only runs
# 15:30-17:00 — a mid-day intraday gap otherwise stays open through entry.
_DEFAULT_PREENTRY_TIME = "15:17"
# Bounded wait for the pre-entry download jobs. Must be short enough that a slow
# fetch cannot overrun the 15:20 entry — if it does, the 15:18 smoke check still
# catches the stale data and pauses.
_PREENTRY_WAIT_SEC = 90


def preentry_refresh_enabled() -> bool:
    return os.getenv("SECTOR_FOLLOW_PREENTRY_REFRESH_ENABLED", "true").lower() == "true"


def preentry_refresh_time() -> time:
    """Pre-entry refresh fire time (default 15:17 IST — before the 15:18 smoke)."""
    raw = os.getenv("SECTOR_FOLLOW_PREENTRY_REFRESH_TIME", _DEFAULT_PREENTRY_TIME)
    try:
        hh, mm = (int(x) for x in raw.split(":", 1))
        return time(hh, mm)
    except (TypeError, ValueError):
        return time(15, 17)


# --------------------------------------------------------------------------- #
# Pure helpers (testable without threads/clocks)
# --------------------------------------------------------------------------- #
def _is_trading_day(d) -> bool:
    """Weekday check (holidays not modelled — matches data_freshness_service)."""
    return d.weekday() < 5


def _within_window(now_t: time, end_t: time) -> bool:
    """True iff ``now_t`` is in the post-close re-check window ``[15:30, end_t]``."""
    return _WINDOW_START <= now_t <= end_t


def _seconds_until_next_window_start(now: datetime) -> float:
    """Seconds until tomorrow's 15:30 IST window opens (min 60s)."""
    nxt = datetime.combine(now.date() + timedelta(days=1), _WINDOW_START, tzinfo=_IST)
    return max(60.0, (nxt - now).total_seconds())


# --------------------------------------------------------------------------- #
# Convergence check + alerting
# --------------------------------------------------------------------------- #
def run_backfill_checks(today=None) -> dict:
    """Run the index then stock stale-check; return the combined verdict.

    ``all_fresh`` is True iff neither universe found a stale symbol (the
    convergence signal that lets the periodic loop back off). ``errors`` unions
    both universes' fail-graceful error lists. Never raises.
    """
    from services.sector_follow_index_backfill import (
        check_and_refresh_if_stale as _index_check,
    )
    from services.sector_follow_stock_backfill import (
        check_and_refresh_if_stale as _stock_check,
    )

    index_res = _index_check(today)
    stock_res = _stock_check(today)
    errors = list(index_res.get("errors", [])) + list(stock_res.get("errors", []))

    # A "skipped_locked" arm read nothing (historify was briefly locked) — it is
    # NOT proof the feed is fresh, so it must not let the periodic loop back off.
    def _arm_fresh(r: dict) -> bool:
        return not r.get("stale_symbols") and r.get("status") != "skipped_locked"

    all_fresh = _arm_fresh(index_res) and _arm_fresh(stock_res)
    return {
        "index": index_res,
        "stock": stock_res,
        "all_fresh": all_fresh,
        "errors": errors,
    }


def _log_and_alert(res: dict, phase: str) -> None:
    idx = res.get("index", {})
    stk = res.get("stock", {})
    logger.info(
        "sector_follow backfill %s: index(stale=%d refreshed=%d) "
        "stock(stale=%d refreshed=%d) all_fresh=%s errors=%d",
        phase,
        len(idx.get("stale_symbols", [])),
        len(idx.get("refreshed", [])),
        len(stk.get("stale_symbols", [])),
        len(stk.get("refreshed", [])),
        res.get("all_fresh"),
        len(res.get("errors", [])),
    )
    if res.get("errors"):
        try:
            from services.notification_service import get_notification_service

            get_notification_service().publish_anomaly(
                source=f"sector_follow_backfill:{phase}",
                message="; ".join(res["errors"])[:500],
                severity="warning",
            )
        except Exception:  # alerting must never break the backfill path
            logger.exception("sector_follow backfill anomaly alert failed")


def run_boot_backfill_checks(today=None) -> dict:
    """One-shot boot convergence: catch up whatever is stale, log + alert.

    Blocks until any submitted download jobs reach a terminal status so the
    sibling scheduler's lock-protected boot worker doesn't start its writes
    while this one's 5-worker pool is still mid-download (issue #151 —
    in-process DuckDB write contention).
    """
    logger.info("sector_follow backfill: boot convergence check starting")
    res = run_backfill_checks(today)
    _log_and_alert(res, phase="boot")

    # Wait inside the boot_convergence_lock for the submitted jobs to finish.
    # check_and_refresh_if_stale returns ``{"job_id": ...}`` when work was
    # submitted; an empty/stale-free arm has no job_id and contributes nothing.
    try:
        from services.historify_service import wait_for_jobs

        job_ids = [
            (res.get("index") or {}).get("job_id"),
            (res.get("stock") or {}).get("job_id"),
        ]
        finals = wait_for_jobs(job_ids)
        if finals:
            logger.info("sector_follow backfill: boot jobs final status: %s", finals)
    except Exception:  # waiting must never break the boot path
        logger.exception("sector_follow backfill: wait_for_jobs raised")
    return res


def run_preentry_backfill_checks(today=None) -> dict:
    """Pre-15:20-entry convergence: fetch whatever intraday is behind so the
    evaluator has today's data at the 15:20 entry (issue #237).

    The boot check runs once (hours earlier) and the periodic loop only runs
    15:30-17:00, so a mid-day intraday gap stays open through the entry window —
    the 06-29/06-30 zero-order days. This closes it: run the same stale-check the
    boot/periodic paths use, then wait (bounded to ``_PREENTRY_WAIT_SEC``, short
    enough not to overrun 15:20) for the download jobs so today's bars land in
    historify (the evaluator's fallback source) before the 15:18 smoke + 15:20
    entry. Additive, idempotent (fresh → no-op), and fail-graceful. Mirrors
    ``run_boot_backfill_checks`` minus the boot serialisation lock — 15:17 is a
    quiet window with no sibling convergence running.

    Returns the ``run_backfill_checks`` verdict, or ``{"skipped": True}`` when the
    feature flag is off.
    """
    if not preentry_refresh_enabled():
        logger.info(
            "sector_follow pre-entry refresh disabled "
            "(SECTOR_FOLLOW_PREENTRY_REFRESH_ENABLED!=true)"
        )
        return {"skipped": True}

    logger.info("sector_follow backfill: pre-entry (%s) convergence check starting", _now_hhmm())
    res = run_backfill_checks(today)
    _log_and_alert(res, phase="preentry")

    try:
        from services.historify_service import wait_for_jobs

        job_ids = [
            (res.get("index") or {}).get("job_id"),
            (res.get("stock") or {}).get("job_id"),
        ]
        finals = wait_for_jobs(job_ids, timeout_sec=_PREENTRY_WAIT_SEC)
        if finals:
            logger.info("sector_follow backfill: pre-entry jobs final status: %s", finals)
    except Exception:  # waiting must never break the entry path
        logger.exception("sector_follow backfill: pre-entry wait_for_jobs raised")
    return res


def _now_hhmm() -> str:
    return preentry_refresh_time().strftime("%H:%M")


# --------------------------------------------------------------------------- #
# Periodic loop
# --------------------------------------------------------------------------- #
def _periodic_tick(now: datetime, end_t: time) -> tuple[bool, dict | None]:
    """One periodic evaluation. Returns ``(ran, result)``.

    ``ran`` is False (and ``result`` None) when ``now`` is outside the trading-day
    post-close window — the loop just sleeps an interval and retries.
    """
    if not (_is_trading_day(now.date()) and _within_window(now.time(), end_t)):
        return False, None
    res = run_backfill_checks(now.date())
    _log_and_alert(res, phase="periodic")
    return True, res


def _periodic_loop() -> None:
    interval = _interval_seconds()
    end_t = _end_time()
    logger.info(
        "sector_follow backfill periodic loop started (every %ds, window 15:30..%02d:%02d IST)",
        interval,
        end_t.hour,
        end_t.minute,
    )
    while not _stop_event.is_set():
        now = datetime.now(_IST)
        try:
            ran, res = _periodic_tick(now, end_t)
        except Exception:  # a tick must never kill the loop
            logger.exception("sector_follow backfill periodic tick failed")
            ran, res = False, None
        # Both universes fresh for today → back off until tomorrow's window.
        if ran and res and res.get("all_fresh"):
            _stop_event.wait(_seconds_until_next_window_start(now))
            continue
        _stop_event.wait(interval)
    logger.info("sector_follow backfill periodic loop stopped")


def start_periodic_backfill_check() -> bool:
    """Start the periodic daemon thread (idempotent). Returns True if started."""
    global _periodic_thread
    if not _periodic_enabled():
        logger.info(
            "sector_follow backfill periodic check disabled "
            "(SECTOR_FOLLOW_PERIODIC_CHECK_ENABLED!=true)"
        )
        return False
    if _periodic_thread is not None and _periodic_thread.is_alive():
        return False
    _stop_event.clear()
    _periodic_thread = threading.Thread(
        target=_periodic_loop, daemon=True, name="SectorFollowBackfillPeriodic"
    )
    _periodic_thread.start()
    return True


def stop_periodic_backfill_check() -> None:
    """Signal the periodic loop to exit (used by tests / shutdown)."""
    _stop_event.set()


# --------------------------------------------------------------------------- #
# Boot entry
# --------------------------------------------------------------------------- #
def _wait_for_broker_session(max_wait_sec: int = _BOOT_WAIT_MAX_SEC) -> bool:
    """Poll until the broker session is live (operator logged in AND token valid).

    Returns immediately when a live session is already present (the common
    restart case). A stored API key with a dead daily token (the typical
    morning state after the ~3 AM Zerodha rotation) is treated as no session
    — otherwise every backfill fetch 401s and floods errors.jsonl. See
    ``services/broker_session_health.is_live_broker_session``.
    """
    from services.broker_session_health import is_live_broker_session

    deadline = _time.time() + max_wait_sec
    while _time.time() < deadline and not _stop_event.is_set():
        try:
            if is_live_broker_session():
                return True
        except Exception:
            logger.exception("sector_follow backfill: live-session probe raised")
        _stop_event.wait(_BOOT_WAIT_POLL_SEC)
    return False


def _boot_worker() -> None:
    if _wait_for_broker_session():
        # Serialise the convergence work against sibling schedulers so the four
        # boot backfill jobs don't burst onto historify.duckdb simultaneously
        # (see services/boot_convergence.py and issue #140).
        from services.boot_convergence import boot_convergence_lock

        with boot_convergence_lock(name="sector_follow"):
            run_boot_backfill_checks()
    else:
        logger.warning(
            "sector_follow backfill: no broker session appeared at boot; "
            "periodic check will retry in the post-close window"
        )
    start_periodic_backfill_check()


def init_sector_follow_backfill(app=None) -> None:
    """Boot hook: run the convergence check + start the periodic loop.

    Non-blocking — the boot check runs on a daemon thread that first waits for a
    broker session, so a slow/absent login never blocks app boot. Idempotent
    enough for the single-worker eventlet deployment (one process, one thread).
    """
    _stop_event.clear()
    threading.Thread(target=_boot_worker, daemon=True, name="SectorFollowBackfillBoot").start()
    logger.info("sector_follow backfill boot+periodic convergence initialized")
