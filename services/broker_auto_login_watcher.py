"""Continuous broker-session watcher — the PRIMARY auto-login trigger (issue #654).

Kite tokens do not expire on a fixed schedule (see the issue / CLAUDE.md): a
token is flushed at the ~06:30-07:30 IST daily reset (which can land *after* a
morning boot), and it can be invalidated mid-session at any time because Kite
enforces one active session per user (a separate Kite web/app login, or a
``logout``). So expiry is an **event to detect, not a time to schedule** — this
all-day daemon loop is the mechanism that catches both.

Each tick (default every ``AUTO_LOGIN_WATCH_INTERVAL_MIN`` = 5 min, on trading
days inside the watch window):

1. **Probe** the primary (``is_live_broker_session``) and each enabled child.
   Both probes are BROKER-VERIFIED (issue #658): the child probe layers a real
   ``broker_session_health.probe_token`` call on top of the cheap
   ``broker_accounts_service._is_connected`` date pre-filter, so a token Kite
   killed mid-session reads dead — a date-only check cannot see that.
2. **Confirm** — a target must read dead ``AUTO_LOGIN_DEAD_CONFIRM_COUNT`` (2)
   ticks in a row before we act, so a transient network blip never triggers a
   needless re-login.
3. **Re-login** via ``broker_auto_login_service`` (primary / that child) — for
   children with the per-child ``auto_login_enabled`` UI toggle ON. A child
   WITHOUT it is still probed: a confirmed-dead session that was alive earlier
   today Telegram-alerts once per day ("manual login needed") instead of
   healing, so a mid-session kill on a manual-login child is no longer silent
   until a mirror order fails (issue #658). A success heals the live feed
   automatically: the ``upsert_auth`` inside publishes
   the ZMQ ``CACHE_INVALIDATE`` (WS-proxy reconnect) and
   ``notify_broker_session_refreshed`` drives ``ws_recovery_service`` to backfill
   the bars missed while the token was dead — no restart.
4. **Guardrails** — a per-target daily attempt cap
   (``AUTO_LOGIN_MAX_ATTEMPTS_PER_DAY`` = 5) with exponential backoff between
   attempts, so a wrong password / changed Kite endpoint / ToS single-session
   conflict can never hammer Kite. A capped-out target alerts and waits for the
   next day; the manual flow is always the fallback.

Gated by ``BROKER_AUTO_LOGIN_ENABLED`` (default OFF). The pure evaluation
(:func:`evaluate_once`) takes the clock, probes and login callables as arguments
so it is fully testable without threads, sockets or the DB.
"""

from __future__ import annotations

import os
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

PRIMARY = "primary"

# Defaults (env-overridable).
_DEFAULT_INTERVAL_MIN = 5
_DEFAULT_DEAD_CONFIRM = 2
_DEFAULT_MAX_ATTEMPTS = 5
# Watch window: broad by design. The daily reset is ~07:00 and mid-session death
# is any time during the trading day; there is no point re-logging in overnight
# right before the next reset. Env-tunable.
_DEFAULT_START = "06:15"
_DEFAULT_END = "23:30"
# Backoff base between attempts on one target within a day (seconds), doubled per
# prior attempt and capped, so a persistently-failing target is retried rarely.
_BACKOFF_BASE_SEC = 300
_BACKOFF_MAX_SEC = 3600

_BOOT_WAIT_POLL_SEC = 15

_stop_event = threading.Event()
_watch_thread: threading.Thread | None = None


# --------------------------------------------------------------------------- #
# Env knobs
# --------------------------------------------------------------------------- #
def watcher_enabled() -> bool:
    """Master auto-login flag — DB-backed (UI toggle), env is only the seed.

    Read fresh every tick so a UI flip applies within one interval, no restart.
    Falls back to the env var if the settings module is unavailable.
    """
    try:
        from database.broker_auto_login_settings_db import get_enabled

        return get_enabled()
    except Exception:
        logger.exception("watcher_enabled: settings read failed — falling back to env")
        return os.getenv("BROKER_AUTO_LOGIN_ENABLED", "false").lower() == "true"


def _interval_seconds() -> int:
    try:
        return max(
            60, int(os.getenv("AUTO_LOGIN_WATCH_INTERVAL_MIN", str(_DEFAULT_INTERVAL_MIN))) * 60
        )
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_MIN * 60


def _dead_confirm_count() -> int:
    try:
        return max(1, int(os.getenv("AUTO_LOGIN_DEAD_CONFIRM_COUNT", str(_DEFAULT_DEAD_CONFIRM))))
    except (TypeError, ValueError):
        return _DEFAULT_DEAD_CONFIRM


