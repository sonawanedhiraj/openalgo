"""Runtime source-divergence WARNING + Telegram alert helper (issue #231).

Why this module exists
----------------------
PR #227 (issue #225) added contract tests under ``test/contracts/`` that pin
the following observable contract for any service reading the same value from
two sources: when the sources DISAGREE, the service either picks the canonical
one OR raises a divergence signal (a ``logger.warning`` line that an operator
can grep in ``errors.jsonl``). Those tests fail-at-PR-time.

This module is the **runtime sibling**: in production, when two sources
disagree by more than a threshold, the helper emits BOTH ``logger.warning``
AND a Telegram alert via :func:`notification_service.notify` with event_type
``source_divergence``. The operator finds out within seconds instead of
discovering the discrepancy in ``errors.jsonl`` after EOD.

Where it's wired in
-------------------
* ``services/scanner_aggregator_seeder.py`` — when ``historify`` and ``broker``
  disagree on the same symbol's most-recent 1m close by > threshold.
* ``services/engine_eod_reconciliation_service.py`` — when ``trade_journal``
  and ``sandbox.db`` disagree on whether a position is closed (here the
  ``value`` axis is the closing quantity: journal expects ``entry_qty`` to be
  closed; sandbox's covering-fill sum is the second source).
* ``services/scan_rules/fno_intraday_{buy,sell}_chartink.py`` — when the
  ``bars_daily`` cache (provider) and the live 5m aggregator disagree on
  today's close by > threshold.

Helper API
----------
:func:`check_and_alert` takes the two labelled source values and dispatches
the alert once-per-(service, symbol, day_ist). The dedup table is an
in-process dict — it resets at boot AND when the IST date rolls (the dedup
key includes ``day_ist``). Repeat calls with the same args on the same IST
day produce exactly one alert; the IST date rollover (or process restart)
clears the dedup so the operator gets a fresh alert the next day.

Safety
------
* **Never raises.** Broker observability code must be defensive — every
  failure path is logged via ``logger.exception`` and swallowed.
* **Flag-gated.** ``SOURCE_DIVERGENCE_ALERTS_ENABLED`` (default ``true``) is
  the single emergency disable; flip false to silence all three integrations
  without code changes.
* **Threshold-gated.** ``SOURCE_DIVERGENCE_THRESHOLD_PCT`` (default ``0.5``)
  is the relative-divergence cutoff in percent. The helper computes
  ``abs(a - b) / max(abs(a), abs(b), 1e-9) * 100`` and only alerts when that
  exceeds the threshold.

Returns ``True`` iff an alert was emitted (so tests can introspect the
behaviour without going through Telegram).
"""

from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta, timezone

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Default threshold percentage — divergence below this is NOT alerted on.
_DEFAULT_THRESHOLD_PCT = 0.5

# Process-wide dedup table. Keyed by (service, symbol, day_ist_iso). Cleared
# whenever a call arrives for a NEW IST date (so a midnight rollover resets
# state cleanly without a cron). Boot is an empty dict — that is the explicit
# "reset on restart" behaviour.
_alert_dedup: dict[tuple[str, str, str], bool] = {}
_alert_dedup_lock = threading.Lock()
# The IST date the dedup table is keyed against. When the helper sees a newer
# day_ist, the dedup table is wiped before recording the new alert.
_alert_dedup_day: date | None = None


