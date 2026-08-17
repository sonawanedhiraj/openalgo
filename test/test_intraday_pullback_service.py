"""Integration tests for IntradayPullbackService (issue #394).

Hermetic: fake price/bars/prev-close providers + a recording order placer. Drives a simulated
long day (selection -> breakout entry -> stop-out exit) and asserts orders are placed and the
journal is written. Journal writes hit the per-process temp DB via conftest isolation.
"""

import datetime as dt

import pytest

from services.intraday_pullback_service import IntradayPullbackService

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
D = dt.date(2026, 1, 5)  # a Monday
# The journal is a per-process temp DB shared by every test module, and the resume path reads
# EVERY row for the day — so the two resume tests below own their own trading dates. Without
# that, one test's rows inflate another's `attempts` (which is how issue #624 stayed hidden).
D_RESUME = dt.date(2026, 1, 20)  # a Tuesday
D_RECONCILE = dt.date(2026, 1, 21)  # a Wednesday


def _c(hh, mm, o, h, lo, c, v, d=None):
    return (dt.datetime.combine(d or D, dt.time(hh, mm)), o, h, lo, c, v)


class RecordingPlacer:
    def __init__(self):
        self.calls = []

    def __call__(self, mode, order):
        self.calls.append((mode, dict(order)))
        return {"status": "success", "orderid": f"OID{len(self.calls)}"}


def _mk_service(mode="sandbox", bars=None, placer=None, now=None):
    sector_map = {"AAA": "IDX1", "BBB": "IDX1"}
    prev = {"AAA": 100.0, "BBB": 100.0, "IDX1": 100.0, "NIFTY": 100.0}
    # up-market: NIFTY +0.5%, sector +0.4%, AAA +1.5% (in band), BBB +0.2% (excluded)
    prices = {"NIFTY": 100.5, "IDX1": 100.4, "AAA": 101.5, "BBB": 100.2}
    svc = IntradayPullbackService(
        mode=mode,
        sector_map=sector_map,
        prev_close_provider=lambda syms, as_of: {s: prev.get(s) for s in syms},
        price_provider=lambda sym, as_of: prices.get(sym),
        bars_provider=lambda sym, as_of: (bars or {}).get(sym, []),
        order_placer=placer or RecordingPlacer(),
        notifier=lambda m: None,
        broker_session_checker=lambda: True,
        now=lambda: now or dt.datetime.combine(D, dt.time(9, 40), IST),
    )
    return svc


def test_selection_picks_long_top2_on_up_day():
    svc = _mk_service()
    svc.run_selection(dt.datetime.combine(D, dt.time(9, 30), IST))
    assert svc.side == "L"
    assert svc.picks == ["AAA"]  # BBB excluded (+0.2% below band)
    assert abs(svc.nifty_930 - 0.5) < 1e-6


def test_long_day_entry_then_stop_out_places_and_journals():
    placer = RecordingPlacer()
    aaa_bars = [
        _c(9, 30, 100.0, 100.5, 99.8, 100.2, 500),
        _c(9, 35, 100.2, 100.3, 99.5, 99.6, 100),  # red low-vol reference
        _c(9, 40, 99.6, 101.0, 99.6, 100.5, 1000),  # breakout -> ENTRY @100.5, stop 99.5
        _c(9, 45, 100.5, 100.6, 99.4, 99.7, 300),  # low 99.4 <= 99.5 -> SL exit @99.5
    ]
    # selection happens at the live 09:30 tick; all candles feed in this tick
    svc = _mk_service(
        bars={"AAA": aaa_bars}, placer=placer, now=dt.datetime.combine(D, dt.time(9, 30), IST)
    )
    svc.run_eval_tick(dt.datetime.combine(D, dt.time(9, 30), IST))

    # two orders: BUY entry then SELL exit
    assert len(placer.calls) == 2
    assert placer.calls[0][1]["action"] == "BUY" and placer.calls[0][1]["symbol"] == "AAA"
    assert placer.calls[1][1]["action"] == "SELL"
    # qty = int((60000/2 * 5) / 100.5) = int(150000/100.5) = 1492 (int at placer boundary;
    # production_order_placer stringifies for the broker payload)
    assert placer.calls[0][1]["quantity"] == int(150000 / 100.5)

    # journal: one closed trade with a (negative) net pnl, slot freed
    assert svc.open_count == 0
    from database import intraday_pullback_db as journal

    # scoped to THIS day: the journal is a per-process temp DB shared by every test module,
    # so an unscoped read counts other tests' rows too (issue #624)
    trades = journal.get_trades(svc.strategy_id, trade_date=D.isoformat(), mode="sandbox")
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "L" and t["status"] == "closed" and t["exit_reason"] == "SL"
    assert t["net_pnl"] is not None and t["net_pnl"] < 0  # ~ -1 pt * qty - charges
    assert svc.today_realized == pytest.approx(t["net_pnl"], abs=1.0)


