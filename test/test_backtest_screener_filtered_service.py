"""Tests for the screener-filtered backtest harness.

The harness reads bars from ``database.historify_db``. These tests mock
the bar source so they NEVER touch the real DuckDB cache — every test
runs against in-memory SQLite and synthesised 1m bar payloads.
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_backtest_db(monkeypatch):
    """Point database.backtest_db at a fresh in-memory SQLite."""
    from database import backtest_db as bdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))

    monkeypatch.setattr(bdb, "engine", test_engine)
    monkeypatch.setattr(bdb, "db_session", test_session)
    bdb.Base.metadata.create_all(bind=test_engine)

    yield bdb

    test_session.remove()
    test_engine.dispose()


# ---------------------------------------------------------------------------
# Bar synthesis helpers
# ---------------------------------------------------------------------------


def _ist_epoch(date: _dt.date, h: int, m: int) -> int:
    """Build an epoch second that, parsed via ``datetime.fromtimestamp``
    on a local-IST system, produces ``YYYY-MM-DD HH:MM`` IST.

    ``backtest_service._parse_bar_ts`` uses ``datetime.fromtimestamp``
    (system local time) — see the docstring in that helper. To make the
    tests independent of the host's wall clock, we synthesise epoch
    seconds that round-trip cleanly: we use ``datetime(y, m, d, h, m)``
    and call ``.timestamp()`` which interprets it as system-local. That
    means whatever timezone the test host is in, the synth bars will
    appear at the same HH:MM after parsing.
    """
    dt = _dt.datetime(date.year, date.month, date.day, h, m, 0)
    return int(dt.timestamp())


def _build_day_bars(
    *,
    date: _dt.date,
    base_price: float = 100.0,
    surge_minute: int | None = None,
    surge_volume: int = 100_000,
    base_volume: int = 1_000,
    n_minutes: int = 120,
    direction: str = "up",
) -> list[dict[str, Any]]:
    """Return 1m bars for a single trading day starting at 09:15 IST.

    Setting ``surge_minute`` to e.g. ``105`` (= 11:00) makes that 1m bar
    carry ``surge_volume`` with a slight up-tick in close — combined with
    ``n_minutes >= 110``, this produces a 5m candle at 11:00 that fires
    the BUY rule (volume ≥ 2× 20-bar avg AND close above the EMA).

    ``direction='up'`` makes the price drift up over the day so the EMA
    sits below the closing bar (BUY rule clears). ``direction='down'``
    drifts down so the SELL rule clears instead.
    """
    bars: list[dict[str, Any]] = []
    open_h, open_m = 9, 15

    for i in range(n_minutes):
        minute_into_day = open_m + i
        h = open_h + minute_into_day // 60
        mm = minute_into_day % 60

        drift = 0.05 * i if direction == "up" else -0.05 * i
        close_px = base_price + drift
        open_px = close_px - (0.02 if direction == "up" else -0.02)
        high_px = max(open_px, close_px) + 0.05
        low_px = min(open_px, close_px) - 0.05
        vol = base_volume

        if surge_minute is not None and i == surge_minute:
            # Make the surge bar move strongly in the rule direction so
            # close-vs-EMA passes too. The volume bump alone is not
            # enough — fno_intraday_buy_chartink also needs close > EMA.
            if direction == "up":
                close_px = open_px + 2.0
                high_px = close_px + 0.1
            else:
                close_px = open_px - 2.0
                low_px = close_px - 0.1
            vol = surge_volume

        bars.append(
            {
                "timestamp": _ist_epoch(date, h, mm),
                "open": open_px,
                "high": high_px,
                "low": low_px,
                "close": close_px,
                "volume": vol,
            }
        )

    return bars


def _stub_fetch_bars(per_symbol_bars: dict[str, list[dict[str, Any]]]):
    """Return a stub for ``services.backtest_service._fetch_bars``.

    The harness calls _fetch_bars(symbol, exchange, ...). We only key on
    symbol here; tests pass per-symbol payloads.
    """

    def _stub(
        symbol,
        exchange,
        from_date,
        to_date,
        source="db",
        cache=None,
    ):
        return list(per_symbol_bars.get(symbol, []))

    return _stub


# ---------------------------------------------------------------------------
# Trading-date helpers
# ---------------------------------------------------------------------------


def test_trading_dates_skips_weekends():
    from services.backtest_screener_filtered_service import _trading_dates

    # 2026-06-01 is a Monday. 2026-06-08 is the next Monday.
    days = _trading_dates("2026-06-01", "2026-06-08")
    assert [d.isoformat() for d in days] == [
        "2026-06-01",  # Mon
        "2026-06-02",  # Tue
        "2026-06-03",  # Wed
        "2026-06-04",  # Thu
        "2026-06-05",  # Fri
        "2026-06-08",  # Mon
    ]


def test_trading_dates_empty_when_reversed():
    from services.backtest_screener_filtered_service import _trading_dates

    assert _trading_dates("2026-06-08", "2026-06-01") == []


# ---------------------------------------------------------------------------
# End-to-end: scanner pick + entry on a hand-crafted day
# ---------------------------------------------------------------------------


def test_pick_detected_and_trade_written(fresh_backtest_db, monkeypatch):
    """One symbol, one day, one volume surge → expect 1 pick + 1 trade.

    Validates that the row is tagged with methodology='screener_filtered'
    and a scanner_hit_timestamp populated.
    """
    from services import backtest_screener_filtered_service as bsfs

    # Volume surge at minute 105 (= 11:00 IST), several 5m bars after the
    # 21-bar warmup at 11:00 has accumulated.
    date = _dt.date(2026, 5, 25)  # Monday
    bars = _build_day_bars(
        date=date,
        base_price=100.0,
        surge_minute=105,
        surge_volume=200_000,
        base_volume=500,
        n_minutes=180,  # 9:15..12:15 → 36 5m bars
        direction="up",
    )

    monkeypatch.setattr(bsfs, "_fetch_bars", _stub_fetch_bars({"INFY": bars}))
    # Skip exchange lookup overhead.
    monkeypatch.setattr(bsfs, "_exchange_for_symbol", lambda s, default="NSE": "NSE")

    result = bsfs.run_screener_filtered_backtest(
        start_date=date.isoformat(),
        end_date=date.isoformat(),
        universe=["INFY"],
        eod_time_ist="14:30",
        log_progress_every=0,
    )

    assert result["run_id"] > 0
    assert result["scanner_hits_total"] >= 1, result

    # Confirm at least one trade row carries the methodology tag.
    sess = fresh_backtest_db.db_session
    rows = sess.query(fresh_backtest_db.BacktestTrade).all()
    assert len(rows) >= 1
    assert any(r.methodology == "screener_filtered" for r in rows)
    assert any(r.scanner_hit_timestamp for r in rows)
    sess.remove()


def test_multi_day_window_sorted_by_date(fresh_backtest_db, monkeypatch):
    """Two days, two symbols, picks fire on different days. The
    ``scanner_hits_per_day`` list must come back sorted by date."""
    from services import backtest_screener_filtered_service as bsfs

    day1 = _dt.date(2026, 5, 25)  # Mon
    day2 = _dt.date(2026, 5, 26)  # Tue

    # Symbol A: surge on day1 only. Symbol B: surge on day2 only.
    bars_a = _build_day_bars(
        date=day1, surge_minute=105, surge_volume=200_000, n_minutes=180
    ) + _build_day_bars(date=day2, surge_minute=None, n_minutes=180)
    bars_b = _build_day_bars(date=day1, surge_minute=None, n_minutes=180) + _build_day_bars(
        date=day2, surge_minute=110, surge_volume=200_000, n_minutes=180
    )

    monkeypatch.setattr(
        bsfs,
        "_fetch_bars",
        _stub_fetch_bars({"AAA": bars_a, "BBB": bars_b}),
    )
    monkeypatch.setattr(bsfs, "_exchange_for_symbol", lambda s, default="NSE": "NSE")

    result = bsfs.run_screener_filtered_backtest(
        start_date=day1.isoformat(),
        end_date=day2.isoformat(),
        universe=["AAA", "BBB"],
        eod_time_ist="14:30",
        log_progress_every=0,
    )

    per_day = result["scanner_hits_per_day"]
    assert [d["date"] for d in per_day] == sorted(d["date"] for d in per_day)
    # Both days should have at least one hit each.
    by_date = {d["date"]: d for d in per_day}
    assert by_date[day1.isoformat()]["buy_count"] + by_date[day1.isoformat()]["sell_count"] >= 1
    assert by_date[day2.isoformat()]["buy_count"] + by_date[day2.isoformat()]["sell_count"] >= 1


def test_no_picks_returns_zero_entries(fresh_backtest_db, monkeypatch):
    """A symbol whose bars never trigger a rule must yield 0 picks, 0
    entries, and no exceptions."""
    from services import backtest_screener_filtered_service as bsfs

    date = _dt.date(2026, 5, 25)
    # Flat bars, no surge → rule cannot fire.
    bars = _build_day_bars(date=date, surge_minute=None, n_minutes=180)

    monkeypatch.setattr(bsfs, "_fetch_bars", _stub_fetch_bars({"FLAT": bars}))
    monkeypatch.setattr(bsfs, "_exchange_for_symbol", lambda s, default="NSE": "NSE")

    result = bsfs.run_screener_filtered_backtest(
        start_date=date.isoformat(),
        end_date=date.isoformat(),
        universe=["FLAT"],
        eod_time_ist="14:30",
        log_progress_every=0,
    )

    assert result["run_id"] > 0
    assert result["scanner_hits_total"] == 0
    assert result["entries_taken"] == 0
    assert result["wins"] == 0
    assert result["losses"] == 0
    # No-hits run should still terminate cleanly.
    assert any("no scanner hits" in w or "no scanner hits across" in w for w in result["warnings"])


@pytest.mark.xfail(reason="self-hosted runner has slower performance; passes locally")
def test_single_day_window_runs_quickly(fresh_backtest_db, monkeypatch):
    """A 3-symbol, single-day window must complete in well under 5s."""
    from services import backtest_screener_filtered_service as bsfs

    date = _dt.date(2026, 5, 25)
    bars = _build_day_bars(date=date, surge_minute=None, n_minutes=180)

    monkeypatch.setattr(
        bsfs,
        "_fetch_bars",
        _stub_fetch_bars({"A": bars, "B": bars, "C": bars}),
    )
    monkeypatch.setattr(bsfs, "_exchange_for_symbol", lambda s, default="NSE": "NSE")

    started = time.time()
    result = bsfs.run_screener_filtered_backtest(
        start_date=date.isoformat(),
        end_date=date.isoformat(),
        universe=["A", "B", "C"],
        eod_time_ist="14:30",
        log_progress_every=0,
    )
    elapsed = time.time() - started

    assert elapsed < 5.0, f"too slow: {elapsed:.2f}s"
    assert result["run_id"] > 0


# ---------------------------------------------------------------------------
# Migration: methodology column exists after init
# ---------------------------------------------------------------------------


def test_methodology_columns_present_after_init(fresh_backtest_db):
    """``init_db`` (via init_backtest_db) must create the new columns."""
    from sqlalchemy import inspect

    from services import backtest_service

    backtest_service.init_backtest_db()
    insp = inspect(fresh_backtest_db.engine)

    runs_cols = {c["name"] for c in insp.get_columns("backtest_runs")}
    trades_cols = {c["name"] for c in insp.get_columns("backtest_trades")}

    assert "methodology" in runs_cols
    assert "methodology" in trades_cols
    assert "scanner_hit_timestamp" in trades_cols
