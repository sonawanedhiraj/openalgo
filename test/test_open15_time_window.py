"""Tests for the UI-configurable open15 entry-cutoff / exit-time window (issue #451).

Covers: HH:MM parsing + window validation, day-config resolution (row -> env ->
defaults, invalid pair falls back), the core's split entry/tracking gates, the
exit/retry/summary schedule resolution used at boot and re-applied at arm, the
DB round-trip for the new columns, and the blueprint's cross-field validation.
"""

import os

os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "0" * 64)

import datetime as dt  # noqa: E402

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from services.open15_breakout_service import (  # noqa: E402
    Open15BreakoutService,
    Open15Core,
    _exit_schedule_times,
    parse_hhmm,
    resolve_day_config,
    validate_window,
)


def t(h, m, s=0):
    return dt.datetime(2026, 7, 21, h, m, s)


def feed_first_candle(core, sym, opens, cum_end):
    core.on_tick(sym, opens, cum_end * 0.4, t(9, 15, 1))
    core.on_tick(sym, opens * 1.001, cum_end, t(9, 15, 55))


# ---- parse / validate ----------------------------------------------------- #


def test_parse_hhmm():
    assert parse_hhmm("09:29") == 9 * 60 + 29
    assert parse_hhmm("15:10") == 15 * 60 + 10
    assert parse_hhmm("9:05") == 9 * 60 + 5
    for bad in ("", None, "junk", "25:00", "09:60", "09", "09:xx", 930):
        assert parse_hhmm(bad) is None, bad


def test_validate_window():
    assert validate_window("09:29", "09:30") == []
    assert validate_window("09:45", "10:00") == []
    assert validate_window("15:05", "15:10") == []
    assert any("HH:MM" in e for e in validate_window("junk", "09:30"))
    assert any("09:16" in e for e in validate_window("09:15", "09:30"))
    assert any("15:10" in e for e in validate_window("09:29", "15:11"))
    assert any("after no_entry_after" in e for e in validate_window("09:30", "09:30"))
    assert any("after no_entry_after" in e for e in validate_window("10:00", "09:45"))


# ---- day-config resolution ------------------------------------------------ #


def test_resolve_day_config_window_defaults():
    cfg = resolve_day_config(None, 0.0)
    assert cfg["no_entry_after"] == "09:29"
    assert cfg["exit_time"] == "09:30"


def test_resolve_day_config_window_from_row():
    cfg = resolve_day_config({"no_entry_after": "09:45", "exit_time": "10:00"}, 0.0)
    assert cfg["no_entry_after"] == "09:45"
    assert cfg["exit_time"] == "10:00"


def test_resolve_day_config_invalid_window_falls_back():
    cfg = resolve_day_config({"no_entry_after": "10:00", "exit_time": "09:45"}, 0.0)
    assert cfg["no_entry_after"] == "09:29"
    assert cfg["exit_time"] == "09:30"


def test_resolve_day_config_env_default(monkeypatch):
    monkeypatch.setenv("OPEN15_NO_ENTRY_AFTER", "09:40")
    monkeypatch.setenv("OPEN15_EXIT_TIME", "09:50")
    cfg = resolve_day_config(None, 0.0)
    assert (cfg["no_entry_after"], cfg["exit_time"]) == ("09:40", "09:50")
    monkeypatch.setenv("OPEN15_EXIT_TIME", "junk")
    assert resolve_day_config(None, 0.0)["exit_time"] == "09:30"


# ---- core gating ---------------------------------------------------------- #


def _surge_tick(core, sym, minute_ts, level):
    """A tick that satisfies both entry conditions (level break + volume)."""
    return core.on_tick(sym, level + 0.6, 50_000, minute_ts)


def test_core_extended_window_allows_late_entry():
    core = Open15Core(
        {"AAA": 100.0}, vol_mult=1.5, top_n=1, entry_to_min=9 * 60 + 45, track_to_min=10 * 60
    )
    feed_first_candle(core, "AAA", 102.0, 1000)
    h1 = core.sym["AAA"]["fc"]["high"]
    core.on_tick("AAA", 101.5, 1400, t(9, 16, 30))
    # 09:40 is beyond the default 09:29 cutoff but inside the configured one
    action = _surge_tick(core, "AAA", t(9, 40, 10), h1)
    assert action is not None and action["trigger_minute"] == "09:40"


def test_core_blocks_entry_after_cutoff_but_keeps_tracking():
    core = Open15Core(
        {"AAA": 100.0}, vol_mult=1.5, top_n=1, entry_to_min=9 * 60 + 20, track_to_min=9 * 60 + 30
    )
    feed_first_candle(core, "AAA", 102.0, 1000)
    h1 = core.sym["AAA"]["fc"]["high"]
    core.on_tick("AAA", 101.5, 1400, t(9, 16, 30))
    # past the 09:20 cutoff: trigger conditions met but NO entry...
    assert _surge_tick(core, "AAA", t(9, 25, 10), h1) is None
    assert core.entered == {}
    # ...yet the price is still tracked (fresh exit_price for the flatten)
    assert core.last_price["AAA"] == h1 + 0.6
    # past the tracking end (exit minute): tick fully ignored
    core.on_tick("AAA", 90.0, 60_000, t(9, 31, 0))
    assert core.last_price["AAA"] == h1 + 0.6


