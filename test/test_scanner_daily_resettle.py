"""Tests for the scanner daily-D re-settle (issue #299).

A daily-D bar written *intraday* as a provisional/running close (the #277
"historify freezes at 09:45" class) is never corrected by the normal
convergence — ``compute_stale_symbols`` sees a bar for the day already present
and the incremental download SKIPS it. The provisional close then persists into
the scanner's ``yest_d`` gate and manufactures phantom gap signals (the
2026-07-02 DELHIVERY false BUY: stored 07-01 close 475.4 vs settled 507.7).

``resettle_recent_daily`` forces a NON-incremental overwrite re-fetch of the
trailing settled window + refreshes ``ScannerHistoryProvider`` so the corrected
settled close reaches the scanner. These tests are fully mocked — no real broker
download, DuckDB access, or provider construction.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import services.scanner_backfill_scheduler as sched
import services.scanner_universe_backfill as sub

# Reference trading days. THURS is a weekday; the two prior business days are
# WED (06-10) and TUE (06-09). SAT (06-13) exercises weekend roll-back.
THURS = date(2026, 6, 11)
SAT = date(2026, 6, 13)


def _clear_resettle_guard():
    sched._resettled_dates.clear()


# --------------------------------------------------------------------------- #
# Window computation
# --------------------------------------------------------------------------- #
def test_nth_prev_business_day_walks_back_trading_days():
    assert sub._nth_prev_business_day(THURS, 0) == THURS
    assert sub._nth_prev_business_day(THURS, 1) == date(2026, 6, 10)  # WED
    assert sub._nth_prev_business_day(THURS, 2) == date(2026, 6, 9)  # TUE
    # Weekend reference rolls back to Friday first, then steps back trading days.
    assert sub._nth_prev_business_day(SAT, 0) == date(2026, 6, 12)  # FRI
    assert sub._nth_prev_business_day(SAT, 2) == date(2026, 6, 10)  # WED


# --------------------------------------------------------------------------- #
# Core: force non-incremental overwrite + refresh provider
# --------------------------------------------------------------------------- #
def test_resettle_forces_non_incremental_overwrite_and_refreshes_provider():
    universe = ["DELHIVERY", "JSWENERGY"]
    captured: dict = {}

    def fake_backfill(start, end, interval="1m", api_key=None, symbols=None, incremental=True):
        captured.update(start=start, end=end, interval=interval, incremental=incremental)
        return {"status": "success", "job_id": "jD", "symbols": symbols, "interval": interval}

    class _Provider:
        def refresh(self):
            captured["provider_refreshed"] = True
            return {"symbols_loaded": len(universe), "errors": []}

    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch.object(sub, "backfill_scanner_universe", side_effect=fake_backfill),
        patch("services.historify_service.wait_for_jobs", return_value={"jD": "completed"}),
        patch("services.scanner_history_provider.get_provider", return_value=_Provider()),
    ):
        res = sub.resettle_recent_daily(THURS, days=2)

    assert res["status"] == "ok"
    assert res["resettled"] is True
    assert res["interval"] == "D"
    # Overwrite re-fetch — NOT incremental — of the last 2 settled days.
    assert captured["incremental"] is False
    assert captured["interval"] == "D"
    assert captured["start"] == "2026-06-09"
    assert captured["end"] == "2026-06-11"
    assert res["window"] == "2026-06-09..2026-06-11"
    # Provider cache re-read so the corrected settled close reaches the scanner.
    assert captured.get("provider_refreshed") is True
    assert res["provider_symbols_loaded"] == 2
    assert res["errors"] == []


# --------------------------------------------------------------------------- #
# Feature flag
# --------------------------------------------------------------------------- #
def test_resettle_disabled_by_flag(monkeypatch):
    monkeypatch.setenv("SCANNER_DAILY_RESETTLE_ENABLED", "false")
    with patch.object(sub, "backfill_scanner_universe") as m_bf:
        res = sub.resettle_recent_daily(THURS)
    assert res["status"] == "disabled"
    m_bf.assert_not_called()


# --------------------------------------------------------------------------- #
# Empty universe
# --------------------------------------------------------------------------- #
def test_resettle_empty_universe_is_noop():
    with (
        patch.object(sub, "scanner_universe_symbols", return_value=[]),
        patch.object(sub, "backfill_scanner_universe") as m_bf,
    ):
        res = sub.resettle_recent_daily(THURS)
    assert res["status"] == "ok"
    assert res["resettled"] is False
    m_bf.assert_not_called()


# --------------------------------------------------------------------------- #
# Fail-graceful: a backfill rejection is reported and skips the provider refresh
# --------------------------------------------------------------------------- #
def test_resettle_backfill_error_skips_provider_refresh():
    with (
        patch.object(sub, "scanner_universe_symbols", return_value=["DELHIVERY"]),
        patch.object(
            sub,
            "backfill_scanner_universe",
            return_value={"status": "error", "message": "no api key available"},
        ),
        patch("services.scanner_history_provider.get_provider") as m_provider,
    ):
        res = sub.resettle_recent_daily(THURS)
    assert res["status"] == "error"
    assert res["resettled"] is False
    assert any("no api key" in e for e in res["errors"])
    m_provider.assert_not_called()  # never refresh the cache on a failed re-fetch


def test_resettle_never_raises_on_backfill_exception():
    with (
        patch.object(sub, "scanner_universe_symbols", return_value=["DELHIVERY"]),
        patch.object(sub, "backfill_scanner_universe", side_effect=RuntimeError("boom")),
        patch.object(sub, "logger") as m_logger,
    ):
        res = sub.resettle_recent_daily(THURS)  # must NOT raise
    assert res["status"] == "error"
    assert res["errors"]
    m_logger.exception.assert_called()


# --------------------------------------------------------------------------- #
# Scheduler wiring: once per date, only when D is configured
# --------------------------------------------------------------------------- #
def test_maybe_resettle_runs_once_per_date():
    _clear_resettle_guard()
    with (
        patch.object(sched, "_intervals", return_value=["1m", "D"]),
        patch(
            "services.scanner_universe_backfill.resettle_recent_daily",
            return_value={"status": "ok", "window": "w", "resettled": True, "errors": []},
        ) as m_res,
    ):
        sched._maybe_resettle_daily(THURS)
        sched._maybe_resettle_daily(THURS)  # same date → suppressed
    assert m_res.call_count == 1
    _clear_resettle_guard()


def test_maybe_resettle_skipped_when_D_not_configured():
    _clear_resettle_guard()
    with (
        patch.object(sched, "_intervals", return_value=["1m"]),
        patch("services.scanner_universe_backfill.resettle_recent_daily") as m_res,
    ):
        sched._maybe_resettle_daily(THURS)
    m_res.assert_not_called()


def test_maybe_resettle_retries_next_tick_on_transient_failure():
    """A failed attempt (no broker session yet) is not marked done — it retries."""
    _clear_resettle_guard()
    with (
        patch.object(sched, "_intervals", return_value=["D"]),
        patch(
            "services.scanner_universe_backfill.resettle_recent_daily",
            return_value={"status": "error", "resettled": False, "errors": ["no api key"]},
        ) as m_res,
    ):
        sched._maybe_resettle_daily(THURS)
        sched._maybe_resettle_daily(THURS)  # error last time → retried
    assert m_res.call_count == 2
    _clear_resettle_guard()


def test_run_backfill_checks_triggers_resettle_before_stale_check():
    _clear_resettle_guard()
    order: list[str] = []

    def fake_check(today=None, *, interval="1m"):
        order.append(f"check:{interval}")
        return {
            "status": "ok",
            "interval": interval,
            "stale_symbols": [],
            "refreshed": [],
            "errors": [],
            "skipped_fresh": [],
        }

    def fake_resettle(today=None):
        order.append("resettle")
        return {"status": "ok", "window": "w", "resettled": True, "errors": []}

    with (
        patch.object(sched, "_intervals", return_value=["1m", "D"]),
        patch(
            "services.scanner_universe_backfill.check_and_refresh_if_stale", side_effect=fake_check
        ),
        patch(
            "services.scanner_universe_backfill.resettle_recent_daily", side_effect=fake_resettle
        ),
    ):
        sched.run_backfill_checks(THURS)

    assert order[0] == "resettle"  # re-settle runs BEFORE the stale-check
    assert "check:1m" in order and "check:D" in order
    _clear_resettle_guard()
