"""Feed-health indicator + clock-based selection finalize (issue #677).

On 2026-08-25 the tick feed was dead from open (#673) and open15 silently
skipped the day: selection only finalizes inside the tick handler, so zero
ticks meant no seeds, no alert, and a /logs page indistinguishable from a
quiet market. These tests pin the fix:

  - the scheduler's minute job finalizes selection at/after 09:17 with ZERO
    ticks (selection event ``source='scheduler'``), journals a ``feed_health``
    dead event, and Telegram-alerts once;
  - a normal ticking day is byte-identical to before: tick-path finalize
    (``source='tick'``), no feed_health event, no alert;
  - a mid-window feed recovery finds the watch list already armed — an entry
    can trigger on the first resumed tick;
  - state transitions journal exactly once per change (dead -> ok recovery);
  - the Python log-view twin tolerates the new event and the new selection
    key (the #615/#622 regression class).

Same construction pattern as test_open15_breakout_e2e.py: real service + real
core, only the order placer and the clock mocked; DB writes go to the
pytest-isolated temp DB.
"""

import datetime as dt
import json
from unittest.mock import MagicMock

import pytz

from services.open15_breakout_service import Open15BreakoutService, Open15Core, resolve_day_config

IST = pytz.timezone("Asia/Kolkata")


def _frame(symbol, price, cumvol, h, m, s):
    topic = f"NSE_{symbol}_LTP"
    payload = json.dumps(
        {
            "ltp": price,
            "volume": cumvol,
            "exchange_timestamp": dt.datetime(2026, 8, 26, h, m, s).timestamp(),
        }
    )
    return topic, payload


def _now(h, m, s=0):
    return IST.localize(dt.datetime(2026, 8, 26, h, m, s))


FIRST_CANDLES = {
    "AAA": {"open": 103.0, "high": 103.5, "low": 102.5},
    "CCC": {"open": 97.0, "high": 97.5, "low": 96.5},
    "ZZZ": {"open": 101.0, "high": 101.2, "low": 100.8},
}


def _mk_service(orders, clock=(9, 17, 30)):
    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = Open15BreakoutService(order_placer=placer)
    svc.universe = {"AAA", "CCC", "ZZZ"}
    svc.core = Open15Core({"AAA": 100.0, "CCC": 100.0, "ZZZ": 100.0}, vol_mult=1.5, top_n=1)
    svc.day_status = "armed"
    svc._log_date = "2026-08-26"
    svc.day_config = resolve_day_config(
        {"margin_per_slot": 30000, "sizing_mode": "fixed", "vol_mult": 1.5}, 0
    )
    svc._now_ist = lambda: _now(*clock)
    return svc


def _events(svc, name):
    return [e for e in svc.day_log if e.get("event") == name]


def _stub_notify(monkeypatch):
    notifier = MagicMock()
    monkeypatch.setattr("services.notification_service.get_notification_service", lambda: notifier)
    return notifier


def test_dead_feed_scheduler_finalizes_selection_and_alerts_once(monkeypatch):
    """THE 2026-08-25 shape: quotes landed at 09:16, zero ticks ever. The
    minute job must finalize from the snapshot, journal feed_health dead, and
    alert exactly once — the day explains itself instead of looking quiet."""
    from database.open15_breakout_db import init_db

    init_db()
    notifier = _stub_notify(monkeypatch)
    orders = []
    svc = _mk_service(orders)
    svc.core.apply_first_candles(FIRST_CANDLES)

    svc.check_feed_health()

    assert svc.core.finalized
    assert svc.core.selected == {"AAA": "L", "CCC": "S"}
    sel = _events(svc, "selection")
    assert len(sel) == 1 and sel[0]["source"] == "scheduler"
    fh = _events(svc, "feed_health")
    assert len(fh) == 1 and fh[0]["state"] == "dead" and fh[0]["prev"] is None
    assert fh[0]["ticks"] == 0 and fh[0]["symbols_ticking"] == 0
    assert notifier.notify.call_count == 1

    # second minute tick: still dead — no duplicate event, no duplicate alert
    svc.check_feed_health()
    assert len(_events(svc, "feed_health")) == 1
    assert len(_events(svc, "selection")) == 1
    assert notifier.notify.call_count == 1


