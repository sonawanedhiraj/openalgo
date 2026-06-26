"""Scanner zero-results tripwire — downstream silent-failure detector.

The Tier-1 per-cycle completeness metric measures *"are bars closing?"* —
not *"are rules producing any output?"* On Friday 2026-06-19 completeness
sat at 56% (above the 50% WARN floor) while the scanner produced 0 BUY
hits all day because the stored daily gates ran against ~6-day-old bars.
This module catches that exact gap.

A periodic APScheduler check fires every ``SCANNER_DRY_CHECK_INTERVAL_MIN``
minutes during market hours (default 5) and measures the gap between
*now* and the latest ``scan_results`` row with ``source='inhouse'``. If
the gap exceeds ``SCANNER_DRY_THRESHOLD_MIN`` (default 30) the tripwire
fires.

Crucially the tripwire distinguishes a **broken pipeline** from a
**genuinely quiet market** via a chartink cross-check:

* Chartink (the reference) has rows in the same 30-min window  →
  in-house is silent while chartink isn't  →  pipeline is broken,
  alert at **CRIT** severity.
* Chartink is also dry  →  the market just isn't producing setups
  today, alert at **WARN** severity (still observable for operator
  triage but not a page).

Skips that never fire:

* Outside 09:15-15:30 IST market hours
* Inside the 09:15-09:30 IST warm-up window (the scanner can't have
  produced anything yet — the 15-min skip + first 5m bar)
* When no broker session is live (operator off — silence is expected)

Dedup is per-day-and-severity: one CRIT and one WARN at most per day.
Process restart resets dedup intentionally (operator wants a fresh
page after a reboot).

Fail-safe: every external call is wrapped; exceptions never bubble back
into the APScheduler thread; the flag-off path is a no-op.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Per-process per-day dedup keyed by severity. Reset at boot (a fresh alert
# after a restart is desirable). The first element of each pair is the
# date; the second is True iff that severity has already alerted today.
_last_crit_date: date | None = None
_last_warn_date: date | None = None

# When the scanner most-recently (re-)subscribed to the WS proxy in THIS
# process. Set by the connect-callback hook registered in
# ``init_scanner_dry_tripwire``. ``check_dry_scanner`` uses this as a floor on
# the effective baseline so a stale yesterday-row doesn't trigger a CRIT in the
# minutes immediately after a restart or mid-day re-login — the scanner needs
# at least one 5m bar close to produce a row. See issue #146 for the false
# CRIT this fixes (2026-06-26 09:35:00 IST: app restarted 09:10, scanner
# subscribed ~09:11, latest_inhouse_at was yesterday's 00:25; without this
# baseline the gap reads 1990 min and CRITs at 09:35 — when in reality the
# scanner has been subscribed for only 24 min).
_scanner_subscribed_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Flags
# --------------------------------------------------------------------------- #


def tripwire_enabled() -> bool:
    """``SCANNER_DRY_TRIPWIRE_ENABLED`` env (default true)."""
    return os.getenv("SCANNER_DRY_TRIPWIRE_ENABLED", "true").lower() == "true"


def dry_threshold_min() -> int:
    """``SCANNER_DRY_THRESHOLD_MIN`` (default 30). Gap in minutes from the last
    inhouse scan_results row before the tripwire fires."""
    try:
        return int(os.getenv("SCANNER_DRY_THRESHOLD_MIN", "30"))
    except ValueError:
        return 30


def check_interval_min() -> int:
    """``SCANNER_DRY_CHECK_INTERVAL_MIN`` (default 5). How often the
    APScheduler job fires during market hours."""
    try:
        return int(os.getenv("SCANNER_DRY_CHECK_INTERVAL_MIN", "5"))
    except ValueError:
        return 5


def subscribe_warmup_min() -> int:
    """``SCANNER_DRY_SUBSCRIBE_WARMUP_MIN`` (default 5 — one 5m bar close).

    Minimum time the scanner must be subscribed before the tripwire is allowed
    to fire on a stale ``last_inhouse_at``. Without this, a fresh boot during
    market hours alerts CRIT before the first bar of the day has closed.
    """
    try:
        return int(os.getenv("SCANNER_DRY_SUBSCRIBE_WARMUP_MIN", "5"))
    except ValueError:
        return 5


# --------------------------------------------------------------------------- #
# Subscribe-hook state (used by the WS connect callback)
# --------------------------------------------------------------------------- #


def mark_scanner_subscribed(when: datetime | None = None) -> None:
    """Record the moment the scanner (re-)subscribed in this process.

    Called by the WS connect callback registered in ``init_scanner_dry_tripwire``.
    Idempotent — repeated calls during a normal session overwrite with the
    most-recent timestamp (a mid-day re-login resets the warmup window, which
    is the right behaviour: the scanner just lost and regained its feed).

    Args:
        when: Timestamp to record (defaults to ``datetime.now(_IST)``).
    """
    global _scanner_subscribed_at  # noqa: PLW0603 — module-level by design
    _scanner_subscribed_at = when or datetime.now(tz=_IST)
    logger.info("scanner_dry tripwire: scanner subscribed at %s", _scanner_subscribed_at)


def production_scanner_subscribed_at() -> datetime | None:
    """Default provider — returns the module-level subscribe timestamp."""
    return _scanner_subscribed_at


def _reset_subscribe_state_for_tests() -> None:
    """Reset module state — tests only."""
    global _scanner_subscribed_at, _last_crit_date, _last_warn_date  # noqa: PLW0603
    _scanner_subscribed_at = None
    _last_crit_date = None
    _last_warn_date = None


# --------------------------------------------------------------------------- #
# Market-hours helpers
# --------------------------------------------------------------------------- #


_SESSION_OPEN = time(9, 15)
_SCANNER_WARMUP_END = time(9, 30)  # 15-min skip + first 5m bar close
_SESSION_CLOSE = time(15, 30)


def _is_market_hours(now: datetime) -> bool:
    """09:15 IST <= now < 15:30 IST AND weekday."""
    ist_now = now.astimezone(_IST)
    if ist_now.weekday() >= 5:  # Saturday/Sunday
        return False
    return _SESSION_OPEN <= ist_now.time() < _SESSION_CLOSE


def _is_warmup(now: datetime) -> bool:
    """09:15 IST <= now < 09:30 IST (no scan_results can have been written
    yet on a normal session — the scanner waits for the first eval bar)."""
    return _SESSION_OPEN <= now.astimezone(_IST).time() < _SCANNER_WARMUP_END


# --------------------------------------------------------------------------- #
# Production wiring
# --------------------------------------------------------------------------- #


def production_latest_inhouse_run_at() -> datetime | None:
    """Latest ``scan_results.run_at`` for ``source='inhouse'``, parsed as an
    aware datetime in IST. Returns None when the table is empty or
    unreachable. Never raises."""
    try:
        from sqlalchemy import desc

        from database.scanner_db import ScanResult, db_session

        try:
            row = (
                db_session.query(ScanResult.run_at)
                .filter(ScanResult.source == "inhouse")
                .order_by(desc(ScanResult.run_at))
                .first()
            )
        finally:
            db_session.remove()
        if row is None or row[0] is None:
            return None
        return _parse_ist(row[0])
    except Exception:
        logger.debug("scanner dry tripwire latest_inhouse_run_at failed", exc_info=True)
        return None


def production_chartink_rows_since(cutoff: datetime) -> bool:
    """True iff at least one ``scan_cycle`` row with ``cycle_kind='chartink'``
    started_at >= ``cutoff``. Read-only; never raises."""
    try:
        from database.scan_cycle_db import ScanCycle, db_session

        cutoff_iso = cutoff.astimezone(_IST).isoformat()
        try:
            n = (
                db_session.query(ScanCycle.id)
                .filter(ScanCycle.cycle_kind == "chartink")
                .filter(ScanCycle.started_at >= cutoff_iso)
                .limit(1)
                .count()
            )
        finally:
            db_session.remove()
        return bool(n)
    except Exception:
        logger.debug("scanner dry tripwire chartink probe failed", exc_info=True)
        return False


def production_broker_session_checker() -> bool:
    """True iff a broker API key is configured (operator logged in)."""
    try:
        from database.auth_db import get_first_available_api_key

        return bool(get_first_available_api_key())
    except Exception:
        logger.debug("scanner dry tripwire broker-session probe failed", exc_info=True)
        return False


def production_notifier(message: str, severity: str) -> None:
    """Telegram via the existing notification_service. Fail-safe.

    Severity is encoded in the message body (the existing notify() doesn't
    take a severity arg); the event_type stays the same so the operator's
    per-event toggle covers both CRIT and WARN."""
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("scanner_dry", message)
    except Exception:
        logger.exception("scanner dry tripwire Telegram notify failed (severity=%s)", severity)


def production_health_writer(severity: str, details: dict, alert_sent: bool) -> None:
    """Write a ``data_health_check`` row with ``strategy_name='scanner_dry'``.
    overall_ok is False when severity != 'ok' (the gap exceeded threshold)."""
    try:
        from database.data_health_db import insert_check

        insert_check(
            strategy_name="scanner_dry",
            overall_ok=(severity == "ok"),
            stale_symbols=[],
            details=details,
            alert_sent=1 if alert_sent else 0,
        )
    except Exception:
        logger.exception("scanner dry tripwire health-row write failed")


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _parse_ist(raw: str) -> datetime | None:
    """Parse an ISO timestamp; assume IST if no tz is attached."""
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_IST)
        return dt
    except Exception:
        logger.debug("scanner dry tripwire could not parse run_at=%r", raw, exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# The check — pure function, all I/O injected for testability.
# --------------------------------------------------------------------------- #


def check_dry_scanner(
    *,
    as_of: datetime | None = None,
    latest_inhouse_provider: Callable[[], datetime | None] = production_latest_inhouse_run_at,
    chartink_has_rows_since: Callable[[datetime], bool] = production_chartink_rows_since,
    broker_session_checker: Callable[[], bool] = production_broker_session_checker,
    notifier: Callable[[str, str], None] = production_notifier,
    health_writer: Callable[[str, dict, bool], None] = production_health_writer,
    subscribed_at_provider: Callable[[], datetime | None] = production_scanner_subscribed_at,
) -> dict:
    """Run the check and return ``{status, severity, gap_min, …}``.

    Statuses:
    * ``flag_off`` — service disabled.
    * ``off_hours`` — outside 09:15-15:30 IST or weekend.
    * ``warmup`` — within 09:15-09:30 IST.
    * ``no_broker`` — broker session not live.
    * ``ok`` — gap is below threshold.
    * ``alerted_crit`` — gap exceeded AND chartink had rows recently → pipeline
      is broken (in-house should match).
    * ``alerted_warn`` — gap exceeded AND chartink also dry → genuinely quiet
      market; still surfaced for operator visibility but not a page.
    * ``dedup_silent`` — would have alerted but already did so at this severity
      today.

    Fail-open on the flag being off.
    """
    if not tripwire_enabled():
        return {"status": "flag_off"}

    now = as_of or datetime.now(tz=_IST)

    # Issue #158 D4: an explicit historical ``as_of`` (a replay / backfill /
    # one-shot diagnostic, NOT the live periodic tick) must never notify.
    # Without this guard, calling ``check_dry_scanner(as_of=<yesterday>)``
    # fires a CRIT Telegram against today's operator. Threshold is 1 hour
    # behind real wall-clock — comfortably past any plausible live tick
    # latency, well short of any replay/diagnostic window. The verdict is
    # still returned (just no notifier/health-writer calls), so callers can
    # use the result for forensic comparison without paging anyone.
    if as_of is not None:
        wall_now = datetime.now(tz=_IST)
        if (wall_now - now).total_seconds() > 3600:
            return {"status": "historical_silent", "as_of": now.isoformat()}

    if not _is_market_hours(now):
        return {"status": "off_hours"}
    if _is_warmup(now):
        return {"status": "warmup"}
    try:
        if not broker_session_checker():
            return {"status": "no_broker"}
    except Exception:
        return {"status": "no_broker"}

    threshold = dry_threshold_min()
    last = latest_inhouse_provider()

    # Subscribe-aware baseline (issue #146). Without this floor the tripwire
    # CRITs at 09:35 IST on every fresh boot, because last_inhouse_at points
    # at yesterday's session and the gap reads 33+ hours.
    try:
        subscribed_at = subscribed_at_provider()
    except Exception:
        logger.debug("scanner_dry tripwire: subscribed_at_provider raised", exc_info=True)
        subscribed_at = None

    subscribe_floor: datetime | None = None
    if subscribed_at is not None:
        # The scanner needs at least one 5m bar close after subscribing before
        # we can fairly expect a row. SCANNER_DRY_SUBSCRIBE_WARMUP_MIN sets that
        # grace; the floor below is the earliest moment a missing row is honest
        # to alert on.
        subscribe_floor = subscribed_at + timedelta(minutes=subscribe_warmup_min())

    if last is None and subscribe_floor is None:
        # Cold start with no subscribe signal — fall back to the 09:30 IST
        # warmup-end cutoff (pre-#146 behaviour).
        warmup_end = now.astimezone(_IST).replace(
            hour=_SCANNER_WARMUP_END.hour,
            minute=_SCANNER_WARMUP_END.minute,
            second=0,
            microsecond=0,
        )
        effective_baseline = warmup_end
    elif last is None:
        effective_baseline = subscribe_floor
    elif subscribe_floor is None:
        effective_baseline = last
    else:
        # Use the later of the two — the scanner's own row beats the floor
        # once it has produced anything; the floor protects the warmup window.
        effective_baseline = max(last, subscribe_floor)

    gap_min = max(0.0, (now - effective_baseline).total_seconds() / 60.0)

    details = {
        "as_of": now.astimezone(_IST).isoformat(),
        "last_inhouse_at": last.isoformat() if last else None,
        "scanner_subscribed_at": subscribed_at.isoformat() if subscribed_at else None,
        "subscribe_warmup_min": subscribe_warmup_min(),
        "gap_min": round(gap_min, 1),
        "threshold_min": threshold,
    }

    if gap_min < threshold:
        # Healthy — write a heartbeat health row but don't alert.
        try:
            health_writer("ok", details, False)
        except Exception:
            logger.debug("scanner dry tripwire ok-heartbeat write failed", exc_info=True)
        return {"status": "ok", "gap_min": gap_min}

    # Tripwire fires — distinguish CRIT (broken pipeline) from WARN (quiet market).
    cutoff = now - timedelta(minutes=threshold)
    try:
        chartink_alive = chartink_has_rows_since(cutoff)
    except Exception:
        # If chartink probe fails, default to WARN (don't escalate on telemetry).
        chartink_alive = False
    severity = "CRIT" if chartink_alive else "WARN"
    details["chartink_has_rows_since_cutoff"] = chartink_alive
    details["severity"] = severity

    today = now.astimezone(_IST).date()
    global _last_crit_date, _last_warn_date  # noqa: PLW0603 — module-level dedup
    last_alert = _last_crit_date if severity == "CRIT" else _last_warn_date
    already_alerted_today = last_alert == today

    if already_alerted_today:
        try:
            health_writer(severity.lower(), details, False)
        except Exception:
            logger.debug("scanner dry tripwire dedup-silent write failed", exc_info=True)
        return {"status": "dedup_silent", "severity": severity, "gap_min": gap_min}

    message = _format_alert(severity, gap_min, last, chartink_alive)
    # f-string (not %s + args) — see scanner_smoke_check_service for the
    # SensitiveDataFilter + record.args desync rationale.
    logger.error(f"scanner_dry tripwire {severity}: {details}")
    try:
        notifier(message, severity)
        if severity == "CRIT":
            _last_crit_date = today
        else:
            _last_warn_date = today
        sent = True
    except Exception:
        logger.exception("scanner_dry tripwire Telegram send failed")
        sent = False

    try:
        health_writer(severity.lower(), details, sent)
    except Exception:
        logger.debug("scanner dry tripwire fire-write failed", exc_info=True)

    return {
        "status": f"alerted_{severity.lower()}",
        "severity": severity,
        "gap_min": gap_min,
        "chartink_alive": chartink_alive,
    }


def _format_alert(
    severity: str, gap_min: float, last: datetime | None, chartink_alive: bool
) -> str:
    last_str = last.astimezone(_IST).strftime("%H:%M:%S") if last else "never (no rows today)"
    icon = "🚨" if severity == "CRIT" else "⚠️"
    diagnosis = (
        "Chartink HAS recent hits — in-house pipeline is degraded."
        if chartink_alive
        else "Chartink is also dry — market likely quiet; surfaced for visibility only."
    )
    return (
        f"{icon} SCANNER {severity}: no in-house scan_results for {gap_min:.0f} min "
        f"(last: {last_str}). {diagnosis}"
    )


# --------------------------------------------------------------------------- #
# APScheduler entry
# --------------------------------------------------------------------------- #


def _tripwire_job() -> None:
    """Called every ``SCANNER_DRY_CHECK_INTERVAL_MIN`` minutes during market
    hours. Wraps everything so the scheduler thread never sees an exception."""
    try:
        check_dry_scanner()
    except Exception:
        logger.exception("scanner_dry tripwire job raised")


def _on_broker_ws_connected(user_id: str, broker: str) -> None:
    """WS connect-callback: stamp ``_scanner_subscribed_at`` so the tripwire
    grants a warmup grace period before alerting on a stale last_inhouse_at.

    The scanner pre-subscribe callback (``services.scanner_presubscribe``) is
    fired by the same event under a different name. Both callbacks run in their
    own daemon threads, so a slow scanner subscribe never delays this stamp.
    See issue #146.
    """
    try:
        mark_scanner_subscribed()
    except Exception:
        logger.exception("scanner_dry tripwire: mark_scanner_subscribed failed")


def init_scanner_dry_tripwire(app=None, scheduler=None):
    """Register the periodic APScheduler job. Registered even when the flag is
    off so toggling at runtime takes effect without re-init."""
    # Wire the subscribe-aware baseline hook (issue #146) regardless of the
    # SCANNER_DRY_TRIPWIRE_ENABLED flag — if the operator flips the flag on at
    # runtime we want the subscribe state already accumulating.
    try:
        from services.ws_connect_callbacks import register_connect_callback

        register_connect_callback("scanner_dry_tripwire_subscribe_hook", _on_broker_ws_connected)
    except Exception:
        logger.exception("scanner_dry tripwire: failed to register WS connect callback")

    try:
        from apscheduler.triggers.cron import CronTrigger

        if scheduler is None:
            from services.historify_scheduler_service import get_historify_scheduler

            scheduler = get_historify_scheduler()
        if scheduler is None:
            logger.warning(
                "scanner dry tripwire: no scheduler available — skipping job registration"
            )
            return

        interval = check_interval_min()
        # Fire on every multiple of `interval` minutes within 09:30..15:30 IST.
        # APScheduler's CronTrigger supports "*/N" for every-N-minutes ranges.
        scheduler.add_job(
            _tripwire_job,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour="9-15",
                minute=f"*/{interval}",
                timezone="Asia/Kolkata",
            ),
            id="scanner_dry_tripwire",
            replace_existing=True,
            name=f"Scanner zero-results tripwire (every {interval}m, 09:30-15:30 IST)",
        )
        logger.info(
            "scanner_dry_tripwire registered (enabled=%s, threshold_min=%d, interval_min=%d)",
            tripwire_enabled(),
            dry_threshold_min(),
            interval,
        )
    except Exception:
        logger.exception("init_scanner_dry_tripwire failed")
