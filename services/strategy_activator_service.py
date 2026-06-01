"""Stage 1.7 strategy activator.

Decides whether a registered strategy is allowed to take new entries
right now, given:

* its declared :class:`~strategies.base.RegimeProfile` matched against
  the current :class:`~services.market_regime_service.MarketRegime`;
* the EOD guard (intraday strategies are not allowed to open new
  positions after their declared ``eod_exit_time``).

The activator is **passive scaffolding** in this commit — no production
code calls it yet. The intended opt-in is a single line at the engine
entry-decision site:

    from services.strategy_activator_service import is_strategy_active_now
    allowed, reason = is_strategy_active_now(strategy.name)
    if not allowed:
        return  # log + skip

Strategies that haven't set a ``regime_profile`` (the default ``None``)
always pass the regime check — opt-in is per-strategy.
"""

from __future__ import annotations

from datetime import datetime, time

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def _parse_hhmm(value: str | None) -> tuple[int, int] | None:
    """Parse ``"HH:MM"`` to ``(hh, mm)``; ``None`` on any failure."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def is_strategy_active_now(
    strategy_name: str,
    *,
    now: datetime | None = None,
    regime=None,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for the named strategy.

    Parameters
    ----------
    strategy_name:
        Registry key under :mod:`strategies`. Unknown names short-circuit
        to ``(False, "unknown strategy")``.
    now:
        Optional IST datetime override for tests.
    regime:
        Optional :class:`MarketRegime` override for tests. When omitted,
        the current cached regime is used (recomputed if stale).

    The function never raises — any unexpected exception is logged and
    surfaced as ``(False, "activator error: …")`` so the caller can fall
    back to safe behavior.
    """
    try:
        from strategies import get_strategy

        strategy_cls = get_strategy(strategy_name)
        if strategy_cls is None:
            return False, "unknown strategy"

        ts_ist = now if now is not None else datetime.now(IST)
        if ts_ist.tzinfo is None:
            ts_ist = IST.localize(ts_ist)

        # 1. EOD guard for intraday strategies — refuse new entries
        # after eod_exit_time. Positional / overnight strategies (
        # ``intraday=False``) skip this check entirely.
        if getattr(strategy_cls, "intraday", True):
            cutoff = _parse_hhmm(getattr(strategy_cls, "eod_exit_time", None))
            if cutoff is not None:
                cutoff_t = time(cutoff[0], cutoff[1])
                if ts_ist.time() >= cutoff_t:
                    return False, "past eod"

        # 2. Regime-profile gate. ``None`` profile matches everything.
        profile = getattr(strategy_cls, "regime_profile", None)
        if profile is None:
            return True, "no profile"

        if regime is None:
            from services.market_regime_service import get_cached_regime

            regime = get_cached_regime(max_age_minutes=5)
        if regime is None:
            # The classifier failed and we have nothing to compare
            # against. Fail-closed: don't pretend the profile matched.
            return False, "no regime available"

        if regime.matches(profile):
            return True, "profile matches regime"
        return False, _explain_mismatch(profile, regime)
    except Exception as exc:
        logger.exception("activator failed for %s: %s", strategy_name, exc)
        return False, f"activator error: {exc}"


def _explain_mismatch(profile, regime) -> str:
    """Produce a short, log-friendly reason string for a profile/regime
    mismatch. Lists the first failing dimension; that's almost always
    the most useful piece of debug info for a one-line log."""
    if profile.trend is not None and regime.trend not in profile.trend:
        return f"trend {regime.trend!r} not in {sorted(profile.trend)!r}"
    if profile.volatility is not None and regime.volatility not in profile.volatility:
        return f"volatility {regime.volatility!r} not in {sorted(profile.volatility)!r}"
    if profile.breadth is not None and regime.breadth not in profile.breadth:
        return f"breadth {regime.breadth!r} not in {sorted(profile.breadth)!r}"
    if (
        profile.time_of_day is not None
        and regime.time_of_day not in profile.time_of_day
    ):
        return (
            f"time_of_day {regime.time_of_day!r} not in {sorted(profile.time_of_day)!r}"
        )
    return "profile mismatch"
