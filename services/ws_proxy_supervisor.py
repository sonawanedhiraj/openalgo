"""WS-proxy subprocess supervision (issue #376).

The 2026-07-07 incident: a libzmq hard-assert (``No buffer space available
[10055]``, select.cpp:410) killed the WS/ZMQ side at 13:42 IST while Flask kept
serving — 42 minutes of silent tick outage until a manual restart. Flask alive
!= pipeline alive (same class as the 2026-06-23 silent-death incident, #106).

This module polls the WS proxy's runtime handle (subprocess under
gunicorn+eventlet, real OS thread under the dev server — see
``websocket_proxy.app_integration.get_websocket_runtime_status``) every
``_POLL_SEC`` seconds from a daemon thread in the Flask process:

* **Unexpected death** → ``logger.error`` + Telegram alert (event type
  ``ws_proxy_died``) + auto-restart with a ``_RESTART_BACKOFF_SEC`` pause,
  capped at ``WS_PROXY_MAX_RESTARTS_PER_DAY`` (default 3) per IST day.
* **Cap exhausted** → one CRITICAL alert ("operator territory") and no
  further restart attempts until the IST day rolls over.
* Auto-restart runs at ANY hour; Telegram alerts are suppressed outside
  the alerting window (trading day, 08:45-16:00 IST) to avoid off-hours
  spam — the restart itself is still logged at ERROR.

The supervisor NEVER touches the main Flask process — it only respawns the
WS child (or dev-server WS thread). Re-subscription after a restart rides the
existing machinery: the proxy child rebuilds broker adapters on client auth,
the scanner's WS client reconnect loop + ``scanner_presubscribe`` connect
callback re-subscribe the universe, and ``ws_recovery_service`` replays missed
bars on the next broker-session refresh.

The tick-liveness watchdog's auto-heal ladder (step 3) shares this module's
restart budget via :func:`request_supervised_restart`, so ladder restarts and
death-detected restarts count against the same daily cap.

Everything is fail-graceful: the poll loop swallows all exceptions; a failed
restart is alerted and retried on the next death detection (subject to cap).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Poll / backoff cadence. Deliberately module constants (not env) — the issue
# brief fixes the poll at ~30s and there is no per-deploy reason to tune them.
_POLL_SEC = 30.0
_RESTART_BACKOFF_SEC = 60.0

# Telegram-alert window (IST). Restarts outside it are log-only.
_ALERT_WINDOW_START = dt_time(8, 45)
_ALERT_WINDOW_END = dt_time(16, 0)


def max_restarts_per_day() -> int:
    """``WS_PROXY_MAX_RESTARTS_PER_DAY`` (default 3)."""
    try:
        return int(os.getenv("WS_PROXY_MAX_RESTARTS_PER_DAY", "3"))
    except ValueError:
        return 3


def _default_status_provider() -> tuple[str, bool]:
    from websocket_proxy.app_integration import get_websocket_runtime_status

    return get_websocket_runtime_status()


def _default_restarter() -> bool:
    from websocket_proxy.app_integration import restart_websocket_server

    return restart_websocket_server()


def _default_notifier(message: str) -> None:
    """Telegram via the shared notification service (``ws_proxy_died`` event).

    Fail-safe — never raises back into the supervisor loop."""
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("ws_proxy_died", message)
    except Exception:
        logger.exception("ws_proxy_supervisor: Telegram notify failed")


def _default_trading_day_checker(d: date) -> bool:
    try:
        from services.data_freshness_service import is_trading_day

        return is_trading_day(d)
    except Exception:
        logger.debug("ws_proxy_supervisor: is_trading_day failed — assuming weekday rule")
        return d.weekday() < 5


class WSProxySupervisor:
    """Detect WS-proxy death, alert, and auto-restart within a daily budget.

    All I/O is injected for testability; ``check()`` is the pure-ish policy
    step the daemon loop drives.
    """

    def __init__(
        self,
        *,
        status_provider: Callable[[], tuple[str, bool]] = _default_status_provider,
        restarter: Callable[[], bool] = _default_restarter,
        notifier: Callable[[str], None] = _default_notifier,
        now_provider: Callable[[], datetime] | None = None,
        trading_day_checker: Callable[[date], bool] = _default_trading_day_checker,
        sleep_fn: Callable[[float], None] = time.sleep,
        backoff_sec: float = _RESTART_BACKOFF_SEC,
    ) -> None:
        self._status_provider = status_provider
        self._restarter = restarter
        self._notifier = notifier
        self._now = now_provider or (lambda: datetime.now(tz=_IST))
        self._is_trading_day = trading_day_checker
        self._sleep = sleep_fn
        self._backoff_sec = backoff_sec
        self._lock = threading.Lock()
        self._restart_day: date | None = None
        self._restarts_today = 0
        self._cap_alerted = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- policy --------------------------------------------------------------

    def _roll_day(self, today: date) -> None:
        if self._restart_day != today:
            self._restart_day = today
            self._restarts_today = 0
            self._cap_alerted = False

    def _alerting_hours(self, now: datetime) -> bool:
        ist = now.astimezone(_IST)
        if not self._is_trading_day(ist.date()):
            return False
        return _ALERT_WINDOW_START <= ist.time() <= _ALERT_WINDOW_END

    def _alert(self, message: str, *, force: bool = False) -> None:
        if force or self._alerting_hours(self._now()):
            self._notifier(message)
        else:
            logger.info("ws_proxy_supervisor: alert suppressed off-hours: %s", message)

    def check(self) -> str:
        """One supervision step. Returns a status string for tests/introspection."""
        mode, alive = self._status_provider()
        if mode == "none":
            return "not_managed"
        if alive:
            return "alive"

        now = self._now().astimezone(_IST)
        self._roll_day(now.date())

        cap = max_restarts_per_day()
        logger.error(
            "WS proxy DEAD (mode=%s) — Flask is alive but the tick pipeline is down "
            "(restarts today: %d/%d). Issue #376.",
            mode,
            self._restarts_today,
            cap,
        )

        if self._restarts_today >= cap:
            if not self._cap_alerted:
                self._cap_alerted = True
                # Cap exhaustion is terminal for the day — always alert, even
                # off-hours (single message, not spam).
                self._alert(
                    f"🚨 CRITICAL: WS proxy ({mode}) died again but the daily "
                    f"auto-restart cap ({cap}) is exhausted — NOT restarting. "
                    "Tick pipeline is DOWN until a manual OpenAlgo restart. "
                    "Issue #376.",
                    force=True,
                )
                return "cap_exceeded_alerted"
            return "cap_exceeded"

        attempt = self._restarts_today + 1
        self._alert(
            f"🚨 WS proxy ({mode}) exited unexpectedly — Flask is alive but the "
            f"tick pipeline is down. Auto-restarting in {int(self._backoff_sec)}s "
            f"(attempt {attempt}/{cap}). Issue #376."
        )
        try:
            self._sleep(self._backoff_sec)
        except Exception:
            logger.debug("ws_proxy_supervisor: backoff sleep interrupted", exc_info=True)

        self._restarts_today = attempt
        try:
            ok = bool(self._restarter())
        except Exception:
            logger.exception("ws_proxy_supervisor: restart raised")
            ok = False

        if ok:
            logger.warning("WS proxy restarted OK (attempt %d/%d)", attempt, cap)
            self._alert(f"✅ WS proxy restarted OK (attempt {attempt}/{cap}).")
            return "restarted"
        logger.error("WS proxy restart FAILED (attempt %d/%d)", attempt, cap)
        self._alert(
            f"🚨 WS proxy restart FAILED (attempt {attempt}/{cap}) — will retry "
            "on next death detection while budget remains."
        )
        return "restart_failed"

    def request_restart(self, reason: str) -> str:
        """Externally-requested restart (tick-liveness auto-heal step 3).

        Consumes the same daily budget as death-detected restarts. Only acts
        when the proxy handle exists; if the proxy is currently *alive* it is
        left alone — a stalled-but-alive pipeline is the adapter-reconnect
        step's job, not ours (restarting a live proxy would drop every client).
        Returns a status string. Never raises.
        """
        with self._lock:
            try:
                mode, alive = self._status_provider()
                if mode == "none":
                    return "not_managed"
                if alive:
                    logger.info(
                        "ws_proxy_supervisor: restart requested (%s) but proxy is alive — "
                        "skipping (stall recovery is the adapter-reconnect step's job)",
                        reason,
                    )
                    return "alive_skip"
                logger.error("ws_proxy_supervisor: restart requested: %s", reason)
                return self.check()
            except Exception:
                logger.exception("ws_proxy_supervisor.request_restart failed")
                return "error"

    # -- daemon loop ---------------------------------------------------------

    def _loop(self) -> None:
        from services.thread_registry import beat as _beat

        while not self._stop.is_set():
            _beat("WSProxySupervisor")
            try:
                with self._lock:
                    self.check()
            except Exception:
                logger.exception("ws_proxy_supervisor check failed")
            self._stop.wait(_POLL_SEC)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="WSProxySupervisor")
        self._thread.start()
        logger.info(
            "WS proxy supervisor started (poll=%ds, backoff=%ds, max_restarts/day=%d)",
            int(_POLL_SEC),
            int(self._backoff_sec),
            max_restarts_per_day(),
        )

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
# Module singleton + wiring
# --------------------------------------------------------------------------- #

_supervisor: WSProxySupervisor | None = None


def get_supervisor() -> WSProxySupervisor | None:
    return _supervisor


def request_supervised_restart(reason: str) -> str:
    """Entry point for the tick-liveness auto-heal ladder (step 3).

    Routes through the running supervisor so the restart consumes the shared
    daily budget. Returns ``"unavailable"`` when no supervisor was initialized
    (e.g. Docker/standalone mode). Never raises.
    """
    sup = _supervisor
    if sup is None:
        logger.warning("request_supervised_restart(%s): no supervisor initialized", reason)
        return "unavailable"
    return sup.request_restart(reason)


def init_ws_proxy_supervisor(app=None) -> WSProxySupervisor | None:
    """Build and start the process-wide supervisor. Idempotent; never raises.

    Skips (returns None) when this process manages no WS proxy — e.g.
    Docker/standalone mode where start.sh runs the proxy externally.
    """
    global _supervisor
    try:
        if _supervisor is not None:
            return _supervisor
        mode, _alive = _default_status_provider()
        if mode == "none":
            logger.info("WS proxy supervisor: no in-process WS proxy to supervise — skipping")
            return None
        _supervisor = WSProxySupervisor()
        _supervisor.start()
        if app is not None:
            app.ws_proxy_supervisor = _supervisor
        return _supervisor
    except Exception:
        logger.exception("init_ws_proxy_supervisor failed")
        return None
