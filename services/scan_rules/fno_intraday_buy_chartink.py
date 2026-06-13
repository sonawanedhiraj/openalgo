"""Chartink-equivalent BUY rule.

Mirrors the operator's live Chartink ``fno-intraday-buy`` formula with 12
AND-gated conditions. All gates must clear; evaluation short-circuits on the
first miss. Gate 11 of the original formula (``open > low``) is tautological and
skipped — the internal numbering below is preserved from the source formula for
traceability, so the order here is intentionally non-sequential.

Gates (source frame / lookback):
  6  daily close > 100                              daily[-1]      1
  12 daily close < 5000                             daily[-1]      1
  1  daily close > 1d-ago close * 1.03              daily[-2:]     2
  9  daily open  > 1d-ago close                     daily[-2:]     2
  10 daily open  > pivot (H+L+C of [-2]) / 3        daily[-2:]     2
  2  daily vol   > SMA(daily vol, 50)               daily SMA      50
  8  daily vol   > SMA(daily vol, 200)              daily SMA      200
  7  weekly ATR(21) > 5% * daily close              weekly         22
  13 5m vol > 2 * SMA(5m vol, 10)                   bars_5m        10
  5  15m RSI(14) > 50                               bars_15m       15
  3  5m Supertrend(7,3)[-1] < daily close           bars_5m        >=7
  4  5m Supertrend(7,3)[-2] >= 1d-ago daily close   bars_5m        >=7

Insufficient warm-up rejects the symbol (no gate skipping). Indicator NaN
during warm-up is treated as a rejection, not a silent pass — a bare
``volume <= NaN`` comparison is ``False`` (would wrongly "pass" the gate), so
every indicator value is ``pd.isna``-checked before use.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import time as dtime

import pandas as pd
import pytz

from services.indicators import atr, rsi, sma, supertrend
from services.scanner_service import scan_rule
from utils.logging import get_logger

logger = get_logger(__name__)

_IST = pytz.timezone("Asia/Kolkata")
_SETTLE_CUTOFF = dtime(15, 31)  # after this IST time, today's daily bar has settled


@scan_rule(
    "fno_intraday_buy_chartink",
    "buy",
    "12-gate Chartink BUY mirror (gap-up + volume surge + trend confirmation).",
)
def rule(bars: pd.DataFrame, indicators: dict) -> bool:
    """12-gate Chartink BUY mirror. Returns ``True`` only if every gate passes."""
    try:
        return _evaluate(bars, indicators)
    except Exception:
        # An indicator computation raised (e.g. ATR over a NaN-laden series).
        # Reject this symbol rather than crash the scan loop.
        logger.debug("fno_intraday_buy_chartink: evaluation raised, rejecting", exc_info=True)
        return False


def _evaluate(bars: pd.DataFrame, indicators: dict) -> bool:
    bars_5m = indicators.get("bars_5m")
    if bars_5m is None:
        bars_5m = bars  # rule_fn is called with the 5m frame as `bars`
    bars_15m = indicators.get("bars_15m")
    bars_daily = indicators.get("bars_daily")
    bars_weekly = indicators.get("bars_weekly")

    # --- Warm-up guards: insufficient history rejects (does NOT skip gates) ---
    # Daily needs 200 rows for SMA(volume, 200) and >=2 rows for [-1]/[-2] indexing.
    if bars_daily is None or len(bars_daily) < 200:
        return False
    if bars_weekly is None or len(bars_weekly) < 22:
        return False
    if bars_5m is None or len(bars_5m) < 10:  # SMA(5m vol, 10) + Supertrend(7) warm-up
        return False
    if bars_15m is None or len(bars_15m) < 15:  # RSI(14) warm-up
        return False

    # --- Live-bar alignment ---
    # Intraday, today's daily bar at iloc[-1] is still forming. Until 15:31 IST
    # the most recent *settled* daily bar is iloc[-2]; "yesterday" is then
    # iloc[-3]. After 15:31 IST today's bar has settled, so use -1 / -2.
    now_ist = datetime.now(_IST)
    if now_ist.time() < _SETTLE_CUTOFF:
        today_idx, yest_idx = -2, -3
    else:
        today_idx, yest_idx = -1, -2
    if len(bars_daily) < abs(yest_idx):
        return False

    today_d = bars_daily.iloc[today_idx]
    yest_d = bars_daily.iloc[yest_idx]

    # Reject if any required daily field is NaN.
    if _any_nan(
        today_d.close, today_d.open, today_d.volume,
        yest_d.close, yest_d.high, yest_d.low,
    ):
        return False

    # Gate 6: daily close > 100
    if today_d.close <= 100:
        return False
    # Gate 12: daily close < 5000
    if today_d.close >= 5000:
        return False
    # Gate 1: daily close > 1d-ago close * buy_mult (default 3% gap). Threshold is
    # read at call time from CHARTINK_RULE_BUY_GAP_PCT so changes take effect
    # without a restart and stay aligned with the Chartink screener formula.
    buy_pct = float(os.environ.get("CHARTINK_RULE_BUY_GAP_PCT", "3.0"))
    buy_mult = 1.0 + buy_pct / 100.0
    if today_d.close <= yest_d.close * buy_mult:
        return False
    # Gate 9: daily open > 1d-ago close
    if today_d.open <= yest_d.close:
        return False
    # Gate 10: daily open > typical pivot = (H + L + C of 1d-ago) / 3
    pivot = (yest_d.high + yest_d.low + yest_d.close) / 3.0
    if today_d.open <= pivot:
        return False

    # Gates 2 + 8: daily volume vs SMA(50) and SMA(200)
    sma_vol_50 = sma(bars_daily["volume"], 50).iloc[today_idx]
    sma_vol_200 = sma(bars_daily["volume"], 200).iloc[today_idx]
    if _any_nan(sma_vol_50, sma_vol_200):
        return False
    if today_d.volume <= sma_vol_50:
        return False
    if today_d.volume <= sma_vol_200:
        return False

    # Gate 7: weekly ATR(21) > 5% * daily close.
    # Exclude the current (potentially partial) week when we have a spare row.
    weekly_for_atr = bars_weekly.iloc[:-1] if len(bars_weekly) > 22 else bars_weekly
    weekly_atr = atr(weekly_for_atr, period=21).iloc[-1]
    if _any_nan(weekly_atr):
        return False
    if weekly_atr <= today_d.close * 0.05:
        return False

    # Gate 13: 5m volume > 2 * SMA(5m vol, 10)
    sma_5m_vol_10 = sma(bars_5m["volume"], 10).iloc[-1]
    last_5m_vol = bars_5m["volume"].iloc[-1]
    if _any_nan(sma_5m_vol_10, last_5m_vol):
        return False
    if last_5m_vol <= sma_5m_vol_10 * 2.0:
        return False

    # Gate 5: 15m RSI(14) > 50
    rsi_15m = rsi(bars_15m["close"], period=14).iloc[-1]
    if _any_nan(rsi_15m):
        return False
    if rsi_15m <= 50:
        return False

    # Gates 3 + 4: 5m Supertrend(7, 3). [0] = iloc[-1] (current), [-1] = iloc[-2].
    st = supertrend(bars_5m, period=7, multiplier=3.0)
    if len(st) < 2:
        return False
    st_now = st["line"].iloc[-1]
    st_prev = st["line"].iloc[-2]
    if _any_nan(st_now, st_prev):
        return False
    # Gate 3: current 5m Supertrend < daily close (price above the trend line)
    if st_now >= today_d.close:
        return False
    # Gate 4: prior 5m Supertrend >= 1d-ago daily close
    if st_prev < yest_d.close:
        return False

    return True


def _any_nan(*values: float) -> bool:
    """True if any value is NaN — used to reject rather than silently pass a gate."""
    return any(pd.isna(v) for v in values)
