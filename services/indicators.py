"""Thin indicator wrappers backed by ``pandas-ta-classic``.

Why this module exists:

* ``pandas-ta-classic`` is the community-maintained fork of pandas-ta that
  remains compatible with numpy>=2. The upstream ``pandas_ta`` package
  hasn't released a numpy-2-compatible build, and openalgo pins numpy 2.4.x.
* Keeping callers off the upstream module name (``pandas_ta_classic``)
  means we can swap the underlying library again — for example to a future
  pandas-ta release, ta-lib, or a hand-rolled implementation — without
  touching every strategy that uses an indicator.

For ATR specifically: ``pandas_ta_classic.atr`` defaults to RMA / Wilder
smoothing with an SMA seed at bar ``period`` (the textbook convention).
That is **not** quite the same as the hand-rolled ATR in
``simplified_stock_engine_core._update_atr_wilder``, which starts Wilder
smoothing immediately after the first bar. The parity test in
``test/test_indicators.py`` documents the divergence so we don't
accidentally swap one for the other in the live engine without a
deliberate decision.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta_classic as ta


def atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder / RMA smoothing, textbook convention).

    ``bars`` must have columns ``high``, ``low``, ``close``.
    """
    return ta.atr(bars["high"], bars["low"], bars["close"], length=period)


def ema(series: pd.Series, period: int = 20) -> pd.Series:
    """Exponential Moving Average."""
    return ta.ema(series, length=period)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index."""
    return ta.rsi(series, length=period)


def volume_average(series: pd.Series, period: int = 20) -> pd.Series:
    """Simple rolling mean of volume — convenience alias for clarity at call sites."""
    return series.rolling(period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average — thin wrapper around ``series.rolling(period).mean()``.

    Kept as a named helper so call sites (e.g. the Chartink BUY formula's
    ``SMA(volume, 50)`` / ``SMA(volume, 200)`` gates) stay decoupled from the
    raw pandas rolling idiom. ``volume_average`` is retained for backward
    compatibility; new code should prefer ``sma``.
    """
    return series.rolling(period).mean()


def supertrend(bars: pd.DataFrame, period: int = 7, multiplier: float = 3.0) -> pd.DataFrame:
    """Supertrend indicator (default 7, 3) backed by ``pandas-ta-classic``.

    The Supertrend bands are built from the basic upper/lower bands
    ``HL2 ± multiplier × ATR(period)`` (HL2 = (high + low) / 2), then made
    "sticky" so the active band only tightens until price closes through it,
    at which point the trend ``direction`` flips. See
    https://www.tradingview.com/support/solutions/43000634738-supertrend/

    ``bars`` must have columns ``high``, ``low``, ``close``.

    Returns a DataFrame aligned to ``bars.index`` with stable column names,
    insulating call sites from pandas-ta's ``SUPERT_{len}_{mult}`` naming:

    * ``line``       — the Supertrend line (the active band)
    * ``direction``  — trend direction, ``+1`` (up) or ``-1`` (down)
    * ``long_band``  — lower band, populated while in an uptrend
    * ``short_band`` — upper band, populated while in a downtrend
    """
    raw = ta.supertrend(
        bars["high"], bars["low"], bars["close"], length=period, multiplier=multiplier
    )
    suffix = f"_{period}_{multiplier}"
    out = pd.DataFrame(
        {
            "line": raw[f"SUPERT{suffix}"],
            "direction": raw[f"SUPERTd{suffix}"],
            "long_band": raw[f"SUPERTl{suffix}"],
            "short_band": raw[f"SUPERTs{suffix}"],
        },
        index=bars.index,
    )
    return out
