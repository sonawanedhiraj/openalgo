"""Broker fill reconciliation + simulated non-traded rows (issue #555).

Two gaps this pins shut:

1. **Every P&L the strategy published was a quote-derived estimate.** The prices
   it journals are decision-moment observations — the tick that fired the gate,
   the option quote at that instant — never what the broker transacted. So the
   number on the logs page had never been checked against the broker, and
   slippage was structurally unmeasurable.
2. **A trigger we did not take was a dead end.** ``unaffordable`` / cap-skipped
   rows journaled a contract and an entry premium and then stopped: no exit, no
   P&L, no label — so "is the slot capital, or the signal, what is capping this
   strategy?" could not be answered.

The load-bearing invariants, each with a test below:

* ``pnl`` stays the ONE gross number (issue #552). Reconciliation OVERWRITES it
  rather than adding a second convention, so no consumer has to know which kind
  of price produced it.
* Sim money never becomes real money — not in ``total_realized_pnl`` (which
  sizes tomorrow's real orders), not in the day's P&L.
* A sim row NEVER causes an order, and never consults the position book: no
  order was sent, so the book can only surface someone else's position.
"""

import datetime as dt
import json

import pytest

from services.open15_breakout_service import Open15BreakoutService, Open15Core, resolve_day_config


@pytest.fixture(autouse=True)
def _clean_journal():
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()
    yield
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()


# --------------------------------------------------------------------------- #
# recompute_pnl — the formula must not change, only which prices go into it
# --------------------------------------------------------------------------- #
def test_recompute_pnl_long_stock_matches_the_flatten_formula():
    from services.open15_breakout_service import mis_round_trip_charges
    from services.open15_fill_reconcile import recompute_pnl

    row = {"instrument": "stock", "side": "L", "quantity": 100}
    gross, charges = recompute_pnl(row, 100.0, 103.0)
    assert gross == pytest.approx(300.0)
    assert charges == mis_round_trip_charges(100.0 * 100, 103.0 * 100)


def test_recompute_pnl_short_stock_inverts_the_direction():
    from services.open15_fill_reconcile import recompute_pnl

    row = {"instrument": "stock", "side": "S", "quantity": 100}
    gross, _ = recompute_pnl(row, 100.0, 97.0)
    assert gross == pytest.approx(300.0), "a short that fell is a profit"


def test_recompute_pnl_option_is_always_a_premium_buy():
    """Even on a SHORT signal: the strategy buys a PE, it never sells options.

    Reading the signal's side here would flip the sign on every short-signal
    option trade and report a winning day as a losing one.
    """
    from services.open15_fill_reconcile import recompute_pnl

    row = {"instrument": "option", "side": "S", "quantity": 650}
    gross, charges = recompute_pnl(row, 45.0, 50.0)
    assert gross == pytest.approx(5 * 650)
    assert charges and charges > 0


def _stub_order_status(monkeypatch, by_orderid):
    """Point the module's broker seam at a dict of ``{orderid: data}``."""
    import sys

    def get_order_status(payload, api_key=None, **_kw):
        data = by_orderid.get(str(payload.get("orderid")))
        if data is None:
            return False, {"message": "not found"}, 404
        return True, {"data": data}, 200

    monkeypatch.setitem(
        sys.modules,
        "services.orderstatus_service",
        type("M", (), {"get_order_status": staticmethod(get_order_status)}),
    )
    monkeypatch.setitem(
        sys.modules,
        "database.auth_db",
        type("M", (), {"get_first_available_api_key": staticmethod(lambda: "k")}),
    )


# --------------------------------------------------------------------------- #
# fetch_fill — a zero average_price is "not reported", never a price
# --------------------------------------------------------------------------- #
def test_a_zero_average_price_is_not_treated_as_a_fill(monkeypatch):
    """Zerodha reports 0 for an unfilled order.

    Taking it literally computes a P&L equal to the entire notional and stamps
    it ``reconciled`` — a fabricated number wearing the badge that says the
    broker confirmed it.
    """
    from services.open15_fill_reconcile import fetch_fill

    _stub_order_status(monkeypatch, {"T-1": {"average_price": 0, "order_status": "open"}})
    out = fetch_fill("T-1", "key")
    assert out is not None and out["price"] is None
    assert out["order_status"] == "open"


