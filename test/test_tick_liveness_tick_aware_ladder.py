"""Tick-aware heal-ladder pacing (issue #675).

On 2026-08-25 heal step 1 (re-subscribe) restored the tick feed at 09:25:25,
but the watchdog's only recovery signal is the scanner's 5m bar-close stamp —
and a bar only closes when a tick lands in a LATER bucket, so the earliest
possible proof of step 1's success was the 09:30:00 roll. The ladder's 120s
step wait escalated to step 2 (adapter reconnect) at 09:27:25, which killed
the feed step 1 had just healed (pre-#673 the reconnect also wiped every
subscription). The ladder now paces on a tick-granular stamp while the bar
close stays the HEALTH verdict:

  - fresh ticks hold the ladder (``ticks_fresh_awaiting_bar``) — escalating a
    transport-level heal while the pipeline is already RECEIVING is always
    wrong;
  - the bar close still delivers RECOVERED (unchanged semantics);
  - ticks fresh + bars silent past a full bucket = the wedge is AFTER
    ingestion → one distinct alert, then the ladder resumes;
  - no tick signal (provider None or ticks dark) = byte-identical pre-#675
    escalation — which is also why the constructor default is None and only
    ``init_tick_liveness_watchdog`` wires the production provider (a default
    read of the process-wide scanner stamp would let one test file's tick
    hold the ladder in another — the #470/#472 pollution class).

Frozen-clock, same pattern as test_tick_liveness_watchdog.py.
"""

from __future__ import annotations

import datetime as dt
import json
from datetime import timezone
from unittest import mock

import pytest

from services import scanner_service
from services import tick_liveness_watchdog as tlw

_IST = timezone(dt.timedelta(hours=5, minutes=30))


def _ist(h, mi, s=0) -> dt.datetime:
    """2026-08-25 (the incident day, a Tuesday) at the given IST time."""
    return dt.datetime(2026, 8, 25, h, mi, s, tzinfo=_IST)


def _wall(when: dt.datetime) -> float:
    return when.timestamp()


@pytest.fixture(autouse=True)
def _enable_flags(monkeypatch):
    monkeypatch.setenv("SCANNER_ENABLED", "true")
    monkeypatch.setenv("TICK_LIVENESS_WATCHDOG_ENABLED", "true")
    monkeypatch.setenv("TICK_LIVENESS_AUTOHEAL_ENABLED", "true")
    monkeypatch.setenv("SCANNER_LIVENESS_MAX_SILENT_MIN", "10")
    monkeypatch.setenv("SCANNER_LIVENESS_REALERT_MIN", "30")
    monkeypatch.setenv("SCANNER_LIVENESS_LADDER_COOLDOWN_MIN", "30")


def _make_wd(clock, state, steps, *, with_tick_provider=True):
    """Watchdog over mutable clock/state dicts so one instance can be driven
    through a whole timeline."""
    alerts: list[str] = []
    wd = tlw.TickLivenessWatchdog(
        last_bar_provider=lambda: state.get("last_bar"),
        notifier=alerts.append,
        now_provider=lambda: clock["now"],
        trading_day_checker=lambda _d: True,
        heal_steps=steps,
        started_wall=_wall(_ist(8, 57)),
        last_tick_provider=(lambda: state.get("last_tick")) if with_tick_provider else None,
    )
    return wd, alerts


def _recording_steps(names, on_dispatch=None):
    calls: list[str] = []

    def make(name):
        def fn():
            calls.append(name)
            if on_dispatch:
                on_dispatch(name)
            return True

        return fn

    return [(n, make(n)) for n in names], calls


# --------------------------------------------------------------------------- #
# THE 2026-08-25 incident, replayed with the fix
# --------------------------------------------------------------------------- #


