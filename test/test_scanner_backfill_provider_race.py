"""Regression tests for the boot-time backfill ↔ warmup race condition.

Root cause of the 2026-06-19 through 2026-06-24 scanner dark period:

  T+0ms    scanner_backfill_scheduler submits async D download (no data yet)
  T+141ms  run_boot_warmup() → ScannerHistoryProvider.refresh() → DuckDB empty
           → pd.DataFrame() sentinel stored for every configured symbol
  T+90s    D download completes, rows land in DuckDB
  T+300s   Scanner bar-close tick → get_daily() → PRE-FIX: returns None forever
                                                 POST-FIX: lazy-loads real data

All tests in this file FAIL on pre-fix code and PASS on post-fix code.
"""

from __future__ import annotations

import pandas as pd

from services.scanner_history_provider import ScannerHistoryProvider

_OHLCV_COLS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]


def _make_frame(n: int, base: float = 100.0) -> pd.DataFrame:
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


def test_provider_sees_empty_when_refresh_runs_before_download_completes(monkeypatch):
    """Full race end-to-end: warmup refresh before backfill download → sentinel →
    later lazy-load serves real data.

    This is the exact sequence that caused 5 days of scanner dark (2026-06-19→24).

    PRE-FIX: get_daily() at T+300s returns None — non-None empty DF short-circuits
             the lazy-load path and the provider is permanently dark for these symbols.
    POST-FIX: empty sentinel falls through to lazy-load; DuckDB now has rows → served.
    """
    backfill_done = [False]

    def fetch(*, symbol, exchange, interval, start_timestamp, end_timestamp):
        if backfill_done[0]:
            return _make_frame(300 if interval == "D" else 40)
        return pd.DataFrame()

    monkeypatch.setattr("services.scanner_history_provider.historify_db.get_ohlcv", fetch)

    p = ScannerHistoryProvider(["SBIN", "INFY"], daily_lookback_bars=205)

    # T+141ms: warmup refresh while backfill is still downloading → sentinels
    p.refresh()
    assert p.get_cache_status()["daily_rows_total"] == 0

    # T+90s: backfill download completes — DuckDB now has rows
    backfill_done[0] = True

    # T+300s: first scanner bar-close → get_daily() is called
    result_sbin = p.get_daily("SBIN")
    assert result_sbin is not None, (
        "get_daily() must recover via lazy-load after backfill download completes; "
        "pre-fix the empty sentinel permanently blocks lazy-load for configured symbols"
    )
    assert len(result_sbin) == 205

    result_infy = p.get_daily("INFY")
    assert result_infy is not None, "fix must apply to all configured symbols, not just first"
    assert len(result_infy) == 205


def test_provider_reflects_fresh_data_only_after_explicit_refresh(monkeypatch):
    """Documents the pre-fix architectural gap: fresh backfill data required an
    explicit refresh() call — no implicit self-healing existed.

    Post-fix: get_daily() lazy-loads without any manual intervention.
    The test name records the pre-fix symptom that this fix eliminates.

    PRE-FIX: result is None — provider stays dark until someone calls refresh().
    POST-FIX: result is the real frame — lazy-load serves data immediately.
    """
    backfill_done = [False]

    def fetch(*, symbol, exchange, interval, start_timestamp, end_timestamp):
        if backfill_done[0]:
            return _make_frame(300 if interval == "D" else 40)
        return pd.DataFrame()

    monkeypatch.setattr("services.scanner_history_provider.historify_db.get_ohlcv", fetch)

    p = ScannerHistoryProvider(["RELIANCE"], daily_lookback_bars=205)

    # Warmup refresh before backfill — sentinel stored
    p.refresh()
    assert p.get_daily("RELIANCE") is None  # no data yet, expected

    # Backfill arrives — NO explicit refresh() is called
    backfill_done[0] = True

    # Post-fix: lazy-load must serve the data WITHOUT requiring a second refresh()
    result = p.get_daily("RELIANCE")
    assert result is not None, (
        "post-fix: lazy-load must serve backfill data without an explicit refresh(); "
        "pre-fix this returns None — the operator had no automatic self-healing path"
    )
    assert len(result) == 205
