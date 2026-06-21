"""Scanner pre-entry smoke check — 09:18 IST Tier-2 safety gate.

Closes the gap CLAUDE.md flags in the Tier-1 hardening section:

    Limitation: a *total* feed outage produces no bar closes at all, so this
    path never fires — that case is the 15:18 smoke check's job (Tier 2, not
    yet shipped).

This module IS that Tier 2 — adapted for the scanner's morning cadence (the
scanner does not have a single entry-job at 15:20 like sector_follow; it
evaluates per 5m bar close all session long). The check fires once at
09:18 IST (post-open, pre-first-evaluatable-bar at 09:30) and asserts that
the four conditions a healthy scanner depends on are in place:

1. **Tick aggregator coverage** — the in-process ``ScannerService`` has
   produced at least one live bar today for ≥ ``SCANNER_SMOKE_MIN_COVERAGE``
   (default 0.5) of the ``SCANNER_SYMBOLS`` universe. Mirrors the
   sector_follow probe.
2. **Stored 1m freshness** — the most recent ``data_health_check`` row for
   ``strategy_name='scanner_universe_1m'`` reports ``overall_ok=True``.
3. **Stored D freshness** — the most recent ``data_health_check`` row for
   ``strategy_name='scanner_universe_D'`` reports ``overall_ok=True``.
4. **Broker session live** — a broker API key is configured (operator
   logged in for today).

The Friday 2026-06-19 outage was exactly this failure mode: scanner-universe
1m had been stale since 06-15 (gate 2), OpenAlgo was down pre-12:31 IST
(gate 4), and yet the scanner had no morning assertion. The Tier-1
completeness metric measures "are bars closing?" but cannot detect either
of these — the bars never start.

On failure: writes a ``data_health_check`` row with
``strategy_name='scanner_smoke_check'`` and emits a CRIT Telegram via
``notification_service.notify('scanner_smoke_check_fail', …)``.
**No runtime override is written** — unlike sector_follow which holds a
single entry-job, the scanner is a passive consumer driven by 5m bar
closes; there's no entry-job to gate. Visibility *is* the fix; the operator
acts on the alert.

Fail-safe: every external call is wrapped, exceptions never bubble back into
the APScheduler thread, and the flag-off path is a no-op.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from utils.logging import get_logger

logger = get_logger(__name__)

# IST tz used for the cron, the freshness comparisons, and the date keys.
_IST = timezone(timedelta(hours=5, minutes=30))

# Per-process dedup so the APScheduler firing twice in the same minute (e.g.
# misconfigured catch-up) doesn't double-alert. Reset at boot, which is fine —
# a fresh alert after a restart is desirable, not noise.
_last_alert_date: date | None = None


# --------------------------------------------------------------------------- #
# Flags (CLAUDE.md PARAMETER_LOG)
# --------------------------------------------------------------------------- #


def smoke_check_enabled() -> bool:
    """``SCANNER_SMOKE_CHECK_ENABLED`` env flag (default true). When false the
    APScheduler job is registered but the service is a no-op."""
    return os.getenv("SCANNER_SMOKE_CHECK_ENABLED", "true").lower() == "true"


def smoke_min_coverage() -> float:
    """``SCANNER_SMOKE_MIN_COVERAGE`` (default 0.5). The minimum fraction of
    ``SCANNER_SYMBOLS`` that must have produced at least one live bar today."""
    try:
        return float(os.getenv("SCANNER_SMOKE_MIN_COVERAGE", "0.5"))
    except ValueError:
        return 0.5


def smoke_check_time() -> tuple[int, int]:
    """``SCANNER_SMOKE_CHECK_TIME`` (default 09:18 IST). The cron fire time."""
    raw = os.getenv("SCANNER_SMOKE_CHECK_TIME", "09:18")
    try:
        hh, mm = raw.split(":", 1)
        return int(hh), int(mm)
    except (ValueError, TypeError):
        return 9, 18


# --------------------------------------------------------------------------- #
# Production wiring — what the live boot path uses.
# --------------------------------------------------------------------------- #


def production_intraday_provider(symbol: str, as_of: datetime) -> tuple[float | None, float | None]:
    """TODAY's ``(close, volume)`` from the in-process scanner aggregator;
    ``(None, None)`` when the scanner is absent or has no bars for today.
    Mirrors ``sector_follow_service.production_intraday_provider`` — they
    share the same scanner singleton."""
    try:
        from services.scanner_service import get_scanner_service

        svc = get_scanner_service()
        if svc is None:
            return None, None
        as_of_date = as_of.astimezone(_IST).date()
        return svc.get_today_ohlcv(symbol, as_of_date)
    except Exception:
        logger.debug(
            "scanner smoke-check intraday provider unavailable for %s", symbol, exc_info=True
        )
        return None, None


def production_broker_session_checker() -> bool:
    """True iff a broker API key is configured (operator logged in)."""
    try:
        from database.auth_db import get_first_available_api_key

        return bool(get_first_available_api_key())
    except Exception:
        logger.debug("scanner smoke-check broker-session probe failed", exc_info=True)
        return False


def production_freshness_reader(strategy_name: str) -> dict | None:
    """Latest ``data_health_check`` row for ``strategy_name``, or None."""
    try:
        from database.data_health_db import get_latest_check

        return get_latest_check(strategy_name)
    except Exception:
        logger.debug(
            "scanner smoke-check freshness read failed for %s", strategy_name, exc_info=True
        )
        return None


def production_universe_provider() -> list[str]:
    """The same ``SCANNER_SYMBOLS`` source the rest of the scanner uses."""
    raw = os.getenv("SCANNER_SYMBOLS", "")
    return sorted({s.strip().upper() for s in raw.split(",") if s.strip()})


def production_notifier(message: str) -> None:
    """Telegram CRIT via the existing notification_service. Fail-safe."""
    try:
        from services.notification_service import notify

        notify("scanner_smoke_check_fail", message)
    except Exception:
        logger.exception("scanner smoke-check Telegram notify failed")


def production_health_writer(
    overall_ok: bool, stale_symbols: list[str], details: dict, alert_sent: bool
) -> None:
    """Write the canonical ``data_health_check`` row for this strategy."""
    try:
        from database.data_health_db import insert_check

        insert_check(
            strategy_name="scanner_smoke_check",
            overall_ok=overall_ok,
            stale_symbols=stale_symbols,
            details=details,
            alert_sent=1 if alert_sent else 0,
        )
    except Exception:
        logger.exception("scanner smoke-check health-row write failed")


# --------------------------------------------------------------------------- #
# The actual check — pure function, all I/O is injected for testability.
# --------------------------------------------------------------------------- #


def assert_scanner_pipeline_healthy(
    *,
    as_of: datetime | None = None,
    universe_provider: Callable[[], list[str]] = production_universe_provider,
    intraday_provider: Callable[
        [str, datetime], tuple[float | None, float | None]
    ] = production_intraday_provider,
    freshness_reader: Callable[[str], dict | None] = production_freshness_reader,
    broker_session_checker: Callable[[], bool] = production_broker_session_checker,
    notifier: Callable[[str], None] = production_notifier,
    health_writer: Callable[[bool, list[str], dict, bool], None] = production_health_writer,
) -> tuple[bool, dict]:
    """Run the four gates and return ``(ok, details)``.

    When ``ok`` is False, a CRIT Telegram is emitted and a
    ``data_health_check`` row (``strategy_name='scanner_smoke_check'``) is
    written. When True, an INFO log is written and the row is still written
    so the operator dashboard has a daily heartbeat.

    Fail-open on the flag being off (returns ``(True, {"skipped": True})``).
    """
    if not smoke_check_enabled():
        logger.debug("scanner smoke check skipped (flag off)")
        return True, {"skipped": True}

    now = as_of or datetime.now(tz=_IST)
    today = now.astimezone(_IST).date()
    global _last_alert_date  # noqa: PLW0603 — module-level dedup

    universe = universe_provider()
    total = len(universe)
    if total == 0:
        # Empty SCANNER_SYMBOLS — treat as configured-off; never fire.
        details = {"universe_empty": True}
        logger.info("scanner smoke check no-op: SCANNER_SYMBOLS unset")
        return True, details

    # Gate 1 — aggregator coverage
    stale_symbols: list[str] = []
    n_have = 0
    for sym in universe:
        try:
            close, _vol = intraday_provider(sym, now)
        except Exception:
            close = None
        if close is not None:
            n_have += 1
        else:
            stale_symbols.append(sym)
    min_cov = smoke_min_coverage()
    agg_frac = n_have / total
    agg_ok = agg_frac >= min_cov

    # Gate 2 + 3 — stored freshness (1m AND D)
    fresh_1m = freshness_reader("scanner_universe_1m") or {}
    fresh_d = freshness_reader("scanner_universe_D") or {}
    fresh_1m_ok = bool(fresh_1m.get("overall_ok"))
    fresh_d_ok = bool(fresh_d.get("overall_ok"))

    # Gate 4 — broker session
    try:
        session_ok = bool(broker_session_checker())
    except Exception:
        session_ok = False

    ok = agg_ok and fresh_1m_ok and fresh_d_ok and session_ok
    details = {
        "as_of": today.isoformat(),
        "aggregator_coverage": f"{n_have}/{total}",
        "aggregator_frac": round(agg_frac, 3),
        "aggregator_ok": agg_ok,
        "min_coverage": min_cov,
        "fresh_1m_ok": fresh_1m_ok,
        "fresh_d_ok": fresh_d_ok,
        "broker_session_ok": session_ok,
    }

    if ok:
        logger.info("scanner smoke check 09:18 PASSED: %s", details)
        health_writer(True, [], details, False)
        return True, details

    # FAIL — build the reason string and alert (deduped to once per day).
    reasons = []
    if not agg_ok:
        reasons.append(f"aggregator coverage {n_have}/{total} (<{min_cov:.0%})")
    if not fresh_1m_ok:
        reasons.append("scanner_universe_1m stale")
    if not fresh_d_ok:
        reasons.append("scanner_universe_D stale")
    if not session_ok:
        reasons.append("broker session not live")
    reason = "; ".join(reasons)
    logger.error("scanner smoke check 09:18 FAILED: %s", reason)

    alert_already_sent_today = _last_alert_date == today
    if not alert_already_sent_today:
        message = (
            f"🚨 SCANNER smoke check 09:18 FAILED ({today.isoformat()}): {reason}. "
            "The in-house scanner may produce stale or zero hits today — "
            "operator review required."
        )
        try:
            notifier(message)
            _last_alert_date = today
        except Exception:
            logger.exception("scanner smoke-check Telegram send failed")

    health_writer(False, stale_symbols[:50], details, not alert_already_sent_today)
    return False, details


# --------------------------------------------------------------------------- #
# APScheduler entry point — module-level so the jobstore can serialize it.
# --------------------------------------------------------------------------- #


def _smoke_check_job() -> None:
    """Called by APScheduler at 09:18 IST Mon-Fri. Wraps everything in a
    try/except — the scheduler thread must never see an exception."""
    try:
        assert_scanner_pipeline_healthy()
    except Exception:
        logger.exception("scanner smoke check job raised")


def init_scanner_smoke_check(app=None, scheduler=None):
    """Register the 09:18 IST APScheduler job. The job is registered even when
    the flag is off so toggling the flag at runtime takes effect without
    re-init; the per-fire ``smoke_check_enabled()`` check gates the work."""
    try:
        from apscheduler.triggers.cron import CronTrigger

        if scheduler is None:
            # Pull the shared scheduler the rest of the project uses.
            from services.historify_scheduler_service import (
                get_historify_scheduler,
            )

            scheduler = get_historify_scheduler()
        if scheduler is None:
            logger.warning(
                "scanner smoke check: no scheduler available — skipping job registration"
            )
            return

        hh, mm = smoke_check_time()
        scheduler.add_job(
            _smoke_check_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=hh, minute=mm, timezone="Asia/Kolkata"),
            id="scanner_smoke_check",
            replace_existing=True,
            name=f"Scanner pre-entry smoke check ({hh:02d}:{mm:02d} IST)",
        )
        logger.info(
            "scanner_smoke_check registered (enabled=%s, time=%02d:%02d IST, min_cov=%.2f)",
            smoke_check_enabled(),
            hh,
            mm,
            smoke_min_coverage(),
        )
    except Exception:
        logger.exception("init_scanner_smoke_check failed")
