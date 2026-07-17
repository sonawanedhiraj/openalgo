"""Tests for the tick-liveness watchdog + auto-heal ladder + instrumentation
(issue #376).

Frozen-clock unit tests: the watchdog's only time source is an injected
``now_provider`` returning an aware IST datetime, and the last-bar wall-clock is
injected too, so silence duration is fully deterministic.
"""

from __future__ import annotations

import datetime as dt
from datetime import timezone

import pytest

from services import tick_liveness_watchdog as tlw

_IST = timezone(dt.timedelta(hours=5, minutes=30))


def _ist(y=2026, mo=7, d=8, h=13, mi=0, s=0) -> dt.datetime:
    """A Wednesday (2026-07-08) mid-session IST datetime by default."""
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=_IST)


def _wall(when: dt.datetime) -> float:
    return when.timestamp()


@pytest.fixture(autouse=True)
def _enable_scanner_and_flags(monkeypatch):
    """Watchdog + scanner both enabled by default; tests flip as needed."""
    monkeypatch.setenv("SCANNER_ENABLED", "true")
    monkeypatch.setenv("TICK_LIVENESS_WATCHDOG_ENABLED", "true")
    monkeypatch.setenv("TICK_LIVENESS_AUTOHEAL_ENABLED", "true")
    monkeypatch.setenv("SCANNER_LIVENESS_MAX_SILENT_MIN", "10")
    monkeypatch.setenv("SCANNER_LIVENESS_REALERT_MIN", "30")
    monkeypatch.setenv("SCANNER_LIVENESS_LADDER_COOLDOWN_MIN", "30")


def _make_wd(
    *,
    now: dt.datetime,
    last_bar: float | None,
    trading_day: bool = True,
    steps=None,
    started_wall: float | None = None,
):
    alerts: list[str] = []
    wd = tlw.TickLivenessWatchdog(
        last_bar_provider=lambda: last_bar,
        notifier=alerts.append,
        now_provider=lambda: now,
        trading_day_checker=lambda _d: trading_day,
        heal_steps=steps if steps is not None else [],
        # Anchor the floor well before the scenario so silence is measured from
        # the last bar / session open, not from watchdog construction.
        started_wall=started_wall if started_wall is not None else _wall(_ist(h=9, mi=15)),
    )
    return wd, alerts


# --------------------------------------------------------------------------- #
# Core detection
# --------------------------------------------------------------------------- #


def test_silence_beyond_threshold_alerts_during_market_hours(monkeypatch):
    # Alert-only (autoheal off) so the CRIT alert count is not polluted by the
    # ladder's step/terminal alerts.
    monkeypatch.setenv("TICK_LIVENESS_AUTOHEAL_ENABLED", "false")
    now = _ist(h=13, mi=0)
    last = _wall(_ist(h=12, mi=40))  # 20 min ago > 10 min threshold
    wd, alerts = _make_wd(now=now, last_bar=last)
    res = wd.check()
    assert res["status"] == "alerted"
    assert res["silent_min"] >= 20 - 0.01
    assert len(alerts) == 1
    assert "TICK LIVENESS CRIT" in alerts[0]


def test_fresh_bars_no_alert():
    now = _ist(h=13, mi=0)
    last = _wall(_ist(h=12, mi=57))  # 3 min ago < 10 min threshold
    wd, alerts = _make_wd(now=now, last_bar=last)
    res = wd.check()
    assert res["status"] == "ok"
    assert alerts == []


def test_off_hours_never_alerts_even_when_silent():
    now = _ist(h=16, mi=30)  # after 15:30 close
    last = _wall(_ist(h=13, mi=0))
    wd, alerts = _make_wd(now=now, last_bar=last)
    assert wd.check()["status"] == "off_hours"
    assert alerts == []


def test_before_grace_window_never_alerts():
    now = _ist(h=9, mi=20)  # before 09:25 grace end
    last = _wall(_ist(h=9, mi=15))
    wd, alerts = _make_wd(now=now, last_bar=last, started_wall=_wall(_ist(h=9, mi=15)))
    assert wd.check()["status"] == "off_hours"
    assert alerts == []


