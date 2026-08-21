"""Tests for the scanner-universe boot+periodic state-convergence backfill.

Covers the scanner-side analogue of the sector_follow convergence (Bugs A + B
from the 2026-06-13 Friday replay): keep the ``SCANNER_SYMBOLS`` F&O universe
fresh in BOTH ``1m`` and ``D``, fetching only the stale tail. Fully mocked —
``get_data_freshness`` (the DuckDB read) and the backfill pipeline are patched,
so no real broker download or DuckDB access happens.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import services.scanner_backfill_scheduler as sched
import services.scanner_smoke_check_service as smoke_svc
import services.scanner_universe_backfill as sub

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _stub_daily_resettle(monkeypatch):
    """These tests exercise the stale-check convergence contract, NOT the
    daily-D resettle (that has its own suite, test_scanner_daily_resettle.py).

    ``run_backfill_checks(resettle=True)`` calls the REAL
    ``resettle_recent_daily`` unless stubbed. In isolation that fails fast
    (fresh temp DB → no API key), but any earlier test in a full-suite run
    that leaves an active user + API key in the session temp DB removes that
    accident: with SCANNER_SYMBOLS set from .env, the resettle then launches
    a genuine full-universe download job whose ``wait_for_jobs`` poll hangs
    until pytest-timeout kills the run (2026-07-26). Stub it — and clear the
    once-per-process latch both ways so no latch state leaks across files.
    """
    sched._resettled_dates.clear()
    monkeypatch.setattr(
        sub,
        "resettle_recent_daily",
        lambda today=None, **kw: {
            "status": "ok",
            "interval": "D",
            "window": None,
            "resettled": False,
            "errors": [],
        },
    )
    yield
    sched._resettled_dates.clear()


# Reference trading days. THURS is a weekday; WED is the prior business day.
THURS = date(2026, 6, 11)
WED = date(2026, 6, 10)
SAT = date(2026, 6, 13)

_RESULT_KEYS = {
    "status",
    "interval",
    "stale_symbols",
    "refreshed",
    "still_stale",
    "errors",
    "skipped_fresh",
}


def _epoch(d: date, hh: int = 15, mm: int = 29) -> int:
    """UTC epoch for an IST wall-clock time on ``d`` (matches market_data convention)."""
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=_IST).timestamp())


# --------------------------------------------------------------------------- #
# 1. Stale → triggers refresh of only the stale subset, for the given interval
# --------------------------------------------------------------------------- #
def test_stale_triggers_refresh_of_stale_subset_1m():
    universe = ["RELIANCE", "SBIN", "TCS"]
    captured: dict = {}
    # Mutable freshness store so the fake backfill can simulate the download
    # actually landing new bars — the post-#304 verification re-reads this via
    # get_data_freshness after the job "completes".
    freshness = {s: _epoch(WED) for s in universe}

    def fake_backfill(start, end, interval="1m", symbols=None):
        captured["symbols"] = symbols
        captured["start"] = start
        captured["end"] = end
        captured["interval"] = interval
        for s in symbols or []:
            freshness[s] = _epoch(THURS)
        return {"status": "success", "job_id": "j1", "symbols": symbols, "interval": interval}

    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            side_effect=lambda *a, **k: dict(freshness),
        ),
        patch.object(sub, "backfill_scanner_universe", side_effect=fake_backfill),
        patch("services.historify_service.wait_for_jobs", return_value={"j1": "completed"}),
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")

    # `job_id` (issue #154) may be added when a backfill job is submitted —
    # subset check keeps the contract additive.
    assert _RESULT_KEYS.issubset(set(res))
    assert res["status"] == "ok"
    assert res["interval"] == "1m"
    assert set(res["stale_symbols"]) == set(universe)
    assert set(res["refreshed"]) == set(universe)
    assert res["still_stale"] == []
    assert res["skipped_fresh"] == []
    assert res["errors"] == []
    assert set(captured["symbols"]) == set(universe)
    assert captured["interval"] == "1m"
    assert captured["end"] == "2026-06-11"
    # Issue #193 — with Wednesday's data on disk and ref=Thursday, the
    # incremental window collapses to a single-day catch-up (start = WED + 1
    # = THURS = end). Pre-#193 this asserted ``start < end`` which would have
    # failed on the post-#193 behavior; the byte-exact equality is the
    # regression: only today's bars are fetched, not a fixed 4-day window.
    assert captured["start"] == "2026-06-11"
    assert captured["start"] <= captured["end"]


def test_two_days_stale_range_starts_from_last_stored_plus_one():
    """Issue #304 defect 1 — the real-world scenario: symbols 2 business days
    behind (last stored WED-1, ref THURS) must fetch from last_stored+1, not
    from ref..ref (the today-only bug that permanently skipped the interim day)."""
    universe = ["RELIANCE", "SBIN"]
    two_days_stale = WED - timedelta(days=1)  # Tuesday
    captured: dict = {}
    freshness = {s: _epoch(two_days_stale) for s in universe}

    def fake_backfill(start, end, interval="1m", symbols=None):
        captured["start"] = start
        captured["end"] = end
        for s in symbols or []:
            freshness[s] = _epoch(THURS)
        return {"status": "success", "job_id": "j1", "symbols": symbols, "interval": interval}

    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            side_effect=lambda *a, **k: dict(freshness),
        ),
        patch.object(sub, "backfill_scanner_universe", side_effect=fake_backfill),
        patch("services.historify_service.wait_for_jobs", return_value={"j1": "completed"}),
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")

    assert res["stale_symbols"] == sorted(universe)
    # Range must cover the interim day: start = two_days_stale + 1 day, not
    # ref..ref (the reported bug fetched 2026-07-02..2026-07-02 while symbols
    # were last stored 2026-06-30 — permanently skipping 07-01).
    expected_start = (two_days_stale + timedelta(days=1)).strftime("%Y-%m-%d")
    assert captured["start"] == expected_start
    assert captured["start"] < captured["end"]
    assert captured["end"] == THURS.strftime("%Y-%m-%d")
    assert res["refreshed"] == sorted(universe)


def test_max_catchup_days_cap_clamps_and_warns(monkeypatch, caplog):
    """Issue #304 — a symbol stale far beyond SCANNER_BACKFILL_MAX_CATCHUP_DAYS
    must have its fetch window clamped to the cap (not reach back to the true
    last-stored date), and a WARNING naming the symbols + pointing at the manual
    CLI must be logged."""
    import logging

    monkeypatch.setenv("SCANNER_BACKFILL_MAX_CATCHUP_DAYS", "3")
    universe = ["RELIANCE"]
    months_stale = THURS - timedelta(days=60)
    captured: dict = {}

    def fake_backfill(start, end, interval="1m", symbols=None):
        captured["start"] = start
        captured["end"] = end
        return {"status": "success", "job_id": "j1", "symbols": symbols, "interval": interval}

    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value={s: _epoch(months_stale) for s in universe},
        ),
        patch.object(sub, "backfill_scanner_universe", side_effect=fake_backfill),
        patch("services.historify_service.wait_for_jobs", return_value={"j1": "completed"}),
        caplog.at_level(logging.WARNING, logger="services.scanner_universe_backfill"),
    ):
        sub.check_and_refresh_if_stale(THURS, interval="1m")

    expected_start = (THURS - timedelta(days=3)).strftime("%Y-%m-%d")
    assert captured["start"] == expected_start
    assert any(
        "clamped" in r.message and "RELIANCE" in r.message and "--from" in r.message
        for r in caplog.records
    )


def test_max_catchup_days_default_and_env_override(monkeypatch):
    assert sub.max_catchup_days() == 7
    monkeypatch.setenv("SCANNER_BACKFILL_MAX_CATCHUP_DAYS", "14")
    assert sub.max_catchup_days() == 14
    monkeypatch.setenv("SCANNER_BACKFILL_MAX_CATCHUP_DAYS", "not-a-number")
    assert sub.max_catchup_days() == 7
    monkeypatch.setenv("SCANNER_BACKFILL_MAX_CATCHUP_DAYS", "0")
    assert sub.max_catchup_days() == 1  # floored at 1


# --------------------------------------------------------------------------- #
# 1b. Issue #380 — same-day PARTIAL session must trigger the 1m catch-up
# --------------------------------------------------------------------------- #
def test_same_day_partial_session_triggers_1m_catchup():
    """The #380 golden case through the convergence entry: a symbol whose only
    same-day bar is 09:16 must be caught up by the post-close check, not
    skipped as fresh. Pre-fix this fails: compute_stale_symbols measured
    staleness in business days between bar DATES, so the lone 09:16 bar made
    the symbol fresh, the 15:30-17:00 loop logged "1m feed fresh — no refresh",
    and every scanner_aggregator_seeder run fell back to ~225 per-symbol broker
    get_history calls (849 on 2026-07-07). THURS is a past trading day, so the
    full-session coverage requirement applies deterministically (no wall-clock
    dependence — cf. the smoke-hold wall-clock flake learning)."""
    universe = ["RELIANCE", "SBIN"]
    captured: dict = {}
    freshness = {
        "RELIANCE": _epoch(THURS, 9, 16),  # partial tape — 2 minutes of the session
        "SBIN": _epoch(THURS),  # full session (15:29 close)
    }

    def fake_backfill(start, end, interval="1m", symbols=None):
        captured["symbols"] = symbols
        captured["start"] = start
        captured["end"] = end
        for s in symbols or []:
            freshness[s] = _epoch(THURS)  # the re-download lands the full day
        return {"status": "success", "job_id": "j380", "symbols": symbols, "interval": interval}

    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            side_effect=lambda *a, **k: dict(freshness),
        ),
        patch.object(sub, "backfill_scanner_universe", side_effect=fake_backfill),
        patch("services.historify_service.wait_for_jobs", return_value={"j380": "completed"}),
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")

    assert res["status"] == "ok"
    assert res["stale_symbols"] == ["RELIANCE"]
    assert res["skipped_fresh"] == ["SBIN"]
    assert res["refreshed"] == ["RELIANCE"]
    assert res["still_stale"] == []
    # The partial day itself is the gap — a same-day catch-up window (the 1m
    # incremental download re-fetches from the last bar's own date, refilling
    # the rest of the session).
    assert captured["symbols"] == ["RELIANCE"]
    assert captured["start"] == THURS.strftime("%Y-%m-%d")
    assert captured["end"] == THURS.strftime("%Y-%m-%d")


def test_same_day_partial_session_flag_off_restores_date_semantics(monkeypatch):
    """Operator escape hatch: SCANNER_BACKFILL_SESSION_COVERAGE_ENABLED=false
    restores the pre-#380 date-granular predicate (same-day bar == fresh)."""
    monkeypatch.setenv("SCANNER_BACKFILL_SESSION_COVERAGE_ENABLED", "false")
    universe = ["RELIANCE"]
    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value={"RELIANCE": _epoch(THURS, 9, 16)},
        ),
        patch.object(sub, "backfill_scanner_universe") as m_bf,
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")

    assert res["status"] == "ok"
    assert res["stale_symbols"] == []
    assert res["skipped_fresh"] == ["RELIANCE"]
    m_bf.assert_not_called()