# --------------------------------------------------------------------------- #
# reconcile_fills — overwrite in place, one P&L convention survives
# --------------------------------------------------------------------------- #
def _insert_real_row(**over):
    from database.open15_breakout_db import insert_trade

    kw = {
        "trade_date": "2026-08-06",
        "symbol": "HAL",
        "side": "L",
        "mode": "live",
        "instrument": "stock",
        "quantity": 100,
        "trigger_price": 100.0,
        "exit_price": 103.0,
        "entry_order_id": "E-1",
        "exit_order_id": "X-1",
        "entry_status": "success",
        "exit_status": "success",
        "status": "closed",
        "fill": "real",
        "pnl": 300.0,
        "charges_inr": 50.0,
    }
    kw.update(over)
    return insert_trade(**kw)


def test_fill_prices_overwrite_the_quote_derived_pnl_in_place(monkeypatch):
    """The #552 rule survives: ONE gross number, re-derived — not a second one."""
    from database.open15_breakout_db import Open15Trade, db_session
    from services.open15_fill_reconcile import reconcile_fills

    row_id = _insert_real_row()
    _stub_order_status(
        monkeypatch,
        {
            "E-1": {"average_price": 100.5, "quantity": 100, "order_status": "complete"},
            "X-1": {"average_price": 102.5, "quantity": 100, "order_status": "complete"},
        },
    )
    monkeypatch.setattr(
        "services.open15_fill_reconcile.broker_pnl_by_symbol", lambda _k: {"HAL": 200.0}
    )

    res = reconcile_fills("2026-08-06")
    assert res["reconciled"] == 1

    row = db_session.query(Open15Trade).filter(Open15Trade.id == row_id).first()
    assert row.entry_fill_price == 100.5 and row.exit_fill_price == 102.5
    # gross re-derived from the FILLS, not the quotes: (102.5-100.5)*100
    assert row.pnl == pytest.approx(200.0)
    assert row.pnl_source == "fill" and row.fill_reconcile_status == "reconciled"
    assert row.broker_pnl == 200.0
    # the decision-moment prices are UNTOUCHED — `fill - quote` is the slippage
    # this strategy exists to measure, and collapsing them destroys it
    assert row.trigger_price == 100.0 and row.exit_price == 103.0
    db_session.remove()


def test_an_unreported_exit_leg_leaves_the_row_pending_and_its_pnl_alone(monkeypatch):
    """Half a reconciliation is not a reconciliation.

    Mixing a fill entry with a quote exit would publish a P&L that matches
    neither the broker nor the journal, under a badge claiming it is confirmed.
    """
    from database.open15_breakout_db import Open15Trade, db_session
    from services.open15_fill_reconcile import reconcile_fills

    row_id = _insert_real_row()
    _stub_order_status(
        monkeypatch,
        {
            "E-1": {"average_price": 100.5, "quantity": 100, "order_status": "complete"},
            "X-1": {"average_price": 0, "order_status": "open"},
        },
    )
    monkeypatch.setattr("services.open15_fill_reconcile.broker_pnl_by_symbol", lambda _k: {})

    res = reconcile_fills("2026-08-06")
    assert res["pending"] == 1 and res["reconciled"] == 0

    row = db_session.query(Open15Trade).filter(Open15Trade.id == row_id).first()
    assert row.entry_fill_price == 100.5 and row.exit_fill_price is None
    assert row.pnl == 300.0, "the quote-derived P&L stands until BOTH legs report"
    assert row.fill_reconcile_status == "pending"
    assert row.pnl_source is None
    db_session.remove()


def test_a_rejected_leg_is_unavailable_not_pending_forever(monkeypatch):
    from database.open15_breakout_db import Open15Trade, db_session
    from services.open15_fill_reconcile import reconcile_fills

    row_id = _insert_real_row()
    _stub_order_status(
        monkeypatch, {"E-1": {"average_price": 0, "order_status": "rejected"}, "X-1": {}}
    )
    monkeypatch.setattr("services.open15_fill_reconcile.broker_pnl_by_symbol", lambda _k: {})

    reconcile_fills("2026-08-06")
    row = db_session.query(Open15Trade).filter(Open15Trade.id == row_id).first()
    # a final answer, so the retry loop must stop asking
    assert row.fill_reconcile_status == "unavailable"
    db_session.remove()


