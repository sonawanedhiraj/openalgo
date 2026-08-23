"""Broker-rejected entries become terminal PAPER fills (issue #548).

The 2026-08-05 incident: Zerodha rejected three live option entries with a
static-IP 403. The rejection message was discarded, the rows sat at
``status='error'`` forever because ``flatten`` only looked at ``status='open'``,
the daily ``max_trades`` cap was consumed by trades that never existed, and
nothing alerted. These tests pin every part of the fix.

Drives the same production pipeline as ``test_open15_breakout_e2e`` — raw ZMQ
frames through ``_handle_raw`` — with the order placer as the only seam, so a
regression in the wiring surfaces here rather than in market.
"""

import datetime as dt
import json

import pytest

from services.open15_breakout_service import Open15BreakoutService, Open15Core, resolve_day_config


@pytest.fixture(autouse=True)
def _clean_journal():
    """Empty the journal between tests.

    Every test here writes rows for the same ``trade_date`` (``_log_date``,
    2026-08-05 — see ``_trade_date`` and issue #553), so without this a query
    for "the AAA row" picks up a previous test's row and the file passes
    test-by-test but fails as a suite.
    """
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()
    yield
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()


REJECT_MSG = (
    "IP (122.169.47.35) is not allowed to place orders for this app. "
    "Update allowed IPs on the Kite developer console."
)


def _frame(symbol, price, cumvol, h, m, s):
    topic = f"NSE_{symbol}_LTP"
    payload = json.dumps(
        {
            "ltp": price,
            "volume": cumvol,
            "exchange_timestamp": dt.datetime(2026, 8, 5, h, m, s).timestamp(),
        }
    )
    return topic, payload


def _now(h, m, s=0):
    import pytz

    return pytz.timezone("Asia/Kolkata").localize(dt.datetime(2026, 8, 5, h, m, s))


def _mk_service(orders, *, reject=True, max_trades=3):
    """Service whose broker rejects (or accepts) every order."""

    def placer(mode, order):
        orders.append({"mode": mode, **order})
        if reject:
            return {"status": "error", "message": REJECT_MSG}
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = Open15BreakoutService(order_placer=placer)
    svc.universe = {"AAA", "CCC", "ZZZ"}
    svc.core = Open15Core({"AAA": 100.0, "CCC": 100.0, "ZZZ": 100.0}, vol_mult=1.5, top_n=1)
    svc.day_status = "armed"
    svc._log_date = "2026-08-05"
    svc.day_config = resolve_day_config(
        {"margin_per_slot": 30000, "sizing_mode": "fixed", "vol_mult": 1.5}, 0
    )
    svc.day_config["max_trades"] = max_trades
    # The book must AGREE with what the broker was made to do, or the fixture
    # describes a state that cannot exist (issue #626). Rejecting => provably
    # flat (the static-IP case, where the order never reached the exchange).
    # Accepting => a position exists; leaving this at 0 would describe an order
    # that was filled and simultaneously never filled, and the pre-exit check
    # now (correctly) papers exactly that.
    svc._broker_qty = (lambda symbol, exchange: 0) if reject else (lambda symbol, exchange: 300)
    return svc


def _run_to_selection(svc):
    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("ZZZ", 101.0)):
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))
    svc._handle_raw(*_frame("CCC", 96.9, 6000, 9, 16, 15), _now(9, 16, 15))


def _trigger(svc, sym="AAA", h=9, m=17, s=12):
    """Push the symbol through its level on a volume surge -> entry attempt."""
    level = svc.core.sym[sym]["fc"]["high"]
    svc._handle_raw(*_frame(sym, level + 0.5, 6000 + 9000, h, m, s), _now(h, m, s))
    return level


