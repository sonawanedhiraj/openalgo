"""Resume-replay safety for intraday_pullback_top2 (issue #624).

On a mid-session restart the service re-feeds the day's candles from 09:15 to rebuild the
state machine's volume baseline and reference candle. Before #624 that replay also RE-DECIDED
the day: the first replayed candle stopped out a position opened hours later (a phantom ``SL``
stamped 09:15, priced at the stop) and the replay then re-reached the original breakout and
opened a duplicate position at a stale price — twice on 2026-08-17, on real orders.

These tests drive the incident shape with a FULL-DAY bar series (09:15 -> now), which is what
production's ``ScannerService.get_today_bars`` returns. The pre-#624 tests supplied only a
post-entry tail, which is why they never saw it.

Each test owns its own trade_date. The journal is a per-process temp DB shared by the whole
SESSION (conftest isolation is per-process, not per-file) and ``_reconstruct_from_journal``
reads EVERY row for the day — so a date shared with another test, in this file or any other,
lets one test's rows decide another's side and trade count. The other intraday_pullback test
modules all use the 2026-01-05 week; this one deliberately uses the next one.
"""

import datetime as dt

from services.intraday_pullback_service import IntradayPullbackService

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
# distinct weekdays (2026-01-12 is a Monday) so no test — here or elsewhere — sees another's rows.
# They must also be TRADING days: run_eval_tick returns silently on a holiday, which would make
# every assertion below vacuously true. 2026-01-15 is an NSE holiday, hence the gap.
D_STOP, D_LATER, D_REENTRY, D_LATEBOOT, D_STALE = (
    dt.date(2026, 1, n) for n in (12, 13, 14, 16, 19)
)


def test_fixture_dates_are_trading_days():
    """Guards every other test in this module against silently skipping the day."""
    from services.intraday_pullback_service import _is_trading_day

    for d in (D_STOP, D_LATER, D_REENTRY, D_LATEBOOT, D_STALE):
        assert _is_trading_day(d), f"{d} is not an NSE trading day — pick another fixture date"


def _c(d, hh, mm, o, h, lo, c, v):
    """A closed 5m candle with a NAIVE IST timestamp (what get_today_bars returns)."""
    return (dt.datetime.combine(d, dt.time(hh, mm)), o, h, lo, c, v)


class RecordingPlacer:
    def __init__(self):
        self.calls = []

    def __call__(self, mode, order):
        self.calls.append((mode, dict(order)))
        return {"status": "success", "orderid": f"OID{len(self.calls)}"}


def _mk_service(bars=None, placer=None, now=None, prices=None):
    prev = {"AAA": 100.0, "BBB": 100.0, "IDX1": 100.0, "NIFTY": 100.0}
    # default: up-market so the LONG gates pass
    prices = prices or {"NIFTY": 100.5, "IDX1": 100.4, "AAA": 101.5, "BBB": 100.2}
    return IntradayPullbackService(
        mode="sandbox",
        sector_map={"AAA": "IDX1", "BBB": "IDX1"},
        prev_close_provider=lambda syms, as_of: {s: prev.get(s) for s in syms},
        price_provider=lambda sym, as_of: prices.get(sym),
        bars_provider=lambda sym, as_of: list((bars or {}).get(sym, [])),
        order_placer=placer or RecordingPlacer(),
        notifier=lambda m: None,
        broker_session_checker=lambda: True,
        now=lambda: now,
    )


def _full_day_long_bars(d):
    """09:15 -> 10:20, with the 09:45 breakout that produced the 09:50 journal entry.

    The 09:15 candle's low (98.0) is far below the position's 99.5 stop: pre-#624 this is the
    candle that fired the phantom SL. No candle after the 09:50 entry breaches it.
    """
    return [
        _c(d, 9, 15, 100.0, 100.4, 98.0, 99.9, 800),  # deep low — would breach a 99.5 stop
        _c(d, 9, 20, 99.9, 100.2, 99.7, 100.1, 400),
        _c(d, 9, 40, 100.1, 100.3, 99.5, 99.6, 100),  # red low-vol reference
        _c(d, 9, 45, 99.6, 101.0, 99.6, 100.5, 1000),  # the breakout that entered at 09:50
        _c(d, 10, 20, 100.5, 100.9, 100.2, 100.7, 300),  # after the entry, no breach
    ]


def _open_journal_row(d):
    from database import intraday_pullback_db as journal

    journal.init_db()
    return journal.record_entry(
        strategy_id=None,
        mode="sandbox",
        side="L",
        symbol="AAA",
        trade_date=d.isoformat(),
        quantity=1000,
        entry_time=dt.datetime.combine(d, dt.time(9, 50)),
        entry_price=100.5,
        stop_price=99.5,
        status="open",
        gate={"nifty_930": 0.5},
    )


