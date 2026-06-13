"""Tests for the Task 3 ScannerHistoryProvider warm-up + scheduled-refresh wiring.

Covers:
- ``run_boot_warmup`` calls ``get_provider().refresh()`` once when enabled.
- The ``SCANNER_HISTORY_WARMUP_ENABLED=false`` gate skips the refresh.
- A refresh failure is swallowed (never raised) so boot is unaffected.
- ``HistorifyScheduler._register_scanner_history_job`` adds the 16:00 IST cron
  job with the right id + function, and the env gate suppresses it.
"""

from unittest.mock import MagicMock, patch

from apscheduler.triggers.cron import CronTrigger

import services.scanner_history_provider as shp
from services.historify_scheduler_service import (
    HistorifyScheduler,
    refresh_scanner_history,
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