def test_holiday_never_alerts():
    now = _ist(h=13, mi=0)
    last = _wall(_ist(h=12, mi=0))
    wd, alerts = _make_wd(now=now, last_bar=last, trading_day=False)
    assert wd.check()["status"] == "off_hours"
    assert alerts == []


def test_weekend_via_trading_day_checker(monkeypatch):
    # Saturday 2026-07-11; production checker would return False.
    now = dt.datetime(2026, 7, 11, 13, 0, tzinfo=_IST)
    last = _wall(dt.datetime(2026, 7, 11, 12, 0, tzinfo=_IST))
    wd, alerts = _make_wd(now=now, last_bar=last, trading_day=False, started_wall=last - 3600)
    assert wd.check()["status"] == "off_hours"
    assert alerts == []


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.setenv("TICK_LIVENESS_WATCHDOG_ENABLED", "false")
    now = _ist(h=13, mi=0)
    last = _wall(_ist(h=12, mi=0))
    wd, alerts = _make_wd(now=now, last_bar=last)
    assert wd.check()["status"] == "flag_off"
    assert alerts == []


def test_scanner_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("SCANNER_ENABLED", "false")
    now = _ist(h=13, mi=0)
    last = _wall(_ist(h=12, mi=0))
    wd, alerts = _make_wd(now=now, last_bar=last)
    assert wd.check()["status"] == "scanner_disabled"
    assert alerts == []


def test_no_bar_since_boot_uses_floor():
    # last_bar None; floor = max(session open, started_wall). Silence measured
    # from the floor. started_wall is 09:15; at 13:00 that's ~225 min > 10.
    now = _ist(h=13, mi=0)
    wd, alerts = _make_wd(now=now, last_bar=None, started_wall=_wall(_ist(h=9, mi=15)))
    res = wd.check()
    assert res["status"] == "alerted"
    assert "none since boot" in alerts[0]


def test_started_wall_floor_prevents_stale_yesterday_alert():
    # Boot at 12:55 today; last bar was "yesterday" (a very old wall time).
    # Floor is the 12:55 start, so at 13:00 only 5 min of silence → OK.
    now = _ist(h=13, mi=0)
    yesterday = _wall(_ist(d=7, h=15, mi=0))
    wd, alerts = _make_wd(now=now, last_bar=yesterday, started_wall=_wall(_ist(h=12, mi=55)))
    assert wd.check()["status"] == "ok"
    assert alerts == []


# --------------------------------------------------------------------------- #
# Re-alert throttling + recovery
# --------------------------------------------------------------------------- #


def test_realert_throttled_then_fires_after_interval(monkeypatch):
    monkeypatch.setenv("TICK_LIVENESS_AUTOHEAL_ENABLED", "false")
    last = _wall(_ist(h=12, mi=40))
    # First check at 13:00 → alert.
    wd, alerts = _make_wd(now=_ist(h=13, mi=0), last_bar=last)
    assert wd.check()["status"] == "alerted"
    assert len(alerts) == 1

    # 15 min later, still silent, within 30-min re-alert window → no new alert.
    wd._now = lambda: _ist(h=13, mi=15)
    res = wd.check()
    assert res["status"] == "silent"
    assert len(alerts) == 1

    # 31 min after the first alert → re-alert fires.
    wd._now = lambda: _ist(h=13, mi=31)
    res = wd.check()
    assert res["status"] == "realerted"
    assert len(alerts) == 2
    assert "still down" in alerts[1]


def test_recovery_line_emitted_when_bars_resume():
    last = _wall(_ist(h=12, mi=40))
    wd, alerts = _make_wd(now=_ist(h=13, mi=0), last_bar=last)
    assert wd.check()["status"] == "alerted"

    # Bars resume: last bar now recent, at a later now.
    wd._last_bar = lambda: _wall(_ist(h=13, mi=9))
    wd._now = lambda: _ist(h=13, mi=10)
    res = wd.check()
    assert res["status"] == "recovered"
    assert any("RECOVERED" in a for a in alerts)
    # Outage state reset — a subsequent healthy check is plain ok.
    assert wd.check()["status"] == "ok"