def _max_attempts() -> int:
    try:
        return max(1, int(os.getenv("AUTO_LOGIN_MAX_ATTEMPTS_PER_DAY", str(_DEFAULT_MAX_ATTEMPTS))))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ATTEMPTS


def _parse_time(raw: str, fallback: time) -> time:
    try:
        hh, mm = (int(x) for x in raw.split(":", 1))
        return time(hh, mm)
    except (TypeError, ValueError):
        return fallback


def _window() -> tuple[time, time]:
    start = _parse_time(os.getenv("AUTO_LOGIN_WATCH_START", _DEFAULT_START), time(6, 15))
    end = _parse_time(os.getenv("AUTO_LOGIN_WATCH_END", _DEFAULT_END), time(23, 30))
    return start, end


# --------------------------------------------------------------------------- #
# Per-target state
# --------------------------------------------------------------------------- #
@dataclass
class TargetState:
    """Mutable per-target bookkeeping. ``key`` is 'primary' or 'child:<id>'."""

    dead_streak: int = 0
    attempts_today: int = 0
    attempts_date: date | None = None
    last_attempt_ts: float = 0.0
    # Alert-only targets (child with auto-login OFF, issue #658): only a session
    # that was seen ALIVE today and then died is a mid-session kill worth an
    # alert — a not-yet-logged-in morning is the 09:00/15:00 login reminders'
    # job, and re-alerting it here daily would just train the operator to
    # ignore the channel. In-memory, so a restart between death and alert
    # loses the flag (fail-quiet; mirror-failure alerts remain the backstop).
    was_alive_today: bool = False
    alerted_today: bool = False

    def roll_day(self, today: date) -> None:
        if self.attempts_date != today:
            self.attempts_date = today
            self.attempts_today = 0
            self.last_attempt_ts = 0.0
            self.was_alive_today = False
            self.alerted_today = False


@dataclass
class WatcherState:
    targets: dict[str, TargetState] = field(default_factory=dict)

    def get(self, key: str) -> TargetState:
        return self.targets.setdefault(key, TargetState())


# --------------------------------------------------------------------------- #
# Pure evaluation (no clocks/threads/DB — injectables for tests)
# --------------------------------------------------------------------------- #
def _is_trading_day(d: date) -> bool:
    from services.data_freshness_service import is_trading_day as _shared

    return _shared(d)


def _within_window(now_t: time) -> bool:
    start, end = _window()
    return start <= now_t <= end


def _backoff_ready(target: TargetState, now_ts: float) -> bool:
    """True when enough time has passed since this target's last attempt today."""
    if target.attempts_today == 0:
        return True
    delay = min(_BACKOFF_MAX_SEC, _BACKOFF_BASE_SEC * (2 ** (target.attempts_today - 1)))
    return (now_ts - target.last_attempt_ts) >= delay


def evaluate_once(
    state: WatcherState,
    now: datetime,
    *,
    probe_primary,
    login_primary,
    list_child_targets,
    probe_child,
    login_child,
    dead_confirm: int | None = None,
    max_attempts: int | None = None,
) -> list[dict]:
    """Run one watch evaluation. Returns a list of action results (empty if idle).

    All IO is injected:
      * ``probe_primary() -> bool`` — True when the primary session is live.
      * ``login_primary() -> dict`` — the auto_login_primary result.
      * ``list_child_targets() -> list[(id, name, can_heal)]`` — every enabled
        child; ``can_heal`` says whether automatic re-login may fix it (stored
        password + per-child opt-in). 2-tuples ``(id, name)`` are accepted for
        backward compatibility and imply ``can_heal=True``.
      * ``probe_child(id) -> bool`` — True when that child's broker session is
        live (broker-verified, issue #658).
      * ``login_child(id, name) -> dict`` — the child auto-login result.

    A confirmed-dead target with ``can_heal=False`` never attempts a login: if
    it was alive earlier today (a mid-session kill, not a not-yet-logged-in
    morning) it alerts once per day instead.

    Never raises: a probe/login that throws is treated as a failure for that
    target and logged.
    """
    if not (_is_trading_day(now.date()) and _within_window(now.time())):
        return []

    dead_confirm = dead_confirm if dead_confirm is not None else _dead_confirm_count()
    max_attempts = max_attempts if max_attempts is not None else _max_attempts()
    now_ts = now.timestamp()
    results: list[dict] = []

    # ---- primary --------------------------------------------------------- #
    results.extend(
        _evaluate_target(
            state.get(PRIMARY),
            PRIMARY,
            now.date(),
            now_ts,
            probe=probe_primary,
            login=login_primary,
            dead_confirm=dead_confirm,
            max_attempts=max_attempts,
        )
    )

    # ---- children -------------------------------------------------------- #
    try:
        children = list_child_targets()
    except Exception:
        logger.exception("auto-login watcher: listing child targets failed")
        children = []
    for child in children:
        account_id, name, *rest = child
        can_heal = bool(rest[0]) if rest else True
        key = f"child:{account_id}"
        results.extend(
            _evaluate_target(
                state.get(key),
                key,
                now.date(),
                now_ts,
                probe=lambda aid=account_id: probe_child(aid),
                login=lambda aid=account_id, nm=name: login_child(aid, nm),
                dead_confirm=dead_confirm,
                max_attempts=max_attempts,
                alert_only=not can_heal,
            )
        )
    return results


