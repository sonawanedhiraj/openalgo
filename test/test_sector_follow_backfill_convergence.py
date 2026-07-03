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

_RESULT_KEYS = {"status", "stale_symbols", "refreshed", "still_stale", "errors", "skipped_fresh"}


def _epoch(d: date, hh: int = 15, mm: int = 29) -> int:
    """UTC epoch for an IST wall-clock time on ``d`` (matches market_data convention)."""
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=_IST).timestamp())


# --------------------------------------------------------------------------- #
# 1. Stale → triggers refresh
# --------------------------------------------------------------------------- #
def test_index_stale_triggers_refresh_of_every_symbol():
    universe = ["NIFTYAUTO", "NIFTYBANK", "NIFTYIT"]
    captured: dict = {}
    # Mutable freshness store so the fake backfill can simulate the download
    # actually landing new bars — the post-#313 verification re-reads this via
    # get_data_freshness after the job "completes".
    freshness = {s: _epoch(WED) for s in universe}

    def fake_backfill(start, end, symbols=None):
        captured["symbols"] = symbols
        captured["start"] = start
        captured["end"] = end
        for s in symbols or []:
            freshness[s] = _epoch(THURS)
        return {"status": "success", "job_id": "j1", "symbols": symbols}

    with (
        patch.object(idx, "sector_index_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            side_effect=lambda *a, **k: dict(freshness),
        ),
        patch.object(idx, "backfill_sector_indices", side_effect=fake_backfill),
        patch("services.historify_service.wait_for_jobs", return_value={"j1": "completed"}),
    ):
        res = idx.check_and_refresh_if_stale(THURS)

    # `job_id` (issue #154) may be added when a backfill job is submitted —
    # subset check keeps the contract additive.
    assert _RESULT_KEYS.issubset(set(res))
    assert res["status"] == "ok"
    assert set(res["stale_symbols"]) == set(universe)
    assert set(res["refreshed"]) == set(universe)
    assert res["still_stale"] == []
    assert res["skipped_fresh"] == []
    assert res["errors"] == []
    # Only the stale symbols are handed to the fetch; window ends on the ref day.
    assert set(captured["symbols"]) == set(universe)
    assert captured["end"] == "2026-06-11"
    # Issue #193 — incremental window collapses to today-only when the prior
    # day's data is already on disk (WED stored, THURS being fetched).
    # Pre-#193 asserted strict ``<``; the new behavior is byte-exact equality.
    assert captured["start"] == "2026-06-11"
    assert captured["start"] <= captured["end"]


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
        # Simulate the download landing new bars for the requested symbols.
        for s in symbols or []:
            freshness[s] = _epoch(THURS)
        return {"status": "success", "job_id": "j", "symbols": symbols}

    with (
        patch.object(idx, "sector_index_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            side_effect=lambda *a, **k: dict(freshness),
        ),
        patch.object(idx, "backfill_sector_indices", side_effect=fake_backfill),
        patch("services.historify_service.wait_for_jobs", return_value={"j": "completed"}),
    ):
        res = idx.check_and_refresh_if_stale(THURS)

    assert set(res["stale_symbols"]) == set(stale)
    assert set(res["skipped_fresh"]) == set(fresh)
    assert set(res["refreshed"]) == set(stale)
    assert res["still_stale"] == []
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
    assert res["still_stale"] == universe


# --------------------------------------------------------------------------- #
# 4b. Issue #313 (ports #304) — submission success is NOT completion: a symbol
# whose MAX(timestamp) does NOT advance after the job completes must be
# reported still_stale, not refreshed.
# --------------------------------------------------------------------------- #
def test_index_verification_reports_still_stale_when_job_completes_without_advancing():
    """A job accepted cleanly (status=success, job_id set) whose underlying
    fetch never lands new bars for one index (e.g. a per-symbol broker
    rejection mid-batch) must report that index still_stale/errored — the
    pre-#313 code marked every stale index refreshed at submission time."""
    universe = ["NIFTYAUTO", "NIFTYBANK"]
    # NIFTYAUTO's fetch will "land" (simulated in fake_backfill); NIFTYBANK's
    # won't — the freshness read after the job never advances for it.
    freshness = {s: _epoch(WED) for s in universe}

    def fake_backfill(start, end, symbols=None):
        freshness["NIFTYAUTO"] = _epoch(THURS)
        return {"status": "success", "job_id": "j1", "symbols": symbols}

    with (
        patch.object(idx, "sector_index_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            side_effect=lambda *a, **k: dict(freshness),
        ),
        patch.object(idx, "backfill_sector_indices", side_effect=fake_backfill),
        patch("services.historify_service.wait_for_jobs", return_value={"j1": "completed"}),
    ):
        res = idx.check_and_refresh_if_stale(THURS)

    assert res["refreshed"] == ["NIFTYAUTO"]
    assert res["still_stale"] == ["NIFTYBANK"]
    # NIFTYBANK is 1/2 = 50% still-stale > the 20% escalation threshold.
    assert res["status"] == "error"
    assert any("still stale" in e for e in res["errors"])