def test_reconciled_rows_are_not_re_queried(monkeypatch):
    """Idempotence: the arm-time catch-up runs every day, forever."""
    from services.open15_fill_reconcile import reconcile_fills

    _insert_real_row(fill_reconcile_status="reconciled")
    calls = []
    _stub_order_status(monkeypatch, {})
    monkeypatch.setattr(
        "services.open15_fill_reconcile.fetch_fill",
        lambda oid, key: calls.append(oid) or None,
    )
    monkeypatch.setattr("services.open15_fill_reconcile.broker_pnl_by_symbol", lambda _k: {})

    reconcile_fills("2026-08-06")
    assert calls == [], "a settled row must never be asked about again"


def test_paper_and_sim_rows_are_never_reconciled(monkeypatch):
    """They have no orders. Asking about a blank order id is a broker call spent
    to learn nothing, and any answer it produced would be about another order."""
    from services.open15_fill_reconcile import reconcile_fills

    _insert_real_row(fill="paper", entry_order_id="", exit_order_id="")
    _insert_real_row(symbol="SAIL", fill="sim", entry_order_id="", exit_order_id="")
    calls = []
    _stub_order_status(monkeypatch, {})
    monkeypatch.setattr(
        "services.open15_fill_reconcile.fetch_fill", lambda oid, key: calls.append(oid) or None
    )
    monkeypatch.setattr("services.open15_fill_reconcile.broker_pnl_by_symbol", lambda _k: {})

    reconcile_fills("2026-08-06")
    assert calls == []


# --------------------------------------------------------------------------- #
# fill classes — sim money must never become real money
# --------------------------------------------------------------------------- #
def test_sim_rows_are_excluded_from_realized_pnl_and_reported_apart():
    """``total_realized_pnl`` sizes TOMORROW'S REAL ORDERS under compound sizing.

    Before #555 the real-fill predicate was ``fill != 'paper'``, which silently
    classified every future fill class as REAL — so adding ``sim`` would have
    compounded position size off money that was never made.
    """
    from database.open15_breakout_db import (
        insert_trade,
        paper_pnl_by_date,
        sim_pnl_by_date,
        total_realized_pnl,
        trades_pnl_by_date,
    )

    base = {"trade_date": "2026-08-06", "side": "L", "mode": "live", "quantity": 10}
    insert_trade(symbol="A", fill="real", pnl=1000.0, charges_inr=100.0, **base)
    insert_trade(symbol="B", fill="paper", pnl=500.0, charges_inr=50.0, **base)
    insert_trade(symbol="C", fill="sim", pnl=2000.0, charges_inr=200.0, **base)

    assert total_realized_pnl() == pytest.approx(900.0)
    assert trades_pnl_by_date() == {"2026-08-06": 900.0}
    assert paper_pnl_by_date() == {"2026-08-06": 450.0}
    assert sim_pnl_by_date() == {"2026-08-06": 1800.0}


def test_a_pre_555_row_with_no_fill_label_still_counts_as_real():
    """Backward compatibility: every row written before the column existed."""
    from database.open15_breakout_db import insert_trade, total_realized_pnl

    insert_trade(
        trade_date="2026-07-23",
        symbol="OIL",
        side="L",
        mode="sandbox",
        quantity=10,
        pnl=100.0,
        charges_inr=18.0,
    )
    assert total_realized_pnl() == pytest.approx(82.0)


# --------------------------------------------------------------------------- #
# simulated rows end-to-end, through the real tick pipeline
# --------------------------------------------------------------------------- #
def _frame(symbol, price, cumvol, h, m, s):
    return (
        f"NSE_{symbol}_LTP",
        json.dumps(
            {
                "ltp": price,
                "volume": cumvol,
                "exchange_timestamp": dt.datetime(2026, 8, 6, h, m, s).timestamp(),
            }
        ),
    )


def _now(h, m, s=0):
    import pytz

    return pytz.timezone("Asia/Kolkata").localize(dt.datetime(2026, 8, 6, h, m, s))


def _mk_option_service(orders, *, premiums, lot=650):
    """Option-mode service whose ATM contract is unaffordable at the slot size."""

    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = Open15BreakoutService(
        order_placer=placer,
        quote_fn=lambda sym, exch: premiums.pop(0) if premiums else None,
        quote_snapshot_fn=None,
    )
    svc.universe = {"AAA", "CCC", "ZZZ"}
    svc.core = Open15Core({"AAA": 100.0, "CCC": 100.0, "ZZZ": 100.0}, vol_mult=1.5, top_n=1)
    svc.day_status = "armed"
    svc._log_date = "2026-08-06"
    svc.day_config = resolve_day_config(
        {"margin_per_slot": 10_000, "sizing_mode": "fixed", "vol_mult": 1.5}, 0
    )
    svc.day_config["instrument"] = "atm_option"
    svc.day_config["max_trades"] = 3
    return svc


