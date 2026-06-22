"""E2E tests for services/scanner_smoke_check_service.py.

The smoke check has four gates (aggregator coverage, 1m freshness, D
freshness, broker session). These tests inject the providers — no live
scanner, no DBs, no Telegram — and verify each failure path independently.

The Friday 2026-06-19 outage is the regression these tests prevent: a
silent morning with stale stored 1m + D + a non-running OpenAlgo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import services.scanner_smoke_check_service as svc

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _reset_dedup_state():
    """Each test starts with the per-process dedup state clean so a CRIT
    Telegram in one test cannot block the assertion in the next."""
    svc._last_alert_date = None
    yield
    svc._last_alert_date = None


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch):
    """Force the flag on for every test (default ON in production, but
    .env handling differs across CI / dev workstations)."""
    monkeypatch.setenv("SCANNER_SMOKE_CHECK_ENABLED", "true")
    monkeypatch.setenv("SCANNER_SMOKE_MIN_COVERAGE", "0.5")


def _stub_providers(
    *,
    universe: list[str],
    aggregator_covered: list[str],
    fresh_1m_ok: bool = True,
    fresh_d_ok: bool = True,
    session_ok: bool = True,
):
    """Build a tuple of injected providers for the test.

    ``aggregator_covered`` is the subset of ``universe`` for which the
    intraday provider returns a non-None close (i.e. live aggregator bars
    exist). Everything else returns ``(None, None)`` — the "no bar today"
    signal.
    """
    covered_set = set(aggregator_covered)
    notified: list[str] = []
    health_rows: list[dict] = []

    def universe_provider() -> list[str]:
        return list(universe)

    def intraday_provider(symbol: str, _as_of):
        if symbol in covered_set:
            return 100.0, 1234  # stub close + volume
        return None, None

    def freshness_reader(strategy_name: str):
        if strategy_name == "scanner_universe_1m":
            return {"overall_ok": fresh_1m_ok}
        if strategy_name == "scanner_universe_D":
            return {"overall_ok": fresh_d_ok}
        return None

    def broker_session_checker() -> bool:
        return session_ok

    def notifier(msg: str) -> None:
        notified.append(msg)

    def health_writer(overall_ok, stale_symbols, details, alert_sent):
        health_rows.append(
            {
                "overall_ok": overall_ok,
                "stale_symbols": list(stale_symbols),
                "details": dict(details),
                "alert_sent": bool(alert_sent),
            }
        )

    return (
        {
            "universe_provider": universe_provider,
            "intraday_provider": intraday_provider,
            "freshness_reader": freshness_reader,
            "broker_session_checker": broker_session_checker,
            "notifier": notifier,
            "health_writer": health_writer,
        },
        notified,
        health_rows,
    )


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_all_gates_pass_no_telegram_health_row_ok():
    """All four gates green → ok=True, no Telegram, health row written with
    overall_ok=True and alert_sent=False."""
    providers, notified, health_rows = _stub_providers(
        universe=["AAA", "BBB", "CCC", "DDD"],
        aggregator_covered=["AAA", "BBB", "CCC", "DDD"],
    )
    ok, details = svc.assert_scanner_pipeline_healthy(**providers)
    assert ok is True
    assert notified == []
    assert len(health_rows) == 1
    assert health_rows[0]["overall_ok"] is True
    assert health_rows[0]["alert_sent"] is False
    assert details["aggregator_frac"] == 1.0


# --------------------------------------------------------------------------- #
# Gate 1: aggregator coverage
# --------------------------------------------------------------------------- #


def test_aggregator_coverage_just_below_threshold_alerts():
    """Coverage 49% (below default 0.5) → ok=False, CRIT Telegram, health row
    overall_ok=False, alert_sent=True."""
    providers, notified, health_rows = _stub_providers(
        universe=[f"S{i:03d}" for i in range(100)],
        aggregator_covered=[f"S{i:03d}" for i in range(49)],  # 49/100 = 0.49
    )
    ok, details = svc.assert_scanner_pipeline_healthy(**providers)
    assert ok is False
    assert len(notified) == 1
    assert "aggregator coverage 49/100" in notified[0]
    assert health_rows[0]["overall_ok"] is False
    assert health_rows[0]["alert_sent"] is True
    assert details["aggregator_ok"] is False


def test_aggregator_coverage_at_threshold_passes_gate():
    """Coverage exactly at the threshold (50%) passes — proves the comparison
    is >=, not >."""
    providers, notified, _rows = _stub_providers(
        universe=[f"S{i:03d}" for i in range(10)],
        aggregator_covered=[f"S{i:03d}" for i in range(5)],  # 5/10 = 0.50
    )
    ok, details = svc.assert_scanner_pipeline_healthy(**providers)
    assert ok is True
    assert notified == []
    assert details["aggregator_ok"] is True


# --------------------------------------------------------------------------- #
# Gate 2 + 3: stored freshness
# --------------------------------------------------------------------------- #


def test_stale_1m_alerts_even_when_aggregator_full():
    """Aggregator is 100% covered but scanner_universe_1m freshness row is
    not OK → ok=False, CRIT names the 1m staleness reason."""
    providers, notified, _rows = _stub_providers(
        universe=["A", "B"],
        aggregator_covered=["A", "B"],
        fresh_1m_ok=False,
    )
    ok, details = svc.assert_scanner_pipeline_healthy(**providers)
    assert ok is False
    assert "scanner_universe_1m stale" in notified[0]
    assert details["fresh_1m_ok"] is False


def test_stale_d_alerts():
    """scanner_universe_D not OK → ok=False with the D reason."""
    providers, notified, _rows = _stub_providers(
        universe=["A", "B"],
        aggregator_covered=["A", "B"],
        fresh_d_ok=False,
    )
    ok, _ = svc.assert_scanner_pipeline_healthy(**providers)
    assert ok is False
    assert "scanner_universe_D stale" in notified[0]


def test_missing_freshness_rows_treated_as_stale():
    """If the freshness reader returns None (e.g. data_health_check table
    empty), we treat it as not-OK rather than silently passing — a fresh
    install must not look healthy until the convergence backfill has run."""
    universe = ["A", "B"]
    notified: list[str] = []

    ok, details = svc.assert_scanner_pipeline_healthy(
        universe_provider=lambda: universe,
        intraday_provider=lambda *_a: (100.0, 1234),
        freshness_reader=lambda _name: None,  # no row at all
        broker_session_checker=lambda: True,
        notifier=lambda msg: notified.append(msg),
        health_writer=lambda *_a, **_k: None,
    )
    assert ok is False
    assert details["fresh_1m_ok"] is False
    assert details["fresh_d_ok"] is False
    assert len(notified) == 1


# --------------------------------------------------------------------------- #
# Gate 4: broker session
# --------------------------------------------------------------------------- #


def test_no_broker_session_alerts():
    """Broker session not live → ok=False with the broker-session reason."""
    providers, notified, _rows = _stub_providers(
        universe=["A"],
        aggregator_covered=["A"],
        session_ok=False,
    )
    ok, details = svc.assert_scanner_pipeline_healthy(**providers)
    assert ok is False
    assert "broker session not live" in notified[0]
    assert details["broker_session_ok"] is False


# --------------------------------------------------------------------------- #
# Operational concerns
# --------------------------------------------------------------------------- #


def test_flag_off_is_noop(monkeypatch):
    """SCANNER_SMOKE_CHECK_ENABLED=false → service returns (True, {skipped})
    with no provider calls; safe to deploy + flip off without re-init."""
    monkeypatch.setenv("SCANNER_SMOKE_CHECK_ENABLED", "false")
    calls = []

    def _trap(*_a, **_k):
        calls.append("called")
        return [], None, None, None

    ok, details = svc.assert_scanner_pipeline_healthy(
        universe_provider=lambda: (calls.append("universe"), ["A"])[1],
        intraday_provider=lambda *_a: (calls.append("intraday"), (100.0, 1))[1],
        freshness_reader=lambda _n: (calls.append("fresh"), {"overall_ok": True})[1],
        broker_session_checker=lambda: (calls.append("session"), True)[1],
        notifier=lambda _m: calls.append("notify"),
        health_writer=lambda *_a, **_k: calls.append("health"),
    )
    assert ok is True
    assert details == {"skipped": True}
    assert calls == []  # NOTHING was called


def test_empty_universe_is_noop():
    """SCANNER_SYMBOLS unset → universe_provider returns []. We must not
    raise and must not alert — an unconfigured deploy is not a failure."""
    providers, notified, health_rows = _stub_providers(
        universe=[],
        aggregator_covered=[],
    )
    ok, details = svc.assert_scanner_pipeline_healthy(**providers)
    assert ok is True
    assert notified == []
    assert health_rows == []
    assert details == {"universe_empty": True}


def test_alert_deduped_within_same_day():
    """A CRIT alert fires once per day; a second run on the same date with
    the same failure must NOT re-Telegram (operator paged twice = unhelpful)."""
    fixed_day = datetime(2026, 6, 19, 9, 18, tzinfo=_IST)
    providers, notified, health_rows = _stub_providers(
        universe=["A"],
        aggregator_covered=[],  # 0% coverage — fails gate 1
    )
    # First run — alerts.
    ok1, _ = svc.assert_scanner_pipeline_healthy(as_of=fixed_day, **providers)
    assert ok1 is False
    assert len(notified) == 1
    # Second run, same day — must NOT alert again.
    ok2, _ = svc.assert_scanner_pipeline_healthy(as_of=fixed_day, **providers)
    assert ok2 is False
    assert len(notified) == 1, "dedup failed — fired twice on the same day"
    # Health rows still written each time so the dashboard has a trail,
    # but alert_sent is False on the second one.
    assert len(health_rows) == 2
    assert health_rows[0]["alert_sent"] is True
    assert health_rows[1]["alert_sent"] is False


def test_dedup_resets_across_days():
    """Day-boundary roll → next day's failure alerts again, even if the
    previous day's alert already fired."""
    day1 = datetime(2026, 6, 19, 9, 18, tzinfo=_IST)
    day2 = datetime(2026, 6, 22, 9, 18, tzinfo=_IST)
    providers, notified, _rows = _stub_providers(
        universe=["A"],
        aggregator_covered=[],
    )
    svc.assert_scanner_pipeline_healthy(as_of=day1, **providers)
    svc.assert_scanner_pipeline_healthy(as_of=day2, **providers)
    assert len(notified) == 2


