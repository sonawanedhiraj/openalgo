"""Tests for services.scanner_history_provider.ScannerHistoryProvider.

The DuckDB layer (``database.historify_db.get_ohlcv``) is mocked throughout so
these tests run without a live OpenAlgo/DuckDB instance.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from services.scanner_history_provider import ScannerHistoryProvider

_OHLCV_COLS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]


def _make_frame(n: int, base: float = 100.0) -> pd.DataFrame:
    """Build a synthetic OHLCV frame with ``n`` rows."""
    return pd.DataFrame(
        {
            "timestamp": [1_700_000_000 + i * 86_400 for i in range(n)],
            "open": [base + i for i in range(n)],
            "high": [base + i + 1 for i in range(n)],
            "low": [base + i - 1 for i in range(n)],
            "close": [base + i + 0.5 for i in range(n)],
            "volume": [1_000 + i for i in range(n)],
            "oi": [0 for _ in range(n)],
        },
        columns=_OHLCV_COLS,
    )


def _fake_get_ohlcv(*, symbol, exchange, interval, start_timestamp, end_timestamp):
    """Daily → 300 rows, weekly → 40 rows; UNKNOWN → empty frame."""
    if symbol == "UNKNOWN":
        return pd.DataFrame()
    return _make_frame(300 if interval == "D" else 40)


def test_get_daily_returns_lookback_tail(monkeypatch):
    monkeypatch.setattr("services.scanner_history_provider.historify_db.get_ohlcv", _fake_get_ohlcv)
    p = ScannerHistoryProvider(["SBIN"], daily_lookback_bars=205)
    p.refresh()
    df = p.get_daily("SBIN")
    assert df is not None
    assert len(df) == 205  # trimmed from the 300 the fake returns
    assert list(df.columns) == _OHLCV_COLS


def test_get_weekly_returns_lookback_tail(monkeypatch):
    monkeypatch.setattr("services.scanner_history_provider.historify_db.get_ohlcv", _fake_get_ohlcv)
    p = ScannerHistoryProvider(["SBIN"], weekly_lookback_bars=22)
    p.refresh()
    df = p.get_weekly("SBIN")
    assert df is not None
    assert len(df) == 22


def test_lazy_load_for_uncached_symbol(monkeypatch):
    calls = []

    def tracking(*, symbol, exchange, interval, start_timestamp, end_timestamp):
        calls.append((symbol, interval))
        return _make_frame(300 if interval == "D" else 40)

    monkeypatch.setattr("services.scanner_history_provider.historify_db.get_ohlcv", tracking)
    # INFY not in the configured symbol list and refresh() not called.
    p = ScannerHistoryProvider(["SBIN"])
    df = p.get_daily("INFY")
    assert df is not None
    assert ("INFY", "D") in calls
    # Second read is served from cache — no new DuckDB call.
    calls.clear()
    p.get_daily("INFY")
    assert calls == []


def test_refresh_updates_last_refresh_at(monkeypatch):
    monkeypatch.setattr("services.scanner_history_provider.historify_db.get_ohlcv", _fake_get_ohlcv)
    p = ScannerHistoryProvider(["SBIN", "INFY"])
    assert p.get_cache_status()["last_refresh_at"] is None
    result = p.refresh()
    assert result["symbols_loaded"] == 2
    assert result["errors"] == []
    status = p.get_cache_status()
    assert status["last_refresh_at"] is not None
    assert status["symbol_count"] == 2
    assert status["daily_rows_total"] == 2 * 205


def test_missing_data_returns_none(monkeypatch):
    monkeypatch.setattr("services.scanner_history_provider.historify_db.get_ohlcv", _fake_get_ohlcv)
    p = ScannerHistoryProvider(["UNKNOWN"])
    p.refresh()
    assert p.get_daily("UNKNOWN") is None
    assert p.get_weekly("UNKNOWN") is None


def test_error_on_one_symbol_captured_others_load(monkeypatch):
    def flaky(*, symbol, exchange, interval, start_timestamp, end_timestamp):
        if symbol == "BAD":
            raise RuntimeError("duckdb boom")
        return _make_frame(300 if interval == "D" else 40)

    monkeypatch.setattr("services.scanner_history_provider.historify_db.get_ohlcv", flaky)
    p = ScannerHistoryProvider(["SBIN", "BAD", "INFY"])
    result = p.refresh()
    assert result["symbols_loaded"] == 2
    assert len(result["errors"]) == 1
    assert result["errors"][0]["symbol"] == "BAD"
    assert "boom" in result["errors"][0]["error"]
    # Healthy symbols still served.
    assert p.get_daily("SBIN") is not None
    assert p.get_daily("INFY") is not None


def test_empty_sentinel_blocks_lazy_load_after_data_arrives(monkeypatch):
    """Empty cache sentinel must NOT permanently block lazy-load once backfill arrives.

    Race: refresh() runs while DuckDB has no rows → pd.DataFrame() sentinel stored.
    Backfill completes → data lands in DuckDB. The next get_daily() call must
    lazy-load that fresh data rather than returning None forever.

    PRE-FIX: returns None (non-None empty DF short-circuits the lazy-load path).
    POST-FIX: falls through to lazy-load and returns real data.
    """
    backfill_done = [False]

    def fetch(*, symbol, exchange, interval, start_timestamp, end_timestamp):
        if backfill_done[0]:
            return _make_frame(300 if interval == "D" else 40)
        return pd.DataFrame()

    monkeypatch.setattr("services.scanner_history_provider.historify_db.get_ohlcv", fetch)
    p = ScannerHistoryProvider(["SBIN"], daily_lookback_bars=205)

    # refresh() while DuckDB has no data → empty sentinel stored for SBIN
    p.refresh()
    assert p.get_cache_status()["daily_rows_total"] == 0

    # Backfill completes — data is now in DuckDB
    backfill_done[0] = True

    # Must lazy-load without an explicit second refresh()
    result = p.get_daily("SBIN")
    assert result is not None, (
        "empty sentinel must not permanently block lazy-load; "
        "post-fix the cache falls through to _fetch when the stored frame is empty"
    )
    assert len(result) == 205


def test_second_refresh_cures_empty_sentinel(monkeypatch):
    """An explicit refresh() after backfill overwrites empty sentinels with real data.

    Documents that refresh() is the pre-fix escape hatch. Post-fix, lazy-load makes
    an explicit second refresh() unnecessary — but it must still work when called.
    Both pre-fix and post-fix should PASS this test; the regression tests above
    (test_empty_sentinel_blocks_lazy_load_after_data_arrives) catch the pre-fix gap.
    """
    data_ready = [False]

    def fetch(*, symbol, exchange, interval, start_timestamp, end_timestamp):
        if data_ready[0]:
            return _make_frame(300 if interval == "D" else 40)
        return pd.DataFrame()

    monkeypatch.setattr("services.scanner_history_provider.historify_db.get_ohlcv", fetch)
    p = ScannerHistoryProvider(["SBIN"], daily_lookback_bars=205)

    # First refresh with empty DuckDB → sentinel
    p.refresh()
    assert p.get_daily("SBIN") is None

    # Backfill arrives; second explicit refresh()
    data_ready[0] = True
    p.refresh()

    result = p.get_daily("SBIN")
    assert result is not None, "second refresh() after backfill must overwrite empty sentinel"
    assert len(result) == 205


def test_concurrent_reads_during_refresh(monkeypatch):
    monkeypatch.setattr("services.scanner_history_provider.historify_db.get_ohlcv", _fake_get_ohlcv)
    p = ScannerHistoryProvider(["SBIN", "INFY", "TCS", "WIPRO"])
    p.refresh()

    errors = []

    def reader():
        try:
            for _ in range(20):
                d = p.get_daily("SBIN")
                assert d is None or len(d) == 205
            return True
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)
            return False

    def refresher():
        try:
            for _ in range(5):
                p.refresh()
            return True
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)
            return False

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(reader) for _ in range(4)] + [ex.submit(refresher)]
        results = [f.result() for f in futures]

    assert not errors
    assert all(results)
    assert p.get_cache_status()["symbol_count"] == 4
