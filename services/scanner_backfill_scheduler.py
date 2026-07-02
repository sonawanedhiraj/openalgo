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

# Once-per-process-per-date guard for the daily-D re-settle (see
# _maybe_resettle_daily). A settled daily bar only needs correcting once per day;
# without this the periodic loop would re-fetch the whole universe every 30 min.
# A restart clears it (fine — the boot convergence then re-settles once).
_resettled_dates: set = set()

# Pre-entry refresh (#239): a single convergence fetch just before the 09:18
# smoke check so a cold boot at 08:31 IST has fresh historify data AND the
# WS subscription is established before evaluation begins.  The boot check
# runs once (can be hours earlier) and the periodic loop only runs 15:30-17:00,
# so the early-morning gap stays open through the 09:18 smoke check — the
# 2026-06-30 all-day signal drought.  This closes it: run the same stale-check
# the boot/periodic paths use, then wait (bounded to ``_PREENTRY_WAIT_SEC``,
# short enough not to overrun 09:18) for the download jobs and optionally
# trigger the WS subscription if it has not come up yet.
# Additive, idempotent (fresh → no-op), fail-graceful.
_DEFAULT_PREENTRY_TIME = "09:16"
# Bounded wait for the pre-entry download jobs.  Must be short enough that a
# slow fetch cannot overrun the 09:18 smoke check — if it does, the smoke check
# still catches the stale data.  120s = 2m of headroom before 09:18.
_PREENTRY_WAIT_SEC = 120


def preentry_refresh_enabled() -> bool:
    """``SCANNER_PREENTRY_REFRESH_ENABLED`` env flag (default true)."""
    return os.getenv("SCANNER_PREENTRY_REFRESH_ENABLED", "true").lower() == "true"


def preentry_refresh_time() -> time:
    """Pre-entry refresh fire time (default 09:16 IST — before the 09:18 smoke).

    ``SCANNER_PREENTRY_REFRESH_TIME`` env var, ``HH:MM`` format.
    """
    raw = os.getenv("SCANNER_PREENTRY_REFRESH_TIME", _DEFAULT_PREENTRY_TIME)
    try:
        hh, mm = (int(x) for x in raw.split(":", 1))
        return time(hh, mm)
    except (TypeError, ValueError):
        return time(9, 16)


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
def _maybe_resettle_daily(today) -> None:
    """Re-settle the trailing daily-D window once per day (before the stale-check).

    A daily bar written intraday as a provisional/running close is never fixed by
    the incremental convergence (it sees the day's bar already present and skips
    it), so a stale close persists into the scanner's ``yest_d`` gate and fires
    phantom signals (2026-07-02 DELHIVERY false BUY — issue #299). This forces a
    non-incremental overwrite re-fetch of the settled window + a provider cache
    refresh, bounded to once per process per date. Only runs when ``D`` is a
    configured interval. Fully fail-graceful — never raises into the caller.
    """
    ref = today or datetime.now(_IST).date()
    if "D" not in _intervals():
        return
    if ref in _resettled_dates:
        return
    try:
        from services.scanner_universe_backfill import resettle_recent_daily

        res = resettle_recent_daily(ref)
        # Mark done unless the attempt failed for a transient reason (no broker
        # session yet / fetch error) — a failed attempt should retry on the next
        # convergence tick rather than be suppressed for the rest of the day.
        if res.get("status") in ("ok", "disabled") or res.get("resettled"):
            _resettled_dates.add(ref)
        logger.info(
            "scanner daily-D resettle [%s]: status=%s window=%s resettled=%s errors=%d",
            ref,
            res.get("status"),
            res.get("window"),
            res.get("resettled"),
            len(res.get("errors", [])),
        )
    except Exception:  # a resettle failure must never break the convergence path
        logger.exception("scanner daily-D resettle raised")