def test_normal_ticking_day_is_unchanged(monkeypatch):
    """Ticks finalize selection as before (source=tick); the health check
    stays completely silent — no event, no alert, no extra selection."""
    from database.open15_breakout_db import init_db

    init_db()
    notifier = _stub_notify(monkeypatch)
    orders = []
    svc = _mk_service(orders)
    svc.core.apply_first_candles(FIRST_CANDLES)

    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("ZZZ", 101.0)):
        svc._handle_raw(*_frame(sym, px, 5000, 9, 16, 10), _now(9, 16, 10))
    assert svc.core.finalized

    sel = _events(svc, "selection")
    assert len(sel) == 1 and sel[0]["source"] == "tick"

    svc.check_feed_health()
    assert _events(svc, "feed_health") == []
    assert len(_events(svc, "selection")) == 1
    assert notifier.notify.call_count == 0


def test_recovery_arms_entries_and_journals_transition(monkeypatch):
    """Feed dead at 09:17 (scheduler finalizes), ticks resume at 09:20: the
    FIRST resumed surge tick can trigger an entry because the watch list was
    pre-armed, and the next health check journals the dead->ok recovery."""
    from database.open15_breakout_db import init_db

    init_db()
    _stub_notify(monkeypatch)
    orders = []
    svc = _mk_service(orders)
    svc.core.apply_first_candles(FIRST_CANDLES)
    svc.check_feed_health()
    assert svc.core.selected == {"AAA": "L", "CCC": "S"}

    # ticks resume: two quiet ticks build the baseline minute, then the surge
    # breaks the 09:15 candle high with >=1.5x the baseline minute volume.
    svc._handle_raw(*_frame("AAA", 103.2, 5500, 9, 18, 55), _now(9, 18, 55))
    svc._handle_raw(*_frame("AAA", 103.2, 6000, 9, 19, 55), _now(9, 19, 55))
    level = FIRST_CANDLES["AAA"]["high"]
    svc._handle_raw(*_frame("AAA", level + 0.5, 6000 + 9000, 9, 20, 12), _now(9, 20, 12))
    assert len(orders) == 1 and orders[0]["symbol"] == "AAA" and orders[0]["action"] == "BUY"

    svc._now_ist = lambda: _now(9, 21, 0)
    for sym in ("CCC", "ZZZ"):
        svc._handle_raw(*_frame(sym, 100.0, 7000, 9, 20, 30), _now(9, 20, 30))
    svc.check_feed_health()
    fh = _events(svc, "feed_health")
    assert [e["state"] for e in fh] == ["dead", "ok"]
    assert fh[-1]["prev"] == "dead" and fh[-1]["symbols_ticking"] == 3


def test_partial_feed_reports_degraded(monkeypatch):
    """Only 1 of 3 universe symbols ticking (< 50%) -> one degraded event."""
    from database.open15_breakout_db import init_db

    init_db()
    notifier = _stub_notify(monkeypatch)
    orders = []
    svc = _mk_service(orders)
    svc.core.apply_first_candles(FIRST_CANDLES)
    svc._handle_raw(*_frame("AAA", 103.0, 5000, 9, 16, 10), _now(9, 16, 10))

    svc.check_feed_health()
    fh = _events(svc, "feed_health")
    assert len(fh) == 1 and fh[0]["state"] == "degraded"
    assert fh[0]["symbols_ticking"] == 1 and fh[0]["universe"] == 3
    assert notifier.notify.call_count == 0  # only dead alerts


def test_status_carries_feed_health():
    orders = []
    svc = _mk_service(orders)
    svc.core.apply_first_candles(FIRST_CANDLES)
    s = svc.get_status()["feed_health"]
    assert s["state"] == "dead" and s["ticks"] == 0 and s["universe"] == 3
    svc._handle_raw(*_frame("AAA", 103.0, 5000, 9, 16, 10), _now(9, 16, 10))
    s = svc.get_status()["feed_health"]
    assert s["state"] == "degraded" and s["symbols_ticking"] == 1
    assert s["last_tick"] is not None


def test_pre_open_status_is_waiting_with_countdown():
    """issue #682: armed before 09:15 -> ``waiting`` with a server-computed
    countdown, never DEAD — the feed was never live to be dead, and red
    before an alarm is possible trains the operator to ignore red."""
    svc = _mk_service([], clock=(9, 12, 30))
    s = svc.get_status()["feed_health"]
    assert s["state"] == "waiting"
    assert s["opens_at"] == "09:15" and s["opens_in_s"] == 150
    # from 09:15:00 sharp the #677 derivation is unchanged
    svc._now_ist = lambda: _now(9, 15, 0)
    s = svc.get_status()["feed_health"]
    assert s["state"] == "dead" and "opens_in_s" not in s


