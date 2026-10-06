"""cas_auction_reversal (issue #752): decision, sandbox/live entry, verification,
T+1 exit — with the real journal (temp DB via test/conftest.py) and fakes at
every broker seam."""

from datetime import datetime, timedelta, timezone

import pytest

import database.cas_auction_reversal_db as cdb
import restx_api  # noqa: F401  (imported first, as app.py does — place_order_service ↔ restx_api cycle)
from services.cas_auction_reversal_service import (
    CasAuctionReversalService,
    book_quantities,
    cnc_round_trip_charges,
    dislocation_pct,
    resolve_config,
    select_candidates,
    size_qty,
)

IST = timezone(timedelta(hours=5, minutes=30))
CFG = {
    "threshold_pct": 0.5,
    "max_positions": 10,
    "capital_per_trade_inr": 50_000,
    "poll_interval_s": 15,
}


@pytest.fixture(autouse=True)
def _tables():
    cdb.init_db()
    s = cdb.db_session
    for m in (cdb.CasArTrade, cdb.CasArPoll, cdb.CasArCandidate):
        s.query(m).delete()
    s.commit()
    s.remove()
    yield


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt

    def at(self, hhmmss, day=None):
        h, m, s = (int(x) for x in hhmmss.split(":"))
        d = day or self.dt.date()
        self.dt = datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST)
        return self


class Broker:
    """Quotes keyed by phase, order book, fills and holdings."""

    def __init__(self):
        self.quotes = {}
        self.orders = []
        self.fill_status = "filled"
        self.fill_price = {}
        self.holdings = []
        self.positions = []
        self.book_readable = True
        self.cancelled = []
        self._n = 0

    def cancel(self, oid):
        self.cancelled.append(oid)
        return {"status": "success", "orderid": oid}

    def quote_batch(self, symbols):
        return {s: dict(self.quotes[s]) for s in symbols if s in self.quotes}

    def place(self, order):
        self._n += 1
        oid = f"OID{self._n}"
        self.orders.append({**order, "orderid": oid})
        return {"status": "success", "orderid": oid}

    def fill(self, oid):
        o = next(o for o in self.orders if o["orderid"] == oid)
        if self.fill_status == "pending":
            return {"status": "pending", "price": None, "qty": None, "message": None}
        if self.fill_status == "rejected":
            return {
                "status": "rejected",
                "price": None,
                "qty": 0,
                "message": "RMS: insufficient funds",
            }
        price = self.fill_price.get(o["symbol"], o.get("price") or 100.0)
        return {"status": "filled", "price": price, "qty": int(o["quantity"]), "message": None}

    def book(self):
        if not self.book_readable:
            return None, None
        return self.holdings, self.positions


def _svc(clock, broker, mode="sandbox", notes=None, paused=False, ticks=None):
    notes = notes if notes is not None else []
    svc = CasAuctionReversalService(
        universe_provider=lambda: ["AAA", "BBB", "CCC", "DDD"],
        quote_batch=broker.quote_batch,
        order_placer=broker.place,
        fill_reader=broker.fill,
        book_reader=broker.book,
        mode_resolver=lambda: mode,
        notifier=notes.append,
        trading_day_checker=lambda d: True,
        config_reader=lambda: dict(CFG),
        tick_size_reader=lambda s: (ticks or {}).get(s, 0.05),
        order_canceller=broker.cancel,
        next_trading_day=lambda d: d + timedelta(days=3 if d.weekday() == 4 else 1),
        now=clock,
        sleep=lambda s: None,
    )
    svc.manual_pause = paused
    svc._ensure_monitor_thread = lambda: None  # drive polls by hand
    return svc


def _run_session(clock, broker, svc):
    """arm 15:14:30 → pre-close poll → frozen post-15:15 poll → IEP poll → decide."""
    day = clock.dt.date()
    clock.at("15:14:30", day)
    svc.arm()
    for s in ("AAA", "BBB", "CCC", "DDD"):
        broker.quotes[s] = {"ltp": 100.0, "iep": None, "imbalance_qty": None, "volume": 1000}
    clock.at("15:14:50", day)
    svc.poll_once()
    clock.at("15:15:05", day)
    svc.poll_once()  # freezes ref = 100 (last continuous print)
    broker.quotes["AAA"]["iep"] = 99.3  # -0.70% → candidate
    broker.quotes["BBB"]["iep"] = 99.8  # -0.20% → not
    broker.quotes["CCC"]["iep"] = 98.0  # -2.00% → candidate, rank 1
    broker.quotes["DDD"]["iep"] = 101.0  # pushed UP → never bought
    clock.at("15:23:20", day)
    svc.poll_once()
    clock.at("15:23:30", day)
    return svc.run_decide()


