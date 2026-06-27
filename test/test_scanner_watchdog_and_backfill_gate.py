"""Tests for #158 D1 + D3 — scanner reliability follow-up bundle.

D1: scanner_ws_watchdog thresholds are env-overridable, defaults bumped
    180/360/120/60 (from 90/180/60/30) so normal mid-session quiet
    periods don't trigger ws.close → reconnect every 2 minutes.

D3: scanner_backfill_scheduler periodic tick gates on broker session so
    the 'no api key available' WARNING doesn't fire every interval
    during the morning re-login gap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

_IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# D1 — watchdog thresholds
# --------------------------------------------------------------------------- #


def test_watchdog_uses_bumped_defaults():
    """New defaults: soft=180s, hard=360s, cooldown=120s, interval=60s.

    The bumped values eliminate the ~121 false ws.close → reconnect events
    per trading day on a healthy feed (#158 D1 evidence).
    """
    from services.scanner_ws_watchdog import ScannerWsWatchdog

    wd = ScannerWsWatchdog(
        tick_source=lambda: None,
        recover_soft=lambda: None,
        recover_hard=lambda: None,
    )
    assert wd.soft_threshold == 180.0
    assert wd.hard_threshold == 360.0
    assert wd.cooldown == 120.0
    assert wd.interval == 60.0


def test_watchdog_thresholds_overridable_via_env(monkeypatch):
    """Each threshold is env-overridable so a noisy deploy can be tuned
    without a code change."""
    from services.scanner_ws_watchdog import ScannerWsWatchdog

    monkeypatch.setenv("SCANNER_WS_WATCHDOG_SOFT_THRESHOLD_SEC", "300")
    monkeypatch.setenv("SCANNER_WS_WATCHDOG_HARD_THRESHOLD_SEC", "600")
    monkeypatch.setenv("SCANNER_WS_WATCHDOG_COOLDOWN_SEC", "180")
    monkeypatch.setenv("SCANNER_WS_WATCHDOG_INTERVAL_SEC", "90")

    wd = ScannerWsWatchdog(
        tick_source=lambda: None,
        recover_soft=lambda: None,
        recover_hard=lambda: None,
    )
    assert wd.soft_threshold == 300.0
    assert wd.hard_threshold == 600.0
    assert wd.cooldown == 180.0
    assert wd.interval == 90.0


def test_watchdog_explicit_args_override_env(monkeypatch):
    """An explicit kwarg wins over the env var — tests can pin determinism."""
    from services.scanner_ws_watchdog import ScannerWsWatchdog

    monkeypatch.setenv("SCANNER_WS_WATCHDOG_SOFT_THRESHOLD_SEC", "300")

    wd = ScannerWsWatchdog(
        tick_source=lambda: None,
        recover_soft=lambda: None,
        recover_hard=lambda: None,
        soft_threshold=42.0,  # explicit override
    )
    assert wd.soft_threshold == 42.0


def test_watchdog_invalid_env_falls_back_to_default(monkeypatch):
    """A malformed env value falls back silently to the new default — never
    crashes the boot path."""
    from services.scanner_ws_watchdog import ScannerWsWatchdog

    monkeypatch.setenv("SCANNER_WS_WATCHDOG_SOFT_THRESHOLD_SEC", "not-a-number")
    wd = ScannerWsWatchdog(
        tick_source=lambda: None,
        recover_soft=lambda: None,
        recover_hard=lambda: None,
    )
    assert wd.soft_threshold == 180.0


def test_watchdog_check_returns_fresh_under_180s():
    """Sanity: a tick 120s old (within new soft window) is 'fresh'."""
    from services.scanner_ws_watchdog import ScannerWsWatchdog

    now = 1_000_000.0
    wd = ScannerWsWatchdog(
        tick_source=lambda: now - 120.0,  # 120s old
        recover_soft=lambda: None,
        recover_hard=lambda: None,
        now=lambda: now,
        market_open=lambda _t: True,
    )
    assert wd.check() == "fresh"


def test_watchdog_check_returns_soft_after_180s():
    """A tick 200s old (just over the new soft window) triggers soft."""
    from services.scanner_ws_watchdog import ScannerWsWatchdog

    now = 1_000_000.0
    recovered: list[bool] = []
    wd = ScannerWsWatchdog(
        tick_source=lambda: now - 200.0,  # 200s old > new 180s floor
        recover_soft=lambda: recovered.append(True),
        recover_hard=lambda: None,
        now=lambda: now,
        market_open=lambda _t: True,
    )
    assert wd.check() == "soft"
    assert recovered == [True]


# --------------------------------------------------------------------------- #
# D3 — backfill periodic tick broker-session gate
# --------------------------------------------------------------------------- #


def test_periodic_tick_skips_when_no_broker_session(monkeypatch):
    """No broker session → tick returns (False, None) without invoking
    run_backfill_checks. Eliminates the 'no api key available' WARNING flood
    during morning re-login."""
    monkeypatch.setenv("SCANNER_BACKFILL_GATE_ON_BROKER_SESSION", "true")
    from services import scanner_backfill_scheduler as sched

    # Trading day inside the window: Mon 2026-06-29 16:00 IST.
    now = datetime(2026, 6, 29, 16, 0, tzinfo=_IST)
    end_t = sched.time(17, 0)

    with (
        patch("services.broker_session_health.is_live_broker_session", return_value=False),
        patch.object(sched, "_is_trading_day", return_value=True),
        patch.object(sched, "_within_window", return_value=True),
        patch.object(sched, "run_backfill_checks") as run_fn,
    ):
        ran, res = sched._periodic_tick(now, end_t)

    assert ran is False
    assert res is None
    run_fn.assert_not_called()


def test_periodic_tick_runs_when_broker_session_live(monkeypatch):
    """Live broker session → normal evaluation proceeds."""
    monkeypatch.setenv("SCANNER_BACKFILL_GATE_ON_BROKER_SESSION", "true")
    from services import scanner_backfill_scheduler as sched

    now = datetime(2026, 6, 29, 16, 0, tzinfo=_IST)
    end_t = sched.time(17, 0)

    with (
        patch("services.broker_session_health.is_live_broker_session", return_value=True),
        patch.object(sched, "_is_trading_day", return_value=True),
        patch.object(sched, "_within_window", return_value=True),
        patch.object(sched, "run_backfill_checks", return_value={"all_fresh": True, "errors": []}),
        patch.object(sched, "_persist_health"),
        patch.object(sched, "_log_and_alert"),
    ):
        ran, res = sched._periodic_tick(now, end_t)

    assert ran is True
    assert res == {"all_fresh": True, "errors": []}


def test_periodic_tick_gate_can_be_disabled(monkeypatch):
    """Flag off (legacy behaviour) → tick runs even without broker session."""
    monkeypatch.setenv("SCANNER_BACKFILL_GATE_ON_BROKER_SESSION", "false")
    from services import scanner_backfill_scheduler as sched

    now = datetime(2026, 6, 29, 16, 0, tzinfo=_IST)
    end_t = sched.time(17, 0)

    with (
        patch.object(sched, "_is_trading_day", return_value=True),
        patch.object(sched, "_within_window", return_value=True),
        patch.object(
            sched, "run_backfill_checks", return_value={"all_fresh": True, "errors": []}
        ) as run_fn,
        patch.object(sched, "_persist_health"),
        patch.object(sched, "_log_and_alert"),
    ):
        ran, res = sched._periodic_tick(now, end_t)

    assert ran is True
    run_fn.assert_called_once()


def test_periodic_tick_outside_window_short_circuits_before_gate(monkeypatch):
    """Outside trading-day window → returns (False, None) without even
    checking broker session."""
    monkeypatch.setenv("SCANNER_BACKFILL_GATE_ON_BROKER_SESSION", "true")
    from services import scanner_backfill_scheduler as sched

    now = datetime(2026, 6, 29, 6, 0, tzinfo=_IST)  # before window
    end_t = sched.time(17, 0)

    with (
        patch("services.broker_session_health.is_live_broker_session") as session_check,
        patch.object(sched, "_is_trading_day", return_value=True),
        patch.object(sched, "_within_window", return_value=False),
    ):
        ran, res = sched._periodic_tick(now, end_t)

    assert ran is False
    session_check.assert_not_called()
