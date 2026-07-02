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
    """Clear per-process dedup AND the subscribe-baseline marker so each test
    starts from a known baseline (issue #146)."""
    svc._reset_subscribe_state_for_tests()
    yield
    svc._reset_subscribe_state_for_tests()


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


# --------------------------------------------------------------------------- #
# APScheduler job registration
# --------------------------------------------------------------------------- #


def test_tripwire_job_registers_with_scheduler():
    """Verify that init_scanner_dry_tripwire registers the periodic job
    with APScheduler. This is the regression test for the B4 fix —
    the tripwire job has never actually registered since shipping."""
    from unittest.mock import MagicMock

    # Create a mock scheduler (a duck-typed mock of HistorifyScheduler)
    mock_scheduler = MagicMock()
    mock_scheduler.add_job = MagicMock(return_value=None)

    # Call init with the mock scheduler
    svc.init_scanner_dry_tripwire(app=None, scheduler=mock_scheduler)

    # Verify add_job was called exactly once with correct args
    assert mock_scheduler.add_job.call_count == 1
    call_args = mock_scheduler.add_job.call_args

    # Verify the job function is the tripwire job
    assert call_args[0][0] == svc._tripwire_job

    # Verify the job ID and other key params
    assert call_args[1]["id"] == "scanner_dry_tripwire"
    assert call_args[1]["replace_existing"] is True
    assert "trigger" in call_args[1]


# --------------------------------------------------------------------------- #
# Subscribe-aware baseline (issue #146)
# --------------------------------------------------------------------------- #


def test_fresh_subscribe_with_stale_yesterday_row_does_not_fire(monkeypatch):
    """The scenario the 2026-06-26 09:35 IST CRIT exposed: app restarted at
    09:11, scanner subscribed at 09:12, last_inhouse_at points at yesterday
    00:25. At 09:35 the tripwire must NOT fire — the scanner has only been
    subscribed for ~23 min, less than the 5-min warmup + 30-min threshold."""
    monkeypatch.setenv("SCANNER_DRY_SUBSCRIBE_WARMUP_MIN", "5")
    now = _ist(2026, 6, 26, 9, 35)
    yesterday = _ist(2026, 6, 25, 0, 25)
    svc.mark_scanner_subscribed(_ist(2026, 6, 26, 9, 12))

    providers, notified, _h = _stub(latest_inhouse=yesterday, chartink_alive=True)
    res = svc.check_dry_scanner(as_of=now, **providers)

    assert res["status"] == "ok"
    assert notified == []


def test_subscribed_long_enough_with_stale_row_does_fire(monkeypatch):
    """The honest CRIT path: scanner subscribed an hour ago and still no row.
    Stale yesterday row is overridden by the subscribe baseline (12:00 + 5min
    warmup = 12:05). At 13:00 the gap is 55 min, exceeds the 30-min threshold,
    Chartink has rows → CRIT fires."""
    monkeypatch.setenv("SCANNER_DRY_SUBSCRIBE_WARMUP_MIN", "5")
    now = _ist(2026, 6, 26, 13, 0)
    yesterday = _ist(2026, 6, 25, 0, 25)
    svc.mark_scanner_subscribed(_ist(2026, 6, 26, 12, 0))

    providers, notified, _h = _stub(latest_inhouse=yesterday, chartink_alive=True)
    res = svc.check_dry_scanner(as_of=now, **providers)

    assert res["status"] == "alerted_crit"
    assert res["severity"] == "CRIT"
    assert len(notified) == 1


def test_subscribe_floor_does_not_mask_a_recent_row(monkeypatch):
    """If the scanner produced a row 2 min ago (more recent than the
    subscribe baseline), the row wins. Healthy state — no alert."""
    monkeypatch.setenv("SCANNER_DRY_SUBSCRIBE_WARMUP_MIN", "5")
    now = _ist(2026, 6, 26, 14, 0)
    svc.mark_scanner_subscribed(_ist(2026, 6, 26, 9, 12))
    recent = now - timedelta(minutes=2)

    providers, notified, _h = _stub(latest_inhouse=recent, chartink_alive=True)
    res = svc.check_dry_scanner(as_of=now, **providers)

    assert res["status"] == "ok"
    assert notified == []


