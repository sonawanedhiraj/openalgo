"""Tick-capture coverage for open15_vol_breakout (issue #528).

Pre-#528 the service persisted ticks for the day's 3 SELECTED symbols only, so
the strategy's own 09:15-09:30 entry window was un-backtestable for any question
involving other symbols (e.g. adding intraday top gainers to the watch list).
These tests pin the universe-wide capture, the legacy targeted mode behind
``OPEN15_TICK_CAPTURE_UNIVERSE=false``, and the fail-graceful contract: a broken
writer is instrumentation failure, never trade failure.
"""

import datetime as dt
import json

from services.open15_breakout_service import (
    Open15BreakoutService,
    Open15Core,
    _tick_capture_universe_enabled,
    resolve_day_config,
)


class FakeWriter:
    """Stand-in for TickLogWriter recording what would hit disk."""

    def __init__(self, boom: bool = False):
        self.records: list[tuple] = []
        self.boom = boom

    def enqueue(self, symbol, price, volume, ts=None):
        if self.boom:
            raise RuntimeError("disk full")
        self.records.append((symbol, price, volume, ts))

    def symbols(self) -> set[str]:
        return {r[0] for r in self.records}


def _frame(symbol, price, cumvol, h, m, s):
    topic = f"NSE_{symbol}_LTP"
    payload = json.dumps(
        {
            "ltp": price,
            "volume": cumvol,
            "exchange_timestamp": dt.datetime(2026, 7, 21, h, m, s).timestamp(),
        }
    )
    return topic, payload


def _now(h, m, s=0):
    import pytz

    return pytz.timezone("Asia/Kolkata").localize(dt.datetime(2026, 7, 21, h, m, s))


def _mk_service(orders, *, universe_capture: bool, writer=None):
    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = Open15BreakoutService(order_placer=placer)
    svc.universe = {"AAA", "CCC", "ZZZ"}
    svc.core = Open15Core({"AAA": 100.0, "CCC": 100.0, "ZZZ": 100.0}, vol_mult=1.5, top_n=1)
    svc.day_status = "armed"
    svc._log_date = "2026-07-21"
    svc.day_config = resolve_day_config(
        {"margin_per_slot": 30000, "sizing_mode": "fixed", "vol_mult": 1.5}, 0
    )
    svc._tick_writer = writer if writer is not None else FakeWriter()
    svc._capture_universe = universe_capture
    return svc


def _drive_open(svc):
    """09:15 first candles for all three, then the 09:16 tick that selects."""
    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("ZZZ", 101.0)):
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))


def test_universe_capture_persists_every_symbol_across_the_window():
    """#528: unselected symbols and the pre-selection 09:15 minute both land."""
    orders = []
    svc = _mk_service(orders, universe_capture=True)
    w = svc._tick_writer

    _drive_open(svc)
    assert svc.core.selected == {"AAA": "L", "CCC": "S"}
    # ZZZ was never selected — pre-#528 its ticks were dropped at finalization
    assert w.symbols() == {"AAA", "CCC", "ZZZ"}
    assert sum(1 for r in w.records if r[0] == "ZZZ") == 2  # both 09:15 ticks kept

    # ... and it keeps streaming for the rest of the window
    svc._handle_raw(*_frame("ZZZ", 104.0, 20000, 9, 22, 30), _now(9, 22, 30))
    svc._handle_raw(*_frame("ZZZ", 104.5, 21000, 9, 28, 59), _now(9, 28, 59))
    zzz = [r for r in w.records if r[0] == "ZZZ"]
    assert len(zzz) == 4
    assert zzz[0][3].strftime("%H:%M:%S") == "09:15:01"
    assert zzz[-1][3].strftime("%H:%M:%S") == "09:28:59"

    # the selection-finalizing tick is written exactly once, not duplicated
    assert sum(1 for r in w.records if r[0] == "AAA") == 3


def test_universe_capture_still_logs_selection_and_places_entries():
    """The capture change must not perturb the decision log or the entry path."""
    orders = []
    svc = _mk_service(orders, universe_capture=True)
    _drive_open(svc)

    sel = [e for e in svc.day_log if e["event"] == "selection"]
    assert len(sel) == 1 and sel[0]["selected"] == {"AAA": "L", "CCC": "S"}

    h1 = svc.core.sym["AAA"]["fc"]["high"]
    svc._handle_raw(*_frame("AAA", h1 + 0.5, 6000 + 9000, 9, 17, 12), _now(9, 17, 12))
    assert len(orders) == 1 and orders[0]["symbol"] == "AAA"


def test_targeted_mode_keeps_pre_528_behaviour():
    """OPEN15_TICK_CAPTURE_UNIVERSE=false -> selected symbols only, buffered."""
    orders = []
    svc = _mk_service(orders, universe_capture=False)
    w = svc._tick_writer

    _drive_open(svc)
    assert w.symbols() == {"AAA", "CCC"}  # ZZZ's buffered ticks dropped
    assert svc._first_min_buffer == {}

    svc._handle_raw(*_frame("ZZZ", 104.0, 20000, 9, 22, 30), _now(9, 22, 30))
    assert w.symbols() == {"AAA", "CCC"}


def test_writer_failure_never_costs_an_entry():
    """Capture is instrumentation: a raising writer must not block the order."""
    orders = []
    svc = _mk_service(orders, universe_capture=True, writer=FakeWriter(boom=True))

    _drive_open(svc)
    assert svc.core.selected == {"AAA": "L", "CCC": "S"}
    h1 = svc.core.sym["AAA"]["fc"]["high"]
    svc._handle_raw(*_frame("AAA", h1 + 0.5, 6000 + 9000, 9, 17, 12), _now(9, 17, 12))
    assert len(orders) == 1 and orders[0]["action"] == "BUY"


def test_universe_flag_defaults_on_and_is_switchable(monkeypatch):
    monkeypatch.delenv("OPEN15_TICK_CAPTURE_UNIVERSE", raising=False)
    assert _tick_capture_universe_enabled() is True
    monkeypatch.setenv("OPEN15_TICK_CAPTURE_UNIVERSE", "false")
    assert _tick_capture_universe_enabled() is False
    monkeypatch.setenv("OPEN15_TICK_CAPTURE_UNIVERSE", "TRUE")
    assert _tick_capture_universe_enabled() is True