def test_rejection_is_captured_and_never_left_dangling():
    """The whole 2026-08-05 failure in one test: message kept, row terminal."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders)
    _run_to_selection(svc)
    _trigger(svc)

    assert len(orders) == 1, "the entry order is still attempted"
    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    # DEFECT 1 (pre-#548): reason/error were NULL and the text lived only in the log
    assert row.status == "rejected" and row.fill == "paper"
    assert row.entry_status == "error" and row.reason == "entry_rejected"
    assert row.error_message == REJECT_MSG

    ev = [e for e in svc.day_log if e["event"] == "entry_rejected"]
    assert len(ev) == 1
    assert ev[0]["error"] == REJECT_MSG and ev[0]["slot_released"] is True

    # DEFECT 2 (pre-#548): flatten skipped status!='open', so the row never closed
    svc.flatten("eod_0930")
    assert len(orders) == 1, "a rejected entry must NEVER be squared off — nothing was bought"
    db_session.expire_all()
    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row.status == "rejected" and row.exit_ts is not None
    assert row.exit_status == "not_placed"
    # the measurement survives: priced exactly as a sandbox run would have been
    assert row.exit_price is not None and row.pnl is not None and row.charges_inr is not None
    assert [e["event"] for e in svc.day_log].count("exit_paper") == 1
    db_session.remove()


def test_paper_pnl_never_reaches_realized_pnl_or_the_real_day_total():
    """Paper money must not compound tomorrow's position size."""
    from database.open15_breakout_db import (
        db_session,
        init_db,
        paper_pnl_by_date,
        total_realized_pnl,
        trades_pnl_by_date,
    )

    init_db()
    before = total_realized_pnl()
    svc = _mk_service([])
    _run_to_selection(svc)
    _trigger(svc)
    svc.core.last_price["AAA"] = 120.0  # a large "would-have-been" gain
    svc.flatten("eod_0930")

    assert total_realized_pnl() == before, "paper P&L must not drive compound sizing"
    assert "2026-08-05" not in trades_pnl_by_date()
    assert paper_pnl_by_date().get("2026-08-05") is not None
    db_session.remove()


def test_rejection_releases_its_max_trades_slot():
    """Pre-#548 three rejections consumed the whole daily cap."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders, max_trades=1)
    _run_to_selection(svc)
    _trigger(svc, "AAA")
    # CCC triggers next: with the cap consumed by a rejection it would be skipped
    lc = svc.core.sym["CCC"]["fc"]["low"]
    svc._handle_raw(*_frame("CCC", lc - 0.5, 6000 + 9000, 9, 18, 5), _now(9, 18, 5))

    assert len(orders) == 2, "the rejected AAA slot must be free for CCC to try"
    skips = db_session.query(Open15Trade).filter(Open15Trade.reason == "max_trades_cap").count()
    assert skips == 0
    db_session.remove()


def test_paper_fills_are_themselves_capped_at_max_trades():
    """A persistently-rejecting broker must not simulate an unbounded day."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    svc = _mk_service([], max_trades=1)
    _run_to_selection(svc)
    _trigger(svc, "AAA")
    lc = svc.core.sym["CCC"]["fc"]["low"]
    svc._handle_raw(*_frame("CCC", lc - 0.5, 6000 + 9000, 9, 18, 5), _now(9, 18, 5))

    rows = {
        r.symbol: r
        for r in db_session.query(Open15Trade).filter(Open15Trade.trade_date == "2026-08-05").all()
    }
    assert rows["AAA"].fill == "paper" and rows["AAA"].reason == "entry_rejected"
    # beyond the paper cap: still recorded with its error, but not priced
    assert rows["CCC"].fill == "none" and rows["CCC"].reason == "entry_rejected_paper_cap"
    assert rows["CCC"].error_message == REJECT_MSG
    # (real, paper, sim, shadow) — each non-real bucket is counted apart and
    # carries its own budget (issues #555, #581), so a simulated or shadow row
    # can never spend the paper budget and mask a broker rejecting everything
    assert svc._count_fills() == (0, 1, 0, 0)
    db_session.remove()


