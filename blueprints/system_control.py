"""Process control for this OpenAlgo instance (issue #694).

GET  /system/api/status
    Read-only: PID, uptime, git branch@commit, live scheduler/thread counts.
    Session auth.

POST /system/api/shutdown
    Guarded process exit — the dashboard's "Shut down OpenAlgo" button.
    Session auth + global CSRF. The body MUST carry ``confirm: "SHUTDOWN"``
    (verified server-side — the typed phrase is a server check, not a UI
    nicety), and during market hours on a trading day the request is refused
    unless ``override_market_hours: true``. The refusal payload carries
    best-effort open-position hints so the dialog can show what a mid-market
    kill leaves unmanaged (T+1 exits, EOD watchdogs and square-off backstops
    all die with the process).

Exit mechanics: the endpoint responds 200 first; a named daemon thread
(``system_shutdown_exit``, catalogued in the thread registry) then logs and
Telegram-alerts, asks the WS-proxy supervisor to stand down (so it does not
fight the exit by restarting the subprocess it supervises), shuts down the
APScheduler instances it can reach, and finally calls ``os._exit(0)``.
``os._exit`` is deliberate: ``sys.exit`` from a request thread only kills that
thread, and Windows signal delivery to the dev server is unreliable — explicit
cleanup then a hard exit is the honest version, identical under the Windows
dev server and eventlet/gunicorn ``-w 1``. Every cleanup step is individually
wrapped: a shutdown that can hang on its own cleanup is worse than an abrupt
one, so failures are logged and skipped, never allowed to block the exit.

Read-only on every trading path — this module imports no strategy code and
never places, modifies or cancels an order.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess  # nosec B404 — fixed git argv only, no user input reaches it
import threading
import time

import pytz
from flask import Blueprint, jsonify, request

from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

system_bp = Blueprint("system_bp", __name__, url_prefix="/system")

_IST = pytz.timezone("Asia/Kolkata")
_BOOT_TS = time.time()

# Market-hours guard window (IST). Wider than the 09:15-15:30 session on
# purpose: pre-open arms (09:10) and post-close reconciles (15:30-15:35) are
# also work a shutdown would kill mid-flight.
_GUARD_START = "09:00"
_GUARD_END = "15:35"

_CONFIRM_PHRASE = "SHUTDOWN"

_git_cache: dict | None = None


def _git_info() -> dict:
    """{branch, commit} of the running checkout; cached; fail-open to unknown."""
    global _git_cache
    if _git_cache is not None:
        return _git_cache
    info = {"branch": "unknown", "commit": "unknown"}
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for key, args in (
            ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
            ("commit", ["git", "rev-parse", "--short", "HEAD"]),
        ):
            out = subprocess.run(  # nosec B603 — fixed argv above, no user input
                args, cwd=root, capture_output=True, text=True, timeout=3, check=False
            )
            if out.returncode == 0 and out.stdout.strip():
                info[key] = out.stdout.strip()
    except Exception:
        logger.exception("system: git info read failed")
    _git_cache = info
    return info


def _now_ist() -> dt.datetime:
    return dt.datetime.now(_IST)


def _market_guard_active(now: dt.datetime | None = None) -> bool:
    """True when a shutdown should require the explicit override.

    Trading-day aware via ``data_freshness_service.is_trading_day``; if that
    check itself fails, fail TOWARD guarding on weekdays — a spurious extra
    confirmation is cheap, a silent mid-market kill is not.
    """
    now = now or _now_ist()
    hhmm = now.strftime("%H:%M")
    if not (_GUARD_START <= hhmm < _GUARD_END):
        return False
    try:
        from services.data_freshness_service import is_trading_day

        return bool(is_trading_day(now.date()))
    except Exception:
        logger.exception("system: is_trading_day failed — guarding on weekday rule")
        return now.weekday() < 5


def _open_position_hints() -> dict[str, int]:
    """Best-effort open-row counts per strategy journal, for the dialog.

    Each source is independently wrapped and silently omitted when unreadable —
    a missing count degrades the warning to its generic form; it must never
    invent a number (the #568 rule: no metric ever borrows or guesses).
    """
    hints: dict[str, int] = {}
    try:
        from database.open15_breakout_db import _REAL_FILL, Open15Trade, db_session

        n = db_session.query(Open15Trade).filter(Open15Trade.status == "open", _REAL_FILL).count()
        db_session.remove()
        if n:
            hints["open15_vol_breakout"] = n
    except Exception:
        logger.exception("system: open15 open-row hint failed")
    return hints


@system_bp.route("/api/status", methods=["GET"])
@check_session_validity
def status():
    up_s = int(time.time() - _BOOT_TS)
    schedulers = jobs = None
    try:
        from services.scheduler_registry import live_jobs

        live = live_jobs()
        jobs = len(live)
        schedulers = len({j.get("scheduler") for j in live.values() if j.get("scheduler")})
    except Exception:
        logger.exception("system: scheduler snapshot failed")
    threads = None
    try:
        threads = sum(1 for t in threading.enumerate() if t.daemon)
    except Exception:
        logger.exception("system: thread snapshot failed")
    return jsonify(
        {
            "status": "running",
            "pid": os.getpid(),
            "uptime_s": up_s,
            "started_at": dt.datetime.fromtimestamp(_BOOT_TS, _IST).strftime(
                "%Y-%m-%d %H:%M:%S IST"
            ),
            **_git_info(),
            "live_jobs": jobs,
            "schedulers": schedulers,
            "daemon_threads": threads,
            "market_guard_active": _market_guard_active(),
        }
    )


def _graceful_exit(requested_by: str) -> None:
    """Log, alert, stand down supervisors/schedulers, then hard-exit.

    Every step wrapped: cleanup must never block the exit it serves.
    """
    logger.error("system: SHUTDOWN requested via dashboard by %s — exiting", requested_by)
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify(
            "system_shutdown",
            f"OpenAlgo shutting down — requested from the dashboard by {requested_by} "
            f"at {_now_ist().strftime('%H:%M:%S IST')}. Restart is manual: uv run app.py",
        )
    except Exception:
        logger.exception("system: shutdown Telegram notify failed")
    try:
        from services.ws_proxy_supervisor import get_supervisor

        sup = get_supervisor()
        if sup is not None:
            sup.stop()
    except Exception:
        logger.exception("system: ws-proxy supervisor stop failed")
    try:
        from services.scheduler_registry import _resolve_schedulers

        for name, sched in _resolve_schedulers():
            try:
                if getattr(sched, "running", False):
                    sched.shutdown(wait=False)
            except Exception:
                logger.exception("system: scheduler %s shutdown failed", name)
    except Exception:
        logger.exception("system: scheduler enumeration failed")
    time.sleep(1.0)  # let the HTTP response flush and the log lines land
    logger.error("system: bye")
    os._exit(0)


@system_bp.route("/api/shutdown", methods=["POST"])
@check_session_validity
def shutdown():
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != _CONFIRM_PHRASE:
        return jsonify(
            {"status": "error", "message": f'confirm must be exactly "{_CONFIRM_PHRASE}"'}
        ), 400

    if _market_guard_active() and not body.get("override_market_hours"):
        return jsonify(
            {
                "status": "refused",
                "reason": "market_hours",
                "message": (
                    "Market hours (09:00-15:35 IST on a trading day). Shutting down now "
                    "kills T+1 exits, EOD watchdogs and square-off backstops. "
                    "Re-send with override_market_hours=true to proceed anyway."
                ),
                "open_position_hints": _open_position_hints(),
            }
        ), 409

    requested_by = "ui"
    try:
        from flask import session

        requested_by = str(session.get("user") or "ui")
    except Exception:
        logger.exception("system: session user read failed")

    threading.Thread(
        target=_graceful_exit,
        args=(requested_by,),
        daemon=True,
        name="system_shutdown_exit",
    ).start()
    return jsonify(
        {
            "status": "shutting_down",
            "message": "OpenAlgo is shutting down. Restart is manual: uv run app.py on the host.",
            "at": _now_ist().strftime("%H:%M:%S IST"),
        }
    )
