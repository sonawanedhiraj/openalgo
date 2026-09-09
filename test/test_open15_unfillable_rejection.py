"""A structural broker refusal is never a PAPER fill (issue #715).

MAXHEALTH, 2026-09-09: Zerodha refused the entry under its OI floor (#595 —
fewer than 500 lots of open interest on the contract). The #548 path filed it
as a PAPER fill and priced it at 09:30 to +Rs15,855. Paper answers "what would
this order have done had the broker not refused it", which only means
something when the SAME order could have filled; an OI-floor refusal is a
different claim — the contract cannot be bought under ANY variant of the
strategy — so the row measured nothing and polluted the paper bucket. Nine
rows across five days carried the same message.

Drives the same production pipeline as ``test_open15_rejected_entry`` (raw ZMQ
frames through ``_handle_raw``, the order placer as the only seam).
"""

import datetime as dt
import json

import pytest

from services.open15_breakout_service import Open15BreakoutService, Open15Core, resolve_day_config

OI_MSG = (
    "MIS LIMIT orders are blocked for this MAXHEALTH contract due to its open "
    "interest (OI) being less than 500 lots. [Read more.](https://support.zerodha.com/"
    "category/trading-and-markets/alerts-and-nudges/kite-error-messages/articles/"
    "oi-based-restrictions)"
)
IP_MSG = (
    "IP (122.169.47.35) is not allowed to place orders for this app. "
    "Update allowed IPs on the Kite developer console."
)
DATE = "2026-09-09"


@pytest.fixture(autouse=True)
def _clean_journal():
    """Every test writes rows for the same trade_date (issue #553)."""
    from database.open15_breakout_db import Open15DayLog, Open15Trade, db_session, init_db

    init_db()
    db_session.query(Open15Trade).delete()
    db_session.query(Open15DayLog).delete()
    db_session.commit()
    db_session.remove()
    yield
    db_session.query(Open15Trade).delete()
    db_session.query(Open15DayLog).delete()
    db_session.commit()
    db_session.remove()


def _frame(symbol, price, cumvol, h, m, s):
    payload = json.dumps(
        {
            "ltp": price,
            "volume": cumvol,
            "exchange_timestamp": dt.datetime(2026, 9, 9, h, m, s).timestamp(),
        }
    )
    return f"NSE_{symbol}_LTP", payload


def _now(h, m, s=0):
    import pytz

    return pytz.timezone("Asia/Kolkata").localize(dt.datetime(2026, 9, 9, h, m, s))


def _mk_service(orders, *, reply, max_trades=3):
    """Service whose broker answers each order with ``reply(order) -> dict``."""

    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return reply(order)

    svc = Open15BreakoutService(order_placer=placer)
    svc.universe = {"AAA", "CCC", "ZZZ"}
    svc.core = Open15Core({"AAA": 100.0, "CCC": 100.0, "ZZZ": 100.0}, vol_mult=1.5, top_n=1)
    svc.day_status = "armed"
    svc._log_date = DATE
    svc.day_config = resolve_day_config(
        {"margin_per_slot": 30000, "sizing_mode": "fixed", "vol_mult": 1.5}, 0
    )
    svc.day_config["max_trades"] = max_trades
    svc._broker_qty = lambda symbol, exchange: 0  # a refused order never reaches the book
    svc._alert_rejection = lambda *a, **k: None
    return svc


def _run_to_selection(svc):
    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("ZZZ", 101.0)):
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))
    svc._handle_raw(*_frame("CCC", 96.9, 6000, 9, 16, 15), _now(9, 16, 15))


def _trigger(svc, sym="AAA", h=9, m=17, s=12):
    fc = svc.core.sym[sym]["fc"]
    px = fc["high"] + 0.5 if sym == "AAA" else fc["low"] - 0.5
    svc._handle_raw(*_frame(sym, px, 6000 + 9000, h, m, s), _now(h, m, s))


def _rows():
    from database.open15_breakout_db import Open15Trade, db_session

    db_session.expire_all()
    return {r.symbol: r for r in db_session.query(Open15Trade).all()}


# --------------------------------------------------------------------------- #
# the classifier
# --------------------------------------------------------------------------- #
def test_classifier_names_the_oi_floor_and_nothing_else():
    from services.open15_liquidity import classify_unfillable_rejection as c

    assert c(OI_MSG) == "oi_below_broker_min"
    # the claim, not the exact prose: a wording tweak on the broker's side must
    # not silently re-open the paper bucket
    assert c("blocked: open interest below 500 lots") == "oi_below_broker_min"
    # transient refusals stay paper — the same order COULD have filled
    assert c(IP_MSG) is None
    assert c("Insufficient funds. Margin required: 149255.00.") is None
    assert c("broker reported the entry rejected") is None
    assert c(None) is None and c("") is None


