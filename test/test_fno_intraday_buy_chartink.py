"""Unit tests for the ``fno_intraday_buy_chartink`` 12-gate Chartink BUY rule.

Each of the 12 gates is exercised in isolation (one pass + one fail), plus
insufficient-warm-up rejections, a golden full-pass scenario, NaN guards, and
live-bar (pre/post 15:31 IST) alignment. Synthetic daily/weekly/5m/15m frame
builders at the top keep each test ~10 lines.

Time is frozen by patching the ``datetime`` symbol inside the rule module so
the rule's ``datetime.now(_IST)`` returns a fixed instant. Default is 16:00 IST
(post-settle) so the rule uses the ``-1 / -2`` daily indexing — the most recent
daily bar is "today".
"""

from datetime import datetime as _RealDateTime

import numpy as np
import pandas as pd
import pytest

import services.scan_rules.fno_intraday_buy_chartink as rulemod
from services.scan_rules.fno_intraday_buy_chartink import rule


# --------------------------------------------------------------------------- #
# Time freezing
# --------------------------------------------------------------------------- #
def _freeze(monkeypatch, hour, minute=0):
    """Pin ``rulemod.datetime.now(tz)`` to 2026-06-04 hour:minute (tz-aware)."""

    class _FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            naive = _RealDateTime(2026, 6, 4, hour, minute)
            return tz.localize(naive) if tz is not None else naive

    monkeypatch.setattr(rulemod, "datetime", _FrozenDateTime)


@pytest.fixture(autouse=True)
def _default_post_close(monkeypatch):
    """All tests default to 16:00 IST (post-settle → -1/-2 indexing)."""
    _freeze(monkeypatch, 16, 0)


# --------------------------------------------------------------------------- #
# Synthetic frame builders
# --------------------------------------------------------------------------- #
def make_daily_bars(
    n=210,
    flat_close=2000.0,
    today_close=2100.0,
    today_open=2050.0,
    today_vol=5000.0,
    old_vol=1000.0,
    recent_vol=1000.0,
    yest_high=None,
    yest_low=None,
):
    """Daily frame: flat history at ``flat_close`` then a gap-up bar last.

    Volume layout (so SMA(50) and SMA(200) can be steered independently):
    first ``n-50`` bars ``old_vol``, next 49 ``recent_vol``, last ``today_vol``.
    """
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = [flat_close] * (n - 1) + [today_close]
    open_ = [flat_close] * (n - 1) + [today_open]
    high = [flat_close] * (n - 1) + [today_close + 10]
    low = [flat_close] * (n - 1) + [today_open - 10]
    # Shape the second-last (yesterday) bar so gate-9 and gate-10 thresholds differ.
    high[-2] = yest_high if yest_high is not None else flat_close
    low[-2] = yest_low if yest_low is not None else flat_close
    vol = [old_vol] * (n - 50) + [recent_vol] * 49 + [today_vol]
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def make_weekly_bars(n=25, close=2000.0, rng=100.0):
    """Weekly frame with constant high-low range ``2*rng`` → ATR≈``2*rng``."""
    idx = pd.date_range("2024-01-07", periods=n, freq="W")
    return pd.DataFrame(
        {
            "open": [close] * n,
            "high": [close + rng] * n,
            "low": [close - rng] * n,
            "close": [close] * n,
            "volume": [1000.0] * n,
        },
        index=idx,
    )


def make_5m_bars(n=20, start_close=2060.0, step=2.0, last_vol=1000.0, base_vol=100.0):
    """5m frame: gently rising closes (Supertrend uptrend) + a volume spike last."""
    idx = pd.date_range("2026-06-04 09:15", periods=n, freq="5min")
    close = [start_close + step * i for i in range(n)]
    vol = [base_vol] * (n - 1) + [last_vol]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 5 for c in close],
            "low": [c - 5 for c in close],
            "close": close,
            "volume": vol,
        },
        index=idx,
    )


def make_15m_bars(n=20, start_close=2000.0, step=5.0, rising=True):
    """15m frame: monotone closes → RSI≈100 (rising) or ≈0 (falling)."""
    idx = pd.date_range("2026-06-04 09:15", periods=n, freq="15min")
    close = [start_close + step * i * (1 if rising else -1) for i in range(n)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 2 for c in close],
            "low": [c - 2 for c in close],
            "close": close,
            "volume": [1000.0] * n,
        },
        index=idx,
    )


def make_indicators(daily, weekly, b5m, b15m):
    return {
        "bars_5m": b5m,
        "bars_15m": b15m,
        "bars_daily": daily,
        "bars_weekly": weekly,
        "ema_20": None,  # backward-compat key the rule does not read
    }


def happy():
    """All-12-gates-pass indicators bundle."""
    return make_indicators(
        make_daily_bars(), make_weekly_bars(), make_5m_bars(), make_15m_bars()
    )


# --------------------------------------------------------------------------- #
# Insufficient warm-up
# --------------------------------------------------------------------------- #
def test_warmup_daily_none():
    ind = happy()
    ind["bars_daily"] = None
    assert rule(None, ind) is False


def test_warmup_daily_short():
    ind = happy()
    ind["bars_daily"] = make_daily_bars(n=199)
    assert rule(None, ind) is False


def test_warmup_weekly_short():
    ind = happy()
    ind["bars_weekly"] = make_weekly_bars(n=21)
    assert rule(None, ind) is False


def test_warmup_15m_short():
    ind = happy()
    ind["bars_15m"] = make_15m_bars(n=14)
    assert rule(None, ind) is False