def test_daily_D_arm_keeps_date_granular_semantics():
    """The D arm must NOT inherit the session-coverage predicate — a daily bar's
    intraday timestamp says nothing about coverage (one bar IS the whole day),
    and provisional-close correction is the resettle's job (#299), not the
    incremental convergence's."""
    universe = ["RELIANCE"]
    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value={"RELIANCE": _epoch(THURS, 9, 16)},  # intraday-stamped D bar
        ),
        patch.object(sub, "backfill_scanner_universe") as m_bf,
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="D")

    assert res["status"] == "ok"
    assert res["stale_symbols"] == []
    assert res["skipped_fresh"] == ["RELIANCE"]
    m_bf.assert_not_called()


def test_verification_holds_symbol_still_stale_when_session_only_partially_lands():
    """#304 verification must apply the same #380 predicate: a job that only
    advances the tape to mid-session (partial download failure) must land the
    symbol in still_stale, not refreshed — otherwise the very date-granularity
    the predicate fixes would sneak back in through the verification re-read."""
    universe = ["RELIANCE"]
    freshness = {"RELIANCE": _epoch(THURS, 9, 16)}

    def fake_backfill(start, end, interval="1m", symbols=None):
        freshness["RELIANCE"] = _epoch(THURS, 11, 0)  # landed only part of the day
        return {"status": "success", "job_id": "j1", "symbols": symbols, "interval": interval}

    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            side_effect=lambda *a, **k: dict(freshness),
        ),
        patch.object(sub, "backfill_scanner_universe", side_effect=fake_backfill),
        patch("services.historify_service.wait_for_jobs", return_value={"j1": "completed"}),
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")

    assert res["refreshed"] == []
    assert res["still_stale"] == ["RELIANCE"]
    assert res["status"] == "error"  # 100% still-stale > the 20% escalation threshold


