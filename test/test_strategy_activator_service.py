"""Tests for the Stage 1.7 strategy activator.

These tests pin down the contract:

* Default (``None``) ``regime_profile`` on a strategy returns
  ``(True, "no profile")`` regardless of regime.
* A profile that conflicts with the supplied regime returns
  ``(False, "<dim> <value> not in <set>")``.
* Past-EOD queries against intraday strategies return ``(False, "past eod")``
  *before* the regime check (so the EOD guard wins even if the regime
  would have matched).
* Unknown strategy names return ``(False, "unknown strategy")``.

All inputs are injected — we don't depend on the actual registered
strategies or any market data.
"""

from __future__ import annotations

from datetime import datetime

import pytest
import pytz

from services import strategy_activator_service as sas
from services.market_regime_service import MarketRegime
from strategies import register, registered_strategies
from strategies.base import BaseStrategy, RegimeProfile

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Test fixtures: synthetic strategies, registered into a temporary slot.
# ---------------------------------------------------------------------------


@pytest.fixture
def no_profile_strategy():
    """Register a placeholder strategy with no regime constraint and
    clean it back out after the test."""
    name = "_test_no_profile_strategy"
    _register_stub(name, profile=None, intraday=True, eod_exit_time="15:20")
    yield name
    _deregister(name)


@pytest.fixture
def bullish_only_strategy():
    name = "_test_bullish_only_strategy"
    _register_stub(
        name,
        profile=RegimeProfile.of(trend={"bullish"}),
        intraday=True,
        eod_exit_time="15:20",
    )
    yield name
    _deregister(name)


@pytest.fixture
def overnight_strategy():
    """Positional strategy — should never trip the EOD guard."""
    name = "_test_overnight_strategy"
    _register_stub(name, profile=None, intraday=False, eod_exit_time="15:20")
    yield name
    _deregister(name)


def _register_stub(name, *, profile, intraday, eod_exit_time):
    @register(name)
    class _Stub(BaseStrategy):  # noqa: D401
        pass

    _Stub.name = name
    _Stub.intraday = intraday
    _Stub.eod_exit_time = eod_exit_time
    _Stub.regime_profile = profile

    # Concretize abstract methods so instantiation would work if anyone
    # tried (the activator only inspects the *class*, but be defensive).
    for hook in (
        "on_scan_hit",
        "seed_history",
        "on_bar",
        "on_tick",
        "confirm_entry",
        "confirm_exit",
        "clear_pending_entry",
        "clear_pending_exit",
    ):
        setattr(_Stub, hook, lambda self, *a, **kw: None)


def _deregister(name):
    # The registry isn't exposed for mutation by design — poke the
    # private dict so tests don't pollute it.
    from strategies import _registry

    _registry.pop(name, None)


def _make_regime(**overrides):
    base = {
        "timestamp": IST.localize(datetime(2026, 6, 1, 10, 30)),
        "trend": "bullish",
        "volatility": "medium",
        "breadth": "wide",
        "sector_leaders": [],
        "sector_leader_concentration": 0.0,
        "time_of_day": "mid_morning",
        "raw_metrics": {},
    }
    base.update(overrides)
    return MarketRegime(**base)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unknown_strategy_returns_false_unknown(monkeypatch):
    allowed, reason = sas.is_strategy_active_now("does_not_exist")
    assert allowed is False
    assert reason == "unknown strategy"


def test_no_profile_strategy_passes_regardless_of_regime(no_profile_strategy):
    regime = _make_regime(trend="bearish", volatility="extreme")
    allowed, reason = sas.is_strategy_active_now(
        no_profile_strategy,
        now=IST.localize(datetime(2026, 6, 1, 10, 30)),
        regime=regime,
    )
    assert allowed is True
    assert reason == "no profile"


def test_profile_matching_regime_returns_true(bullish_only_strategy):
    regime = _make_regime(trend="bullish")
    allowed, reason = sas.is_strategy_active_now(
        bullish_only_strategy,
        now=IST.localize(datetime(2026, 6, 1, 10, 30)),
        regime=regime,
    )
    assert allowed is True
    assert reason == "profile matches regime"


def test_profile_conflict_returns_false_with_reason(bullish_only_strategy):
    regime = _make_regime(trend="bearish")
    allowed, reason = sas.is_strategy_active_now(
        bullish_only_strategy,
        now=IST.localize(datetime(2026, 6, 1, 10, 30)),
        regime=regime,
    )
    assert allowed is False
    assert "trend" in reason and "bearish" in reason


def test_past_eod_blocks_intraday_even_with_matching_regime(bullish_only_strategy):
    """Even though the regime matches, the EOD guard should fire first
    so intraday strategies don't open new positions after their cutoff."""
    regime = _make_regime(trend="bullish")
    allowed, reason = sas.is_strategy_active_now(
        bullish_only_strategy,
        now=IST.localize(datetime(2026, 6, 1, 15, 21)),  # past 15:20
        regime=regime,
    )
    assert allowed is False
    assert reason == "past eod"


def test_past_eod_does_not_block_overnight_strategy(overnight_strategy):
    """Positional strategies (``intraday=False``) skip the EOD guard."""
    allowed, reason = sas.is_strategy_active_now(
        overnight_strategy,
        now=IST.localize(datetime(2026, 6, 1, 23, 30)),
        regime=_make_regime(),
    )
    assert allowed is True
    assert reason == "no profile"


def test_missing_regime_when_profile_present_fails_closed(
    bullish_only_strategy, monkeypatch
):
    """If the classifier returns ``None`` (compute failed) we fail
    closed — a profiled strategy must not be entered against an
    unknown regime."""
    monkeypatch.setattr(
        "services.market_regime_service.get_cached_regime",
        lambda max_age_minutes=5: None,
    )
    allowed, reason = sas.is_strategy_active_now(
        bullish_only_strategy,
        now=IST.localize(datetime(2026, 6, 1, 10, 30)),
        regime=None,
    )
    assert allowed is False
    assert reason == "no regime available"
