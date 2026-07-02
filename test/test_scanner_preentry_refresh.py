"""Tests for scanner pre-entry data refresh + WS subscription nudge (issue #239).

Covers:
- ``run_preentry_scanner_refresh`` calls ``check_and_refresh_if_stale`` for
  both ``1m`` and ``D`` intervals, waits for jobs, and nudges WS subscription
  when not yet subscribed.
- Flag-off → immediate ``{"skipped": True}`` return, no provider work.
- Wait-failure is swallowed (fail-graceful).
- WS nudge failure is swallowed (fail-graceful).
- ``init_scanner_preentry_refresh`` registers an APScheduler job at the
  configured time (default 09:16); time < 09:18 (before the smoke check).
- ``preentry_refresh_time`` returns a configurable default; override via env.
- ``_scanner_preentry_refresh_job`` wraps ``run_preentry_scanner_refresh`` and
  never raises.

All tests are hermetic (no real DuckDB, broker, APScheduler, or DB access).
``wait_for_jobs`` is patched at ``services.historify_service.wait_for_jobs``
because the production code imports it lazily inside a try block (deferred
import to keep the module import-light; same pattern as run_boot_backfill_checks).
"""

from __future__ import annotations

from datetime import time
from unittest.mock import MagicMock, patch

import pytest

import services.scanner_backfill_scheduler as sched

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _fresh_res(interval: str) -> dict:
    return {
        "status": "ok",
        "interval": interval,
        "stale_symbols": [],
        "refreshed": [],
        "skipped_fresh": ["SYM1"],
        "errors": [],
    }


def _stale_res(interval: str, job_id: str = "jx") -> dict:
    return {
        "status": "ok",
        "interval": interval,
        "stale_symbols": ["RELIANCE"],
        "refreshed": ["RELIANCE"],
        "skipped_fresh": [],
        "errors": [],
        "job_id": job_id,
    }


def _backfill_res_with_jobs() -> dict:
    return {
        "intervals": {
            "1m": _stale_res("1m", "j1"),
            "D": _stale_res("D", "j2"),
        },
        "all_fresh": False,
        "errors": [],
    }


# --------------------------------------------------------------------------- #
# 1. Flag-off → immediate skip, no provider calls
# --------------------------------------------------------------------------- #


def test_preentry_flag_off_skips_immediately(monkeypatch):
    """``SCANNER_PREENTRY_REFRESH_ENABLED=false`` → returns ``{"skipped": True}``
    without touching ``run_backfill_checks``."""
    monkeypatch.setenv("SCANNER_PREENTRY_REFRESH_ENABLED", "false")
    called = []

    with patch.object(
        sched,
        "run_backfill_checks",
        side_effect=lambda *a, **kw: (called.append("run"), {})[1],
    ):
        res = sched.run_preentry_scanner_refresh()

    assert res == {"skipped": True}
    assert "run" not in called


# --------------------------------------------------------------------------- #
# 2. Happy path: calls checks, persists health, and calls wait_for_jobs
# --------------------------------------------------------------------------- #


def test_preentry_calls_run_backfill_checks_and_waits(monkeypatch):
    """run_preentry_scanner_refresh calls run_backfill_checks (via monkeypatch)
    and then wait_for_jobs with the job_ids from both intervals."""
    monkeypatch.setenv("SCANNER_PREENTRY_REFRESH_ENABLED", "true")

    backfill_res = _backfill_res_with_jobs()
    wait_called = []

    mock_wait = MagicMock(side_effect=lambda ids, **kw: (wait_called.append(ids), {})[1])

    with (
        patch.object(sched, "run_backfill_checks", return_value=backfill_res),
        patch.object(sched, "_persist_health"),
        patch.object(sched, "_log_and_alert"),
        # Patch at the source module — the function is imported lazily inside the try block
        patch("services.historify_service.wait_for_jobs", mock_wait),
        # WS nudge: subscriber already has symbols — no nudge needed
        patch.object(
            sched, "scanner_pre_subscriber", new=MagicMock(subscribed={"SYM1"}), create=True
        ),
    ):
        res = sched.run_preentry_scanner_refresh()

    assert res is backfill_res
    # wait_for_jobs was called with a list containing both job_ids
    assert wait_called, "wait_for_jobs was not called"
    ids = wait_called[0]
    assert "j1" in ids
    assert "j2" in ids


# --------------------------------------------------------------------------- #
# 3. WS nudge fires when subscriber has no symbols
# --------------------------------------------------------------------------- #


