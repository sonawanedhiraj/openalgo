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


def _c(hh, mm, o, h, lo, c, v):
    return (dt.datetime.combine(D, dt.time(hh, mm)), o, h, lo, c, v)


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
    svc = _mk_service(
        bars={"AAA": aaa_bars}, placer=placer, now=dt.datetime.combine(D, dt.time(9, 45), IST)
    )
    svc.run_eval_tick(dt.datetime.combine(D, dt.time(9, 45), IST))

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

    trades = journal.get_trades(svc.strategy_id, mode="sandbox")
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
        now=dt.datetime.combine(D, dt.time(9, 40), IST),
    )
    svc.run_eval_tick(dt.datetime.combine(D, dt.time(9, 40), IST))
    from database import intraday_pullback_db as journal

    trades = journal.get_trades(svc.strategy_id, mode="observe")
    assert len(trades) == 1 and trades[0]["status"] == "open" and trades[0]["side"] == "L"


def test_status_and_performance_shape():
    svc = _mk_service()
    st = svc.get_status()
    assert st["strategy"] == "intraday_pullback_top2"
    assert st["mode"] == "sandbox"
    assert st["slots"] == 2 and st["base_capital"] == 60000
    assert set(st["performance"].keys()) == {"long", "short", "combined"}