def run_backfill_checks(today=None) -> dict:
    """Run the per-interval stale-check (1m then D); return the combined verdict.

    ``all_fresh`` is True iff no interval found a stale symbol (the convergence
    signal that lets the periodic loop back off). ``errors`` unions every
    interval's fail-graceful error list. Never raises.

    Before the stale-check, a once-per-day daily-D re-settle corrects any
    provisional (intraday-captured) daily close so the scanner's ``yest_d`` gate
    reads the settled value (issue #299).
    """
    from services.scanner_universe_backfill import check_and_refresh_if_stale

    _maybe_resettle_daily(today)

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


def run_preentry_scanner_refresh(today=None) -> dict:
    """Pre-09:18-smoke-check convergence: fetch whatever is stale so the smoke
    check passes and the scanner has fresh historify data from the start of the
    session (issue #239).

    The boot check runs once at startup (can be hours earlier) and the periodic
    loop only runs 15:30-17:00, so a cold boot at 08:31 IST produces 0/216
    aggregator coverage by 09:18.  This closes it: run the same stale-check
    the boot/periodic paths use, wait (bounded to ``_PREENTRY_WAIT_SEC``) for
    the download jobs so today's bars land in historify before the smoke check,
    and — if the WS subscription has not come up yet — trigger it via
    ``scanner_pre_subscriber.ensure`` so the aggregator starts filling.

    Returns the ``run_backfill_checks`` verdict, or ``{"skipped": True}`` when
    the feature flag is off.  Never raises.
    """
    if not preentry_refresh_enabled():
        logger.info("scanner pre-entry refresh disabled (SCANNER_PREENTRY_REFRESH_ENABLED!=true)")
        return {"skipped": True}

    _pre_t = preentry_refresh_time()
    logger.info(
        "scanner backfill: pre-entry (%02d:%02d) convergence check starting",
        _pre_t.hour,
        _pre_t.minute,
    )
    res = run_backfill_checks(today)
    _persist_health(res)
    _log_and_alert(res, phase="preentry")

    try:
        from services.historify_service import wait_for_jobs

        intervals = res.get("intervals") or {}
        job_ids = [ires.get("job_id") for ires in intervals.values()]
        finals = wait_for_jobs(job_ids, timeout_sec=_PREENTRY_WAIT_SEC)
        if finals:
            logger.info("scanner backfill: pre-entry jobs final status: %s", finals)
    except Exception:  # waiting must never break the entry path
        logger.exception("scanner backfill: pre-entry wait_for_jobs raised")

    # Trigger WS subscription if the scanner has not yet subscribed.  The boot
    # wire_pre_subscribe daemon already handles the normal path; this is a
    # defensive nudge for the race where the boot retry has not yet fired by
    # 09:16 (slow broker login, cold start late in the morning).  Fail-safe:
    # ensure() is idempotent and the import is deferred so test contexts that
    # never wire app.py stay clean.
    try:
        from database.auth_db import get_first_available_api_key, verify_api_key
        from services.scanner_presubscribe import scanner_pre_subscriber

        if not scanner_pre_subscriber.subscribed:
            api_key = get_first_available_api_key()
            if api_key:
                user_id = verify_api_key(api_key)
                if user_id:
                    from database.auth_db import get_broker_name

                    broker = get_broker_name(user_id)
                    raw = os.getenv("SCANNER_SYMBOLS", "")
                    symbols = sorted({s.strip().upper() for s in raw.split(",") if s.strip()})
                    if symbols:
                        n = scanner_pre_subscriber.ensure(user_id, broker, symbols)
                        logger.info(
                            "scanner pre-entry refresh: triggered WS subscribe for %d symbols", n
                        )
    except Exception:  # WS nudge must never break the historify path
        logger.exception("scanner pre-entry refresh: WS subscription nudge failed (non-fatal)")

    return res


