"""Declarative inventory of every long-lived thread in the OpenAlgo worker.

Why this exists
---------------

Scheduling in this install is only half APScheduler. The other half is ~18
long-lived daemon threads, several of which are cron jobs in everything but
name — ``ScannerBackfillPeriodic`` (30 min), ``ScannerStragglerRecheck``
(15 min), ``TickLivenessWatchdog`` (30 s) and friends. None of them appeared
anywhere in the UI, and none of them was independently verified to be alive.

``services/thread_watchdog_service.py`` reads ``health_metrics.thread_count``
and alerts when the count climbs — it detects a *leak*. It has no notion of
which threads are *expected*, so it cannot detect the opposite failure: a
thread that died, or (worse) one that is still alive but wedged on a socket
read. ``Thread.is_alive()`` stays ``True`` for a wedged thread forever, which
is exactly the shape of the 2026-07-07 silent tick-flow death.

This module closes that gap with two pieces:

* a **catalog** (:data:`CATALOG`) naming every long-lived thread, its class,
  its cadence and the env flag that governs it; and
* an in-memory **heartbeat** (:func:`beat`) that the recurring loops stamp at
  the top of each tick, so "alive" can be distinguished from "working".

Alerting policy — deliberately narrow
-------------------------------------

Phase 1 alerts **only on threads that beat at least once and then stopped**
(vanished, or went silent past ``THREAD_HEARTBEAT_STALE_MULTIPLIER`` times
their declared cadence). A thread that has never beaten is reported in the
snapshot as ``not_started`` but never alerts, because "not started" is
routinely legitimate: no broker session yet, outside the strategy's window,
bot not configured, flag off. Alerting on those would produce daily noise on
a single-broker install and train the operator to ignore the channel.

Design rules
------------

* :func:`beat` is called on every tick of a 5-second loop. It takes one lock,
  writes two floats, and can never raise.
* This module is **read-only on every other module** and holds no DB handle.
  The only side effect is the alert publish in :func:`check_and_alert`.
* Under eventlet, some threads are deliberately created from the *unpatched*
  ``threading`` module to escape the green-thread scheduler. Enumeration
  therefore walks both modules — see :func:`_live_thread_names`.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

from utils.logging import get_logger

logger = get_logger(__name__)

# Thread classes. The UI groups by these and applies a different policy to each.
GROUP_LOOP = "loop"  # recurring work on a cadence — scheduler-like
GROUP_TRANSPORT = "transport"  # feed / socket / queue pumps — never disableable
GROUP_POLLER = "poller"  # outbound bot pollers, one per token
GROUP_BOOT = "boot"  # one-shot boot workers: run once, then exit

# Control tiers, mirroring services/scheduler_registry.py. Display-only in this
# phase: no toggle is rendered anywhere yet.
TIER_PROTECTED = "protected"
TIER_GUARDED = "guarded"
TIER_FREE = "free"

# Snapshot states.
STATE_RUNNING = "running"
STATE_STALE = "stale"  # alive, but the heartbeat is older than the deadline
STATE_DEAD = "dead"  # beat at least once, then vanished
STATE_NOT_STARTED = "not_started"  # never seen — legitimate in many cases
STATE_COMPLETED = "completed"  # boot one-shot that ran and exited

_DEFAULT_STALE_MULTIPLIER = 3.0
_DEFAULT_DEDUP_MIN = 30


@dataclass(frozen=True)
class ThreadSpec:
    """One expected long-lived thread.

    Attributes:
        thread_name: The exact ``threading.Thread(name=...)`` value. This is the
            join key against a live process, so it must match the source
            literal — ``test_thread_registry`` asserts that it still does.
        label: Human-readable name for the UI.
        group: One of the ``GROUP_*`` constants.
        owner: ``module.py:line``-style pointer to where the thread is created.
        cadence_sec: Declared tick interval for ``loop`` threads; ``None`` for
            every other group (they are event-driven, not periodic).
        window: Human text for a loop that only runs inside an IST window.
        env_flag: The env var that governs whether the thread starts at all.
        tier: Control tier — recorded now, enforced in Phase 2.
        description: One line explaining what the thread does.
    """

    thread_name: str
    label: str
    group: str
    owner: str
    description: str
    cadence_sec: float | None = None
    window: str | None = None
    env_flag: str | None = None
    tier: str = TIER_FREE
    tags: tuple[str, ...] = field(default=())


CATALOG: tuple[ThreadSpec, ...] = (
    # ---- Recurring work loops (scheduler-like) ---------------------------
    ThreadSpec(
        thread_name="ScannerBackfillPeriodic",
        label="Scanner backfill convergence",
        group=GROUP_LOOP,
        owner="services/scanner_backfill_scheduler.py",
        description=(
            "Re-checks the SCANNER_SYMBOLS universe for stale 1m/D bars and "
            "fetches only the tail that is behind today's close."
        ),
        cadence_sec=30 * 60,
        window="15:30-17:00 IST, trading days",
        env_flag="SCANNER_BACKFILL_PERIODIC_CHECK_ENABLED",
        tier=TIER_GUARDED,
    ),
    ThreadSpec(
        thread_name="OptionLiquidityConvergence",
        label="Option-liquidity missed-sweep catch-up",
        group=GROUP_LOOP,
        owner="services/option_liquidity_service.py",
        description=(
            "After the 15:45 sweep time on a trading day, re-runs the "
            "option-liquidity sweep if today has no score rows (issue #589) — "
            "an evening boot after an outage recovers the day."
        ),
        cadence_sec=20 * 60,
        window="15:55-24:00 IST, trading days",
        env_flag="OPTION_LIQUIDITY_CONVERGENCE_ENABLED",
        tier=TIER_FREE,
    ),
    ThreadSpec(
        thread_name="ScannerStragglerRecheck",
        label="Scanner straggler re-check",
        group=GROUP_LOOP,
        owner="services/scanner_backfill_scheduler.py",
        description=(
            "Mid-session catch-up for symbols the 09:16 pre-entry refresh left "
            "stale; releases/narrows the scanner smoke post-hold (issue #390)."
        ),
        cadence_sec=15 * 60,
        window="09:20-15:30 IST, trading days",
        env_flag="SCANNER_STRAGGLER_RECHECK_ENABLED",
        tier=TIER_GUARDED,
    ),
    ThreadSpec(
        thread_name="SectorFollowBackfillPeriodic",
        label="sector_follow feed convergence",
        group=GROUP_LOOP,
        owner="services/sector_follow_backfill_scheduler.py",
        description=(
            "Keeps the sector_follow index + locked-static-30 stock 1m feeds fresh without a cron."
        ),
        cadence_sec=30 * 60,
        window="15:30-17:00 IST, trading days",
        env_flag="SECTOR_FOLLOW_PERIODIC_CHECK_ENABLED",
        tier=TIER_GUARDED,
    ),
    ThreadSpec(
        thread_name="TickLivenessWatchdog",
        label="Tick-liveness watchdog",
        group=GROUP_LOOP,
        owner="services/tick_liveness_watchdog.py",
        description=(
            "Alerts and auto-heals when no live scanner bar closes during "
            "market hours (the libzmq 10055 silent-death class, issue #376)."
        ),
        cadence_sec=30,
        env_flag="TICK_LIVENESS_WATCHDOG_ENABLED",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="WSProxySupervisor",
        label="WS-proxy supervisor",
        group=GROUP_LOOP,
        owner="services/ws_proxy_supervisor.py",
        description="Restarts the websocket_proxy subprocess on unexpected exit.",
        cadence_sec=30,
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="BrokerAutoLoginWatcher",
        label="Broker auto-login watcher",
        group=GROUP_LOOP,
        owner="services/broker_auto_login_watcher.py",
        description=(
            "Detects a dead primary/child broker session (N confirmed dead probes) "
            "and re-logs-in headlessly — catches the daily-reset flush and "
            "mid-session single-session invalidation (issue #654)."
        ),
        cadence_sec=5 * 60,
        window="06:15-23:30 IST, trading days",
        env_flag="BROKER_AUTO_LOGIN_ENABLED",
        tier=TIER_GUARDED,
    ),
    ThreadSpec(
        thread_name="ScannerWsWatchdog",
        label="Scanner WS watchdog",
        group=GROUP_LOOP,
        owner="services/scanner_ws_watchdog.py",
        description="Watches scanner WS staleness against soft/hard thresholds.",
        cadence_sec=60,
        env_flag="SCANNER_WS_WATCHDOG_INTERVAL_SEC",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="ThreadWatchdog",
        label="Thread-count watchdog",
        group=GROUP_LOOP,
        owner="services/thread_watchdog_service.py",
        description=(
            "Alerts when the process thread count crosses the leak thresholds; "
            "also drives this registry's own staleness check."
        ),
        cadence_sec=30,
        env_flag="THREAD_WATCHDOG_ENABLED",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="HealthCollector",
        label="Health metric collector",
        group=GROUP_LOOP,
        owner="utils/health_monitor.py",
        description="Samples FD count, RSS, DB/WS connections and thread count.",
        cadence_sec=10,
        env_flag="HEALTH_MONITOR_ENABLED",
        tier=TIER_GUARDED,
    ),
    ThreadSpec(
        thread_name="MarketDataHealthCheck",
        label="Market-data health check",
        group=GROUP_LOOP,
        owner="services/market_data_service.py",
        description="Watches inbound market-data flow for stalls.",
        cadence_sec=5,
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="MarketDataCleanup",
        label="Market-data cleanup",
        group=GROUP_LOOP,
        owner="services/market_data_service.py",
        description="Evicts stale entries from the in-memory market-data cache.",
        cadence_sec=300,
        tier=TIER_FREE,
    ),
    ThreadSpec(
        thread_name="FlowPriceMonitor",
        label="Flow price monitor",
        group=GROUP_LOOP,
        owner="services/flow_price_monitor_service.py",
        description="Polls live prices for Flow price-trigger alerts.",
        cadence_sec=5,
        tier=TIER_GUARDED,
    ),
    # ---- Feed / transport (never disableable) ----------------------------
    ThreadSpec(
        thread_name="ZerodhaWS",
        label="Zerodha WebSocket reader",
        group=GROUP_TRANSPORT,
        owner="broker/zerodha/streaming/zerodha_websocket.py",
        description="The broker tick feed. Everything downstream depends on it.",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="ZerodhaWSSubscriptions",
        label="Zerodha subscription pump",
        group=GROUP_TRANSPORT,
        owner="broker/zerodha/streaming/zerodha_websocket.py",
        description="Drains pending symbol subscriptions in batches.",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="ZerodhaWSHealthCheck",
        label="Zerodha WS health check",
        group=GROUP_TRANSPORT,
        owner="broker/zerodha/streaming/zerodha_websocket.py",
        description="Adapter-level reconnect check for the broker socket.",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="WebSocketProxyServer",
        label="WebSocket proxy server",
        group=GROUP_TRANSPORT,
        owner="websocket_proxy/app_integration.py",
        description="Runs the unified WS proxy (port 8765) event loop.",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="WebSocketClientLoop",
        label="WS client event loop",
        group=GROUP_TRANSPORT,
        owner="services/websocket_client.py",
        description="asyncio loop backing the in-process WS client.",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="ScannerZMQSubscriber",
        label="Scanner ZMQ subscriber",
        group=GROUP_TRANSPORT,
        owner="services/scanner_service.py",
        description="Consumes normalized ticks off the ZMQ bus for the scanner.",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="open15-zmq",
        label="open15 tick subscriber",
        group=GROUP_TRANSPORT,
        owner="services/open15_breakout_service.py",
        description=(
            "Additive ZMQ SUB for the open15 strategy. Alive only inside "
            "09:14:50 -> exit+5s IST, so 'not started' is normal all day."
        ),
        window="09:14:50 - exit+5s IST",
        env_flag="OPEN15_ENABLED",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="SimplifiedTickLogWriter",
        label="Tick-log writer",
        group=GROUP_TRANSPORT,
        owner="services/simplified_stock_engine_ticklog.py",
        description="Async queue drain writing engine tick logs to disk.",
        tier=TIER_FREE,
    ),
    # ---- Bot pollers (one per token) -------------------------------------
    ThreadSpec(
        thread_name="TelegramBotThread",
        label="Telegram bot (outbound/UI)",
        group=GROUP_POLLER,
        owner="services/telegram_bot_service.py",
        description=(
            "The UI-toggled interactive bot. Owns the token when active; the "
            "inbound poller refuses to start while it does (issue #238)."
        ),
        tier=TIER_FREE,
    ),
    ThreadSpec(
        thread_name="TelegramInboundThread",
        label="Telegram inbound poller",
        group=GROUP_POLLER,
        owner="services/telegram_inbound_service.py",
        description="getUpdates poller; starts only when the UI bot is down.",
        env_flag="TELEGRAM_INBOUND_ENABLED",
        tier=TIER_FREE,
    ),
    ThreadSpec(
        thread_name="WhatsAppBotThread",
        label="WhatsApp bot",
        group=GROUP_POLLER,
        owner="services/whatsapp_bot_service.py",
        description="WhatsApp bot loop.",
        tier=TIER_FREE,
    ),
    # ---- Boot one-shots (run once, then exit) ----------------------------
    ThreadSpec(
        thread_name="ScannerBackfillBoot",
        label="Scanner backfill (boot)",
        group=GROUP_BOOT,
        owner="services/scanner_backfill_scheduler.py",
        description="Waits for a broker session, then converges 1m + D once.",
        env_flag="SCANNER_BACKFILL_ENABLED",
        tier=TIER_GUARDED,
    ),
    ThreadSpec(
        thread_name="SectorFollowBackfillBoot",
        label="sector_follow backfill (boot)",
        group=GROUP_BOOT,
        owner="services/sector_follow_backfill_scheduler.py",
        description="Boot-time index + stock feed convergence.",
        tier=TIER_GUARDED,
    ),
    ThreadSpec(
        thread_name="BrokerAutoLoginBoot",
        label="Broker auto-login (boot)",
        group=GROUP_BOOT,
        owner="services/broker_auto_login_watcher.py",
        description="If the broker session is dead at boot, auto-login once, then start the watcher.",
        env_flag="BROKER_AUTO_LOGIN_ENABLED",
        tier=TIER_GUARDED,
    ),
    ThreadSpec(
        thread_name="ScannerAggregatorSeed",
        label="Scanner aggregator seed",
        group=GROUP_BOOT,
        owner="services/scanner_aggregator_seeder.py",
        description="Warms the in-process bar aggregators from stored/broker bars.",
        tier=TIER_GUARDED,
    ),
    ThreadSpec(
        thread_name="ScannerPreSubscribe",
        label="Scanner pre-subscribe",
        group=GROUP_BOOT,
        owner="services/scanner_presubscribe.py",
        description="Subscribes the scanner universe to the WS feed at boot.",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="RegimeSectorPreSubscribe",
        label="Regime sector pre-subscribe",
        group=GROUP_BOOT,
        owner="services/scanner_presubscribe.py",
        description="Subscribes the regime/sector index set at boot.",
        tier=TIER_GUARDED,
    ),
    ThreadSpec(
        thread_name="ScannerHistoryWarmup",
        label="Scanner history warmup",
        group=GROUP_BOOT,
        owner="app.py",
        description="Primes the scanner history provider cache at boot.",
        tier=TIER_GUARDED,
    ),
    ThreadSpec(
        thread_name="AbandonedExitRecovery",
        label="Abandoned-exit recovery",
        group=GROUP_BOOT,
        owner="services/abandoned_exit_recovery_service.py",
        description="Recovers exits stranded by a crash mid-flatten.",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="futures_follow_rehydrate",
        label="futures_follow rehydrate",
        group=GROUP_BOOT,
        owner="services/futures_follow_service.py",
        description=(
            "Rebuilds open-lot state from the position book so T+1 exits know "
            "what to close (the issue #497 read path)."
        ),
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="IntradayPullbackBootResume",
        label="intraday_pullback resume",
        group=GROUP_BOOT,
        owner="services/intraday_pullback_service.py",
        description="Restores intraday_pullback state after a restart.",
        tier=TIER_PROTECTED,
    ),
    ThreadSpec(
        thread_name="WhatsAppAutoStart",
        label="WhatsApp auto-start",
        group=GROUP_BOOT,
        owner="app.py",
        description="Starts the WhatsApp bot at boot when configured.",
        tier=TIER_FREE,
    ),
)

_BY_NAME: dict[str, ThreadSpec] = {spec.thread_name: spec for spec in CATALOG}

# thread_name -> (monotonic_at, wall_clock_at, count). Guarded by _lock.
_beats: dict[str, tuple[float, float, int]] = {}
# thread_name -> monotonic timestamp of the last alert published for it.
_last_alert: dict[str, float] = {}
_lock = threading.Lock()


def _env_true(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes")


def _stale_multiplier() -> float:
    """How many missed ticks before a heartbeat counts as stale."""
    try:
        value = float(os.environ.get("THREAD_HEARTBEAT_STALE_MULTIPLIER", ""))
    except ValueError:
        return _DEFAULT_STALE_MULTIPLIER
    return value if value > 1 else _DEFAULT_STALE_MULTIPLIER


def _dedup_window_sec() -> float:
    try:
        return float(os.environ.get("THREAD_REGISTRY_ALERT_DEDUP_MIN", "")) * 60.0
    except ValueError:
        return _DEFAULT_DEDUP_MIN * 60.0


def beat(thread_name: str) -> None:
    """Record that ``thread_name`` completed a tick.

    Called at the top of every loop iteration, including 5-second loops, so it
    must stay trivial: one lock, two floats. It never raises — a bookkeeping
    failure must not be able to kill the loop it is measuring.
    """
    try:
        now = time.monotonic()
        with _lock:
            previous = _beats.get(thread_name)
            count = (previous[2] + 1) if previous else 1
            _beats[thread_name] = (now, time.time(), count)
    except Exception:  # pragma: no cover - defensive
        logger.exception("thread_registry: beat(%r) failed", thread_name)


def _live_thread_names() -> set[str]:
    """Names of all currently-alive threads.

    Walks the patched ``threading`` module **and**, under eventlet, the
    original unpatched one. Several services deliberately create their threads
    from ``eventlet.patcher.original("threading")`` to escape the green-thread
    scheduler; those do not appear in the patched module's enumerate().
    """
    names: set[str] = set()
    modules = [threading]
    try:
        import eventlet.patcher

        original = eventlet.patcher.original("threading")
        if original is not threading:
            modules.append(original)
    except Exception:
        # No eventlet (Windows/dev server) — the patched module is the only one.
        pass

    for module in modules:
        try:
            for thread in module.enumerate():
                name = getattr(thread, "name", None)
                if name and thread.is_alive():
                    names.add(name)
        except Exception:
            logger.exception("thread_registry: enumerate failed for %r", module)
    return names


def _classify(spec: ThreadSpec, alive: bool, beat_info, now: float) -> tuple[str, float | None]:
    """Return ``(state, heartbeat_age_sec)`` for one catalog entry."""
    age = (now - beat_info[0]) if beat_info else None

    if spec.group == GROUP_BOOT:
        # A one-shot that is gone but was seen is a success, not a death.
        if alive:
            return STATE_RUNNING, age
        return (STATE_COMPLETED if beat_info else STATE_NOT_STARTED), age

    if not alive:
        return (STATE_DEAD if beat_info else STATE_NOT_STARTED), age

    if spec.group == GROUP_LOOP and spec.cadence_sec:
        if beat_info is None:
            # Alive but never beat: either just started, or it never reaches the
            # beat call. Reported as not_started; never alerted on.
            return STATE_NOT_STARTED, None
        if age is not None and age > spec.cadence_sec * _stale_multiplier():
            return STATE_STALE, age

    return STATE_RUNNING, age


def snapshot() -> list[dict]:
    """Join the catalog against the live process.

    Returns:
        One dict per catalog entry, ordered by group then label, carrying the
        spec fields plus ``alive``, ``state``, ``heartbeat_age_sec``,
        ``beat_count`` and ``env_flag_value``.
    """
    live = _live_thread_names()
    now = time.monotonic()
    with _lock:
        beats = dict(_beats)

    rows: list[dict] = []
    for spec in CATALOG:
        alive = spec.thread_name in live
        info = beats.get(spec.thread_name)
        state, age = _classify(spec, alive, info, now)
        rows.append(
            {
                "thread_name": spec.thread_name,
                "label": spec.label,
                "group": spec.group,
                "owner": spec.owner,
                "description": spec.description,
                "cadence_sec": spec.cadence_sec,
                "window": spec.window,
                "env_flag": spec.env_flag,
                "env_flag_value": (os.environ.get(spec.env_flag) if spec.env_flag else None),
                "tier": spec.tier,
                "alive": alive,
                "state": state,
                "heartbeat_age_sec": round(age, 1) if age is not None else None,
                "last_beat_at": info[1] if info else None,
                "beat_count": info[2] if info else 0,
            }
        )

    # Anything alive that the catalog does not know about. Surfaced rather than
    # hidden so a newly-added thread shows up as a gap to be catalogued.
    known = set(_BY_NAME)
    for name in sorted(live - known):
        rows.append(
            {
                "thread_name": name,
                "label": name,
                "group": "unregistered",
                "owner": None,
                "description": "Alive but not in the catalog.",
                "cadence_sec": None,
                "window": None,
                "env_flag": None,
                "env_flag_value": None,
                "tier": TIER_FREE,
                "alive": True,
                "state": STATE_RUNNING,
                "heartbeat_age_sec": None,
                "last_beat_at": None,
                "beat_count": 0,
            }
        )

    order = {GROUP_LOOP: 0, GROUP_TRANSPORT: 1, GROUP_POLLER: 2, GROUP_BOOT: 3}
    rows.sort(key=lambda r: (order.get(r["group"], 9), r["label"]))
    return rows


def summarize(rows: list[dict] | None = None) -> dict:
    """Counts for the page header."""
    rows = snapshot() if rows is None else rows
    catalogued = [r for r in rows if r["group"] != "unregistered"]
    return {
        "expected": len(catalogued),
        "alive": sum(1 for r in catalogued if r["alive"]),
        "stale": sum(1 for r in catalogued if r["state"] == STATE_STALE),
        "dead": sum(1 for r in catalogued if r["state"] == STATE_DEAD),
        "not_started": sum(1 for r in catalogued if r["state"] == STATE_NOT_STARTED),
        "unregistered": sum(1 for r in rows if r["group"] == "unregistered"),
    }


def evaluate_alerts(rows: list[dict] | None = None) -> list[dict]:
    """Return the rows that warrant an operator alert.

    Only ``stale`` and ``dead`` qualify, and both require ``beat_count > 0`` —
    a thread must have proved it can run before its silence means anything.
    """
    rows = snapshot() if rows is None else rows
    return [
        r for r in rows if r["state"] in (STATE_STALE, STATE_DEAD) and r.get("beat_count", 0) > 0
    ]


def check_and_alert(now: float | None = None) -> list[dict]:
    """Evaluate the registry and publish an alert per newly-degraded thread.

    Deduped per thread on a ``THREAD_REGISTRY_ALERT_DEDUP_MIN`` window so a
    thread that stays wedged reminds rather than storms. Never raises.

    Returns:
        The rows that were alerted on (empty when healthy or disabled).
    """
    if not _env_true("THREAD_REGISTRY_ENABLED"):
        return []
    try:
        degraded = evaluate_alerts()
        if not degraded:
            return []

        moment = time.monotonic() if now is None else now
        window = _dedup_window_sec()
        fired: list[dict] = []
        for row in degraded:
            name = row["thread_name"]
            with _lock:
                last = _last_alert.get(name)
                if last is not None and (moment - last) < window:
                    continue
                _last_alert[name] = moment
            fired.append(row)

        for row in fired:
            age = row["heartbeat_age_sec"]
            detail = f"no heartbeat for {age}s" if age is not None else "thread gone"
            message = (
                f"Thread {row['label']} ({row['thread_name']}) is {row['state'].upper()}"
                f" - {detail}. Owner: {row['owner']}."
            )
            logger.warning("thread_registry: %s", message)
            try:
                from services.notification_service import get_notification_service

                get_notification_service().notify(
                    "thread_registry",
                    message,
                    thread_name=row["thread_name"],
                    state=row["state"],
                    heartbeat_age_sec=age,
                )
            except Exception:
                logger.exception("thread_registry: alert publish failed")
        return fired
    except Exception:
        logger.exception("thread_registry: check_and_alert failed")
        return []


def reset_for_tests() -> None:
    """Clear heartbeat and dedup state so tests start clean."""
    with _lock:
        _beats.clear()
        _last_alert.clear()
