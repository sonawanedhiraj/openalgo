"""Tests for services/thread_watchdog_service.ThreadWatchdog.

All tests operate on a freshly constructed ThreadWatchdog instance with
injected alert_writer / notifier / resolver callbacks — no database, no
Telegram, no daemon thread is started.

Scenarios covered:
  1. Below threshold       → no alert
  2. Crosses threshold     → exactly one alert
  3. Stays above threshold → no duplicate within the dedup window
  4. Returns below then crosses again → new alert fires, resolver called
  5. WARNING ↔ CRITICAL transitions → each level change fires a new alert
"""

from __future__ import annotations

import pytest

from services.thread_watchdog_service import ThreadWatchdog

WARN = 100
CRIT = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_watchdog(
    dedup_window_min: int = 15,
    warn: int = WARN,
    crit: int = CRIT,
    time_fn=None,
) -> tuple[ThreadWatchdog, list, list, list]:
    fired: list[tuple[int, str, float]] = []
    notified: list[tuple[int, str]] = []
    resolved: list[bool] = []

    def writer(count, level, threshold):
        fired.append((count, level, threshold))

    def notifier(count, level):
        notified.append((count, level))

    def resolver():
        resolved.append(True)

    wd = ThreadWatchdog(
        warn_threshold=warn,
        crit_threshold=crit,
        dedup_window_min=dedup_window_min,
        alert_writer=writer,
        notifier=notifier,
        resolver=resolver,
        _time_fn=time_fn,
    )
    return wd, fired, notified, resolved


# ---------------------------------------------------------------------------
# 1. Below threshold → no alert
# ---------------------------------------------------------------------------


class TestBelowThreshold:
    def test_no_alert_when_count_is_low(self):
        wd, fired, notified, resolved = _make_watchdog()
        result = wd.check(50)
        assert result is None
        assert fired == []
        assert notified == []
        assert resolved == []

    def test_no_alert_at_warn_minus_one(self):
        wd, fired, notified, _ = _make_watchdog()
        assert wd.check(WARN - 1) is None
        assert fired == []

    def test_repeated_below_no_spurious_alerts(self):
        wd, fired, notified, resolved = _make_watchdog()
        for count in (10, 50, 70, 99):
            assert wd.check(count) is None
        assert fired == []
        assert resolved == []


# ---------------------------------------------------------------------------
# 2. Crosses threshold → exactly one alert
# ---------------------------------------------------------------------------


class TestCrossesThreshold:
    def test_crossing_warn_threshold_fires_exactly_one_alert(self):
        wd, fired, notified, _ = _make_watchdog()
        result = wd.check(WARN)
        assert result == "warning"
        assert len(fired) == 1
        assert fired[0] == (WARN, "warning", float(WARN))
        assert len(notified) == 1
        assert notified[0] == (WARN, "warning")

    def test_crossing_crit_threshold_directly_fires_critical(self):
        wd, fired, notified, _ = _make_watchdog()
        result = wd.check(CRIT)
        assert result == "critical"
        assert len(fired) == 1
        assert fired[0][1] == "critical"
        assert fired[0][2] == float(CRIT)

    def test_count_at_exact_crit_boundary(self):
        wd, fired, _, _ = _make_watchdog()
        assert wd.check(CRIT) == "critical"
        assert fired[0][1] == "critical"


# ---------------------------------------------------------------------------
# 3. Stays above threshold → no duplicate within dedup window
# ---------------------------------------------------------------------------


class TestDedup:
    def test_no_duplicate_within_dedup_window(self):
        t = [0.0]
        wd, fired, notified, _ = _make_watchdog(dedup_window_min=15, time_fn=lambda: t[0])
        wd.check(120)  # t=0  → fires
        t[0] = 60.0  # 60s later (< 15min window)
        wd.check(130)  # same level → should NOT fire
        assert len(fired) == 1
        assert len(notified) == 1

    def test_sustained_alert_fires_again_after_dedup_window_expires(self):
        t = [0.0]
        wd, fired, notified, _ = _make_watchdog(dedup_window_min=1, time_fn=lambda: t[0])
        wd.check(120)  # t=0  → fires (#1)
        t[0] = 30.0  # 30s (< 60s dedup)
        wd.check(120)  # no fire
        t[0] = 70.0  # 70s (> 60s dedup)
        wd.check(120)  # fires again (#2)
        assert len(fired) == 2
        assert len(notified) == 2

    def test_boundary_just_before_dedup_window_no_fire(self):
        t = [0.0]
        wd, fired, _, _ = _make_watchdog(dedup_window_min=1, time_fn=lambda: t[0])
        wd.check(120)
        t[0] = 59.9  # just under 60s
        wd.check(120)
        assert len(fired) == 1


# ---------------------------------------------------------------------------
# 4. Returns below threshold then crosses again → new alert
# ---------------------------------------------------------------------------


class TestReturnAndRecross:
    def test_returns_below_then_crosses_again_fires_new_alert(self):
        wd, fired, notified, resolved = _make_watchdog()
        wd.check(120)  # first crossing → fires
        wd.check(50)  # below → resolves
        wd.check(110)  # second crossing → new alert
        assert len(fired) == 2
        assert len(notified) == 2
        assert len(resolved) == 1

    def test_resolve_callback_called_on_return_below(self):
        wd, fired, _, resolved = _make_watchdog()
        wd.check(150)
        wd.check(40)
        assert len(resolved) == 1

    def test_no_resolve_if_never_crossed(self):
        wd, _, _, resolved = _make_watchdog()
        wd.check(50)
        wd.check(30)
        assert resolved == []

    def test_state_fully_resets_between_episodes(self):
        wd, fired, notified, resolved = _make_watchdog()
        # Episode 1
        wd.check(120)
        wd.check(50)
        # Episode 2 — should behave like a fresh crossing
        wd.check(130)
        assert len(fired) == 2
        assert len(resolved) == 1
        assert fired[1][1] == "warning"


# ---------------------------------------------------------------------------
# 5. WARNING ↔ CRITICAL transitions
# ---------------------------------------------------------------------------


class TestSeverityTransitions:
    def test_warning_to_critical_fires_escalation_alert(self):
        wd, fired, notified, _ = _make_watchdog()
        wd.check(120)  # warning
        wd.check(250)  # critical → different level → fires
        assert len(fired) == 2
        assert fired[0][1] == "warning"
        assert fired[1][1] == "critical"
        assert len(notified) == 2

    def test_critical_to_warning_fires_deescalation_alert(self):
        wd, fired, notified, resolved = _make_watchdog()
        wd.check(250)  # critical
        wd.check(120)  # back to warning → fires
        assert len(fired) == 2
        assert fired[1][1] == "warning"
        assert resolved == []  # still above warn threshold, no resolve

    def test_critical_to_below_resolves_without_firing(self):
        wd, fired, notified, resolved = _make_watchdog()
        wd.check(250)  # critical → fires
        result = wd.check(50)  # below threshold
        assert result is None
        assert len(fired) == 1  # no new fire
        assert len(resolved) == 1

    def test_warning_to_critical_no_intermediate_dedup_interference(self):
        t = [0.0]
        wd, fired, _, _ = _make_watchdog(dedup_window_min=15, time_fn=lambda: t[0])
        wd.check(120)  # warning at t=0
        t[0] = 5.0  # 5s later (well inside dedup window)
        wd.check(250)  # critical → TRANSITION overrides dedup
        assert len(fired) == 2
        assert fired[1][1] == "critical"
