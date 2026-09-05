"""Child-account realized P&L capture (issue #700).

Kite serves the tradebook for TODAY only, so a child's P&L exists only if this
module writes it down the same day. The tests pin the rules that keep that
record honest: fills come from the child's tradebook keyed by ``order_id``,
partials are volume-weighted, pairing is FIFO per symbol, charges are labelled
by source, and a day with any fill still UNKNOWN writes no row at all.

DB isolation comes from test/conftest.py's global redirect; broker I/O is
stubbed at the module seams (``_broker_module``, ``get_auth_token``,
``_charges_module``) — never a real broker call.
"""

from datetime import date, datetime, timedelta

import pytest

from database import account_orders_db, broker_accounts_db
from services import account_pnl_service as svc

IST = timedelta(hours=5, minutes=30)


@pytest.fixture(autouse=True)
def _tables():
    broker_accounts_db.init_db()
    account_orders_db.init_db()
    yield
    for model in (account_orders_db.AccountOrder, account_orders_db.AccountDailyPnl):
        account_orders_db.db_session.query(model).delete()
    account_orders_db.db_session.commit()
    account_orders_db.db_session.remove()
    broker_accounts_db.db_session.query(broker_accounts_db.BrokerAccount).delete()
    broker_accounts_db.db_session.commit()
    broker_accounts_db.db_session.remove()


@pytest.fixture()
def child():
    return broker_accounts_db.add_account(
        display_name="Kid A",
        api_key="key_kid_a_0123456789",  # pragma: allowlist secret
        api_secret="secret_kid_a_0123456789",  # pragma: allowlist secret
        capital_inr=100000,
    )


TODAY = svc.today_ist()


