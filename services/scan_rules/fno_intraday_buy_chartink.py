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
from datetime import datetime, timedelta

import pandas as pd
import pytz

from services.indicators import atr, rsi, sma, supertrend
from services.scan_rules._today_running import derive_today_and_yest
from services.scanner_service import scan_rule
from utils.logging import get_logger

logger = get_logger(__name__)

_IST = pytz.timezone("Asia/Kolkata")


def _reject_missing(symbol: str, reason: str) -> bool:
    """Loudly log a missing-input rejection (Tier-1 Fix #2), then return False.

    A ``None`` daily/weekly/intraday frame means the data pipeline did not supply
    an input — a supply problem worth a WARNING, not the silent ``return False``
    that made the 2026-06-15 failures look like ordinary quiet days. (Short-but-
    present frames are normal warm-up and stay at DEBUG below.)"""
    logger.warning("fno_intraday_buy_chartink %s: rejecting — %s", symbol, reason)
    return False


def _dbar_date_verify_enabled() -> bool:
    """``SCANNER_DBAR_DATE_VERIFY_ENABLED`` env flag (default true). Gates the
    post-settle daily-bar-date staleness guard below."""
    return os.environ.get("SCANNER_DBAR_DATE_VERIFY_ENABLED", "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def _daily_bar_date(bars_daily: pd.DataFrame, idx: int):
    """IST calendar date of the daily bar at ``idx``, or ``None`` when it cannot
    be derived.

    Reads the ``timestamp`` column that historify-sourced frames always carry
    (epoch seconds, or a datetime). Synthetic test frames that lack the column
    return ``None`` so the date guard skips them — it only fires where there is
    a real timestamp to check against (production reads).
    """
    cols = getattr(bars_daily, "columns", [])
    if "timestamp" not in cols:
        return None
    try:
        ts = bars_daily.iloc[idx].get("timestamp")
        if ts is None or pd.isna(ts):
            return None
        # Convert via pandas (not ``datetime.fromtimestamp``) so the conversion
        # is independent of the module-level ``datetime`` symbol — tests
        # monkeypatch that to freeze ``now`` and it has no ``fromtimestamp``.
        if isinstance(ts, (int, float)):
            return pd.Timestamp(float(ts), unit="s", tz="UTC").tz_convert(_IST).date()
        return pd.Timestamp(ts).date()
    except Exception:
        return None


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
    # Issue #158 D2: skip index symbols silently — this is an F&O-stock rule,
    # and indices (NSE_INDEX) are subscribed for tick flow for the regime /
    # sector_follow services, never for evaluation here. Without this check,
    # every 5m bar close emits "bars_daily is None (no daily-D data)" for
    # NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY/NIFTYNXT50 → 470 daily WARNINGs.
    if indicators.get("exchange") == "NSE_INDEX":
        return False

    bars_5m = indicators.get("bars_5m")
    if bars_5m is None:
        bars_5m = bars  # rule_fn is called with the 5m frame as `bars`
    bars_15m = indicators.get("bars_15m")
    bars_daily = indicators.get("bars_daily")
    bars_weekly = indicators.get("bars_weekly")

    # --- Three-tier parameter resolution: parameters dict → env var → default ---
    p = indicators.get("parameters", {})
    buy_pct = float(p.get("gap_pct", os.environ.get("CHARTINK_RULE_BUY_GAP_PCT", "3.0")))
    atr_thresh = float(p.get("atr_pct", "5.0")) / 100.0
    vol_5m_mult = float(p.get("vol_5m_mult", "2.0"))
    rsi_thresh = float(p.get("rsi_threshold", "50.0"))
    st_period = int(p.get("supertrend_period", "7"))
    st_mult = float(p.get("supertrend_mult", "3.0"))
    price_min = float(p.get("price_min", "100.0"))
    price_max = float(p.get("price_max", "5000.0"))
    vol_sma_s = int(p.get("vol_sma_short", "50"))
    vol_sma_l = int(p.get("vol_sma_long", "200"))

    # --- Warm-up guards: insufficient history rejects (does NOT skip gates) ---
    # Daily needs vol_sma_l rows for SMA(volume, vol_sma_l) and >=2 rows for [-1]/[-2] indexing.
    # Tier-1 Fix #2: a None frame (data missing) is WARNING; a short-but-present
    # frame (warm-up) is DEBUG, so the loud signal is specifically "no data".
    sym = indicators.get("symbol", "?")
    if bars_daily is None:
        return _reject_missing(sym, "bars_daily is None (no daily-D data)")
    if len(bars_daily) < vol_sma_l:
        logger.debug(
            "fno_intraday_buy_chartink %s: daily warm-up (%d<%d rows)",
            sym,
            len(bars_daily),
            vol_sma_l,
        )
        return False
    if bars_weekly is None:
        return _reject_missing(sym, "bars_weekly is None")
    if len(bars_weekly) < 22:
        logger.debug("fno_intraday_buy_chartink %s: weekly warm-up (%d<22)", sym, len(bars_weekly))
        return False
    if bars_5m is None:
        return _reject_missing(sym, "bars_5m is None")
    if len(bars_5m) < 10:  # SMA(5m vol, 10) + Supertrend(7) warm-up
        logger.debug("fno_intraday_buy_chartink %s: 5m warm-up (%d<10)", sym, len(bars_5m))
        return False
    if bars_15m is None:
        return _reject_missing(sym, "bars_15m is None")
    if len(bars_15m) < 15:  # RSI(14) warm-up
        logger.debug("fno_intraday_buy_chartink %s: 15m warm-up (%d<15)", sym, len(bars_15m))
        return False

    # --- Today's running daily snapshot + yesterday's settled bar (Issue #197) ---
    # Production ``bars_daily`` is the ScannerHistoryProvider cache backfilled
    # from historify.duckdb at boot, and does NOT include today's bar until
    # the post-close backfill runs at 15:30-17:00 IST. The shared helper
    # ``derive_today_and_yest`` resolves the right pair regardless: it uses
    # ``iloc[-1]`` as today when its date matches (broker refreshed or
    # synthetic test frame) or aggregates today's 5m bars into a running
    # daily snapshot otherwise.
    now_ist = datetime.now(_IST)
    today_d, yest_d, yest_idx = derive_today_and_yest(bars_daily, bars_5m, now_ist)
    if today_d is None or yest_d is None:
        return _reject_missing(
            sym,
            "cannot derive today's running daily snapshot (no today 5m bars and "
            "bars_daily has no today-dated bar)",
        )

    # --- D-bar-date verify (Tier-1 Fix #1) ---
    # The stale-D defense fires when the LATEST SETTLED bar is itself older
    # than yesterday (Issue #197 reframing — see SELL rule for details).
    if yest_idx == -1 and _dbar_date_verify_enabled():
        bar_date = _daily_bar_date(bars_daily, yest_idx)
        if bar_date is not None and bar_date < now_ist.date() - timedelta(days=5):
            logger.warning(
                "fno_intraday_buy_chartink %s: latest settled daily bar is "
                "STALE (bar_date=%s > 5 days behind today=%s) — aborting",
                indicators.get("symbol", "?"),
                bar_date,
                now_ist.date(),
            )
            return False

    # Reject if any required daily field is NaN.
    if _any_nan(
        today_d.close,
        today_d.open,
        today_d.volume,
        yest_d.close,
        yest_d.high,
        yest_d.low,
    ):
        return False

    # Gate 6: daily close > price_min
    if today_d.close <= price_min:
        return False
    # Gate 12: daily close < price_max
    if today_d.close >= price_max:
        return False
    # Gate 1: daily close > 1d-ago close * buy_mult (default 3% gap). Threshold is
    # read via three-tier resolution: parameters dict → env var → hardcoded default.
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

    # Gates 2 + 8: daily volume vs SMA(vol_sma_s) and SMA(vol_sma_l). The SMA
    # is computed at the LATEST SETTLED bar (yest_idx) so the reference is a
    # function of prior settled history; today's running volume is the test
    # value compared against it.
    sma_vol_50 = sma(bars_daily["volume"], vol_sma_s).iloc[yest_idx]
    sma_vol_200 = sma(bars_daily["volume"], vol_sma_l).iloc[yest_idx]
    if _any_nan(sma_vol_50, sma_vol_200):
        return False
    if today_d.volume <= sma_vol_50:
        return False
    if today_d.volume <= sma_vol_200:
        return False

    # Gate 7: weekly ATR(21) > atr_thresh * daily close.
    # Exclude the current (potentially partial) week when we have a spare row.
    weekly_for_atr = bars_weekly.iloc[:-1] if len(bars_weekly) > 22 else bars_weekly
    weekly_atr = atr(weekly_for_atr, period=21).iloc[-1]
    if _any_nan(weekly_atr):
        return False
    if weekly_atr <= today_d.close * atr_thresh:
        return False

    # Gate 13: 5m volume > vol_5m_mult * SMA(5m vol, 10)
    sma_5m_vol_10 = sma(bars_5m["volume"], 10).iloc[-1]
    last_5m_vol = bars_5m["volume"].iloc[-1]
    if _any_nan(sma_5m_vol_10, last_5m_vol):
        return False
    if last_5m_vol <= sma_5m_vol_10 * vol_5m_mult:
        return False

    # Gate 5: 15m RSI(14) > rsi_thresh
    rsi_15m = rsi(bars_15m["close"], period=14).iloc[-1]
    if _any_nan(rsi_15m):
        return False
    if rsi_15m <= rsi_thresh:
        return False

    # Gates 3 + 4: 5m Supertrend(st_period, st_mult). [0] = iloc[-1] (current), [-1] = iloc[-2].
    st = supertrend(bars_5m, period=st_period, multiplier=st_mult)
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
