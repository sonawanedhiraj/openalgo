"""Scanner reference-data contract — broker prev-close registry + certificate (issue #305).

Why this module exists
----------------------
On 2026-07-02 DELHIVERY fired BUY 42x while trading BELOW its real prior
close: the rule's ``yest_d.close`` reference (475.4, a stale historify-D
slot frozen at the 06-30 value) diverged 6.8% from the real 07-01 settled
close (~510.0) that the broker already knew. The system detected the
divergence 691 times the prior day (``aggregator_seeder`` logged
``historify_last_close=475.4 broker_last_close=510.0 divergence=6.78%``)
and threw the broker value away — every guard was alert-only.

This module makes the broker's knowledge *enforceable*:

1. **Registry** — ``record_broker_prev_close`` captures the T-1 settled
   close per symbol from the broker bars the ``scanner_aggregator_seeder``
   ALREADY fetches at boot (no new broker API load). Entries are
   **day-scoped by recording date**: a value recorded at 08:40 today is
   today's correct T-1 reference; a value recorded yesterday morning was
   *yesterday's* T-1 and must never masquerade as today's — so
   ``get_broker_prev_close`` serves only same-IST-day recordings.
2. **Certificate** — ``compute_reference_certificate`` derives the settled
   reference close the same way the rules do (the shared
   ``services.scan_rules._today_running.derive_today_and_yest`` helper) and
   cross-checks it against the recorded broker prev-close. The verdict is
   passed to the rules via the indicators dict
   (``reference_certified`` / ``reference_divergence_pct`` / value keys) so
   the rules stay pure — they read a pre-computed verdict, never re-derive.

Failure semantics (load-bearing):

* divergence > ``SCANNER_REFERENCE_DIVERGENCE_MAX_PCT`` (default 1.0) →
  **NOT certified** (fail-closed on a confirmed divergence).
* no broker prev-close recorded today → **certified** (fail-open on a
  missing cross-check) + a dedup'd once-per-(symbol, day) WARNING so the
  coverage gap is visible in ``errors.jsonl``.
* ``SCANNER_REFERENCE_CHECK_ENABLED=false`` → the whole check is skipped
  (no keys added to the indicators dict; rules treat a MISSING key as
  certified for backward compatibility).
* Observability code must never raise into rule evaluation — every path is
  wrapped; an internal error yields a certified (fail-open) verdict with a
  ``logger.exception`` trace.
"""

from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta, timezone

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

_DEFAULT_DIVERGENCE_MAX_PCT = 1.0

# symbol -> (prev_close, as_of datetime IST, recorded IST date). In-process
# only — resets on restart, which is correct: the boot seeder re-records on
# every start, and a stale pre-restart value must not survive into a new day.
_registry: dict[str, tuple[float, datetime, date]] = {}
_registry_lock = threading.Lock()

