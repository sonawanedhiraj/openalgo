"""Tests for the Task 3 ScannerHistoryProvider warm-up + scheduled-refresh wiring.

Covers:
- ``run_boot_warmup`` calls ``get_provider().refresh()`` once when enabled.
- The ``SCANNER_HISTORY_WARMUP_ENABLED=false`` gate skips the refresh.
- A refresh failure is swallowed (never raised) so boot is unaffected.
- ``HistorifyScheduler._register_scanner_history_job`` adds the 16:00 IST cron
  job with the right id + function, and the env gate suppresses it.
"""

from unittest.mock import MagicMock, patch

import pandas as pd
from apscheduler.triggers.cron import CronTrigger

import services.scanner_history_provider as shp
from services.historify_scheduler_service import (
    HistorifyScheduler,
    refresh_scanner_history,
)
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


# --------------------------------------------------------------- boot warm-up


def test_run_boot_warmup_calls_refresh_once(monkeypatch):
    monkeypatch.setenv("SCANNER_HISTORY_WARMUP_ENABLED", "true")
    fake_provider = MagicMock()
    fake_provider.symbols = ["SBIN", "INFY"]
    fake_provider.refresh.return_value = {"symbols_loaded": 2, "errors": []}

    with patch.object(shp, "get_provider", return_value=fake_provider):
        result = shp.run_boot_warmup()

    fake_provider.refresh.assert_called_once_with()
    assert result == {"symbols_loaded": 2, "errors": []}


def test_run_boot_warmup_disabled_skips_refresh(monkeypatch):
    monkeypatch.setenv("SCANNER_HISTORY_WARMUP_ENABLED", "false")
    fake_provider = MagicMock()

    with patch.object(shp, "get_provider", return_value=fake_provider):
        result = shp.run_boot_warmup()

    fake_provider.refresh.assert_not_called()
    assert result is None


def test_run_boot_warmup_swallows_refresh_error(monkeypatch):
    monkeypatch.setenv("SCANNER_HISTORY_WARMUP_ENABLED", "true")
    fake_provider = MagicMock()
    fake_provider.symbols = ["SBIN"]
    fake_provider.refresh.side_effect = RuntimeError("duckdb down")

    with patch.object(shp, "get_provider", return_value=fake_provider):
        # Must not raise — boot continues even if the bulk load fails.
        result = shp.run_boot_warmup()

    assert result is None
    fake_provider.refresh.assert_called_once()


def test_refresh_scanner_history_job_calls_provider():
    """The module-level cron job target refreshes the singleton provider."""
    fake_provider = MagicMock()
    fake_provider.refresh.return_value = {"symbols_loaded": 5, "errors": []}

    with patch(
        "services.scanner_history_provider.get_provider",
        return_value=fake_provider,
    ):
        refresh_scanner_history()

    fake_provider.refresh.assert_called_once_with()


# ----------------------------------------------------------- scheduler wiring


def _fresh_scheduler_with_mock():
    """Return the HistorifyScheduler singleton with a mocked APScheduler."""
    sched = HistorifyScheduler()
    sched._scheduler = MagicMock()
    return sched


def test_register_scanner_history_job_adds_cron(monkeypatch):
    monkeypatch.setenv("SCANNER_HISTORY_SCHEDULED_REFRESH_ENABLED", "true")
    sched = _fresh_scheduler_with_mock()

    sched._register_scanner_history_job()

    sched._scheduler.add_job.assert_called_once()
    _, kwargs = sched._scheduler.add_job.call_args
    args, _ = sched._scheduler.add_job.call_args
    # First positional arg is the job function.
    assert args[0] is refresh_scanner_history
    assert kwargs["id"] == "scanner_history_refresh"
    assert kwargs["replace_existing"] is True
    trigger = kwargs["trigger"]
    assert isinstance(trigger, CronTrigger)
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["hour"] == "16"
    assert fields["minute"] == "0"


def test_register_scanner_history_job_gate_off(monkeypatch):
    monkeypatch.setenv("SCANNER_HISTORY_SCHEDULED_REFRESH_ENABLED", "false")
    sched = _fresh_scheduler_with_mock()

    sched._register_scanner_history_job()

    sched._scheduler.add_job.assert_not_called()


def test_boot_warmup_before_backfill_completes_leaves_provider_empty(monkeypatch):
    """Documents boot-ordering: warmup refresh runs before backfill, leaving provider
    in empty-sentinel state. Post-fix, lazy-load recovers when data arrives.

    Full boot sequence:
      1. app.py boots → scanner_backfill_scheduler submits D download (async, no rows yet)
      2. app.py boots → run_boot_warmup() → refresh() → DuckDB empty → sentinels
      3. [later] backfill download completes → DuckDB now has rows
      4. scanner fires on bar-close → get_daily() → lazy-load → returns data  ← FIX

    PRE-FIX: step 4 returns None — sentinel permanently blocks the lazy-load path.
    POST-FIX: step 4 lazy-loads from DuckDB and returns real data.
    """
    backfill_done = [False]

    def fetch(*, symbol, exchange, interval, start_timestamp, end_timestamp):
        if backfill_done[0]:
            return _make_frame(300 if interval == "D" else 40)
        return pd.DataFrame()

    monkeypatch.setattr("services.scanner_history_provider.historify_db.get_ohlcv", fetch)

    # Steps 1+2: provider created + warmup refresh before backfill completes
    provider = ScannerHistoryProvider(["HDFCBANK", "ICICIBANK"], daily_lookback_bars=205)
    provider.refresh()  # simulates run_boot_warmup() → refresh()

    # Provider is in empty-sentinel state immediately after warmup
    assert provider.get_cache_status()["daily_rows_total"] == 0
    assert provider.get_daily("HDFCBANK") is None  # still no data in DuckDB

    # Step 3: backfill download completes
    backfill_done[0] = True

    # Step 4: scanner calls get_daily() — must lazy-load successfully (post-fix)
    result = provider.get_daily("HDFCBANK")
    assert result is not None, (
        "boot-ordering race: warmup refresh stores empty sentinels before backfill; "
        "post-fix the next get_daily() call lazy-loads from the now-populated DuckDB"
    )
    assert len(result) == 205

    # Second symbol in the list must also self-heal via lazy-load
    result2 = provider.get_daily("ICICIBANK")
    assert result2 is not None
    assert len(result2) == 205
