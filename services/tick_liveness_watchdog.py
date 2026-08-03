"""Tick-liveness watchdog + auto-heal ladder + socket instrumentation (issue #376).

The 2026-07-07 incident: a libzmq hard-assert (WSAENOBUFS 10055) killed the
WS/ZMQ side at 13:42 IST while Flask kept serving. Every existing guard was
blind to a TOTAL tick-flow death:

* the completeness metric only emits when its window ROLLS — which requires a
  bar close, which requires ticks (documented blind spot, hit that day);
* the scanner_dry tripwire's 30-min threshold is too slow for a mid-session
  stall and it watches *rule output*, not the feed;
* ``scanner_ws_watchdog`` recovers the scanner's WS *client* but never alerts,
  and client-side reconnects cannot heal a dead proxy *server*.

This watchdog polls — on its OWN clock, independent of the tick stream — the
wall-time of the last genuine live bar close
(``services.scanner_service.get_last_live_bar_close_wall``, stamped by
``_on_bar_close`` for non-replay bars only). On trading days between
09:25 and 15:30 IST (grace after open; holiday-aware via
``data_freshness_service.is_trading_day``), silence longer than
``SCANNER_LIVENESS_MAX_SILENT_MIN`` (default 10) minutes triggers:

1. ``logger.error`` + Telegram CRIT (registered event type ``tick_liveness``),
   re-alerted at most every ``SCANNER_LIVENESS_REALERT_MIN`` (default 30)
   minutes while the outage persists, and one INFO "recovered after X min"
   line (plus a Telegram notice) when bars resume.
2. An **auto-heal escalation ladder** (``TICK_LIVENESS_AUTOHEAL_ENABLED``,
   default true), one step per ~2 minutes while still dark:

   * **Step 1 — re-subscribe nudge**: the same mechanism as the #296
     pre-entry refresh (``scanner_pre_subscriber.ensure``), with
     ``reset=True`` so already-"subscribed" symbols are re-issued.
   * **Step 2 — broker-adapter reconnect**: publish a FEED cache-invalidation
     (``database.cache_invalidation.publish_feed_cache_invalidation``) — the
     exact ZMQ event the daily ~3AM re-login rides; the proxy's
     ``_handle_cache_invalidation`` → ``_reconnect_broker_adapter`` snapshots
     subscriptions, reconnects with a fresh token, and re-subscribes.
   * **Step 3 — WS-proxy restart**: ``ws_proxy_supervisor.
     request_supervised_restart`` (shares the supervisor's daily restart cap).
   * **Terminal**: if bars are still dark after step 3, one CRITICAL alert
     naming everything tried — "manual OpenAlgo restart required". The only
     remaining cause is main-process-level failure, which in-process code
     cannot heal (external supervision is tracked separately, #106).

   Each step is individually fail-graceful (a raising step logs and the
   ladder escalates); a step that restores bar flow resets the ladder; the
   whole ladder runs at most once per
   ``SCANNER_LIVENESS_LADDER_COOLDOWN_MIN`` (default 30) minutes.

3. **Hourly resource instrumentation** (the 10055 lead): one INFO line with
   process handle/TCP/thread counts (psutil — already a dependency; ctypes
   ``GetProcessHandleCount`` fallback), the WS child's counts when it exists,
   and the scanner pre-subscribe symbol count. Trend data only; never raises.

Master flag ``TICK_LIVENESS_WATCHDOG_ENABLED`` (default true) — consulted
per-check so a runtime flip takes effect without re-init. When
``SCANNER_ENABLED`` != true there are no bar closes by construction, so the
check is a no-op.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

_SESSION_OPEN = dt_time(9, 15)
_WINDOW_START = dt_time(9, 25)  # grace after open — first bars need ~10 min
_WINDOW_END = dt_time(15, 30)

# Poll cadence + ladder step pacing. Module constants by design (not envs).
_POLL_SEC = 30.0
_STEP_WAIT_SEC = 120.0  # ~2 min for a step to show effect before escalating
_INSTRUMENTATION_INTERVAL_SEC = 3600.0


# --------------------------------------------------------------------------- #
# Flags / thresholds
# --------------------------------------------------------------------------- #


def watchdog_enabled() -> bool:
    """``TICK_LIVENESS_WATCHDOG_ENABLED`` env (default true)."""
    return os.getenv("TICK_LIVENESS_WATCHDOG_ENABLED", "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def autoheal_enabled() -> bool:
    """``TICK_LIVENESS_AUTOHEAL_ENABLED`` env (default true). When false the
    watchdog is alert-only — steps 1-3 never run."""
    return os.getenv("TICK_LIVENESS_AUTOHEAL_ENABLED", "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def _scanner_enabled() -> bool:
    return os.getenv("SCANNER_ENABLED", "false").strip().lower() == "true"


def max_silent_min() -> int:
    """``SCANNER_LIVENESS_MAX_SILENT_MIN`` (default 10)."""
    try:
        return max(1, int(os.getenv("SCANNER_LIVENESS_MAX_SILENT_MIN", "10")))
    except ValueError:
        return 10


def realert_min() -> int:
    """``SCANNER_LIVENESS_REALERT_MIN`` (default 30)."""
    try:
        return max(1, int(os.getenv("SCANNER_LIVENESS_REALERT_MIN", "30")))
    except ValueError:
        return 30


def ladder_cooldown_min() -> int:
    """``SCANNER_LIVENESS_LADDER_COOLDOWN_MIN`` (default 30)."""
    try:
        return max(1, int(os.getenv("SCANNER_LIVENESS_LADDER_COOLDOWN_MIN", "30")))
    except ValueError:
        return 30


# --------------------------------------------------------------------------- #
# Production wiring (injected into the watchdog; each is fail-graceful)
# --------------------------------------------------------------------------- #


def production_last_bar_provider() -> float | None:
    """Wall-clock of the scanner's last LIVE (non-replay) bar close."""
    try:
        from services.scanner_service import get_last_live_bar_close_wall

        return get_last_live_bar_close_wall()
    except Exception:
        logger.debug("tick_liveness: last-bar provider failed", exc_info=True)
        return None


