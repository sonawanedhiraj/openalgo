"""EOD watchdog — APScheduler safety net for intraday position flattening.

On 2026-06-01 a real NBCC LONG 500 was stranded past 15:20 IST EOD because:

* OpenAlgo was restarted three times between 15:01 and 15:06 IST.
* The broker WebSocket tick stream never resumed post-restart.
* ``SimplifiedStockEngineService._maybe_flatten_eod`` is tick-driven only —
  it fires from ``on_quote()``. No ticks = no EOD check.
* The engine's in-memory positions dict was wiped by restart, so even if
  ticks had resumed, the engine wouldn't have known NBCC was open.

This module schedules one daily cron job per registered intraday strategy
that calls :func:`services.simplified_stock_engine_service.flatten_strategy_positions`
at ``min(strategy.eod_exit_time, SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME)`` — the
cap (default ``15:14`` IST) guarantees the watchdog fires *before* the venue's
15:15 MIS auto-square-off, which rejects MIS orders placed after it (the
2026-06-10 OIL/HINDZINC/TATAELXSI orphans were caused by the watchdog firing at
the declared 15:20, after sandbox had already force-closed and started blocking
flatten orders). The whole watchdog is gated by
``SIMPLIFIED_ENGINE_EOD_WATCHDOG_ENABLED`` (default ``true``). The flatten path
goes through
``services.place_order_service.place_order`` (an in-process REST call), so it
works even when the broker tick stream is dead — as long as the broker's
order endpoint is up.

The scheduler is independent of the broker WebSocket, independent of any
APScheduler instance used elsewhere in OpenAlgo (Python strategies, Flow,
Historify), and independent of the tick-driven EOD path. The two EOD
exits are by design redundant — the tick-driven one fires earlier
(intra-tick, around the first tick after ``eod_exit_time``) and the
watchdog catches anything the tick path missed.

Misfire grace: ``300s``. If APScheduler is busy or the process was paused
when the cron fired, the job catches up within 5 minutes — enough room
for a slow restart but tight enough that an EOD job doesn't run an hour
late on a wedged scheduler.

Failure handling: per-strategy job exceptions are caught and routed to
:func:`services.notification_service.NotificationService.publish_eod_watchdog_failure`.
The watchdog's job is never to crash — operator-visible Telegram + a
loud log are the right escalation.
"""

from __future__ import annotations

import os
import threading
from typing import Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from strategies import list_intraday_strategies, registered_strategies
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# The watchdog must fire BEFORE the venue's MIS auto-square-off, not after.
# In sandbox the NSE/BSE/NFO/BFO square-off is 15:15 IST
# (``nse_bse_square_off_time``, see sandbox/squareoff_manager.py) and the
# sandbox order_manager *rejects* MIS orders placed at/after that time
# ("MIS orders cannot be placed after square-off time"). The engine's own
# tick-driven exit and this watchdog both keyed off the strategy's declared
# ``eod_exit_time`` (15:20) — five minutes too late — so on 2026-06-10 the
# watchdog's 15:20 flatten orders for OIL/HINDZINC/TATAELXSI were blocked by
# sandbox and the positions fell to sandbox MIS auto-square-off instead
# (only reconciled into the journal at 15:30). We cap the watchdog fire time
# so it always runs at least one minute before the venue closes MIS.
_WATCHDOG_CAP_TIME = "15:14"


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var. Mirrors the helper in
    simplified_stock_engine_service (kept local to avoid an import cycle —
    this module is imported at app boot before the engine service)."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Module-level singleton — there's only ever one watchdog per process. We use
