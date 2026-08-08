"""Realized CAGR / Sharpe / Max-drawdown from a strategy's closed trades.

Issue #568. The strategies dashboard rendered a hardcoded ``—`` for these three
metrics in the Sandbox and Live columns because nothing ever computed them —
``_lifetime_from_pnls`` returned only cumulative P&L, closed-trade count and win
rate. This module is the missing computation.

Pure by design: it takes ``(date, pnl)`` pairs and a capital basis and returns
numbers. No DB session, no Flask, no clock — so the maths is unit-testable
without fixtures for ten tables, and the same function serves sandbox, live and
any future replay.

Three deliberate constraints, each of which exists to stop this module from
publishing a confident wrong number:

1. **Sharpe is scale-invariant; CAGR and Max-DD % are not.** Sharpe is
   ``mean(r)/std(r)`` — dividing every day by the same capital cancels, so it is
   honest regardless of what ``capital`` is. CAGR and drawdown-percent are ratios
   *of* capital and inherit whatever that basis means. R56 warns explicitly that
   the simplified engine's ₹20,000 is a **per-trade risk-sizing base, not a book
   that compounds**, and that reading its result as an account percentage is
   misleading (it prints a nonsense "-1258% ROI"). Callers get ``max_dd_inr``
   alongside ``max_dd_pct`` for exactly this reason, and
   ``capital_basis_is_notional`` flags the caveat through to the UI.

2. **Annualising a short window is extrapolation, not measurement.** 41 trading
   days of +₹8,220 on ₹20,000 compounds to a >700% "CAGR" — arithmetically
   correct and completely meaningless. CAGR is therefore withheld below
   ``MIN_TRADING_DAYS_CAGR`` and the caller is told *why* via ``notes`` rather
   than being handed a number it cannot interpret. The metric appears on its own
   once enough history accumulates.

3. **Insufficient data yields ``None``, never ``0.0``.** A zero Sharpe reads as
   "measured, and it's bad"; ``None`` reads as "not enough data yet". The
   dashboard renders the latter as ``—``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date

from utils.logging import get_logger

logger = get_logger(__name__)

#: NSE trading days per year — the annualisation factor for daily Sharpe.
TRADING_DAYS_PER_YEAR = 252

#: Minimum distinct trading days before each metric is reported.
#:
#: Sharpe is a mean/std ratio and is merely *noisy* on a short sample, so it
#: clears at 20 days. CAGR raises the window return to the power of
#: ``1/years`` — on a sub-quarter sample that amplifies noise into a headline
#: number, so it needs a quarter of trading history. Proposed for
#: ``docs/PARAMETER_LOG.md`` in the PR that introduces them (new tunables on a
#: feature branch are proposed, not committed direct-to-dev).
MIN_TRADING_DAYS_SHARPE = 20
MIN_TRADING_DAYS_CAGR = 60


def _empty(reason: str) -> dict:
    return {
        "cagr_pct": None,
        "sharpe": None,
        "max_dd_pct": None,
        "max_dd_inr": None,
        "roc_pct": None,
        "trading_days": 0,
        "capital_basis_inr": None,
        "capital_basis_is_notional": False,
        "notes": reason,
    }


def compute_realized_metrics(
    daily_pnl: Iterable[tuple[date, float]],
    *,
    capital_inr: float | None,
    capital_is_notional: bool = False,
) -> dict:
    """Compute realized performance metrics from a per-day P&L series.

    Args:
        daily_pnl: ``(trade_date, net_pnl)`` pairs. Duplicate dates are summed,
            so callers may pass one entry per trade; ordering is irrelevant.
        capital_inr: The capital basis for percentage metrics. ``None`` (or
            non-positive) suppresses CAGR / Max-DD % while still returning
            Sharpe and Max-DD in rupees, since those do not need it.
        capital_is_notional: Set when the basis is a per-trade risk-sizing
            number rather than a compounding book (the simplified engine's
            ₹20,000). Passed straight through so the UI can caveat the cell —
            this module does not refuse to compute, it refuses to *hide* that
            the denominator is soft.

    Returns:
        dict with ``cagr_pct``, ``sharpe``, ``max_dd_pct``, ``max_dd_inr``,
        ``roc_pct``, ``trading_days``, ``capital_basis_inr``,
        ``capital_basis_is_notional`` and ``notes``. Every metric is ``None``
        when it cannot be honestly computed.
    """
    try:
        by_day: dict[date, float] = {}
        for d, pnl in daily_pnl:
            if d is None or pnl is None:
                continue
            by_day[d] = by_day.get(d, 0.0) + float(pnl)
    except Exception:
        logger.exception("compute_realized_metrics: malformed daily_pnl series")
        return _empty("malformed input series")

    if not by_day:
        return _empty("no closed trades yet")

    series = [by_day[d] for d in sorted(by_day)]
    n_days = len(series)
    total = sum(series)

    out = _empty("")
    out["trading_days"] = n_days
    out["capital_basis_inr"] = capital_inr if (capital_inr or 0) > 0 else None
    out["capital_basis_is_notional"] = bool(capital_is_notional)

    # --- Max drawdown on the cumulative realized-P&L curve -------------------
    # Always meaningful in rupees. Peak starts at 0 so a strategy that is under
    # water from its first day reports that loss as drawdown, rather than
    # measuring from a peak it never reached.
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for v in series:
        cum += v
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    out["max_dd_inr"] = round(max_dd, 2)

    notes: list[str] = []

    # --- Sharpe (scale-invariant) -------------------------------------------
    if n_days < MIN_TRADING_DAYS_SHARPE:
        notes.append(f"Sharpe needs >={MIN_TRADING_DAYS_SHARPE} trading days ({n_days} so far)")
    else:
        mean = total / n_days
        var = sum((v - mean) ** 2 for v in series) / (n_days - 1)
        sd = math.sqrt(var)
        if sd <= 0:
            notes.append("Sharpe undefined (zero variance)")
        else:
            out["sharpe"] = round((mean / sd) * math.sqrt(TRADING_DAYS_PER_YEAR), 2)

    # --- Capital-relative metrics -------------------------------------------
    if not out["capital_basis_inr"]:
        notes.append("CAGR / Max DD % need a positive capital basis")
        out["notes"] = "; ".join(notes)
        return out

    cap = float(out["capital_basis_inr"])
    out["max_dd_pct"] = round(100.0 * max_dd / cap, 2)
    out["roc_pct"] = round(100.0 * total / cap, 2)

    if n_days < MIN_TRADING_DAYS_CAGR:
        notes.append(
            f"CAGR needs >={MIN_TRADING_DAYS_CAGR} trading days ({n_days} so far) - "
            "annualising a shorter window extrapolates noise"
        )
    else:
        ending = cap + total
        if ending <= 0:
            # A book wiped past zero has no real growth rate; -100% is the
            # honest floor and (ending/cap)**(1/years) would raise on a
            # negative base anyway.
            out["cagr_pct"] = -100.0
        else:
            years = n_days / TRADING_DAYS_PER_YEAR
            out["cagr_pct"] = round(100.0 * ((ending / cap) ** (1.0 / years) - 1.0), 2)

    if capital_is_notional:
        notes.append(
            "capital basis is a per-trade risk-sizing figure, not a compounding "
            "book - read %-of-capital metrics with care (R56)"
        )

    out["notes"] = "; ".join(notes)
    return out
