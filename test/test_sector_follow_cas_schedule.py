"""Schedule-resolution tests for sector_follow_cap5_vol (issue #512).

From 2026-08-03 NSE ends continuous trading in CAS-eligible cash scrips (every
F&O name — the whole LOCK_STATIC_30 universe) at 15:15 IST, then runs a Closing
Auction Session 15:15..15:35 whose 15:25..15:30 phase accepts LIMIT orders only.
This strategy places CNC MARKET orders with no MIS square-off backstop, so its
whole job chain must fire inside continuous trading.

These tests pin the contract: the defaults, the env tunables, the
continuous-trading clamp, and the refresh < smoke < entry < exit ordering.
"""

from datetime import time

import pytest

from services.sector_follow_backfill_scheduler import preentry_refresh_time
from services.sector_follow_service import (
    _CONTINUOUS_CLOSE_IST,
    _SCHEDULE_CEILING_IST,
    resolve_schedule,
)

_SCHEDULE_ENV = (
    "SECTOR_FOLLOW_SMOKE_CHECK_TIME",
    "SECTOR_FOLLOW_ENTRY_TIME",
    "SECTOR_FOLLOW_EXIT_TIME",
    "SECTOR_FOLLOW_PREENTRY_REFRESH_TIME",
)


@pytest.fixture(autouse=True)
def _clear_schedule_env(monkeypatch):
    """Defaults must be observable regardless of the operator's live .env."""
    for var in _SCHEDULE_ENV:
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
def test_default_schedule_is_the_post_cas_window():
    assert resolve_schedule() == {
        "smoke": time(15, 3),
        "entry": time(15, 5),
        "exit": time(15, 10),
    }


def test_default_preentry_refresh_precedes_the_smoke_check():
    assert preentry_refresh_time() == time(15, 2)
    assert preentry_refresh_time() < resolve_schedule()["smoke"]


def test_every_default_lands_inside_continuous_trading():
    """The whole chain must complete before the 15:15 CAS cutoff."""
    times = [preentry_refresh_time(), *resolve_schedule().values()]
    assert max(times) <= _SCHEDULE_CEILING_IST < _CONTINUOUS_CLOSE_IST


def test_ordering_invariant_holds_for_defaults():
    sched = resolve_schedule()
    assert preentry_refresh_time() < sched["smoke"] < sched["entry"] <= sched["exit"]


# --------------------------------------------------------------------------- #
# Env tunables
# --------------------------------------------------------------------------- #
def test_env_overrides_are_honoured(monkeypatch):
    monkeypatch.setenv("SECTOR_FOLLOW_SMOKE_CHECK_TIME", "14:30")
    monkeypatch.setenv("SECTOR_FOLLOW_ENTRY_TIME", "14:35")
    monkeypatch.setenv("SECTOR_FOLLOW_EXIT_TIME", "14:40")
    assert resolve_schedule() == {
        "smoke": time(14, 30),
        "entry": time(14, 35),
        "exit": time(14, 40),
    }


@pytest.mark.parametrize("bad", ["", "nonsense", "25:00", "15:99", "1520", "15:20:00"])
def test_malformed_env_falls_back_to_default_without_raising(monkeypatch, bad):
    """A typo in .env must never leave the strategy unscheduled."""
    monkeypatch.setenv("SECTOR_FOLLOW_ENTRY_TIME", bad)
    assert resolve_schedule()["entry"] == time(15, 5)


# --------------------------------------------------------------------------- #
# The CAS clamp — the actual safety property
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("late", ["15:11", "15:20", "15:25", "15:30", "15:40"])
def test_times_past_the_ceiling_are_clamped_back(monkeypatch, late):
    """An operator must not be able to push an order into the auction window."""
    monkeypatch.setenv("SECTOR_FOLLOW_EXIT_TIME", late)
    assert resolve_schedule()["exit"] == _SCHEDULE_CEILING_IST


def test_the_pre_cas_schedule_can_no_longer_be_restored_by_env(monkeypatch):
    """Golden regression: the old 15:20/15:25 pair is exactly what broke."""
    monkeypatch.setenv("SECTOR_FOLLOW_ENTRY_TIME", "15:20")
    monkeypatch.setenv("SECTOR_FOLLOW_EXIT_TIME", "15:25")
    sched = resolve_schedule()
    assert sched["entry"] <= _SCHEDULE_CEILING_IST
    assert sched["exit"] <= _SCHEDULE_CEILING_IST


def test_ordering_violation_reverts_the_whole_schedule_to_defaults(monkeypatch):
    """Exit before entry would square off T+1 before today's buy — reject it."""
    monkeypatch.setenv("SECTOR_FOLLOW_ENTRY_TIME", "15:08")
    monkeypatch.setenv("SECTOR_FOLLOW_EXIT_TIME", "15:04")
    assert resolve_schedule() == {
        "smoke": time(15, 3),
        "entry": time(15, 5),
        "exit": time(15, 10),
    }


# --------------------------------------------------------------------------- #
# Registered APScheduler jobs reflect the resolved schedule
# --------------------------------------------------------------------------- #
class _FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, func, trigger=None, id=None, replace_existing=False, name=None):
        self.jobs[id] = {"trigger": trigger, "name": name}


def _cron_hhmm(trigger) -> time:
    fields = {f.name: str(f) for f in trigger.fields}
    return time(int(fields["hour"]), int(fields["minute"]))


def _register(monkeypatch):
    from services.sector_follow_service import SectorFollowService

    svc = SectorFollowService.__new__(SectorFollowService)
    svc.scheduler = None
    svc.mode = "scaffold"
    svc.strategy_id = 1
    sched = _FakeScheduler()
    svc.register_jobs(scheduler=sched)
    return sched


def test_registered_jobs_use_the_resolved_times(monkeypatch):
    sched = _register(monkeypatch)
    assert _cron_hhmm(sched.jobs["sector_follow_entry"]["trigger"]) == time(15, 5)
    assert _cron_hhmm(sched.jobs["sector_follow_exit"]["trigger"]) == time(15, 10)
    assert _cron_hhmm(sched.jobs["sector_follow_smoke_check"]["trigger"]) == time(15, 3)


def test_registered_job_names_report_the_real_fire_time(monkeypatch):
    """The operator reads these names in the scheduler UI — they must not lie."""
    sched = _register(monkeypatch)
    assert "15:05" in sched.jobs["sector_follow_entry"]["name"]
    assert "15:10" in sched.jobs["sector_follow_exit"]["name"]


def test_registered_jobs_follow_env_overrides(monkeypatch):
    monkeypatch.setenv("SECTOR_FOLLOW_ENTRY_TIME", "14:50")
    monkeypatch.setenv("SECTOR_FOLLOW_EXIT_TIME", "14:55")
    monkeypatch.setenv("SECTOR_FOLLOW_SMOKE_CHECK_TIME", "14:45")
    sched = _register(monkeypatch)
    assert _cron_hhmm(sched.jobs["sector_follow_entry"]["trigger"]) == time(14, 50)
    assert _cron_hhmm(sched.jobs["sector_follow_exit"]["trigger"]) == time(14, 55)


def test_exit_job_never_registers_inside_the_auction_window(monkeypatch):
    monkeypatch.setenv("SECTOR_FOLLOW_EXIT_TIME", "15:25")
    sched = _register(monkeypatch)
    fired = _cron_hhmm(sched.jobs["sector_follow_exit"]["trigger"])
    assert fired < _CONTINUOUS_CLOSE_IST
