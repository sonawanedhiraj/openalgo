"""End-to-end session simulation for open15_vol_breakout (issue #428 follow-up).

Drives the EXACT production pipeline — raw ZMQ frames -> _handle_raw (parse ->
normalize -> gates -> core -> capture -> order placement) -> journal ->
flatten -> summary -> persisted day log — with only two seams mocked: the
order placer (records calls, returns success) and the wall clock. DB writes go
to the pytest-isolated temp DB (test/conftest.py redirect).

This is the test class that would have caught the 2026-07-20 failure family:
anything broken in the wiring between components surfaces here, not in market.
"""

import datetime as dt
import json

from services.open15_breakout_service import Open15BreakoutService, Open15Core, resolve_day_config


def _frame(symbol, price, cumvol, h, m, s):
    """Build a (topic, payload) pair exactly as the WS proxy publishes."""
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


def _mk_service(orders):
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
    return svc


def test_full_session_entry_exit_journal_and_daylog():
    from database.open15_breakout_db import Open15Trade, db_session, get_day_log, init_db

    init_db()
    orders = []
    svc = _mk_service(orders)

    # 09:15 first candles: AAA gaps +3% (top gainer), CCC -3% (top loser), ZZZ +1%
    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("ZZZ", 101.0)):
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))

    # 09:16 quiet minute (finalizes selection on first tick)
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))
    assert svc.core.selected == {"AAA": "L", "CCC": "S"}
    svc._handle_raw(*_frame("CCC", 96.9, 6000, 9, 16, 15), _now(9, 16, 15))

    # 09:17: AAA mid-bar volume surge + level break -> ENTRY through the full path
    h1 = svc.core.sym["AAA"]["fc"]["high"]
    svc._handle_raw(*_frame("AAA", h1 + 0.5, 6000 + 9000, 9, 17, 12), _now(9, 17, 12))
    assert len(orders) == 1 and orders[0]["action"] == "BUY" and orders[0]["symbol"] == "AAA"
    assert orders[0]["quantity"] == int(150000 / (h1 + 0.5))
    # journal row written
    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row is not None and row.status == "open" and row.trigger_second == 12
    assert row.trigger_price == h1 + 0.5 and row.level == h1

    # unselected symbol never trades even with a huge surge
    zh = svc.core.sym["ZZZ"]["fc"]["high"]
    svc._handle_raw(*_frame("ZZZ", zh + 5, 99000, 9, 18, 5), _now(9, 18, 5))
    assert len(orders) == 1

    # a later AAA tick inside minute 09:17 sets the entry-minute close
    svc._handle_raw(*_frame("AAA", h1 + 0.9, 16000, 9, 17, 55), _now(9, 17, 55))
    svc._handle_raw(*_frame("AAA", h1 + 0.7, 16500, 9, 18, 20), _now(9, 18, 20))

    # 09:30 flatten: exit order + journal update + near-miss log for CCC
    svc.flatten("eod_0930")
    assert len(orders) == 2 and orders[1]["action"] == "SELL"
    db_session.expire_all()
    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row.status == "closed" and row.exit_price is not None and row.pnl is not None
    assert row.entry_minute_close == h1 + 0.9
    # issue #433: exit timestamp + modelled MIS round-trip charges stamped at close
    assert row.exit_ts is not None
    assert row.charges_inr is not None and row.charges_inr > 0
    events = [e["event"] for e in svc.day_log]
    assert "entry" in events and "exit" in events and "no_entry" in events
    assert svc.day_status == "done"

    # 09:35 summary persists the day log with captured-drift numbers
    svc.summary()
    snap = get_day_log("2026-07-21")
    assert snap and any(e["event"] == "summary" for e in snap)
    summ = [e for e in snap if e["event"] == "summary"][-1]
    assert summ["entered"] == 1 and summ["captured_drift"][0]["symbol"] == "AAA"
    db_session.remove()