def test_successful_step1_holds_the_ladder_until_the_bar_close_recovers():
    """Feed dead from open; step 1 restores ticks at 09:25:25; the 5m bar
    cannot close before the 09:30:00 roll. The ladder must HOLD on fresh
    ticks — step 2 (the reconnect that re-killed the feed on 2026-08-25)
    never fires — and the 09:30 bar close delivers RECOVERED."""
    clock = {"now": _ist(9, 25, 24)}
    state = {"last_bar": None, "last_tick": None}

    def on_dispatch(name):
        if name == "resubscribe_nudge":
            # the re-subscribe worked: ticks start flowing immediately
            state["last_tick"] = _wall(clock["now"]) + 1.0

    steps, calls = _recording_steps(
        ["resubscribe_nudge", "adapter_reconnect", "ws_proxy_restart"], on_dispatch
    )
    wd, alerts = _make_wd(clock, state, steps)

    res = wd.check()  # 09:25:24 — CRIT + step 1
    assert res["status"] == "alerted"
    assert calls == ["resubscribe_nudge"]

    # ticks keep flowing; polls at 09:25:55 .. 09:29:55 must all HOLD.
    for h, mi, s in [(9, 25, 55), (9, 26, 25), (9, 26, 55), (9, 27, 25), (9, 28, 55), (9, 29, 55)]:
        clock["now"] = _ist(h, mi, s)
        state["last_tick"] = _wall(clock["now"]) - 5.0
        res = wd.check()
        assert res["ladder"] == "ticks_fresh_awaiting_bar", (h, mi, s)
    assert calls == ["resubscribe_nudge"]  # step 2 NEVER fired

    # 09:30:00 roll closes the 09:25-09:30 bucket -> stamp -> RECOVERED.
    clock["now"] = _ist(9, 30, 25)
    state["last_bar"] = _wall(_ist(9, 30, 1))
    res = wd.check()
    assert res["status"] == "recovered"
    assert calls == ["resubscribe_nudge"]
    assert any("RECOVERED" in a for a in alerts)


def test_dark_ticks_escalate_exactly_as_before():
    """No tick ever arrives: the ladder walks all three steps on the 120s
    cadence — the pre-#675 behavior, unchanged."""
    clock = {"now": _ist(9, 25, 24)}
    state = {"last_bar": None, "last_tick": None}
    steps, calls = _recording_steps(["s1", "s2", "s3"])
    wd, _ = _make_wd(clock, state, steps)

    wd.check()
    clock["now"] = _ist(9, 27, 25)
    wd.check()
    clock["now"] = _ist(9, 29, 26)
    wd.check()
    assert calls == ["s1", "s2", "s3"]


def test_no_provider_is_byte_identical():
    """Constructed without a tick provider (every pre-#675 call site and
    test): escalation proceeds regardless of any tick state."""
    clock = {"now": _ist(9, 25, 24)}
    state = {"last_bar": None, "last_tick": _wall(_ist(9, 25, 20))}  # fresh, but invisible
    steps, calls = _recording_steps(["s1", "s2"])
    wd, _ = _make_wd(clock, state, steps, with_tick_provider=False)

    wd.check()
    clock["now"] = _ist(9, 27, 25)
    wd.check()
    assert calls == ["s1", "s2"]


def test_ticks_flowing_but_no_bar_close_alerts_wedge_once_then_resumes():
    """Ticks fresh past the full-bucket grace with bars still silent: the
    wedge is AFTER ingestion — one distinct alert, ladder resumes."""
    clock = {"now": _ist(9, 25, 24)}
    state = {"last_bar": None, "last_tick": None}
    steps, calls = _recording_steps(["s1", "s2", "s3"])
    wd, alerts = _make_wd(clock, state, steps)

    wd.check()  # step 1
    assert calls == ["s1"]
    # ticks flow from 09:25:30 onward; grace is 330s from first observation
    for h, mi, s in [(9, 25, 55), (9, 27, 25), (9, 29, 25), (9, 30, 55)]:
        clock["now"] = _ist(h, mi, s)
        state["last_tick"] = _wall(clock["now"]) - 5.0
        res = wd.check()
        assert res["ladder"] == "ticks_fresh_awaiting_bar"
    # past the grace: wedge alert + ladder resumes with step 2
    clock["now"] = _ist(9, 31, 55)
    state["last_tick"] = _wall(clock["now"]) - 5.0
    res = wd.check()
    assert calls == ["s1", "s2"]
    wedge = [a for a in alerts if "AFTER tick" in a]
    assert len(wedge) == 1
    # further checks: no duplicate wedge alert
    clock["now"] = _ist(9, 33, 56)
    state["last_tick"] = _wall(clock["now"]) - 5.0
    wd.check()
    assert len([a for a in alerts if "AFTER tick" in a]) == 1
    assert calls == ["s1", "s2", "s3"]