def production_notifier(message: str) -> None:
    """Telegram via the shared notification service (``tick_liveness`` event)."""
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("tick_liveness", message)
    except Exception:
        logger.exception("tick_liveness: Telegram notify failed")


def production_trading_day_checker(d: date) -> bool:
    """NSE trading day — weekday AND not a market holiday (#253 semantics)."""
    try:
        from services.data_freshness_service import is_trading_day

        return is_trading_day(d)
    except Exception:
        logger.debug("tick_liveness: is_trading_day failed — weekday fallback", exc_info=True)
        return d.weekday() < 5


# -- auto-heal ladder steps ---------------------------------------------------


def _resolve_session() -> tuple[str | None, str | None]:
    """(user_id, broker) for the first configured API key, or (None, None)."""
    from database.auth_db import get_broker_name, get_first_available_api_key, verify_api_key

    api_key = get_first_available_api_key()
    if not api_key:
        return None, None
    user_id = verify_api_key(api_key)
    if not user_id:
        return None, None
    try:
        broker = get_broker_name(user_id)
    except Exception:
        broker = None
    return user_id, broker


def heal_step_resubscribe() -> bool:
    """Step 1 — force a WS re-subscribe of the scanner universe.

    The same nudge the #296 09:16 pre-entry refresh uses
    (``scanner_pre_subscriber.ensure``), but with ``reset=True`` so an
    already-populated ``subscribed`` set is cleared and re-issued — during a
    liveness outage the subscriptions usually *look* established.
    """
    from services.scanner_presubscribe import scanner_pre_subscriber

    user_id, broker = _resolve_session()
    if not user_id:
        logger.warning("tick_liveness auto-heal step 1: no broker session — cannot re-subscribe")
        return False
    raw = os.getenv("SCANNER_SYMBOLS", "")
    symbols = sorted({s.strip().upper() for s in raw.split(",") if s.strip()})
    if not symbols:
        logger.warning("tick_liveness auto-heal step 1: SCANNER_SYMBOLS empty — nothing to do")
        return False
    n = scanner_pre_subscriber.ensure(user_id, broker, symbols, reset=True)
    logger.warning("tick_liveness auto-heal step 1: re-subscribe issued for %d symbols", n)
    return True


