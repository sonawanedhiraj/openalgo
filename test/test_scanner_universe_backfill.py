"""Tests for the scanner-universe boot+periodic state-convergence backfill.

Covers the scanner-side analogue of the sector_follow convergence (Bugs A + B
from the 2026-06-13 Friday replay): keep the ``SCANNER_SYMBOLS`` F&O universe
fresh in BOTH ``1m`` and ``D``, fetching only the stale tail. Fully mocked —
``get_data_freshness`` (the DuckDB read) and the backfill pipeline are patched,
so no real broker download or DuckDB access happens.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

import services.scanner_backfill_scheduler as sched
import services.scanner_universe_backfill as sub

_IST = timezone(timedelta(hours=5, minutes=30))

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


def test_run_backfill_checks_marks_not_fresh_when_any_interval_stale():
    def fake_check(today=None, *, interval="1m"):
        r = _fresh_result(interval)
        if interval == "D":
            r["stale_symbols"] = ["RELIANCE"]
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
    assert res["intervals"]["D"]["stale_symbols"] == ["RELIANCE"]


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
