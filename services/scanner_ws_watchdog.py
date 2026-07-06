"""Scanner WebSocket liveness watchdog — recover a stalled tick stream.

The event-driven pre-subscribe (see :mod:`services.scanner_presubscribe`) gets
the scanner's symbols subscribed whenever the broker WS connects. But a
connection can stay *open* while the feed goes silent — the broker adapter
wedges, the proxy stops forwarding, or a half-open TCP socket never raises
``ConnectionClosed`` so the client's own reconnect loop never fires. The
scanner then sees zero ticks with no error anywhere.

This watchdog watches the *liveness* of the tick stream rather than the socket
state. During NSE market hours it samples the time of the last market-data
tick and escalates:

* **soft recovery** (tick age > ``soft_threshold``, default 90s): close the
  underlying websocket so the client's existing reconnect loop re-establishes
  the feed. Records a cooldown so a single stall does not retrigger every tick.
* **hard recovery** (still stale > ``hard_threshold``, default 180s, after the
  cooldown elapsed): tear the WS client down entirely and re-init, forcing a
  fresh auth + the connect-callback re-subscribe.

Tick source / recovery actions are injected so the policy is unit-testable
without a live broker; :func:`start_scanner_ws_watchdog` wires the production
glue (reads the scanner client's market-data callback, drives ``ws.close()`` /
``close_all_clients``).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))


def _env_float(name: str, default: float) -> float:
    """Read a float-valued env var with a fallback default."""
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool_true(name: str) -> bool:
    """True iff env var is set to a truthy value (default True)."""
    return os.environ.get(name, "true").strip().lower() in ("true", "1", "yes", "on")


def _default_market_open(epoch: float) -> bool:
    """True during NSE cash hours (Mon-Fri, 09:15-15:30 IST)."""
    dt = datetime.fromtimestamp(epoch, _IST)
    if dt.weekday() >= 5:  # Saturday/Sunday
        return False
    minutes = dt.hour * 60 + dt.minute
    return (9 * 60 + 15) <= minutes <= (15 * 60 + 30)


class ScannerWsWatchdog:
    """Detect a stale scanner tick stream and trigger soft/hard WS recovery."""

    def __init__(
        self,
        *,
        tick_source: Callable[[], float | None],
        recover_soft: Callable[[], None],
        recover_hard: Callable[[], None],
        now: Callable[[], float] = time.time,
        market_open: Callable[[float], bool] = _default_market_open,
        # Issue #158 D1: defaults bumped from 90/180/60/30s. The old 90s
        # soft floor fired ~121× per trading day on the live feed — a
        # normal mid-session pause between ticks (sparse symbol, end-of-bar
        # quiet) crossed it. The new 180/360/120/60 defaults trigger only
        # on genuinely stuck feeds while still recovering within a single
        # 5m bar. All four are env-overridable for per-deploy tuning.
        soft_threshold: float | None = None,
        hard_threshold: float | None = None,
        cooldown: float | None = None,
        interval: float | None = None,
    ):
        if soft_threshold is None:
            soft_threshold = _env_float("SCANNER_WS_WATCHDOG_SOFT_THRESHOLD_SEC", 180.0)
        if hard_threshold is None:
            hard_threshold = _env_float("SCANNER_WS_WATCHDOG_HARD_THRESHOLD_SEC", 360.0)
        if cooldown is None:
            cooldown = _env_float("SCANNER_WS_WATCHDOG_COOLDOWN_SEC", 120.0)
        if interval is None:
            interval = _env_float("SCANNER_WS_WATCHDOG_INTERVAL_SEC", 60.0)
        self._tick_source = tick_source
        self._recover_soft = recover_soft
        self._recover_hard = recover_hard
        self._now = now
        self._market_open = market_open
        self.soft_threshold = soft_threshold
        self.hard_threshold = hard_threshold
        self.cooldown = cooldown
        self.interval = interval
        self._cooldown_start: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> str:
        """Run one liveness check; returns the action taken (for tests/introspection)."""
        t = self._now()
        if not self._market_open(t):
            self._cooldown_start = None
            return "closed"
        last = self._tick_source()
        if last is None:
            return "no_ticks"  # no baseline yet — cannot judge staleness
        age = t - last
        if age <= self.soft_threshold:
            self._cooldown_start = None
            return "fresh"
        # Stale. Hold off if a soft recovery is still inside its cooldown window.
        if self._cooldown_start is not None and (t - self._cooldown_start) < self.cooldown:
            return "cooldown"
        # Cooldown elapsed (or first detection). Escalate to hard if the soft
        # recovery already had its chance and the feed is still hard-stale.
        if self._cooldown_start is not None and age > self.hard_threshold:
            logger.error(
                "Scanner WS hard-stale: %.0fs since last tick — hard recovery (re-init client)",
                age,
            )
            self._recover_hard()
            self._cooldown_start = None
            return "hard"
        logger.warning(
            "Scanner WS stale: %.0fs since last tick — soft recovery (ws.close → reconnect)",
            age,
        )
        self._recover_soft()
        self._cooldown_start = t
        return "soft"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:
                logger.exception("Scanner WS watchdog check failed")
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ScannerWsWatchdog")
        self._thread.start()
        logger.info(
            "Scanner WS watchdog started (soft=%.0fs hard=%.0fs cooldown=%.0fs interval=%.0fs)",
            self.soft_threshold,
            self.hard_threshold,
            self.cooldown,
            self.interval,
        )

    def stop(self) -> None:
        self._stop.set()


def start_scanner_ws_watchdog(username: str, *, app=None) -> ScannerWsWatchdog:
    """Build and start a watchdog wired to the scanner's broker WS client."""
    last_tick: list[float | None] = [None]
    state = {"client_id": None}

    def _ensure_tick_callback():
        from services.websocket_service import get_websocket_connection

        ok, client, _ = get_websocket_connection(username)
        if not ok or client is None:
            return None
        if state["client_id"] != id(client):
            # Fresh client instance — register a once-per-tick stamp recorder.
            client.register_callback(
                "market_data", lambda _d: last_tick.__setitem__(0, time.time())
            )
            state["client_id"] = id(client)
        return client

    def _tick_source() -> float | None:
        _ensure_tick_callback()
        return last_tick[0]

    def _recover_soft() -> None:
        import asyncio

        from services.websocket_service import get_websocket_connection

        ok, client, _ = get_websocket_connection(username)
        if ok and client is not None and client.loop and client.ws:
            # Close the socket WITHOUT clearing client.running so its own
            # reconnect loop re-establishes the feed.
            asyncio.run_coroutine_threadsafe(client.ws.close(), client.loop)

    def _recover_hard() -> None:
        from services.websocket_client import close_all_clients
        from services.websocket_service import get_websocket_connection

        close_all_clients()
        last_tick[0] = None
        state["client_id"] = None
        get_websocket_connection(username)  # re-establish + fire connect callbacks

    wd = ScannerWsWatchdog(
        tick_source=_tick_source, recover_soft=_recover_soft, recover_hard=_recover_hard
    )
    wd.start()
    if app is not None:
        app.scanner_ws_watchdog = wd
    return wd
