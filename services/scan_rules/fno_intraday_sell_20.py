"""SELL-side intraday rule mirroring the spirit of Chartink's
``alert-for-intraday-sell-fno`` screener.

Mirror of the BUY rule: volume surge ≥2× the 20-bar trailing average AND
close strictly *below* the 20-period EMA. Same NaN-safety and warm-up
behaviour.
"""

from __future__ import annotations

import pandas as pd

from services.scanner_service import scan_rule

_SELL_VOLUME_MULTIPLIER = 2.0


@scan_rule(
    "fno_intraday_sell_20",
    "sell",
    "Volume surge ≥2× 20-bar average AND close below 20-bar EMA.",
)
def rule(bars: pd.DataFrame, indicators: dict) -> bool:
    if len(bars) < 21:
        return False

    vol_avg = bars["volume"].rolling(20).mean().iloc[-2]
    if pd.isna(vol_avg) or vol_avg <= 0:
        return False

    vol_surge = bars["volume"].iloc[-1] / vol_avg
    if vol_surge < _SELL_VOLUME_MULTIPLIER:
        return False

    ema20 = indicators.get("ema_20")
    if ema20 is None or len(ema20) == 0:
        return False
    last_ema = ema20.iloc[-1]
    if pd.isna(last_ema):
        return False

    return bool(bars["close"].iloc[-1] < last_ema)
