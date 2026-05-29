"""Tests for the Stage-0 daily-risk circuit breaker.

The breaker reads today's ``completed_trades`` ledger from the simplified
stock engine and trips when either:
  * the number of net-loss round trips reaches ``RISK_MAX_LOSSES_PER_DAY``,
  * the realized drawdown (signed sum of net P&L across trades) reaches
    ``RISK_MAX_DAILY_DRAWDOWN_PCT`` of ``RISK_CAPITAL_BASELINE``.

Each test rebuilds the engine in memory and rebinds the singleton accessor
so the breaker reads from our controlled ledger instead of whatever the
running app has.
"""

from __future__ import annotations

import datetime as dt

import pytest

from services.simplified_stock_engine_core import (
    CompletedTrade,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
)


@pytest.fixture
def breaker_env(monkeypatch):
    """Build an isolated engine and pin the singleton accessor to it.

    The breaker pulls trades from
    ``simplified_stock_engine_service.get_simplified_stock_engine_service()``,
    so we patch that accessor for the duration of the test.
    """
    engine = SimplifiedStockEngine(config=SimplifiedEngineConfig())

    class _FakeService:
        def __init__(self, eng: SimplifiedStockEngine) -> None:
            self.engine = eng

    fake_service = _FakeService(engine)
    monkeypatch.setattr(
        "services.simplified_stock_engine_service.get_simplified_stock_engine_service",
        lambda: fake_service,
    )

    # Reset env defaults so each test gets predictable thresholds.
    monkeypatch.setenv("RISK_MAX_LOSSES_PER_DAY", "3")
    monkeypatch.setenv("RISK_MAX_DAILY_DRAWDOWN_PCT", "3.0")
    monkeypatch.setenv("RISK_CAPITAL_BASELINE", "100000")

    return engine


def _planted_trade(
    *,
    symbol: str,
    qty: int,
    entry_price: float,
    exit_price: float,
    reason: str = "stop_loss",
) -> CompletedTrade:
    """Build a CompletedTrade with deterministic entry/exit times."""
    return CompletedTrade(
        symbol=symbol,
        qty=qty,
        entry_price=entry_price,
        exit_price=exit_price,
        entry_time=dt.datetime(2026, 5, 29, 10, 0),
        exit_time=dt.datetime(2026, 5, 29, 10, 5),
        exit_reason=reason,
    )


# ---------------------------------------------------------------------------
# Loss-count gate
# ---------------------------------------------------------------------------


def test_no_trades_breaker_clear(breaker_env):
    from services.risk_service import daily_circuit_breaker_tripped

    tripped, reason = daily_circuit_breaker_tripped()
    assert tripped is False
    assert reason == ""


def test_two_losses_under_limit_breaker_clear(breaker_env):
    """Two losing trades, threshold is 3 — gate stays open."""
    engine = breaker_env
    # Long that lost: bought 10@100, sold 10@99 -> gross = -10.
    for sym in ("AAA", "BBB"):
        engine.completed_trades.append(
            _planted_trade(symbol=sym, qty=10, entry_price=100.0, exit_price=99.0)
        )

    from services.risk_service import daily_circuit_breaker_tripped

    tripped, _ = daily_circuit_breaker_tripped()
    assert tripped is False


def test_three_losses_trips_loss_count_gate(breaker_env):
    """Three losing trades hits the default limit."""
    engine = breaker_env
    for sym in ("AAA", "BBB", "CCC"):
        engine.completed_trades.append(
            _planted_trade(symbol=sym, qty=10, entry_price=100.0, exit_price=99.0)
        )

    from services.risk_service import daily_circuit_breaker_tripped

    tripped, reason = daily_circuit_breaker_tripped()
    assert tripped is True
    assert "3 losses today" in reason
    assert "max 3" in reason


# ---------------------------------------------------------------------------
# Drawdown gate
# ---------------------------------------------------------------------------


def test_drawdown_below_limit_breaker_clear(breaker_env):
    """Loss = 2.9% of baseline — under the 3.0% trip threshold."""
    engine = breaker_env
    # Long: bought 100@100=10000, sold 100@71.05=7105 → gross = -2895.
    # Charges are ~few rupees so net ~-2900 ≈ 2.9% of 100k.
    engine.completed_trades.append(
        _planted_trade(symbol="AAA", qty=100, entry_price=100.0, exit_price=71.05)
    )

    from services.risk_service import daily_circuit_breaker_tripped

    tripped, reason = daily_circuit_breaker_tripped()
    assert tripped is False, f"unexpected trip with reason={reason!r}"


def test_drawdown_above_limit_trips_drawdown_gate(breaker_env, monkeypatch):
    """Loss = 3.1% of baseline → tripped on drawdown, not on count."""
    engine = breaker_env
    # Raise the loss-count limit so only the drawdown gate can fire.
    monkeypatch.setenv("RISK_MAX_LOSSES_PER_DAY", "10")

    # Long: bought 100@100=10000, sold 100@69@= 6900 → gross = -3100 (~3.1%).
    engine.completed_trades.append(
        _planted_trade(symbol="AAA", qty=100, entry_price=100.0, exit_price=69.0)
    )

    from services.risk_service import daily_circuit_breaker_tripped

    tripped, reason = daily_circuit_breaker_tripped()
    assert tripped is True
    assert "drawdown" in reason
    assert "%" in reason


# ---------------------------------------------------------------------------
# Fail-safe semantics
# ---------------------------------------------------------------------------


def test_engine_unreachable_fails_open(monkeypatch):
    """A read failure must not block trading."""

    def _boom():
        raise RuntimeError("engine accessor exploded")

    monkeypatch.setattr(
        "services.simplified_stock_engine_service.get_simplified_stock_engine_service",
        _boom,
    )

    from services.risk_service import daily_circuit_breaker_tripped

    tripped, reason = daily_circuit_breaker_tripped()
    assert tripped is False
    assert reason == ""
