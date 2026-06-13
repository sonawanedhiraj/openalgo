"""Unit tests for the scanner WS liveness watchdog policy.

The watchdog's tick source, clock, market-hours predicate, and recovery
actions are all injected, so these tests drive the escalation logic with no
broker, WebSocket, or wall-clock dependency.
"""

from datetime import datetime

from services.scanner_ws_watchdog import (
    _IST,
    ScannerWsWatchdog,
    _default_market_open,
)


class Clock:
    """Mutable callable clock — Clock(t)() returns t; advance with .tick(n)."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, n):
        self.t += n


class Recorder:
    """Counts how many times it was invoked (stand-in for a recovery action)."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


def _make(clock, last_holder, *, market_open=True, **kw):
    """Build a watchdog whose tick_source reads last_holder[0]."""
    soft, hard = Recorder(), Recorder()
    wd = ScannerWsWatchdog(
        tick_source=lambda: last_holder[0],
        recover_soft=soft,
        recover_hard=hard,
        now=clock,
        market_open=(lambda _t: market_open),
        **kw,
    )
    return wd, soft, hard


# --- market-hours predicate (real default) ----------------------------------


def _ist_epoch(h, m):
    # 2026-06-04 is a Thursday (weekday) — a normal trading day.
    return datetime(2026, 6, 4, h, m, tzinfo=_IST).timestamp()


def test_pre_market_no_action():
    clock = Clock(_ist_epoch(9, 0))  # 09:00 IST, before open
    soft, hard = Recorder(), Recorder()
    wd = ScannerWsWatchdog(
        tick_source=lambda: clock() - 9999,  # very stale, but market closed
        recover_soft=soft,
        recover_hard=hard,
        now=clock,
        market_open=_default_market_open,
    )
    assert wd.check() == "closed"
    assert soft.calls == 0 and hard.calls == 0


def test_post_market_no_action():
    clock = Clock(_ist_epoch(16, 0))  # 16:00 IST, after close
    soft, hard = Recorder(), Recorder()
    wd = ScannerWsWatchdog(
        tick_source=lambda: clock() - 9999,
        recover_soft=soft,
        recover_hard=hard,
        now=clock,
        market_open=_default_market_open,
    )
    assert wd.check() == "closed"
    assert soft.calls == 0 and hard.calls == 0


def test_market_open_predicate_boundaries():
    assert _default_market_open(_ist_epoch(9, 15)) is True
    assert _default_market_open(_ist_epoch(15, 30)) is True
    assert _default_market_open(_ist_epoch(9, 14)) is False
    # 2026-06-06 is a Saturday — closed even at midday.
    sat = datetime(2026, 6, 6, 11, 0, tzinfo=_IST).timestamp()
    assert _default_market_open(sat) is False


# --- staleness escalation -----------------------------------------------------


def test_fresh_ticks_no_action():
    clock = Clock(1000.0)
    holder = [clock() - 10]  # 10s old — well within 90s
    wd, soft, hard = _make(clock, holder)
    assert wd.check() == "fresh"
    assert soft.calls == 0 and hard.calls == 0


def test_no_ticks_yet_no_action():
    clock = Clock(1000.0)
    wd, soft, hard = _make(clock, [None])
    assert wd.check() == "no_ticks"
    assert soft.calls == 0 and hard.calls == 0


def test_stale_90s_triggers_soft_recovery():
    clock = Clock(1000.0)
    holder = [clock() - 120]  # 120s old > 90s soft threshold
    wd, soft, hard = _make(clock, holder)
    assert wd.check() == "soft"
    assert soft.calls == 1 and hard.calls == 0


def test_cooldown_blocks_double_trigger():
    clock = Clock(1000.0)
    holder = [clock() - 120]
    wd, soft, hard = _make(clock, holder)
    assert wd.check() == "soft"  # first stall -> soft
    clock.tick(30)  # 30s later, still inside 60s cooldown
    holder[0] = clock() - 150  # still stale
    assert wd.check() == "cooldown"
    assert soft.calls == 1  # NOT retriggered
    assert hard.calls == 0


def test_still_stale_after_cooldown_triggers_hard_recovery():
    clock = Clock(1000.0)
    holder = [clock() - 120]
    wd, soft, hard = _make(clock, holder)
    assert wd.check() == "soft"
    clock.tick(70)  # past the 60s cooldown
    holder[0] = clock() - 200  # 200s old > 180s hard threshold
    assert wd.check() == "hard"
    assert soft.calls == 1 and hard.calls == 1


def test_recovery_resets_after_feed_returns():
    clock = Clock(1000.0)
    holder = [clock() - 120]
    wd, soft, hard = _make(clock, holder)
    assert wd.check() == "soft"
    # Feed comes back fresh — cooldown state should clear.
    clock.tick(10)
    holder[0] = clock() - 5
    assert wd.check() == "fresh"
    assert wd._cooldown_start is None
    # A later stall is treated as a first detection again -> soft, not hard.
    clock.tick(200)
    holder[0] = clock() - 300
    assert wd.check() == "soft"
    assert soft.calls == 2 and hard.calls == 0
