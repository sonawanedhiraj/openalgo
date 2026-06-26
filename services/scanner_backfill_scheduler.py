"""Boot-time + periodic state-convergence backfill for the in-house scanner.

The scanner-side sibling of ``services.sector_follow_backfill_scheduler``. Where
that service keeps the ``sector_follow`` locked-static-30 + 8 indices fresh in
``1m`` only, this one keeps the **scanner's own ``SCANNER_SYMBOLS`` F&O universe**
fresh in **both storage intervals** — ``1m`` (the intraday tape) and ``D`` (the
daily gates that ``ScannerHistoryProvider`` reads). It is the durable fix for the
two supply bugs the 2026-06-13 Friday replay surfaced (universe never backfilled;
stored-``D`` universally stale — see ``services.scanner_universe_backfill``).

Why a sibling service, not an arm on the existing one
-----------------------------------------------------
The two universes are independent and have different shapes: ``sector_follow``'s
set is locked-static-30 and 1m-only, while the scanner universe is ~200 names that
rotate and needs both intervals. Keeping them separate avoids coupling the two
cadences and lets the scanner side be enabled/disabled on its own flag.

Same convergence pattern as the sector_follow scheduler:

  * ``run_boot_backfill_checks`` — one-shot boot convergence (1m then D), writes a
    ``data_health_check`` row per interval, logs + alerts.
  * ``start_periodic_backfill_check`` — a daemon thread that re-checks every
    ``SCANNER_BACKFILL_PERIODIC_INTERVAL_MIN`` minutes inside the
    ``15:30``..``SCANNER_BACKFILL_PERIODIC_END_TIME`` IST window on trading days,
    backing off until the next day once both intervals report fresh.
  * ``init_scanner_backfill_scheduler`` — the app.py boot entry: waits for a
    broker session to appear (so the catch-up actually fetches), runs the boot
    check on a daemon thread (never blocks boot), then starts the periodic loop.

The per-interval CLI (``python -m services.scanner_universe_backfill --from --to
--interval``) remains for manual historical catch-up (notably the initial deep 1m
backfill for never-fetched symbols, which is beyond the small lookback window).
"""

from __future__ import annotations

import os
import threading
import time as _time
from datetime import datetime, time, timedelta, timezone

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Periodic re-check window: starts at the market close, ends while Zerodha is
# still publishing the day's post-close 1m/D bars.
_WINDOW_START = time(15, 30)
_DEFAULT_END_TIME = "17:00"
_DEFAULT_INTERVAL_MIN = 30

# How long the boot worker waits for a broker session (API key) to appear before
# running the convergence check.
_BOOT_WAIT_MAX_SEC = 7200
_BOOT_WAIT_POLL_SEC = 15

_stop_event = threading.Event()
_periodic_thread: threading.Thread | None = None


# --------------------------------------------------------------------------- #
# Env-configurable knobs
# --------------------------------------------------------------------------- #
def _backfill_enabled() -> bool:
    """Master gate for the scanner boot+periodic convergence (default on)."""
    return os.getenv("SCANNER_BACKFILL_ENABLED", "true").lower() == "true"


def _periodic_enabled() -> bool:
    return os.getenv("SCANNER_BACKFILL_PERIODIC_CHECK_ENABLED", "true").lower() == "true"


def _intervals() -> list[str]:
    """Which storage intervals to converge, in order (default ``1m`` then ``D``).

    ``SCANNER_BACKFILL_INTERVALS`` (comma-separated) lets an operator drop one arm
    (e.g. ``1m`` only) if the D download adds undesirable broker load. Unknown
    tokens are dropped; an empty/garbage value falls back to both.
    """
    from services.scanner_universe_backfill import STORAGE_INTERVALS

    raw = os.getenv("SCANNER_BACKFILL_INTERVALS", "1m,D")
    chosen = [t.strip() for t in raw.split(",") if t.strip() in STORAGE_INTERVALS]
    return chosen or list(STORAGE_INTERVALS)


def _interval_seconds() -> int:
    try:
        return max(
            60,
            int(os.getenv("SCANNER_BACKFILL_PERIODIC_INTERVAL_MIN", str(_DEFAULT_INTERVAL_MIN)))
            * 60,
        )
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_MIN * 60


