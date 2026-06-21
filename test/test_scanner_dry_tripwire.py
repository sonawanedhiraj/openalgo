"""E2E tests for services/scanner_dry_tripwire_service.py.

Cover every branch of the tripwire's decision tree: off-hours, weekend,
warm-up, no broker session, fresh row (no alert), stale gap with
Chartink-alive (CRIT), stale gap with Chartink-dry (WARN), per-day-
per-severity dedup, and dedup-reset across days.

The Friday 2026-06-19 outage is the regression these tests prevent: the
scanner read live ticks but evaluated against ~6-day-old daily gates and
produced 0 BUY hits for the whole session. The completeness metric sat at
56% (above its 50% WARN floor) so it never alerted. This tripwire catches
that exact silent-failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import services.scanner_dry_tripwire_service as svc

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _reset_dedup_state():
    """Clear per-process dedup so each test starts from a known baseline."""
    svc._last_crit_date = None
    svc._last_warn_date = None
    yield
    svc._last_crit_date = None
    svc._last_warn_date = None


@pytest.fixture(autouse=True)
def _flags_on(monkeypatch):
    monkeypatch.setenv("SCANNER_DRY_TRIPWIRE_ENABLED", "true")
    monkeypatch.setenv("SCANNER_DRY_THRESHOLD_MIN", "30")
    monkeypatch.setenv("SCANNER_DRY_CHECK_INTERVAL_MIN", "5")


def _ist(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=_IST)


def _stub(
    *,
    latest_inhouse: datetime | None,
    chartink_alive: bool = False,
    session_ok: bool = True,
):
    notified: list[tuple[str, str]] = []
    health_rows: list[dict] = []

    def latest_inhouse_provider():
        return latest_inhouse

    def chartink_has_rows_since(_cutoff):
        return chartink_alive

    def broker_session_checker():
        return session_ok

    def notifier(msg, severity):
        notified.append((severity, msg))

    def health_writer(severity, details, alert_sent):
        health_rows.append(
            {"severity": severity, "details": dict(details), "alert_sent": alert_sent}
        )

    return (
        {
            "latest_inhouse_provider": latest_inhouse_provider,
            "chartink_has_rows_since": chartink_has_rows_since,
            "broker_session_checker": broker_session_checker,
            "notifier": notifier,
            "health_writer": health_writer,
        },
        notified,
        health_rows,
    )


# --------------------------------------------------------------------------- #
# Skips (status: off_hours / warmup / no_broker / flag_off)
# --------------------------------------------------------------------------- #


def test_off_hours_is_silent_after_close():
    """16:00 IST → market closed → status off_hours, no provider work."""
    providers, notified, health = _stub(latest_inhouse=None)
    res = svc.check_dry_scanner(as_of=_ist(2026, 6, 22, 16, 0), **providers)
    assert res == {"status": "off_hours"}
    assert notified == [] and health == []


def test_off_hours_is_silent_on_weekend():
    """Saturday 11:00 IST → weekday() returns 5 → off_hours."""
    providers, notified, _h = _stub(latest_inhouse=None)
    res = svc.check_dry_scanner(as_of=_ist(2026, 6, 20, 11, 0), **providers)  # Saturday
    assert res["status"] == "off_hours"
    assert notified == []


def test_warmup_window_is_silent():
    """09:20 IST → still in the 09:15-09:30 warm-up, no scan_results yet
    expected → status warmup, never alerts."""
    providers, notified, _h = _stub(latest_inhouse=None)
    res = svc.check_dry_scanner(as_of=_ist(2026, 6, 22, 9, 20), **providers)
    assert res == {"status": "warmup"}
    assert notified == []


def test_no_broker_session_is_silent():
    """Operator off → silence is expected. We don't want a 6.5-hour CRIT
    every weekday for an unconfigured laptop."""
    providers, notified, _h = _stub(latest_inhouse=None, session_ok=False)
    res = svc.check_dry_scanner(as_of=_ist(2026, 6, 22, 11, 0), **providers)
    assert res == {"status": "no_broker"}
    assert notified == []


def test_flag_off_short_circuits(monkeypatch):
    monkeypatch.setenv("SCANNER_DRY_TRIPWIRE_ENABLED", "false")
    called = []
    res = svc.check_dry_scanner(
        as_of=_ist(2026, 6, 22, 11, 0),
        latest_inhouse_provider=lambda: (called.append("latest"), None)[1],
        chartink_has_rows_since=lambda _c: (called.append("chartink"), False)[1],
        broker_session_checker=lambda: (called.append("session"), True)[1],
        notifier=lambda _m, _s: called.append("notify"),
        health_writer=lambda *_a, **_k: called.append("health"),
    )
    assert res == {"status": "flag_off"}
    assert called == []


# --------------------------------------------------------------------------- #
# Healthy path (status: ok)
# --------------------------------------------------------------------------- #


def test_recent_row_within_threshold_is_ok():
    """Last row 5 min ago, threshold 30 → ok, no alert, but heartbeat
    health row is written (so dashboards see a healthy trace)."""
    now = _ist(2026, 6, 22, 11, 0)
    providers, notified, health = _stub(latest_inhouse=now - timedelta(minutes=5))
    res = svc.check_dry_scanner(as_of=now, **providers)
    assert res["status"] == "ok"
    assert res["gap_min"] == pytest.approx(5.0, abs=0.01)
    assert notified == []
    assert len(health) == 1
    assert health[0]["severity"] == "ok"
    assert health[0]["alert_sent"] is False


# --------------------------------------------------------------------------- #
# Fires: CRIT (broken pipeline)
# --------------------------------------------------------------------------- #


def test_stale_gap_with_chartink_alive_fires_crit():
    """30-min gap AND Chartink HAS recent rows → in-house is broken →
    CRIT alert with the diagnosis text."""
    now = _ist(2026, 6, 22, 11, 0)
    providers, notified, health = _stub(
        latest_inhouse=now - timedelta(minutes=35), chartink_alive=True
    )
    res = svc.check_dry_scanner(as_of=now, **providers)
    assert res["status"] == "alerted_crit"
    assert res["severity"] == "CRIT"
    assert len(notified) == 1
    severity, msg = notified[0]
    assert severity == "CRIT"
    assert "🚨" in msg
    assert "SCANNER CRIT" in msg
    assert "Chartink HAS recent hits" in msg
    assert "35 min" in msg
    assert health[0]["severity"] == "crit"
    assert health[0]["alert_sent"] is True


def test_stale_gap_with_no_inhouse_row_yet_fires():
    """No inhouse row at all today (latest_inhouse=None) → gap is measured
    from end-of-warmup (09:30 IST). At 11:00 IST that's 90 min → fires."""
    now = _ist(2026, 6, 22, 11, 0)
    providers, notified, _h = _stub(latest_inhouse=None, chartink_alive=True)
    res = svc.check_dry_scanner(as_of=now, **providers)
    assert res["status"] == "alerted_crit"
    assert res["gap_min"] == pytest.approx(90.0, abs=0.01)
    assert "never (no rows today)" in notified[0][1]