def test_an_order_that_actually_filled_is_squared_off_not_papered():
    """An ambiguous failure (timeout) whose lot DID reach the exchange."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders)
    svc._broker_qty = lambda symbol, exchange: 1500  # the book says we hold it
    _run_to_selection(svc)
    _trigger(svc)
    svc.core.last_price["AAA"] = 104.0

    svc.flatten("eod_0930")
    assert len(orders) == 2 and orders[1]["action"] == "SELL", "a real lot must be squared off"
    db_session.expire_all()
    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row.fill == "real" and row.status in ("closed", "error")
    assert any(e["event"] == "rejection_unverified" for e in svc.day_log)
    db_session.remove()


def test_unreadable_book_papers_rather_than_sending_a_naked_order():
    """An unknown book must never produce a square-off we cannot justify."""
    from database.open15_breakout_db import db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders)
    svc._broker_qty = lambda symbol, exchange: None  # broker session down
    _run_to_selection(svc)
    _trigger(svc)
    svc.core.last_price["AAA"] = 104.0

    svc.flatten("eod_0930")
    assert len(orders) == 1, "no square-off — that SELL would open a naked short"
    unverified = [e for e in svc.day_log if e["event"] == "rejection_unverified"]
    assert len(unverified) == 1 and unverified[0]["book_qty"] is None
    db_session.remove()


def test_accepted_orders_are_still_marked_real():
    """The happy path keeps working and is explicitly tagged ``real``."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    _run_to_selection(svc)
    _trigger(svc)

    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row.status == "open" and row.fill == "real" and row.error_message is None
    assert not [e for e in svc.day_log if e["event"] == "entry_rejected"]
    svc.flatten("eod_0930")
    assert len(orders) == 2
    db_session.expire_all()
    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row.status == "closed" and row.fill == "real"
    db_session.remove()


def test_alert_is_sent_once_per_day_not_once_per_rejection():
    """Three identical static-IP rejections are one alert, not three."""
    from database.open15_breakout_db import db_session, init_db

    init_db()
    sent = []
    svc = _mk_service([], max_trades=3)
    svc._alert_rejection = lambda symbol, qty, msg: sent.append(symbol) or None
    _run_to_selection(svc)
    _trigger(svc, "AAA")
    lc = svc.core.sym["CCC"]["fc"]["low"]
    svc._handle_raw(*_frame("CCC", lc - 0.5, 6000 + 9000, 9, 18, 5), _now(9, 18, 5))
    assert len(sent) == 2, "the hook fires per rejection"

    # the real implementation dedups by day
    svc2 = _mk_service([])
    calls = []
    svc2._rejection_alert_date = None
    orig_notify = svc2._alert_rejection
    svc2._alert_rejection = lambda s, q, m: (calls.append(s), orig_notify(s, q, m))[0]
    svc2._alert_rejection("AAA", 10, REJECT_MSG)
    first = svc2._rejection_alert_date
    svc2._alert_rejection("CCC", 10, REJECT_MSG)
    assert first is not None and svc2._rejection_alert_date == first
    db_session.remove()


def test_summary_reports_filled_and_paper_separately():
    """Pre-#548 the summary read ``entered: 5`` on a day with zero fills."""
    from database.open15_breakout_db import db_session, init_db

    init_db()
    svc = _mk_service([])
    _run_to_selection(svc)
    _trigger(svc)
    svc.core.last_price["AAA"] = 104.0
    svc.flatten("eod_0930")
    svc.summary()

    summ = [e for e in svc.day_log if e["event"] == "summary"][-1]
    assert summ["filled"] == 0 and summ["paper"] == 1
    assert summ["entered"] == 1, "the trigger count is kept, just no longer alone"
    db_session.remove()


def _row_pnl(events) -> float:
    """Sum of the per-symbol row values the page actually renders."""
    from services.open15_log_view import selection_outcomes

    return round(
        sum(r["pnl"] for r in selection_outcomes("2026-08-05", events) if r.get("pnl") is not None),
        2,
    )


def test_digest_and_outcome_rows_keep_paper_apart():
    """The history sidebar must not show simulated money as a day's P&L."""
    from services.open15_log_view import selection_outcomes, summarize_day

    events = [
        {"event": "armed", "mode": "live"},
        {"event": "selection", "selected": {"AAA": "L"}, "gaps_pct": {"AAA": 3.0}},
        {
            "event": "entry_rejected",
            "symbol": "AAA",
            "qty": 100,
            "entry_price": 103.5,
            "error": REJECT_MSG,
            "fill": "paper",
        },
        {"event": "exit_paper", "symbol": "AAA", "exit_price": 105.0, "gross": 150.0, "pnl": 120.0},
        {"event": "summary", "day": "done", "selected": 1, "filled": 0, "paper": 1},
    ]
    dig = summarize_day("2026-08-05", events)
    assert dig["entered"] == 0 and dig["paper"] == 1
    assert dig["pnl"] is None, "a rejected day has no real P&L"
    # NET (120), not the event's gross (150) — issue #552. This assertion used
    # to read 150.0 three lines above a row assertion of 120.0, i.e. the header
    # and the table disagreeing was pinned here as correct.
    assert dig["paper_pnl"] == 120.0 == _row_pnl(events)

    row = selection_outcomes("2026-08-05", events)[0]
    assert row["fill"] == "paper" and row["entered"] is False
    assert row["error_message"] == REJECT_MSG and row["pnl"] == 120.0