# --------------------------------------------------------------------------- #
# placement-time refusal
# --------------------------------------------------------------------------- #
def test_oi_floor_rejection_is_recorded_but_never_priced():
    """The MAXHEALTH shape, fixed: no paper row, no exit, no P&L bucket."""
    from database.open15_breakout_db import paper_pnl_by_date, total_realized_pnl

    orders = []
    svc = _mk_service(orders, reply=lambda o: {"status": "error", "message": OI_MSG})
    _run_to_selection(svc)
    _trigger(svc)

    assert len(orders) == 1, "the broker is still the authority — the order is attempted"
    row = _rows()["AAA"]
    assert row.status == "rejected"
    assert row.fill == "none", "not paper: this contract could never have filled"
    assert row.reason == "entry_rejected_unfillable"
    assert row.error_message == OI_MSG, "the broker's reason is kept"
    assert row.pnl is None and row.exit_price is None
    assert "AAA" not in svc.positions, "never registered, so flatten cannot price it"
    assert svc._count_fills() == (0, 0, 0, 0)

    ev = [e for e in svc.day_log if e["event"] == "entry_rejected"]
    assert len(ev) == 1, "the existing event name — nothing new for the page to learn"
    assert ev[0]["fill"] == "none" and ev[0]["unfillable"] == "oi_below_broker_min"
    assert ev[0]["paper_capped"] is False and ev[0]["slot_released"] is True

    before = total_realized_pnl()
    svc.core.last_price["AAA"] = 130.0  # a huge "would-have-been" gain
    svc.flatten("eod_0930")
    assert len(orders) == 1, "nothing to square off"
    row = _rows()["AAA"]
    assert row.pnl is None and row.exit_ts is None, "the gain was never obtainable"
    assert not [e for e in svc.day_log if e["event"] == "exit_paper"]
    assert total_realized_pnl() == before
    assert DATE not in paper_pnl_by_date()

    summ = [e for e in svc.day_log if e["event"] == "summary"]
    svc.summary()
    summ = [e for e in svc.day_log if e["event"] == "summary"][-1]
    assert (summ["paper"], summ["unfillable"]) == (0, 1)


def test_unfillable_does_not_spend_the_paper_cap_or_the_trade_cap():
    """An unfillable refusal is not a paper event: a later transient rejection
    is still priced, and the max_trades slot is free for the next trigger."""
    orders = []

    def reply(order):
        return {"status": "error", "message": OI_MSG if order["symbol"] == "AAA" else IP_MSG}

    svc = _mk_service(orders, reply=reply, max_trades=1)
    _run_to_selection(svc)
    _trigger(svc, "AAA")
    _trigger(svc, "CCC", 9, 18, 5)

    assert len(orders) == 2, "the unfillable AAA slot is free for CCC to try"
    rows = _rows()
    assert rows["AAA"].fill == "none" and rows["AAA"].reason == "entry_rejected_unfillable"
    # CCC's refusal is transient (static IP) — the same order could have filled,
    # so it IS paper, and it is priced because AAA never spent the paper cap
    assert rows["CCC"].fill == "paper" and rows["CCC"].reason == "entry_rejected"
    assert "CCC" in svc.positions and "AAA" not in svc.positions
    assert svc._count_fills() == (0, 1, 0, 0)


# --------------------------------------------------------------------------- #
# post-ACK refusal (issue #626 seam)
# --------------------------------------------------------------------------- #
def test_post_ack_oi_floor_rejection_leaves_the_run_entirely(monkeypatch):
    """ACK'd, then refused under the OI floor: out of positions, not paper."""
    import services.open15_fill_reconcile as recon

    orders = []
    svc = _mk_service(orders, reply=lambda o: {"status": "success", "orderid": "T-1"})
    _run_to_selection(svc)
    _trigger(svc)
    assert svc._count_fills()[0] == 1

    monkeypatch.setattr(
        recon,
        "fetch_fill",
        lambda _oid, _key: {"price": None, "qty": 0, "order_status": "rejected", "message": OI_MSG},
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "database.auth_db",
        type("M", (), {"get_first_available_api_key": staticmethod(lambda: "k")}),
    )
    assert svc.verify_entries() == 1

    assert "AAA" not in svc.positions, "a `none` row left in positions would count as REAL"
    assert svc._count_fills() == (0, 0, 0, 0)
    row = _rows()["AAA"]
    assert row.status == "rejected" and row.fill == "none"
    assert row.reason == "entry_rejected_unfillable"
    ev = [e for e in svc.day_log if e["event"] == "entry_rejected"]
    assert len(ev) == 1 and ev[0]["post_ack"] is True
    assert ev[0]["fill"] == "none" and ev[0]["unfillable"] == "oi_below_broker_min"

    svc.flatten("eod_0930")
    assert len(orders) == 1, "no exit for a position that never existed"
    assert _rows()["AAA"].pnl is None


