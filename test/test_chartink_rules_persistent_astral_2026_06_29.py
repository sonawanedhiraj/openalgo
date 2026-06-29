"""Regression test for Issue #197 — locks in the 2026-06-29 PERSISTENT and
ASTRAL SELL scenarios.

On 2026-06-29 (Monday) Chartink fired both stocks into the operator's SELL
screener (PERSISTENT −9.8%, ASTRAL −9.5%) but the in-house scanner produced
zero hits all day. Root cause: the rule resolved ``today_d`` from
``bars_daily.iloc[-2]`` pre-15:31 IST under the assumption that
``iloc[-1]`` was today's forming bar. In production ``bars_daily`` is the
``ScannerHistoryProvider`` cache backfilled from historify at boot, which
does NOT include today's bar — so the rule was comparing
*Thursday-vs-Wednesday* instead of *today-vs-yesterday*, invisible to the
session's actual price action.

This test replays the captured 5m / 15m / D fixtures for both stocks
through the fixed rule (which derives today_d from today's 5m bars) and
asserts:

  1. The SELL rule fires on a per-5m-bar replay of today's session.
  2. The BUY rule does NOT fire (sanity — these are crash days, not gap-ups).

Fixtures live under ``test/fixtures/scan_rules/`` and were captured live
via ``/api/v1/history`` at ~11:30 IST on 2026-06-29. The D-bar fixtures
include today's *running* bar (broker returns it intra-session), but the
test deliberately strips it before invoking the rule — simulating the
production state where ``ScannerHistoryProvider`` has only settled bars.
"""

from __future__ import annotations

import json
from datetime import date as _date
from datetime import datetime as _RealDateTime
from pathlib import Path

import pandas as pd
import pytest
import pytz

import services.scan_rules.fno_intraday_buy_chartink as buymod
import services.scan_rules.fno_intraday_sell_chartink as sellmod
from services.scan_rules.fno_intraday_buy_chartink import rule as buy_rule
from services.scan_rules.fno_intraday_sell_chartink import rule as sell_rule

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "scan_rules"
_IST = pytz.timezone("Asia/Kolkata")
# The capture timestamp — 2026-06-29 11:30 IST is well within the session and
# is the wall-clock at which Chartink had already fired both stocks 3 times.
_NOW_IST = _IST.localize(_RealDateTime(2026, 6, 29, 11, 30))
_TODAY = _date(2026, 6, 29)


def _freeze_now(monkeypatch):
    """Pin ``datetime.now`` in both rule modules to ``_NOW_IST``."""

    class _Frozen:
        @classmethod
        def now(cls, tz=None):
            return _NOW_IST.astimezone(tz) if tz is not None else _NOW_IST.replace(tzinfo=None)

    monkeypatch.setattr(sellmod, "datetime", _Frozen)
    monkeypatch.setattr(buymod, "datetime", _Frozen)


def _load_bars(sym: str, interval: str) -> pd.DataFrame:
    with (_FIXTURE_DIR / f"{sym}_{interval}.json").open(encoding="utf-8") as f:
        d = json.load(f)
    df = pd.DataFrame(d["data"]).sort_values("timestamp").reset_index(drop=True)
    return df


def _roll_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """The broker /history API does NOT expose 'W'; roll from D for the
    weekly ATR gate."""
    d = daily.copy()
    d["dt"] = pd.to_datetime(d["timestamp"], unit="s", utc=True).dt.tz_convert(_IST)
    d = d.set_index("dt")
    w = (
        d.resample("W-FRI")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
        .reset_index(drop=True)
    )
    return w


def _slice_to(bars: pd.DataFrame, max_ts: int) -> pd.DataFrame:
    """Return only bars with timestamp <= max_ts (epoch seconds)."""
    return bars[bars["timestamp"] <= max_ts].reset_index(drop=True)


def _build_indicators(sym, daily_no_today, weekly, bars_5m, bars_15m):
    return {
        "symbol": sym,
        "exchange": "NSE",
        "bars_5m": bars_5m,
        "bars_15m": bars_15m,
        "bars_daily": daily_no_today,
        "bars_weekly": weekly,
        "parameters": {"gap_pct": 1.5, "atr_pct": 5.0, "rsi_threshold": 50.0},
    }


