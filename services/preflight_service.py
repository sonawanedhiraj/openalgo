"""Stage-0 ``/preflight`` orchestrator — go / no-go for a scan cycle.

The Cowork ``fno-scan-cycle`` skill calls ``GET /preflight`` at step 0 before
each scan cycle. The route delegates to ``run_preflight()`` which evaluates
five independent checks and returns a structured response with per-check
detail plus a flat ``reasons`` list of failed-check messages. The skill aborts
when ``go_decision == "abort"``.

Design rules:

* **Cheap.** No broker API calls. The broker-session check uses a DB-only
  primitive (a non-revoked ``Auth`` row exists) and skips with ``ok=True``
  if even that primitive can't run — preflight must never abort because we
  can't observe broker state.
* **Fail-safe writes.** Every preflight call leaves a ``cycle_heartbeat``
  trace independent of any real cycle. The heartbeat write is wrapped so
  audit failure can never break the response.
* **Stateless decisions.** Each per-check helper takes its inputs explicitly
  so the tests can drive them in isolation without touching globals.
"""

import json
import os
import re
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

import pytz

from database.daily_intent_db import get_daily_intent
from services import scan_cycle_service
from services.mode_service import EffectiveMode, resolve_effective_mode
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# scan_cycle ids autoincrement from 1, so 0 is a safe sentinel for
# "preflight-only heartbeat with no associated cycle". The cycle_heartbeat
# table has cycle_id NOT NULL but no FK, so this is legal without a schema
# change.
PREFLIGHT_CYCLE_ID_SENTINEL = 0

# Default ceiling on ERROR-level entries in errors.jsonl per trailing hour.
# Bumped 5 → 10 because real prod can occasionally throw under 10/hour
# without being broken (e.g. transient broker hiccups). Configurable via
# PREFLIGHT_MAX_ERRORS_LAST_HOUR.
PREFLIGHT_MAX_ERRORS_LAST_HOUR_DEFAULT = 10

# Default rolling window (minutes) for the recent-errors rate gate. Shortened
# 60 → 30 because real intraday problems surface fast; a long window just keeps
# stale errors hanging around. Configurable via PREFLIGHT_ERROR_WINDOW_MIN.
PREFLIGHT_ERROR_WINDOW_MIN_DEFAULT = 30

# Pre-market errors (before today's 09:15 IST open) are structurally different
# from intraday issues — e.g. the morning broker-token-rollover WS reconnect
# storm. They shouldn't block an intraday liveness gate. Toggle via
# PREFLIGHT_IGNORE_PREMARKET_ERRORS (default on).
MARKET_OPEN_LOCAL = dtime(9, 15)

# Substring markers that flag an errors.jsonl entry as originating from the
# test suite rather than a real production code path. Pytest runs land in
# the same centralised errors.jsonl as prod errors, so the preflight rate
# gate has to filter them out — otherwise a single test run can blow past
# the threshold for the rest of the hour.
_TEST_TRACEBACK_MARKERS = ("test/", "unittest/mock", "pytest")
_TEST_MESSAGE_MARKERS = (
    "[pytest]",
    "engine blew up",
    "simulated downstream failure",
    "bogus-id",
)

# Additional unambiguously test-only markers. These are safe because:
#   * ``OID-\d+`` — synthetic test order IDs. Real broker order IDs are bare
#     digit strings (e.g. ``250528000123``); the ``OID-`` prefix never appears
#     in legitimate prod errors.
#   * ``<locals>.`` — Python qualified-name fragment emitted when a function
#     is defined inside another function. The trailing dot distinguishes it
#     from accidental matches; prod code does not nest functions in a way
#     that surfaces this fragment in error messages, but test fixtures do
#     (lambdas/closures defined inside test bodies).
#   * ``<lambda>`` — test fixtures routinely use lambda mocks; prod code
#     paths rarely surface ``<lambda>`` in error messages.
_TEST_SYNTHETIC_OID_RE = re.compile(r"OID-\d+")
_TEST_QUALNAME_MARKERS = ("<locals>.", "<lambda>")