def test_multiple_gate_failures_all_listed_in_alert():
    """When gates 1+2+3+4 all fail, the alert message names every reason —
    triage shouldn't require trial-and-error."""
    providers, notified, _rows = _stub_providers(
        universe=["A", "B"],
        aggregator_covered=[],
        fresh_1m_ok=False,
        fresh_d_ok=False,
        session_ok=False,
    )
    ok, _ = svc.assert_scanner_pipeline_healthy(**providers)
    assert ok is False
    msg = notified[0]
    assert "aggregator coverage 0/2" in msg
    assert "scanner_universe_1m stale" in msg
    assert "scanner_universe_D stale" in msg
    assert "broker session not live" in msg


# --------------------------------------------------------------------------- #
# Job registration
# --------------------------------------------------------------------------- #


def test_scheduler_add_job_method_exists():
    """Verify that HistorifyScheduler exposes an add_job passthrough method.

    This is a regression test for B3 — the smoke check job had never
    registered because the wrapper didn't expose add_job. The fix adds
    the passthrough method to maintain consistency with other delegated
    methods (remove_job, get_job, pause_job, resume_job).
    """
    from services.historify_scheduler_service import HistorifyScheduler

    scheduler = HistorifyScheduler()
    # Verify add_job method exists and is callable
    assert hasattr(scheduler, "add_job"), "HistorifyScheduler missing add_job method"
    assert callable(scheduler.add_job), "add_job must be callable"