# --------------------------------------------------------------------------- #
# Periodic loop
# --------------------------------------------------------------------------- #
def _periodic_tick(now: datetime, end_t: time) -> tuple[bool, dict | None]:
    """One periodic evaluation. Returns ``(ran, result)``.

    ``ran`` is False (and ``result`` None) when ``now`` is outside the trading-day
    post-close window — the loop just sleeps an interval and retries.

    Issue #158 D3: also short-circuits when no broker session is live —
    without this, the periodic tick fires every interval during the morning
    re-login gap (3 AM rotation, operator not yet logged in), and the
    backfill submits with no api_key, logging a noisy ``WARNING`` for every
    stale symbol it found. With this gate, the loop quietly waits for the
    broker session and runs the catch-up cleanly when ready.
    """
    if not (_is_trading_day(now.date()) and _within_window(now.time(), end_t)):
        return False, None
    # Quiet-skip when no broker session — same fail-graceful pattern as the
    # other backfill / convergence services. Logged at INFO (single line per
    # tick) so it's diagnosable but doesn't flood. Honors a flag to disable.
    if _gate_on_broker_session():
        try:
            from services.broker_session_health import is_live_broker_session

            if not is_live_broker_session():
                logger.info("scanner backfill periodic tick: no broker session yet — skipping")
                return False, None
        except Exception:
            logger.exception("scanner backfill: live-session probe raised — proceeding anyway")
    res = run_backfill_checks(now.date())
    _persist_health(res)
    _log_and_alert(res, phase="periodic")
    return True, res


def _gate_on_broker_session() -> bool:
    """``SCANNER_BACKFILL_GATE_ON_BROKER_SESSION`` env flag (default true).
    When True, the periodic tick skips when no broker session is live so
    'no api key available' warnings stop flooding the morning logs."""
    return os.environ.get("SCANNER_BACKFILL_GATE_ON_BROKER_SESSION", "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


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


# --------------------------------------------------------------------------- #
# Pre-entry refresh APScheduler registration (#239)
# --------------------------------------------------------------------------- #


def _scanner_preentry_refresh_job() -> None:
    """09:16 IST: fetch stale historify data + nudge WS subscription before the
    09:18 smoke check (issue #239).  Module-level + fail-safe so a fetch error
    never kills the scheduler thread.  Independent of the boot lock — 09:16 is
    a quiet window with no sibling convergence running."""
    try:
        run_preentry_scanner_refresh()
    except Exception:
        logger.exception("scanner pre-entry refresh job failed")


def init_scanner_preentry_refresh(app=None, scheduler=None) -> None:
    """Register the 09:16 IST APScheduler job for the scanner pre-entry refresh.

    Registered even when the flag is off so toggling ``SCANNER_PREENTRY_REFRESH_ENABLED``
    at runtime takes effect without a restart; the per-fire ``preentry_refresh_enabled()``
    check gates the work.

    Args:
        app: Flask app instance (not used; kept for interface parity with other init fns).
        scheduler: APScheduler instance. Defaults to the shared historify scheduler.
    """
    try:
        from apscheduler.triggers.cron import CronTrigger

        if scheduler is None:
            from services.historify_scheduler_service import get_historify_scheduler

            scheduler = get_historify_scheduler()
        if scheduler is None:
            logger.warning(
                "scanner pre-entry refresh: no scheduler available — skipping job registration"
            )
            return

        _pre_t = preentry_refresh_time()
        scheduler.add_job(
            _scanner_preentry_refresh_job,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=_pre_t.hour,
                minute=_pre_t.minute,
                timezone="Asia/Kolkata",
            ),
            id="scanner_preentry_refresh",
            replace_existing=True,
            name=(
                f"Scanner pre-entry data refresh + WS subscribe nudge "
                f"({_pre_t.hour:02d}:{_pre_t.minute:02d} IST)"
            ),
        )
        logger.info(
            "scanner_preentry_refresh registered (enabled=%s, time=%02d:%02d IST)",
            preentry_refresh_enabled(),
            _pre_t.hour,
            _pre_t.minute,
        )
    except Exception:
        logger.exception("init_scanner_preentry_refresh failed")
