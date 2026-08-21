"""Regression tests for the late-boot day-log clobber (issue #597).

Observed 2026-08-13: the day traded (selection + entries + exits persisted in
the day log), then an OpenAlgo restart at 14:33 re-ran ``arm()``, which reset
``day_log`` to ``[]``, logged ``skipped_late_boot`` and persisted it —
REPLACING the real events. The /open15_vol_breakout/logs history row then read
"skipped / skipped_late_boot · 0 sel · 0 filled" while the P&L chips beside it
showed the day's real fills.

The fix: a late-boot ``arm()`` on a date that already has a persisted day log
loads it and appends a ``late_boot_restart`` marker instead of clobbering it.
A genuinely fresh late-boot day keeps the loud ``skipped_late_boot`` persist.
"""

import os

os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "0" * 64)

import datetime as dt  # noqa: E402

import pytest  # noqa: E402

import database.open15_breakout_db as db  # noqa: E402
from services.open15_breakout_service import IST, Open15BreakoutService  # noqa: E402

TRADE_DATE = "2026-08-13"

# Trimmed real-shaped events for a day that traded (cf. test_open15_log_view).
TRADED_DAY = [
    {"ts": "09:10:00.190", "event": "armed", "universe": 211, "vol_mult": 1.5},
    {
        "ts": "09:16:00.167",
        "event": "selection",
        "selected": {"ASHOKLEY": "L", "OFSS": "L", "DRREDDY": "S"},
        "candidates": 211,
    },
    {
        "ts": "09:25:36.168",
        "event": "entry",
        "symbol": "ASHOKLEY",
        "side": "BUY",
        "qty": 100,
        "order_status": "success",
    },
    {
        "ts": "09:30:04.076",
        "event": "exit",
        "symbol": "ASHOKLEY",
        "action": "SELL",
        "qty": 100,
        "pnl": 1438.0,
        "order_status": "success",
        "reason": "eod_0930",
    },
    {"ts": "09:35:00.014", "event": "summary", "selected": 3, "entered": 1, "day": "done"},
]


@pytest.fixture
def late_boot_service(monkeypatch):
    """A service whose clock reads 14:33 IST on TRADE_DATE (post-09:15:30)."""
    db.init_db()
    # tests in this file share the (conftest-redirected temp) DB — start each
    # from a clean slate for the date under test
    db.db_session.query(db.Open15DayLog).filter(db.Open15DayLog.trade_date == TRADE_DATE).delete()
    db.db_session.commit()
    late_now = IST.localize(dt.datetime(2026, 8, 13, 14, 33, 9))
    monkeypatch.setattr(Open15BreakoutService, "_now_ist", staticmethod(lambda: late_now))
    return Open15BreakoutService(order_placer=lambda mode, order: {"status": "success"})


def test_late_boot_arm_preserves_traded_day_log(late_boot_service):
    assert db.save_day_log(TRADE_DATE, TRADED_DAY)

    late_boot_service.arm()

    # this process instance still refuses to trade...
    assert late_boot_service.day_status == "skipped_late_boot"
    # ...but the persisted log keeps every real event, with a restart marker
    persisted = db.get_day_log(TRADE_DATE)
    events = [e["event"] for e in persisted]
    assert events[: len(TRADED_DAY)] == [e["event"] for e in TRADED_DAY]
    assert events[-1] == "late_boot_restart"
    assert "skipped_late_boot" not in events
    marker = persisted[-1]
    assert marker["armed_at"] == "14:33:09"
    assert marker["preserved_events"] == len(TRADED_DAY)


def test_late_boot_arm_preserved_day_digest_keeps_real_status(late_boot_service):
    from services.open15_log_view import summarize_day

    db.save_day_log(TRADE_DATE, TRADED_DAY)
    late_boot_service.arm()

    d = summarize_day(TRADE_DATE, db.get_day_log(TRADE_DATE))
    assert d["status"] == "done"
    assert d["selected"] == 3
    assert d["entered"] == 1
    assert d["pnl"] == 1438.0


def test_late_boot_arm_on_fresh_day_still_persists_loud_skip(late_boot_service):
    late_boot_service.arm()

    assert late_boot_service.day_status == "skipped_late_boot"
    persisted = db.get_day_log(TRADE_DATE)
    assert [e["event"] for e in persisted] == ["skipped_late_boot"]
    assert persisted[0]["armed_at"] == "14:33:09"


def test_second_late_boot_appends_marker_over_persisted_skip(late_boot_service):
    # first late boot on a fresh day persists the skip; a second restart must
    # append to it, not overwrite it with a fresh single-event log
    late_boot_service.arm()
    late_boot_service.arm()

    events = [e["event"] for e in db.get_day_log(TRADE_DATE)]
    assert events == ["skipped_late_boot", "late_boot_restart"]
