"""Unit tests for the three-tier parameter resolution in the Chartink BUY / SELL rules.

Each rule's ``_evaluate`` now reads tunables from:
  1. ``indicators["parameters"]`` dict (per-definition override, Chunk C)
  2. env var (existing env-level override)
  3. hardcoded default

These tests verify that path with synthetic DataFrames. To isolate gate 1
(gap_pct) cleanly without needing a passing Supertrend/RSI/volume frame, the
tests:
  a) Use a "control" path that confirms default-params logic is unchanged.
  b) Use a "custom params" path that changes the threshold and confirms the
     gate flips from False→True (or True→False) accordingly.

The Supertrend and RSI gates are bypassed by monkeypatching the underlying
indicator functions to return controlled DataFrames so the test doesn't
depend on building 200-row history that satisfies every gate simultaneously.
"""

from __future__ import annotations

import datetime
from unittest import mock

import numpy as np
import pandas as pd
import pytest
import pytz

import services.scan_rules.fno_intraday_buy_chartink as buy_rule_mod
import services.scan_rules.fno_intraday_sell_chartink as sell_rule_mod

_IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Synthetic frame builders
# ---------------------------------------------------------------------------


def _make_ohlcv(n: int, close: float, vol: float, high_offset: float = 1.0) -> pd.DataFrame:
    """Build a uniform OHLCV frame with ``n`` rows."""
    return pd.DataFrame(
        {
            "open": [close * 0.999] * n,
            "high": [close + high_offset] * n,
            "low": [close - high_offset] * n,
            "close": [close] * n,
            "volume": [vol] * n,
        }
    )


def _make_daily_buy(
    n: int = 210,
    today_close: float = 520.0,
    yest_close: float = 500.0,
    yest_high: float = 505.0,
    yest_low: float = 490.0,
    today_open: float = 505.0,
    today_vol: float = 200_000.0,
    avg_vol: float = 100_000.0,
) -> pd.DataFrame:
    """Build a daily frame that passes all BUY daily gates with default parameters.

    Layout: n-2 filler rows + yest row + today row.
    today_close=520 vs yest_close=500 → 4% gap → passes default 3% gate.
    today_open=505 > yest_close=500 → passes gate 9.
    today_open=505 > pivot=(505+490+500)/3=498.33 → passes gate 10.
    today_vol=200_000 > avg_vol=100_000 → passes SMA(50) and SMA(200) gates.
    today_close=520 > price_min=100 → passes gate 6.
    today_close=520 < price_max=5000 → passes gate 12.
    """
    filler = _make_ohlcv(n - 2, close=yest_close, vol=avg_vol)
    yest = pd.DataFrame(
        {
            "open": [yest_close * 0.999],
            "high": [yest_high],
            "low": [yest_low],
            "close": [yest_close],
            "volume": [avg_vol],
        }
    )
    today = pd.DataFrame(
        {
            "open": [today_open],
            "high": [today_close + 2.0],
            "low": [today_close - 2.0],
            "close": [today_close],
            "volume": [today_vol],
        }
    )
    return pd.concat([filler, yest, today], ignore_index=True)


def _make_weekly_pass(n: int = 24, close: float = 520.0) -> pd.DataFrame:
    """Build a weekly frame whose ATR(21) easily exceeds 5% of daily close."""
    # Use a wide high-low range so ATR is large (>>5% of close).
    return pd.DataFrame(
        {
            "open": [close * 0.95] * n,
            "high": [close * 1.15] * n,  # 15% above close → ATR >> 5%
            "low": [close * 0.85] * n,
            "close": [close] * n,
            "volume": [1_000_000.0] * n,
        }
    )


def _make_5m_pass(n: int = 15, close: float = 520.0, surge_vol: float = 30_000.0) -> pd.DataFrame:
    """Build a 5m frame where the last bar volume is 3× the SMA(10) → passes gate 13."""
    avg_vol = surge_vol / 3.0
    df = pd.DataFrame(
        {
            "open": [close * 0.999] * n,
            "high": [close + 0.5] * n,
            "low": [close - 0.5] * n,
            "close": [close] * n,
            "volume": [avg_vol] * n,
        }
    )
    df.loc[df.index[-1], "volume"] = surge_vol
    return df


def _make_15m_pass(n: int = 20, close: float = 520.0) -> pd.DataFrame:
    """Build a 15m frame that produces RSI(14) clearly above 50 (rising prices)."""
    closes = [close - (n - i) * 0.1 for i in range(n)]  # slightly rising
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [10_000.0] * n,
        }
    )


def _make_15m_weak(n: int = 20, close: float = 520.0) -> pd.DataFrame:
    """Build a 15m frame that produces RSI(14) clearly below 50 (falling prices)."""
    closes = [close + (n - i) * 0.1 for i in range(n)]  # slightly falling
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [10_000.0] * n,
        }
    )