def _end_time() -> time:
    raw = os.getenv("SCANNER_BACKFILL_PERIODIC_END_TIME", _DEFAULT_END_TIME)
    try:
        hh, mm = (int(x) for x in raw.split(":", 1))
        return time(hh, mm)
    except (TypeError, ValueError):
        return time(17, 0)


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


def _health_strategy_name(interval: str) -> str:
    """``data_health_check.strategy_name`` for a scanner interval row.

    Reuses the existing ``data_health_check`` table with a per-interval pseudo-
    strategy key (no schema change) so ``get_latest_check`` / ``get_recent_checks``
    keep working and the parent can query scanner coverage directly.
    """
    return f"scanner_universe_{interval}"


# --------------------------------------------------------------------------- #
# Convergence check + health persistence + alerting
# --------------------------------------------------------------------------- #
def run_backfill_checks(today=None) -> dict:
    """Run the per-interval stale-check (1m then D); return the combined verdict.

    ``all_fresh`` is True iff no interval found a stale symbol (the convergence
    signal that lets the periodic loop back off). ``errors`` unions every
    interval's fail-graceful error list. Never raises.
    """
    from services.scanner_universe_backfill import check_and_refresh_if_stale

    per_interval: dict[str, dict] = {}
    errors: list[str] = []
    all_fresh = True
    for interval in _intervals():
        res = check_and_refresh_if_stale(today, interval=interval)
        per_interval[interval] = res
        errors.extend(res.get("errors", []))
        # A "skipped_locked" arm read nothing (historify briefly locked) — not
        # proof of freshness, so don't let the periodic loop back off on it.
        if res.get("stale_symbols") or res.get("status") == "skipped_locked":
            all_fresh = False
    return {"intervals": per_interval, "all_fresh": all_fresh, "errors": errors}


def _persist_health(res: dict) -> None:
    """Write one ``data_health_check`` row per interval (best-effort)."""
    try:
        from database.data_health_db import insert_check
    except Exception:  # DB layer unavailable (e.g. some test contexts) — skip silently
        logger.exception("scanner backfill: data_health_db import failed")
        return

    for interval, ires in res.get("intervals", {}).items():
        stale = ires.get("stale_symbols", [])
        had_error = bool(ires.get("errors"))
        # overall_ok: the feed is healthy iff the check actually ran (status "ok"),
        # nothing was stale, AND no fetch error. A "skipped_locked" read is not a
        # health signal, so it must not record a falsely-healthy row.
        overall_ok = ires.get("status") == "ok" and not stale and not had_error
        try:
            insert_check(
                strategy_name=_health_strategy_name(interval),
                overall_ok=overall_ok,
                stale_symbols=stale,
                details={
                    "interval": interval,
                    "refreshed": ires.get("refreshed", []),
                    "skipped_fresh_count": len(ires.get("skipped_fresh", [])),
                    "errors": ires.get("errors", []),
                },
                alert_sent=1 if had_error else 0,
            )
        except Exception:  # health persistence must never break the backfill path
            logger.exception("scanner backfill: failed to persist data_health row for %s", interval)


def _log_and_alert(res: dict, phase: str) -> None:
    for interval, ires in res.get("intervals", {}).items():
        logger.info(
            "scanner backfill %s [%s]: stale=%d refreshed=%d skipped_fresh=%d errors=%d",
            phase,
            interval,
            len(ires.get("stale_symbols", [])),
            len(ires.get("refreshed", [])),
            len(ires.get("skipped_fresh", [])),
            len(ires.get("errors", [])),
        )
    logger.info(
        "scanner backfill %s: all_fresh=%s total_errors=%d",
        phase,
        res.get("all_fresh"),
        len(res.get("errors", [])),
    )
    if res.get("errors"):
        try:
            from services.notification_service import get_notification_service

            get_notification_service().publish_anomaly(
                source=f"scanner_backfill:{phase}",
                message="; ".join(res["errors"])[:500],
                severity="warning",
            )
        except Exception:  # alerting must never break the backfill path
            logger.exception("scanner backfill anomaly alert failed")