def heal_step_adapter_reconnect() -> bool:
    """Step 2 — broker-adapter reconnect via the daily-relogin machinery.

    Publishes a FEED cache-invalidation on the shared ZMQ bus — exactly the
    event ``database.auth_db.upsert_auth`` emits on the ~3AM token refresh.
    The WS proxy's ``_handle_cache_invalidation`` consumes it and calls
    ``_reconnect_broker_adapter(user_id)``: snapshot held subscriptions,
    disconnect, re-read the token, reconnect, re-subscribe. If the proxy
    process is dead the publish goes nowhere — bars stay dark and the ladder
    escalates to step 3.
    """
    from database.cache_invalidation import publish_feed_cache_invalidation

    user_id, _broker = _resolve_session()
    if not user_id:
        logger.warning("tick_liveness auto-heal step 2: no broker session — cannot reconnect")
        return False
    ok = bool(publish_feed_cache_invalidation(user_id))
    logger.warning(
        "tick_liveness auto-heal step 2: adapter-reconnect (FEED cache-invalidate) published=%s",
        ok,
    )
    return ok


def heal_step_restart_ws_proxy() -> bool:
    """Step 3 — restart the WS proxy through the supervisor (shared daily cap)."""
    from services.ws_proxy_supervisor import request_supervised_restart

    status = request_supervised_restart(
        "tick_liveness auto-heal step 3: bars dark after re-subscribe + adapter reconnect"
    )
    logger.warning("tick_liveness auto-heal step 3: supervised restart → %s", status)
    return status == "restarted"


_DEFAULT_HEAL_STEPS: list[tuple[str, Callable[[], bool]]] = [
    ("resubscribe_nudge", heal_step_resubscribe),
    ("adapter_reconnect", heal_step_adapter_reconnect),
    ("ws_proxy_restart", heal_step_restart_ws_proxy),
]


# --------------------------------------------------------------------------- #
# Resource instrumentation (the 10055 lead) — one INFO line, never raises.
# --------------------------------------------------------------------------- #


def collect_resource_snapshot() -> dict[str, Any]:
    """Handle/TCP/thread counts for this process (+ the WS child if any).

    Best-effort on every field; a failed probe just omits its key."""
    snap: dict[str, Any] = {}
    try:
        import psutil  # noqa: PLC0415 — already a project dependency

        proc = psutil.Process()
        try:
            if hasattr(proc, "num_handles"):
                snap["handles"] = proc.num_handles()
            elif hasattr(proc, "num_fds"):
                snap["fds"] = proc.num_fds()
        except Exception:
            logger.debug("resource snapshot: handle count failed", exc_info=True)
        try:
            conn_fn = getattr(proc, "net_connections", None) or proc.connections
            snap["tcp_conns"] = len(conn_fn(kind="tcp"))
        except Exception:
            logger.debug("resource snapshot: tcp count failed", exc_info=True)
        try:
            snap["threads"] = proc.num_threads()
        except Exception:
            logger.debug("resource snapshot: thread count failed", exc_info=True)

        # The WS child is where the 07-07 WSAENOBUFS pressure most plausibly
        # built — trend its counts too when it exists.
        try:
            from websocket_proxy.app_integration import get_websocket_subprocess_pid

            pid = get_websocket_subprocess_pid()
            if pid:
                child = psutil.Process(pid)
                if hasattr(child, "num_handles"):
                    snap["ws_child_handles"] = child.num_handles()
                child_conn_fn = getattr(child, "net_connections", None) or child.connections
                snap["ws_child_tcp_conns"] = len(child_conn_fn(kind="tcp"))
        except Exception:
            logger.debug("resource snapshot: ws child probe failed", exc_info=True)
    except Exception:
        # psutil unavailable/broken — ctypes fallback for the handle count
        # only (Windows); TCP is skipped rather than adding a dependency.
        try:
            import ctypes  # noqa: PLC0415

            count = ctypes.c_ulong()
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
                snap["handles"] = int(count.value)
        except Exception:
            logger.debug("resource snapshot: ctypes fallback failed", exc_info=True)

    try:
        from services.scanner_presubscribe import scanner_pre_subscriber

        snap["presubscribed_symbols"] = len(scanner_pre_subscriber.subscribed)
    except Exception:
        logger.debug("resource snapshot: presubscribe count failed", exc_info=True)
    return snap


