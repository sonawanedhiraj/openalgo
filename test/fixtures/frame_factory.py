"""Production-shaped OHLCV frame factory for scanner tests (issue #306).

Why this module exists
-----------------------
Existing scanner rule tests (``test/test_fno_intraday_buy_chartink.py`` and
friends) build DataFrames with a bare ``open/high/low/close/volume`` shape —
**no timestamp column at all**. That was a deliberate legacy choice (see
``services/scan_rules/_today_running.py`` Path A) to keep the original gate
tests simple, but it means the happy-path suite has never exercised the code
paths that only activate when a *real* timestamp column is present:

* ``derive_today_and_yest``'s Path B (live 5m aggregation) only engages when
  the 5m frame carries a timestamp column — either the live scanner's ``ts``
  (naive IST ``datetime``) or historify's ``timestamp`` (epoch seconds). A
  frame with neither column silently takes Path A/C instead. PR #279 shipped
  a fix for Path B recognising ``ts`` — but its own tests (rightly) used
  ``timestamp``-column frames to describe the historify shape, and the
  synthetic gate-logic tests never touched a live-shaped frame at all. The
  net effect: Path B could regress to "dead" again and the full unit suite
  would stay green (issue #278 background: the original ``"timestamp" in
  columns`` check never matched the live scanner's ``ts``-column frame in
  production, even though it matched every historify-shaped test fixture).
* ``_daily_bar_date`` in ``services/scan_rules/fno_intraday_buy_chartink.py``
  (and the SELL mirror) reads the daily frame's ``timestamp`` column to run
  the post-settle staleness guard. Without that column the guard returns
  ``None`` and is SKIPPED — so any happy-path test that omits it is
  (unintentionally) running with the staleness guard turned off.

**The ts-vs-timestamp trap:** the live in-process 5m aggregator
(``ScannerService._append_bar``) emits a ``ts`` column of **naive IST
datetimes**. Historify-sourced frames (``ScannerHistoryProvider`` daily/
weekly, and any historify-backed 5m read) emit a ``timestamp`` column of
**epoch seconds** (int64). These are NOT interchangeable column names and
code that checks for one will silently miss frames shaped like the other.
See references #278, #279 for the original discovery and fix.

This factory exists so new tests build frames that carry the *correct*
column for their claimed source, by construction — a test author who wants
"the live 5m frame" gets a ``ts`` column; a test author who wants "the
historify daily frame" gets a ``timestamp`` column. Getting this backwards is
exactly the bug class that let PR #279's Path B ship dead in production
while its own tests passed.

Scope note
----------
This module is additive — it does NOT touch the existing bare-frame builders
in ``test/test_fno_intraday_{buy,sell}_chartink.py`` (``make_daily_bars`` /
``make_5m_bars`` / etc.). Migrating those is a follow-up (see issue #306
scope). New tests — especially golden incident-replay tests under
``test/golden_scanner/`` — should prefer this factory.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytz

_IST = pytz.timezone("Asia/Kolkata")

# Columns every frame this factory builds must carry, keyed by frame kind.
# Used by the self-check below and by test/golden_scanner/test_fixture_shape_inventory.py.
LIVE_5M_TS_COLUMN = "ts"
HISTORIFY_TS_COLUMN = "timestamp"


def _ist_midnight(d) -> datetime:
    """Naive-IST midnight for calendar date ``d`` (accepts ``date`` or ``datetime``)."""
    if isinstance(d, datetime):
        d = d.date()
    return datetime(d.year, d.month, d.day)


def _self_check_has_ts_column(frame: pd.DataFrame, col: str, builder_name: str) -> None:
    """Assert ``frame`` carries ``col`` — the self-check every builder below runs on
    its own output before returning it (issue #306 acceptance: "all frames it
    makes carry their timestamp columns"). Raising here means a bug in THIS
    factory, not in caller code, so it is deliberately an ``assert`` (fail fast,
    fail loud) rather than a silent skip.
    """
    assert col in frame.columns, (
        f"frame_factory.{builder_name} produced a frame without a '{col}' column "
        f"— this is a bug in the factory itself, not caller code. Columns present: "
        f"{list(frame.columns)}"
    )


def make_live_5m_frame(
    closes: list[float],
    date,
    *,
    start_time: str = "09:15",
    step_minutes: int = 5,
    volumes: list[float] | float = 1000.0,
    high_pad: float = 5.0,
    low_pad: float = 5.0,
    opens: list[float] | None = None,
) -> pd.DataFrame:
    """Build a live-scanner-shaped 5m frame: ``ts`` (naive IST datetime) +
    open/high/low/close/volume — EXACTLY the shape
    ``ScannerService._append_bar`` produces (see ``services/scanner_service.py``
    ``_append_bar``, which builds each row as
    ``{"ts": bar.get("ts"), "open": ..., "high": ..., "low": ..., "close": ...,
    "volume": ...}``) and what ``derive_today_and_yest``'s Path B keys off via
    ``_TS_COLS = ("timestamp", "ts")`` (``services/scan_rules/_today_running.py``).

    Args:
        closes: per-bar close prices, in chronological order. Bar count is
            ``len(closes)``.
        date: calendar date (``date`` or ``datetime``) the whole tape is dated —
            all bars fall on this single IST day, ``step_minutes`` apart
            starting at ``start_time``.
        start_time: ``"HH:MM"`` of the first bar (IST, naive). Default 09:15 —
            the NSE F&O session open.
        step_minutes: minutes between bars (default 5, matching the interval
            name).
        volumes: per-bar volume, or a single float applied to every bar.
        high_pad / low_pad: symmetric padding around close for high/low.
        opens: per-bar open; defaults to each bar's own close (a doji) unless
            given — pass a shifted list to model a directional bar.

    Returns:
        DataFrame with columns ``ts, open, high, low, close, volume`` — ``ts``
        is a naive (tz-unaware) ``datetime64`` column, matching the live
        aggregator's naive-IST convention (NOT epoch seconds — see the
        ts-vs-timestamp trap in the module docstring).
    """
    n = len(closes)
    hh, mm = (int(x) for x in start_time.split(":"))
    base = _ist_midnight(date) + timedelta(hours=hh, minutes=mm)
    ts = [base + timedelta(minutes=step_minutes * i) for i in range(n)]
    vol = list(volumes) if isinstance(volumes, list) else [volumes] * n
    op = opens if opens is not None else list(closes)
    frame = pd.DataFrame(
        {
            "ts": ts,
            "open": op,
            "high": [c + high_pad for c in closes],
            "low": [c - low_pad for c in closes],
            "close": list(closes),
            "volume": vol,
        }
    )
    _self_check_has_ts_column(frame, LIVE_5M_TS_COLUMN, "make_live_5m_frame")
    return frame


def make_historify_daily_frame(
    closes: list[float],
    end_date,
    *,
    volumes: list[float] | float = 1_000_000.0,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
    session_time: str = "09:15",
) -> pd.DataFrame:
    """Build a historify-shaped daily frame: ``timestamp`` (epoch seconds,
    int64) + open/high/low/close/volume — the shape
    ``database.historify_db.get_ohlcv`` returns and ``ScannerHistoryProvider``
    caches for ``bars_daily`` (see ``database/historify_db.py`` docstring:
    "DataFrame with columns: timestamp, open, high, low, close, volume, oi").

    A convenience one-liner: "N daily bars ending ``end_date`` with last close
    ``closes[-1]``" — pass ``closes`` as a list and the frame is dated so the
    LAST bar lands on ``end_date`` and earlier bars step back one calendar day
    each (weekends are NOT skipped — this models calendar days, not trading
    days; pass a `pd.bdate_range`-derived closes list if trading-day spacing
    matters for a specific test).

    Args:
        closes: per-bar close prices, oldest first, so ``closes[-1]`` is the
            close of the LAST (most recent / ``end_date``-dated) bar.
        end_date: calendar date (``date`` or ``datetime``) of the last bar.
        volumes: per-bar volume, or a single float applied to every bar.
        highs / lows / opens: optional per-bar overrides; default to a flat
            +/-0.5% band around each bar's close (open = close).
        session_time: IST time-of-day stamped onto each bar (historify stamps
            the session open, 09:15 IST, by convention).

    Returns:
        DataFrame with columns ``timestamp, open, high, low, close, volume`` —
        ``timestamp`` is int64 epoch seconds (NOT a datetime column — see the
        ts-vs-timestamp trap in the module docstring; this is what makes a
        historify frame "historify-shaped" rather than "live-shaped").
    """
    n = len(closes)
    hh, mm = (int(x) for x in session_time.split(":"))
    dates = [_ist_midnight(end_date) - timedelta(days=(n - 1 - i)) for i in range(n)]
    ts = [int(_IST.localize(d.replace(hour=hh, minute=mm)).timestamp()) for d in dates]
    vol = list(volumes) if isinstance(volumes, list) else [volumes] * n
    op = opens if opens is not None else list(closes)
    hi = highs if highs is not None else [c * 1.005 for c in closes]
    lo = lows if lows is not None else [c * 0.995 for c in closes]
    frame = pd.DataFrame(
        {
            "timestamp": pd.array(ts, dtype="int64"),
            "open": op,
            "high": hi,
            "low": lo,
            "close": list(closes),
            "volume": vol,
        }
    )
    _self_check_has_ts_column(frame, HISTORIFY_TS_COLUMN, "make_historify_daily_frame")
    return frame


def make_15m_frame(
    closes: list[float],
    date,
    *,
    start_time: str = "09:15",
    step_minutes: int = 15,
    volumes: list[float] | float = 1000.0,
    high_pad: float = 2.0,
    low_pad: float = 2.0,
) -> pd.DataFrame:
    """Build a live-shaped 15m frame — the shape
    ``ScannerService._Rolling15mBars.get_recent_bars`` returns (``ts, open,
    high, low, close, volume`` — see ``services/scanner_service.py``
    ``_Rolling15mBars._on_bar``, which stashes ``"ts": bar.get("ts")`` per
    closed 15m bar).

    Same call shape as :func:`make_live_5m_frame` with a 15-minute default
    step; both rules' 15m RSI(14) gate reads only ``close``.
    """
    return make_live_5m_frame(
        closes,
        date,
        start_time=start_time,
        step_minutes=step_minutes,
        volumes=volumes,
        high_pad=high_pad,
        low_pad=low_pad,
    )


def make_weekly_frame(
    closes: list[float],
    end_date,
    *,
    volumes: list[float] | float = 5_000_000.0,
    range_pad: float = 100.0,
) -> pd.DataFrame:
    """Build a weekly frame matching ``ScannerHistoryProvider.get_weekly`` —
    same ``timestamp``-epoch-seconds shape as the daily frame (historify
    resamples daily bars to weekly and the resulting frame carries the same
    column contract), spaced 7 calendar days apart ending ``end_date``.

    Only ``close`` (RSI is NOT read here) and ``high``/``low`` (ATR(21), the
    BUY/SELL Gate 7 reference) matter to the rules; volume is present for
    shape-completeness but unused by either rule's weekly gate.
    """
    n = len(closes)
    dates = [_ist_midnight(end_date) - timedelta(weeks=(n - 1 - i)) for i in range(n)]
    ts = [int(_IST.localize(d.replace(hour=9, minute=15)).timestamp()) for d in dates]
    vol = list(volumes) if isinstance(volumes, list) else [volumes] * n
    frame = pd.DataFrame(
        {
            "timestamp": pd.array(ts, dtype="int64"),
            "open": list(closes),
            "high": [c + range_pad for c in closes],
            "low": [c - range_pad for c in closes],
            "close": list(closes),
            "volume": vol,
        }
    )
    _self_check_has_ts_column(frame, HISTORIFY_TS_COLUMN, "make_weekly_frame")
    return frame


def flat_closes(n: int, close: float) -> list[float]:
    """``n`` repetitions of ``close`` — convenience for "flat history" setups."""
    return [close] * n


def ramp_closes(n: int, start: float, step: float) -> list[float]:
    """Linear ramp of ``n`` closes from ``start``, incrementing ``step`` per bar —
    convenience for a monotone rising/falling tape (drives RSI toward its
    extreme and gives Supertrend a clean directional read)."""
    return [start + step * i for i in range(n)]