# a lock around start/stop so a (theoretical) concurrent restart can't race
# and leave two schedulers running.
_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def start_eod_watchdog() -> dict[str, Any]:
    """Start the EOD watchdog scheduler. Idempotent — calling twice is a no-op.

    Returns a summary dict::

        {
            "started": <bool>,
            "jobs": [{"strategy": "<name>", "eod_exit_time": "HH:MM"}, ...],
            "skipped": [{"strategy": "<name>", "reason": "<reason>"}, ...],
        }

    Skipped reasons:

    * ``positional`` — strategy declared ``intraday = False``.
    * ``bad_time`` — strategy's ``eod_exit_time`` doesn't parse as ``HH:MM``.
    """
    global _scheduler

    if not _env_bool("SIMPLIFIED_ENGINE_EOD_WATCHDOG_ENABLED", True):
        logger.info(
            "[EOD-WATCHDOG] disabled via SIMPLIFIED_ENGINE_EOD_WATCHDOG_ENABLED — not starting"
        )
        return {"started": False, "jobs": [], "skipped": [], "disabled": True}

    with _lock:
        if _scheduler is not None and _scheduler.running:
            logger.warning("[EOD-WATCHDOG] start_eod_watchdog called but scheduler already running")
            return {"started": False, "jobs": [], "skipped": []}

        _scheduler = BackgroundScheduler(
            timezone=IST,
            # Keep the executor pool tiny — at most one job per strategy fires
            # per day, and they don't overlap.
            executors={
                "default": {
                    "type": "threadpool",
                    "max_workers": 2,
                }
            },
        )

    jobs: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    # Surface positional strategies for diagnostics — operators sometimes
    # add an overnight strategy and wonder why the watchdog ignores it.
    try:
        all_registered = registered_strategies()
        intraday = list_intraday_strategies()
        intraday_names = {n for n, _ in intraday}
        for name in all_registered.keys() - intraday_names:
            skipped.append({"strategy": name, "reason": "positional"})
    except Exception:
        logger.exception("[EOD-WATCHDOG] registry enumeration failed")
        return {"started": False, "jobs": [], "skipped": []}

    # Resolve the fire-time cap once. The watchdog never fires later than this
    # so it always beats the venue MIS square-off (see _WATCHDOG_CAP_TIME).
    cap = _parse_hhmm(os.getenv("SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME", _WATCHDOG_CAP_TIME))
    if cap is None:
        logger.error(
            "[EOD-WATCHDOG] invalid SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME — falling back to %s",
            _WATCHDOG_CAP_TIME,
        )
        cap = _parse_hhmm(_WATCHDOG_CAP_TIME)

    for strategy_name, eod_time in intraday:
        parsed = _parse_hhmm(eod_time)
        if parsed is None:
            logger.error(
                "[EOD-WATCHDOG] %s has invalid eod_exit_time=%r — skipping",
                strategy_name,
                eod_time,
            )
            skipped.append({"strategy": strategy_name, "reason": "bad_time"})
            continue

        # Fire at min(declared eod_exit_time, cap): honor an earlier
        # strategy cut-off, but never run after the venue square-off.
        hh, mm = min(parsed, cap)
        fire_time = f"{hh:02d}:{mm:02d}"

        _scheduler.add_job(
            _run_strategy_eod_flatten,
            CronTrigger(
                hour=hh,
                minute=mm,
                day_of_week="mon-fri",
                timezone=IST,
            ),
            args=[strategy_name],
            id=f"eod_watchdog_{strategy_name}",
            replace_existing=True,
            misfire_grace_time=300,
        )
        jobs.append(
            {
                "strategy": strategy_name,
                "eod_exit_time": eod_time,
                "fire_time": fire_time,
            }
        )
        logger.info(
            "[EOD-WATCHDOG] Scheduled %s daily at %s IST (mon-fri; declared=%s)",
            strategy_name,
            fire_time,
            eod_time,
        )

    if not jobs:
        logger.warning("[EOD-WATCHDOG] No intraday strategies registered — watchdog idle")

    _scheduler.start()
    logger.info("[EOD-WATCHDOG] Started (jobs=%d, skipped=%d)", len(jobs), len(skipped))
    return {"started": True, "jobs": jobs, "skipped": skipped}


def stop_eod_watchdog() -> None:
    """Stop the watchdog and release the singleton.

    Safe to call when the scheduler isn't running. Used by tests to tear
    down between cases.
    """
    global _scheduler
    with _lock:
        if _scheduler is None:
            return
        try:
            if _scheduler.running:
                _scheduler.shutdown(wait=False)
        except Exception:
            logger.exception("[EOD-WATCHDOG] shutdown raised — ignoring")
        finally:
            _scheduler = None


def get_scheduler() -> BackgroundScheduler | None:
    """Return the active scheduler for tests / status surfaces. ``None`` when
    not started."""
    return _scheduler


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    """Parse ``"HH:MM"`` into ``(hour, minute)``; return ``None`` if invalid."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def _run_strategy_eod_flatten(strategy_name: str) -> None:
    """Cron job body. Calls flatten_strategy_positions and notifies the operator.

    Wrapped in a broad try/except — the watchdog must never crash the
    APScheduler thread. Any unexpected exception escalates to a Telegram
    alert and a logger.exception with the full traceback in errors.jsonl.
    """
    logger.info("[EOD-WATCHDOG] Firing for strategy=%s", strategy_name)
    try:
        from services.simplified_stock_engine_service import flatten_strategy_positions

        result = flatten_strategy_positions(strategy_name, reason="eod_watchdog")
        logger.info(
            "[EOD-WATCHDOG] %s result: attempted=%d succeeded=%d failed=%d skipped=%d",
            strategy_name,
            result.get("attempted", 0),
            result.get("succeeded", 0),
            len(result.get("failed", []) or []),
            len(result.get("skipped", []) or []),
        )
        _publish_summary(strategy_name, result)
    except Exception as e:
        logger.exception("[EOD-WATCHDOG] %s job crashed", strategy_name)
        try:
            from services.notification_service import get_notification_service

            get_notification_service().publish_eod_watchdog_failure(
                strategy_name=strategy_name,
                error=f"watchdog crashed: {e}",
            )
        except Exception:
            logger.warning(
                "[EOD-WATCHDOG] crash-notification also failed for %s",
                strategy_name,
            )


def _publish_summary(strategy_name: str, result: dict[str, Any]) -> None:
    """Send the per-job summary to the notification service. Fail-safe."""
    try:
        from services.notification_service import get_notification_service

        get_notification_service().publish_eod_watchdog_summary(
            strategy_name=strategy_name, result=result
        )
    except Exception as e:  # noqa: BLE001 — fail-safe
        logger.warning(
            "[EOD-WATCHDOG] summary notification failed for %s: %s",
            strategy_name,
            e,
        )