# --------------------------------------------------------------------------- #
# 2. Fresh → no-op (no fetch)
# --------------------------------------------------------------------------- #
def test_fresh_is_a_noop():
    universe = ["RELIANCE", "SBIN", "TCS"]
    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value={s: _epoch(THURS) for s in universe},
        ),
        patch.object(sub, "backfill_scanner_universe") as m_bf,
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")

    assert res["status"] == "ok"
    assert res["stale_symbols"] == []
    assert res["refreshed"] == []
    assert set(res["skipped_fresh"]) == set(universe)
    m_bf.assert_not_called()


# --------------------------------------------------------------------------- #
# 3. Partial staleness on the D interval → only the stale half is fetched
# --------------------------------------------------------------------------- #
def test_partial_staleness_fetches_only_stale_subset_daily():
    fresh = ["AAA", "BBB"]
    stale = ["CCC", "DDD"]
    universe = fresh + stale
    freshness = {s: _epoch(THURS) for s in fresh}
    freshness.update({s: _epoch(WED) for s in stale})
    captured: dict = {}

    def fake_backfill(start, end, interval="1m", symbols=None):
        captured["symbols"] = symbols
        captured["interval"] = interval
        # Simulate the download landing new bars for the requested symbols.
        for s in symbols or []:
            freshness[s] = _epoch(THURS)
        return {"status": "success", "job_id": "j", "symbols": symbols, "interval": interval}

    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            side_effect=lambda *a, **k: dict(freshness),
        ),
        patch.object(sub, "backfill_scanner_universe", side_effect=fake_backfill),
        patch("services.historify_service.wait_for_jobs", return_value={"j": "completed"}),
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="D")

    assert res["interval"] == "D"
    assert set(res["stale_symbols"]) == set(stale)
    assert set(res["skipped_fresh"]) == set(fresh)
    assert set(res["refreshed"]) == set(stale)
    assert res["still_stale"] == []
    # The fresh half is never re-fetched.
    assert set(captured["symbols"]) == set(stale)
    assert captured["interval"] == "D"