def _now_ist() -> datetime:
    """Wall-clock now in IST. Monkeypatched by tests."""
    return datetime.now(IST)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _premarket_cutoff(now: datetime) -> datetime:
    """Naive-IST datetime for today's 09:15 IST market open.

    Returned naive (no tzinfo) to match the naive timestamps produced by
    :func:`_parse_jsonl_ts`.
    """
    return now.replace(tzinfo=None).replace(
        hour=MARKET_OPEN_LOCAL.hour,
        minute=MARKET_OPEN_LOCAL.minute,
        second=0,
        microsecond=0,
    )


def _env_time(name: str, default: str) -> dtime:
    raw = (os.getenv(name) or default).strip()
    parts = raw.split(":")
    try:
        return dtime(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        d = default.split(":")
        return dtime(int(d[0]), int(d[1]))


# ---------------------------------------------------------------------------
# Per-check helpers
# ---------------------------------------------------------------------------


def _check_intent(today_str: str) -> dict:
    """Is there any daily_intent on record for today (IST)?"""
    row = get_daily_intent(today_str)
    if row is None:
        return {
            "ok": False,
            "value": None,
            "set_by": None,
            "reason": "no daily_intent declared for today",
        }
    return {
        "ok": True,
        "value": row.get("intent"),
        "set_by": row.get("set_by"),
        "reason": None,
    }


def _check_effective_mode(today_str: str) -> dict:
    """Is the resolved effective mode actionable (not 'skip')?

    Note: DISABLED is captured by the intent check; this check only fails on
    SKIP — an explicit operator decision to sit the day out.
    """
    try:
        mode = resolve_effective_mode(today_str)
    except Exception as e:
        logger.exception("preflight: effective_mode resolution failed: %s", e)
        return {
            "ok": False,
            "value": None,
            "reason": f"effective_mode resolution failed: {e}",
        }

    if mode == EffectiveMode.SKIP:
        return {"ok": False, "value": "skip", "reason": "daily_intent is skip"}

    return {"ok": True, "value": mode.value, "reason": None}


def _check_recent_cycles(now: datetime) -> dict:
    """Has a scan_cycle landed within the staleness window during market hours?

    Rules:
    * Outside market hours (or weekend) → always OK; the scheduler isn't
      expected to be running.
    * Inside market hours with zero cycles today → OK (fresh-start; the
      scheduler hasn't fired yet today, not stalled). This avoids the
      chicken-and-egg deadlock on overnight-restart mornings where
      preflight blocks the very first cycle of the day.
    * Inside market hours with no cycles yet AND current time before the
      first-cycle grace cutoff → OK (legacy guard, retained for cases where
      cycles_since() can't be evaluated).
    * Inside market hours with a stale last cycle → consult preflight
      heartbeats. The webhook only writes a scan_cycle row when at least
      one symbol is found; an all-empty-screener day produces zero rows
      even though the scheduler is firing on schedule. If preflight
      heartbeats land within the window, the scheduler is alive — OK.
    * Inside market hours otherwise → require a cycle within the threshold.
    """
    threshold = _env_int("PREFLIGHT_STALE_CYCLE_MINUTES", 30)
    open_t = _env_time("PREFLIGHT_MARKET_OPEN_IST", "09:15")
    close_t = _env_time("PREFLIGHT_MARKET_CLOSE_IST", "15:30")
    grace_until_t = _env_time("PREFLIGHT_FIRST_CYCLE_GRACE_UNTIL_IST", "09:30")

    is_weekday = now.weekday() < 5
    cur_t = now.time()
    in_market = is_weekday and open_t <= cur_t <= close_t

    cycles: list[dict] = []
    try:
        cycles = scan_cycle_service.get_recent_cycles(hours=24)
    except Exception as e:
        logger.warning("preflight: get_recent_cycles failed: %s", e)

    last_cycle_at = cycles[0]["started_at"] if cycles else None
    minutes_since: int | None = None
    if last_cycle_at:
        try:
            last_dt = datetime.fromisoformat(last_cycle_at)
            if last_dt.tzinfo is None:
                last_dt = IST.localize(last_dt)
            delta = (now - last_dt).total_seconds() / 60.0
            minutes_since = max(int(delta), 0)
        except (ValueError, TypeError) as e:
            logger.warning("preflight: unparseable last_cycle_at %r: %s", last_cycle_at, e)
            minutes_since = None

    base = {
        "last_cycle_at": last_cycle_at,
        "minutes_since": minutes_since,
        "threshold_minutes": threshold,
    }

    if not in_market:
        return {**base, "ok": True, "reason": None}

    # Fresh-start path: distinguish "no cycles at all today" (scheduler
    # hasn't run yet — not stalled) from "cycles fired earlier today but
    # last one is stale" (genuine scheduler stall). The legacy code aborted
    # on both, which deadlocked the morning-of-restart skill at Step 0.
    today_start_iso = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    cycles_today: int | None = None
    try:
        cycles_today = scan_cycle_service.cycles_since(today_start_iso)
    except Exception as e:
        # Read failure — fall through to legacy grace/staleness logic.
        logger.warning("preflight: cycles_since failed: %s", e)

    if cycles_today == 0:
        return {
            **base,
            "ok": True,
            "reason": "no cycles today — fresh-start (scheduler hasn't fired yet)",
        }

    stale_reason = (
        f"no scan_cycle in last {threshold} minutes during market hours "
        "— scheduler may be stalled"
    )

    if last_cycle_at is None:
        # First-cycle-of-the-day grace.
        if cur_t < grace_until_t:
            return {**base, "ok": True, "reason": None}
        return {**base, "ok": False, "reason": stale_reason}

    if minutes_since is None or minutes_since > threshold:
        # The webhook only writes a scan_cycle row when at least one symbol
        # is found, so an empty-screener day produces no rows even though
        # the scheduler is alive. Fall back to preflight heartbeats — they
        # land every cycle regardless of signal count.
        window_start = (now - timedelta(minutes=threshold)).isoformat()
        try:
            hb_count = scan_cycle_service.preflight_heartbeats_since(window_start)
        except Exception as e:
            logger.warning("preflight: preflight_heartbeats_since failed: %s", e)
            hb_count = 0
        if hb_count > 0:
            return {
                **base,
                "ok": True,
                "preflight_heartbeats_in_window": hb_count,
                "reason": (
                    "preflight heartbeats present; scheduler alive but no "
                    "signals matched"
                ),
            }
        return {**base, "ok": False, "reason": stale_reason}

    return {**base, "ok": True, "reason": None}


def _check_broker_session() -> dict:
    """Fast DB-only proxy for 'is the broker session live?'

    A non-revoked row in the ``Auth`` table indicates the user has an active
    broker session — no API call to the broker. If the primitive can't run
    at all (missing module, missing PEPPER, table absent, query error) we
    skip with ``ok=True`` and a documented reason; preflight must never
    abort because we lack visibility into broker state.
    """
    try:
        from database import auth_db as adb
    except Exception as e:
        return {
            "ok": True,
            "broker": None,
            "user": None,
            "reason": f"check skipped — no fast primitive available: {e}",
        }

    try:
        row = adb.db_session.query(adb.Auth).filter_by(is_revoked=False).first()
    except Exception as e:
        # Table missing, engine wrong, etc. Skip rather than abort.
        return {
            "ok": True,
            "broker": None,
            "user": None,
            "reason": f"check skipped — no fast primitive available: {e}",
        }
    finally:
        try:
            adb.db_session.remove()
        except Exception:
            pass

    if row is None:
        return {
            "ok": False,
            "broker": None,
            "user": None,
            "reason": "no active broker session",
        }

    return {
        "ok": True,
        "broker": row.broker,
        "user": row.name,
        "reason": None,
    }


def _parse_jsonl_ts(ts_raw: str) -> datetime | None:
    """Parse the ``ts`` field from one errors.jsonl line into a naive datetime.

    The centralised JSON logger writes ``"YYYY-MM-DD HH:MM:SS"`` (no TZ); we
    accept a few common variants for robustness. Returns None on failure so
    bad lines are silently skipped.
    """
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S,%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(ts_raw, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(ts_raw)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(IST).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def _is_test_source_entry(entry: dict) -> bool:
    """Heuristic: did this errors.jsonl entry come from the test suite?

    Pytest runs land in the same centralised errors.jsonl as real prod
    errors. We exclude entries whose traceback, logger, or message clearly
    originated in tests so the preflight rate gate doesn't trip on test
    noise.

    Matches if ANY of:
      * the traceback contains ``test/``, ``unittest/mock``, or ``pytest``
      * the logger starts with ``test_`` and has at least two segments after
        ``test_`` (e.g. ``test_signal_review``) — single-segment names like
        ``test_service`` are treated as real services, not tests
      * the message contains ``[pytest]`` or one of the known synthetic
        markers used by the regression suite
      * the message matches ``OID-\\d+`` (synthetic test order id)
      * the message or traceback contains ``<locals>.`` (Python qualified
        name from a nested function — almost always a test fixture)
      * the message contains ``<lambda>`` (lambda mock in a test fixture)
    """
    if not isinstance(entry, dict):
        return False

    raw_tb = entry.get("exception")
    if raw_tb is None:
        raw_tb = entry.get("traceback")
    if isinstance(raw_tb, list):
        tb_text = "".join(str(s) for s in raw_tb)
    elif isinstance(raw_tb, str):
        tb_text = raw_tb
    else:
        tb_text = ""

    if tb_text:
        for marker in _TEST_TRACEBACK_MARKERS:
            if marker in tb_text:
                return True
        # ``<locals>.`` in a traceback frame is also a strong test signal.
        if "<locals>." in tb_text:
            return True

    logger_name = entry.get("logger") or ""
    if isinstance(logger_name, str):
        lname = logger_name.lower()
        # Require at least two underscores total so single-segment service
        # names ("test_service") aren't mistaken for pytest modules
        # ("test_signal_review").
        if lname.startswith("test_") and lname.count("_") >= 2:
            return True

    message = entry.get("message") or ""
    if isinstance(message, str):
        for marker in _TEST_MESSAGE_MARKERS:
            if marker in message:
                return True
        for marker in _TEST_QUALNAME_MARKERS:
            if marker in message:
                return True
        if _TEST_SYNTHETIC_OID_RE.search(message):
            return True

    return False


def _count_recent_errors(
    errors_path: Path,
    now: datetime,
    window_minutes: int,
    premarket_cutoff: datetime | None = None,
) -> int:
    """Count entries in errors.jsonl whose ts is within the trailing window.

    Skips entries flagged by :func:`_is_test_source_entry` so the count
    reflects production-relevant errors only. When ``premarket_cutoff`` is
    given and later than the rolling-window cutoff, it tightens the floor so
    pre-market errors (before today's 09:15 IST open) are excluded.
    """
    cutoff = now.replace(tzinfo=None) - timedelta(minutes=window_minutes)
    if premarket_cutoff is not None and premarket_cutoff > cutoff:
        cutoff = premarket_cutoff
    count = 0
    try:
        with errors_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                ts_raw = obj.get("ts")
                if not ts_raw:
                    continue
                parsed = _parse_jsonl_ts(ts_raw)
                if parsed is None:
                    continue
                if parsed < cutoff:
                    continue
                if _is_test_source_entry(obj):
                    continue
                count += 1
    except OSError as e:
        logger.warning("preflight: errors.jsonl read failed (%s): %s", errors_path, e)
        return 0
    return count


def _check_recent_errors(now: datetime) -> dict:
    """Is the ERROR rate in the recent rolling window below threshold?"""
    threshold = _env_int(
        "PREFLIGHT_MAX_ERRORS_LAST_HOUR", PREFLIGHT_MAX_ERRORS_LAST_HOUR_DEFAULT
    )
    window_minutes = _env_int(
        "PREFLIGHT_ERROR_WINDOW_MIN", PREFLIGHT_ERROR_WINDOW_MIN_DEFAULT
    )
    log_dir = os.getenv("LOG_DIR", "log")
    errors_path = Path(log_dir) / "errors.jsonl"

    if not errors_path.exists():
        return {
            "ok": True,
            "count_last_hour": 0,
            "window_minutes": window_minutes,
            "threshold": threshold,
            "reason": "no errors.jsonl found",
        }

    premarket_cutoff: datetime | None = None
    if _env_bool("PREFLIGHT_IGNORE_PREMARKET_ERRORS", True):
        candidate = _premarket_cutoff(now)
        # Before today's 09:15 open the cutoff is in the future — fall back to
        # the plain rolling window so pre-market preflight calls still count.
        if candidate <= now.replace(tzinfo=None):
            premarket_cutoff = candidate

    count = _count_recent_errors(
        errors_path,
        now,
        window_minutes=window_minutes,
        premarket_cutoff=premarket_cutoff,
    )
    if count > threshold:
        return {
            "ok": False,
            "count_last_hour": count,
            "window_minutes": window_minutes,
            "threshold": threshold,
            "reason": f"{count} errors in last {window_minutes} min",
        }
    return {
        "ok": True,
        "count_last_hour": count,
        "window_minutes": window_minutes,
        "threshold": threshold,
        "reason": None,
    }


# ---------------------------------------------------------------------------
# Heartbeat side-effect
# ---------------------------------------------------------------------------


def _write_preflight_heartbeat(status: str, detail: Any) -> None:
    """Persist one ``cycle_heartbeat`` row with stage='preflight'.

    Uses ``PREFLIGHT_CYCLE_ID_SENTINEL`` (= 0) because the column is
    NOT NULL but has no FK. ScanCycle ids autoincrement from 1 so this never
    collides with a real cycle.

    Fail-safe: any DB error logs a warning and returns silently. Preflight
    must always return a usable response even if the audit write fails.
    """
    from database import scan_cycle_db as scdb

    try:
        if isinstance(detail, (dict, list)):
            detail_str: str | None = json.dumps(detail, default=str)
        else:
            detail_str = detail

        row = scdb.CycleHeartbeat(
            cycle_id=PREFLIGHT_CYCLE_ID_SENTINEL,
            stage="preflight",
            ts=datetime.now(IST).isoformat(),
            status=status,
            detail=detail_str,
        )
        scdb.db_session.add(row)
        scdb.db_session.commit()
    except Exception as e:
        logger.exception("preflight: heartbeat write failed: %s", e)
        try:
            scdb.db_session.rollback()
        except Exception:
            pass
    finally:
        try:
            scdb.db_session.remove()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _check_daily_circuit_breaker() -> dict:
    """Has today's realized P&L tripped the Stage-0 daily circuit breaker?

    Delegates to :func:`services.risk_service.daily_circuit_breaker_tripped`.
    That helper is fail-safe — a data read error returns ``(False, "")`` so
    preflight is never blocked because the metric source is unavailable.
    """
    try:
        from services.risk_service import daily_circuit_breaker_tripped

        tripped, reason = daily_circuit_breaker_tripped()
    except Exception as e:
        logger.warning("preflight: circuit-breaker check raised: %s", e)
        return {"ok": True, "tripped": False, "reason": None}

    if tripped:
        return {"ok": False, "tripped": True, "reason": reason}
    return {"ok": True, "tripped": False, "reason": None}


def run_preflight() -> dict:
    """Evaluate all preflight checks and return the structured response."""
    now = _now_ist()
    today_str = now.strftime("%Y-%m-%d")

    intent = _check_intent(today_str)
    effective_mode = _check_effective_mode(today_str)
    recent_cycles = _check_recent_cycles(now)
    broker_session = _check_broker_session()
    recent_errors = _check_recent_errors(now)
    daily_circuit_breaker = _check_daily_circuit_breaker()

    checks = {
        "intent": intent,
        "effective_mode": effective_mode,
        "recent_cycles": recent_cycles,
        "broker_session": broker_session,
        "recent_errors": recent_errors,
        "daily_circuit_breaker": daily_circuit_breaker,
    }

    reasons = [c["reason"] for c in checks.values() if not c["ok"] and c.get("reason")]
    go = all(c["ok"] for c in checks.values())

    response = {
        "ok": go,
        "go_decision": "go" if go else "abort",
        "checked_at": now.isoformat(),
        "checks": checks,
        "reasons": reasons,
    }

    _write_preflight_heartbeat(
        "ok" if go else "error",
        {"go_decision": response["go_decision"], "reasons": reasons},
    )

    # Fail-safe operator notification on abort. Never block the response on a
    # notification failure — the skill must still see go_decision='abort' and
    # bail cleanly even if Telegram is down.
    try:
        from services.notification_service import get_notification_service

        notifier = get_notification_service()
        if not go:
            # De-duplicated inside the notifier: repeated aborts for the same
            # failure collapse to one alert + cooldown reminders (2026-06-03).
            notifier.publish_preflight_abort(reasons)
        else:
            # Emits a one-shot "✅ Preflight cleared" only if a prior abort
            # episode was active; otherwise a no-op.
            notifier.publish_preflight_clear()
    except Exception as e:  # noqa: BLE001 — fail-safe
        logger.warning("preflight: notification publish failed: %s", e)

    return response
