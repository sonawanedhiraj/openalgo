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

from datetime import date, datetime
from unittest.mock import patch

import pandas as pd
import pytest

import services.scanner_backfill_scheduler as sched
import services.scanner_reference_data as refdata
import services.scanner_universe_backfill as sub
from test.fixtures.frame_factory import make_historify_daily_frame, make_live_5m_frame

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


# --------------------------------------------------------------------------- #
# Issue #314 — feed the re-settle's broker-verified closes into the
# scanner_reference_data prev-close registry unconditionally.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_prev_close_registry():
    """Isolate the module-level registry across tests in this file."""
    refdata.reset_for_tests()
    yield
    refdata.reset_for_tests()


class _FakeProviderWithDaily:
    """Minimal ``ScannerHistoryProvider`` stand-in: ``refresh()`` + ``get_daily()``."""

    def __init__(self, daily: dict[str, pd.DataFrame]):
        self._daily = daily
        self.refreshed = False

    def refresh(self):
        self.refreshed = True
        return {"symbols_loaded": len(self._daily), "errors": []}

    def get_daily(self, symbol: str):
        return self._daily.get(symbol)


def _patch_resettle_pipeline(universe, provider, *, job_id="jD"):
    """Common patch set for a successful resettle run driving the registry wiring."""
    return (
        patch.object(sub, "scanner_universe_symbols", return_value=universe),
        patch.object(
            sub,
            "backfill_scanner_universe",
            return_value={
                "status": "success",
                "job_id": job_id,
                "symbols": universe,
                "interval": "D",
            },
        ),
        patch("services.historify_service.wait_for_jobs", return_value={job_id: "completed"}),
        patch("services.scanner_history_provider.get_provider", return_value=provider),
    )


def test_resettle_boot_case_records_t1_close_and_certificate_serves_it():
    """Boot / pre-market re-settle: the daily frame's latest bar is dated
    STRICTLY BEFORE ``today`` (a pre-market run has no bar for today yet) —
    its close is recorded as today's T-1 and ``get_broker_prev_close`` serves
    it."""
    today = date(2026, 7, 3)  # Friday
    universe = ["DELHIVERY"]
    # 07-01 and 07-02 settled bars only — no 07-03 (today) bar exists yet.
    daily = {
        "DELHIVERY": make_historify_daily_frame(
            [505.0, 510.0],
            end_date=date(2026, 7, 2),  # last close 510.0 ← T-1 settled close
        )
    }
    provider = _FakeProviderWithDaily(daily)

    p1, p2, p3, p4 = _patch_resettle_pipeline(universe, provider)
    with p1, p2, p3, p4:
        res = sub.resettle_recent_daily(today, days=2)

    assert res["status"] == "ok"
    assert res["resettled"] is True
    assert res["prev_close_registry"]["recorded"] == ["DELHIVERY"]
    assert res["prev_close_registry"]["errors"] == 0

    got = refdata.get_broker_prev_close("DELHIVERY", today=today)
    assert got is not None
    assert got[0] == 510.0


def test_resettle_post_close_case_does_not_poison_registry_with_todays_own_close():
    """Post-close re-settle (16:00 IST job): the daily frame now ALSO carries
    TODAY's own just-settled bar. The registry must hold YESTERDAY's close,
    never today's own — recording today's close as "today's prev-close" would
    poison every subsequent rule evaluation for the rest of the day. This is
    the load-bearing semantic trap called out in issue #314."""
    today = date(2026, 7, 3)  # Friday
    universe = ["DELHIVERY"]
    daily = {
        "DELHIVERY": make_historify_daily_frame(
            # 07-01=505.0, 07-02=510.0 (← the correct T-1 close),
            # 07-03=520.0 (← TODAY's own settle — must be ignored).
            [505.0, 510.0, 520.0],
            end_date=today,
        )
    }
    provider = _FakeProviderWithDaily(daily)

    p1, p2, p3, p4 = _patch_resettle_pipeline(universe, provider)
    with p1, p2, p3, p4:
        res = sub.resettle_recent_daily(today, days=2)

    assert res["prev_close_registry"]["recorded"] == ["DELHIVERY"]
    got = refdata.get_broker_prev_close("DELHIVERY", today=today)
    assert got is not None
    assert got[0] == 510.0, "must record YESTERDAY's close, not today's own settled bar"
    assert got[0] != 520.0


def test_resettle_registry_fail_graceful_never_breaks_resettle():
    """A registry-record failure (e.g. a symbol's frame is malformed) must
    never surface as a resettle failure — only that symbol is skipped/errored,
    the overall resettle still reports success."""
    universe = ["DELHIVERY", "BROKEN"]
    today = date(2026, 7, 3)
    good = make_historify_daily_frame([505.0, 510.0], end_date=date(2026, 7, 2))
    # "BROKEN" has no timestamp column at all — must be skipped, not raise.
    broken = pd.DataFrame({"close": [500.0]})
    provider = _FakeProviderWithDaily({"DELHIVERY": good, "BROKEN": broken})

    p1, p2, p3, p4 = _patch_resettle_pipeline(universe, provider)
    with p1, p2, p3, p4:
        res = sub.resettle_recent_daily(today, days=2)

    assert res["status"] == "ok"
    assert res["resettled"] is True
    assert res["prev_close_registry"]["recorded"] == ["DELHIVERY"]
    assert "BROKEN" in res["prev_close_registry"]["skipped"]
    assert res["prev_close_registry"]["errors"] == 0
    assert refdata.get_broker_prev_close("DELHIVERY", today=today)[0] == 510.0
    assert refdata.get_broker_prev_close("BROKEN", today=today) is None