def test_ticks_resume_then_die_again_releases_the_hold():
    """The hold is conditional on ticks STAYING fresh: a second death (the
    2026-08-25 step-2-wipe shape) releases it and the ladder escalates."""
    clock = {"now": _ist(9, 25, 24)}
    state = {"last_bar": None, "last_tick": None}
    steps, calls = _recording_steps(["s1", "s2"])
    wd, _ = _make_wd(clock, state, steps)

    wd.check()  # step 1
    clock["now"] = _ist(9, 25, 55)
    state["last_tick"] = _wall(clock["now"]) - 5.0
    assert wd.check()["ladder"] == "ticks_fresh_awaiting_bar"

    # feed dies again: tick stamp goes stale -> hold releases -> step 2
    clock["now"] = _ist(9, 28, 0)
    res = wd.check()
    assert calls == ["s1", "s2"]
    assert res["ladder"].startswith("step_s2")


def test_recovered_resets_the_tick_hold_state():
    """After RECOVERED, a later outage starts a clean hold cycle (no stale
    _ticks_resumed_wall anchor shortening the next grace window)."""
    clock = {"now": _ist(9, 25, 24)}
    state = {"last_bar": None, "last_tick": None}
    steps, _calls = _recording_steps(["s1"])
    wd, _ = _make_wd(clock, state, steps)

    wd.check()
    clock["now"] = _ist(9, 26, 0)
    state["last_tick"] = _wall(clock["now"]) - 5.0
    wd.check()
    assert wd._ticks_resumed_wall is not None
    clock["now"] = _ist(9, 30, 25)
    state["last_bar"] = _wall(_ist(9, 30, 1))
    assert wd.check()["status"] == "recovered"
    assert wd._ticks_resumed_wall is None and wd._tick_wedge_alerted is False


# --------------------------------------------------------------------------- #
# The scanner-side tick stamp
# --------------------------------------------------------------------------- #


def test_ingest_message_stamps_live_tick_for_watched_symbol_only():
    scanner_service._reset_live_tick_for_tests()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=mock.MagicMock())
    with mock.patch.object(svc.aggregator, "on_tick"):
        svc._ingest_message(
            "NSE_INFY_QUOTE",  # unwatched — must not stamp
            json.dumps({"ltp": 1500.0, "volume": 1000, "timestamp": 1748580900}),
        )
        assert scanner_service.get_last_live_tick_wall() is None
        svc._ingest_message(
            "NSE_RELIANCE_QUOTE",
            json.dumps({"ltp": 2500.0, "volume": 1000, "timestamp": 1748580900}),
        )
    assert scanner_service.get_last_live_tick_wall() is not None
    scanner_service._reset_live_tick_for_tests()


def test_replayed_bars_never_stamp_the_tick_heartbeat():
    """Replays enter via MultiIntervalAggregator.replay_bars, not
    _ingest_message — a historical replay must not make a dead feed look
    alive to the ladder (same rule as the bar-close stamp)."""
    scanner_service._reset_live_tick_for_tests()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=mock.MagicMock())
    bars = [
        {
            "ts": 1748580900,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
        }
    ]
    try:
        svc.aggregator.replay_bars("RELIANCE", bars)
    except Exception:
        pass  # shape differences are irrelevant — the stamp must stay None
    assert scanner_service.get_last_live_tick_wall() is None
    scanner_service._reset_live_tick_for_tests()
