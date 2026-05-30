"""BUY-side intraday rule mirroring the spirit of Chartink's
``fno-intraday-buy-20`` screener.

Placeholder logic (operators will tune the thresholds against shadow-mode
output before this is ever wired to the engine):

* Latest bar's volume ≥ 2× the trailing 20-bar average volume.
* Latest bar's close is strictly above the 20-period EMA.

Both gates must clear. Insufficient history (fewer than 21 bars) → ``False``.
NaN-safe: a NaN average or EMA returns ``False`` rather than raising.
"""

from __future__ import annotations

import pandas as pd

from services.scanner_service import scan_rule

_BUY_VOLUME_MULTIPLIER = 2.0


@scan_rule(
    "fno_intraday_buy_20",
    "buy",
    "Volume surge ≥2× 20-bar average AND close above 20-bar EMA.",
)
def rule(bars: pd.DataFrame, indicators: dict) -> bool:
    if len(bars) < 21:
        return False

    # Trailing 20-bar average — use up-to-but-not-including the latest bar
    # so the surge ratio compares the closing bar against its history,
    # not against a window that already includes itself.
    vol_avg = bars["volume"].rolling(20).mean().iloc[-2]
    if pd.isna(vol_avg) or vol_avg <= 0:
        return False

    vol_surge = bars["volume"].iloc[-1] / vol_avg
    if vol_surge < _BUY_VOLUME_MULTIPLIER:
        return False

    ema20 = indicators.get("ema_20")
    if ema20 is None or len(ema20) == 0:
        return False
    last_ema = ema20.iloc[-1]
    if pd.isna(last_ema):
        return False

    return bool(bars["close"].iloc[-1] > last_ema)