def test_pre_open_minute_job_journals_and_alerts_nothing(monkeypatch):
    """issue #682: check_feed_health before the open — no transition, no
    journal, no Telegram, no scheduler finalize. Guarded in the function so
    the invariant survives a schedule edit, not just the job's 09:16 start."""
    from database.open15_breakout_db import init_db

    init_db()
    notifier = _stub_notify(monkeypatch)
    svc = _mk_service([], clock=(9, 13, 0))
    svc.core.apply_first_candles(FIRST_CANDLES)

    svc.check_feed_health()

    assert _events(svc, "feed_health") == []
    assert not svc.core.finalized
    assert notifier.notify.call_count == 0


def test_holiday_gate_skips_arm_and_persists_nothing(monkeypatch):
    """issue #682: the cron is plain mon-fri, so an NSE weekday holiday used
    to ARM and then read as a dead feed all day. The gate must set
    day_status='holiday' and create NO day log (the history rail stays clean,
    like a weekend)."""
    import database.open15_breakout_db as db

    db.init_db()
    # earlier tests in this file persist day logs for the same date — start
    # from a clean slate so "persists nothing" is actually assertable
    db.db_session.query(db.Open15DayLog).filter(db.Open15DayLog.trade_date == "2026-08-26").delete()
    db.db_session.commit()
    import services.data_freshness_service as dfs

    monkeypatch.setattr(dfs, "is_trading_day", lambda d, exchange=None: False)
    svc = Open15BreakoutService(order_placer=lambda mode, order: {"status": "success"})
    svc._now_ist = lambda: _now(9, 10, 0)

    svc.arm()

    assert svc.day_status == "holiday"
    assert svc.day_log == []
    assert not db.get_day_log("2026-08-26")


def test_holiday_gate_fails_open_to_arming(monkeypatch):
    """A raising calendar lookup must never dark a real trading day — the
    #253 fail-open discipline. Here the arm proceeds past the gate into its
    existing late-boot refusal, proving the gate did not block."""
    import database.open15_breakout_db as db

    db.init_db()
    import services.data_freshness_service as dfs

    def _boom(d, exchange=None):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(dfs, "is_trading_day", _boom)
    svc = Open15BreakoutService(order_placer=lambda mode, order: {"status": "success"})
    svc._now_ist = lambda: _now(9, 16, 0)  # past 09:15:30 -> late-boot path

    svc.arm()

    assert svc.day_status == "skipped_late_boot"


def test_effective_today_labels_holiday():
    """The #645 label-the-state rule: a holiday day must say so, never present
    itself as a generic 'not armed'."""
    import pytz as _pytz

    from blueprints.open15_breakout import _effective_today

    class _Svc:
        pass

    svc = _Svc()
    svc.day_status = "holiday"
    svc._log_date = dt.datetime.now(_pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    svc.day_config = {"instrument": "stock"}
    cfg, src = _effective_today(svc)
    assert src == "holiday" and cfg == {"instrument": "stock"}


def test_log_view_twin_tolerates_new_event_and_selection_key():
    """The #615/#622 regression class: the Python row builders must not go
    dark (or raise) on feed_health or on the selection event's source key."""
    from services.open15_log_view import selection_outcomes, summarize_day

    events = [
        {"ts": "09:10:00", "event": "armed", "universe": 3, "mode": "sandbox"},
        {
            "ts": "09:17:00",
            "event": "selection",
            "selected": {"AAA": "L"},
            "gaps_pct": {"AAA": 3.0},
            "prev_closes": {"AAA": 100.0},
            "candidates": 3,
            "source": "scheduler",
        },
        {
            "ts": "09:17:00",
            "event": "feed_health",
            "state": "dead",
            "prev": None,
            "ticks": 0,
            "symbols_ticking": 0,
            "universe": 3,
            "last_tick": None,
            "selection_source": "scheduler",
        },
        {"ts": "09:25:00", "event": "feed_health", "state": "ok", "prev": "dead"},
    ]
    digest = summarize_day("2026-08-26", events)
    assert digest["selected"] == 1
    rows = selection_outcomes("2026-08-26", events, [])
    assert [r["symbol"] for r in rows] == ["AAA"]
