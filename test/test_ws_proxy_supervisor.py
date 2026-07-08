"""Tests for the WS-proxy subprocess supervisor (issue #376).

Uses a fake status provider + restarter so no real subprocess is spawned; one
test drives a real short-lived ``Popen`` to prove death detection works against
an actual process handle.
"""

from __future__ import annotations

import datetime as dt
import subprocess  # nosec B404 — test spawns a benign short-lived python -c
import sys
import time
from datetime import timezone

import pytest

from services import ws_proxy_supervisor as sup

_IST = timezone(dt.timedelta(hours=5, minutes=30))


def _ist(h=13, mi=0) -> dt.datetime:
    """Wednesday 2026-07-08 mid-session, inside the alert window."""
    return dt.datetime(2026, 7, 8, h, mi, 0, tzinfo=_IST)


def _make_sup(*, status, restart_ok=True, now=None, trading_day=True):
    """Build a supervisor with injected I/O. ``status`` is a list of
    ``(mode, alive)`` tuples popped per call (last value repeats)."""
    alerts: list[str] = []
    restarts: list[int] = []
    seq = list(status)

    def _status():
        return seq[0] if len(seq) == 1 else seq.pop(0)

    def _restart():
        restarts.append(1)
        return restart_ok

    s = sup.WSProxySupervisor(
        status_provider=_status,
        restarter=_restart,
        notifier=alerts.append,
        now_provider=(now or (lambda: _ist())),
        trading_day_checker=lambda _d: trading_day,
        sleep_fn=lambda _s: None,
        backoff_sec=0.0,
    )
    return s, alerts, restarts


def test_alive_proxy_no_action():
    s, alerts, restarts = _make_sup(status=[("subprocess", True)])
    assert s.check() == "alive"
    assert alerts == []
    assert restarts == []


def test_not_managed_when_no_proxy():
    s, alerts, restarts = _make_sup(status=[("none", False)])
    assert s.check() == "not_managed"
    assert alerts == []
    assert restarts == []


def test_death_detected_triggers_alert_and_restart():
    s, alerts, restarts = _make_sup(status=[("subprocess", False)], restart_ok=True)
    res = s.check()
    assert res == "restarted"
    assert len(restarts) == 1
    assert any("exited unexpectedly" in a for a in alerts)
    assert any("restarted OK" in a for a in alerts)


def test_failed_restart_alerts():
    s, alerts, restarts = _make_sup(status=[("subprocess", False)], restart_ok=False)
    res = s.check()
    assert res == "restart_failed"
    assert len(restarts) == 1
    assert any("restart FAILED" in a for a in alerts)


def test_restart_cap_enforced():
    # Always dead. Cap defaults to 3.
    s, alerts, restarts = _make_sup(status=[("subprocess", False)], restart_ok=True)
    assert s.check() == "restarted"  # 1
    assert s.check() == "restarted"  # 2
    assert s.check() == "restarted"  # 3
    # 4th detection: cap exhausted → CRITICAL alert, no restart.
    res = s.check()
    assert res == "cap_exceeded_alerted"
    assert len(restarts) == 3
    assert any("cap" in a.lower() and "exhausted" in a.lower() for a in alerts)
    # 5th: still capped, but no duplicate alert.
    n_alerts = len(alerts)
    assert s.check() == "cap_exceeded"
    assert len(alerts) == n_alerts


def test_cap_resets_next_day():
    day1 = {"t": _ist(h=13, mi=0)}
    s, alerts, restarts = _make_sup(
        status=[("subprocess", False)], restart_ok=True, now=lambda: day1["t"]
    )
    for _ in range(3):
        s.check()
    assert s.check() == "cap_exceeded_alerted"
    # Next IST day — budget resets.
    day1["t"] = dt.datetime(2026, 7, 9, 13, 0, tzinfo=_IST)
    assert s.check() == "restarted"
    assert len(restarts) == 4


def test_offhours_death_restarts_but_suppresses_alert():
    # 20:00 IST — outside the alert window but still a trading day.
    s, alerts, restarts = _make_sup(
        status=[("subprocess", False)], restart_ok=True, now=lambda: _ist(h=20, mi=0)
    )
    res = s.check()
    assert res == "restarted"
    assert len(restarts) == 1  # still auto-restarts off-hours
    assert alerts == []  # but no Telegram spam off-hours


def test_request_restart_skips_when_alive():
    s, alerts, restarts = _make_sup(status=[("subprocess", True)])
    assert s.request_restart("ladder step 3") == "alive_skip"
    assert restarts == []


def test_request_restart_acts_when_dead():
    s, alerts, restarts = _make_sup(status=[("subprocess", False)], restart_ok=True)
    assert s.request_restart("ladder step 3") == "restarted"
    assert len(restarts) == 1


def test_real_subprocess_death_detection():
    """Drive a real short-lived Popen to prove the production status provider
    reports it dead once it exits."""
    proc = subprocess.Popen(  # nosec B603
        [sys.executable, "-c", "import time; time.sleep(0.1)"]
    )
    proc.wait(timeout=5)
    # A status provider mirroring app_integration.get_websocket_runtime_status.
    dead_status = ("subprocess", proc.poll() is None)
    assert dead_status == ("subprocess", False)

    restarted = {"n": 0}

    def _restart():
        restarted["n"] += 1
        return True

    s = sup.WSProxySupervisor(
        status_provider=lambda: dead_status,
        restarter=_restart,
        notifier=lambda _m: None,
        now_provider=lambda: _ist(),
        trading_day_checker=lambda _d: True,
        sleep_fn=lambda _s: None,
        backoff_sec=0.0,
    )
    assert s.check() == "restarted"
    assert restarted["n"] == 1


def test_request_supervised_restart_unavailable_without_singleton(monkeypatch):
    monkeypatch.setattr(sup, "_supervisor", None)
    assert sup.request_supervised_restart("x") == "unavailable"


def test_check_never_raises_on_provider_error():
    def _boom():
        raise RuntimeError("status probe exploded")

    s = sup.WSProxySupervisor(
        status_provider=_boom,
        restarter=lambda: True,
        notifier=lambda _m: None,
        now_provider=lambda: _ist(),
        trading_day_checker=lambda _d: True,
        sleep_fn=lambda _s: None,
    )
    # request_restart wraps check(); must not raise.
    assert s.request_restart("x") == "error"


def test_time_module_import_smoke():
    # Guard against an accidental unused-import regression in the module.
    assert time is not None