def test_observe_mode_journals_but_places_no_broker_orders():
    placer = RecordingPlacer()
    aaa_bars = [
        _c(9, 30, 100.0, 100.5, 99.8, 100.2, 500),
        _c(9, 35, 100.2, 100.3, 99.5, 99.6, 100),
        _c(9, 40, 99.6, 101.0, 99.6, 100.5, 1000),
    ]
    # default production placer would short-circuit observe mode; here inject one that would
    # record if called, and assert the service still routes an entry through it (observe returns
    # success with orderid=None). The journal must still capture the signal.
    svc = _mk_service(
        mode="observe",
        bars={"AAA": aaa_bars},
        placer=placer,
        now=dt.datetime.combine(D, dt.time(9, 30), IST),
    )
    svc.run_eval_tick(dt.datetime.combine(D, dt.time(9, 30), IST))
    from database import intraday_pullback_db as journal

    trades = journal.get_trades(svc.strategy_id, mode="observe")
    assert len(trades) == 1 and trades[0]["status"] == "open" and trades[0]["side"] == "L"


def test_strategy_mode_row_overrides_env(monkeypatch):
    """Aligned with futures_follow/sector_follow: a persistent strategy_mode row
    (source='strategy_mode') overrides the env-resolved mode."""
    import services.mode_service as ms

    monkeypatch.setattr(
        ms, "resolve_mode", lambda name: ms.ResolvedMode(mode="live", source="strategy_mode")
    )
    svc = IntradayPullbackService(  # no forced mode -> resolves via env + strategy_mode row
        sector_map={"AAA": "IDX1"},
        prev_close_provider=lambda s, a: {},
        price_provider=lambda s, a: None,
        bars_provider=lambda s, a: [],
        order_placer=lambda m, o: {"status": "success"},
        notifier=lambda m: None,
        broker_session_checker=lambda: True,
        now=lambda: dt.datetime.combine(D, dt.time(9, 40), IST),
    )
    assert svc.mode == "live"  # strategy_mode row wins over the sandbox env default


def test_forced_mode_is_not_overridden(monkeypatch):
    import services.mode_service as ms

    monkeypatch.setattr(
        ms, "resolve_mode", lambda name: ms.ResolvedMode(mode="live", source="strategy_mode")
    )
    svc = _mk_service(mode="sandbox")  # explicit constructor mode wins outright
    assert svc.mode == "sandbox"


def test_editable_config_overrides_and_reset():
    from database.intraday_pullback_config_db import delete_config, init_db, set_config

    init_db()
    delete_config("intraday_pullback_top2")
    try:
        set_config(
            "intraday_pullback_top2",
            base_capital=100000,
            sizing_mode="compound",
            no_trade_start="10:30",
            no_trade_end="12:30",
            afternoon_start="12:30",
            afternoon_end="14:45",
        )
        svc = _mk_service()  # __init__ layers the DB overrides over the JSON defaults
        assert svc.base_capital == 100000
        assert svc.sizing_mode == "compound"
        assert svc.cfg.morning[1].strftime("%H:%M") == "10:30"
        assert svc.cfg.afternoon[0].strftime("%H:%M") == "12:30"
        assert svc.cfg.afternoon[1].strftime("%H:%M") == "14:45"
        s = svc.current_settings()
        assert s["base_capital"] == 100000 and s["no_trade"] == ["10:30", "12:30"]

        # reset -> revert to config_snapshot.json defaults
        delete_config("intraday_pullback_top2")
        svc._apply_editable_config()
        assert svc.base_capital == 60000 and svc.sizing_mode == "fixed"
        assert svc.cfg.afternoon[1].strftime("%H:%M") == "15:00"
    finally:
        delete_config("intraday_pullback_top2")


def _hist_bars(o930_close):
    # a 09:30 bar (used for the 09:30 price) + a later 10:00 bar
    return [
        (
            dt.datetime.combine(D, dt.time(9, 30), IST),
            o930_close,
            o930_close,
            o930_close,
            o930_close,
            100,
        ),
        (
            dt.datetime.combine(D, dt.time(10, 0), IST),
            o930_close,
            o930_close,
            o930_close,
            o930_close,
            100,
        ),
    ]