@pytest.mark.parametrize("sym", ["PERSISTENT", "ASTRAL"])
def test_sell_rule_fires_on_2026_06_29_crash_day(sym, monkeypatch):
    """Replay every 5m bar of 2026-06-29 through the SELL rule and assert
    it fires on at least one 5m bar close. This is the regression: the
    pre-#197 rule fired ZERO times on these crash days for the entire
    session.
    """
    _freeze_now(monkeypatch)

    daily_full = _load_bars(sym, "D")
    bars_5m_full = _load_bars(sym, "5m")
    bars_15m_full = _load_bars(sym, "15m")
    weekly = _roll_weekly(daily_full)

    # Strip today's running D-bar so we exactly simulate the
    # ScannerHistoryProvider production state (settled bars only — the
    # backfill never inserted today's bar yet).
    daily_dates = (
        pd.to_datetime(daily_full["timestamp"], unit="s", utc=True).dt.tz_convert(_IST).dt.date
    )
    daily_no_today = daily_full[daily_dates < _TODAY].reset_index(drop=True)
    assert len(daily_no_today) >= 3, "fixture must include enough settled history"
    assert daily_dates.iloc[-1] == _TODAY, "fixture must include today's running bar"

    # Walk every 5m bar that closed on or before 11:30 IST today (the
    # capture wall-clock). For each closed bar, slice the 5m/15m frames
    # to bars at or before that close and evaluate the rule.
    today_close_cap_ts = int(_NOW_IST.timestamp())
    today_5m = bars_5m_full[
        (
            pd.to_datetime(bars_5m_full["timestamp"], unit="s", utc=True)
            .dt.tz_convert(_IST)
            .dt.date
            == _TODAY
        )
        & (bars_5m_full["timestamp"] <= today_close_cap_ts)
    ]
    assert len(today_5m) >= 8, f"need warm-up bars for {sym}; got {len(today_5m)}"

    any_fires = False
    for _, row in today_5m.iterrows():
        ts = int(row["timestamp"])
        b5 = _slice_to(bars_5m_full, ts)
        b15 = _slice_to(bars_15m_full, ts)
        if len(b5) < 8 or len(b15) < 15:
            continue
        ind = _build_indicators(sym, daily_no_today, weekly, b5, b15)
        if sell_rule(None, ind):
            any_fires = True
            break  # one fire is enough — production scanner fires every 5m bar

    assert any_fires, (
        f"SELL rule never fired for {sym} on 2026-06-29 — Issue #197 regression. "
        f"Today's 5m bars walked: {len(today_5m)}; daily_no_today rows: {len(daily_no_today)}"
    )


@pytest.mark.parametrize("sym", ["PERSISTENT", "ASTRAL"])
def test_buy_rule_does_not_fire_on_crash_day(sym, monkeypatch):
    """Sanity check: a −9% crash day must NOT fire the BUY rule. Catches
    the case where the today_d helper accidentally returns gap-up
    semantics (e.g. mixing up high/low or sign-flipping volume).
    """
    _freeze_now(monkeypatch)

    daily_full = _load_bars(sym, "D")
    bars_5m_full = _load_bars(sym, "5m")
    bars_15m_full = _load_bars(sym, "15m")
    weekly = _roll_weekly(daily_full)

    daily_dates = (
        pd.to_datetime(daily_full["timestamp"], unit="s", utc=True).dt.tz_convert(_IST).dt.date
    )
    daily_no_today = daily_full[daily_dates < _TODAY].reset_index(drop=True)

    # BUY rule needs SMA(volume, 200) — make sure we have enough settled bars.
    if len(daily_no_today) < 200:
        pytest.skip(f"insufficient settled D history for {sym}: {len(daily_no_today)}")

    today_close_cap_ts = int(_NOW_IST.timestamp())
    today_5m = bars_5m_full[
        (
            pd.to_datetime(bars_5m_full["timestamp"], unit="s", utc=True)
            .dt.tz_convert(_IST)
            .dt.date
            == _TODAY
        )
        & (bars_5m_full["timestamp"] <= today_close_cap_ts)
    ]

    for _, row in today_5m.iterrows():
        ts = int(row["timestamp"])
        b5 = _slice_to(bars_5m_full, ts)
        b15 = _slice_to(bars_15m_full, ts)
        if len(b5) < 10 or len(b15) < 15:
            continue
        ind = _build_indicators(sym, daily_no_today, weekly, b5, b15)
        # BUY rule reads CHARTINK_RULE_BUY_GAP_PCT; parameters dict wins.
        ind["parameters"]["gap_pct"] = 1.5
        assert not buy_rule(None, ind), (
            f"BUY rule fired on a crash-day for {sym} at "
            f"{_RealDateTime.fromtimestamp(ts, tz=_IST).strftime('%H:%M:%S')} — "
            f"today_d derivation may have sign-flipped"
        )