def test_journal_row_and_day_log_share_one_date_key():
    """A row must never be filed under a different date than its day log (#553).

    Pre-fix the journal read the wall clock while the day log used the arm-time
    `_log_date`, so these two tests passed only on 2026-08-05 itself.
    """
    from database.open15_breakout_db import Open15Trade, db_session, get_day_log, init_db

    init_db()
    svc = _mk_service([])
    _run_to_selection(svc)
    _trigger(svc)

    rows = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").all()
    assert rows, "the trigger journaled nothing"
    assert {r.trade_date for r in rows} == {svc._log_date}
    assert get_day_log(svc._log_date), "the day log is filed under the same key"
    db_session.remove()


# --------------------------------------------------------------------------- #
# issue #626 — an ACK is not a fill, and flatten must not sell what we never got
#
# #548 covered the rejection the broker gives at PLACEMENT time (the static-IP
# 403 above): `ok` is False, the row is papered immediately, done. It does not
# cover the shape that hit TIINDIA on 2026-08-18 — Zerodha returned HTTP 200
# with an order id, and RMS refused the order afterwards. The row was written
# `status='open'`, so `flatten` sent a SELL for 800 calls we did not own. Kite
# priced that as a naked short (Rs4.45L SPAN) and refused it too; with funds
# available it would have opened a real short position.
# --------------------------------------------------------------------------- #
def test_an_acknowledged_but_unfilled_entry_is_never_squared_off():
    """The book says flat, so the exit is papered and NO order is sent."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)  # broker ACKs the entry
    svc._broker_qty = lambda symbol, exchange: 0  # ...but nothing was ever filled
    _run_to_selection(svc)
    _trigger(svc)

    assert len(orders) == 1, "the entry was accepted, so it was attempted"
    svc.flatten("eod_0930")

    assert len(orders) == 1, (
        "no exit order may be sent for a position that was never filled — "
        "that order is a NAKED SHORT, not a square-off"
    )
    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row.fill == "paper", "nothing was held, so this cannot count as real money"
    assert row.exit_status == "not_placed"
    db_session.expire_all()
    db_session.remove()


def test_a_confirmed_position_is_still_squared_off_normally():
    """The guard must not stop the exit it exists to protect."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    svc._broker_qty = lambda symbol, exchange: 300  # the book confirms we hold it
    _run_to_selection(svc)
    _trigger(svc)
    svc.flatten("eod_0930")

    assert len(orders) == 2, "a real position is squared off exactly as before"
    assert orders[1]["action"] == "SELL"
    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row.fill == "real" and row.status == "closed"
    db_session.expire_all()
    db_session.remove()


def test_an_unreadable_book_still_squares_off():
    """The asymmetry, stated (issue #626).

    `None` means "we could not ask", and once an entry is believed filled the
    dangerous direction reverses: NOT sending the exit strands a real position,
    while an unnecessary exit is caught by the 15:15 MIS auto-square-off. This
    is the opposite default from `_resolve_paper_position`, where the entry was
    known to have been REFUSED — and the difference is deliberate.
    """
    from database.open15_breakout_db import init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    svc._broker_qty = lambda symbol, exchange: None  # broker session down
    _run_to_selection(svc)
    _trigger(svc)
    svc.flatten("eod_0930")

    assert len(orders) == 2, "an unverifiable position is still squared off"


def test_the_pre_exit_check_has_no_off_switch(monkeypatch):
    """#651 deleted the rollback flag; the legacy path no longer exists.

    Setting the old env var must be INERT. Squaring off a position the book says
    is not there is how #626 sent a naked 800-lot SELL that Kite priced at
    Rs4.45L of SPAN — not a behaviour anyone should be able to select back on.
    """
    from database.open15_breakout_db import init_db

    init_db()
    monkeypatch.setenv("OPEN15_CONFIRM_EXIT_POSITION", "false")  # inert since #651
    orders = []
    svc = _mk_service(orders, reject=False)
    svc._broker_qty = lambda symbol, exchange: 0  # book says we hold nothing
    _run_to_selection(svc)
    _trigger(svc)
    svc.flatten("eod_0930")

    assert len(orders) == 1, "the flag is dead — no square-off without a position"