# --------------------------------------------------------------------------- #
# the page: digest + row builder keep it apart from paper
# --------------------------------------------------------------------------- #
def _events(unfillable=True, post_ack=False):
    ev = [
        {"ts": "09:10:00.000", "event": "armed", "universe": 5, "mode": "live"},
        {
            "event": "selection",
            "selected": {"MAXHEALTH": "L", "MCX": "L"},
            "gaps_pct": {"MAXHEALTH": 1.2, "MCX": 0.6},
        },
    ]
    if post_ack:
        ev.append(
            {
                "ts": "09:17:57.000",
                "event": "entry",
                "symbol": "MAXHEALTH",
                "qty": 2100,
                "order_status": "success",
                "order_id": "X",
            }
        )
    ev.append(
        {
            "ts": "09:17:58.000",
            "event": "entry_rejected",
            "symbol": "MAXHEALTH",
            "instrument": "option",
            "contract": "MAXHEALTH29SEP261010CE",
            "qty": 2100,
            "entry_price": 26.9,
            "error": OI_MSG,
            "fill": "none" if unfillable else "paper",
            "paper_capped": False,
            "unfillable": "oi_below_broker_min" if unfillable else None,
            "slot_released": True,
            **({"post_ack": True} if post_ack else {}),
        }
    )
    ev.append(
        {
            "ts": "09:18:00.000",
            "event": "entry",
            "symbol": "MCX",
            "qty": 250,
            "order_status": "success",
        }
    )
    return ev


def test_digest_counts_unfillable_apart_from_paper_and_not_as_entered():
    from services.open15_log_view import summarize_day

    d = summarize_day(DATE, _events())
    assert (d["entered"], d["paper"], d["unfillable"]) == (1, 0, 1)
    assert d["paper_pnl"] is None
    # post-ACK: the symbol emits BOTH `entry` and `entry_rejected` (#626) and
    # must not be counted as entered
    d = summarize_day(DATE, _events(post_ack=True))
    assert (d["entered"], d["paper"], d["unfillable"]) == (1, 0, 1)
    # the pre-#715 shape is unchanged
    d = summarize_day(DATE, _events(unfillable=False))
    assert (d["entered"], d["paper"], d["unfillable"]) == (1, 1, 0)


def test_outcome_row_has_no_fill_class_and_no_pnl():
    from services.open15_log_view import selection_outcomes

    rows = {r["symbol"]: r for r in selection_outcomes(DATE, _events())}
    r = rows["MAXHEALTH"]
    assert r["entered"] is False and r["fill"] == "none"
    assert r["skip_reason"] == "entry_rejected_unfillable"
    assert r["unfillable"] == "oi_below_broker_min"
    assert r.get("pnl") is None
    assert r["error_message"] == OI_MSG


def test_logs_page_js_renders_the_unfillable_shape():
    """The row builder and the banner must know the shape (#615/#622 rule)."""
    import inspect

    import blueprints.open15_breakout as bp

    src = inspect.getsource(bp)
    assert "e.unfillable" in src and "b-unfill" in src
    assert "d.unfillable" in src, "history sidebar"
    assert "summ.unfillable" in src, "chips"