def run_boot_backfill_checks(today=None) -> dict:
    """One-shot boot convergence: catch up whatever is stale, persist + log + alert.

    Blocks until any submitted download jobs reach a terminal status so the
    sibling scheduler's lock-protected boot worker doesn't start its writes
    while this one's 5-worker pool is still mid-download (issue #151 —
    in-process DuckDB write contention).
    """
    logger.info("scanner backfill: boot convergence check starting")
    res = run_backfill_checks(today)
    _persist_health(res)
    _log_and_alert(res, phase="boot")

    # Wait inside the boot_convergence_lock for both interval arms' jobs to
    # finish. check_and_refresh_if_stale returns ``{"job_id": ...}`` per
    # interval when work was submitted; a stale-free arm contributes nothing.
    try:
        from services.historify_service import wait_for_jobs

        intervals = res.get("intervals") or {}
        job_ids = [ires.get("job_id") for ires in intervals.values()]
        finals = wait_for_jobs(job_ids)
        if finals:
            logger.info("scanner backfill: boot jobs final status: %s", finals)
    except Exception:  # waiting must never break the boot path
        logger.exception("scanner backfill: wait_for_jobs raised")
    return res


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
    _persist_health(res)
    _log_and_alert(res, phase="periodic")
    return True, res


def _periodic_loop() -> None:
    interval = _interval_seconds()
    end_t = _end_time()
    logger.info(
        "scanner backfill periodic loop started (every %ds, window 15:30..%02d:%02d IST)",
        interval,
        end_t.hour,
        end_t.minute,
    )
    while not _stop_event.is_set():
        now = datetime.now(_IST)
        try:
            ran, res = _periodic_tick(now, end_t)
        except Exception:  # a tick must never kill the loop
            logger.exception("scanner backfill periodic tick failed")
            ran, res = False, None
        # All intervals fresh for today → back off until tomorrow's window.
        if ran and res and res.get("all_fresh"):
            _stop_event.wait(_seconds_until_next_window_start(now))
            continue
        _stop_event.wait(interval)
    logger.info("scanner backfill periodic loop stopped")


def start_periodic_backfill_check() -> bool:
    """Start the periodic daemon thread (idempotent). Returns True if started."""
    global _periodic_thread
    if not _periodic_enabled():
        logger.info(
            "scanner backfill periodic check disabled (SCANNER_BACKFILL_PERIODIC_CHECK_ENABLED!=true)"
        )
        return False
    if _periodic_thread is not None and _periodic_thread.is_alive():
        return False
    _stop_event.clear()
    _periodic_thread = threading.Thread(
        target=_periodic_loop, daemon=True, name="ScannerBackfillPeriodic"
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
            logger.exception("scanner backfill: live-session probe raised")
        _stop_event.wait(_BOOT_WAIT_POLL_SEC)
    return False


def _boot_worker() -> None:
    if _wait_for_broker_session():
        # Serialise the convergence work against sibling schedulers so the four
        # boot backfill jobs don't burst onto historify.duckdb simultaneously
        # (see services/boot_convergence.py and issue #140).
        from services.boot_convergence import boot_convergence_lock

        with boot_convergence_lock(name="scanner"):
            run_boot_backfill_checks()
    else:
        logger.warning(
            "scanner backfill: no broker session appeared at boot; "
            "periodic check will retry in the post-close window"
        )
    start_periodic_backfill_check()


def init_scanner_backfill_scheduler(app=None) -> None:
    """Boot hook: run the convergence check + start the periodic loop.

    Non-blocking — the boot check runs on a daemon thread that first waits for a
    broker session, so a slow/absent login never blocks app boot. Gated by
    ``SCANNER_BACKFILL_ENABLED`` (default true).
    """
    if not _backfill_enabled():
        logger.info("scanner backfill convergence disabled (SCANNER_BACKFILL_ENABLED!=true)")
        return
    _stop_event.clear()
    threading.Thread(target=_boot_worker, daemon=True, name="ScannerBackfillBoot").start()
    logger.info("scanner backfill boot+periodic convergence initialized")
