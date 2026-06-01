"""Tests for the Stage 1.7 market regime classifier scaffold.

All market-data sources are mocked — these tests never hit the broker,
duckdb, or the SQLite ``market_intel`` table. The intent is to pin down
the classifier's category-bucketing logic and the matches() semantics
on :class:`MarketRegime` / :class:`RegimeProfile`.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest
import pytz

from services import market_regime_service as mrs
from strategies.base import RegimeProfile

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# matches() — covers all four constrained dimensions
# ---------------------------------------------------------------------------


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
    return mrs.MarketRegime(**base)


def test_matches_none_profile_accepts_every_regime():
    regime = _make_regime(trend="bearish", volatility="extreme")
    assert regime.matches(None) is True


def test_matches_returns_true_when_every_constraint_is_satisfied():
    regime = _make_regime()
    profile = RegimeProfile.of(
        trend={"bullish"},
        volatility={"low", "medium"},
        breadth={"wide", "mixed"},
        time_of_day={"mid_morning", "afternoon"},
    )
    assert regime.matches(profile) is True


def test_matches_returns_false_on_trend_mismatch():
    regime = _make_regime(trend="bearish")
    profile = RegimeProfile.of(trend={"bullish"})
    assert regime.matches(profile) is False


def test_matches_returns_false_on_volatility_mismatch():
    regime = _make_regime(volatility="extreme")
    profile = RegimeProfile.of(volatility={"low", "medium"})
    assert regime.matches(profile) is False


def test_matches_ignores_unspecified_dimensions():
    """A profile that only constrains trend should accept any breadth /
    volatility / time_of_day."""
    regime = _make_regime(trend="bullish", volatility="extreme", breadth="narrow")
    profile = RegimeProfile.of(trend={"bullish"})
    assert regime.matches(profile) is True


# ---------------------------------------------------------------------------
# time_of_day bucketing — explicit boundary checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hh", "mm", "expected"),
    [
        (9, 15, "opening"),
        (9, 59, "opening"),
        (10, 0, "mid_morning"),
        (11, 29, "mid_morning"),
        (11, 30, "lunch"),
        (12, 59, "lunch"),
        (13, 0, "afternoon"),
        (14, 29, "afternoon"),
        (14, 30, "power_hour"),
        (15, 14, "power_hour"),
        (15, 15, "eod"),
        (15, 30, "eod"),
        (8, 30, "opening"),  # pre-market sentinel
    ],
)
def test_time_of_day_bucketing(hh, mm, expected):
    ts = IST.localize(datetime(2026, 6, 1, hh, mm))
    assert mrs._classify_time_of_day(ts) == expected


# ---------------------------------------------------------------------------
# compute_current_regime — populates all 5 dims
# ---------------------------------------------------------------------------


def _fake_nifty_ohlcv(n: int = 80, trend: str = "bullish") -> pd.DataFrame:
    """Fabricate a 1-D OHLCV frame that triggers a bullish / bearish /
    flat EMA cross. ``trend`` selects the slope."""
    if trend == "bullish":
        closes = [20000 + i * 50 for i in range(n)]
    elif trend == "bearish":
        closes = [25000 - i * 50 for i in range(n)]
    else:
        closes = [22000 + ((-1) ** i) * 5 for i in range(n)]
    return pd.DataFrame(
        {
            "timestamp": range(n),
            "open": closes,
            "high": [c + 10 for c in closes],
            "low": [c - 10 for c in closes],
            "close": closes,
            "volume": [0] * n,
            "oi": [0] * n,
        }
    )


def test_compute_current_regime_populates_every_dimension(monkeypatch):
    """End-to-end happy-path: every regime dim should be a non-empty
    label and every raw_metrics bucket should be filled."""
    monkeypatch.setattr(
        "database.historify_db.get_ohlcv",
        lambda symbol, exchange, interval: _fake_nifty_ohlcv(),
    )
    # No breadth universe configured ⇒ breadth falls to 'mixed'.
    monkeypatch.delenv("REGIME_BREADTH_UNIVERSE", raising=False)
    monkeypatch.delenv("REGIME_VIX_FALLBACK", raising=False)

    now = IST.localize(datetime(2026, 6, 1, 10, 30))
    regime = mrs.compute_current_regime(now=now)

    assert regime.timestamp == now
    assert regime.trend in {"bullish", "bearish", "range_bound"}
    assert regime.volatility in {"low", "medium", "high", "extreme"}
    assert regime.breadth in {"wide", "mixed", "narrow"}
    assert regime.time_of_day == "mid_morning"
    assert isinstance(regime.sector_leaders, list)
    # Concentration is (top - median) / (|top| + 0.01) — non-negative but
    # not bounded above (can exceed 1.0 when laggards drift further than
    # the leader). >0.5 = dominant, <0.2 = broad rotation.
    assert regime.sector_leader_concentration >= 0.0
    # Raw metrics surface the underlying numbers for every dimension.
    assert set(regime.raw_metrics) >= {
        "trend",
        "volatility",
        "breadth",
        "sector_rotation",
        "time_of_day",
    }


def test_compute_current_regime_breadth_wide_when_majority_above_ma(monkeypatch):
    """Inject a universe + bars_loader so we can verify the >65% bucket."""

    def loader(symbol, exchange, interval):
        # 4 of 5 symbols print well above their 20d SMA, 1 below.
        if symbol == "DOWN":
            closes = [100 - i for i in range(40)]  # last close < SMA
        else:
            closes = [50 + i for i in range(40)]  # last close > SMA
        return pd.DataFrame({"close": closes})

    monkeypatch.setattr(
        "database.historify_db.get_ohlcv",
        lambda *a, **kw: _fake_nifty_ohlcv(),
    )

    regime = mrs.compute_current_regime(
        now=IST.localize(datetime(2026, 6, 1, 10, 30)),
        universe_loader=lambda: ["A", "B", "C", "D", "DOWN"],
        bars_loader=loader,
    )
    assert regime.breadth == "wide"
    assert regime.raw_metrics["breadth"]["above_count"] == 4
    assert regime.raw_metrics["breadth"]["evaluated"] == 5


def test_compute_current_regime_falls_back_to_range_bound_with_no_history(monkeypatch):
    monkeypatch.setattr(
        "database.historify_db.get_ohlcv",
        lambda *a, **kw: pd.DataFrame(columns=["close"]),
    )
    regime = mrs.compute_current_regime(
        now=IST.localize(datetime(2026, 6, 1, 10, 30)),
        universe_loader=lambda: [],
    )
    assert regime.trend == "range_bound"
    assert regime.raw_metrics["trend"]["reason"] == "insufficient_history"


def test_get_cached_regime_recomputes_on_stale(monkeypatch):
    """Once the cached snapshot ages out, ``get_cached_regime`` should
    rebuild it. We swap ``compute_current_regime`` for a counter and
    use the real wall-clock so the freshness comparison is meaningful."""
    mrs.reset_cache()
    call_count = {"n": 0}

    def fake_compute():
        call_count["n"] += 1
        # Stamp with real now-IST so the freshness window applies.
        return _make_regime(timestamp=datetime.now(IST))

    monkeypatch.setattr(mrs, "compute_current_regime", fake_compute)
    # First call computes.
    r1 = mrs.get_cached_regime(max_age_minutes=60)
    assert r1 is not None
    assert call_count["n"] == 1
    # Second call is fresh enough — cache hit, no recompute.
    r2 = mrs.get_cached_regime(max_age_minutes=60)
    assert r2 is r1
    assert call_count["n"] == 1

    # ``max_age_minutes=0`` forces a miss because the cutoff is "now",
    # and the cached timestamp must be strictly later than now to win.
    mrs.get_cached_regime(max_age_minutes=0)
    assert call_count["n"] == 2

    mrs.reset_cache()