# --------------------------------------------------------------------------- #
# Fires: WARN (quiet market)
# --------------------------------------------------------------------------- #


def test_stale_gap_with_chartink_dry_fires_warn():
    """30-min gap AND Chartink also dry → genuinely quiet market →
    WARN alert with the quiet-market diagnosis."""
    now = _ist(2026, 6, 22, 11, 0)
    providers, notified, _h = _stub(
        latest_inhouse=now - timedelta(minutes=35), chartink_alive=False
    )
    res = svc.check_dry_scanner(as_of=now, **providers)
    assert res["status"] == "alerted_warn"
    assert res["severity"] == "WARN"
    assert len(notified) == 1
    severity, msg = notified[0]
    assert severity == "WARN"
    assert "⚠️" in msg
    assert "SCANNER WARN" in msg
    assert "Chartink is also dry" in msg


# --------------------------------------------------------------------------- #
# Dedup
# --------------------------------------------------------------------------- #


def test_crit_dedup_within_same_day():
    """Two CRIT-triggering checks on the same day → one alert, second is
    dedup_silent. Health rows still written each time (heartbeat trail)."""
    now = _ist(2026, 6, 22, 11, 0)
    providers, notified, health = _stub(
        latest_inhouse=now - timedelta(minutes=35), chartink_alive=True
    )
    res1 = svc.check_dry_scanner(as_of=now, **providers)
    res2 = svc.check_dry_scanner(as_of=now + timedelta(minutes=5), **providers)
    assert res1["status"] == "alerted_crit"
    assert res2["status"] == "dedup_silent"
    assert len(notified) == 1
    assert len(health) == 2
    assert health[0]["alert_sent"] is True
    assert health[1]["alert_sent"] is False


def test_crit_and_warn_are_separate_dedup_states():
    """A CRIT alert at 10:00 doesn't suppress a later WARN at 13:00 — they
    use independent dedup keys so a regime change (Chartink goes dry mid-day)
    is still surfaced once."""
    crit_at = _ist(2026, 6, 22, 10, 0)
    warn_at = _ist(2026, 6, 22, 13, 0)

    crit_providers, notified, _h = _stub(
        latest_inhouse=crit_at - timedelta(minutes=35), chartink_alive=True
    )
    svc.check_dry_scanner(as_of=crit_at, **crit_providers)

    warn_providers, notified2, _h2 = _stub(
        latest_inhouse=warn_at - timedelta(minutes=35), chartink_alive=False
    )
    # Reuse the shared notifier from crit_providers so we collect ALL alerts.
    warn_providers["notifier"] = lambda m, s: notified.append((s, m))
    svc.check_dry_scanner(as_of=warn_at, **warn_providers)

    assert len(notified) == 2
    assert notified[0][0] == "CRIT"
    assert notified[1][0] == "WARN"


def test_dedup_resets_across_days():
    """A CRIT alert on day 1 doesn't suppress a CRIT alert on day 2."""
    day1 = _ist(2026, 6, 22, 11, 0)
    day2 = _ist(2026, 6, 23, 11, 0)
    providers1, notified, _h = _stub(
        latest_inhouse=day1 - timedelta(minutes=35), chartink_alive=True
    )
    svc.check_dry_scanner(as_of=day1, **providers1)
    providers2, notified2, _h2 = _stub(
        latest_inhouse=day2 - timedelta(minutes=35), chartink_alive=True
    )
    # Shared notifier
    providers2["notifier"] = lambda m, s: notified.append((s, m))
    svc.check_dry_scanner(as_of=day2, **providers2)
    assert len(notified) == 2
    assert notified[0][0] == "CRIT"
    assert notified[1][0] == "CRIT"


def test_chartink_probe_failing_defaults_to_warn():
    """If chartink_has_rows_since raises, we don't escalate to CRIT on
    a telemetry hiccup — we default to WARN."""
    now = _ist(2026, 6, 22, 11, 0)

    def explode(_cutoff):
        raise RuntimeError("scan_cycle DB connection lost")

    providers, notified, _h = _stub(latest_inhouse=now - timedelta(minutes=35))
    providers["chartink_has_rows_since"] = explode

    res = svc.check_dry_scanner(as_of=now, **providers)
    assert res["status"] == "alerted_warn"
    assert res["severity"] == "WARN"
