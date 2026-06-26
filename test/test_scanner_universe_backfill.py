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

_RESULT_KEYS = {"status", "interval", "stale_symbols", "refreshed", "errors", "skipped_fresh"}


def _epoch(d: date, hh: int = 15, mm: int = 29) -> int:
    """UTC epoch for an IST wall-clock time on ``d`` (matches market_data convention)."""
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=_IST).timestamp())


# --------------------------------------------------------------------------- #
# 1. Stale → triggers refresh of only the stale subset, for the given interval
# --------------------------------------------------------------------------- #
def test_stale_triggers_refresh_of_stale_subset_1m():
    universe = ["RELIANCE", "SBIN", "TCS"]
    captured: dict = {}

    def fake_backfill(start, end, interval="1m", symbols=None):
        captured["symbols"] = symbols
        captured["start"] = start
        captured["end"] = end
        captured["interval"] = interval
        return {"status": "success", "job_id": "j1", "symbols": symbols, "interval": interval}

    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value={s: _epoch(WED) for s in universe},
        ),
        patch.object(sub, "backfill_scanner_universe", side_effect=fake_backfill),
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="1m")

    # `job_id` (issue #154) may be added when a backfill job is submitted —
    # subset check keeps the contract additive.
    assert _RESULT_KEYS.issubset(set(res))
    assert res["status"] == "ok"
    assert res["interval"] == "1m"
    assert set(res["stale_symbols"]) == set(universe)
    assert set(res["refreshed"]) == set(universe)
    assert res["skipped_fresh"] == []
    assert res["errors"] == []
    assert set(captured["symbols"]) == set(universe)
    assert captured["interval"] == "1m"
    assert captured["end"] == "2026-06-11"
    assert captured["start"] < captured["end"]


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
        return {"status": "success", "job_id": "j", "symbols": symbols, "interval": interval}

    with (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch(
            "services.data_freshness_service.get_data_freshness",
            return_value=freshness,
        ),
        patch.object(sub, "backfill_scanner_universe", side_effect=fake_backfill),
    ):
        res = sub.check_and_refresh_if_stale(THURS, interval="D")

    assert res["interval"] == "D"
    assert set(res["stale_symbols"]) == set(stale)
    assert set(res["skipped_fresh"]) == set(fresh)
    assert set(res["refreshed"]) == set(stale)
    # The fresh half is never re-fetched.
    assert set(captured["symbols"]) == set(stale)
    assert captured["interval"] == "D"


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