def test_status_exposes_running_max_vol_mid_window():
    """issue #524: the running max vol ratio must be readable from /api/status
    DURING the window — the decision log only publishes it at the exit job, so
    pre-#524 the UI's `max vol×` column was blank for the whole session."""
    from database.open15_breakout_db import db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders)
    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("ZZZ", 101.0)):
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))

    st = svc.get_status()
    # both selected symbols present the moment selection finalizes; the unselected
    # ZZZ never appears, and CCC has no in-window tick yet -> None, not 0.0
    assert set(st["watch_stats"]) == {"AAA", "CCC"}
    assert st["watch_stats"]["CCC"]["max_vol_ratio"] is None
    assert st["vol_needed"] == 1.5  # the day config's multiplier, not the env

    # 09:17 partial surge, below the 1.5x threshold -> visible before any exit job
    h1 = svc.core.sym["AAA"]["fc"]["high"]
    svc._handle_raw(*_frame("AAA", h1 + 0.2, 6000 + 1000, 9, 17, 20), _now(9, 17, 20))
    live = svc.get_status()["watch_stats"]["AAA"]
    assert live["max_vol_ratio"] == 1.0 and live["entered"] is False
    assert live["level_broken"] is True
    assert not orders  # observing the near-miss must not place anything

    # and the eod block publishes the same picture for EVERY selected symbol
    svc.flatten("eod_0930")
    ws = next(e for e in svc.day_log if e["event"] == "watch_stats")
    assert ws["needed"] == 1.5
    assert ws["stats"]["AAA"]["max_vol_ratio"] == 1.0
    assert ws["stats"]["CCC"]["max_vol_ratio"] is None
    db_session.remove()


def test_watch_stats_event_covers_entered_symbols():
    """The `no_entry` events skip symbols that traded (issue #524) — the
    `watch_stats` event is what fills their `max vol×` cell."""
    from database.open15_breakout_db import db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders)
    _drive_to_entry(svc)
    assert len(orders) == 1  # AAA entered
    svc.flatten("eod_0930")

    no_entry_syms = {e["symbol"] for e in svc.day_log if e["event"] == "no_entry"}
    assert "AAA" not in no_entry_syms  # exactly the pre-#524 blank
    ws = next(e for e in svc.day_log if e["event"] == "watch_stats")
    assert ws["stats"]["AAA"]["entered"] is True
    assert ws["stats"]["AAA"]["max_vol_ratio"] >= 1.5  # it cleared the gate
    db_session.remove()


def test_dead_feed_day_logs_loudly():
    """Armed but ZERO ticks -> the 09:30 flatten must say so explicitly."""
    orders = []
    svc = _mk_service(orders)
    svc.flatten("eod_0930")
    assert orders == []
    events = {e["event"] for e in svc.day_log}
    assert "no_ticks_received" in events


def test_ticks_outside_window_or_unknown_symbol_are_ignored():
    orders = []
    svc = _mk_service(orders)
    # before 09:14:50 wall clock -> gated
    svc._handle_raw(*_frame("AAA", 103.0, 1000, 9, 10, 0), _now(9, 10, 0))
    # unknown symbol -> gated
    svc._handle_raw(*_frame("XXX", 103.0, 1000, 9, 15, 5), _now(9, 15, 5))
    # bad payload must not raise through the loop path
    try:
        svc._handle_raw("NSE_AAA_LTP", "not-json", _now(9, 15, 6))
    except Exception:
        pass  # the loop catches; direct call may raise — either way no state change
    assert svc.core.sym == {} and orders == []