def log_resource_snapshot() -> None:
    """Emit the hourly INFO trend line. Guaranteed not to raise."""
    try:
        logger.info("resource snapshot (issue #376 / 10055 trend): %s", collect_resource_snapshot())
    except Exception:
        logger.debug("log_resource_snapshot failed", exc_info=True)


# --------------------------------------------------------------------------- #
# The watchdog
# --------------------------------------------------------------------------- #


class TickLivenessWatchdog:
    """Detect total tick-flow death mid-session; alert + auto-heal.

    All I/O is injected so ``check()`` is frozen-clock testable. The single
    time source is ``now_provider`` (an aware datetime); wall-seconds are
    derived via ``.timestamp()`` so injected clocks and the bar-close stamps
    (``time.time()`` in scanner_service) share one scale.
    """

    def __init__(
        self,
        *,
        last_bar_provider: Callable[[], float | None] = production_last_bar_provider,
        notifier: Callable[[str], None] = production_notifier,
        now_provider: Callable[[], datetime] | None = None,
        trading_day_checker: Callable[[date], bool] = production_trading_day_checker,
        heal_steps: list[tuple[str, Callable[[], bool]]] | None = None,
        started_wall: float | None = None,
    ) -> None:
        self._last_bar = last_bar_provider
        self._notifier = notifier
        self._now = now_provider or (lambda: datetime.now(tz=_IST))
        self._is_trading_day = trading_day_checker
        self._steps = list(heal_steps) if heal_steps is not None else list(_DEFAULT_HEAL_STEPS)
        # Baseline floor: a boot mid-session must not read "silent since
        # yesterday" — silence is measured from the LATER of (last bar,
        # today's 09:15 open, watchdog start).
        self._started_wall = started_wall if started_wall is not None else time.time()
        # Outage / alert state.
        self._outage_since_wall: float | None = None
        self._last_alert_wall: float | None = None
        self._last_heal_step_name: str | None = None
        # Ladder state.
        self._ladder_started_wall: float | None = None  # cooldown anchor (persists)
        self._ladder_step_idx: int = 0
        self._ladder_last_step_wall: float | None = None
        self._ladder_terminal_alerted: bool = False
        # Instrumentation cadence.
        self._last_instrumentation_wall: float = 0.0
        # Thread plumbing.
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- outage/ladder state helpers ------------------------------------------

    def _reset_outage(self) -> None:
        self._outage_since_wall = None
        self._last_alert_wall = None
        self._last_heal_step_name = None
        self._ladder_step_idx = 0
        self._ladder_last_step_wall = None
        self._ladder_terminal_alerted = False
        # NOTE: _ladder_started_wall is intentionally kept — it anchors the
        # once-per-cooldown rule across outage episodes.

    # -- the check -------------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """One liveness evaluation. Returns ``{status, ...}``; never raises
        (callers still wrap it — belt and braces)."""
        if not watchdog_enabled():
            return {"status": "flag_off"}
        if not _scanner_enabled():
            return {"status": "scanner_disabled"}

        now = self._now().astimezone(_IST)
        in_window = self._is_trading_day(now.date()) and _WINDOW_START <= now.time() <= _WINDOW_END
        if not in_window:
            if self._outage_since_wall is not None:
                logger.info(
                    "tick_liveness: market window closed during an active outage — "
                    "resetting outage state (no recovery observed in-session)"
                )
                self._reset_outage()
            return {"status": "off_hours"}

        now_wall = now.timestamp()
        session_open_wall = now.replace(
            hour=_SESSION_OPEN.hour, minute=_SESSION_OPEN.minute, second=0, microsecond=0
        ).timestamp()
        floor = max(session_open_wall, self._started_wall)
        try:
            last = self._last_bar()
        except Exception:
            logger.debug("tick_liveness: last_bar_provider raised", exc_info=True)
            last = None
        reference = max(last, floor) if last is not None else floor
        silent_min = max(0.0, (now_wall - reference) / 60.0)
        threshold = max_silent_min()

        if silent_min < threshold:
            if self._outage_since_wall is not None:
                outage_min = (now_wall - self._outage_since_wall) / 60.0
                step_note = self._last_heal_step_name or "none"
                logger.info(
                    "tick_liveness: RECOVERED after %.0f min of silence "
                    "(last auto-heal step tried: %s)",
                    outage_min,
                    step_note,
                )
                self._safe_notify(
                    f"✅ Tick liveness RECOVERED — live bars resumed after "
                    f"~{outage_min:.0f} min. Last auto-heal step tried: {step_note}."
                )
                self._reset_outage()
                return {"status": "recovered", "outage_min": outage_min}
            return {"status": "ok", "silent_min": silent_min}

        # --- silent beyond threshold: alert (throttled) + ladder -------------
        result: dict[str, Any] = {"status": "silent", "silent_min": silent_min}
        last_str = (
            datetime.fromtimestamp(last, tz=_IST).strftime("%H:%M:%S")
            if last is not None
            else "none since boot"
        )
        if self._outage_since_wall is None:
            self._outage_since_wall = reference
            self._last_alert_wall = now_wall
            self._alert_crit(silent_min, threshold, last_str, first=True)
            result["status"] = "alerted"
        elif (now_wall - (self._last_alert_wall or 0.0)) >= realert_min() * 60.0:
            self._last_alert_wall = now_wall
            self._alert_crit(silent_min, threshold, last_str, first=False)
            result["status"] = "realerted"

        result["ladder"] = self._maybe_run_ladder(now_wall)
        return result

    def _alert_crit(self, silent_min: float, threshold: int, last_str: str, *, first: bool) -> None:
        heal_note = (
            "Auto-heal ladder engaged."
            if autoheal_enabled()
            else "Auto-heal DISABLED (TICK_LIVENESS_AUTOHEAL_ENABLED=false) — alert-only."
        )
        prefix = "🚨 TICK LIVENESS CRIT" if first else "🚨 TICK LIVENESS CRIT (still down)"
        message = (
            f"{prefix}: NO live bar closes for {silent_min:.0f} min "
            f"(threshold {threshold}m; last live bar: {last_str}). Flask is alive "
            f"but the tick pipeline appears DEAD (WS proxy / ZMQ / broker feed). "
            f"{heal_note} Issue #376."
        )
        # f-string, not %s+args — see scanner_smoke_check_service for the
        # SensitiveDataFilter + record.args desync rationale.
        logger.error(f"tick_liveness: {message}")
        self._safe_notify(message)

    def _safe_notify(self, message: str) -> None:
        try:
            self._notifier(message)
        except Exception:
            logger.exception("tick_liveness: notifier failed")

    # -- auto-heal ladder ------------------------------------------------------

    def _maybe_run_ladder(self, now_wall: float) -> str:
        """Advance the escalation ladder by at most one step. Returns a status
        string describing what happened this tick."""
        if not autoheal_enabled():
            return "autoheal_off"

        starting_fresh = self._ladder_step_idx == 0 and self._ladder_last_step_wall is None
        if starting_fresh:
            cooldown_sec = ladder_cooldown_min() * 60.0
            if (
                self._ladder_started_wall is not None
                and (now_wall - self._ladder_started_wall) < cooldown_sec
            ):
                return "cooldown"
            self._ladder_started_wall = now_wall
            self._ladder_terminal_alerted = False
        elif (
            self._ladder_last_step_wall is not None
            and (now_wall - self._ladder_last_step_wall) < _STEP_WAIT_SEC
        ):
            return "waiting"

        if self._ladder_step_idx >= len(self._steps):
            if not self._ladder_terminal_alerted:
                self._ladder_terminal_alerted = True
                tried = ", ".join(name for name, _fn in self._steps) or "none"
                message = (
                    "🚨 CRITICAL: tick-liveness auto-heal EXHAUSTED — tried "
                    f"[{tried}] and live bars are STILL not flowing. "
                    "Manual OpenAlgo restart required (remaining causes are "
                    "main-process-level and cannot be healed in-process). "
                    "Issue #376."
                )
                logger.error(f"tick_liveness: {message}")
                self._safe_notify(message)
                return "exhausted_alerted"
            return "exhausted"

        name, fn = self._steps[self._ladder_step_idx]
        step_no = self._ladder_step_idx + 1
        self._ladder_step_idx += 1
        self._ladder_last_step_wall = now_wall
        self._last_heal_step_name = name
        try:
            dispatched = bool(fn())
            outcome = "dispatched" if dispatched else "failed"
        except Exception:
            logger.exception("tick_liveness: auto-heal step %d (%s) raised", step_no, name)
            outcome = "raised"
        message = (
            f"🔧 Tick-liveness auto-heal step {step_no}/{len(self._steps)} "
            f"({name}): {outcome}. Watching ~{int(_STEP_WAIT_SEC)}s for bars "
            "to resume before escalating."
        )
        logger.warning(f"tick_liveness: {message}")
        self._safe_notify(message)
        return f"step_{name}_{outcome}"

    # -- daemon loop -----------------------------------------------------------

    def _loop(self) -> None:
        from services.thread_registry import beat as _beat

        while not self._stop.is_set():
            _beat("TickLivenessWatchdog")
            try:
                self.check()
            except Exception:
                logger.exception("tick_liveness watchdog check failed")
            try:
                now_wall = time.time()
                if now_wall - self._last_instrumentation_wall >= _INSTRUMENTATION_INTERVAL_SEC:
                    self._last_instrumentation_wall = now_wall
                    log_resource_snapshot()
            except Exception:
                logger.debug("tick_liveness instrumentation tick failed", exc_info=True)
            self._stop.wait(_POLL_SEC)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="TickLivenessWatchdog")
        self._thread.start()
        logger.info(
            "tick_liveness watchdog started (enabled=%s, autoheal=%s, threshold=%dm, "
            "realert=%dm, ladder_cooldown=%dm, poll=%ds)",
            watchdog_enabled(),
            autoheal_enabled(),
            max_silent_min(),
            realert_min(),
            ladder_cooldown_min(),
            int(_POLL_SEC),
        )

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
# Module singleton + wiring
# --------------------------------------------------------------------------- #

_watchdog: TickLivenessWatchdog | None = None


def get_watchdog() -> TickLivenessWatchdog | None:
    return _watchdog


def init_tick_liveness_watchdog(app=None) -> TickLivenessWatchdog | None:
    """Build and start the process-wide watchdog. Idempotent; never raises.

    Started even when the flag is off (like the dry tripwire) so a runtime
    flag flip takes effect without re-init — the flag is consulted per-check.
    """
    global _watchdog
    try:
        if _watchdog is not None:
            return _watchdog
        _watchdog = TickLivenessWatchdog()
        _watchdog.start()
        if app is not None:
            app.tick_liveness_watchdog = _watchdog
        return _watchdog
    except Exception:
        logger.exception("init_tick_liveness_watchdog failed")
        return None