def _run_to_selection(svc):
    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("ZZZ", 101.0)):
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))
    svc._handle_raw(*_frame("CCC", 96.9, 6000, 9, 16, 15), _now(9, 16, 15))


def _trigger(svc, sym="AAA", h=9, m=17, s=12):
    """Push a symbol through its breakout level on a volume surge.

    Side-aware on purpose: a SHORT watch breaks its first-candle LOW, so driving
    every symbol through the HIGH silently no-ops on the short leg — which is
    exactly how the sim-cap test below passed while testing nothing.
    """
    fc = svc.core.sym[sym]["fc"]
    long_side = svc.core.selected.get(sym, "L") == "L"
    price = (fc["high"] + 0.5) if long_side else (fc["low"] - 0.5)
    svc._handle_raw(*_frame(sym, price, 6000 + 9000, h, m, s), _now(h, m, s))


def test_an_unaffordable_trigger_is_priced_at_one_lot_and_never_orders(monkeypatch):
    """The whole ask #4/#5 in one test.

    Before #555 this row stopped at "skipped: unaffordable" with an entry
    premium and nothing else — the day could not say whether the slot capital
    or the signal was what capped the strategy.
    """
    from database.open15_breakout_db import Open15Trade, db_session

    monkeypatch.setattr(
        "services.open15_option_shadow.resolve_atm_option",
        lambda *a, **k: {"symbol": "AAA25AUG26100CE", "strike": 100, "lotsize": 650},
    )
    orders = []
    # entry quote 44.0 (650 x 44 = 28,600 > the 10k slot -> unaffordable),
    # then the exit quote at flatten
    svc = _mk_option_service(orders, premiums=[44.0, 45.8])
    _run_to_selection(svc)
    _trigger(svc)
    svc.flatten()

    assert orders == [], "a trade we could not afford must never place an order"
    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row.status == "skipped" and row.reason == "unaffordable"
    assert row.fill == "sim"
    # `quantity` records what was ORDERED (nothing); `sim_quantity` is the
    # pricing size — conflating them is how a sim row starts looking like a trade
    assert row.quantity == 0 and row.sim_quantity == 650
    assert row.opt_entry_premium == 44.0 and row.opt_exit_premium == 45.8
    assert row.pnl == pytest.approx((45.8 - 44.0) * 650)
    assert row.charges_inr and row.charges_inr > 0
    assert row.pnl_source == "quote"
    assert row.fill_reconcile_status == "not_applicable"
    db_session.remove()


def test_a_sim_row_never_reads_the_position_book(monkeypatch):
    """No order was sent, so the book can only surface an unrelated position.

    The rejection path reads it because a timeout may have half-reached the
    exchange; a skip cannot have. Reading it anyway risks promoting a trade we
    never placed into a live square-off — the one failure this path must not
    have.
    """
    from database.open15_breakout_db import db_session

    monkeypatch.setattr(
        "services.open15_option_shadow.resolve_atm_option",
        lambda *a, **k: {"symbol": "AAA25AUG26100CE", "strike": 100, "lotsize": 650},
    )
    svc = _mk_option_service([], premiums=[44.0, 45.8])

    def _boom(symbol, exchange):
        raise AssertionError("the position book must not be read for a sim row")

    svc._broker_qty = _boom
    _run_to_selection(svc)
    _trigger(svc)
    svc.flatten()  # must not raise
    db_session.remove()


def test_the_sim_cap_bounds_a_day_of_unaffordable_triggers(monkeypatch):
    from database.open15_breakout_db import Open15Trade, db_session

    monkeypatch.setenv("OPEN15_PAPER_SIM_MAX", "1")
    monkeypatch.setattr(
        "services.open15_option_shadow.resolve_atm_option",
        lambda *a, **k: {"symbol": "X25AUG26100CE", "strike": 100, "lotsize": 650},
    )
    svc = _mk_option_service([], premiums=[44.0, 44.0, 45.8])
    _run_to_selection(svc)
    _trigger(svc, "AAA")
    _trigger(svc, "CCC", m=18)

    rows = {r.symbol: r for r in db_session.query(Open15Trade).all()}
    # BOTH triggers fired and both were journaled — the cap changes whether a row
    # is PRICED, never whether it is recorded. Asserting only on the sim rows
    # would pass just as well if the second trigger had never happened at all.
    assert set(rows) == {"AAA", "CCC"}
    assert rows["AAA"].fill == "sim" and rows["AAA"].sim_quantity == 650
    assert rows["CCC"].reason == "unaffordable", "still a real skip, just not priced"
    assert rows["CCC"].fill is None and rows["CCC"].sim_quantity is None
    db_session.remove()


