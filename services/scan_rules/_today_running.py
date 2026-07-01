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

1. If ``bars_daily.iloc[-1]`` carries a ``timestamp``/``ts`` column AND its
   IST date == today's IST date, treat that bar as today's running snapshot
   (broker refreshed it) and ``iloc[-2]`` as yesterday.
2. Otherwise — including the synthetic-frame case where no timestamp
   column is present (existing unit tests) — derive today's running OHLCV
   by aggregating today's 5m bars: ``open`` = first 5m bar's open,
   ``close`` = last 5m bar's close, ``high``/``low``/``volume`` aggregated.
   ``yest_d`` is then ``bars_daily.iloc[-1]`` (the latest settled bar).
3. The synthetic-frame branch (no timestamp column) preserves the
   pre-#197 test behavior: ``iloc[-1]`` is treated as today, ``iloc[-2]``
   as yesterday. This keeps every existing unit test in
   ``test/test_fno_intraday_{buy,sell}_chartink.py`` green.

The timestamp column is named differently by source: ``timestamp`` (historify
frames, epoch seconds) vs ``ts`` (the live in-process 5m aggregator frame,
a naive IST ``datetime``). Path B recognises BOTH — the live scanner's 5m
frame uses ``ts``, and matching only ``timestamp`` left Path B dead so the
rules read a FROZEN ~09:45 historify daily bar all session (issue #203).

Volume SMA computations should use ``yest_idx`` (returned alongside) so
they cover the prior ``vol_sma_l`` SETTLED days, never including today's
running volume.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytz

_IST = pytz.timezone("Asia/Kolkata")

# The daily/5m frame's timestamp column is named differently by source:
#   * ``timestamp`` — historify-sourced frames (``ScannerHistoryProvider``),
#     epoch seconds (or a datetime).
#   * ``ts`` — the live in-process 5m aggregator frame built by
#     ``ScannerService._append_bar`` (``bar.get("ts")``), a **naive IST
#     datetime**, NOT epoch seconds.
# Path B must recognise EITHER column so the live scanner's ``ts``-column 5m
# frame is used (issue #203 follow-up: the ``ts``-vs-``timestamp`` mismatch
# left Path B dead, so the rules read the frozen historify daily bar).
_TS_COLS = ("timestamp", "ts")


def _ts_col(frame: pd.DataFrame) -> str | None:
    """Return the name of whichever timestamp column is present (``timestamp``
    preferred over ``ts``), or ``None`` when neither is present (synthetic
    test frames)."""
    cols = getattr(frame, "columns", [])
    for name in _TS_COLS:
        if name in cols:
            return name
    return None


def _to_ist_date(ts):
    """IST calendar date of a single timestamp scalar (epoch seconds OR a
    naive/aware datetime), or ``None`` if it cannot be parsed.

    The numeric-vs-datetime branch is robust against numpy integer types
    (issue #203): ``isinstance(np.int64, int)`` returns False under some
    NumPy / pandas combinations, which previously sent epoch-second
    integers down the ``pd.Timestamp(ts)`` path that interprets them as
    nanoseconds — yielding 1970-01-01. Use ``float(ts)`` with a fallback
    so any integer-like scalar (int, np.int64, np.int32, …) routes to
    the seconds-since-epoch path. A ``datetime`` (the live ``ts`` column)
    is a ``ValueError``/``TypeError`` on ``float()`` so it falls through to
    the datetime branch, where a naive value is treated as already IST.
    """
    if ts is None or pd.isna(ts):
        return None
    try:
        return pd.Timestamp(float(ts), unit="s", tz="UTC").tz_convert(_IST).date()
    except (TypeError, ValueError):
        stamp = pd.Timestamp(ts)
        return stamp.date()


def _daily_bar_date(bars_daily: pd.DataFrame, idx: int):
    """IST calendar date of the daily bar at ``idx``, or ``None`` when the
    frame carries no ``timestamp``/``ts`` column (synthetic test frames)."""
    col = _ts_col(bars_daily)
    if col is None:
        return None
    try:
        return _to_ist_date(bars_daily.iloc[idx].get(col))
    except Exception:
        return None


def _today_5m_subset(bars_5m: pd.DataFrame, today_date) -> pd.DataFrame:
    """Slice ``bars_5m`` to bars dated ``today_date`` IST.

    Recognises either the ``timestamp`` (historify, epoch seconds) or the
    ``ts`` (live aggregator, naive IST datetime) column. If the frame carries
    neither (synthetic test frames), treat the entire frame as "today" — same
    fallback used by ``_daily_bar_date``.
    """
    col = _ts_col(bars_5m)
    if col is None:
        return bars_5m
    ts = bars_5m[col]
    if ts.empty:
        return bars_5m.iloc[0:0]
    if pd.api.types.is_numeric_dtype(ts):
        # Historify ``timestamp`` column — epoch seconds.
        dt = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert(_IST)
    else:
        # Live aggregator ``ts`` column — naive IST datetimes (or a mix).
        dt = pd.to_datetime(ts)
    mask = dt.dt.date == today_date
    return bars_5m[mask]


def derive_today_and_yest(
    bars_daily: pd.DataFrame,
    bars_5m: pd.DataFrame,
    now_ist: datetime | None = None,
) -> tuple[pd.Series | None, pd.Series | None, int | None]:
    """Resolve ``(today_d, yest_d, yest_idx)``.

    Resolution order (issue #203):

    1. **Synthetic test frame** (no ``timestamp`` column on ``bars_daily``):
       trust ``iloc[-1]`` as today, ``iloc[-2]`` as yesterday. This keeps
       every unit test in ``test/test_fno_intraday_{buy,sell}_chartink.py``
       green — they pass DataFrames without timestamps.
    2. **Live 5m derivation** whenever today's 5m bars exist. The live 5m
       aggregator is updated by every WS tick, so ``close`` = the latest
       LTP. ``yest_d`` is the latest SETTLED bar in ``bars_daily``
       (``iloc[-1]`` if its date is < today, else ``iloc[-2]``).
    3. **Frozen historify fallback**: only when 5m has no today bars
       (overnight / pre-open). Use ``iloc[-1]`` if its date is today.

    Why prefer 5m over historify even when historify HAS a today bar:
    ``ScannerHistoryProvider`` refreshes ``bars_daily`` once at boot and
    caches it. During the session that cache is FROZEN — any stock that
    was down at boot looks "still down" for the rest of the day even if
    it recovers. The 15:10 IST 2026-06-29 incident: 41 SELL hits, only
    7 confirmed real by direct broker check. TCS fired SELL while live
    LTP was +0.41% UP — its ``today_d.close`` was a frozen 14:28 snapshot.
    See issue #203.

    Returns ``(None, None, None)`` when today's running snapshot cannot
    be constructed (callers should reject the symbol loudly via their
    own ``_reject_missing`` helper so the supply gap is visible).

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

    # Path A — synthetic test frame (no timestamp column on daily). Trust
    # iloc[-1] as today, iloc[-2] as yesterday. Preserves existing unit
    # tests that pass DataFrames without a `timestamp` column.
    if last_d_date is None:
        return bars_daily.iloc[-1], bars_daily.iloc[-2], -2

    # Path B (production) — 5m-derived today_d. Triggered when bars_5m carries
    # EITHER a `timestamp` (historify epoch-seconds) OR a `ts` (live aggregator
    # naive-IST-datetime) column. The live scanner's 5m frame — built by
    # ScannerService._append_bar — uses `ts`, NOT `timestamp`; only matching
    # `timestamp` left this path DEAD for the live scanner, so the rules read
    # the FROZEN historify daily bar via Path C (issue #203 follow-up).
    # Synthetic test 5m frames without either column fall through to Path C so
    # the daily-only test scenarios keep their pre-#203 behaviour.
    has_5m_timestamps = bars_5m is not None and _ts_col(bars_5m) is not None
    today_5m = _today_5m_subset(bars_5m, today_date) if has_5m_timestamps else None
    if today_5m is not None and len(today_5m) > 0:
        today_d = pd.Series(
            {
                "open": float(today_5m["open"].iloc[0]),
                "high": float(today_5m["high"].max()),
                "low": float(today_5m["low"].min()),
                "close": float(today_5m["close"].iloc[-1]),
                "volume": float(today_5m["volume"].sum()),
            }
        )
        # yest_d is the latest SETTLED bar — iloc[-1] if it's strictly
        # before today, otherwise iloc[-2] (when historify already has a
        # today-dated bar, e.g., post-close).
        if last_d_date < today_date:
            return today_d, bars_daily.iloc[-1], -1
        return today_d, bars_daily.iloc[-2], -2

    # Path C — fallback to historify's iloc[-1] only when 5m has no today
    # bars (overnight / pre-open). This is exercised only outside market
    # hours; during a live session Path B always wins.
    if last_d_date == today_date:
        return bars_daily.iloc[-1], bars_daily.iloc[-2], -2

    # No live 5m AND historify ends at yesterday-or-earlier → cannot
    # construct today_d safely.
    return None, None, None