def test_verification_reports_still_stale_when_job_completes_without_advancing():
    """Issue #304 defect 2 — a symbol whose MAX(timestamp) does NOT advance
    after the job completes must be reported still_stale/failed, not refreshed.
    Reproduces the observed 'refreshed=216 errors=0' false-success report: the
    job is accepted (status=success, job_id set) but the underlying fetch never
    actually lands new bars for one symbol (e.g. a per-symbol broker rejection
    mid-batch)."""
    universe = ["RELIANCE", "SBIN"]
    # RELIANCE's fetch will "land" (simulated in fake_backfill); SBIN's won't —
    # the freshness read after the job never advances for SBIN.
    freshness = {s: _epoch(WED) for s in universe}

    def fake_backfill(start, end, interval="1m", symbols=None):
        freshness["RELIANCE"] = _epoch(THURS)
        # SBIN deliberately left stale — simulates a partial download failure
        # that still reports job status "success" at submission time.
        return {"status": "success", "job_id": "j1", "symbols": symbols, "interval": interval}

    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            side_effect=lambda *a, **k: dict(freshness),
        ),
        patch.object(sub, "backfill_scanner_universe", side_effect=fake_backfill),
        patch("services.historify_service.wait_for_jobs", return_value={"j1": "completed"}),
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")

    assert res["refreshed"] == ["RELIANCE"]
    assert res["still_stale"] == ["SBIN"]
    # SBIN is 1/2 = 50% still-stale > the 20% escalation threshold.
    assert res["status"] == "error"
    assert any("still stale" in e for e in res["errors"])


# --------------------------------------------------------------------------- #
# 4. Broker session failure → caught, reported, never propagated
# --------------------------------------------------------------------------- #
class _BrokerSessionExpired(Exception):
    """Stand-in for a dead daily Zerodha token surfacing from the fetch pipeline."""


def test_broker_failure_is_caught_logged_and_reported():
    universe = ["RELIANCE", "SBIN"]

    def boom(*args, **kwargs):
        raise _BrokerSessionExpired("token expired")

    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value={s: _epoch(WED) for s in universe},
        ),
        patch.object(sub, "backfill_scanner_universe", side_effect=boom),
        patch.object(sub, "logger") as m_logger,
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")  # must NOT raise

    assert res["status"] == "error"
    assert res["errors"]
    assert res["refreshed"] == []
    assert set(res["stale_symbols"]) == set(universe)
    m_logger.exception.assert_called()


def test_transient_lock_skips_quietly_without_alerting():
    """A DuckDB lock-contention read error is downgraded to a quiet skip — status
    'skipped_locked', no errors (so no Telegram), and NOT logged at exception."""
    universe = ["RELIANCE", "SBIN"]

    def locked(*args, **kwargs):
        raise RuntimeError(
            "Connection Error: Can't open a connection to same database file with "
            "a different configuration than existing connections"
        )

    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch("services.data_freshness_service.get_data_freshness", side_effect=locked),
        patch.object(sub, "backfill_scanner_universe") as m_bf,
        patch.object(sub, "logger") as m_logger,
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")  # must NOT raise

    assert res["status"] == "skipped_locked"
    assert res["errors"] == []
    assert res["stale_symbols"] == []
    m_bf.assert_not_called()  # no refresh attempted on a skip
    m_logger.exception.assert_not_called()  # quiet — INFO only
    m_logger.info.assert_called()


def test_backfill_error_status_surfaces_as_error():
    """A non-exception backfill rejection also populates errors (no raise)."""
    universe = ["RELIANCE"]
    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value={s: _epoch(WED) for s in universe},
        ),
        patch.object(
            sub,
            "backfill_scanner_universe",
            return_value={"status": "error", "message": "no api key available"},
        ),
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")

    assert res["status"] == "error"
    assert any("no api key" in e for e in res["errors"])
    assert res["refreshed"] == []