# Once-per-(symbol, IST-day) dedup for the missing-broker-prev-close WARNING
# (the fail-open path). Mirrors the rules' _warn_shallow_daily_once pattern.
_missing_warned: set[tuple[str, str]] = set()
_missing_warned_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Env flags (proposed PARAMETER_LOG entries in the PR body — issue #305)
# --------------------------------------------------------------------------- #


def reference_check_enabled() -> bool:
    """``SCANNER_REFERENCE_CHECK_ENABLED`` env flag (default true). When false
    the certificate is not computed at all (no keys added to the indicators
    dict — the rules' missing-key-is-certified backward compat kicks in)."""
    return os.environ.get("SCANNER_REFERENCE_CHECK_ENABLED", "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def reference_divergence_max_pct() -> float:
    """``SCANNER_REFERENCE_DIVERGENCE_MAX_PCT`` env knob (default 1.0) — the
    maximum settled-reference vs broker-prev-close divergence, in percent,
    before the reference is declared NOT certified."""
    try:
        return float(
            os.environ.get("SCANNER_REFERENCE_DIVERGENCE_MAX_PCT", str(_DEFAULT_DIVERGENCE_MAX_PCT))
        )
    except (TypeError, ValueError):
        return _DEFAULT_DIVERGENCE_MAX_PCT


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def record_broker_prev_close(symbol: str, close: float, as_of: datetime | None = None) -> None:
    """Record ``symbol``'s broker-known T-1 settled close.

    ``as_of`` defaults to now (IST). The entry is keyed by the *recording*
    IST date — only same-day recordings are served back (see module
    docstring for the day-scoping semantics). Never raises.
    """
    try:
        value = float(close)
    except (TypeError, ValueError):
        logger.warning(
            "scanner_reference_data: unparseable prev-close %r for %s — not recorded",
            close,
            symbol,
        )
        return
    if as_of is None:
        as_of = datetime.now(_IST)
    recorded_day = as_of.astimezone(_IST).date() if as_of.tzinfo else as_of.date()
    with _registry_lock:
        _registry[symbol] = (value, as_of, recorded_day)
    logger.debug(
        "scanner_reference_data: recorded broker prev-close %s=%.2f (as_of=%s)",
        symbol,
        value,
        as_of,
    )


def get_broker_prev_close(symbol: str, today: date | None = None) -> tuple[float, datetime] | None:
    """Return ``(prev_close, as_of)`` for ``symbol`` — but ONLY when the value
    was recorded today (IST). A recording from a prior day was that day's
    T-1 close, not today's, and is ignored (fail-open at the caller)."""
    if today is None:
        today = datetime.now(_IST).date()
    with _registry_lock:
        entry = _registry.get(symbol)
    if entry is None:
        return None
    value, as_of, recorded_day = entry
    if recorded_day != today:
        return None
    return value, as_of


def record_prev_close_from_bars(symbol: str, bars: list[dict], today: date | None = None) -> bool:
    """Derive + record the T-1 settled close from a broker 1m-bar series.

    ``bars`` is the ``[{ts, open, high, low, close, volume}, ...]`` list the
    aggregator_seeder already fetched (``ts`` is a naive IST datetime,
    ascending). The T-1 close is the close of the LAST bar dated strictly
    before ``today`` — correct for both a pre-open boot (every bar is < today)
    and a mid-session restart (today's running bars are skipped; the last
    yesterday bar is the settled close). Returns True iff a value was
    recorded. Never raises.
    """
    try:
        if today is None:
            today = datetime.now(_IST).date()
        prev_close = None
        for b in bars:
            ts = b.get("ts")
            if ts is None:
                continue
            try:
                bar_day = ts.date()
            except AttributeError:
                continue
            if bar_day < today and b.get("close") is not None:
                prev_close = b["close"]
        if prev_close is None:
            logger.debug(
                "scanner_reference_data: no pre-%s bar in broker series for %s — "
                "prev-close not recorded",
                today,
                symbol,
            )
            return False
        record_broker_prev_close(symbol, float(prev_close))
        return True
    except Exception:  # noqa: BLE001 — observability must never break the seed path
        logger.exception(
            "scanner_reference_data: record_prev_close_from_bars failed for %s", symbol
        )
        return False


def reset_for_tests() -> None:
    """Wipe the registry + warning dedup (test helper, NOT for production)."""
    with _registry_lock:
        _registry.clear()
    with _missing_warned_lock:
        _missing_warned.clear()


# --------------------------------------------------------------------------- #
# Certificate
# --------------------------------------------------------------------------- #


def _warn_missing_once(symbol: str, day_iso: str) -> None:
    """Dedup'd once-per-(symbol, IST-day) WARNING for the fail-open path
    (settled reference exists but there is no broker prev-close to check it
    against — e.g. the seeder's broker fallback never fired for this symbol)."""
    key = (symbol, day_iso)
    with _missing_warned_lock:
        if key in _missing_warned:
            return
        _missing_warned.add(key)
    logger.warning(
        "scanner_reference_data %s: no broker prev-close recorded today — settled "
        "reference close CANNOT be cross-checked (fail-open: treated as certified). "
        "The aggregator_seeder's broker fallback did not fetch this symbol at boot.",
        symbol,
    )


def compute_reference_certificate(
    symbol: str,
    bars_5m,
    bars_daily,
    exchange: str | None = None,
    now_ist: datetime | None = None,
) -> dict:
    """Validate the settled daily reference close against the recorded broker
    prev-close; return the verdict keys for the indicators dict.

    Returns ``{}`` when the check is disabled (rules treat a missing key as
    certified). Otherwise returns::

        {
            "reference_certified": bool,
            "reference_divergence_pct": float | None,
            "reference_settled_close": float | None,
            "reference_broker_prev_close": float | None,
        }

    The settled reference is derived exactly the way the rules derive it
    (``derive_today_and_yest`` → ``yest_d.close``) so the certificate
    validates the very value the gates will compare against. Never raises —
    any internal failure yields a certified (fail-open) verdict.
    """
    if not reference_check_enabled():
        return {}

    verdict: dict = {
        "reference_certified": True,
        "reference_divergence_pct": None,
        "reference_settled_close": None,
        "reference_broker_prev_close": None,
    }
    try:
        # Index symbols are never evaluated by the F&O rules (issue #158 D2) —
        # skip quietly so they cannot generate missing-cross-check warnings.
        if exchange == "NSE_INDEX":
            return verdict

        if now_ist is None:
            now_ist = datetime.now(_IST)

        # Derive the settled reference the same way the rules do. When it
        # cannot be derived, the rules will loudly reject on their own
        # missing-derivation path — nothing for the certificate to validate.
        if bars_daily is None or len(bars_daily) < 2:
            return verdict
        import pandas as pd  # noqa: PLC0415 — keep module import light

        from services.scan_rules._today_running import derive_today_and_yest  # noqa: PLC0415

        _today_d, yest_d, _yest_idx = derive_today_and_yest(bars_daily, bars_5m, now_ist)
        if yest_d is None:
            return verdict
        settled = yest_d.get("close") if hasattr(yest_d, "get") else yest_d["close"]
        if settled is None or pd.isna(settled):
            return verdict
        settled = float(settled)
        verdict["reference_settled_close"] = settled

        today = now_ist.astimezone(_IST).date() if now_ist.tzinfo else now_ist.date()
        broker_entry = get_broker_prev_close(symbol, today=today)
        if broker_entry is None:
            # Fail-open on a missing cross-check — but say so, once per day.
            _warn_missing_once(symbol, today.isoformat())
            return verdict
        broker_close, _as_of = broker_entry
        verdict["reference_broker_prev_close"] = float(broker_close)

        div_pct = abs(settled - broker_close) / max(abs(broker_close), 1e-9) * 100.0
        verdict["reference_divergence_pct"] = round(div_pct, 4)
        if div_pct > reference_divergence_max_pct():
            verdict["reference_certified"] = False
        return verdict
    except Exception:  # noqa: BLE001 — the certificate must never break rule evaluation
        logger.exception(
            "scanner_reference_data: certificate computation failed for %s — fail-open",
            symbol,
        )
        verdict["reference_certified"] = True
        return verdict
