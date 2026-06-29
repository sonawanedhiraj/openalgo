"""Today's-running-daily snapshot helper shared by the Chartink BUY/SELL rules.

Issue #197 — both rules previously resolved ``today_d`` from
``bars_daily.iloc[-1]`` (assumed to be today's forming bar) or ``iloc[-2]``
(assumed to be yesterday's settled bar, pre-15:31). In production
``bars_daily`` is the ``ScannerHistoryProvider`` cache, which is backfilled
from ``historify.duckdb`` at boot and **does not include today's bar
during the trading session** (the post-close backfill writes today's D bar
at 15:30-17:00 IST). The off-by-one shift meant the rules silently compared
*Thursday vs Wednesday* instead of *today vs yesterday* — invisible to the
session's actual price action.

This helper resolves ``today_d`` and ``yest_d`` from whatever data is
actually present:

1. If ``bars_daily.iloc[-1]`` carries a ``timestamp`` column AND its IST
   date == today's IST date, treat that bar as today's running snapshot
   (broker refreshed it) and ``iloc[-2]`` as yesterday.
2. Otherwise — including the synthetic-frame case where no ``timestamp``
   column is present (existing unit tests) — derive today's running OHLCV
   by aggregating today's 5m bars: ``open`` = first 5m bar's open,
   ``close`` = last 5m bar's close, ``high``/``low``/``volume`` aggregated.
   ``yest_d`` is then ``bars_daily.iloc[-1]`` (the latest settled bar).
3. The synthetic-frame branch (no ``timestamp`` column) preserves the
   pre-#197 test behavior: ``iloc[-1]`` is treated as today, ``iloc[-2]``
   as yesterday. This keeps every existing unit test in
   ``test/test_fno_intraday_{buy,sell}_chartink.py`` green.

Volume SMA computations should use ``yest_idx`` (returned alongside) so
they cover the prior ``vol_sma_l`` SETTLED days, never including today's
running volume.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytz

_IST = pytz.timezone("Asia/Kolkata")


def _daily_bar_date(bars_daily: pd.DataFrame, idx: int):
    """IST calendar date of the daily bar at ``idx``, or ``None`` when the
    frame carries no ``timestamp`` column (synthetic test frames).
    """
    if "timestamp" not in getattr(bars_daily, "columns", []):
        return None
    try:
        ts = bars_daily.iloc[idx].get("timestamp")
        if ts is None or pd.isna(ts):
            return None
        if isinstance(ts, (int, float)):
            return pd.Timestamp(float(ts), unit="s", tz="UTC").tz_convert(_IST).date()
        return pd.Timestamp(ts).date()
    except Exception:
        return None


def _today_5m_subset(bars_5m: pd.DataFrame, today_date) -> pd.DataFrame:
    """Slice ``bars_5m`` to bars dated ``today_date`` IST.

    If the frame carries no ``timestamp`` column (synthetic test frames),
    treat the entire frame as "today" — same fallback used by
    ``_daily_bar_date``.
    """
    if "timestamp" not in getattr(bars_5m, "columns", []):
        return bars_5m
    ts = bars_5m["timestamp"]
    if ts.empty:
        return bars_5m.iloc[0:0]
    dt = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert(_IST)
    mask = dt.dt.date == today_date
    return bars_5m[mask]


def derive_today_and_yest(
    bars_daily: pd.DataFrame,
    bars_5m: pd.DataFrame,
    now_ist: datetime | None = None,
) -> tuple[pd.Series | None, pd.Series | None, int | None]:
    """Resolve ``(today_d, yest_d, yest_idx)``.

    Returns ``(None, None, None)`` when today's running snapshot cannot be
    constructed (callers should reject the symbol loudly via their own
    ``_reject_missing`` helper so the supply gap is visible).

    ``yest_idx`` is the integer index into ``bars_daily`` of the
    yesterday-settled bar, used by volume-SMA computations that should
    cover prior settled history rather than include today's running
    volume.
    """
    if bars_daily is None or len(bars_daily) < 2:
        return None, None, None

    if now_ist is None:
        now_ist = datetime.now(_IST)
    today_date = now_ist.date()
    last_d_date = _daily_bar_date(bars_daily, -1)

    # Branch 1 — synthetic test frame (no timestamp column) OR broker
    # already refreshed today's bar into bars_daily.iloc[-1].
    if last_d_date is None or last_d_date == today_date:
        return bars_daily.iloc[-1], bars_daily.iloc[-2], -2

    # Branch 2 — bars_daily ends at yesterday or earlier (the production
    # path during the trading session). Derive today's running OHLCV from
    # today's 5m bars.
    today_5m = _today_5m_subset(bars_5m, today_date) if bars_5m is not None else None
    if today_5m is None or len(today_5m) == 0:
        return None, None, None

    today_d = pd.Series(
        {
            "open": float(today_5m["open"].iloc[0]),
            "high": float(today_5m["high"].max()),
            "low": float(today_5m["low"].min()),
            "close": float(today_5m["close"].iloc[-1]),
            "volume": float(today_5m["volume"].sum()),
        }
    )
    return today_d, bars_daily.iloc[-1], -1
