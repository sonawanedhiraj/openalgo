"""Daily-risk gates for the simplified stock engine.

Stage-0 operational floor: after N realized losses in one day, or once
intraday drawdown exceeds M% of the configured capital baseline, refuse new
entries until the next trading day. This is independent of the per-symbol
cooldown / same-day stop-out block — those are symbol-scoped; this gate is
account-scoped.

Data source: the engine's in-memory ``completed_trades`` ledger. It is the
canonical, real-time record of today's round trips (entry+exit price, qty,
reason). The ledger is cleared automatically on day rollover, so reading it
gives "today's" P&L without any date filtering on our side.

Fail-safe semantics: if the engine is unavailable or any metric read raises,
``daily_circuit_breaker_tripped`` returns ``(False, "")``. We will not block
trading because a metric read failed — that's the operator's risk preference,
documented in the original task brief.
"""

from __future__ import annotations

import os

from services.simplified_stock_engine_core import compute_zerodha_intraday_charges
from utils.logging import get_logger

logger = get_logger(__name__)


DEFAULT_MAX_LOSSES_PER_DAY = 3
DEFAULT_MAX_DAILY_DRAWDOWN_PCT = 3.0
DEFAULT_CAPITAL_BASELINE = 100_000.0


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _compute_metrics(trades: list) -> tuple[int, float]:
    """Returns ``(loss_count, net_realized_pnl)`` for the given trades.

    - ``loss_count``: trades whose net P&L (gross minus approximate Zerodha
      intraday charges) is strictly negative. Matches the operator's
      intuition for "losers."
    - ``net_realized_pnl``: signed sum of (gross - charges) across all trades.
      Negative = drawdown; positive = realized profit.

    Uses :func:`compute_zerodha_intraday_charges` so the numbers stay
    aligned with the EOD trading summary that the engine logs.
    """
    loss_count = 0
    net_total = 0.0
    for trade in trades:
        try:
            charges = compute_zerodha_intraday_charges(trade.buy_value, trade.sell_value)
            net = trade.gross_pnl - charges.total
        except Exception:
            logger.exception("[RISK] could not compute net P&L for trade %r", trade)
            continue
        net_total += net
        if net < 0:
            loss_count += 1
    return loss_count, net_total


def daily_circuit_breaker_tripped() -> tuple[bool, str]:
    """Has today's trading state hit the loss-count or drawdown limit?

    Returns ``(tripped, reason)``. ``tripped=True`` means new opening
    entries must be refused. ``reason`` is a short human-readable string
    suitable for log lines and preflight responses.

    Configurable via ``.env``:

    - ``RISK_MAX_LOSSES_PER_DAY`` (default 3): trip when net losses ≥ this.
    - ``RISK_MAX_DAILY_DRAWDOWN_PCT`` (default 3.0): trip when realized
      drawdown ≥ this fraction of ``RISK_CAPITAL_BASELINE``.
    - ``RISK_CAPITAL_BASELINE`` (default 100000): denominator for the % calc.

    Fail-safe: any error reading metrics returns ``(False, "")`` so a
    transient data-fetch failure cannot itself block trading. We log the
    exception so operators can investigate.
    """
    max_losses = _env_int("RISK_MAX_LOSSES_PER_DAY", DEFAULT_MAX_LOSSES_PER_DAY)
    max_dd_pct = _env_float("RISK_MAX_DAILY_DRAWDOWN_PCT", DEFAULT_MAX_DAILY_DRAWDOWN_PCT)
    baseline = _env_float("RISK_CAPITAL_BASELINE", DEFAULT_CAPITAL_BASELINE)

    try:
        from services.simplified_stock_engine_service import (
            get_simplified_stock_engine_service,
        )

        service = get_simplified_stock_engine_service()
        trades = list(service.engine.completed_trades)
    except Exception:
        logger.exception("[RISK] could not read completed_trades; failing open")
        return False, ""

    try:
        loss_count, net_pnl = _compute_metrics(trades)
    except Exception:
        logger.exception("[RISK] could not compute metrics; failing open")
        return False, ""

    if loss_count >= max_losses:
        return (
            True,
            f"daily limits: {loss_count} losses today (max {max_losses})",
        )

    if baseline > 0 and net_pnl < 0:
        dd_pct = abs(net_pnl) / baseline * 100.0
        if dd_pct >= max_dd_pct:
            return (
                True,
                (
                    f"daily limits: realized drawdown {dd_pct:.2f}% "
                    f"(loss=Rs{abs(net_pnl):.2f}, baseline=Rs{baseline:.2f}, "
                    f"limit {max_dd_pct:.2f}%)"
                ),
            )

    return False, ""