def test_sim_can_be_switched_off_entirely(monkeypatch):
    """Rollback switch: the row must degrade to exactly the pre-#555 bare skip."""
    from database.open15_breakout_db import Open15Trade, db_session

    monkeypatch.setenv("OPEN15_SIM_SKIPPED_ENABLED", "false")
    monkeypatch.setattr(
        "services.open15_option_shadow.resolve_atm_option",
        lambda *a, **k: {"symbol": "AAA25AUG26100CE", "strike": 100, "lotsize": 650},
    )
    svc = _mk_option_service([], premiums=[44.0])
    _run_to_selection(svc)
    _trigger(svc)
    svc.flatten()

    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row.reason == "unaffordable"
    assert row.fill is None and row.sim_quantity is None and row.pnl is None
    db_session.remove()


# --------------------------------------------------------------------------- #
# the #552 invariant, now across all THREE buckets
# --------------------------------------------------------------------------- #
def test_each_bucket_digest_equals_the_rows_the_page_renders():
    """The regression shape that catches this whole class.

    A pre-#552 test pinned the digest at one number three lines above the row it
    rendered at another. So the assertion is not "the digest is X" — it is "the
    digest equals the sum of the rows", per bucket, which is the invariant that
    was never expressed.
    """
    from services.open15_log_view import selection_outcomes, summarize_day

    day = [
        {"ts": "09:10:00", "event": "armed", "mode": "live", "instrument": "atm_option"},
        {
            "ts": "09:16:00",
            "event": "selection",
            "selected": {"HAL": "L", "JUBL": "L", "SAIL": "L"},
            "gaps_pct": {"HAL": 2.3, "JUBL": 1.2, "SAIL": 2.27},
        },
        {
            "ts": "09:21:59",
            "event": "entry",
            "symbol": "HAL",
            "instrument": "option",
            "contract": "HAL25AUG264750CE",
            "premium": 158.9,
            "qty": 150,
            "trigger_price": 4770.9,
            "order_status": "success",
        },
        {
            "ts": "09:18:59",
            "event": "entry_rejected",
            "symbol": "JUBL",
            "instrument": "option",
            "contract": "JUBLFOOD25AUG26480CE",
            "qty": 1250,
            "entry_price": 16.45,
            "fill": "paper",
            "error": "static IP 403",
        },
        {
            "ts": "09:26:58",
            "event": "entry_skipped",
            "symbol": "SAIL",
            "reason": "unaffordable",
            "fill": "sim",
            "opt_symbol": "SAIL25AUG26180CE",
            "opt_entry_premium": 5.76,
            "sim_quantity": 4700,
        },
        {
            "ts": "09:30:03",
            "event": "exit",
            "symbol": "HAL",
            "instrument": "option",
            "qty": 150,
            "exit_price": 197.9,
            "stock_exit_price": 4835.0,
            "gross": 5850.0,
            "charges": 287.76,
            "pnl": 5562.24,
        },
        {
            "ts": "09:30:03",
            "event": "exit_paper",
            "symbol": "JUBL",
            "instrument": "option",
            "qty": 1250,
            "exit_price": 16.2,
            "gross": -312.5,
            "charges": 229.22,
            "pnl": -541.72,
            "fill": "paper",
        },
        {
            "ts": "09:30:03",
            "event": "exit_sim",
            "symbol": "SAIL",
            "instrument": "option",
            "qty": 4700,
            "exit_price": 5.71,
            "gross": -235.0,
            "charges": 236.0,
            "pnl": -471.0,
            "fill": "sim",
        },
    ]
    digest = summarize_day("2026-08-06", day)
    rows = {r["symbol"]: r for r in selection_outcomes("2026-08-06", day)}

    for bucket, key in (("real", "pnl"), ("paper", "paper_pnl"), ("sim", "sim_pnl")):
        rendered = sum(
            r["pnl"]
            for r in rows.values()
            if r["pnl"] is not None and (r["fill"] or "real") == bucket
        )
        assert digest[key] == pytest.approx(rendered), f"{bucket} digest != rendered rows"

    # and the three are genuinely distinct — a blended figure would satisfy the
    # loop above only by accident
    assert digest["pnl"] == pytest.approx(5562.24)
    assert digest["paper_pnl"] == pytest.approx(-541.72)
    assert digest["sim_pnl"] == pytest.approx(-471.0)
    assert (digest["entered"], digest["paper"], digest["sim"]) == (1, 1, 1)