def test_max_trades_cap_skips_and_journals(monkeypatch):
    """issue #437: the daily entry cap holds across both sides; later triggers
    are journaled as skips, not silently dropped."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders)
    svc.day_config = resolve_day_config(
        {"margin_per_slot": 30000, "sizing_mode": "fixed", "vol_mult": 1.5, "max_trades": 1}, 0
    )

    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("ZZZ", 101.0)):
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))
    svc._handle_raw(*_frame("CCC", 96.9, 6000, 9, 16, 15), _now(9, 16, 15))

    # AAA triggers -> takes the single slot
    h1 = svc.core.sym["AAA"]["fc"]["high"]
    svc._handle_raw(*_frame("AAA", h1 + 0.5, 6000 + 9000, 9, 17, 12), _now(9, 17, 12))
    assert len(orders) == 1
    # CCC (short watch) triggers -> capped, journaled as skip, NO order
    l1 = svc.core.sym["CCC"]["fc"]["low"]
    svc._handle_raw(*_frame("CCC", l1 - 0.5, 6000 + 9000, 9, 18, 30), _now(9, 18, 30))
    assert len(orders) == 1
    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "CCC").first()
    assert row is not None and row.status == "skipped" and row.reason == "max_trades_cap"
    assert row.quantity == 0
    db_session.remove()


def test_option_mode_full_session(monkeypatch):
    """issue #437: ATM-option mode — fit-to-capital lots, NFO BUY at trigger,
    SELL at flatten, real premium P&L + per-lot opt columns journaled."""
    import services.open15_option_shadow as shadow
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders)
    svc.day_config = resolve_day_config(
        {
            "margin_per_slot": 30000,
            "sizing_mode": "fixed",
            "vol_mult": 1.5,
            "instrument": "atm_option",
        },
        0,
    )
    monkeypatch.setattr(
        shadow,
        "resolve_atm_option",
        lambda underlying, side, spot, trade_date: {
            "symbol": "AAA28JUL26105CE",
            "strike": 105.0,
            "expiry": "28-JUL-26",
            "lotsize": 75,
        },
    )
    premium = {"now": 152.0}
    svc.quote_fn = lambda sym, ex: premium["now"]

    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("ZZZ", 101.0)):
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))

    h1 = svc.core.sym["AAA"]["fc"]["high"]
    svc._handle_raw(*_frame("AAA", h1 + 0.5, 6000 + 9000, 9, 17, 12), _now(9, 17, 12))
    # 30000 // (152 * 75) = 2 lots -> qty 150, BUY on NFO
    assert len(orders) == 1
    assert orders[0]["symbol"] == "AAA28JUL26105CE"
    assert orders[0]["action"] == "BUY" and orders[0]["quantity"] == 150
    assert orders[0]["exchange"] == "NFO"

    premium["now"] = 170.0
    svc.flatten("eod_0930")
    assert len(orders) == 2 and orders[1]["action"] == "SELL" and orders[1]["quantity"] == 150

    db_session.expire_all()
    row = (
        db_session.query(Open15Trade)
        .filter(Open15Trade.symbol == "AAA")
        .order_by(Open15Trade.id.desc())
        .first()
    )
    assert row.instrument == "option" and row.opt_symbol == "AAA28JUL26105CE"
    assert row.opt_entry_premium == 152.0 and row.opt_exit_premium == 170.0
    assert row.pnl == (170.0 - 152.0) * 150  # real premium P&L on full qty
    assert row.charges_inr is not None and 200 < row.charges_inr < 350
    # per-lot net for the shadow-consistent columns
    assert row.opt_lot_size == 75 and 1150 < row.opt_pnl < 1330
    db_session.remove()


def test_option_mode_unaffordable_skip(monkeypatch):
    """issue #437: 1 lot beyond slot capital -> journaled skip, no order."""
    import services.open15_option_shadow as shadow
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders)
    svc.day_config = resolve_day_config(
        {
            "margin_per_slot": 30000,
            "sizing_mode": "fixed",
            "vol_mult": 1.5,
            "instrument": "atm_option",
        },
        0,
    )
    monkeypatch.setattr(
        shadow,
        "resolve_atm_option",
        lambda underlying, side, spot, trade_date: {
            "symbol": "AAA28JUL26105CE",
            "strike": 105.0,
            "expiry": "28-JUL-26",
            "lotsize": 625,
        },
    )
    svc.quote_fn = lambda sym, ex: 75.7  # 625 x 75.7 = 47,312 > 30,000

    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("ZZZ", 101.0)):
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))
    h1 = svc.core.sym["AAA"]["fc"]["high"]
    svc._handle_raw(*_frame("AAA", h1 + 0.5, 6000 + 9000, 9, 17, 12), _now(9, 17, 12))

    assert orders == []
    row = (
        db_session.query(Open15Trade)
        .filter(Open15Trade.symbol == "AAA")
        .order_by(Open15Trade.id.desc())
        .first()
    )
    assert row is not None and row.status == "skipped" and row.reason == "unaffordable"
    assert row.opt_entry_premium == 75.7 and row.opt_lot_size == 625
    db_session.remove()


def _mk_option_service(orders, monkeypatch, lotsize=75):
    """Option-mode service with the ATM resolver pinned to one contract."""
    import services.open15_option_shadow as shadow

    svc = _mk_service(orders)
    svc.day_config = resolve_day_config(
        {
            "margin_per_slot": 30000,
            "sizing_mode": "fixed",
            "vol_mult": 1.5,
            "instrument": "atm_option",
        },
        0,
    )
    monkeypatch.setattr(
        shadow,
        "resolve_atm_option",
        lambda underlying, side, spot, trade_date: {
            "symbol": "AAA28JUL26105CE",
            "strike": 105.0,
            "expiry": "28-JUL-26",
            "lotsize": lotsize,
        },
    )
    return svc