def test_window_close_during_outage_resets_state():
    last = _wall(_ist(h=15, mi=0))
    wd, alerts = _make_wd(now=_ist(h=15, mi=25), last_bar=last)
    assert wd.check()["status"] == "alerted"
    # Roll past 15:30 close.
    wd._now = lambda: _ist(h=15, mi=40)
    assert wd.check()["status"] == "off_hours"
    assert wd._outage_since_wall is None


# --------------------------------------------------------------------------- #
# Auto-heal ladder
# --------------------------------------------------------------------------- #


def _counting_step(name, log, ret=True):
    def _fn():
        log.append(name)
        return ret

    return (name, _fn)


def test_ladder_advances_one_step_per_check():
    log: list[str] = []
    steps = [
        _counting_step("resub", log),
        _counting_step("reconnect", log),
        _counting_step("restart", log),
    ]
    last = _wall(_ist(h=12, mi=40))
    wd, alerts = _make_wd(now=_ist(h=13, mi=0), last_bar=last, steps=steps)

    # Step 1 fires on the first silent check.
    wd.check()
    assert log == ["resub"]

    # Within the step-wait window (120s) → no escalation.
    wd._now = lambda: _ist(h=13, mi=1)
    wd.check()
    assert log == ["resub"]

    # After the step-wait → step 2.
    wd._now = lambda: _ist(h=13, mi=3)
    wd.check()
    assert log == ["resub", "reconnect"]

    # After another wait → step 3.
    wd._now = lambda: _ist(h=13, mi=6)
    wd.check()
    assert log == ["resub", "reconnect", "restart"]


def test_ladder_terminal_alert_after_all_steps_exhausted():
    log: list[str] = []
    steps = [_counting_step("only", log)]
    last = _wall(_ist(h=12, mi=40))
    wd, alerts = _make_wd(now=_ist(h=13, mi=0), last_bar=last, steps=steps)

    wd.check()  # step 1 (only step)
    wd._now = lambda: _ist(h=13, mi=3)
    res = wd.check()  # exhausted → terminal alert
    assert res["ladder"] == "exhausted_alerted"
    assert any("EXHAUSTED" in a for a in alerts)


def test_ladder_disabled_when_autoheal_off(monkeypatch):
    monkeypatch.setenv("TICK_LIVENESS_AUTOHEAL_ENABLED", "false")
    log: list[str] = []
    steps = [_counting_step("resub", log)]
    last = _wall(_ist(h=12, mi=40))
    wd, alerts = _make_wd(now=_ist(h=13, mi=0), last_bar=last, steps=steps)
    res = wd.check()
    assert res["status"] == "alerted"
    assert res["ladder"] == "autoheal_off"
    assert log == []  # no heal step ran


def test_ladder_raising_step_escalates_gracefully():
    log: list[str] = []

    def _boom():
        log.append("boom")
        raise RuntimeError("step blew up")

    steps = [("boom", _boom), _counting_step("next", log)]
    last = _wall(_ist(h=12, mi=40))
    wd, alerts = _make_wd(now=_ist(h=13, mi=0), last_bar=last, steps=steps)
    wd.check()  # step 1 raises — swallowed
    assert log == ["boom"]
    wd._now = lambda: _ist(h=13, mi=3)
    wd.check()  # ladder still advances to step 2
    assert log == ["boom", "next"]


# --------------------------------------------------------------------------- #
# Instrumentation
# --------------------------------------------------------------------------- #


def test_collect_resource_snapshot_never_raises():
    snap = tlw.collect_resource_snapshot()
    assert isinstance(snap, dict)


def test_log_resource_snapshot_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(tlw, "collect_resource_snapshot", _boom)
    # Must not propagate.
    tlw.log_resource_snapshot()


def test_snapshot_ctypes_fallback_when_psutil_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    snap = tlw.collect_resource_snapshot()
    assert isinstance(snap, dict)  # either ctypes handles key or empty; never raises