def test_backfill_logs_warning_when_symbol_errors(caplog):
    """Tier-1 Fix #2: a failed catch-up logs a WARNING naming the affected
    symbols + reason, not only a quiet error key in the returned dict (FM-11)."""
    import logging

    universe = ["RELIANCE", "SBIN"]
    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value={s: _epoch(WED) for s in universe},
        ),
        patch.object(
            sub,
            "backfill_scanner_universe",
            return_value={"status": "error", "message": "no api key available"},
        ),
        caplog.at_level(logging.WARNING, logger="services.scanner_universe_backfill"),
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")

    assert res["status"] == "error"
    assert any("catch-up FAILED" in r.message and "no api key" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# 5. Empty universe (SCANNER_SYMBOLS unset) → no-op, no fetch
# --------------------------------------------------------------------------- #
def test_empty_universe_is_a_noop():
    with (
        patch.object(sub, "scanner_universe_symbols", return_value=[]),
        patch.object(sub, "backfill_scanner_universe") as m_bf,
        patch("services.data_freshness_service.get_data_freshness") as m_fresh,
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")

    assert res["status"] == "ok"
    assert res["stale_symbols"] == []
    m_bf.assert_not_called()
    m_fresh.assert_not_called()


# --------------------------------------------------------------------------- #
# 6. Index symbols route to NSE_INDEX in the download payload
# --------------------------------------------------------------------------- #
def test_payload_routes_indices_to_nse_index():
    payload = sub._symbols_payload(["RELIANCE", "NIFTY", "BANKNIFTY", "SBIN"])
    by_symbol = {p["symbol"]: p["exchange"] for p in payload}
    assert by_symbol["NIFTY"] == "NSE_INDEX"
    assert by_symbol["BANKNIFTY"] == "NSE_INDEX"
    assert by_symbol["RELIANCE"] == "NSE"
    assert by_symbol["SBIN"] == "NSE"


def test_backfill_rejects_unknown_interval():
    res = sub.backfill_scanner_universe("2026-06-01", "2026-06-11", interval="5m")
    assert res["status"] == "error"
    assert "interval" in res["message"]


# --------------------------------------------------------------------------- #
# 7. Boot hook runs every configured interval and persists a health row each
# --------------------------------------------------------------------------- #
def _fresh_result(interval: str) -> dict:
    return {
        "status": "ok",
        "interval": interval,
        "stale_symbols": [],
        "refreshed": [],
        "errors": [],
        "skipped_fresh": ["RELIANCE"],
    }


def test_boot_runs_both_intervals_and_persists_health():
    calls: list[str] = []
    health_rows: list[tuple] = []

    def fake_check(today=None, *, interval="1m"):
        calls.append(interval)
        return _fresh_result(interval)

    def fake_insert(strategy_name, overall_ok, stale_symbols=None, details=None, alert_sent=0):
        health_rows.append((strategy_name, overall_ok))
        return 1

    with (
        patch.object(sched, "_intervals", return_value=["1m", "D"]),
        patch(
            "services.scanner_universe_backfill.check_and_refresh_if_stale",
            side_effect=fake_check,
        ),
        patch("database.data_health_db.insert_check", side_effect=fake_insert),
    ):
        res = sched.run_boot_backfill_checks(THURS)

    assert calls == ["1m", "D"]
    assert res["all_fresh"] is True
    assert res["errors"] == []
    # One health row per interval, both healthy.
    assert ("scanner_universe_1m", True) in health_rows
    assert ("scanner_universe_D", True) in health_rows


def test_boot_backfill_releases_smoke_hold_when_recheck_passes(monkeypatch):
    """Issue #319: boot convergence completion must also re-check + release a
    smoke-check post-hold armed earlier in the process (e.g. armed pre-restart
    or by a prior 09:18 FAIL), not just the 15:30+ periodic loop.

    ``re_check_and_release`` calls ``assert_scanner_pipeline_healthy()`` with
    no overrides, so its providers resolve via default-argument binding —
    patching the module's ``production_*`` names doesn't reach those already-
    bound defaults. The test drives the real production call chain instead
    (broker session + freshness DB read + scanner aggregator), matching prod.
    """
    monkeypatch.setenv("SCANNER_SMOKE_BLOCK_ENABLED", "true")
    monkeypatch.setenv("SCANNER_SYMBOLS", "A")
    smoke_svc._reset_hold_for_tests()
    smoke_svc._last_alert_date = None
    try:
        smoke_svc.set_post_hold(reason="test: pre-armed before boot convergence")
        assert smoke_svc.get_post_hold() is not None

        def fake_check(today=None, *, interval="1m"):
            return _fresh_result(interval)

        with (
            patch.object(sched, "_intervals", return_value=["1m", "D"]),
            patch(
                "services.scanner_universe_backfill.check_and_refresh_if_stale",
                side_effect=fake_check,
            ),
            patch("database.data_health_db.insert_check", return_value=1),
            patch("services.historify_service.wait_for_jobs", return_value={}),
            # The re-check's production providers — now report healthy.
            patch("database.auth_db.get_first_available_api_key", return_value="test_api_key"),
            patch("database.data_health_db.get_latest_check", return_value={"overall_ok": True}),
            patch(
                "services.scanner_service.get_scanner_service",
                return_value=MagicMock(get_today_ohlcv=MagicMock(return_value=(100.0, 1000))),
            ),
        ):
            sched.run_boot_backfill_checks(THURS)

        assert smoke_svc.get_post_hold() is None
    finally:
        smoke_svc._reset_hold_for_tests()
        smoke_svc._last_alert_date = None


def test_boot_backfill_keeps_smoke_hold_when_genuinely_stale(monkeypatch):
    """A re-check that still fails after the boot convergence must keep the
    hold armed — the boot completion must never blindly clear it."""
    monkeypatch.setenv("SCANNER_SMOKE_BLOCK_ENABLED", "true")
    monkeypatch.setenv("SCANNER_SYMBOLS", "A")
    smoke_svc._reset_hold_for_tests()
    smoke_svc._last_alert_date = None
    try:
        smoke_svc.set_post_hold(reason="test: pre-armed before boot convergence")
        assert smoke_svc.get_post_hold() is not None

        def fake_check(today=None, *, interval="1m"):
            return _fresh_result(interval)

        with (
            patch.object(sched, "_intervals", return_value=["1m", "D"]),
            patch(
                "services.scanner_universe_backfill.check_and_refresh_if_stale",
                side_effect=fake_check,
            ),
            patch("database.data_health_db.insert_check", return_value=1),
            patch("services.historify_service.wait_for_jobs", return_value={}),
            # Still genuinely unhealthy — no broker session.
            patch("database.auth_db.get_first_available_api_key", return_value=None),
            patch("database.data_health_db.get_latest_check", return_value={"overall_ok": False}),
            patch("services.scanner_service.get_scanner_service", return_value=None),
        ):
            sched.run_boot_backfill_checks(THURS)

        assert smoke_svc.get_post_hold() is not None
    finally:
        smoke_svc._reset_hold_for_tests()
        smoke_svc._last_alert_date = None


def test_morning_scenario_verified_refresh_persists_ok_and_releases_hold(monkeypatch):
    """Issue #338 acceptance test — the exact 2026-07-06 morning failure,
    end-to-end through the REAL ``data_health_db`` (isolated per-process temp
    DB via test/conftest.py, not mocked) so the persisted-row semantics are
    genuinely exercised rather than stubbed away.

    Scenario: both intervals are stale at the start (a normal morning that
    needs catch-up); ``check_and_refresh_if_stale`` verifies every symbol
    caught up (``still_stale=[]``, ``errors=[]``) — matching the real #304
    verified-refresh contract. ``run_boot_backfill_checks`` must:
      1. persist ``data_health_check`` rows with ``overall_ok=1`` for both
         ``scanner_universe_1m`` and ``scanner_universe_D`` (not 0 — the
         pre-fix bug), and
      2. release a pre-armed smoke-check post-hold via the real
         ``re_check_and_release`` → ``production_freshness_reader`` →
         ``get_latest_check`` chain (gates 2+3 of the smoke check), because
         the rows it reads now say verified-fresh.

    MUST FAIL on the pre-fix tree: pre-fix, ``_persist_health`` wrote
    ``overall_ok = (status=='ok' and not stale_symbols and not errors)``,
    and ``stale_symbols`` here is the PRE-refresh list (non-empty) — so both
    rows would be written ``overall_ok=0`` and the hold would stay armed.
    """
    from database.data_health_db import get_latest_check

    monkeypatch.setenv("SCANNER_SMOKE_BLOCK_ENABLED", "true")
    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE,SBIN")
    smoke_svc._reset_hold_for_tests()
    smoke_svc._last_alert_date = None
    try:
        smoke_svc.set_post_hold(reason="test: 09:18 FAILED — stale feed at boot")
        assert smoke_svc.get_post_hold() is not None

        def fake_check(today=None, *, interval="1m"):
            # Stale at the start (found_stale non-empty)...
            return {
                "status": "ok",
                "interval": interval,
                "stale_symbols": ["RELIANCE", "SBIN"],
                "refreshed": ["RELIANCE", "SBIN"],
                # ...but the #304 post-job verification confirms full catch-up.
                "still_stale": [],
                "errors": [],
                "skipped_fresh": [],
            }

        with (
            patch.object(sched, "_intervals", return_value=["1m", "D"]),
            patch(
                "services.scanner_universe_backfill.check_and_refresh_if_stale",
                side_effect=fake_check,
            ),
            patch("services.historify_service.wait_for_jobs", return_value={}),
            # Real production chain for the re-check's other gates.
            patch("database.auth_db.get_first_available_api_key", return_value="test_api_key"),
            patch(
                "services.scanner_service.get_scanner_service",
                return_value=MagicMock(get_today_ohlcv=MagicMock(return_value=(100.0, 1000))),
            ),
        ):
            sched.run_boot_backfill_checks(THURS)

        # 1. Persisted rows are verified-fresh (overall_ok=1), reading straight
        #    from the real data_health_check table.
        row_1m = get_latest_check("scanner_universe_1m")
        row_d = get_latest_check("scanner_universe_D")
        assert row_1m is not None and row_1m["overall_ok"] is True
        assert row_d is not None and row_d["overall_ok"] is True
        # stale_symbols column now holds the POST-refresh still_stale list.
        assert row_1m["stale_symbols"] == []
        # found_stale (pre-refresh) is kept in details for observability.
        assert row_1m["details"]["found_stale"] == ["RELIANCE", "SBIN"]
        assert row_1m["details"]["still_stale"] == []

        # 2. The smoke-check post-hold is RELEASED — the re-check read these
        #    exact rows and saw verified-fresh.
        assert smoke_svc.get_post_hold() is None
    finally:
        smoke_svc._reset_hold_for_tests()
        smoke_svc._last_alert_date = None


def test_morning_scenario_genuinely_still_stale_keeps_hold_armed(monkeypatch):
    """Companion to the acceptance test above: when the post-job verification
    shows symbols genuinely still stale, the persisted row must be
    ``overall_ok=0`` and a pre-armed hold must stay armed."""
    from database.data_health_db import get_latest_check

    monkeypatch.setenv("SCANNER_SMOKE_BLOCK_ENABLED", "true")
    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE,SBIN")
    smoke_svc._reset_hold_for_tests()
    smoke_svc._last_alert_date = None
    try:
        smoke_svc.set_post_hold(reason="test: 09:18 FAILED — stale feed at boot")
        assert smoke_svc.get_post_hold() is not None

        def fake_check(today=None, *, interval="1m"):
            return {
                "status": "ok",
                "interval": interval,
                "stale_symbols": ["RELIANCE", "SBIN"],
                "refreshed": [],
                "still_stale": ["RELIANCE", "SBIN"],  # verification: still behind
                "errors": [],
                "skipped_fresh": [],
            }

        with (
            patch.object(sched, "_intervals", return_value=["1m", "D"]),
            patch(
                "services.scanner_universe_backfill.check_and_refresh_if_stale",
                side_effect=fake_check,
            ),
            patch("services.historify_service.wait_for_jobs", return_value={}),
            patch("database.auth_db.get_first_available_api_key", return_value="test_api_key"),
            patch(
                "services.scanner_service.get_scanner_service",
                return_value=MagicMock(get_today_ohlcv=MagicMock(return_value=(100.0, 1000))),
            ),
        ):
            sched.run_boot_backfill_checks(THURS)

        row_1m = get_latest_check("scanner_universe_1m")
        assert row_1m is not None and row_1m["overall_ok"] is False
        assert row_1m["stale_symbols"] == ["RELIANCE", "SBIN"]

        # Hold must stay armed — the data is genuinely still stale.
        assert smoke_svc.get_post_hold() is not None
    finally:
        smoke_svc._reset_hold_for_tests()
        smoke_svc._last_alert_date = None


def test_already_fresh_noop_path_persists_ok(monkeypatch):
    """Already-fresh no-op path (no stale symbols found at all) must also
    persist verified overall_ok=1 rows."""
    from database.data_health_db import get_latest_check

    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE,SBIN")

    def fake_check(today=None, *, interval="1m"):
        return {
            "status": "ok",
            "interval": interval,
            "stale_symbols": [],
            "refreshed": [],
            "still_stale": [],
            "errors": [],
            "skipped_fresh": ["RELIANCE", "SBIN"],
        }

    with (
        patch.object(sched, "_intervals", return_value=["1m", "D"]),
        patch(
            "services.scanner_universe_backfill.check_and_refresh_if_stale",
            side_effect=fake_check,
        ),
        patch("services.historify_service.wait_for_jobs", return_value={}),
    ):
        res = sched.run_boot_backfill_checks(THURS)

    assert res["all_fresh"] is True
    row_1m = get_latest_check("scanner_universe_1m")
    assert row_1m is not None and row_1m["overall_ok"] is True


def test_skipped_locked_path_persists_not_ok(monkeypatch):
    """A transient DuckDB-lock skip (``status='skipped_locked'``) verified
    nothing — it must NOT be persisted as overall_ok=1, so the periodic loop
    keeps retrying rather than backing off on a read that never happened."""
    from database.data_health_db import get_latest_check

    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE,SBIN")

    def fake_check(today=None, *, interval="1m"):
        return {
            "status": "skipped_locked",
            "interval": interval,
            "stale_symbols": [],
            "refreshed": [],
            "still_stale": [],
            "errors": [],
            "skipped_fresh": [],
        }

    with (
        patch.object(sched, "_intervals", return_value=["1m", "D"]),
        patch(
            "services.scanner_universe_backfill.check_and_refresh_if_stale",
            side_effect=fake_check,
        ),
        patch("services.historify_service.wait_for_jobs", return_value={}),
    ):
        res = sched.run_boot_backfill_checks(THURS)

    assert res["all_fresh"] is False
    row_1m = get_latest_check("scanner_universe_1m")
    assert row_1m is not None and row_1m["overall_ok"] is False


def test_boot_backfill_smoke_release_noop_without_hold():
    """No hold armed at boot time → the release call is a silent no-op,
    verified by asserting the underlying pipeline check function is never
    invoked (``re_check_and_release`` short-circuits on the armed-check)."""
    smoke_svc._reset_hold_for_tests()
    smoke_svc._last_alert_date = None
    assert smoke_svc.get_post_hold() is None

    def fake_check(today=None, *, interval="1m"):
        return _fresh_result(interval)

    with (
        patch.object(sched, "_intervals", return_value=["1m", "D"]),
        patch(
            "services.scanner_universe_backfill.check_and_refresh_if_stale",
            side_effect=fake_check,
        ),
        patch("database.data_health_db.insert_check", return_value=1),
        patch("services.historify_service.wait_for_jobs", return_value={}),
        patch.object(smoke_svc, "assert_scanner_pipeline_healthy") as mock_check,
    ):
        res = sched.run_boot_backfill_checks(THURS)

    assert res["all_fresh"] is True
    mock_check.assert_not_called()
    assert smoke_svc.get_post_hold() is None


def test_run_backfill_checks_verified_fresh_after_successful_catchup():
    """Issue #338 — finding stale symbols and then VERIFYING they all caught up
    (still_stale empty, no errors) is a fresh outcome. Pre-fix this asserted
    ``all_fresh is False`` purely because ``stale_symbols`` (the PRE-refresh
    list) was non-empty — the exact bug: every normal morning that needed ANY
    catch-up reported not-fresh even when the catch-up fully succeeded."""

    def fake_check(today=None, *, interval="1m"):
        r = _fresh_result(interval)
        if interval == "D":
            r["stale_symbols"] = ["RELIANCE"]  # found stale at the start...
            r["still_stale"] = []  # ...but verified fully caught up.
        return r

    with (
        patch.object(sched, "_intervals", return_value=["1m", "D"]),
        patch(
            "services.scanner_universe_backfill.check_and_refresh_if_stale",
            side_effect=fake_check,
        ),
    ):
        res = sched.run_backfill_checks(THURS)

    assert res["all_fresh"] is True
    assert res["intervals"]["D"]["stale_symbols"] == ["RELIANCE"]
    assert res["intervals"]["D"]["still_stale"] == []


def test_run_backfill_checks_marks_not_fresh_when_genuinely_still_stale():
    """A symbol that is STILL stale after the post-job (#304) verification
    must keep ``all_fresh=False`` — the catch-up genuinely didn't work."""

    def fake_check(today=None, *, interval="1m"):
        r = _fresh_result(interval)
        if interval == "D":
            r["stale_symbols"] = ["RELIANCE"]
            r["still_stale"] = ["RELIANCE"]  # verification says still behind.
        return r

    with (
        patch.object(sched, "_intervals", return_value=["1m", "D"]),
        patch(
            "services.scanner_universe_backfill.check_and_refresh_if_stale",
            side_effect=fake_check,
        ),
    ):
        res = sched.run_backfill_checks(THURS)

    assert res["all_fresh"] is False
    assert res["intervals"]["D"]["still_stale"] == ["RELIANCE"]


# --------------------------------------------------------------------------- #
# 8. Periodic window helpers + clean stop
# --------------------------------------------------------------------------- #
def test_within_window_boundaries():
    end_t = time(17, 0)
    assert sched._within_window(time(15, 30), end_t) is True
    assert sched._within_window(time(17, 0), end_t) is True
    assert sched._within_window(time(15, 29), end_t) is False
    assert sched._within_window(time(17, 1), end_t) is False


def test_periodic_tick_skips_outside_window_runs_inside():
    end_t = time(17, 0)

    # Weekend → never runs.
    ran, res = sched._periodic_tick(
        datetime(SAT.year, SAT.month, SAT.day, 16, 0, tzinfo=_IST), end_t
    )
    assert ran is False and res is None

    # Weekday after 17:00 → outside window.
    ran, res = sched._periodic_tick(datetime(2026, 6, 11, 17, 30, tzinfo=_IST), end_t)
    assert ran is False and res is None

    # Weekday inside the window → runs.
    # Issue #158 D3 added a broker-session gate; mock it to True so the
    # within-window scenario this test verifies still runs (the gate's
    # behaviour is exercised in test_scanner_watchdog_and_backfill_gate).
    with (
        patch("services.broker_session_health.is_live_broker_session", return_value=True),
        patch.object(
            sched,
            "run_backfill_checks",
            return_value={"intervals": {}, "all_fresh": True, "errors": []},
        ),
        patch.object(sched, "_persist_health"),
    ):
        ran, res = sched._periodic_tick(datetime(2026, 6, 11, 16, 0, tzinfo=_IST), end_t)
    assert ran is True and res["all_fresh"] is True


def test_periodic_loop_backs_off_only_on_verified_fresh(monkeypatch):
    """Issue #338 — the periodic loop's backoff must key off VERIFIED
    freshness, end-to-end through the real ``run_backfill_checks`` (not a
    stubbed ``all_fresh``). A tick that found stale symbols but verified them
    all caught up must back off; a tick with genuinely still-stale symbols
    must NOT back off (keep the short retry interval)."""
    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE,SBIN")

    def fake_check_verified(today=None, *, interval="1m"):
        return {
            "status": "ok",
            "interval": interval,
            "stale_symbols": ["RELIANCE"],
            "refreshed": ["RELIANCE"],
            "still_stale": [],
            "errors": [],
            "skipped_fresh": [],
        }

    with (
        patch("services.broker_session_health.is_live_broker_session", return_value=True),
        patch.object(sched, "_intervals", return_value=["1m", "D"]),
        patch(
            "services.scanner_universe_backfill.check_and_refresh_if_stale",
            side_effect=fake_check_verified,
        ),
        patch("services.historify_service.wait_for_jobs", return_value={}),
    ):
        ran, res = sched._periodic_tick(datetime(2026, 6, 11, 16, 0, tzinfo=_IST), time(17, 0))
    assert ran is True
    assert res["all_fresh"] is True  # verified fresh → loop should back off

    def fake_check_still_stale(today=None, *, interval="1m"):
        return {
            "status": "ok",
            "interval": interval,
            "stale_symbols": ["RELIANCE"],
            "refreshed": [],
            "still_stale": ["RELIANCE"],
            "errors": [],
            "skipped_fresh": [],
        }

    with (
        patch("services.broker_session_health.is_live_broker_session", return_value=True),
        patch.object(sched, "_intervals", return_value=["1m", "D"]),
        patch(
            "services.scanner_universe_backfill.check_and_refresh_if_stale",
            side_effect=fake_check_still_stale,
        ),
        patch("services.historify_service.wait_for_jobs", return_value={}),
    ):
        ran, res = sched._periodic_tick(datetime(2026, 6, 11, 16, 0, tzinfo=_IST), time(17, 0))
    assert ran is True
    assert res["all_fresh"] is False  # still genuinely stale → keep retrying


def test_periodic_loop_exits_cleanly_when_stopped():
    sched._stop_event.set()
    try:
        sched._periodic_loop()
    finally:
        sched._stop_event.clear()


def test_intervals_env_filters_unknown_tokens(monkeypatch):
    monkeypatch.setenv("SCANNER_BACKFILL_INTERVALS", "1m,foo,D")
    assert sched._intervals() == ["1m", "D"]
    monkeypatch.setenv("SCANNER_BACKFILL_INTERVALS", "garbage")
    assert sched._intervals() == ["1m", "D"]  # falls back to both


def test_backfill_disabled_skips_init(monkeypatch):
    monkeypatch.setenv("SCANNER_BACKFILL_ENABLED", "false")
    with patch.object(sched, "_boot_worker") as m_worker:
        sched.init_scanner_backfill_scheduler()
    m_worker.assert_not_called()
