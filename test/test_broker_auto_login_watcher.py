"""Tests for the continuous broker auto-login watcher (issue #654).

``evaluate_once`` is pure: clock, probes and login callables are injected, so
these tests exercise the dead-probe confirmation, per-target daily attempt cap,
backoff, child handling and recovery reset without threads, sockets or the DB.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import services.broker_auto_login_watcher as w

_IST = timezone(w._IST.utcoffset(None))
NOW = datetime(2026, 8, 19, 10, 0, tzinfo=_IST)  # a Wednesday, inside the window


@pytest.fixture(autouse=True)
def _force_window(monkeypatch):
    monkeypatch.setattr(w, "_is_trading_day", lambda d: True)
    monkeypatch.setattr(w, "_within_window", lambda t: True)


def _ok(scope):
    return {"scope": scope, "ok": True, "message": "logged in"}


def _fail(scope):
    return {"scope": scope, "ok": False, "message": "boom"}


def _run(
    state,
    now,
    *,
    primary_alive,
    primary_login=None,
    children=None,
    child_alive=None,
    child_login=None,
    dead_confirm=2,
    max_attempts=5,
):
    return w.evaluate_once(
        state,
        now,
        probe_primary=lambda: primary_alive,
        login_primary=primary_login or (lambda: _ok("primary")),
        list_child_targets=lambda: children or [],
        probe_child=child_alive or (lambda aid: True),
        login_child=child_login or (lambda aid, name: _ok(f"child:{aid}")),
        dead_confirm=dead_confirm,
        max_attempts=max_attempts,
    )


def test_idle_when_primary_alive():
    state = w.WatcherState()
    assert _run(state, NOW, primary_alive=True) == []
    assert state.get(w.PRIMARY).dead_streak == 0


def test_dead_confirm_gate():
    """One dead probe does not trigger a login; the second (confirming) one does."""
    state = w.WatcherState()
    calls = []

    def login():
        calls.append(1)
        return _ok("primary")

    # tick 1: dead but unconfirmed
    assert _run(state, NOW, primary_alive=False, primary_login=login) == []
    assert calls == []
    assert state.get(w.PRIMARY).dead_streak == 1

    # tick 2: confirmed dead -> login fires
    results = _run(state, NOW, primary_alive=False, primary_login=login)
    assert calls == [1]
    assert results and results[0]["ok"]
    # a successful login resets the streak (next live probe confirms)
    assert state.get(w.PRIMARY).dead_streak == 0


def test_skips_outside_window(monkeypatch):
    monkeypatch.setattr(w, "_within_window", lambda t: False)
    state = w.WatcherState()
    assert _run(state, NOW, primary_alive=False) == []
    # nothing evaluated → no dead streak recorded
    assert state.get(w.PRIMARY).dead_streak == 0


def test_skips_non_trading_day(monkeypatch):
    monkeypatch.setattr(w, "_is_trading_day", lambda d: False)
    state = w.WatcherState()
    assert _run(state, NOW, primary_alive=False) == []


def test_attempt_cap_and_alert(monkeypatch):
    """After max_attempts failed logins in a day, it stops trying and alerts once."""
    alerts = []
    monkeypatch.setattr(w, "_alert", lambda msg: alerts.append(msg))
    # Neutralise backoff so repeated attempts happen on consecutive ticks.
    monkeypatch.setattr(w, "_backoff_ready", lambda target, now_ts: True)

    state = w.WatcherState()
    # Pre-confirm dead so each tick from here attempts.
    state.get(w.PRIMARY).dead_streak = 1  # next dead tick confirms (dead_confirm=2)

    login_calls = []

    def login():
        login_calls.append(1)
        return _fail("primary")

    # Drive many ticks: dead_confirm=2 means tick where streak reaches 2 triggers.
    for _ in range(10):
        _run(state, NOW, primary_alive=False, primary_login=login, dead_confirm=2, max_attempts=3)

    assert len(login_calls) == 3  # capped at max_attempts
    assert len(alerts) == 1  # alerted exactly once at the cap boundary


def test_backoff_blocks_immediate_retry():
    """A second attempt is blocked until the backoff delay elapses."""
    state = w.WatcherState()
    t = state.get(w.PRIMARY)
    t.attempts_today = 1
    t.attempts_date = NOW.date()
    t.last_attempt_ts = NOW.timestamp()
    t.dead_streak = 5  # already confirmed dead

    # Immediately after the attempt: backoff not ready → no new login.
    calls = []
    _run(
        state,
        NOW,
        primary_alive=False,
        primary_login=lambda: calls.append(1) or _fail("primary"),
        dead_confirm=1,
        max_attempts=5,
    )
    assert calls == []


def test_daily_attempt_counter_rolls_over():
    state = w.WatcherState()
    t = state.get(w.PRIMARY)
    t.attempts_today = 5
    t.attempts_date = datetime(2026, 8, 18, tzinfo=_IST).date()  # yesterday
    t.dead_streak = 5

    calls = []
    _run(
        state,
        NOW,
        primary_alive=False,
        primary_login=lambda: calls.append(1) or _ok("primary"),
        dead_confirm=1,
        max_attempts=5,
    )
    # New day → counter reset → an attempt is allowed again.
    assert calls == [1]


def test_children_evaluated_and_isolated():
    """2-tuple children (legacy shape) still work and imply can_heal=True."""
    state = w.WatcherState()
    children = [(1, "kid1"), (2, "kid2")]

    # kid1 dead+confirmed, kid2 alive.
    state.get("child:1").dead_streak = 1
    child_login_calls = []

    results = _run(
        state,
        NOW,
        primary_alive=True,
        children=children,
        child_alive=lambda aid: aid == 2,  # kid1 dead, kid2 alive
        child_login=lambda aid, name: child_login_calls.append(aid) or _ok(f"child:{aid}"),
        dead_confirm=2,
    )
    assert child_login_calls == [1]
    assert any(r["scope"] == "child:1" and r["ok"] for r in results)


# --------------------------------------------------------------------------- #
# Issue #658: alert-only children (auto-login OFF) + broker-verified probe
# --------------------------------------------------------------------------- #
def test_alert_only_child_mid_session_kill_alerts_once_never_logs_in(monkeypatch):
    """A non-healable child that was alive today and then dies alerts once/day."""
    alerts = []
    monkeypatch.setattr(w, "_alert", lambda msg: alerts.append(msg))
    state = w.WatcherState()
    children = [(1, "manual-kid", False)]
    login_calls = []

    def login(aid, name):
        login_calls.append(aid)
        return _ok(f"child:{aid}")

    # Tick 1: alive → was_alive_today recorded.
    _run(state, NOW, primary_alive=True, children=children, child_alive=lambda aid: True)
    assert state.get("child:1").was_alive_today is True

    # Tick 2: dead, unconfirmed → quiet.
    _run(
        state,
        NOW,
        primary_alive=True,
        children=children,
        child_alive=lambda aid: False,
        child_login=login,
    )
    assert alerts == []

    # Tick 3: confirmed dead → alert fires, login does NOT.
    _run(
        state,
        NOW,
        primary_alive=True,
        children=children,
        child_alive=lambda aid: False,
        child_login=login,
    )
    assert len(alerts) == 1
    assert "manual login needed" in alerts[0]
    assert login_calls == []

    # Tick 4: still dead → no second alert today.
    _run(
        state,
        NOW,
        primary_alive=True,
        children=children,
        child_alive=lambda aid: False,
        child_login=login,
    )
    assert len(alerts) == 1


def test_alert_only_child_never_alive_today_stays_quiet(monkeypatch):
    """Not-yet-logged-in morning is the login reminders' job, not the watcher's."""
    alerts = []
    monkeypatch.setattr(w, "_alert", lambda msg: alerts.append(msg))
    state = w.WatcherState()
    children = [(1, "manual-kid", False)]

    for _ in range(5):
        _run(state, NOW, primary_alive=True, children=children, child_alive=lambda aid: False)
    assert alerts == []


def test_alert_only_flags_reset_next_day(monkeypatch):
    """A new day re-arms the once-a-day alert (roll_day resets both flags)."""
    alerts = []
    monkeypatch.setattr(w, "_alert", lambda msg: alerts.append(msg))
    state = w.WatcherState()
    children = [(1, "manual-kid", False)]

    # Day 1: alive → dead ×2 → alert.
    _run(state, NOW, primary_alive=True, children=children, child_alive=lambda aid: True)
    for _ in range(2):
        _run(state, NOW, primary_alive=True, children=children, child_alive=lambda aid: False)
    assert len(alerts) == 1

    # Day 2: same pattern → alerts again.
    day2 = NOW.replace(day=NOW.day + 1)
    _run(state, day2, primary_alive=True, children=children, child_alive=lambda aid: True)
    for _ in range(2):
        _run(state, day2, primary_alive=True, children=children, child_alive=lambda aid: False)
    assert len(alerts) == 2


def test_healable_child_still_heals_without_being_alive_first():
    """can_heal=True keeps #654 semantics: morning auto-login needs no prior-alive."""
    state = w.WatcherState()
    children = [(1, "kid", True)]
    login_calls = []

    for _ in range(2):
        _run(
            state,
            NOW,
            primary_alive=True,
            children=children,
            child_alive=lambda aid: False,
            child_login=lambda aid, name: login_calls.append(aid) or _ok(f"child:{aid}"),
        )
    assert login_calls == [1]