def _st_pass_buy(daily_close: float = 520.0, yest_close: float = 500.0) -> pd.DataFrame:
    """Return a fake Supertrend DataFrame that passes BUY gates 3 + 4.

    Gate 3: st_now < daily_close → use daily_close - 10
    Gate 4: st_prev >= yest_close → use yest_close + 5
    """
    return pd.DataFrame(
        {
            "line": [yest_close + 5.0, daily_close - 10.0],
            "direction": [1, 1],
            "long_band": [np.nan, np.nan],
            "short_band": [np.nan, np.nan],
        }
    )


def _st_pass_sell(daily_close: float = 520.0, yest_close: float = 500.0) -> pd.DataFrame:
    """Return a fake Supertrend DataFrame that passes SELL gates 3 + 4.

    Gate 3: st_now > daily_close → use daily_close + 10
    Gate 4: st_prev <= yest_close → use yest_close - 5
    """
    return pd.DataFrame(
        {
            "line": [yest_close - 5.0, daily_close + 10.0],
            "direction": [-1, -1],
            "long_band": [np.nan, np.nan],
            "short_band": [np.nan, np.nan],
        }
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _freeze_ist_post_settle(monkeypatch):
    """Freeze the rule's internal clock to after 15:31 IST so today_idx=-1/yest_idx=-2.

    This keeps the warm-up check simple: daily frame needs len>=2 plus the two
    date-check rows. Pre-settle would add complexity (need len>=3 for yest_idx=-3).
    Also disables the D-bar-date verify gate (no timestamp column in synthetic frames,
    but belt-and-suspenders).

    Clears the gap-pct env vars so the tests are not affected by a running live app
    that may have set them to non-default values.
    """
    frozen = datetime.datetime(2026, 5, 30, 16, 0, tzinfo=_IST)
    monkeypatch.setattr(buy_rule_mod, "datetime", mock.MagicMock(now=lambda tz: frozen))
    monkeypatch.setattr(sell_rule_mod, "datetime", mock.MagicMock(now=lambda tz: frozen))
    monkeypatch.setenv("SCANNER_DBAR_DATE_VERIFY_ENABLED", "false")
    # Clear the gap-pct env vars so the hardcoded defaults (3.0) apply.
    # A running live app may have these set to different values.
    monkeypatch.delenv("CHARTINK_RULE_BUY_GAP_PCT", raising=False)
    monkeypatch.delenv("CHARTINK_RULE_SELL_GAP_PCT", raising=False)


# ---------------------------------------------------------------------------
# BUY rule tests
# ---------------------------------------------------------------------------


def test_buy_rule_default_params_no_parameters_key(monkeypatch):
    """Call with indicators={} (no 'parameters' key) — default thresholds apply."""
    today_close = 520.0
    yest_close = 500.0  # 4% gap → passes default 3% threshold

    monkeypatch.setattr(buy_rule_mod, "supertrend", lambda bars, period, multiplier: _st_pass_buy())

    indicators = {
        "bars_daily": _make_daily_buy(today_close=today_close, yest_close=yest_close),
        "bars_weekly": _make_weekly_pass(),
        "bars_5m": _make_5m_pass(),
        "bars_15m": _make_15m_pass(),
        # No "parameters" key — exercises the p={} default path
    }
    result = buy_rule_mod._evaluate(pd.DataFrame(), indicators)
    assert result is True


def test_buy_rule_custom_gap_pct_changes_gate1(monkeypatch):
    """A close exactly 2.5% above prev_close:
    - default gap_pct=3.0 → gate 1 FAILS → False
    - custom gap_pct=2.0  → gate 1 PASSES → True (everything else passes too)
    """
    today_close = 512.5  # 500 × 1.025 = 512.5 → exactly 2.5% gap
    yest_close = 500.0

    monkeypatch.setattr(
        buy_rule_mod,
        "supertrend",
        lambda bars, period, multiplier: _st_pass_buy(
            daily_close=today_close, yest_close=yest_close
        ),
    )

    daily = _make_daily_buy(
        today_close=today_close, yest_close=yest_close, today_open=yest_close + 1.0
    )  # open > yest_close

    base_indicators = {
        "bars_daily": daily,
        "bars_weekly": _make_weekly_pass(close=today_close),
        "bars_5m": _make_5m_pass(close=today_close),
        "bars_15m": _make_15m_pass(close=today_close),
    }

    # Default: 3% threshold → 2.5% gap fails gate 1
    result_default = buy_rule_mod._evaluate(pd.DataFrame(), dict(base_indicators))
    assert result_default is False, "Expected gate1 to fail with default 3% threshold"

    # Custom: 2.0% threshold → 2.5% gap passes gate 1
    custom_indicators = dict(base_indicators)
    custom_indicators["parameters"] = {"gap_pct": "2.0"}
    result_custom = buy_rule_mod._evaluate(pd.DataFrame(), custom_indicators)
    assert result_custom is True, "Expected gate1 to pass with custom 2% threshold"


def test_buy_rule_empty_parameters_dict_behaves_as_defaults(monkeypatch):
    """indicators={'parameters': {}} → all defaults apply; same result as no key."""
    today_close = 520.0
    yest_close = 500.0

    monkeypatch.setattr(buy_rule_mod, "supertrend", lambda bars, period, multiplier: _st_pass_buy())

    indicators_no_key = {
        "bars_daily": _make_daily_buy(today_close=today_close, yest_close=yest_close),
        "bars_weekly": _make_weekly_pass(),
        "bars_5m": _make_5m_pass(),
        "bars_15m": _make_15m_pass(),
    }
    indicators_empty = dict(indicators_no_key)
    indicators_empty["parameters"] = {}

    assert buy_rule_mod._evaluate(pd.DataFrame(), indicators_no_key) == buy_rule_mod._evaluate(
        pd.DataFrame(), indicators_empty
    )


# ---------------------------------------------------------------------------
# SELL rule tests
# ---------------------------------------------------------------------------


def _make_daily_sell(
    n: int = 10,
    today_close: float = 485.0,
    yest_close: float = 500.0,
    yest_high: float = 505.0,
    yest_low: float = 490.0,
    today_open: float = 494.0,
    today_vol: float = 200_000.0,
    yest_vol: float = 100_000.0,
) -> pd.DataFrame:
    """Build a daily frame that passes all SELL daily gates with default parameters.

    today_close=485 vs yest_close=500 → -3.0% gap → passes default 3% sell gate (<=0.97×500=485).
    today_open=494 < yest_close=500 → passes gate 9.
    today_open=494 < pivot=(505+490+500)/3=498.33 → passes gate 10.
    today_vol=200_000 > yest_vol=100_000 → passes volume gate.
    today_close=485 > price_min=100 → passes gate 6.
    today_close=485 < price_max=5000 → passes gate 12.

    Note: 0.97×500=485.0 exactly; daily close must be STRICTLY less.
    Use 484.9 if you need to pass default. But for testing the boundary,
    we test with today_close slightly below the threshold.
    """
    filler = _make_ohlcv(n - 2, close=yest_close, vol=yest_vol)
    yest = pd.DataFrame(
        {
            "open": [yest_close * 0.999],
            "high": [yest_high],
            "low": [yest_low],
            "close": [yest_close],
            "volume": [yest_vol],
        }
    )
    today = pd.DataFrame(
        {
            "open": [today_open],
            "high": [today_open + 1.0],
            "low": [today_close - 2.0],
            "close": [today_close],
            "volume": [today_vol],
        }
    )
    return pd.concat([filler, yest, today], ignore_index=True)


def test_sell_rule_default_params_no_parameters_key(monkeypatch):
    """Call with indicators={} — default thresholds apply; a -3.1% gap passes."""
    today_close = 484.5  # 500 × 0.969 = 484.5 → 3.1% gap → passes default 3% gate
    yest_close = 500.0

    monkeypatch.setattr(
        sell_rule_mod,
        "supertrend",
        lambda bars, period, multiplier: _st_pass_sell(
            daily_close=today_close, yest_close=yest_close
        ),
    )

    indicators = {
        "bars_daily": _make_daily_sell(
            today_close=today_close, yest_close=yest_close, today_open=yest_close - 7.0
        ),
        "bars_weekly": _make_weekly_pass(close=today_close),
        "bars_5m": _make_5m_pass(close=today_close),
        "bars_15m": _make_15m_weak(close=today_close),
        # No "parameters" key
    }
    result = sell_rule_mod._evaluate(pd.DataFrame(), indicators)
    assert result is True


def test_sell_rule_custom_gap_pct_changes_gate1(monkeypatch):
    """A close exactly 2.5% below prev_close:
    - default gap_pct=3.0 → gate 1 FAILS (not enough gap DOWN) → False
    - custom gap_pct=2.0  → gate 1 PASSES → True (everything else passes too)
    """
    today_close = 487.5  # 500 × 0.975 = 487.5 → exactly 2.5% gap DOWN
    yest_close = 500.0

    monkeypatch.setattr(
        sell_rule_mod,
        "supertrend",
        lambda bars, period, multiplier: _st_pass_sell(
            daily_close=today_close, yest_close=yest_close
        ),
    )

    daily = _make_daily_sell(
        today_close=today_close, yest_close=yest_close, today_open=yest_close - 7.0
    )  # open < yest_close
    base_indicators = {
        "bars_daily": daily,
        "bars_weekly": _make_weekly_pass(close=today_close),
        "bars_5m": _make_5m_pass(close=today_close),
        "bars_15m": _make_15m_weak(close=today_close),
    }

    # Default: 3% threshold → 2.5% gap DOWN is not enough → fails gate 1
    result_default = sell_rule_mod._evaluate(pd.DataFrame(), dict(base_indicators))
    assert result_default is False, "Expected gate1 to fail with default 3% threshold"

    # Custom: 2.0% threshold → 2.5% gap DOWN is enough → passes gate 1
    custom_indicators = dict(base_indicators)
    custom_indicators["parameters"] = {"gap_pct": "2.0"}
    result_custom = sell_rule_mod._evaluate(pd.DataFrame(), custom_indicators)
    assert result_custom is True, "Expected gate1 to pass with custom 2% threshold"
