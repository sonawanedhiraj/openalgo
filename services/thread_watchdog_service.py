"""Thread-count watchdog.

Reads the latest ``health_metrics.thread_count`` row written by the
background health collector (``utils/health_monitor.py``) and emits a
``thread_count_high`` health alert plus a Telegram push when the count
crosses a configurable threshold.

Dedup policy
------------
* **Transition**: a new alert fires whenever the severity level changes
  (None → WARNING, WARNING → CRITICAL, CRITICAL → WARNING, any → None resolve).
* **Sustained**: while the count stays at the same severity level, a
  reminder alert fires at most once every ``THREAD_WATCHDOG_DEDUP_WINDOW_MIN``
  minutes (default 15). This prevents alert storms while still surfacing a
  sustained leak.
* **Resolution**: when the count drops below the WARNING threshold, any open
  ``thread_count_high`` alerts are auto-resolved and state is reset — the next
  crossing fires a fresh alert.

This watchdog would have surfaced the 2026-06-22 #76 incident ~2 hours before
the crash (thread count climbed 20→543 due to leaked asyncio threads; a WARN
at 100 and CRIT at 200 would have fired well before Windows select()
FD_SETSIZE limit (~512) killed the WS proxy at 10:23 IST).
"""

from __future__ import annotations

import os
import threading
import time

from utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

THREAD_WATCHDOG_ENABLED: bool = os.getenv("THREAD_WATCHDOG_ENABLED", "true").lower() == "true"
WARN_THRESHOLD: int = int(os.getenv("THREAD_WATCHDOG_WARN_THRESHOLD", "100"))
CRIT_THRESHOLD: int = int(os.getenv("THREAD_WATCHDOG_CRIT_THRESHOLD", "200"))
DEDUP_WINDOW_MIN: int = int(os.getenv("THREAD_WATCHDOG_DEDUP_WINDOW_MIN", "15"))
CHECK_INTERVAL_SEC: int = int(os.getenv("THREAD_WATCHDOG_CHECK_INTERVAL_SEC", "30"))


# ---------------------------------------------------------------------------
# Default side-effect callbacks (substituted in tests)
# ---------------------------------------------------------------------------


def _default_alert_writer(count: int, level: str, threshold: float) -> None:
    """Write a ``thread_count_high`` row to ``health_alerts``."""
    msg = f"Thread count {level}: {count} (threshold: {int(threshold)})"
    try:
        from database.health_db import HealthAlert, health_session

        HealthAlert.create_alert(
            alert_type="thread_count_high",
            severity=level,
            metric_name="thread_count",
            metric_value=float(count),
            threshold_value=float(threshold),
            message=msg,
        )
        health_session.remove()
    except Exception:
        logger.exception("thread_watchdog: HealthAlert.create_alert failed")
    logger.warning("thread watchdog: %s", msg)


def _default_notifier(count: int, level: str) -> None:
    """Telegram push via the existing anomaly-alert path."""
    try:
        from services.notification_service import get_notification_service

        get_notification_service().publish_anomaly_alert(
            source="thread_watchdog",
            message=f"Thread count {level}: {count}",
            severity=level,
        )
    except Exception:
        logger.exception("thread_watchdog: notification failed")


def _default_resolver(warn_threshold: int) -> None:
    """Auto-resolve open ``thread_count_high`` alerts."""
    try:
        from database.health_db import HealthAlert, health_session

        HealthAlert.auto_resolve_alerts("thread_count", 0, warn_threshold)
        health_session.remove()
    except Exception:
        logger.exception("thread_watchdog: auto_resolve_alerts failed")
    logger.info("thread watchdog: thread count returned to normal — alerts resolved")


# ---------------------------------------------------------------------------
# Core watchdog
# ---------------------------------------------------------------------------