def test_preentry_nudges_ws_when_not_subscribed(monkeypatch):
    """When ``scanner_pre_subscriber.subscribed`` is empty AND a broker session
    is live, ``ensure`` is called with the SCANNER_SYMBOLS universe.

    The production code imports ``scanner_pre_subscriber`` lazily from
    ``services.scanner_presubscribe`` inside the WS-nudge try block, so we
    patch the attribute on the source module (not on the scheduler module).
    """
    monkeypatch.setenv("SCANNER_PREENTRY_REFRESH_ENABLED", "true")
    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE,SBIN,NIFTY")

    backfill_res = {"intervals": {}, "all_fresh": True, "errors": []}
    ensure_calls = []

    mock_subscriber = MagicMock()
    mock_subscriber.subscribed = set()  # empty → nudge should fire
    mock_subscriber.ensure.side_effect = lambda uid, brk, syms: (
        ensure_calls.append((uid, brk, syms)),
        3,
    )[1]

    with (
        patch.object(sched, "run_backfill_checks", return_value=backfill_res),
        patch.object(sched, "_persist_health"),
        patch.object(sched, "_log_and_alert"),
        patch("services.historify_service.wait_for_jobs", return_value={}),
        # Patch at the source module — production code does:
        #   from services.scanner_presubscribe import scanner_pre_subscriber
        patch("services.scanner_presubscribe.scanner_pre_subscriber", mock_subscriber),
        # Patch the database calls (deferred inside the try block)
        patch("database.auth_db.get_first_available_api_key", return_value="test_api_key"),
        patch("database.auth_db.verify_api_key", return_value="user123"),
        patch("database.auth_db.get_broker_name", return_value="zerodha"),
        # ws_connection_getter inside ensure() calls websocket_service — stub it out
        patch(
            "services.websocket_service.get_websocket_connection", return_value=("ok", "user123")
        ),
    ):
        sched.run_preentry_scanner_refresh()

    assert ensure_calls, "ensure() was never called"
    uid, brk, syms = ensure_calls[0]
    assert uid == "user123"
    assert brk == "zerodha"
    # SCANNER_SYMBOLS contains RELIANCE, SBIN, NIFTY
    assert "RELIANCE" in syms and "SBIN" in syms and "NIFTY" in syms


# --------------------------------------------------------------------------- #
# 4. WS nudge skipped when subscriber already has symbols
# --------------------------------------------------------------------------- #


def test_preentry_no_ws_nudge_when_already_subscribed(monkeypatch):
    """When ``scanner_pre_subscriber.subscribed`` is non-empty, ``ensure`` must
    NOT be called (idempotent guard)."""
    monkeypatch.setenv("SCANNER_PREENTRY_REFRESH_ENABLED", "true")
    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE,SBIN")

    backfill_res = {"intervals": {}, "all_fresh": True, "errors": []}

    mock_subscriber = MagicMock()
    mock_subscriber.subscribed = {"RELIANCE", "SBIN"}  # already subscribed

    with (
        patch.object(sched, "run_backfill_checks", return_value=backfill_res),
        patch.object(sched, "_persist_health"),
        patch.object(sched, "_log_and_alert"),
        patch("services.historify_service.wait_for_jobs", return_value={}),
        patch.object(sched, "scanner_pre_subscriber", new=mock_subscriber, create=True),
    ):
        sched.run_preentry_scanner_refresh()

    mock_subscriber.ensure.assert_not_called()


# --------------------------------------------------------------------------- #
# 5. wait_for_jobs failure is swallowed
# --------------------------------------------------------------------------- #


def test_preentry_wait_failure_is_swallowed(monkeypatch):
    """A wait_for_jobs exception must not propagate — the job must still return
    the backfill result."""
    monkeypatch.setenv("SCANNER_PREENTRY_REFRESH_ENABLED", "true")

    backfill_res = _backfill_res_with_jobs()

    with (
        patch.object(sched, "run_backfill_checks", return_value=backfill_res),
        patch.object(sched, "_persist_health"),
        patch.object(sched, "_log_and_alert"),
        patch(
            "services.historify_service.wait_for_jobs", side_effect=RuntimeError("DuckDB timeout")
        ),
        patch.object(
            sched, "scanner_pre_subscriber", new=MagicMock(subscribed={"SYM1"}), create=True
        ),
    ):
        res = sched.run_preentry_scanner_refresh()  # must not raise

    assert res is backfill_res


# --------------------------------------------------------------------------- #
# 6. WS nudge failure is swallowed
# --------------------------------------------------------------------------- #