def test_mid_day_resubscribe_resets_warmup(monkeypatch):
    """A mid-day Zerodha re-login re-fires the connect callback. The tripwire
    must grant a new warmup window (otherwise it fires CRIT right after a
    routine token refresh). 12:00 re-login + 5-min warmup = 12:05 floor; at
    12:30 the gap is 25 min, under threshold → OK."""
    monkeypatch.setenv("SCANNER_DRY_SUBSCRIBE_WARMUP_MIN", "5")
    # Earlier subscribe — overwritten by the re-subscribe at 12:00.
    svc.mark_scanner_subscribed(_ist(2026, 6, 26, 9, 12))
    # Mid-day re-login at 12:00.
    svc.mark_scanner_subscribed(_ist(2026, 6, 26, 12, 0))

    now = _ist(2026, 6, 26, 12, 30)
    yesterday = _ist(2026, 6, 25, 0, 25)

    providers, notified, _h = _stub(latest_inhouse=yesterday, chartink_alive=True)
    res = svc.check_dry_scanner(as_of=now, **providers)

    assert res["status"] == "ok"
    assert notified == []


def test_no_subscribe_signal_falls_back_to_warmup_end_cutoff(monkeypatch):
    """When the scanner has never reported a subscribe (e.g. very early in
    boot, or scanner disabled) AND there's no row yet, the tripwire falls
    back to the legacy 09:30 IST cutoff so its alerting behaviour is
    well-defined even without the subscribe hook firing."""
    monkeypatch.setenv("SCANNER_DRY_SUBSCRIBE_WARMUP_MIN", "5")
    now = _ist(2026, 6, 26, 11, 0)  # 90 min after warmup cutoff
    # No mark_scanner_subscribed call — _scanner_subscribed_at is None.

    providers, notified, _h = _stub(latest_inhouse=None, chartink_alive=True)
    res = svc.check_dry_scanner(as_of=now, **providers)

    # Gap = 90 min from 09:30 cutoff, exceeds 30-min threshold, Chartink alive → CRIT.
    assert res["status"] == "alerted_crit"
    assert res["severity"] == "CRIT"


def test_details_include_subscribed_at_and_warmup(monkeypatch):
    """The structured log payload exposes the subscribe context so an operator
    can see why a particular check did or didn't fire."""
    monkeypatch.setenv("SCANNER_DRY_SUBSCRIBE_WARMUP_MIN", "5")
    subscribe_at = _ist(2026, 6, 26, 9, 12)
    svc.mark_scanner_subscribed(subscribe_at)
    now = _ist(2026, 6, 26, 9, 35)

    providers, _n, health = _stub(latest_inhouse=_ist(2026, 6, 25, 0, 25), chartink_alive=True)
    svc.check_dry_scanner(as_of=now, **providers)

    assert health, "health row should be written for a heartbeat OK"
    details = health[-1]["details"]
    assert details["scanner_subscribed_at"] == subscribe_at.isoformat()
    assert details["subscribe_warmup_min"] == 5


def test_subscribed_at_provider_exception_falls_back_safely(monkeypatch):
    """If the subscribed_at_provider raises (shouldn't happen, but guard
    against module-state corruption), the check still completes and treats
    subscribe state as unknown (subscribed_at=None).

    With issue #239: subscribed_at=None AND gap_min=90 > 60 → the WS-absence
    escalation fires CRIT (the provider exception is treated as "WS never came
    up"). This is the safer behaviour — unknown subscribe state is treated as
    absent rather than silently defaulting to WARN.
    """
    monkeypatch.setenv("SCANNER_DRY_SUBSCRIBE_WARMUP_MIN", "5")
    now = _ist(2026, 6, 26, 11, 0)

    providers, _n, health = _stub(latest_inhouse=None, chartink_alive=False)

    def explode():
        raise RuntimeError("module state corrupted")

    res = svc.check_dry_scanner(as_of=now, subscribed_at_provider=explode, **providers)
    # subscribed_at=None AND gap_min=90 > 60 → WS-absence escalation → CRIT
    # (issue #239: unknown subscribe state escalates, not silently WARNs).
    assert res["status"] == "alerted_crit"
    assert res["severity"] == "CRIT"
    assert health[-1]["details"].get("escalation_reason") == "ws_subscription_absent"


# --------------------------------------------------------------------------- #
# WS-absence CRITICAL escalation (issue #239)
# --------------------------------------------------------------------------- #


def test_ws_absent_and_large_gap_escalates_to_crit():
    """``scanner_subscribed_at=None`` AND ``gap_min > 60`` must escalate to
    CRITICAL without querying Chartink.  This is the exact 2026-06-30 failure
    fingerprint: ``gap_min=7745, scanner_subscribed_at=None, severity=WARN``
    — the WARN meant no page fired despite a 5-day drought.  After this fix
    the same payload yields CRIT."""
    # Do NOT call mark_scanner_subscribed — _scanner_subscribed_at stays None.
    now = _ist(2026, 6, 30, 9, 30)
    # last_inhouse_at 5 days ago → gap = 7200+ min >> 60
    last = _ist(2026, 6, 25, 0, 25)

    chartink_called = []

    providers, notified, health = _stub(
        latest_inhouse=last,
        chartink_alive=False,  # not queried for this path
    )
    providers["chartink_has_rows_since"] = lambda _c: (chartink_called.append(True), False)[1]
    # subscribed_at=None (module state reset by autouse fixture)
    providers["subscribed_at_provider"] = lambda: None

    res = svc.check_dry_scanner(as_of=now, **providers)

    assert res["severity"] == "CRIT", f"Expected CRIT, got {res}"
    assert res["status"] == "alerted_crit"
    # Chartink must NOT be queried — the subscription absence is the signal
    assert not chartink_called, "chartink_has_rows_since must not be called for ws_absent path"
    # Telegram alert fired
    assert len(notified) == 1
    sev, msg = notified[0]
    assert sev == "CRIT"
    assert "WS subscription" in msg or "ws_subscription_absent" in msg or "tick feed" in msg
    # Payload carries escalation reason
    health_row = health[-1]
    assert health_row["details"].get("escalation_reason") == "ws_subscription_absent"