OPTION_DAY = [
    {
        "ts": "09:16",
        "event": "selection",
        # a skipped symbol is always a WATCHED one — both parsers key their rows
        # off the selection event, so omitting SAIL here would drop its row and
        # the parity assertion below would agree on nothing
        "selected": {"HAL": "L", "SAIL": "L"},
        "gaps_pct": {"HAL": 2.3, "SAIL": 2.27},
    },
    {
        "ts": "09:21",
        "event": "entry",
        "symbol": "HAL",
        "instrument": "option",
        "contract": "HAL25AUG264750CE",
        "premium": 158.9,
        "trigger_price": 4770.9,
        "qty": 150,
        "order_status": "success",
    },
    {
        "ts": "09:26",
        "event": "entry_skipped",
        "symbol": "SAIL",
        "reason": "unaffordable",
        "fill": "sim",
        "opt_symbol": "SAIL25AUG26180CE",
        "opt_entry_premium": 5.76,
        "sim_quantity": 4700,
    },
    {
        "ts": "09:30",
        "event": "exit",
        "symbol": "HAL",
        "instrument": "option",
        "exit_price": 197.9,
        "stock_exit_price": 4835.0,
        "pnl": 5562.24,
    },
]

# The journal — authoritative for prices and P&L (issue #557). Deliberately
# carries NO matching `fill_reconcile_row` event: this is the sealed-log case,
# where reconciliation landed after the day was written, which is precisely
# what the event-sourced version could not represent.
OPTION_JOURNAL = [
    {
        "symbol": "HAL",
        "instrument": "option",
        "opt_symbol": "HAL25AUG264750CE",
        "opt_entry_premium": 158.9,
        "opt_exit_premium": 197.9,
        "entry_fill_price": 159.35,
        "exit_fill_price": 197.2,
        "pnl_source": "fill",
        "fill": "real",
        "quantity": 150,
        "trigger_price": 4770.9,
        "exit_price": 4835.0,
        "pnl": 5677.5,
        "charges_inr": 115.26,
    },
    {
        "symbol": "SAIL",
        "instrument": "option",
        "opt_symbol": "SAIL25AUG26180CE",
        "opt_entry_premium": 5.76,
        "fill": "sim",
        "quantity": 0,
        "sim_quantity": 4700,
        "trigger_price": 178.9,
        "reason": "unaffordable",
    },
]


def test_logs_page_js_and_python_agree_on_the_new_fields():
    """The page duplicates ``selection_outcomes`` in JS — they must not drift.

    That duplication is exactly what let #545 be fixed in Python while the page
    kept rendering the old thing, so the parity check is extended to every field
    #555 adds rather than trusting the two implementations to stay aligned.
    """
    from services.open15_log_view import selection_outcomes
    from test.test_open15_log_view import _run_render_sel

    js = _run_render_sel(OPTION_DAY, journal=OPTION_JOURNAL)
    py = {
        r["symbol"]: r for r in selection_outcomes("2026-08-06", OPTION_DAY, journal=OPTION_JOURNAL)
    }
    assert set(js) == set(py)
    assert js["HAL"]["instr"] == py["HAL"]["instrument"] == "option"
    assert js["HAL"]["contract"] == py["HAL"]["opt_symbol"] == "HAL25AUG264750CE"
    assert js["HAL"]["optEntry"] == py["HAL"]["opt_entry_premium"] == 158.9
    assert js["HAL"]["optExit"] == py["HAL"]["opt_exit_premium"] == 197.9
    assert js["HAL"]["stockEntry"] == py["HAL"]["trigger_price"] == 4770.9
    assert js["HAL"]["entryFill"] == py["HAL"]["entry_fill_price"] == 159.35
    assert js["HAL"]["exitFill"] == py["HAL"]["exit_fill_price"] == 197.2
    assert js["HAL"]["pnlSource"] == py["HAL"]["pnl_source"] == "fill"
    # a sim row is a watched symbol with an outcome, not a dropped one
    assert js["SAIL"]["fill"] == py["SAIL"]["fill"] == "sim"
    assert js["SAIL"]["contract"] == py["SAIL"]["opt_symbol"] == "SAIL25AUG26180CE"