def test_core_default_window_unchanged():
    core = Open15Core({"AAA": 100.0}, vol_mult=1.5, top_n=1)
    assert core.entry_to_min == 9 * 60 + 29
    assert core.track_to_min == 9 * 60 + 30
    feed_first_candle(core, "AAA", 102.0, 1000)
    h1 = core.sym["AAA"]["fc"]["high"]
    core.on_tick("AAA", 101.5, 1400, t(9, 16, 30))
    assert _surge_tick(core, "AAA", t(9, 35, 0), h1) is None
    assert core.entered == {}


# ---- schedule resolution -------------------------------------------------- #


def test_exit_schedule_times_default():
    assert _exit_schedule_times({"exit_time": "09:30"}) == ((9, 30), (9, 32), (9, 35))


def test_exit_schedule_times_custom_crosses_hour():
    assert _exit_schedule_times({"exit_time": "09:59"}) == ((9, 59), (10, 1), (10, 4))


def test_exit_schedule_times_from_db(monkeypatch):
    import database.open15_breakout_db as db

    monkeypatch.setattr(db, "get_config", lambda: {"no_entry_after": "09:45", "exit_time": "10:00"})
    assert _exit_schedule_times() == ((10, 0), (10, 2), (10, 5))


class _FakeSched:
    def __init__(self):
        self.jobs = {}

    def add_job(self, fn, trigger, id=None, replace_existing=None, misfire_grace_time=None):
        self.jobs[id] = trigger

    def reschedule_job(self, job_id, trigger=None):
        self.jobs[job_id] = trigger


def test_register_jobs_uses_configured_exit_time(monkeypatch):
    import database.open15_breakout_db as db

    monkeypatch.setattr(db, "get_config", lambda: {"no_entry_after": "09:45", "exit_time": "10:00"})
    svc = Open15BreakoutService(order_placer=lambda m, o: {"status": "success"})
    sched = _FakeSched()
    svc.register_jobs(scheduler=sched)
    assert "hour='10'" in str(sched.jobs["open15_exit"])
    assert "minute='2'" in str(sched.jobs["open15_exit_retry"])
    assert "minute='5'" in str(sched.jobs["open15_summary"])


def test_apply_exit_schedule_repoints_jobs():
    svc = Open15BreakoutService(order_placer=lambda m, o: {"status": "success"})
    sched = _FakeSched()
    svc._sched = sched
    svc.day_config = resolve_day_config({"no_entry_after": "09:45", "exit_time": "11:15"}, 0.0)
    svc._apply_exit_schedule()
    assert "hour='11'" in str(sched.jobs["open15_exit"])
    assert "minute='17'" in str(sched.jobs["open15_exit_retry"])
    assert "minute='20'" in str(sched.jobs["open15_summary"])


def test_apply_exit_schedule_noop_without_scheduler():
    svc = Open15BreakoutService(order_placer=lambda m, o: {"status": "success"})
    svc.day_config = resolve_day_config({"exit_time": "10:00"}, 0.0)
    svc._apply_exit_schedule()  # must not raise (unit tests drive arm directly)


# ---- DB round-trip -------------------------------------------------------- #


def test_config_db_roundtrip_new_columns():
    import database.open15_breakout_db as db

    db.init_db()
    assert db.save_config(
        30000.0,
        "fixed",
        1.5,
        updated_by="test",
        instrument="stock",
        max_trades=3,
        no_entry_after="09:45",
        exit_time="10:00",
    )
    cfg = db.get_config()
    assert cfg["no_entry_after"] == "09:45"
    assert cfg["exit_time"] == "10:00"


# ---- blueprint validation ------------------------------------------------- #


@pytest.fixture
def client(monkeypatch):
    import utils.session as sess

    monkeypatch.setattr(sess, "is_session_valid", lambda: True)
    import blueprints.open15_breakout as bp

    app = Flask(__name__)
    app.register_blueprint(bp.open15_bp)
    app.config["TESTING"] = True
    return app.test_client()


def test_config_post_rejects_bad_window(client, monkeypatch):
    import database.open15_breakout_db as db

    monkeypatch.setattr(db, "get_config", lambda: None)
    r = client.post(
        "/open15_vol_breakout/api/config",
        json={"no_entry_after": "10:00", "exit_time": "09:45"},
    )
    assert r.status_code == 400
    assert any("after no_entry_after" in e for e in r.get_json()["errors"])
    r = client.post("/open15_vol_breakout/api/config", json={"exit_time": "15:30"})
    assert r.status_code == 400
    assert any("15:10" in e for e in r.get_json()["errors"])


def test_config_post_saves_valid_window(client, monkeypatch):
    import database.open15_breakout_db as db

    saved = {}

    def fake_save(*args, **kwargs):
        saved.update(kwargs)
        return True

    monkeypatch.setattr(db, "get_config", lambda: None)
    monkeypatch.setattr(db, "save_config", fake_save)
    r = client.post(
        "/open15_vol_breakout/api/config",
        json={"no_entry_after": "09:45", "exit_time": "10:00"},
    )
    assert r.status_code == 200
    assert saved["no_entry_after"] == "09:45"
    assert saved["exit_time"] == "10:00"