def _utc(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    """Naive UTC for an IST wall-clock time on ``day``."""
    return datetime(day.year, day.month, day.day, hh, mm, ss) - IST


def _ist_str(day: date, hh: int, mm: int, ss: int = 0) -> str:
    return f"{day.isoformat()} {hh:02d}:{mm:02d}:{ss:02d}"


def _mirror(
    account_id, symbol, action, qty, orderid, when: datetime, strategy="open15_vol_breakout"
):
    row = account_orders_db.record_mirror_attempt(
        account_id=account_id,
        strategy_name=strategy,
        symbol=symbol,
        exchange="NFO",
        action=action,
        product="MIS",
        parent_qty=qty,
        child_qty=qty,
        status="placed",
        broker_orderid=orderid,
    )
    # pin created_at to the intended IST day / order
    account_orders_db.db_session.query(account_orders_db.AccountOrder).filter(
        account_orders_db.AccountOrder.id == row["id"]
    ).update({"created_at": when})
    account_orders_db.db_session.commit()
    account_orders_db.db_session.remove()
    return row


def _trade(order_id, symbol, side, qty, price, ts):
    return {
        "trade_id": f"T{order_id}{qty}",
        "order_id": order_id,
        "exchange": "NFO",
        "tradingsymbol": symbol,
        "product": "MIS",
        "transaction_type": side,
        "quantity": qty,
        "average_price": price,
        "fill_timestamp": ts,
    }


class FakeBroker:
    def __init__(self, trades=None, positions=None, tradebook_ok=True):
        self.trades = trades or []
        self.positions = positions
        self.tradebook_ok = tradebook_ok

    def get_trade_book(self, auth):
        if not self.tradebook_ok:
            return {"status": "error", "message": "TokenException"}
        return {"status": "success", "data": self.trades}

    def get_positions(self, auth):
        if self.positions is None:
            return {"status": "error"}
        return {"status": "success", "data": {"net": self.positions, "day": []}}


def _wire(monkeypatch, broker, token="key:token", charges=None):
    monkeypatch.setattr(svc, "_broker_module", lambda b: broker)
    monkeypatch.setattr(svc, "get_auth_token", lambda name: token)
    monkeypatch.setattr(svc, "_br_symbol", lambda b, s, e: s)
    if charges is None:
        monkeypatch.setattr(svc, "_charges_module", lambda b: None)
    else:
        monkeypatch.setattr(svc, "_charges_module", lambda b: charges)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_partial_fills_are_volume_weighted():
    price, qty, at = svc.aggregate_fill(
        [
            _trade("O1", "X", "BUY", 100, 10.0, _ist_str(TODAY, 9, 17, 1)),
            _trade("O1", "X", "BUY", 300, 12.0, _ist_str(TODAY, 9, 17, 5)),
        ]
    )
    assert qty == 400
    assert price == pytest.approx(11.5)
    assert at == _utc(TODAY, 9, 17, 5)


def test_unreadable_tradebook_is_none_not_empty():
    assert svc.index_tradebook({"status": "error"}) is None
    assert svc.index_tradebook(None) is None
    assert svc.index_tradebook({"status": "success", "data": []}) == {}


def test_fifo_pairs_long_and_short_and_reports_open_remainder():
    d = TODAY
    legs = [
        svc.Leg(1, "s", "A", "NFO", "MIS", "BUY", 100, 10.0, _utc(d, 9, 17), d),
        svc.Leg(2, "s", "A", "NFO", "MIS", "BUY", 100, 12.0, _utc(d, 9, 18), d),
        svc.Leg(3, "s", "A", "NFO", "MIS", "SELL", 150, 13.0, _utc(d, 9, 30), d),
        svc.Leg(4, "s", "B", "NFO", "MIS", "SELL", 50, 20.0, _utc(d, 9, 20), d),
        svc.Leg(5, "s", "B", "NFO", "MIS", "BUY", 50, 18.0, _utc(d, 9, 30), d),
    ]
    trips, open_legs = svc.pair_fifo(legs)
    gross = {(t.symbol, t.qty, t.direction): t.gross for t in trips}
    assert gross[("A", 100, "long")] == pytest.approx(300.0)  # 100 × (13-10), FIFO first
    assert gross[("A", 50, "long")] == pytest.approx(50.0)  # 50 × (13-12)
    assert gross[("B", 50, "short")] == pytest.approx(100.0)  # 50 × (20-18)
    assert [(leg.symbol, leg.qty) for leg in open_legs] == [("A", 50)]


def test_t_plus_1_exit_is_attributed_to_the_closing_day():
    d0 = TODAY - timedelta(days=1)
    legs = [
        svc.Leg(1, "s", "A", "NSE", "CNC", "BUY", 10, 100.0, _utc(d0, 15, 5), d0),
        svc.Leg(2, "s", "A", "NSE", "CNC", "SELL", 10, 104.0, _utc(TODAY, 15, 10), TODAY),
    ]
    trips, _ = svc.pair_fifo(legs)
    assert trips[0].close_day == TODAY and trips[0].open_day == d0
    assert trips[0].gross == pytest.approx(40.0)


def test_modelled_charges_are_per_leg_and_side_aware():
    buy = svc.modelled_leg_charges("NFO", "X26SEP100CE", "BUY", 10000.0)
    sell = svc.modelled_leg_charges("NFO", "X26SEP100CE", "SELL", 10000.0)
    assert sell > buy  # STT sits on the sell leg
    assert svc.modelled_leg_charges("NFO", "X26SEP100CE", "BUY", 0) == 0.0


# ---------------------------------------------------------------------------
# capture end-to-end
# ---------------------------------------------------------------------------
def test_capture_writes_fills_and_a_net_day_row(monkeypatch, child):
    sym = "TIINDIA26SEP2800CE"
    _mirror(child["id"], sym, "BUY", 200, "O1", _utc(TODAY, 9, 17))
    _mirror(child["id"], sym, "SELL", 200, "O2", _utc(TODAY, 9, 30))
    broker = FakeBroker(
        trades=[
            _trade("O1", sym, "BUY", 200, 50.0, _ist_str(TODAY, 9, 17, 2)),
            _trade("O2", sym, "SELL", 200, 55.0, _ist_str(TODAY, 9, 30, 1)),
        ],
        positions=[
            {"tradingsymbol": sym, "exchange": "NFO", "product": "MIS", "realised": 1000.0},
            # a manual trade in the same account must NOT enter the strategy figure
            {"tradingsymbol": "OTHER", "exchange": "NSE", "product": "MIS", "realised": 999.0},
        ],
    )
    _wire(monkeypatch, broker)

    res = svc.capture_account_day(child, TODAY, finalize=True)
    assert res["status"] == "captured"
    row = res["strategies"]["open15_vol_breakout"]
    assert row["realized_gross"] == pytest.approx(1000.0)
    assert row["charges_inr"] > 0
    assert row["realized_net"] == pytest.approx(1000.0 - row["charges_inr"], abs=0.01)
    assert row["charges_source"] == "modelled"
    assert row["n_round_trips"] == 1 and row["n_fills"] == 2 and row["n_open_legs"] == 0
    assert row["book_realised"] == pytest.approx(1000.0)
    assert row["finalized"] is True

    # fills landed on the journal rows, priced from the tradebook not sizing
    stored = account_orders_db.list_orders(account_id=child["id"])
    by_oid = {r["broker_orderid"]: r for r in stored}
    assert by_oid["O1"]["fill_price"] == 50.0 and by_oid["O1"]["fill_qty"] == 200
    assert by_oid["O2"]["fill_price"] == 55.0
    assert by_oid["O1"]["charges_source"] == "modelled"


def test_broker_charges_win_when_the_calculator_answers(monkeypatch, child):
    sym = "SAIL26SEP150CE"
    _mirror(child["id"], sym, "BUY", 4700, "O1", _utc(TODAY, 9, 17))
    _mirror(child["id"], sym, "SELL", 4700, "O2", _utc(TODAY, 9, 30))
    broker = FakeBroker(
        trades=[
            _trade("O1", sym, "BUY", 4700, 2.0, _ist_str(TODAY, 9, 17)),
            _trade("O2", sym, "SELL", 4700, 2.5, _ist_str(TODAY, 9, 30)),
        ]
    )

    class Charges:
        @staticmethod
        def build_charge_request(**kw):
            return kw

        @staticmethod
        def get_order_charges(orders, token):
            return {o["order_id"]: 33.0 for o in orders}

    _wire(monkeypatch, broker, charges=Charges)
    res = svc.capture_account_day(child, TODAY)
    row = res["strategies"]["open15_vol_breakout"]
    assert row["charges_source"] == "broker"
    assert row["charges_inr"] == pytest.approx(66.0)
    assert row["realized_net"] == pytest.approx(4700 * 0.5 - 66.0)


def test_no_session_writes_no_row_and_says_so(monkeypatch, child):
    _mirror(child["id"], "X26SEP100CE", "BUY", 100, "O1", _utc(TODAY, 9, 17))
    _wire(monkeypatch, FakeBroker(), token=None)
    res = svc.capture_account_day(child, TODAY)
    assert res["status"] == "no_session"
    assert account_orders_db.list_daily_pnl("open15_vol_breakout") == []


def test_unreadable_tradebook_stamps_nothing_and_writes_no_row(monkeypatch, child):
    _mirror(child["id"], "X26SEP100CE", "BUY", 100, "O1", _utc(TODAY, 9, 17))
    _wire(monkeypatch, FakeBroker(tradebook_ok=False))
    res = svc.capture_account_day(child, TODAY)
    assert res["status"] == "tradebook_unreadable"
    assert account_orders_db.list_orders(account_id=child["id"])[0]["fill_qty"] is None
    assert account_orders_db.list_daily_pnl("open15_vol_breakout") == []


def test_placed_row_absent_from_a_readable_tradebook_is_known_unfilled(monkeypatch, child):
    """0 is the answer, not 'unknown' — so the day row CAN be written."""
    sym = "X26SEP100CE"
    _mirror(child["id"], sym, "BUY", 100, "O1", _utc(TODAY, 9, 17))
    _mirror(child["id"], sym, "SELL", 100, "O2", _utc(TODAY, 9, 30))
    _wire(
        monkeypatch,
        FakeBroker(trades=[_trade("O1", sym, "BUY", 100, 10.0, _ist_str(TODAY, 9, 17))]),
    )
    res = svc.capture_account_day(child, TODAY)
    assert res["status"] == "captured"
    row = res["strategies"]["open15_vol_breakout"]
    assert row["n_fills"] == 1 and row["n_round_trips"] == 0 and row["n_open_legs"] == 1
    assert row["realized_gross"] == 0.0
    by_oid = {r["broker_orderid"]: r for r in account_orders_db.list_orders(account_id=child["id"])}
    assert by_oid["O2"]["fill_qty"] == 0


def test_recapture_is_idempotent_and_never_demotes_final(monkeypatch, child):
    sym = "X26SEP100CE"
    _mirror(child["id"], sym, "BUY", 100, "O1", _utc(TODAY, 9, 17))
    _mirror(child["id"], sym, "SELL", 100, "O2", _utc(TODAY, 9, 30))
    _wire(
        monkeypatch,
        FakeBroker(
            trades=[
                _trade("O1", sym, "BUY", 100, 10.0, _ist_str(TODAY, 9, 17)),
                _trade("O2", sym, "SELL", 100, 11.0, _ist_str(TODAY, 9, 30)),
            ]
        ),
    )
    first = svc.capture_account_day(child, TODAY, finalize=True)["strategies"][
        "open15_vol_breakout"
    ]
    second = svc.capture_account_day(child, TODAY, finalize=False)["strategies"][
        "open15_vol_breakout"
    ]
    assert second["realized_net"] == first["realized_net"]
    assert second["finalized"] is True
    assert len(account_orders_db.list_daily_pnl("open15_vol_breakout")) == 1


def test_rejected_rows_never_become_fills(monkeypatch, child):
    """A post-ACK rejection (#637) is not `placed`, so it is not even looked up."""
    sym = "X26SEP100CE"
    row = _mirror(child["id"], sym, "BUY", 100, "O1", _utc(TODAY, 9, 17))
    account_orders_db.update_status(row["id"], status="rejected", error_text="RMS")
    _wire(
        monkeypatch,
        FakeBroker(trades=[_trade("O1", sym, "BUY", 100, 10.0, _ist_str(TODAY, 9, 17))]),
    )
    res = svc.capture_account_day(child, TODAY)
    assert res["status"] == "no_rows"
    assert account_orders_db.list_orders(account_id=child["id"])[0]["fill_qty"] is None


def test_historical_day_recomputes_from_stored_fills_without_the_broker(monkeypatch, child):
    """The Console-import path: fills are already on the rows; no broker call."""
    d = TODAY - timedelta(days=3)
    sym = "X26SEP100CE"
    a = _mirror(child["id"], sym, "BUY", 100, "O1", _utc(d, 9, 17))
    b = _mirror(child["id"], sym, "SELL", 100, "O2", _utc(d, 9, 30))
    account_orders_db.set_fill(a["id"], fill_price=10.0, fill_qty=100, fill_at=_utc(d, 9, 17))
    account_orders_db.set_fill(b["id"], fill_price=12.0, fill_qty=100, fill_at=_utc(d, 9, 30))

    def boom(*a, **k):
        raise AssertionError("broker must not be called for a historical day")

    monkeypatch.setattr(svc, "_broker_module", boom)
    monkeypatch.setattr(svc, "_charges_module", lambda b: None)
    monkeypatch.setattr(svc, "_br_symbol", lambda b, s, e: s)
    res = svc.capture_account_day(
        child, d, use_broker=False, capture_source="console_csv", finalize=True
    )
    row = res["strategies"]["open15_vol_breakout"]
    assert row["realized_gross"] == pytest.approx(200.0)
    assert row["capture_source"] == "console_csv"
    assert row["book_realised"] is None


def test_capture_all_isolates_a_broken_account(monkeypatch, child):
    other = broker_accounts_db.add_account(
        display_name="Kid B",
        api_key="key_kid_b_0123456789",  # pragma: allowlist secret
        api_secret="secret_kid_b_0123456789",  # pragma: allowlist secret
        capital_inr=50000,
    )
    sym = "X26SEP100CE"
    _mirror(child["id"], sym, "BUY", 100, "O1", _utc(TODAY, 9, 17))
    _mirror(other["id"], sym, "BUY", 100, "O9", _utc(TODAY, 9, 17))

    class Broker(FakeBroker):
        def get_trade_book(self, auth):
            if auth == "bad":
                raise RuntimeError("socket died")
            return {
                "status": "success",
                "data": [_trade("O1", sym, "BUY", 100, 10.0, _ist_str(TODAY, 9, 17))],
            }

    monkeypatch.setattr(svc, "_broker_module", lambda b: Broker())
    monkeypatch.setattr(
        svc, "get_auth_token", lambda name: "bad" if name.endswith(str(other["id"])) else "ok"
    )
    monkeypatch.setattr(svc, "_br_symbol", lambda b, s, e: s)
    monkeypatch.setattr(svc, "_charges_module", lambda b: None)
    results = {r["account_id"]: r["status"] for r in svc.capture_all(TODAY)}
    assert results[child["id"]] == "captured"
    assert results[other["id"]] == "tradebook_unreadable"