def test_the_whole_logs_page_script_parses():
    """A syntax error anywhere in the page JS blanks the ENTIRE page silently.

    The parity harness above executes one extracted function, so it cannot see a
    break in the rest of the file — and the page has no build step or linter in
    front of it.
    """
    import shutil
    import subprocess

    from blueprints.open15_breakout import _LOGS_PAGE

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    blocks = _LOGS_PAGE.split("<script>")[1:]
    assert blocks, "the page must contain script blocks"
    for i, block in enumerate(blocks):
        src = block.split("</script>")[0]
        # parse-only: the page's top-level calls need a DOM, which node has not
        # explicit utf-8: the page contains ⚠ / ₹ / – and Windows would otherwise
        # encode the subprocess pipe as cp1252 and raise before node ever runs
        res = subprocess.run(
            [node, "--check", "-"],
            input=src,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert res.returncode == 0, f"script block {i} does not parse:\n{res.stderr}"


def test_a_reconcile_after_the_log_was_sealed_still_reaches_the_rows():
    """Issue #557 — the case the event-sourced version structurally could not do.

    On 2026-08-06 the chip read +Rs5334 (journal) while the rows beneath it read
    +Rs316 and +Rs5850 (timeline). The rows were not merely out of date: the
    arm-time catch-up — the designed retry for legs the broker reports late —
    runs on a LATER day, so it can never append events to the day it corrects.
    Any fix that keeps sourcing row P&L from events fails this test forever.
    """
    from services.open15_log_view import selection_outcomes, summarize_day

    # a day log sealed BEFORE reconciliation: quote-derived, no reconcile events
    day = [
        {"ts": "09:16", "event": "selection", "selected": {"HAL": "L"}, "gaps_pct": {"HAL": 2.3}},
        {
            "ts": "09:21",
            "event": "entry",
            "symbol": "HAL",
            "instrument": "option",
            "contract": "HAL25AUG264750CE",
            "premium": 158.9,
            "trigger_price": 4770.9,
            "qty": 150,
            "order_status": "success",
        },
        {
            "ts": "09:30",
            "event": "exit",
            "symbol": "HAL",
            "instrument": "option",
            "exit_price": 197.9,
            "gross": 5850.0,
            "charges": 287.51,
            "pnl": 5562.49,
        },
    ]
    # ...and the journal as the reconcile later left it: fill-derived gross
    journal = [
        {
            "symbol": "HAL",
            "instrument": "option",
            "opt_symbol": "HAL25AUG264750CE",
            "opt_entry_premium": 158.9,
            "opt_exit_premium": 197.9,
            "entry_fill_price": 158.5,
            "exit_fill_price": 197.9,
            "pnl_source": "fill",
            "fill": "real",
            "quantity": 150,
            "pnl": 5910.0,
            "charges_inr": 287.51,
        }
    ]
    net = 5910.0 - 287.51  # 5622.49

    rows = selection_outcomes("2026-08-06", day, journal=journal)
    assert len(rows) == 1
    assert rows[0]["pnl"] == pytest.approx(net), "the row must show the RECONCILED net"
    assert rows[0]["entry_fill_price"] == 158.5
    assert rows[0]["pnl_source"] == "fill"

    # and the chip — which reads the journal via trades_pnl — must agree with it
    digest = summarize_day("2026-08-06", day, trades_pnl=net)
    rendered = sum(r["pnl"] for r in rows if r["pnl"] is not None)
    assert digest["pnl"] == pytest.approx(rendered), "chip != rows is the #557 bug"


def test_the_outcome_text_never_repeats_a_stale_pnl():
    """Found in live validation of #557/#559, on the merged page.

    MUTHOOTFIN's P&L column correctly read the reconciled **-Rs288.15** while the
    outcome cell in the SAME ROW still read "entered 09:20:33 -> +Rs316" — the
    event's figure, appended as prose. The #557 overlay fixes data fields; a
    number baked into a rendered string is invisible to it. The `net P&L` column
    owns the number now, so the outcome text must carry none.
    """
    from test.test_open15_log_view import _run_render_sel

    day = [
        {"ts": "09:16", "event": "selection", "selected": {"HAL": "L"}, "gaps_pct": {"HAL": 2.3}},
        {
            "ts": "09:21",
            "event": "entry",
            "symbol": "HAL",
            "trigger_price": 4770.9,
            "at": "09:21:59",
            "order_status": "success",
        },
        # the STALE event value the reconcile later superseded
        {"ts": "09:30", "event": "exit", "symbol": "HAL", "exit_price": 197.9, "pnl": 5850.0},
    ]
    journal = [
        {
            "symbol": "HAL",
            "fill": "real",
            "quantity": 150,
            "pnl": 5910.0,
            "charges_inr": 287.51,
            "pnl_source": "fill",
        }
    ]
    row = _run_render_sel(day, journal=journal)["HAL"]

    assert "5850" not in row["out"], "the outcome text must not carry the stale event P&L"
    assert "316" not in row["out"]
    assert row["net"] == pytest.approx(5622.49), "the P&L column owns the number"
    assert "entered" in row["out"], "the outcome still says what happened"


def test_the_journal_never_invents_a_row_for_a_symbol_that_never_triggered():
    """The overlay corrects rows; it must not create them.

    A journal row whose symbol is absent from the day's watch list would
    otherwise appear as an outcome with no gap, no volume and no reason —
    indistinguishable from a selection the log failed to record.
    """
    from services.open15_log_view import selection_outcomes

    day = [{"ts": "09:16", "event": "selection", "selected": {"HAL": "L"}, "gaps_pct": {}}]
    journal = [{"symbol": "NOTWATCHED", "pnl": 999.0, "charges_inr": 0.0, "fill": "real"}]
    rows = selection_outcomes("2026-08-06", day, journal=journal)
    assert [r["symbol"] for r in rows] == ["HAL"]


def test_the_overlay_reports_net_not_the_journals_gross():
    """The #552 trap, inside the #557 fix.

    The journal stores GROSS in `pnl` with charges separate; every number the
    page and the digest show is NET. Copying `pnl` across would put gross in the
    rows and net in the chip — reintroducing the exact divergence being fixed.
    """
    from services.open15_log_view import selection_outcomes

    day = [{"ts": "09:16", "event": "selection", "selected": {"X": "L"}, "gaps_pct": {}}]
    journal = [{"symbol": "X", "pnl": 1000.0, "charges_inr": 250.0, "fill": "real"}]
    row = selection_outcomes("2026-08-06", day, journal=journal)[0]
    assert row["pnl"] == pytest.approx(750.0), "gross 1000 - charges 250"


def test_both_legs_reach_the_csv_for_an_option_day():
    """Ask #1: in option mode the stock price and the premium are different
    facts, and a row carrying one of them cannot be reconciled to its own P&L."""
    from services.open15_log_view import selection_outcomes

    day = [
        {"ts": "09:16", "event": "selection", "selected": {"HAL": "L"}, "gaps_pct": {"HAL": 2.3}},
        {
            "ts": "09:21",
            "event": "entry",
            "symbol": "HAL",
            "instrument": "option",
            "contract": "HAL25AUG264750CE",
            "premium": 158.9,
            "trigger_price": 4770.9,
            "qty": 150,
            "order_status": "success",
        },
        {
            "ts": "09:30",
            "event": "exit",
            "symbol": "HAL",
            "instrument": "option",
            "exit_price": 197.9,
            "stock_exit_price": 4835.0,
            "pnl": 5562.24,
        },
    ]
    journal = [
        {
            "symbol": "HAL",
            "instrument": "option",
            "opt_symbol": "HAL25AUG264750CE",
            "opt_entry_premium": 158.9,
            "opt_exit_premium": 197.9,
            "entry_fill_price": 159.35,
            "exit_fill_price": 197.2,
            "pnl_source": "fill",
            "fill": "real",
            "quantity": 150,
            "trigger_price": 4770.9,
        }
    ]
    row = selection_outcomes("2026-08-06", day, journal=journal)[0]
    assert row["trigger_price"] == 4770.9  # the stock leg
    assert row["opt_symbol"] == "HAL25AUG264750CE"
    assert row["opt_entry_premium"] == 158.9 and row["opt_exit_premium"] == 197.9
    assert row["entry_fill_price"] == 159.35 and row["exit_fill_price"] == 197.2
    assert row["pnl_source"] == "fill"
    assert row["instrument"] == "option"