# --------------------------------------------------------------------------- #
# issue #626 — the deferred post-ACK entry check
#
# Between the entry and the exit, a phantom position holds a `max_trades` slot
# it never earned. On 2026-08-18 the day reported `entered: 9, filled: 3` while
# one of those three had been refused by RMS. This asks the broker, once a
# minute, what actually happened — off the tick thread, because a synchronous
# broker call in the ZMQ callback stalls every other symbol.
# --------------------------------------------------------------------------- #
def _stub_broker_answer(monkeypatch, answer):
    """Point verify_entries' broker seam at one canned order-status answer."""
    import services.open15_fill_reconcile as recon

    monkeypatch.setattr(recon, "fetch_fill", lambda _oid, _key: answer)
    monkeypatch.setitem(
        __import__("sys").modules,
        "database.auth_db",
        type("M", (), {"get_first_available_api_key": staticmethod(lambda: "k")}),
    )


def _rejected_answer(msg="Insufficient funds. Margin required: 149255.00."):
    return {"price": None, "qty": 0, "order_status": "rejected", "message": msg}


def test_a_post_ack_rejection_is_demoted_and_frees_its_slot(monkeypatch):
    """The TIINDIA shape: accepted, then refused, and nothing noticed."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    _run_to_selection(svc)
    _trigger(svc)
    assert svc._count_fills()[0] == 1, "the ACK'd entry counts as real until verified"

    _stub_broker_answer(monkeypatch, _rejected_answer())
    monkeypatch.setattr(svc, "_alert_rejection", lambda *a, **k: None)
    assert svc.verify_entries() == 1

    n_real, n_paper, _sim, _shadow = svc._count_fills()
    assert (n_real, n_paper) == (0, 1), "a rejection is not a trade — the slot is released"

    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row.status == "rejected" and row.fill == "paper"
    assert "Insufficient funds" in row.error_message

    # the existing, already-rendered event name — see the note in verify_entries
    ev = [e for e in svc.day_log if e["event"] == "entry_rejected"]
    assert len(ev) == 1 and ev[0]["slot_released"] is True and ev[0]["post_ack"] is True
    db_session.remove()


def test_a_demoted_entry_is_never_squared_off(monkeypatch):
    """The two fixes compose: verification demotes, flatten sends no order."""
    from database.open15_breakout_db import init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    # coherent with the rejection the verification is about to report: the order
    # was refused, so the book holds nothing
    svc._broker_qty = lambda symbol, exchange: 0
    _run_to_selection(svc)
    _trigger(svc)

    _stub_broker_answer(monkeypatch, _rejected_answer())
    monkeypatch.setattr(svc, "_alert_rejection", lambda *a, **k: None)
    svc.verify_entries()
    svc.flatten("eod_0930")

    assert len(orders) == 1, "entry only — the demoted row must never reach the broker"


def test_an_affirmative_book_overrides_a_rejection_verdict(monkeypatch):
    """When the two layers disagree, the POSITION wins (issue #626).

    `verify_entries` says the order was rejected; the book says we hold 300.
    That combination should not happen, but if it does, the only safe reading is
    that something is held — so the row is promoted back and squared off. An
    unsent exit on a real position is the failure that costs money; a redundant
    one is not.
    """
    from database.open15_breakout_db import init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    svc._broker_qty = lambda symbol, exchange: 300  # the book insists
    _run_to_selection(svc)
    _trigger(svc)

    _stub_broker_answer(monkeypatch, _rejected_answer())
    monkeypatch.setattr(svc, "_alert_rejection", lambda *a, **k: None)
    svc.verify_entries()
    svc.flatten("eod_0930")

    assert len(orders) == 2, "an affirmative position is always squared off"


def test_a_complete_entry_is_verified_once_and_left_alone(monkeypatch):
    """The happy path costs exactly one broker call, then stops asking."""
    from database.open15_breakout_db import init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    _run_to_selection(svc)
    _trigger(svc)

    calls = []
    import services.open15_fill_reconcile as recon

    def _fetch(oid, key):
        calls.append(oid)
        return {"price": 103.5, "qty": 300, "order_status": "complete", "message": None}

    monkeypatch.setattr(recon, "fetch_fill", _fetch)
    monkeypatch.setitem(
        __import__("sys").modules,
        "database.auth_db",
        type("M", (), {"get_first_available_api_key": staticmethod(lambda: "k")}),
    )

    assert svc.verify_entries() == 0
    assert svc.verify_entries() == 0
    assert len(calls) == 1, "a settled entry is not re-queried every minute"
    assert svc._count_fills()[0] == 1


def test_a_still_open_order_is_asked_again(monkeypatch):
    """`open` is not an answer — it may still fill, so it stays unverified."""
    from database.open15_breakout_db import init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    _run_to_selection(svc)
    _trigger(svc)

    calls = []
    import services.open15_fill_reconcile as recon

    def _fetch(oid, key):
        calls.append(oid)
        return {"price": None, "qty": 0, "order_status": "open", "message": None}

    monkeypatch.setattr(recon, "fetch_fill", _fetch)
    monkeypatch.setitem(
        __import__("sys").modules,
        "database.auth_db",
        type("M", (), {"get_first_available_api_key": staticmethod(lambda: "k")}),
    )

    svc.verify_entries()
    svc.verify_entries()
    assert len(calls) == 2, "a working order is re-checked until it settles"
    assert svc._count_fills()[0] == 1, "and is still treated as a live position meanwhile"


def test_an_unreadable_answer_changes_nothing(monkeypatch):
    """Fail-open: the exit-time book check and the reconciler are still behind it."""
    from database.open15_breakout_db import init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    _run_to_selection(svc)
    _trigger(svc)

    _stub_broker_answer(monkeypatch, None)
    assert svc.verify_entries() == 0
    assert svc._count_fills()[0] == 1


# --------------------------------------------------------------------------- #
# issue #641 — the fill-true P&L lands at exit time, not at exit+5
#
# On 2026-08-19 the 09:30 page published a quote-derived +14,138 gross that the
# 09:35 summary reconcile corrected to +7,670. Two changes close the gap: the
# per-minute verify job PERSISTS the entry fill it was already fetching (and
# discarding), and `flatten` runs the reconcile itself seconds after the exits.
# --------------------------------------------------------------------------- #
def test_a_complete_entry_stamps_its_broker_fill(monkeypatch):
    """The verify job keeps the fill it fetched instead of throwing it away."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    _run_to_selection(svc)
    _trigger(svc)

    _stub_broker_answer(
        monkeypatch, {"price": 103.5, "qty": 300, "order_status": "complete", "message": None}
    )
    svc.verify_entries()

    row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "AAA").first()
    assert row.entry_fill_price == 103.5
    assert row.entry_fill_qty == 300
    # P&L is untouched — reconcile_fills stays the SINGLE writer of
    # pnl/pnl_source (#552), this only pre-stages the entry leg
    assert row.pnl_source is None
    db_session.remove()