def test_resettle_registry_wiring_raising_provider_never_breaks_resettle():
    """If ``get_provider()`` itself blows up inside the registry helper, the
    resettle result must still report the (already-successful) core resettle
    outcome — never raise, never flip ``resettled`` back to False."""
    universe = ["DELHIVERY"]
    today = date(2026, 7, 3)

    class _ExplodingProvider:
        def refresh(self):
            return {"symbols_loaded": 1, "errors": []}

        def get_daily(self, symbol):
            raise RuntimeError("duckdb blew up")

    provider = _ExplodingProvider()
    p1, p2, p3, p4 = _patch_resettle_pipeline(universe, provider)
    with p1, p2, p3, p4:
        res = sub.resettle_recent_daily(today, days=2)  # must NOT raise

    assert res["status"] == "ok"
    assert res["resettled"] is True
    assert res["prev_close_registry"]["errors"] == 1
    assert res["prev_close_registry"]["recorded"] == []
    assert refdata.get_broker_prev_close("DELHIVERY", today=today) is None


def test_resettle_registry_not_touched_on_provider_refresh_failure():
    """When ``get_provider().refresh()`` itself fails, the registry wiring must
    not run at all — a stale/pre-resettle daily cache must never seed the
    registry with unverified data."""
    universe = ["DELHIVERY"]
    today = date(2026, 7, 3)

    class _FailingRefreshProvider:
        def refresh(self):
            raise RuntimeError("refresh failed")

        def get_daily(self, symbol):
            raise AssertionError("get_daily must not be called when refresh() failed")

    provider = _FailingRefreshProvider()
    p1, p2, p3, p4 = _patch_resettle_pipeline(universe, provider)
    with p1, p2, p3, p4:
        res = sub.resettle_recent_daily(today, days=2)  # must NOT raise

    assert res["status"] == "ok"
    assert res["resettled"] is True
    assert "prev_close_registry" not in res
    assert refdata.get_broker_prev_close("DELHIVERY", today=today) is None


def test_resettle_gap_closure_certificate_rejects_stale_reference_on_healthy_1m_day():
    """End-to-end: on a "healthy 1m" day the aggregator_seeder broker fallback
    never fires (registry otherwise empty — historify 1m was NOT short, so
    ``_read_1m_bars_for_symbol`` never reaches the broker arm that would have
    recorded a prev-close). After the #314 re-settle wiring runs, the registry
    has today's real broker-verified T-1 close anyway, and
    ``compute_reference_certificate`` REJECTS a stale ``yest_d_close`` that
    diverges from it — the DELHIVERY 475.4-vs-510 shape from the golden
    2026-07-02 incident (issue #305), now reached via the re-settle path
    instead of the seeder fallback."""
    today = date(2026, 7, 2)
    now_ist = datetime(2026, 7, 2, 9, 30, tzinfo=refdata._IST)
    universe = ["DELHIVERY"]
    # The re-settle's own re-fetched daily bars carry the REAL settled T-1
    # close (510.0) — the broker-verified value.
    daily = {
        "DELHIVERY": make_historify_daily_frame([500.0, 505.0, 510.0], end_date=date(2026, 7, 1))
    }
    provider = _FakeProviderWithDaily(daily)

    p1, p2, p3, p4 = _patch_resettle_pipeline(universe, provider)
    with p1, p2, p3, p4:
        res = sub.resettle_recent_daily(today, days=2)
    assert res["prev_close_registry"]["recorded"] == ["DELHIVERY"]
    registered = refdata.get_broker_prev_close("DELHIVERY", today=today)
    assert registered is not None
    assert registered[0] == 510.0

    # The rule's own daily frame — reused from the scanner_service/provider —
    # is STALE (yest close 475.4, the #277/#299 "frozen 09:45 snapshot"
    # shape): reusing the exact DELHIVERY fixture numbers from the golden
    # #305 incident (test/golden_scanner/test_golden_2026_07_02_delhivery.py).
    # A live 5m frame is required so ``derive_today_and_yest`` takes Path B
    # (live-5m-derived today_d + latest-settled-bars-daily yest_d) instead of
    # Path C, which needs the daily frame's last bar dated exactly `today`.
    stale_rule_daily = make_historify_daily_frame(
        [470.0] * 204 + [475.4], end_date=date(2026, 6, 30), volumes=800_000.0
    )
    stale_rule_5m = make_live_5m_frame([470.0], today, volumes=100_000.0)
    cert = refdata.compute_reference_certificate(
        "DELHIVERY", stale_rule_5m, stale_rule_daily, exchange="NSE", now_ist=now_ist
    )
    assert cert["reference_certified"] is False
    assert cert["reference_settled_close"] == 475.4
    assert cert["reference_broker_prev_close"] == 510.0
    assert cert["reference_divergence_pct"] > refdata.reference_divergence_max_pct()

    # And the rule-level consequence (mirrors the golden #305 regression, but
    # driven entirely by the #314 re-settle-fed registry entry rather than a
    # direct ``record_broker_prev_close`` call): BUY must NOT fire.
    import services.scan_rules.fno_intraday_buy_chartink as buymod

    buymod._uncertified_warned.clear()
    indicators = {"symbol": "DELHIVERY", "exchange": "NSE", **cert}
    assert buymod.rule(None, indicators) is False, (
        "BUY fired despite a re-settle-sourced registry entry rejecting the "
        "stale reference — the #314 wiring did not close the coverage gap"
    )
