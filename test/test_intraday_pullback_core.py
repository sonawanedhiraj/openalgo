"""Unit tests for the pure intraday_pullback evaluator (issue #394).

Feeds 5m candles into the per-stock state machine and asserts the backtest-faithful
entry/exit behavior: reference-candle -> breakout entry, stop-out, EOD flatten, gate and
nf_mom rejection, noreentry-after-SL, the short mirror, margin-slot gating, and selection.
"""

import datetime as dt

from services.intraday_pullback_core import (
    EntryAction,
    ExitAction,
    GateContext,
    PullbackConfig,
    StockState,
    select_top2,
)

CFG = PullbackConfig()
D = dt.date(2026, 1, 5)


def _c(hh, mm, o, h, lo, c, v):
    return (dt.datetime.combine(D, dt.time(hh, mm)), o, h, lo, c, v)


def _ctx(nf=0.5, sc=0.4, nf930=0.4, slot=True):
    return GateContext(
        nifty_ret_now=nf, sector_ret_now=sc, nifty_ret_930=nf930, slot_available=slot
    )


def _run(state, candles, ctx_fn):
    """Feed candles, collect (candle_index, action) pairs."""
    out = []
    for i, cd in enumerate(candles):
        for a in state.process_candle(cd, ctx_fn(i)):
            out.append((i, a))
    return out


def test_long_reference_then_breakout_entry():
    st = StockState("L", CFG)
    candles = [
        _c(9, 30, 100.0, 100.5, 99.8, 100.2, 500),  # green, not a ref
        _c(9, 35, 100.2, 100.3, 99.5, 99.6, 100),  # red + low vol -> reference
        _c(
            9, 40, 99.6, 101.0, 99.6, 100.5, 1000
        ),  # 1000 >= 2.5*avg(500,100)=750, close>ref.open -> ENTRY
    ]
    acts = _run(st, candles, lambda i: _ctx())
    assert len(acts) == 1
    i, a = acts[0]
    assert i == 2 and isinstance(a, EntryAction) and a.side == "L"
    assert a.price == 100.5
    # stop = entry - max(entry-ref.low, floor) = 100.5 - max(1.0, 0.3015) = 99.5
    assert abs(a.stop - 99.5) < 1e-9


def test_long_stop_out_then_noreentry_blocks():
    st = StockState("L", CFG)
    candles = [
        _c(9, 30, 100.0, 100.5, 99.8, 100.2, 500),
        _c(9, 35, 100.2, 100.3, 99.5, 99.6, 100),
        _c(9, 40, 99.6, 101.0, 99.6, 100.5, 1000),  # ENTRY @100.5 stop 99.5
        _c(9, 45, 100.5, 100.6, 99.4, 99.7, 300),  # low 99.4 <= 99.5 -> SL exit
        _c(9, 50, 99.7, 99.8, 99.0, 99.1, 50),  # red low-vol ref...
        _c(9, 55, 99.1, 101.0, 99.1, 100.9, 2000),  # ...breakout, but noreentry -> NO entry
    ]
    acts = _run(st, candles, lambda i: _ctx())
    kinds = [type(a).__name__ for _, a in acts]
    assert kinds == ["EntryAction", "ExitAction"]
    assert acts[1][1].reason == "SL" and acts[1][1].price == 99.5


def test_long_eod_flatten():
    cfg = PullbackConfig()
    st = StockState("L", cfg)
    candles = [
        _c(14, 45, 100.0, 100.5, 99.8, 100.2, 500),
        _c(14, 50, 100.2, 100.3, 99.5, 99.6, 100),
        _c(14, 55, 99.6, 101.0, 99.6, 100.5, 1000),  # ENTRY (afternoon window)
        _c(15, 15, 100.5, 100.9, 100.4, 100.7, 400),  # 15:15 -> EOD exit at close 100.7
    ]
    acts = _run(st, candles, lambda i: _ctx())
    assert [type(a).__name__ for _, a in acts] == ["EntryAction", "ExitAction"]
    assert acts[1][1].reason == "EOD" and acts[1][1].price == 100.7


def test_long_market_gate_rejects_entry():
    st = StockState("L", CFG)
    candles = [
        _c(9, 30, 100.0, 100.5, 99.8, 100.2, 500),
        _c(9, 35, 100.2, 100.3, 99.5, 99.6, 100),
        _c(9, 40, 99.6, 101.0, 99.6, 100.5, 1000),
    ]
    # nifty below +0.3% gate -> no entry
    acts = _run(st, candles, lambda i: _ctx(nf=0.1))
    assert acts == []


def test_long_nf_mom_rejects_when_nifty_below_930():
    st = StockState("L", CFG)
    candles = [
        _c(9, 30, 100.0, 100.5, 99.8, 100.2, 500),
        _c(9, 35, 100.2, 100.3, 99.5, 99.6, 100),
        _c(9, 40, 99.6, 101.0, 99.6, 100.5, 1000),
    ]
    # nifty_now 0.4 >= gate 0.3 but < nifty_930 0.9 -> nf_mom blocks
    acts = _run(st, candles, lambda i: _ctx(nf=0.4, nf930=0.9))
    assert acts == []


