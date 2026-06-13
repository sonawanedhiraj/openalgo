"""Tests for the boot-time + periodic state-convergence backfill.

Covers the refactor that replaced the 16:05 IST index + 16:10 IST stock APScheduler
**cron** jobs with a ``check_and_refresh_if_stale`` convergence pattern (read
MAX(timestamp) per symbol → fetch only the stale tail → idempotent no-op when
fresh → fail-graceful on a dead broker session), plus the boot hook and periodic
loop that orchestrate both universes.

Fully mocked — ``get_data_freshness`` (the DuckDB read) and the backfill pipeline
are patched, so no real broker download or DuckDB access happens.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

import services.sector_follow_backfill_scheduler as sched
import services.sector_follow_index_backfill as idx
import services.sector_follow_stock_backfill as stk

_IST = timezone(timedelta(hours=5, minutes=30))

# Reference trading days. THURS is a weekday; WED is the prior business day.
THURS = date(2026, 6, 11)
WED = date(2026, 6, 10)
SAT = date(2026, 6, 13)

_RESULT_KEYS = {"status", "stale_symbols", "refreshed", "errors", "skipped_fresh"}


def _epoch(d: date, hh: int = 15, mm: int = 29) -> int:
    """UTC epoch for an IST wall-clock time on ``d`` (matches market_data convention)."""
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=_IST).timestamp())


# --------------------------------------------------------------------------- #
# 1. Stale → triggers refresh
# --------------------------------------------------------------------------- #
def test_index_stale_triggers_refresh_of_every_symbol():
    universe = ["NIFTYAUTO", "NIFTYBANK", "NIFTYIT"]
    captured: dict = {}

    def fake_backfill(start, end, symbols=None):
        captured["symbols"] = symbols
        captured["start"] = start
        captured["end"] = end
        return {"status": "success", "job_id": "j1", "symbols": symbols}

    with (
        patch.object(idx, "sector_index_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value={s: _epoch(WED) for s in universe},
        ),
        patch.object(idx, "backfill_sector_indices", side_effect=fake_backfill),
    ):
        res = idx.check_and_refresh_if_stale(THURS)

    assert set(res) == _RESULT_KEYS
    assert res["status"] == "ok"
    assert set(res["stale_symbols"]) == set(universe)
    assert set(res["refreshed"]) == set(universe)
    assert res["skipped_fresh"] == []
    assert res["errors"] == []
    # Only the stale symbols are handed to the fetch; window ends on the ref day.
    assert set(captured["symbols"]) == set(universe)
    assert captured["end"] == "2026-06-11"
    assert captured["start"] < captured["end"]


# --------------------------------------------------------------------------- #
# 2. Fresh → no-op
# --------------------------------------------------------------------------- #
def test_stock_fresh_is_a_noop():
    universe = ["INFY", "SBIN", "TCS"]
    with (
        patch.object(stk, "sector_follow_stock_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value={s: _epoch(THURS) for s in universe},
        ),
        patch.object(stk, "backfill_sector_follow_stocks") as m_bf,
    ):
        res = stk.check_and_refresh_if_stale(THURS)

    assert res["status"] == "ok"
    assert res["stale_symbols"] == []
    assert res["refreshed"] == []
    assert set(res["skipped_fresh"]) == set(universe)
    m_bf.assert_not_called()


# --------------------------------------------------------------------------- #
# 3. Partial staleness → only the stale half is fetched
# --------------------------------------------------------------------------- #
def test_index_partial_staleness_fetches_only_stale_subset():
    fresh = ["A_IDX", "B_IDX"]
    stale = ["C_IDX", "D_IDX"]
    universe = fresh + stale
    freshness = {s: _epoch(THURS) for s in fresh}
    freshness.update({s: _epoch(WED) for s in stale})
    captured: dict = {}

    def fake_backfill(start, end, symbols=None):
        captured["symbols"] = symbols
        return {"status": "success", "job_id": "j", "symbols": symbols}

    with (
        patch.object(idx, "sector_index_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value=freshness,
        ),
        patch.object(idx, "backfill_sector_indices", side_effect=fake_backfill),
    ):
        res = idx.check_and_refresh_if_stale(THURS)

    assert set(res["stale_symbols"]) == set(stale)
    assert set(res["skipped_fresh"]) == set(fresh)
    assert set(res["refreshed"]) == set(stale)
    # The fresh half is never re-fetched.
    assert set(captured["symbols"]) == set(stale)


# --------------------------------------------------------------------------- #
# 4. Broker session failure → caught, reported, never propagated
# --------------------------------------------------------------------------- #
class _BrokerSessionExpired(Exception):
    """Stand-in for a dead daily Zerodha token surfacing from the fetch pipeline."""


def test_broker_failure_is_caught_logged_and_reported():
    universe = ["INFY", "SBIN"]

    def boom(*args, **kwargs):
        raise _BrokerSessionExpired("token expired")

    with (
        patch.object(stk, "sector_follow_stock_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value={s: _epoch(WED) for s in universe},
        ),
        patch.object(stk, "backfill_sector_follow_stocks", side_effect=boom),
        patch.object(stk, "logger") as m_logger,
    ):
        # Must NOT raise — fail-graceful.
        res = stk.check_and_refresh_if_stale(THURS)

    assert res["status"] == "error"
    assert res["errors"]  # populated
    assert res["refreshed"] == []
    assert set(res["stale_symbols"]) == set(universe)
    m_logger.exception.assert_called()


def test_backfill_error_status_surfaces_as_error():
    """A non-exception backfill rejection also populates errors (no raise)."""
    universe = ["INFY"]
    with (
        patch.object(stk, "sector_follow_stock_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value={s: _epoch(WED) for s in universe},
        ),
        patch.object(
            stk,
            "backfill_sector_follow_stocks",
            return_value={"status": "error", "message": "no api key available"},
        ),
    ):
        res = stk.check_and_refresh_if_stale(THURS)

    assert res["status"] == "error"
    assert any("no api key" in e for e in res["errors"])
    assert res["refreshed"] == []


# --------------------------------------------------------------------------- #
# 5. Boot hook fires both index and stock checks (after broker session is up)
# --------------------------------------------------------------------------- #
def _fresh_result() -> dict:
    return {
        "status": "ok",
        "stale_symbols": [],
        "refreshed": [],
        "errors": [],
        "skipped_fresh": [],
    }


def test_boot_runs_both_index_and_stock_checks_in_order():
    calls: list[str] = []

    def idx_check(today=None):
        calls.append("index")
        return _fresh_result()

    def stk_check(today=None):
        calls.append("stock")
        return _fresh_result()

    with (
        patch(
            "services.sector_follow_index_backfill.check_and_refresh_if_stale",
            side_effect=idx_check,
        ),
        patch(
            "services.sector_follow_stock_backfill.check_and_refresh_if_stale",
            side_effect=stk_check,
        ),
    ):
        res = sched.run_boot_backfill_checks(THURS)

    assert calls == ["index", "stock"]
    assert res["all_fresh"] is True
    assert res["errors"] == []


def test_boot_waits_for_broker_session_then_returns_true():
    with patch("database.auth_db.get_first_available_api_key", return_value="api-key"):
        assert sched._wait_for_broker_session(max_wait_sec=5) is True


# --------------------------------------------------------------------------- #
# 6. Periodic loop stops cleanly (both-fresh OR outside the 15:30..17:00 window)
# --------------------------------------------------------------------------- #
def test_within_window_boundaries():
    end_t = time(17, 0)
    assert sched._within_window(time(15, 30), end_t) is True
    assert sched._within_window(time(17, 0), end_t) is True
    assert sched._within_window(time(15, 29), end_t) is False
    assert sched._within_window(time(17, 1), end_t) is False  # after end → stop


def test_periodic_tick_skips_outside_window_runs_inside():
    end_t = time(17, 0)

    # Weekend → never runs regardless of time.
    ran, res = sched._periodic_tick(
        datetime(SAT.year, SAT.month, SAT.day, 16, 0, tzinfo=_IST), end_t
    )
    assert ran is False and res is None

    # Weekday after 17:00 → outside window → stops for the day.
    ran, res = sched._periodic_tick(datetime(2026, 6, 11, 17, 30, tzinfo=_IST), end_t)
    assert ran is False and res is None

    # Weekday inside the window → runs, and reports all_fresh.
    with patch.object(
        sched,
        "run_backfill_checks",
        return_value={"index": {}, "stock": {}, "all_fresh": True, "errors": []},
    ):
        ran, res = sched._periodic_tick(datetime(2026, 6, 11, 16, 0, tzinfo=_IST), end_t)
    assert ran is True and res["all_fresh"] is True


def test_periodic_loop_exits_cleanly_when_stopped():
    """The loop returns promptly once stopped — never hangs."""
    sched._stop_event.set()
    try:
        sched._periodic_loop()  # while-not-set is immediately False → returns
    finally:
        sched._stop_event.clear()
