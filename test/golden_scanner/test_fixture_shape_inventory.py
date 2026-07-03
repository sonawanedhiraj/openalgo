"""Shape-inventory check: does ``test/fixtures/frame_factory.py`` actually
match the live producer shapes it claims to model? (issue #306, scope item 3)

Design note — why this, not a global conftest warn-hook
---------------------------------------------------------
The issue asks for *some* robust check that the factory's frames match
production shapes, with an explicit steer away from a global conftest hook
that inspects every OTHER test's frames: that shape of check is exactly the
kind of thing that looks harmless and then flakes/over-fires the moment any
unrelated test builds an ad hoc frame with a slightly different column
order, dtype, or a deliberately malformed frame (several existing tests in
``test/test_fno_intraday_buy_chartink.py`` and
``test/test_today_running.py`` intentionally build frames MISSING the
timestamp column, or with only some columns, to test the fallback paths —
a global hook would need a maintained exemption list forever).

Instead this is a **standalone, targeted inventory test**: it exercises the
REAL producer code paths where practical and compares column sets — a
one-time, explicit, low-maintenance check rather than an always-on hook.

Two producers, two verification strategies:

* **Live 5m frame** (``ScannerService._append_bar``): this is a pure
  in-memory method — no ZMQ socket, no DB, no thread needed to construct a
  ``ScannerService`` and call it directly (see
  ``test/test_scanner_service.py``'s ``_seed_history`` for the same
  pattern). So this test drives the ACTUAL production method and diffs its
  output columns against ``make_live_5m_frame``'s — the strongest form of
  this check.
* **Historify daily/weekly frame** (``database.historify_db.get_ohlcv`` /
  ``ScannerHistoryProvider``): reading real rows needs a populated DuckDB
  file, which is out of scope for a hermetic unit test (and would need
  broker access to backfill — see CLAUDE.md's historify backfill sections).
  Executing the DB read path isn't practical here, so this half instead
  asserts the DOCUMENTED contract: ``database/historify_db.py``'s
  ``get_ohlcv`` docstring states the return columns explicitly
  ("DataFrame with columns: timestamp, open, high, low, close, volume, oi")
  — this test asserts the factory's column set is a SUBSET of that
  documented contract (the factory omits ``oi``, which neither scan rule
  reads, by design) and locks the ``timestamp``/``ts`` naming so a future
  contract change either updates this test loudly or is a genuine
  regression.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from services.scanner_service import ScannerService
from test.fixtures.frame_factory import (
    HISTORIFY_TS_COLUMN,
    LIVE_5M_TS_COLUMN,
    make_15m_frame,
    make_historify_daily_frame,
    make_live_5m_frame,
    make_weekly_frame,
)


class _NullBus:
    """No-op event bus — ``ScannerService.__init__`` requires one but this
    test never calls ``.start()`` or publishes anything."""

    def publish(self, event) -> None:  # noqa: ANN001 — matches EventBus.publish signature
        pass


def test_live_5m_factory_matches_scanner_append_bar_columns():
    """Drive the REAL ``ScannerService._append_bar`` (the code path that
    builds the live scanner's in-memory 5m/15m rolling frames) and assert
    its output columns are EXACTLY what ``make_live_5m_frame`` produces.

    This is the strongest form of the shape check: it is not a description
    of the production shape, it IS the production method.
    """
    svc = ScannerService(symbols=["RELIANCE"], bus=_NullBus())
    bar = {
        "ts": dt.datetime(2026, 6, 4, 9, 15),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000,
    }
    produced = svc._append_bar("RELIANCE", "5m", bar)

    factory = make_live_5m_frame([100.5], dt.date(2026, 6, 4))

    assert set(produced.columns) == set(factory.columns), (
        f"ScannerService._append_bar columns {sorted(produced.columns)} != "
        f"frame_factory.make_live_5m_frame columns {sorted(factory.columns)} — "
        "the factory has drifted from the live producer. Update "
        "test/fixtures/frame_factory.py::make_live_5m_frame to match."
    )
    assert LIVE_5M_TS_COLUMN in produced.columns
    # The live producer's `ts` values are naive datetimes (or whatever the
    # caller passed as bar["ts"]) — never epoch seconds. Guard the dtype
    # family, not the exact dtype (pandas may box a single-row datetime
    # column as object or datetime64 depending on the input).
    sample_ts = produced[LIVE_5M_TS_COLUMN].iloc[0]
    assert isinstance(sample_ts, (dt.datetime, pd.Timestamp)), (
        f"expected a datetime-like ts value, got {type(sample_ts)}"
    )


def test_15m_factory_matches_append_bar_columns_too():
    """``make_15m_frame`` reuses ``make_live_5m_frame`` under the hood — the
    live 15m rolling frame (``ScannerService._Rolling15mBars``) carries the
    identical column shape (see its ``_on_bar`` construction), so the same
    ``_append_bar``-derived comparison applies."""
    svc = ScannerService(symbols=["RELIANCE"], bus=_NullBus())
    bar = {
        "ts": dt.datetime(2026, 6, 4, 9, 15),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000,
    }
    produced = svc._append_bar("RELIANCE", "15m", bar)
    factory = make_15m_frame([100.5], dt.date(2026, 6, 4))
    assert set(produced.columns) == set(factory.columns)


# --------------------------------------------------------------------------- #
# Historify-sourced frames: compared against the documented contract (see
# module docstring for why the real DB read path isn't exercised here).
# --------------------------------------------------------------------------- #

# The exact set documented by ``database/historify_db.get_ohlcv``'s docstring:
# "DataFrame with columns: timestamp, open, high, low, close, volume, oi".
_HISTORIFY_DOCUMENTED_COLUMNS = {"timestamp", "open", "high", "low", "close", "volume", "oi"}


def test_historify_daily_factory_is_subset_of_documented_contract():
    """``make_historify_daily_frame``'s columns must be a subset of
    ``database.historify_db.get_ohlcv``'s documented return contract, and
    must use the ``timestamp`` (not ``ts``) column name — the exact
    distinction that caused issues #278/#279 (Path B silently not engaging
    for historify-shaped frames that used the wrong column name)."""
    frame = make_historify_daily_frame([100.0, 101.0, 102.0], end_date=dt.date(2026, 6, 4))
    assert set(frame.columns).issubset(_HISTORIFY_DOCUMENTED_COLUMNS), (
        f"make_historify_daily_frame columns {sorted(frame.columns)} include a "
        f"column not in the documented historify_db.get_ohlcv contract "
        f"{sorted(_HISTORIFY_DOCUMENTED_COLUMNS)}"
    )
    assert HISTORIFY_TS_COLUMN in frame.columns
    assert LIVE_5M_TS_COLUMN not in frame.columns  # must NOT carry the live `ts` name
    # timestamp must be epoch-seconds-shaped (numeric), never a datetime —
    # this is the exact property `_today_running._today_5m_subset` branches
    # on to distinguish historify frames from live-aggregator frames.
    assert pd.api.types.is_numeric_dtype(frame[HISTORIFY_TS_COLUMN])


def test_historify_weekly_factory_is_subset_of_documented_contract():
    """Same contract check for the weekly frame builder."""
    frame = make_weekly_frame([100.0] * 25, end_date=dt.date(2026, 6, 4))
    assert set(frame.columns).issubset(_HISTORIFY_DOCUMENTED_COLUMNS)
    assert HISTORIFY_TS_COLUMN in frame.columns
    assert pd.api.types.is_numeric_dtype(frame[HISTORIFY_TS_COLUMN])


def test_live_and_historify_ts_columns_are_mutually_exclusive_by_construction():
    """Belt-and-suspenders: no factory function should ever emit BOTH `ts`
    and `timestamp` on the same frame — the two column families model two
    genuinely different data sources and a frame carrying both would mask
    which path ``derive_today_and_yest`` takes in a way no production frame
    ever does."""
    live = make_live_5m_frame([100.0, 101.0], dt.date(2026, 6, 4))
    historify = make_historify_daily_frame([100.0, 101.0], end_date=dt.date(2026, 6, 4))
    assert HISTORIFY_TS_COLUMN not in live.columns
    assert LIVE_5M_TS_COLUMN not in historify.columns
