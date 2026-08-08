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
    # the book confirms flat by default — the static-IP case, where the order
    # provably never reached the exchange
    svc._broker_qty = lambda symbol, exchange: 0
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
