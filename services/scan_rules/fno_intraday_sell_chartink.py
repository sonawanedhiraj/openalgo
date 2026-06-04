"""Chartink-equivalent SELL rule — mirror of ``fno_intraday_buy_chartink``.

Mirrors the operator's live Chartink ``alert-for-intraday-sell-fno`` formula.
It is the BUY rule with the directional inequalities flipped (Supertrend, RSI,
open-vs-prev-close, open-vs-pivot, and the close-gap), plus a **simpler volume
gate**. As with BUY, evaluation short-circuits on the first miss.

Key differences from the BUY rule (worth flagging):
  * Gates 3, 4, 5, 9, 10 are inequality-flipped (the directional mirror).
  * Gate 1 uses ``* 0.97`` (a 3% gap **DOWN**) instead of BUY's ``* 1.03``.
  * Volume is a single gate ``daily volume > 1 day ago volume`` — a deliberate
    Chartink design choice for the SELL leg. BUY's two daily-volume gates
    (SMA(50) + SMA(200)) AND its 5m-volume-surge gate (g13) are **all absent**
    here. So this SELL rule needs NO 200-day SMA warm-up, and there is no
    5-minute volume condition at all.
  * BUY's tautological gate 11 (``open > low``) mirrors to ``open < high`` for
    SELL — equally tautological, so it is likewise skipped.

That leaves **10 active gates** (vs BUY's 12). The brief's "11" counted BUY's
5m-volume gate as surviving; the source SELL formula has no 5m-volume condition,
so it does not.

Gates (source frame / lookback):
  6  daily close > 100                              daily[-1]      1
  12 daily close < 5000                             daily[-1]      1
  1  daily close < 1d-ago close * 0.97  (gap DOWN)  daily[-2:]     2
  9  daily open  < 1d-ago close                     daily[-2:]     2
  10 daily open  < pivot (H+L+C of [-2]) / 3        daily[-2:]     2
  V  daily volume > 1d-ago volume                   daily[-2:]     2
  7  weekly ATR(21) > 5% * daily close              weekly         22
  5  15m RSI(14) < 50                               bars_15m       15
  3  5m Supertrend(7,3)[0]  > daily close           bars_5m        >=8
  4  5m Supertrend(7,3)[-1] <= 1d-ago daily close   bars_5m        >=8

Insufficient warm-up rejects the symbol (no gate skipping). Indicator NaN
during warm-up is treated as a rejection, not a silent pass — every indicator
value is ``pd.isna``-checked before use.
"""

from __future__ import annotations

from datetime import datetime, time as dtime

import pandas as pd
import pytz

from services.indicators import atr, rsi, supertrend
from services.scanner_service import scan_rule
from utils.logging import get_logger

logger = get_logger(__name__)

_IST = pytz.timezone("Asia/Kolkata")
_SETTLE_CUTOFF = dtime(15, 31)  # after this IST time, today's daily bar has settled


@scan_rule(
    "fno_intraday_sell_chartink",
    "sell",
    "10-gate Chartink SELL mirror (gap-down + simple volume + downtrend confirmation).",
)
def rule(bars: pd.DataFrame, indicators: dict) -> bool:
    """10-gate Chartink SELL mirror. Returns ``True`` only if every gate passes."""
    try:
        return _evaluate(bars, indicators)
    except Exception:
        # An indicator computation raised (e.g. ATR over a NaN-laden series).
        # Reject this symbol rather than crash the scan loop.
        logger.debug("fno_intraday_sell_chartink: evaluation raised, rejecting", exc_info=True)
        return False


def _evaluate(bars: pd.DataFrame, indicators: dict) -> bool:
    bars_5m = indicators.get("bars_5m")
    if bars_5m is None:
        bars_5m = bars  # rule_fn is called with the 5m frame as `bars`
    bars_15m = indicators.get("bars_15m")
    bars_daily = indicators.get("bars_daily")
    bars_weekly = indicators.get("bars_weekly")

    # --- Warm-up guards: insufficient history rejects (does NOT skip gates) ---
    # Unlike BUY, SELL has no SMA(volume, 200) gate, so daily needs only enough
    # rows for [-1]/[-2] (post-settle) or [-2]/[-3] (pre-settle) indexing.
    if bars_daily is None or len(bars_daily) < 3:
        return False
    if bars_weekly is None or len(bars_weekly) < 22:
        return False
    if bars_5m is None or len(bars_5m) < 8:  # Supertrend(7) warm-up (period + ATR seed)
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
        yest_d.close, yest_d.high, yest_d.low, yest_d.volume,
    ):
        return False

    # Gate 6: daily close > 100
    if today_d.close <= 100:
        return False
    # Gate 12: daily close < 5000
    if today_d.close >= 5000:
        return False
    # Gate 1: daily close < 1d-ago close * 0.97 (3% gap DOWN) — flipped from BUY
    if today_d.close >= yest_d.close * 0.97:
        return False
    # Gate 9: daily open < 1d-ago close — flipped from BUY
    if today_d.open >= yest_d.close:
        return False
    # Gate 10: daily open < typical pivot = (H + L + C of 1d-ago) / 3 — flipped from BUY
    pivot = (yest_d.high + yest_d.low + yest_d.close) / 3.0
    if today_d.open >= pivot:
        return False

    # Volume gate: daily volume > 1d-ago volume (simpler than BUY's SMA(50)+SMA(200))
    if today_d.volume <= yest_d.volume:
        return False

    # Gate 7: weekly ATR(21) > 5% * daily close (same as BUY).
    # Exclude the current (potentially partial) week when we have a spare row.
    weekly_for_atr = bars_weekly.iloc[:-1] if len(bars_weekly) > 22 else bars_weekly
    weekly_atr = atr(weekly_for_atr, period=21).iloc[-1]
    if _any_nan(weekly_atr):
        return False
    if weekly_atr <= today_d.close * 0.05:
        return False

    # Gate 5: 15m RSI(14) < 50 — flipped from BUY
    rsi_15m = rsi(bars_15m["close"], period=14).iloc[-1]
    if _any_nan(rsi_15m):
        return False
    if rsi_15m >= 50:
        return False

    # Gates 3 + 4: 5m Supertrend(7, 3). [0] = iloc[-1] (current), [-1] = iloc[-2].
    st = supertrend(bars_5m, period=7, multiplier=3.0)
    if len(st) < 2:
        return False
    st_now = st["line"].iloc[-1]
    st_prev = st["line"].iloc[-2]
    if _any_nan(st_now, st_prev):
        return False
    # Gate 3: current 5m Supertrend > daily close (price below the trend line) — flipped
    if st_now <= today_d.close:
        return False
    # Gate 4: prior 5m Supertrend <= 1d-ago daily close — flipped from BUY
    if st_prev > yest_d.close:
        return False

    return True


def _any_nan(*values: float) -> bool:
    """True if any value is NaN — used to reject rather than silently pass a gate."""
    return any(pd.isna(v) for v in values)