# --------------------------------------------------------------------------- #
# Per-gate pass cases (all gates satisfied → True)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "gate",
    ["g6", "g12", "g1", "g9", "g10", "g2", "g8", "g7", "g13", "g5", "g3", "g4"],
)
def test_gate_passes(gate):
    assert rule(None, happy()) is True


# --------------------------------------------------------------------------- #
# Per-gate fail cases (exactly one gate broken → False)
# --------------------------------------------------------------------------- #
def _fail_g6():  # daily close <= 100
    return make_indicators(
        make_daily_bars(today_close=50.0), make_weekly_bars(), make_5m_bars(), make_15m_bars()
    )


def _fail_g12():  # daily close >= 5000
    return make_indicators(
        make_daily_bars(today_close=6000.0), make_weekly_bars(), make_5m_bars(), make_15m_bars()
    )


def _fail_g1():  # close only +1% (needs > +3%)
    return make_indicators(
        make_daily_bars(today_close=2020.0), make_weekly_bars(), make_5m_bars(), make_15m_bars()
    )


def _fail_g9():  # open <= yest close
    return make_indicators(
        make_daily_bars(today_open=1990.0), make_weekly_bars(), make_5m_bars(), make_15m_bars()
    )


def _fail_g10():  # open below pivot but above yest close
    return make_indicators(
        make_daily_bars(today_open=2050.0, yest_high=2300.0, yest_low=2000.0),
        make_weekly_bars(), make_5m_bars(), make_15m_bars(),
    )


def _fail_g2():  # vol below SMA(50) but above SMA(200) (old_vol low)
    return make_indicators(
        make_daily_bars(today_vol=900.0, old_vol=500.0, recent_vol=1000.0),
        make_weekly_bars(), make_5m_bars(), make_15m_bars(),
    )


def _fail_g8():  # vol above SMA(50) but below SMA(200) (old_vol high)
    return make_indicators(
        make_daily_bars(today_vol=1500.0, old_vol=10000.0, recent_vol=1000.0),
        make_weekly_bars(), make_5m_bars(), make_15m_bars(),
    )


def _fail_g7():  # weekly ATR <= 5% * close
    return make_indicators(
        make_daily_bars(), make_weekly_bars(rng=20.0), make_5m_bars(), make_15m_bars()
    )


def _fail_g13():  # 5m vol not > 2x SMA(10)
    return make_indicators(
        make_daily_bars(), make_weekly_bars(), make_5m_bars(last_vol=150.0), make_15m_bars()
    )


def _fail_g5():  # 15m RSI <= 50
    return make_indicators(
        make_daily_bars(), make_weekly_bars(), make_5m_bars(), make_15m_bars(rising=False)
    )


def _fail_g3():  # 5m Supertrend line >= today close (prices shifted up)
    return make_indicators(
        make_daily_bars(), make_weekly_bars(), make_5m_bars(start_close=2200.0), make_15m_bars()
    )


def _fail_g4():  # 5m prior Supertrend line < yest close (prices shifted down)
    return make_indicators(
        make_daily_bars(), make_weekly_bars(), make_5m_bars(start_close=1900.0), make_15m_bars()
    )


@pytest.mark.parametrize(
    "builder",
    [
        _fail_g6, _fail_g12, _fail_g1, _fail_g9, _fail_g10, _fail_g2,
        _fail_g8, _fail_g7, _fail_g13, _fail_g5, _fail_g3, _fail_g4,
    ],
)
def test_gate_fails(builder):
    assert rule(None, builder()) is False


# --------------------------------------------------------------------------- #
# Golden full-pass
# --------------------------------------------------------------------------- #
def test_golden_full_pass():
    assert rule(None, happy()) is True


# --------------------------------------------------------------------------- #
# NaN guards (NaN indicator must reject, never silently pass)
# --------------------------------------------------------------------------- #
def test_nan_daily_sma_rejects():
    daily = make_daily_bars()
    daily.iloc[-5, daily.columns.get_loc("volume")] = np.nan  # taints SMA(50)/(200)
    assert rule(None, make_indicators(daily, make_weekly_bars(), make_5m_bars(), make_15m_bars())) is False


def test_nan_15m_rsi_rejects():
    # Flat closes → zero gains / zero losses → RSI = 0/0 = NaN (not a silent pass).
    b15 = make_15m_bars(step=0.0)
    assert rule(None, make_indicators(make_daily_bars(), make_weekly_bars(), make_5m_bars(), b15)) is False


# --------------------------------------------------------------------------- #
# Live-bar alignment (pre/post 15:31 IST switch)
# --------------------------------------------------------------------------- #
def _add_forming_bar(daily):
    """Append a non-conforming 'still forming' daily bar (no gap vs prev close)."""
    idx = daily.index[-1] + pd.Timedelta(days=1)
    row = pd.DataFrame(
        {"open": [2100.0], "high": [2110.0], "low": [2090.0], "close": [2100.0], "volume": [5000.0]},
        index=[idx],
    )
    return pd.concat([daily, row])


def test_alignment_preclose_uses_minus2(monkeypatch):
    # Gap-up at [-2], junk forming bar at [-1]. Pre-15:31 → uses [-2]/[-3] → pass.
    daily = _add_forming_bar(make_daily_bars())
    ind = make_indicators(daily, make_weekly_bars(), make_5m_bars(), make_15m_bars())
    _freeze(monkeypatch, 11, 0)
    assert rule(None, ind) is True


def test_alignment_postclose_uses_minus1(monkeypatch):
    # Same data; post-15:31 → uses [-1]/[-2] → junk forming bar fails gate-1.
    daily = _add_forming_bar(make_daily_bars())
    ind = make_indicators(daily, make_weekly_bars(), make_5m_bars(), make_15m_bars())
    _freeze(monkeypatch, 16, 0)
    assert rule(None, ind) is False
