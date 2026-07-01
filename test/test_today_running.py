"""Unit tests for ``services.scan_rules._today_running.derive_today_and_yest``.

Focus: the ``ts``-vs-``timestamp`` Path-B dead-code regression (issue #203
follow-up). The LIVE in-process scanner 5m frame — built by
``ScannerService._append_bar`` — carries a ``ts`` column of **naive IST
datetimes**, NOT the ``timestamp`` epoch-seconds column historify frames use.
Path B was gated on ``"timestamp" in columns`` only, so it never engaged for
the live scanner and the rules read a FROZEN ~09:45 historify daily bar all
session.

These tests are FAST and HERMETIC (synthetic frames, no broker, no DB).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytz

from services.scan_rules._today_running import derive_today_and_yest

_IST = pytz.timezone("Asia/Kolkata")

_TODAY = datetime(2026, 6, 4, 11, 0)  # frozen "now" (11:00 IST, mid-session)
_TODAY_DATE = _TODAY.date()
_YEST_DATE = pd.Timestamp("2026-06-03").date()


# --------------------------------------------------------------------------- #
# Frame builders
# --------------------------------------------------------------------------- #
def _daily_ending_yesterday(n=10, close=2000.0):
    """Historify-style daily frame with a ``timestamp`` (epoch seconds) column
    whose LAST bar is dated ``_YEST_DATE`` (the production intra-session case:
    historify has no today-dated bar yet)."""
    dates = pd.date_range("2026-05-25", periods=n, freq="D")  # ends 2026-06-03
    ts = [int(_IST.localize(datetime(d.year, d.month, d.day, 9, 15)).timestamp()) for d in dates]
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [close] * n,
            "high": [close + 10] * n,
            "low": [close - 10] * n,
            "close": [close] * n,
            "volume": [1000.0] * n,
        }
    )


def _live_5m_frame_ts(n=12, start_close=2020.0, step=4.0, vol=500.0):
    """LIVE aggregator-style 5m frame: a ``ts`` column of NAIVE IST datetimes
    (exactly what ``ScannerService._append_bar`` produces via ``bar.get("ts")``)
    with a rising tape dated TODAY."""
    base = datetime(_TODAY_DATE.year, _TODAY_DATE.month, _TODAY_DATE.day, 9, 15)
    ts = [base + pd.Timedelta(minutes=5 * i) for i in range(n)]
    close = [start_close + step * i for i in range(n)]
    return pd.DataFrame(
        {
            "ts": ts,
            "open": close,
            "high": [c + 5 for c in close],
            "low": [c - 5 for c in close],
            "close": close,
            "volume": [vol] * n,
        }
    )


def _bare_5m_frame(n=12, close=2050.0):
    """5m frame with NO ``ts``/``timestamp`` column (synthetic test frame)."""
    return pd.DataFrame(
        {
            "open": [close] * n,
            "high": [close + 5] * n,
            "low": [close - 5] * n,
            "close": [close] * n,
            "volume": [100.0] * n,
        }
    )


# --------------------------------------------------------------------------- #
# Path B — live `ts`-column 5m frame engages (THE FIX)
# --------------------------------------------------------------------------- #
def test_path_b_engages_for_live_ts_column_frame():
    """The live 5m frame uses a ``ts`` naive-IST-datetime column. Path B must
    engage and today_d must reflect the LIVE 5m aggregate, NOT the frozen
    historify daily bar."""
    daily = _daily_ending_yesterday(close=2000.0)  # frozen bar close = 2000
    # Live tape rises 2020 → 2020 + 4*11 = 2064; last close (live) = 2064.
    bars_5m = _live_5m_frame_ts(start_close=2020.0, step=4.0)
    live_last_close = float(bars_5m["close"].iloc[-1])
    assert live_last_close == 2064.0

    today_d, yest_d, yest_idx = derive_today_and_yest(daily, bars_5m, _TODAY)

    assert today_d is not None and yest_d is not None
    # today_d.close must be the LIVE last 5m close (2064), NOT the frozen
    # historify daily close (2000).
    assert today_d.close == 2064.0
    assert today_d.close != 2000.0
    # open = first 5m bar open (2020), volume = sum of the live bars.
    assert today_d.open == 2020.0
    assert today_d.volume == 500.0 * len(bars_5m)
    # yest_d is the latest SETTLED historify bar (ends yesterday → iloc[-1]).
    assert yest_idx == -1
    assert yest_d.close == 2000.0


def test_live_frozen_proof_ts_vs_timestamp():
    """The exact regression proof: the SAME live tape returns the LIVE close
    when its timestamp column is ``ts``; a bare frame with no timestamp column
    returns the FROZEN historify daily close via Path C.
    """
    daily = _daily_ending_yesterday(close=2000.0)  # frozen bar

    # (a) `ts`-column live frame → Path B → live close.
    live = _live_5m_frame_ts(start_close=2020.0, step=4.0)
    today_live, _, _ = derive_today_and_yest(daily, live, _TODAY)
    assert today_live.close == 2064.0  # LIVE, not frozen

    # (b) same OHLCV but no timestamp column → Path C. Historify ends
    # yesterday so today_d cannot be constructed → (None, None, None).
    bare = live.drop(columns=["ts"])
    today_bare, yest_bare, idx_bare = derive_today_and_yest(daily, bare, _TODAY)
    assert (today_bare, yest_bare, idx_bare) == (None, None, None)


# --------------------------------------------------------------------------- #
# `timestamp`-column frames stay green (regression protection)
# --------------------------------------------------------------------------- #
def test_timestamp_epoch_column_still_works():
    """Historify epoch-seconds ``timestamp`` 5m frames (the pre-existing test
    shape) still engage Path B."""
    daily = _daily_ending_yesterday(close=2000.0)
    live = _live_5m_frame_ts(start_close=2020.0, step=4.0)
    # Convert the `ts` datetime column to an epoch-seconds `timestamp` column.
    epoch = [int(_IST.localize(t).timestamp()) for t in live["ts"]]
    live_ts = live.drop(columns=["ts"]).assign(timestamp=epoch)

    today_d, yest_d, yest_idx = derive_today_and_yest(daily, live_ts, _TODAY)
    assert today_d is not None
    assert today_d.close == 2064.0
    assert yest_idx == -1


# --------------------------------------------------------------------------- #
# Bare frame (no ts/timestamp) — Path A / Path C fallback intact
# --------------------------------------------------------------------------- #
def test_bare_daily_frame_path_a():
    """No timestamp column on the DAILY frame → Path A: iloc[-1] = today,
    iloc[-2] = yesterday (preserves legacy unit-test behaviour)."""
    daily = pd.DataFrame(
        {
            "open": [2000.0] * 5 + [2050.0],
            "high": [2010.0] * 5 + [2110.0],
            "low": [1990.0] * 5 + [2040.0],
            "close": [2000.0] * 5 + [2100.0],
            "volume": [1000.0] * 5 + [5000.0],
        }
    )
    bare_5m = _bare_5m_frame()
    today_d, yest_d, yest_idx = derive_today_and_yest(daily, bare_5m, _TODAY)
    assert yest_idx == -2
    assert today_d.close == 2100.0  # iloc[-1]
    assert yest_d.close == 2000.0  # iloc[-2]


def test_bare_5m_with_dated_daily_falls_to_path_c():
    """Daily has a ``timestamp`` today-dated bar but the 5m frame has no
    timestamp column → Path C uses iloc[-1] (today) / iloc[-2] (yesterday)."""
    dates = pd.date_range("2026-05-26", periods=10, freq="D")  # ends 2026-06-04 (today)
    ts = [int(_IST.localize(datetime(d.year, d.month, d.day, 9, 15)).timestamp()) for d in dates]
    daily = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [2000.0] * 9 + [2050.0],
            "high": [2010.0] * 9 + [2110.0],
            "low": [1990.0] * 9 + [2040.0],
            "close": [2000.0] * 9 + [2100.0],
            "volume": [1000.0] * 10,
        }
    )
    today_d, yest_d, yest_idx = derive_today_and_yest(daily, _bare_5m_frame(), _TODAY)
    assert yest_idx == -2
    assert today_d.close == 2100.0  # today-dated iloc[-1]
    assert yest_d.close == 2000.0