# --------------------------------------------------------------------------- #
# the one-off repair for rows filed before #715
# --------------------------------------------------------------------------- #
def _seed_pre_715_day():
    from database.open15_breakout_db import insert_trade, save_day_log

    rid = insert_trade(
        trade_date=DATE,
        symbol="MAXHEALTH",
        side="L",
        mode="live",
        instrument="option",
        opt_symbol="MAXHEALTH29SEP261010CE",
        quantity=2100,
        trigger_price=1012.8,
        opt_entry_premium=26.9,
        opt_exit_premium=34.75,
        entry_order_id="",
        entry_status="error",
        status="rejected",
        fill="paper",
        reason="entry_rejected",
        error_message=OI_MSG,
        exit_ts="2026-09-09T09:30:00+05:30",
        exit_status="not_placed",
        exit_order_id="",
        pnl=16485.0,
        charges_inr=629.81,
        pnl_source="quote",
    )
    # a transient rejection on the same day stays exactly as it is
    keep = insert_trade(
        trade_date=DATE,
        symbol="DLF",
        side="L",
        mode="live",
        quantity=100,
        trigger_price=800.0,
        entry_order_id="",
        entry_status="error",
        status="rejected",
        fill="paper",
        reason="entry_rejected",
        error_message=IP_MSG,
        pnl=-120.0,
        charges_inr=40.0,
    )
    events = [
        {"ts": "09:10:00.000", "event": "armed", "universe": 207, "mode": "live"},
        {
            "ts": "09:17:57.845",
            "event": "entry_rejected",
            "symbol": "MAXHEALTH",
            "instrument": "option",
            "contract": "MAXHEALTH29SEP261010CE",
            "qty": 2100,
            "entry_price": 26.9,
            "error": OI_MSG,
            "fill": "paper",
            "paper_capped": False,
            "slot_released": True,
        },
        {
            "ts": "09:18:00.000",
            "event": "entry_rejected",
            "symbol": "DLF",
            "qty": 100,
            "entry_price": 800.0,
            "error": IP_MSG,
            "fill": "paper",
            "paper_capped": False,
            "slot_released": True,
        },
        {
            "ts": "09:30:00.396",
            "event": "exit_paper",
            "symbol": "MAXHEALTH",
            "instrument": "option",
            "exit_price": 34.75,
            "gross": 16485.0,
            "charges": 629.81,
            "pnl": 15855.19,
            "fill": "paper",
        },
        {
            "ts": "09:30:00.500",
            "event": "exit_paper",
            "symbol": "DLF",
            "exit_price": 799.0,
            "gross": -100.0,
            "charges": 20.0,
            "pnl": -120.0,
            "fill": "paper",
        },
        {"ts": "09:35:00.033", "event": "summary", "entered": 2, "filled": 0, "paper": 2},
    ]
    assert save_day_log(DATE, events)
    return rid, keep


def test_repair_dry_run_writes_nothing():
    from database.open15_breakout_db import get_day_log
    from services.open15_unfillable_repair import repair

    _seed_pre_715_day()
    res = repair(DATE, apply=False)
    assert [r["symbol"] for r in res["found"]] == ["MAXHEALTH"]
    assert res["found"][0]["unfillable"] == "oi_below_broker_min"
    assert "dry-run" in res["status"]
    rows = _rows()
    assert rows["MAXHEALTH"].fill == "paper" and rows["MAXHEALTH"].pnl == 16485.0
    assert any(e["event"] == "exit_paper" for e in get_day_log(DATE))


def test_repair_apply_reclassifies_row_and_day_log_and_is_idempotent():
    from database.open15_breakout_db import get_day_log, paper_pnl_by_date
    from services.open15_log_view import summarize_day
    from services.open15_unfillable_repair import repair

    _seed_pre_715_day()
    assert paper_pnl_by_date()[DATE] == round(16485.0 - 629.81 + (-120.0 - 40.0), 2)

    res = repair(DATE, apply=True)
    assert res["status"] == "repaired" and res["found"][0]["written"] is True

    rows = _rows()
    m = rows["MAXHEALTH"]
    assert m.fill == "none" and m.reason == "entry_rejected_unfillable"
    assert m.status == "rejected" and m.error_message == OI_MSG, "the broker's reason survives"
    assert m.pnl is None and m.charges_inr is None and m.exit_price is None
    assert m.opt_exit_premium is None and m.exit_ts is None and m.pnl_source is None
    assert m.fill_reconcile_status == "not_applicable"
    # the transient rejection beside it is untouched
    d = rows["DLF"]
    assert d.fill == "paper" and d.reason == "entry_rejected" and d.pnl == -120.0
    assert paper_pnl_by_date()[DATE] == round(-120.0 - 40.0, 2)

    events = get_day_log(DATE)
    rej = {e["symbol"]: e for e in events if e["event"] == "entry_rejected"}
    assert rej["MAXHEALTH"]["fill"] == "none"
    assert rej["MAXHEALTH"]["unfillable"] == "oi_below_broker_min"
    assert rej["MAXHEALTH"]["repaired"] == "715"
    assert rej["DLF"]["fill"] == "paper" and "unfillable" not in rej["DLF"]
    exits = [e["symbol"] for e in events if e["event"] == "exit_paper"]
    assert exits == ["DLF"], "the paper exit that should never have existed is gone"
    summ = next(e for e in events if e["event"] == "summary")
    assert (summ["paper"], summ["unfillable"]) == (1, 1)
    # what the /logs page will now derive from the repaired log
    dig = summarize_day(DATE, events)
    assert (dig["paper"], dig["unfillable"]) == (1, 1)
    assert dig["paper_pnl"] == -120.0

    again = repair(DATE, apply=True)
    assert again["status"] == "nothing to repair"