def test_probe_child_is_broker_verified(monkeypatch):
    """_probe_child layers probe_token on the date pre-filter (issue #658)."""
    import database.auth_db as auth_db
    import database.broker_accounts_db as adb
    import services.broker_accounts_service as accounts_svc
    import services.broker_session_health as health

    acct = {"id": 7, "broker": "zerodha"}
    monkeypatch.setattr(adb, "get_account", lambda aid: acct)
    monkeypatch.setattr(adb, "auth_name", lambda aid: f"acct:{aid}")
    monkeypatch.setattr(auth_db, "get_auth_token", lambda name: "key:token")

    probe_calls = []

    def fake_probe(broker, token):
        probe_calls.append((broker, token))
        return True

    monkeypatch.setattr(health, "probe_token", fake_probe)

    # Fresh date + live token → alive, probe consulted.
    monkeypatch.setattr(accounts_svc, "_is_connected", lambda a: True)
    assert w._probe_child(7) is True
    assert probe_calls == [("zerodha", "key:token")]

    # Broker says dead → dead, even with a fresh date.
    monkeypatch.setattr(health, "probe_token", lambda b, t: False)
    assert w._probe_child(7) is False

    # Stale date short-circuits WITHOUT a broker call.
    probe_calls.clear()
    monkeypatch.setattr(health, "probe_token", fake_probe)
    monkeypatch.setattr(accounts_svc, "_is_connected", lambda a: False)
    assert w._probe_child(7) is False
    assert probe_calls == []

    # Missing token row → dead, no broker call.
    monkeypatch.setattr(accounts_svc, "_is_connected", lambda a: True)
    monkeypatch.setattr(auth_db, "get_auth_token", lambda name: None)
    assert w._probe_child(7) is False
    assert probe_calls == []


def test_alert_reaches_notification_service(monkeypatch):
    """Regression (#688): ``_alert`` imported a module-level ``notify`` that
    ``services.notification_service`` never had — the ImportError was swallowed
    and the watcher's loud alerts (e.g. "manual login needed") never sent.
    Drive the real ``_alert`` (no stubbing) down to the
    ``get_notification_service()`` seam."""
    from types import SimpleNamespace

    monkeypatch.setenv("NOTIFY_BROKER_AUTO_LOGIN", "true")
    import services.notification_service as ns

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ns,
        "get_notification_service",
        lambda: SimpleNamespace(notify=lambda event, msg, **md: sent.append((event, msg))),
    )

    w._alert("manual login needed for child kid1")

    assert sent == [("broker_auto_login", "⚠️ manual login needed for child kid1")], (
        "alert never reached the notification service (the #688 silent drop)"
    )