def _rows(d, symbol="AAA"):
    from database import intraday_pullback_db as journal

    return [
        r
        for r in journal.get_trades(None, trade_date=d.isoformat(), mode="sandbox")
        if r["symbol"] == symbol
    ]


def test_replay_never_stops_out_a_position_on_pre_entry_candles():
    """The 2026-08-17 incident: restart -> phantom SL @09:15 + duplicate entry."""
    _open_journal_row(D_STOP)
    placer = RecordingPlacer()
    restart_at = dt.datetime.combine(D_STOP, dt.time(10, 25), IST)
    svc = _mk_service(bars={"AAA": _full_day_long_bars(D_STOP)}, placer=placer, now=restart_at)
    svc.run_eval_tick(restart_at)

    # NOTHING is placed: the position is untouched and no duplicate is opened.
    assert placer.calls == []
    assert svc.open_count == 1 and "AAA" in svc.open_positions
    st = svc.states["AAA"]
    assert st.pos is not None
    assert st.attempts == 1  # exactly the one real entry, not the replayed duplicate

    rows = _rows(D_STOP)
    assert len(rows) == 1 and rows[0]["status"] == "open"
    assert rows[0]["exit_time"] is None and rows[0]["exit_reason"] is None

    # ...and the replay DID run — this is what stops the test passing vacuously (e.g. if a
    # future change simply skipped the bars instead of muting the decisions). Four candles
    # sit at/before the 09:50 floor; the 10:20 one is live and takes the normal path.
    assert st.diag["replayed"] == 4
    assert len(st.prior_vols) == 5  # volume baseline rebuilt from the whole day


def test_replay_warmup_does_not_disarm_a_genuine_later_stop():
    """The floor mutes history, not the future: the next real candle still stops out."""
    _open_journal_row(D_LATER)
    placer = RecordingPlacer()
    day = _full_day_long_bars(D_LATER)
    restart_at = dt.datetime.combine(D_LATER, dt.time(10, 25), IST)
    svc = _mk_service(bars={"AAA": day}, placer=placer, now=restart_at)
    svc.run_eval_tick(restart_at)
    assert placer.calls == []  # warm-up tick placed nothing

    # a new candle closes below the stop
    day.append(_c(D_LATER, 10, 25, 100.7, 100.8, 99.4, 99.5, 600))
    tick_at = dt.datetime.combine(D_LATER, dt.time(10, 30), IST)
    svc._now = lambda: tick_at
    svc.run_eval_tick(tick_at)

    assert any(o["action"] == "SELL" and o["symbol"] == "AAA" for _m, o in placer.calls)
    assert svc.open_count == 0
    closed = [r for r in _rows(D_LATER) if r["status"] == "closed"]
    assert len(closed) == 1 and closed[0]["exit_reason"] == "SL"
    # the invariant the incident violated
    assert closed[0]["exit_time"] > closed[0]["entry_time"]


def test_replay_does_not_re_enter_a_breakout_already_closed_today():
    """A stock stopped out earlier must not re-enter on its own replayed breakout candle.

    Uses the SHORT book deliberately: ``short.noreentry_after_sl`` is false, so nothing else
    would stop the duplicate. (On the long book ``done`` masks it.)
    """
    from database import intraday_pullback_db as journal

    journal.init_db()
    tid = journal.record_entry(
        strategy_id=None,
        mode="sandbox",
        side="S",
        symbol="AAA",
        trade_date=D_REENTRY.isoformat(),
        quantity=1000,
        entry_time=dt.datetime.combine(D_REENTRY, dt.time(9, 50)),
        entry_price=99.5,
        stop_price=100.5,
        status="open",
        gate={"nifty_930": -0.5},
    )
    journal.close_trade(
        tid,
        exit_time=dt.datetime.combine(D_REENTRY, dt.time(10, 0)),
        exit_price=100.5,
        exit_reason="SL",
        gross_pnl=-1000.0,
        charges_inr=100.0,
        net_pnl=-1100.0,
        exit_order_id="X1",
        status="closed",
    )

    # down market so the SHORT gates would pass if an entry were attempted
    prices = {"NIFTY": 99.5, "IDX1": 99.6, "AAA": 98.5, "BBB": 99.8}
    # volumes are load-bearing: 09:45 must clear the 2.5x-of-the-last-6 gate (1000 >= 2.5*150)
    # so that WITHOUT the replay floor this really does re-enter. It must not be a fixture that
    # simply never triggers.
    short_day = [
        _c(D_REENTRY, 9, 15, 100.0, 102.0, 99.8, 100.1, 200),  # high 102 breaches a 100.5 stop
        _c(D_REENTRY, 9, 40, 100.1, 100.5, 99.9, 100.3, 100),  # green low-vol reference (short)
        _c(D_REENTRY, 9, 45, 100.3, 100.4, 99.0, 99.5, 1000),  # the breakdown that entered 09:50
        _c(D_REENTRY, 10, 20, 99.0, 99.2, 98.6, 98.8, 300),
    ]
    placer = RecordingPlacer()
    restart_at = dt.datetime.combine(D_REENTRY, dt.time(10, 25), IST)
    svc = _mk_service(bars={"AAA": short_day}, placer=placer, now=restart_at, prices=prices)
    svc.run_eval_tick(restart_at)

    assert svc.side == "S"
    assert placer.calls == []  # no re-entry on the replayed 09:45 breakdown
    assert svc.open_count == 0
    assert svc.states["AAA"].attempts == 1
    assert len(_rows(D_REENTRY)) == 1  # still just the one closed trade