def _drive_to_entry(svc):
    """09:15 candles -> 09:16 selection -> 09:17 surge trigger on AAA."""
    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("ZZZ", 101.0)):
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))
    h1 = svc.core.sym["AAA"]["fc"]["high"]
    svc._handle_raw(*_frame("AAA", h1 + 0.5, 6000 + 9000, 9, 17, 12), _now(9, 17, 12))


def _latest(symbol="AAA"):
    from database.open15_breakout_db import Open15Trade, db_session

    db_session.expire_all()
    return (
        db_session.query(Open15Trade)
        .filter(Open15Trade.symbol == symbol)
        .order_by(Open15Trade.id.desc())
        .first()
    )


def test_option_liquidity_recorded_at_entry_and_exit(monkeypatch):
    """issue #488: the contract's own volume + OI are journaled at BOTH decision
    moments, and the snapshot's ltp prices the trade (one quote call, not two)."""
    from database.open15_breakout_db import db_session, init_db

    init_db()
    orders = []
    svc = _mk_option_service(orders, monkeypatch)
    snap = {"ltp": 152.0, "volume": 12345, "oi": 6789}
    svc.quote_snapshot_fn = lambda sym, ex: dict(snap)
    # would price the trade differently — proves the snapshot is the source
    svc.quote_fn = lambda sym, ex: 999.0

    _drive_to_entry(svc)
    assert len(orders) == 1 and orders[0]["quantity"] == 150  # 30000 // (152*75) = 2 lots

    snap.update(ltp=170.0, volume=55555, oi=4321)
    svc.flatten("eod_0930")
    assert len(orders) == 2 and orders[1]["action"] == "SELL"

    row = _latest()
    assert row.opt_entry_premium == 152.0 and row.opt_exit_premium == 170.0
    assert row.opt_entry_volume == 12345 and row.opt_entry_oi == 6789
    assert row.opt_exit_volume == 55555 and row.opt_exit_oi == 4321
    db_session.remove()


def test_option_liquidity_snapshot_unavailable_falls_back(monkeypatch):
    """A snapshot miss must never cost an entry: quote_fn still prices the trade
    and the liquidity columns stay NULL (unknown), not 0 (no liquidity)."""
    from database.open15_breakout_db import db_session, init_db

    init_db()
    orders = []
    svc = _mk_option_service(orders, monkeypatch)
    svc.quote_snapshot_fn = lambda sym, ex: None
    svc.quote_fn = lambda sym, ex: 152.0

    _drive_to_entry(svc)
    assert len(orders) == 1 and orders[0]["quantity"] == 150
    svc.flatten("eod_0930")

    row = _latest()
    assert row.opt_entry_premium == 152.0 and row.opt_exit_premium == 152.0
    assert row.opt_entry_volume is None and row.opt_entry_oi is None
    assert row.opt_exit_volume is None and row.opt_exit_oi is None
    db_session.remove()


def test_option_liquidity_snapshot_raising_is_contained(monkeypatch):
    """A broken snapshot provider is instrumentation failure, not trade failure."""
    from database.open15_breakout_db import db_session, init_db

    init_db()
    orders = []
    svc = _mk_option_service(orders, monkeypatch)

    def boom(sym, ex):
        raise RuntimeError("quote provider down")

    svc.quote_snapshot_fn = boom
    svc.quote_fn = lambda sym, ex: 152.0

    _drive_to_entry(svc)
    assert len(orders) == 1 and orders[0]["quantity"] == 150
    svc.flatten("eod_0930")
    assert len(orders) == 2  # exit still placed

    row = _latest()
    assert row.opt_entry_premium == 152.0
    assert row.opt_entry_volume is None and row.opt_exit_oi is None
    db_session.remove()


def test_missing_quote_fields_read_as_unknown_not_zero():
    """A broker that omits oi/volume must leave NULL — writing 0 would read as
    'no liquidity' and poison the very analysis these columns exist for."""
    from services.open15_breakout_service import _as_int

    assert _as_int(None) is None
    assert _as_int("") is None
    assert _as_int("n/a") is None
    assert _as_int(0) == 0  # a genuine reported zero is preserved
    assert _as_int("12345") == 12345
    assert _as_int(6789.0) == 6789