class ThreadWatchdog:
    """Stateful threshold watchdog for ``thread_count``.

    Construct once and call :meth:`check` on each new reading. The *_fn*
    constructor parameters exist purely for test injection — production code
    should leave them as ``None`` (defaulting to the module-level helpers).
    """

    def __init__(
        self,
        warn_threshold: int | None = None,
        crit_threshold: int | None = None,
        dedup_window_min: int | None = None,
        alert_writer=None,
        notifier=None,
        resolver=None,
        _time_fn=None,
    ) -> None:
        self.warn_threshold: int = warn_threshold if warn_threshold is not None else WARN_THRESHOLD
        self.crit_threshold: int = crit_threshold if crit_threshold is not None else CRIT_THRESHOLD
        self.dedup_window_sec: float = (
            dedup_window_min if dedup_window_min is not None else DEDUP_WINDOW_MIN
        ) * 60.0
        self._alert_writer = alert_writer or _default_alert_writer
        self._notifier = notifier or _default_notifier
        # Bind warn_threshold at construction so the lambda captures the right value.
        _wt = self.warn_threshold
        self._resolver = resolver if resolver is not None else (lambda: _default_resolver(_wt))

        self._time_fn = _time_fn if _time_fn is not None else time.monotonic
        self._last_level: str | None = None  # None | "warning" | "critical"
        self._last_alert_at: float | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, count: int) -> str | None:
        """Evaluate *count* and fire an alert if warranted.

        Returns the severity level that was fired (``"warning"`` or
        ``"critical"``), or ``None`` when no alert fired this call.
        Thread-safe; side effects (DB write, Telegram push) happen outside
        the state lock to avoid blocking other callers.
        """
        with self._lock:
            level = self._level_for(count)
            now = self._time_fn()

            if level is None:
                was_above = self._last_level is not None
                self._last_level = None
                self._last_alert_at = None
                action: str = "resolve" if was_above else "none"
            else:
                is_transition = level != self._last_level
                elapsed = (
                    (now - self._last_alert_at) if self._last_alert_at is not None else float("inf")
                )
                if is_transition or elapsed >= self.dedup_window_sec:
                    self._last_level = level
                    self._last_alert_at = now
                    action = "fire"
                else:
                    action = "none"

        # Side effects outside the lock
        if action == "resolve":
            self._resolver()
            return None
        if action == "fire":
            threshold = self.crit_threshold if level == "critical" else self.warn_threshold
            self._alert_writer(count, level, threshold)
            self._notifier(count, level)
            return level
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _level_for(self, count: int) -> str | None:
        if count >= self.crit_threshold:
            return "critical"
        if count >= self.warn_threshold:
            return "warning"
        return None


# ---------------------------------------------------------------------------
# Module-level singleton + daemon loop
# ---------------------------------------------------------------------------

_watchdog_instance: ThreadWatchdog | None = None
_watchdog_running: bool = False
_watchdog_thread: threading.Thread | None = None
_init_lock = threading.Lock()


def _watchdog_loop() -> None:
    from database.health_db import HealthMetric, health_session

    logger.debug("Thread watchdog loop started (interval: %ds)", CHECK_INTERVAL_SEC)
    while _watchdog_running:
        try:
            metric = HealthMetric.get_current_metrics()
            if (
                metric is not None
                and metric.thread_count is not None
                and _watchdog_instance is not None
            ):
                _watchdog_instance.check(metric.thread_count)
        except Exception:
            logger.exception("thread_watchdog: loop iteration failed")
        finally:
            try:
                health_session.remove()
            except Exception:
                pass

        time.sleep(CHECK_INTERVAL_SEC)

    logger.debug("Thread watchdog loop stopped")


def init_thread_watchdog(app=None) -> None:  # noqa: ARG001
    """Initialize the thread-count watchdog daemon.

    Idempotent — safe to call multiple times (subsequent calls are no-ops).
    The *app* parameter is accepted for consistency with other ``init_*``
    hooks but is not used.
    """
    global _watchdog_instance, _watchdog_running, _watchdog_thread

    if not THREAD_WATCHDOG_ENABLED:
        logger.info("Thread watchdog disabled (THREAD_WATCHDOG_ENABLED=false)")
        return

    with _init_lock:
        if _watchdog_instance is not None:
            return

        _watchdog_instance = ThreadWatchdog()
        _watchdog_running = True
        _watchdog_thread = threading.Thread(
            target=_watchdog_loop,
            name="ThreadWatchdog",
            daemon=True,
        )
        _watchdog_thread.start()

    logger.info(
        "Thread watchdog started (WARN=%d, CRIT=%d, dedup=%dmin, interval=%ds)",
        _watchdog_instance.warn_threshold,
        _watchdog_instance.crit_threshold,
        DEDUP_WINDOW_MIN,
        CHECK_INTERVAL_SEC,
    )