def _stub_reconcile(monkeypatch, results):
    """Replace reconcile_fills with a scripted sequence; record the calls."""
    import services.open15_fill_reconcile as recon

    calls = []

    def _fake(trade_date=None, max_rows=20):
        calls.append(trade_date)
        return dict(results[min(len(calls), len(results)) - 1], rows=[])

    monkeypatch.setattr(recon, "reconcile_fills", _fake)
    return calls


def test_flatten_reconciles_fills_immediately(monkeypatch):
    """A real exit is followed by the broker reconcile in the SAME pass."""
    from database.open15_breakout_db import init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    _run_to_selection(svc)
    _trigger(svc)

    calls = _stub_reconcile(monkeypatch, [{"status": "ok", "reconciled": 1, "pending": 0}])
    svc.flatten("eod_0930")

    assert calls == [svc._trade_date()], "one reconcile, for today, right after the exits"
    assert any(e["event"] == "fill_reconcile" for e in svc.day_log)


def test_flatten_retries_once_while_legs_are_pending(monkeypatch):
    """A fill not yet in the orderbook is asked again seconds later — and the
    still-pending case is left to the summary/next-arm backstops, not looped."""
    from database.open15_breakout_db import init_db

    init_db()
    monkeypatch.setattr("time.sleep", lambda s: None)
    orders = []
    svc = _mk_service(orders, reject=False)
    _run_to_selection(svc)
    _trigger(svc)

    calls = _stub_reconcile(
        monkeypatch,
        [
            {"status": "ok", "reconciled": 0, "pending": 1},
            {"status": "ok", "reconciled": 1, "pending": 0},
        ],
    )
    svc.flatten("eod_0930")

    assert len(calls) == 2, "exactly one in-pass retry"