def _evaluate_target(
    target: TargetState,
    key: str,
    today: date,
    now_ts: float,
    *,
    probe,
    login,
    dead_confirm: int,
    max_attempts: int,
    alert_only: bool = False,
) -> list[dict]:
    """Evaluate one target: probe → confirm → (attempt if under cap + backoff-ready).

    ``alert_only`` (issue #658): never login; a confirmed mid-session death
    (was alive earlier today) alerts once per day instead.
    """
    target.roll_day(today)

    try:
        alive = bool(probe())
    except Exception:
        logger.exception(f"auto-login watcher: probe for {key} raised — treating as dead")
        alive = False

    if alive:
        target.dead_streak = 0
        target.was_alive_today = True
        return []

    target.dead_streak += 1
    if target.dead_streak < dead_confirm:
        logger.info(
            f"auto-login watcher: {key} dead ({target.dead_streak}/{dead_confirm}) — "
            "waiting for confirmation before re-login"
        )
        return []

    if alert_only:
        if target.was_alive_today and not target.alerted_today:
            target.alerted_today = True
            _alert(
                f"auto-login: {key} broker session went DEAD mid-session and auto-login "
                "is OFF for this account — manual login needed (/accounts)"
            )
        return []

    if target.attempts_today >= max_attempts:
        # Capped: alert once at the cap boundary, then stay quiet until tomorrow.
        if target.attempts_today == max_attempts:
            target.attempts_today += 1  # advance past the boundary so we alert once
            _alert(
                f"auto-login: {key} still dead after {max_attempts} attempts today — manual login needed"
            )
        return []

    if not _backoff_ready(target, now_ts):
        return []

    target.attempts_today += 1
    target.last_attempt_ts = now_ts
    logger.warning(
        f"auto-login watcher: {key} confirmed dead — attempt {target.attempts_today}/{max_attempts}"
    )
    try:
        result = login()
    except Exception as e:
        logger.exception(f"auto-login watcher: login for {key} raised: {e}")
        result = {"scope": key, "ok": False, "message": str(e)}

    if result.get("ok"):
        target.dead_streak = 0  # recovered; next tick's live probe confirms
    return [result]


# --------------------------------------------------------------------------- #
# IO adapters (bind evaluate_once to the real system)
# --------------------------------------------------------------------------- #
def _probe_primary() -> bool:
    from services.broker_session_health import is_live_broker_session

    return is_live_broker_session()


def _login_primary() -> dict:
    from services.broker_auto_login_service import auto_login_primary

    return auto_login_primary()


def _list_child_targets() -> list[tuple[int, str, bool]]:
    """Every ENABLED child, with whether AUTOMATIC re-login may heal it (issue #658).

    ``can_heal`` = stored password + per-child ``auto_login_enabled`` (the UI
    toggle; the manual "Auto login" button ignores it — it calls the service
    directly). Children without it are still probed so a mid-session token kill
    is detected and alerted rather than surfacing as a failed mirror order.
    """
    from database import broker_accounts_db as accounts_db

    targets: list[tuple[int, str, bool]] = []
    for acct in accounts_db.list_accounts():
        if not acct.get("is_enabled"):
            continue
        can_heal = bool(acct.get("has_password")) and bool(acct.get("auto_login_enabled"))
        targets.append((acct["id"], acct.get("display_name") or f"account:{acct['id']}", can_heal))
    return targets