def test_preentry_ws_nudge_failure_is_swallowed(monkeypatch):
    """A WS nudge exception must not propagate and the historify result is
    still returned."""
    monkeypatch.setenv("SCANNER_PREENTRY_REFRESH_ENABLED", "true")
    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE")

    backfill_res = {"intervals": {}, "all_fresh": True, "errors": []}

    mock_subscriber = MagicMock()
    mock_subscriber.subscribed = set()
    mock_subscriber.ensure.side_effect = RuntimeError("WS not connected")

    with (
        patch.object(sched, "run_backfill_checks", return_value=backfill_res),
        patch.object(sched, "_persist_health"),
        patch.object(sched, "_log_and_alert"),
        patch("services.historify_service.wait_for_jobs", return_value={}),
        patch.object(sched, "scanner_pre_subscriber", new=mock_subscriber, create=True),
        patch("database.auth_db.get_first_available_api_key", return_value="key"),
        patch("database.auth_db.verify_api_key", return_value="uid"),
        patch("database.auth_db.get_broker_name", return_value="zerodha"),
    ):
        res = sched.run_preentry_scanner_refresh()  # must not raise

    assert res is backfill_res


# --------------------------------------------------------------------------- #
# 7. preentry_refresh_time default and override
# --------------------------------------------------------------------------- #


def test_preentry_time_default(monkeypatch):
    """Default ``SCANNER_PREENTRY_REFRESH_TIME`` should parse to 09:16."""
    monkeypatch.delenv("SCANNER_PREENTRY_REFRESH_TIME", raising=False)
    t = sched.preentry_refresh_time()
    assert t == time(9, 16)


def test_preentry_time_env_override(monkeypatch):
    """``SCANNER_PREENTRY_REFRESH_TIME=09:14`` overrides the default."""
    monkeypatch.setenv("SCANNER_PREENTRY_REFRESH_TIME", "09:14")
    t = sched.preentry_refresh_time()
    assert t == time(9, 14)


def test_preentry_time_bad_value_falls_back(monkeypatch):
    """A garbage env value falls back to the 09:16 default."""
    monkeypatch.setenv("SCANNER_PREENTRY_REFRESH_TIME", "not-a-time")
    t = sched.preentry_refresh_time()
    assert t == time(9, 16)


# --------------------------------------------------------------------------- #
# 8. default time is before 09:18 smoke check
# --------------------------------------------------------------------------- #


def test_preentry_time_is_before_smoke_check(monkeypatch):
    """Confirm: the default pre-entry time must fire BEFORE the 09:18 smoke
    check so the data refresh completes before the smoke gate reads it."""
    monkeypatch.delenv("SCANNER_PREENTRY_REFRESH_TIME", raising=False)
    t = sched.preentry_refresh_time()
    assert t < time(9, 18), f"Pre-entry refresh time {t} must be before the 09:18 smoke check"


# --------------------------------------------------------------------------- #
# 9. init_scanner_preentry_refresh registers the APScheduler job
# --------------------------------------------------------------------------- #


def test_init_scanner_preentry_refresh_registers_job(monkeypatch):
    """``init_scanner_preentry_refresh`` registers a job with id
    ``'scanner_preentry_refresh'`` at the configured time."""
    monkeypatch.setenv("SCANNER_PREENTRY_REFRESH_ENABLED", "true")
    monkeypatch.setenv("SCANNER_PREENTRY_REFRESH_TIME", "09:16")

    mock_scheduler = MagicMock()
    sched.init_scanner_preentry_refresh(scheduler=mock_scheduler)

    mock_scheduler.add_job.assert_called_once()
    # Check the keyword args for the job id
    call_kwargs = mock_scheduler.add_job.call_args[1]
    assert call_kwargs.get("id") == "scanner_preentry_refresh"
    assert call_kwargs.get("replace_existing") is True


def test_init_scanner_preentry_refresh_flag_off_still_registers(monkeypatch):
    """Even when the flag is off, the job should be registered (so toggling
    at runtime takes effect without a restart)."""
    monkeypatch.setenv("SCANNER_PREENTRY_REFRESH_ENABLED", "false")

    mock_scheduler = MagicMock()
    sched.init_scanner_preentry_refresh(scheduler=mock_scheduler)

    # Job must still be registered
    mock_scheduler.add_job.assert_called_once()


def test_init_scanner_preentry_refresh_no_scheduler_is_graceful():
    """When no scheduler is available the init must not raise."""
    with patch(
        "services.historify_scheduler_service.get_historify_scheduler",
        return_value=None,
    ):
        # Must not raise even with no scheduler
        sched.init_scanner_preentry_refresh(scheduler=None)


# --------------------------------------------------------------------------- #
# 10. _scanner_preentry_refresh_job wraps without raising
# --------------------------------------------------------------------------- #


def test_scanner_preentry_refresh_job_swallows_exceptions(monkeypatch):
    """The APScheduler-called job wrapper must never propagate an exception
    to the scheduler thread."""
    monkeypatch.setenv("SCANNER_PREENTRY_REFRESH_ENABLED", "true")

    with patch.object(
        sched,
        "run_preentry_scanner_refresh",
        side_effect=RuntimeError("boom"),
    ):
        sched._scanner_preentry_refresh_job()  # must not raise