def _flag_enabled() -> bool:
    """Master enable for the runtime divergence alerts."""
    return os.environ.get("SOURCE_DIVERGENCE_ALERTS_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _threshold_pct() -> float:
    """Env-tunable divergence threshold in percent (default 0.5)."""
    raw = os.environ.get("SOURCE_DIVERGENCE_THRESHOLD_PCT")
    if raw is None or not raw.strip():
        return _DEFAULT_THRESHOLD_PCT
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD_PCT


def _today_ist() -> date:
    return datetime.now(_IST).date()


def reset_dedup_for_tests() -> None:
    """Wipe the in-process dedup table (test helper, NOT for production use)."""
    global _alert_dedup_day
    with _alert_dedup_lock:
        _alert_dedup.clear()
        _alert_dedup_day = None


def _divergence_pct(a: float, b: float) -> float:
    """Return ``abs(a-b) / max(|a|,|b|, 1e-9) * 100`` — symmetric, NaN-safe."""
    try:
        denom = max(abs(float(a)), abs(float(b)), 1e-9)
        return abs(float(a) - float(b)) / denom * 100.0
    except (TypeError, ValueError):
        return 0.0


def check_and_alert(
    *,
    service: str,
    symbol: str,
    source_a_label: str,
    source_a_value: float,
    source_b_label: str,
    source_b_value: float,
    day_ist: date | None = None,
) -> bool:
    """Alert once-per-(service, symbol, day) on a source divergence.

    Args:
        service: short identifier of the calling service (used in dedup key +
            log line + Telegram message). Examples: ``aggregator_seeder``,
            ``eod_reconciliation``, ``scanner_rule``.
        symbol: market symbol the two sources disagree on. Used in the dedup
            key so distinct symbols get distinct alerts.
        source_a_label / source_a_value: first source's name + numeric value.
        source_b_label / source_b_value: second source's name + numeric value.
        day_ist: IST date for the dedup key. Defaults to today IST. Passing
            an explicit value lets tests pin the rollover behaviour.

    Returns:
        ``True`` iff a fresh alert was emitted (flag on, divergence above
        threshold, not yet alerted for this (service, symbol, day) tuple).
        ``False`` otherwise — including when the helper short-circuited on
        the flag, threshold, or dedup. Never raises.
    """
    try:
        if not _flag_enabled():
            return False

        threshold = _threshold_pct()
        div_pct = _divergence_pct(source_a_value, source_b_value)
        if div_pct <= threshold:
            return False

        if day_ist is None:
            day_ist = _today_ist()

        dedup_key = (service, symbol, day_ist.isoformat())

        global _alert_dedup_day
        with _alert_dedup_lock:
            # IST date rollover — wipe stale state so the next day starts fresh.
            if _alert_dedup_day != day_ist:
                _alert_dedup.clear()
                _alert_dedup_day = day_ist
            if dedup_key in _alert_dedup:
                # Already alerted today for this (service, symbol).
                return False
            _alert_dedup[dedup_key] = True

        # ------------------------------------------------------------------ #
        # Emit. Fail-safe — neither the log nor the notify may raise back.
        # ------------------------------------------------------------------ #
        msg = (
            f"source divergence: service={service} symbol={symbol} "
            f"{source_a_label}={source_a_value!r} {source_b_label}={source_b_value!r} "
            f"divergence={div_pct:.2f}% (>threshold={threshold:.2f}%)"
        )
        try:
            logger.warning(msg)
        except Exception:  # noqa: BLE001 — fail-safe by design
            pass

        try:
            from services.notification_service import get_notification_service

            tg_text = (
                f"⚠️ *Source divergence*\n"
                f"├ Service: `{service}`\n"
                f"├ Symbol: `{symbol}`\n"
                f"├ {source_a_label}: `{source_a_value}`\n"
                f"├ {source_b_label}: `{source_b_value}`\n"
                f"└ Divergence: `{div_pct:.2f}%` (threshold `{threshold:.2f}%`)"
            )
            get_notification_service().notify(
                "source_divergence",
                tg_text,
                service=service,
                symbol=symbol,
                divergence_pct=div_pct,
            )
        except Exception:  # noqa: BLE001 — fail-safe by design
            logger.exception(
                "source_divergence_alerts: notify dispatch failed (service=%s symbol=%s)",
                service,
                symbol,
            )

        return True
    except Exception:  # noqa: BLE001 — fail-safe top-level guard
        logger.exception(
            "source_divergence_alerts.check_and_alert: unexpected failure (service=%s symbol=%s)",
            service,
            symbol,
        )
        return False