def test_short_mirror_breakdown_entry():
    st = StockState("S", CFG)
    candles = [
        _c(9, 30, 100.0, 100.2, 99.5, 99.8, 500),  # not a ref
        _c(9, 35, 99.8, 100.4, 99.8, 100.3, 100),  # green + low vol -> reference (short)
        _c(
            9, 40, 100.3, 100.3, 98.5, 99.0, 1000
        ),  # vol ok, close 99.0 < ref.open 99.8 -> SHORT entry
    ]
    # short gate: nifty <= -0.3, sector < 0
    acts = _run(st, candles, lambda i: _ctx(nf=-0.6, sc=-0.5, nf930=-0.5))
    assert len(acts) == 1 and isinstance(acts[0][1], EntryAction) and acts[0][1].side == "S"
    assert acts[0][1].price == 99.0
    # stop = entry + max(ref.high-entry, floor) = 99.0 + max(100.4-99.0, 0.297) = 100.4
    assert abs(acts[0][1].stop - 100.4) < 1e-9


def test_no_slot_defers_entry_then_enters_when_free():
    st = StockState("L", CFG)
    candles = [
        _c(9, 30, 100.0, 100.5, 99.8, 100.2, 500),
        _c(9, 35, 100.2, 100.3, 99.5, 99.6, 100),
        _c(9, 40, 99.6, 101.0, 99.6, 100.5, 1000),  # breakout but NO slot -> skip, ref retained
        _c(9, 45, 100.5, 101.5, 100.4, 101.2, 2000),  # another breakout, slot now free -> ENTRY
    ]

    def ctx(i):
        return _ctx(slot=(i >= 3))

    acts = _run(st, candles, ctx)
    assert len(acts) == 1 and acts[0][0] == 3 and isinstance(acts[0][1], EntryAction)


def test_diag_reason_no_reference():
    st = StockState("L", CFG)
    for cd in [
        _c(9, 30, 100.0, 101.0, 100.0, 100.8, 500),  # green — never a red low-vol ref (long)
        _c(9, 35, 100.8, 101.5, 100.8, 101.2, 600),
    ]:
        st.process_candle(cd, _ctx())
    assert st.diag["ref_formed"] == 0
    assert st.reason() == "no low-volume reference (no-supply pullback) candle formed"


def test_diag_reason_ref_but_no_breakout():
    st = StockState("L", CFG)
    for cd in [
        _c(9, 30, 100.0, 100.5, 99.8, 100.2, 500),
        _c(9, 35, 100.2, 100.3, 99.5, 99.6, 100),  # red low-vol reference
        _c(9, 40, 99.6, 100.1, 99.6, 100.0, 120),  # up but vol 120 < 2.5*avg -> not a breakout
    ]:
        st.process_candle(cd, _ctx())
    assert st.diag["ref_formed"] >= 1 and st.diag["breakouts"] == 0
    assert st.reason() == "reference formed but no >=2.5x-volume breakout candle followed"


def test_diag_reason_gate_blocked():
    st = StockState("L", CFG)
    for cd in [
        _c(9, 30, 100.0, 100.5, 99.8, 100.2, 500),
        _c(9, 35, 100.2, 100.3, 99.5, 99.6, 100),
        _c(9, 40, 99.6, 101.0, 99.6, 100.5, 1000),  # breakout, but gate blocks (nifty < +0.3%)
    ]:
        st.process_candle(cd, _ctx(nf=0.1))
    assert st.diag["breakouts"] == 1 and st.diag["gate_blocked"] == 1 and st.diag["entries"] == 0
    assert st.reason() == "breakout formed but the live NIFTY/sector gate blocked it"


def test_select_top2_long_and_short():
    sector_of = {"A": "S1", "B": "S1", "C": "S2", "D": "S2", "E": "S1"}
    # long: band [1.0,2.5), sector green
    long_ret = {"A": 2.4, "B": 1.2, "C": 2.9, "D": 0.5, "E": 1.8}
    sret_green = {"S1": 0.5, "S2": 0.5}
    picks = select_top2("L", long_ret, sector_of, sret_green, CFG)
    assert picks == ["A", "E"]  # C excluded (>=2.5), D excluded (<1.0); top-2 by gain desc

    # short: band (-5,-3], sector red
    short_ret = {"A": -3.2, "B": -4.8, "C": -2.0, "D": -5.5, "E": -3.9}
    sret_red = {"S1": -0.4, "S2": -0.4}
    picks_s = select_top2("S", short_ret, sector_of, sret_red, CFG)
    assert picks_s == ["B", "E"]  # C excluded (>-3), D excluded (<=-5); most-negative first