def test_flatten_with_no_real_exit_never_reconciles(monkeypatch):
    """A paper-only day sends no orders, so there is nothing to ask the broker."""
    from database.open15_breakout_db import init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=True)  # entry rejected at placement -> paper
    _run_to_selection(svc)
    _trigger(svc)

    calls = _stub_reconcile(monkeypatch, [{"status": "ok", "reconciled": 0, "pending": 0}])
    svc.flatten("eod_0930")

    assert calls == [], "no exit order sent -> no broker round-trip"


# --------------------------------------------------------------------------- #
# issue #659 — a paper demotion must also close the CHILD account's mirror
#
# The fan-out fires at ACK time, so a child can be genuinely FILLED on an entry
# our own RMS later refused. Demoting the parent to paper suppresses the parent
# exit — the only trigger the child exit had — so the demotion now sweeps the
# child mirror directly (corroborated by our own affirmatively-flat book).
# --------------------------------------------------------------------------- #
def _capture_sweep(monkeypatch):
    import services.account_fanout_service as fanout

    calls = []
    monkeypatch.setattr(
        fanout,
        "flatten_stranded_child_mirrors",
        lambda mode_key, symbols=None, reason="": calls.append(
            {"mode_key": mode_key, "symbols": symbols, "reason": reason}
        ),
    )
    return calls


def test_post_ack_demotion_sweeps_the_child_mirror(monkeypatch):
    """Gap A: demotion with a corroborating flat book → targeted child sweep."""
    from database.open15_breakout_db import init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    svc._broker_qty = lambda symbol, exchange: 0  # our book corroborates the reject
    _run_to_selection(svc)
    _trigger(svc)

    calls = _capture_sweep(monkeypatch)
    _stub_broker_answer(monkeypatch, _rejected_answer())
    monkeypatch.setattr(svc, "_alert_rejection", lambda *a, **k: None)
    assert svc.verify_entries() == 1

    assert calls == [
        {
            "mode_key": "open15_vol_breakout",
            "symbols": ["AAA"],
            "reason": "parent entry demoted to paper (post-ACK reject)",
        }
    ]


def test_demotion_with_a_disagreeing_book_does_not_sweep(monkeypatch):
    """When OUR book insists we hold the lot, flatten will re-promote and exit
    normally (#626 "the book wins") — the normal exit mirror handles the child,
    and an early sweep would leave that mirror echoing against a flat child."""
    from database.open15_breakout_db import init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    svc._broker_qty = lambda symbol, exchange: 300  # the book disagrees
    _run_to_selection(svc)
    _trigger(svc)

    calls = _capture_sweep(monkeypatch)
    _stub_broker_answer(monkeypatch, _rejected_answer())
    monkeypatch.setattr(svc, "_alert_rejection", lambda *a, **k: None)
    svc.verify_entries()

    assert calls == [], "a possibly-real parent position defers to the normal exit mirror"


def test_flatten_paper_branch_sweeps_the_child_mirror(monkeypatch):
    """Gap A at exit time: the book affirms our entry never filled → the row is
    papered and the child mirror is swept in the same breath."""
    from database.open15_breakout_db import init_db

    init_db()
    orders = []
    svc = _mk_service(orders, reject=False)
    svc._broker_qty = lambda symbol, exchange: 0  # entry never filled on OUR side
    _run_to_selection(svc)
    _trigger(svc)

    calls = _capture_sweep(monkeypatch)
    svc.flatten("eod_0930")

    assert len(orders) == 1, "paper row — no parent exit order"
    assert calls == [
        {
            "mode_key": "open15_vol_breakout",
            "symbols": ["AAA"],
            "reason": "parent entry papered at exit (book flat)",
        }
    ]


def test_summary_job_runs_the_full_sweep(monkeypatch):
    """Gap B: the post-summary sweep is the retry a rejected child exit gets."""
    import services.open15_breakout_service as mod

    calls = _capture_sweep(monkeypatch)

    class _Svc:
        def summary(self):
            pass

    monkeypatch.setattr(mod, "get_open15_service", lambda: _Svc())
    mod._summary_job()

    assert calls == [
        {
            "mode_key": "open15_vol_breakout",
            "symbols": None,
            "reason": "open15 post-summary sweep",
        }
    ]