# --------------------------------------------------------------------------- pure
def test_select_candidates_ranks_deepest_first_and_caps():
    ref = {"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0}
    last = {"A": {"iep": 99.3}, "B": {"iep": 99.8}, "C": {"iep": 98.0}, "D": {"iep": 99.0}}
    rows = select_candidates(ref, last, threshold_pct=0.5, max_positions=2)
    assert [r["symbol"] for r in rows] == ["C", "D", "A"]
    assert [r["selected"] for r in rows] == [True, True, False]
    assert rows[2]["skip_reason"] == "max_positions"


def test_dislocation_and_sizing_guards():
    assert dislocation_pct(None, 100) is None and dislocation_pct(99, 0) is None
    assert round(dislocation_pct(99.5, 100), 3) == -0.5
    assert size_qty(50_000, 1234.5) == 40 and size_qty(50_000, None) == 0


def test_cnc_charges_are_about_a_quarter_percent():
    c = cnc_round_trip_charges(50_000, 50_300)
    assert 110 < c < 140  # 0.2% STT + stamp + txn + DP ≈ 0.25%


def test_book_quantities_takes_max_not_sum():
    hold = [{"symbol": "AAA", "exchange": "NSE", "quantity": 50}]
    pos = [
        {"symbol": "AAA", "exchange": "NSE", "product": "CNC", "quantity": 50},
        {"symbol": "BBB", "exchange": "NSE", "product": "MIS", "quantity": 9},
    ]
    assert book_quantities(hold, pos) == {"AAA": 50}


def test_config_clamps(monkeypatch):
    monkeypatch.setenv("CAS_AR_THRESHOLD_PCT", "0.01")
    monkeypatch.setenv("CAS_AR_MAX_POSITIONS", "500")
    cfg = resolve_config()
    assert cfg["threshold_pct"] == 0.2 and cfg["max_positions"] == 30


# --------------------------------------------------------------------------- flow
def test_sandbox_decide_queues_then_buys_at_the_printed_auction_close():
    clock, broker = Clock(datetime(2026, 10, 5, 15, 0, tzinfo=IST)), Broker()
    svc = _svc(clock, broker)
    res = _run_session(clock, broker, svc)
    assert res == {"status": "queued", "mode": "sandbox", "orders": 2}
    assert broker.orders == []  # sandbox never orders before the auction prints
    cands = {c.symbol: c for c in cdb.candidates_for_date("2026-10-05")}
    assert set(cands) == {"AAA", "CCC"} and cands["CCC"].rank == 1

    broker.quotes["AAA"]["ltp"] = 99.25  # the auction print
    broker.quotes["CCC"]["ltp"] = 98.10
    clock.at("15:32:00")
    out = svc.run_fill()
    assert out["placed"] == 2 and out["filled"] == 2
    by = {o["symbol"]: o for o in broker.orders}
    assert by["AAA"]["pricetype"] == "LIMIT" and by["AAA"]["price"] == 99.25
    assert by["CCC"]["quantity"] == 509  # floor(50000 / 98.10)
    rows = {r.symbol: r for r in cdb.trades_for_date("2026-10-05")}
    assert rows["AAA"].status == "open" and rows["AAA"].entry_price == 99.25
    assert rows["AAA"].mode == "sandbox" and rows["AAA"].iep_at_decision == 99.3
    assert svc.run_fill()["status"] == "skipped"  # idempotent
    assert cdb.poll_count("2026-10-05") == 12  # 3 polls × 4 symbols


def test_live_decide_places_limits_into_the_auction_window():
    clock, broker = Clock(datetime(2026, 10, 5, 15, 0, tzinfo=IST)), Broker()
    svc = _svc(clock, broker, mode="live")
    res = _run_session(clock, broker, svc)
    assert res["status"] == "placed" and len(broker.orders) == 2
    by = {o["symbol"]: o for o in broker.orders}
    assert by["CCC"]["price"] == 98.2  # iep 98.0 × 1.002 → tick
    assert all(o["action"] == "BUY" for o in broker.orders)


def test_pause_journals_candidates_but_places_nothing():
    clock, broker = Clock(datetime(2026, 10, 5, 15, 0, tzinfo=IST)), Broker()
    svc = _svc(clock, broker, paused=True)
    res = _run_session(clock, broker, svc)
    assert res["status"] == "no_entries" and broker.orders == []
    assert {c.skip_reason for c in cdb.candidates_for_date("2026-10-05")} == {"paused"}


def test_post_ack_rejection_becomes_paper_and_alerts():
    clock, broker, notes = Clock(datetime(2026, 10, 5, 15, 0, tzinfo=IST)), Broker(), []
    svc = _svc(clock, broker, notes=notes)
    _run_session(clock, broker, svc)
    broker.fill_status = "rejected"
    clock.at("15:32:00")
    out = svc.run_fill()
    assert out["rejected"] == 2
    rows = cdb.trades_for_date("2026-10-05")
    assert all(r.fill == "paper" and r.status == "rejected" for r in rows)
    assert cdb.open_positions(before_date="2026-10-06") == []  # nothing to exit
    assert any("refused post-ACK" in n for n in notes)


def _bought_yesterday(broker):
    clock = Clock(datetime(2026, 10, 5, 15, 0, tzinfo=IST))
    svc = _svc(clock, broker)
    _run_session(clock, broker, svc)
    broker.quotes["AAA"]["ltp"] = 99.25
    broker.quotes["CCC"]["ltp"] = 98.10
    clock.at("15:32:00")
    svc.run_fill()
    clock.at("09:16:00", datetime(2026, 10, 6).date())
    return clock, svc


def test_t1_exit_sells_from_holdings_and_books_net_pnl():
    broker = Broker()
    clock, svc = _bought_yesterday(broker)
    broker.holdings = [
        {"symbol": "AAA", "exchange": "NSE", "quantity": 503},
        {"symbol": "CCC", "exchange": "NSE", "quantity": 509},
    ]
    broker.fill_price = {"AAA": 99.85, "CCC": 99.0}
    n_before = len(broker.orders)
    out = svc.run_exit()
    sells = broker.orders[n_before:]
    assert {(o["symbol"], o["action"], o["quantity"]) for o in sells} == {
        ("AAA", "SELL", 503),
        ("CCC", "SELL", 509),
    }
    assert all(r["status"] == "closed" for r in out["results"])
    closed = {r.symbol: r for r in cdb.real_closed_rows()}
    r = closed["CCC"]
    assert r.exit_reason == "t1_exit" and r.charges_inr > 0
    assert cdb.net_pnl_of_row(r) == round((99.0 - 98.10) * 509 - r.charges_inr, 2)
    assert svc.run_exit_retry() == {"status": "no_positions"}  # idempotent


def test_t1_exit_unreadable_book_still_sends_journalled_qty():
    """A CNC SELL cannot open a short — not sending is the only way to strand."""
    broker = Broker()
    clock, svc = _bought_yesterday(broker)
    broker.book_readable = False
    n_before = len(broker.orders)
    svc.run_exit()
    assert {(o["symbol"], o["quantity"]) for o in broker.orders[n_before:]} == {
        ("AAA", 503),
        ("CCC", 509),
    }


def test_t1_exit_caps_to_book_quantity():
    broker = Broker()
    clock, svc = _bought_yesterday(broker)
    broker.holdings = [{"symbol": "AAA", "exchange": "NSE", "quantity": 100}]
    n_before = len(broker.orders)
    svc.run_exit()
    q = {o["symbol"]: o["quantity"] for o in broker.orders[n_before:]}
    assert q["AAA"] == 100 and q["CCC"] == 509  # CCC flat in book → still sent


def test_exit_does_not_touch_todays_entries():
    clock, broker = Clock(datetime(2026, 10, 5, 15, 0, tzinfo=IST)), Broker()
    svc = _svc(clock, broker)
    _run_session(clock, broker, svc)
    broker.quotes["AAA"]["ltp"] = 99.25
    broker.quotes["CCC"]["ltp"] = 98.10
    clock.at("15:32:00")
    svc.run_fill()
    assert svc.run_exit() == {"status": "no_positions"}  # same day: nothing sold


def test_not_armed_on_holiday():
    clock, broker = Clock(datetime(2026, 10, 5, 15, 14, 30, tzinfo=IST)), Broker()
    svc = _svc(clock, broker)
    svc._trading_day = lambda d: False
    assert svc.arm()["status"] == "skipped"
    clock.at("15:23:30")
    assert svc.run_decide()["status"] == "skipped"


def test_eod_summary_records_final_auction_close():
    clock, broker, notes = Clock(datetime(2026, 10, 5, 15, 0, tzinfo=IST)), Broker(), []
    svc = _svc(clock, broker, notes=notes)
    _run_session(clock, broker, svc)
    broker.quotes["AAA"]["ltp"] = 99.25
    broker.quotes["CCC"]["ltp"] = 99.9  # auction recovered: no longer below threshold
    clock.at("15:32:00")
    svc.run_fill()
    clock.at("15:45:00")
    summary = svc.run_eod_summary()
    c = {x.symbol: x for x in cdb.candidates_for_date("2026-10-05")}
    assert c["AAA"].auction_close == 99.25 and round(c["AAA"].close_dislocation_pct, 2) == -0.75
    assert summary["still_below_threshold_at_close"] == 1
    assert any("cas_auction_reversal 2026-10-05" in n for n in notes)


def test_production_order_placer_routes_cnc_with_the_strategy_mode_key(monkeypatch):
    """The seam every order crosses: CNC, NSE, the strategy label AND
    mode_key=cas_auction_reversal (per-strategy dispatch, #440/#497)."""
    import services.cas_auction_reversal_service as svc_mod

    seen = {}

    def fake_place_order(payload, api_key=None, mode_key=None):
        seen.update(payload=payload, mode_key=mode_key)
        return True, {"status": "success", "orderid": "X1"}, 200

    monkeypatch.setattr("services.place_order_service.place_order", fake_place_order)
    monkeypatch.setattr(svc_mod, "_api_key", lambda: "k")
    out = svc_mod.production_order_placer(
        {"symbol": "SBIN", "action": "BUY", "quantity": 52, "pricetype": "LIMIT", "price": 961.4}
    )
    assert out["orderid"] == "X1"
    assert seen["mode_key"] == "cas_auction_reversal"
    p = seen["payload"]
    assert (p["product"], p["exchange"], p["strategy"], p["pricetype"], p["price"]) == (
        "CNC",
        "NSE",
        "cas_auction_reversal",
        "LIMIT",
        "961.4",
    )


def test_decide_alerts_when_the_iep_never_leaves_the_last_print():
    """Outside CAS Kite returns indicative_close_price == last price (verified
    2026-10-04). If that is all the decision sees, say so loudly."""
    clock, broker, notes = Clock(datetime(2026, 10, 5, 15, 14, 30, tzinfo=IST)), Broker(), []
    svc = _svc(clock, broker, notes=notes)
    svc.arm()
    for s in ("AAA", "BBB", "CCC", "DDD"):
        broker.quotes[s] = {"ltp": 100.0, "iep": 100.0, "imbalance_qty": 0, "volume": 10}
    clock.at("15:15:05")
    svc.poll_once()
    clock.at("15:23:30")
    assert svc.run_decide()["status"] == "no_entries"
    assert svc.day["iep_feed"] == {"with_iep": 4, "moved": 0}
    assert any("not reaching the quote feed" in n for n in notes)


def test_healthy_iep_feed_does_not_alert():
    clock, broker, notes = Clock(datetime(2026, 10, 5, 15, 0, tzinfo=IST)), Broker(), []
    svc = _svc(clock, broker, notes=notes)
    _run_session(clock, broker, svc)
    assert svc.day["iep_feed"]["moved"] == 4
    assert not any("not reaching" in n for n in notes)


# --------------------------------------------------------------------------- issue #759
def test_buy_limit_rounds_up_on_the_symbols_own_tick():
    from services.cas_auction_reversal_service import buy_limit_price

    assert buy_limit_price(168.27, 0.01) == 168.27  # BANDHANBNK: exact, not 168.25
    assert buy_limit_price(168.27, 0.05) == 168.30  # never below the price to cross
    assert buy_limit_price(1368.94, 0.1) == 1369.0
    assert buy_limit_price(99.25, 0.05) == 99.25


def test_sandbox_entry_uses_the_symbol_tick():
    clock, broker = Clock(datetime(2026, 10, 5, 15, 0, tzinfo=IST)), Broker()
    svc = _svc(clock, broker, ticks={"AAA": 0.01})
    _run_session(clock, broker, svc)
    broker.quotes["AAA"]["ltp"] = 99.27
    broker.quotes["CCC"]["ltp"] = 98.12
    clock.at("15:32:00")
    svc.run_fill()
    by = {o["symbol"]: o["price"] for o in broker.orders}
    assert by == {"AAA": 99.27, "CCC": 98.15}  # 0.01 exact; 0.05 rounds UP


def test_unfilled_entry_is_cancelled_at_eod_and_never_sold():
    clock, broker, notes = Clock(datetime(2026, 10, 5, 15, 0, tzinfo=IST)), Broker(), []
    svc = _svc(clock, broker, notes=notes)
    _run_session(clock, broker, svc)
    broker.quotes["AAA"]["ltp"] = 99.25
    broker.quotes["CCC"]["ltp"] = 98.10
    broker.fill_status = "pending"  # the LIMIT never executes
    clock.at("15:32:00")
    assert svc.run_fill()["unverified"] == 2
    clock.at("15:45:00")
    summary = svc.run_eod_summary()
    assert sorted(summary["entries_unfilled"]) == ["AAA", "CCC"]
    assert sorted(broker.cancelled) == sorted(o["orderid"] for o in broker.orders)
    rows = cdb.trades_for_date("2026-10-05")
    assert all(r.status == "unfilled" and r.fill == "none" for r in rows)
    assert cdb.net_pnl_of_row(rows[0]) is None
    assert any("never filled" in n for n in notes)
    assert cdb.open_positions(before_date="2026-10-06") == []
    clock.at("09:16:00", datetime(2026, 10, 6).date())
    n = len(broker.orders)
    assert svc.run_exit() == {"status": "no_positions"} and len(broker.orders) == n


def test_eod_never_cancels_a_filled_entry():
    clock, broker = Clock(datetime(2026, 10, 5, 15, 0, tzinfo=IST)), Broker()
    svc = _svc(clock, broker)
    _run_session(clock, broker, svc)
    broker.quotes["AAA"]["ltp"] = 99.25
    broker.quotes["CCC"]["ltp"] = 98.10
    clock.at("15:32:00")
    svc.run_fill()
    clock.at("15:45:00")
    assert svc.run_eod_summary()["entries_unfilled"] == []
    assert broker.cancelled == []


def _placed_row(symbol="BANDHANBNK", oid="OLD1"):
    return cdb.record_trade(
        mode="sandbox",
        trade_date="2026-10-05",
        symbol=symbol,
        exchange="NSE",
        product="CNC",
        quantity=297,
        entry_order_id=oid,
        status="placed",
        fill="real",
    )


def test_t1_exit_settles_a_stale_placed_row_instead_of_selling():
    """The 2026-10-05 BANDHANBNK shape: a row left 'placed' is not a position."""
    broker = Broker()
    rid = _placed_row()
    broker.orders.append({"symbol": "BANDHANBNK", "quantity": 297, "orderid": "OLD1"})
    broker.fill_status = "pending"
    clock = Clock(datetime(2026, 10, 6, 9, 16, tzinfo=IST))
    svc = _svc(clock, broker)
    assert svc.run_exit() == {"status": "no_positions"}
    assert [o for o in broker.orders if o.get("action") == "SELL"] == []
    assert broker.cancelled == ["OLD1"]
    r = next(r for r in cdb.trades_for_date("2026-10-05") if r.id == rid)
    assert r.status == "unfilled" and r.fill == "none"


def test_t1_exit_sells_a_placed_row_that_did_fill():
    broker = Broker()
    _placed_row()
    broker.orders.append(
        {"symbol": "BANDHANBNK", "quantity": 297, "orderid": "OLD1", "price": 168.27}
    )
    broker.holdings = [{"symbol": "BANDHANBNK", "exchange": "NSE", "quantity": 297}]
    clock = Clock(datetime(2026, 10, 6, 9, 16, tzinfo=IST))
    svc = _svc(clock, broker)
    out = svc.run_exit()
    assert out["results"][0]["status"] == "closed"
    r = cdb.real_closed_rows()[0]
    assert r.entry_price == 168.27 and r.entry_qty == 297


def test_t1_exit_with_unreadable_entry_status_still_sends():
    broker = Broker()
    _placed_row()
    clock = Clock(datetime(2026, 10, 6, 9, 16, tzinfo=IST))
    svc = _svc(clock, broker)
    svc._fill_reader = lambda oid: None if oid == "OLD1" else broker.fill(oid)
    svc.run_exit()
    assert [(o["symbol"], o["action"]) for o in broker.orders] == [("BANDHANBNK", "SELL")]


def test_holiday_skips_the_exit_and_the_next_trading_day_sells():
    broker = Broker()
    clock, svc = _bought_yesterday(broker)  # bought Mon 2026-10-05
    svc._trading_day = lambda d: d.isoformat() != "2026-10-06"  # Tue = holiday
    assert svc.run_exit() == {"status": "skipped", "reason": "not_trading_day"}
    assert all(o["action"] == "BUY" for o in broker.orders)
    clock.at("09:16:00", datetime(2026, 10, 7).date())
    broker.holdings = [
        {"symbol": "AAA", "exchange": "NSE", "quantity": 503},
        {"symbol": "CCC", "exchange": "NSE", "quantity": 509},
    ]
    out = svc.run_exit()
    assert {r["symbol"] for r in out["results"] if r["status"] == "closed"} == {"AAA", "CCC"}


def test_eod_summary_names_the_exit_day_across_a_weekend():
    clock, broker, notes = Clock(datetime(2026, 10, 9, 15, 0, tzinfo=IST)), Broker(), []
    svc = _svc(clock, broker, notes=notes)  # Friday
    _run_session(clock, broker, svc)
    broker.quotes["AAA"]["ltp"] = 99.25
    broker.quotes["CCC"]["ltp"] = 98.10
    clock.at("15:32:00")
    svc.run_fill()
    clock.at("15:45:00")
    assert svc.run_eod_summary()["exit_on"] == "2026-10-12"
    assert any("T+1 exit 2026-10-12 09:16" in n for n in notes)


def test_production_trading_day_checker_uses_the_nse_calendar(monkeypatch):
    from datetime import date

    import database.market_calendar_db as mcal
    import services.data_freshness_service as dfs
    from services.cas_auction_reversal_service import production_trading_day_checker

    seen = []

    def fake_is_trading_day(d, exchange=None):
        seen.append(exchange)
        return d != date(2026, 10, 20)  # Dussehra

    monkeypatch.setattr(dfs, "is_trading_day", fake_is_trading_day)
    monkeypatch.setattr(
        mcal,
        "get_special_session",
        lambda d, ex: {"start_ms": 1, "end_ms": 2} if d == date(2026, 11, 9) else None,
    )
    assert production_trading_day_checker(date(2026, 10, 20)) is False
    assert production_trading_day_checker(date(2026, 11, 9)) is False  # special session
    assert production_trading_day_checker(date(2026, 10, 21)) is True
    assert set(seen) == {"NSE"}


def test_production_canceller_uses_cancel_order_with_the_api_key(monkeypatch):
    import services.cancel_order_service as cos
    import services.cas_auction_reversal_service as mod

    calls = []

    def fake_cancel(orderid, api_key=None, auth_token=None, broker=None):
        calls.append((orderid, api_key))
        return True, {"status": "success", "orderid": orderid}, 200

    monkeypatch.setattr(cos, "cancel_order", fake_cancel)
    monkeypatch.setattr(mod, "_api_key", lambda: "k")
    assert mod.production_order_canceller("26100512278900")["status"] == "success"
    assert calls == [("26100512278900", "k")]


def test_production_next_trading_day_skips_the_weekend():
    from datetime import date

    from services.cas_auction_reversal_service import production_next_trading_day

    assert production_next_trading_day(date(2026, 10, 9)) == date(2026, 10, 12)