def _probe_child(account_id: int) -> bool:
    """Broker-verified child-session probe (issue #658).

    ``_is_connected`` alone is a DATE check (auth row + last_login_at today) —
    it cannot see a token Kite killed mid-session (one active session per
    user). It stays as the cheap pre-filter: a stale date is dead without a
    broker call (tokens die at the daily reset regardless). A fresh date is
    then verified with a real broker probe on the child's own token — the same
    ``probe_token`` core the primary's ``is_live_broker_session`` uses, valid
    here because child auth rows store the identical
    ``"<api_key>:<access_token>"`` format.
    """
    from database import broker_accounts_db as accounts_db
    from database.auth_db import get_auth_token
    from services.broker_accounts_service import _is_connected
    from services.broker_session_health import probe_token

    acct = accounts_db.get_account(account_id)
    if not acct or not _is_connected(acct):
        return False
    token = get_auth_token(accounts_db.auth_name(account_id))
    if not token:
        return False
    return probe_token(acct.get("broker") or "zerodha", token)


def _login_child(account_id: int, name: str) -> dict:
    from services.broker_auto_login_service import _auto_login_child

    return _auto_login_child(account_id, name)


def _alert(message: str) -> None:
    """Best-effort loud alert (log + Telegram). Never raises."""
    logger.error(message)
    try:
        if os.getenv("NOTIFY_BROKER_AUTO_LOGIN", "true").lower() != "true":
            return
        from services.notification_service import get_notification_service

        get_notification_service().notify("broker_auto_login", f"⚠️ {message}")
    except Exception:
        logger.exception("auto-login watcher: alert notification failed")


# --------------------------------------------------------------------------- #
# Loop + boot entry
# --------------------------------------------------------------------------- #
def _watch_loop() -> None:
    interval = _interval_seconds()
    start, end = _window()
    logger.info(
        "broker auto-login watcher started (every %ds, window %02d:%02d..%02d:%02d IST)",
        interval,
        start.hour,
        start.minute,
        end.hour,
        end.minute,
    )
    from services.thread_registry import beat as _beat

    state = WatcherState()
    while not _stop_event.is_set():
        _beat("BrokerAutoLoginWatcher")
        # Master flag is read every tick (DB-backed) so a UI toggle applies within
        # one interval without a restart. Disabled → beat + sleep, no logins.
        if not watcher_enabled():
            _stop_event.wait(interval)
            continue
        try:
            results = evaluate_once(
                state,
                datetime.now(_IST),
                probe_primary=_probe_primary,
                login_primary=_login_primary,
                list_child_targets=_list_child_targets,
                probe_child=_probe_child,
                login_child=_login_child,
            )
            for r in results:
                logger.info(
                    f"auto-login watcher action: {r['scope']} ok={r['ok']} — {r['message']}"
                )
        except Exception:  # a tick must never kill the loop
            logger.exception("broker auto-login watcher tick failed")
        _stop_event.wait(interval)
    logger.info("broker auto-login watcher stopped")


def _boot_worker() -> None:
    """One-shot boot login attempt, then start the watch loop.

    Waits briefly for a broker session to *not* be needed — i.e. if the stored
    token is already live we skip the boot attempt (common restart case); if it
    is dead and credentials are configured, we auto-login immediately so a boot
    before the reset does not sit tokenless.
    """
    from services.thread_registry import beat as _beat

    _beat("BrokerAutoLoginBoot")
    # Small settle delay so plugins / DBs are initialised before the first probe.
    _stop_event.wait(_BOOT_WAIT_POLL_SEC)
    try:
        if watcher_enabled() and not _probe_primary():
            logger.info("broker auto-login: no live session at boot — attempting auto-login")
            from services.broker_auto_login_service import auto_login_all

            auto_login_all()
    except Exception:
        logger.exception("broker auto-login boot attempt failed")
    start_watcher()


def start_watcher() -> bool:
    """Start the watch daemon thread (idempotent). Returns True if started.

    Always starts the thread — it no-ops each tick while the DB master flag is
    off, so flipping the UI toggle on takes effect without a restart.
    """
    global _watch_thread
    if _watch_thread is not None and _watch_thread.is_alive():
        return False
    _stop_event.clear()
    _watch_thread = threading.Thread(target=_watch_loop, daemon=True, name="BrokerAutoLoginWatcher")
    _watch_thread.start()
    return True


def stop_watcher() -> None:
    """Signal the watch loop to exit (tests / shutdown)."""
    _stop_event.set()


def init_broker_auto_login(app=None) -> None:
    """Boot hook: start the watcher (always) + a one-shot boot login if enabled.

    The watcher thread always starts so a UI toggle applies without a restart; it
    no-ops while the DB master flag is off. The boot one-shot login inside
    ``_boot_worker`` is itself gated on the flag.
    """
    _stop_event.clear()
    threading.Thread(target=_boot_worker, daemon=True, name="BrokerAutoLoginBoot").start()
    logger.info("broker auto-login boot+watcher initialized")