def test_ws_absent_but_small_gap_does_not_escalate():
    """``scanner_subscribed_at=None`` with ``gap_min <= 60`` should NOT trigger
    the escalation path — a short gap may be normal warmup."""
    # Do NOT call mark_scanner_subscribed.
    now = _ist(2026, 6, 30, 10, 0)
    # last_inhouse_at 40 minutes ago — gap = 40 min, below the 60-min escalation threshold
    last = now - timedelta(minutes=40)

    providers, notified, health = _stub(
        latest_inhouse=last,
        chartink_alive=True,  # would be CRIT via normal path
    )
    providers["subscribed_at_provider"] = lambda: None

    res = svc.check_dry_scanner(as_of=now, **providers)

    # gap_min=40, threshold=30 → fires, but no ws_absent escalation (gap ≤ 60)
    # chartink_alive=True → normal CRIT path
    assert res["severity"] == "CRIT"
    # Normal diagnosis message (not the WS-absence one)
    sev, msg = notified[0]
    assert "Chartink HAS recent hits" in msg
    # escalation_reason should NOT be set
    assert health[-1]["details"].get("escalation_reason") is None


def test_ws_absent_escalation_deduped_on_same_day():
    """Second call on the same day with ws_absent escalation → dedup_silent."""
    now = _ist(2026, 6, 30, 10, 30)
    last = _ist(2026, 6, 25, 0, 25)

    providers, notified, _h = _stub(latest_inhouse=last)
    providers["subscribed_at_provider"] = lambda: None

    # First call: alerts
    res1 = svc.check_dry_scanner(as_of=now, **providers)
    assert res1["status"] == "alerted_crit"
    assert len(notified) == 1

    # Second call same day: dedup_silent (no second alert)
    res2 = svc.check_dry_scanner(as_of=now + timedelta(minutes=5), **providers)
    assert res2["status"] == "dedup_silent"
    assert res2["severity"] == "CRIT"
    assert len(notified) == 1  # no new notification


def test_format_alert_ws_absence_diagnosis():
    """``_format_alert`` with ``escalation_reason='ws_subscription_absent'``
    produces a human-readable WS-absence message (not the chartink copy)."""
    msg = svc._format_alert(
        "CRIT",
        7745.0,
        _ist(2026, 6, 25, 0, 25),
        False,
        escalation_reason="ws_subscription_absent",
    )
    assert "WS subscription" in msg or "tick feed" in msg
    assert "🚨" in msg
    assert "Chartink" not in msg  # must NOT use the chartink copy for this path


def test_format_alert_normal_crit_path():
    """``_format_alert`` with no escalation_reason and chartink_alive=True
    uses the standard 'Chartink HAS recent hits' diagnosis."""
    msg = svc._format_alert(
        "CRIT",
        35.0,
        _ist(2026, 6, 22, 10, 25),
        True,
        escalation_reason=None,
    )
    assert "Chartink HAS recent hits" in msg
    assert "🚨" in msg


def test_ws_present_but_subscribed_chartink_alive_is_still_crit():
    """Control: when ``subscribed_at`` IS set (WS came up), normal CRIT
    logic applies even if the gap is huge."""
    subscribe_at = _ist(2026, 6, 30, 9, 11)
    svc.mark_scanner_subscribed(subscribe_at)

    now = _ist(2026, 6, 30, 11, 0)
    # last_inhouse_at 61 minutes ago — well above 30-min threshold
    last = now - timedelta(minutes=61)

    providers, notified, health = _stub(latest_inhouse=last, chartink_alive=True)
    # subscribed_at is set — escalation path must NOT fire
    res = svc.check_dry_scanner(as_of=now, **providers)

    assert res["severity"] == "CRIT"
    sev, msg = notified[0]
    assert "Chartink HAS recent hits" in msg  # normal diagnosis
    # escalation_reason should NOT be set
    assert health[-1]["details"].get("escalation_reason") is None