def test_stock_verification_reports_still_stale_when_job_completes_without_advancing():
    universe = ["INFY", "SBIN"]
    freshness = {s: _epoch(WED) for s in universe}

    def fake_backfill(start, end, symbols=None):
        freshness["INFY"] = _epoch(THURS)
        # SBIN deliberately left stale — simulates a partial download failure
        # that still reports job status "success" at submission time.
        return {"status": "success", "job_id": "j1", "symbols": symbols}

    with (
        patch.object(stk, "sector_follow_stock_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            side_effect=lambda *a, **k: dict(freshness),
        ),
        patch.object(stk, "backfill_sector_follow_stocks", side_effect=fake_backfill),
        patch("services.historify_service.wait_for_jobs", return_value={"j1": "completed"}),
    ):
        res = stk.check_and_refresh_if_stale(THURS)

    assert res["refreshed"] == ["INFY"]
    assert res["still_stale"] == ["SBIN"]
    assert res["status"] == "error"
    assert any("still stale" in e for e in res["errors"])


def test_stock_verification_read_failure_falls_back_to_submission_reporting():
    """A verification-read failure must fail open to the pre-#313 submission-
    based reporting (loudly logged), never raise into the convergence path."""
    universe = ["INFY", "SBIN"]
    reads = {"n": 0}

    def flaky_freshness(*a, **k):
        reads["n"] += 1
        if reads["n"] == 1:  # the initial stale-check read succeeds
            return {s: _epoch(WED) for s in universe}
        raise RuntimeError("duckdb read failed mid-verification")

    with (
        patch.object(stk, "sector_follow_stock_symbols", return_value=universe),
        patch("services.data_freshness_service.get_data_freshness", side_effect=flaky_freshness),
        patch.object(
            stk,
            "backfill_sector_follow_stocks",
            return_value={"status": "success", "job_id": "j1", "symbols": universe},
        ),
        patch("services.historify_service.wait_for_jobs", return_value={"j1": "completed"}),
    ):
        res = stk.check_and_refresh_if_stale(THURS)  # must NOT raise

    # Fallback: every submitted symbol reported refreshed (degraded mode).
    assert set(res["refreshed"]) == set(universe)
    assert res["status"] == "ok"


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
    # PR #56 replaced the old get_first_available_api_key gate with is_live_broker_session.
    # Patch the new seam so the test doesn't touch the DB or a real broker.
    with patch("services.broker_session_health.is_live_broker_session", return_value=True):
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


# --------------------------------------------------------------------------- #
# 7. Pre-entry refresh (#237) — fetch stale intraday before the 15:20 entry
# --------------------------------------------------------------------------- #
def test_preentry_refresh_runs_backfill_and_waits(monkeypatch):
    """The 15:17 pre-entry refresh runs the same stale-check the boot/periodic
    paths use and waits (bounded) for the download jobs (#237)."""
    monkeypatch.setenv("SECTOR_FOLLOW_PREENTRY_REFRESH_ENABLED", "true")
    waited = {}

    def fake_wait(job_ids, timeout_sec=600, poll_sec=1.0):
        waited["job_ids"] = job_ids
        waited["timeout_sec"] = timeout_sec
        return {}

    with (
        patch.object(
            sched,
            "run_backfill_checks",
            return_value={
                "index": {"job_id": "J-idx"},
                "stock": {"job_id": "J-stk"},
                "all_fresh": False,
                "errors": [],
            },
        ) as m_run,
        patch("services.historify_service.wait_for_jobs", side_effect=fake_wait),
    ):
        res = sched.run_preentry_backfill_checks(THURS)

    m_run.assert_called_once_with(THURS)
    assert res["all_fresh"] is False
    assert set(waited["job_ids"]) == {"J-idx", "J-stk"}
    # Bounded wait must be short enough not to overrun the 15:20 entry window.
    assert waited["timeout_sec"] == sched._PREENTRY_WAIT_SEC
    assert sched._PREENTRY_WAIT_SEC <= 120


def test_preentry_refresh_skipped_when_flag_off(monkeypatch):
    monkeypatch.setenv("SECTOR_FOLLOW_PREENTRY_REFRESH_ENABLED", "false")
    with patch.object(sched, "run_backfill_checks") as m_run:
        res = sched.run_preentry_backfill_checks(THURS)
    assert res == {"skipped": True}
    m_run.assert_not_called()


def test_preentry_refresh_wait_failure_is_swallowed(monkeypatch):
    """A wait_for_jobs error must never break the entry path."""
    monkeypatch.setenv("SECTOR_FOLLOW_PREENTRY_REFRESH_ENABLED", "true")
    with (
        patch.object(
            sched,
            "run_backfill_checks",
            return_value={"index": {}, "stock": {}, "all_fresh": True, "errors": []},
        ),
        patch(
            "services.historify_service.wait_for_jobs",
            side_effect=RuntimeError("boom"),
        ),
    ):
        # Must not raise.
        res = sched.run_preentry_backfill_checks(THURS)
    assert res["all_fresh"] is True


def test_preentry_refresh_time_default_and_override(monkeypatch):
    monkeypatch.delenv("SECTOR_FOLLOW_PREENTRY_REFRESH_TIME", raising=False)
    assert sched.preentry_refresh_time() == time(15, 17)
    monkeypatch.setenv("SECTOR_FOLLOW_PREENTRY_REFRESH_TIME", "15:10")
    assert sched.preentry_refresh_time() == time(15, 10)
    # Malformed → safe default.
    monkeypatch.setenv("SECTOR_FOLLOW_PREENTRY_REFRESH_TIME", "notatime")
    assert sched.preentry_refresh_time() == time(15, 17)


def test_preentry_refresh_time_before_smoke_check():
    """The refresh must fire strictly before the 15:18 smoke check so the smoke
    sees fresh data."""
    t = sched.preentry_refresh_time()
    assert (t.hour, t.minute) < (15, 18)
