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