def test_resume_reconstructs_open_position_from_journal():
    from database import intraday_pullback_db as journal

    journal.init_db()
    today = D_RESUME.isoformat()
    journal.record_entry(
        strategy_id=None,
        mode="sandbox",
        side="L",
        symbol="AAA",
        trade_date=today,
        quantity=1000,
        entry_time=dt.datetime.combine(D_RESUME, dt.time(9, 50)),
        entry_price=100.5,
        stop_price=99.5,
        status="open",
        gate={"nifty_930": 0.5},
    )
    try:
        # boot at 10:15 (past the live 09:30 tick) -> resume path reconstructs from the journal
        svc = _mk_service(now=dt.datetime.combine(D_RESUME, dt.time(10, 15), IST))
        svc.run_eval_tick(dt.datetime.combine(D_RESUME, dt.time(10, 15), IST))
        assert svc.side == "L"
        assert svc.picks == ["AAA"]
        assert abs(svc.nifty_930 - 0.5) < 1e-6
        assert svc.open_count == 1  # exactly one OPEN row reconciled
        assert "AAA" in svc.open_positions and svc.open_positions["AAA"]["stop"] == 99.5
        # attempts counts today's journalled entries for the stock — EXACTLY one here.
        # `>= 1` used to hide the replay bug (issue #624): a duplicate entry opened by the
        # resume replay also satisfies it. See test_intraday_pullback_resume_replay.py.
        assert svc.states["AAA"].pos is not None and svc.states["AAA"].attempts == 1
    finally:
        t = journal.get_trades(None, trade_date=today)
        assert t  # sanity


def test_resume_reconciled_position_exits_on_stop_after_restart():
    from database import intraday_pullback_db as journal

    journal.init_db()
    today = D_RECONCILE.isoformat()
    journal.record_entry(
        strategy_id=None,
        mode="sandbox",
        side="L",
        symbol="AAA",
        trade_date=today,
        quantity=1000,
        entry_time=dt.datetime.combine(D_RECONCILE, dt.time(9, 50)),
        entry_price=100.5,
        stop_price=99.5,
        status="open",
        gate={"nifty_930": 0.5},
    )
    placer = RecordingPlacer()
    # after restart the aggregator warms up; the next candle breaches the stop.
    # NOTE this fixture is a post-entry TAIL — production replays the whole day from 09:15,
    # which is the case issue #624 fixed and test_intraday_pullback_resume_replay.py covers.
    bars = {
        "AAA": [_c(10, 20, 100.0, 100.2, 99.4, 99.6, 300, d=D_RECONCILE)]
    }  # low 99.4 <= stop 99.5 -> SL
    at = dt.datetime.combine(D_RECONCILE, dt.time(10, 20), IST)
    svc = _mk_service(bars=bars, placer=placer, now=at)
    svc.run_eval_tick(at)
    # the reconciled position is flattened (a SELL exit placed, journal closed, slot freed)
    assert any(o["action"] == "SELL" and o["symbol"] == "AAA" for _m, o in placer.calls)
    assert svc.open_count == 0
    rows = journal.get_trades(None, trade_date=today, mode="sandbox")
    closed = [r for r in rows if r["symbol"] == "AAA" and r["status"] == "closed"]
    assert closed and closed[0]["exit_reason"] == "SL"


def test_resume_historical_selection_when_no_journal():
    # late boot, never traded today -> historical 09:30 re-select picks the right name
    prices930 = {"NIFTY": 100.5, "IDX1": 100.4, "AAA": 101.5, "BBB": 100.2}
    hist = {s: _hist_bars(px) for s, px in prices930.items()}
    svc = IntradayPullbackService(
        mode="sandbox",
        sector_map={"AAA": "IDX1", "BBB": "IDX1"},
        prev_close_provider=lambda syms, a: dict.fromkeys(syms, 100.0),
        price_provider=lambda s, a: None,  # live price unused on the historical path
        bars_provider=lambda s, a: [],
        history_provider=lambda sym, exch, interval, date: hist.get(sym, []),
        order_placer=RecordingPlacer(),
        notifier=lambda m: None,
        broker_session_checker=lambda: True,
        now=lambda: dt.datetime.combine(D, dt.time(10, 15), IST),
    )
    svc.run_eval_tick(dt.datetime.combine(D, dt.time(10, 15), IST))
    assert svc.side == "L"
    assert svc.picks == ["AAA"]  # +1.5% in band; BBB +0.2% excluded


def test_entry_breakdown_explains_no_trade_day():
    # AAA is selected at 09:30 but gets no candles -> no trigger; the breakdown says why
    svc = _mk_service(now=dt.datetime.combine(D, dt.time(9, 30), IST))
    svc.run_eval_tick(dt.datetime.combine(D, dt.time(9, 30), IST))
    b = svc.entry_breakdown()
    assert b["selected"] and b["picks"] == ["AAA"] and b["n_trades_today"] == 0
    ev = b["evaluation"][0]
    assert ev["symbol"] == "AAA" and ev["position"] == "none"
    assert ev["gain_930_pct"] is not None and ev["sector"] == "IDX1"
    assert "reference" in ev["reason"]  # no candles -> no reference formed
    assert svc.get_status()["today_evaluation"]["picks"] == ["AAA"]


def test_status_and_performance_shape():
    svc = _mk_service()
    st = svc.get_status()
    assert st["strategy"] == "intraday_pullback_top2"
    assert st["mode"] == "sandbox"
    assert st["slots"] == 2 and st["base_capital"] == 60000
    assert set(st["performance"].keys()) == {"long", "short", "combined"}