def test_late_boot_historical_selection_does_not_enter_a_passed_breakout():
    """A breakout that fired before boot is history — entering it now uses an hours-old price."""

    def _hist(px):
        t = dt.datetime.combine(D_LATEBOOT, dt.time(9, 30), IST)
        return [(t, px, px, px, px, 100)]

    prices930 = {"NIFTY": 100.5, "IDX1": 100.4, "AAA": 101.5, "BBB": 100.2}
    hist = {s: _hist(px) for s, px in prices930.items()}
    day = [
        _c(D_LATEBOOT, 9, 40, 100.1, 100.3, 99.5, 99.6, 100),  # reference
        _c(D_LATEBOOT, 9, 45, 99.6, 101.0, 99.6, 100.5, 1000),  # breakout — gone by 13:05
    ]
    placer = RecordingPlacer()
    boot_at = dt.datetime.combine(D_LATEBOOT, dt.time(13, 5), IST)
    svc = IntradayPullbackService(
        mode="sandbox",
        sector_map={"AAA": "IDX1", "BBB": "IDX1"},
        prev_close_provider=lambda syms, a: dict.fromkeys(syms, 100.0),
        price_provider=lambda s, a: prices930.get(s),
        bars_provider=lambda s, a: list(day if s == "AAA" else []),
        history_provider=lambda sym, exch, interval, date: hist.get(sym, []),
        order_placer=placer,
        notifier=lambda m: None,
        broker_session_checker=lambda: True,
        now=lambda: boot_at,
    )
    svc.run_eval_tick(boot_at)

    assert svc.picks == ["AAA"]  # still selected and watched
    assert placer.calls == []  # but the passed breakout is not traded
    assert svc.open_count == 0
    assert svc.states["AAA"].diag["replayed"] == 2  # both candles were replayed as warm-up


def test_stale_entry_action_is_refused_and_stale_exit_is_repriced(monkeypatch):
    """Defense in depth: the wall-clock guard, independent of the replay floor."""
    import services.intraday_pullback_service as mod
    from services.intraday_pullback_core import EntryAction, ExitAction

    notes = []
    placer = RecordingPlacer()
    now = dt.datetime.combine(D_STALE, dt.time(11, 45), IST)
    svc = _mk_service(placer=placer, now=now)
    svc._notify = notes.append
    svc.picks = ["AAA"]

    # an entry carrying a 10:25 candle at 11:45 -> refused, no order, loud
    svc._place_entry(
        "AAA",
        "IDX1",
        EntryAction(
            ts=dt.datetime.combine(D_STALE, dt.time(10, 25)), price=100.5, stop=99.5, side="L"
        ),
    )
    assert placer.calls == []
    assert notes and "stale entry" in notes[0]

    # an exit is never blocked, but is re-priced to the live tick and stamped now
    svc.open_positions["AAA"] = {
        "trade_id": None,
        "entry_price": 100.0,
        "qty": 10,
        "stop": 99.5,
        "side": "L",
    }
    svc.open_count = 1
    recorded = {}
    monkeypatch.setattr(mod.journal, "close_trade", lambda tid, **kw: recorded.update(kw))
    svc._place_exit(
        "AAA",
        ExitAction(ts=dt.datetime.combine(D_STALE, dt.time(9, 15)), price=99.5, reason="SL"),
    )

    assert any(o["action"] == "SELL" for _m, o in placer.calls)  # the exit still goes out
    assert recorded["exit_price"] == 101.5  # live AAA price, not the stale stop
    assert mod._naive_ist(recorded["exit_time"]) == now.replace(tzinfo=None)
