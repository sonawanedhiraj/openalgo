"""Child mirror orders are reconciled against the broker (issue #637).

`account_fanout_service` journals `status='placed'` on HTTP 200 — the
ACKNOWLEDGEMENT. Zerodha returns 200 with an order id and its RMS can reject the
order afterwards, which is exactly how a fabricated +Rs7,680 trade reached the
parent's logs page in #626. Before this module there was no reconciliation for
`account_orders` at all, so a refused child order stayed `placed` for ever and
the EOD mirror summary reported a trade that never happened.

The fixtures here are the RAW broker payload, not the mapped one. That is the
point: `transform_order_data` drops `status_message`, and the fields it keeps
(`price`, `quantity`) are what we ASKED for and read identically on a rejected
order and a filled one.
"""

import pytest

from services.account_fill_reconcile import (
    index_orderbook,
    is_terminal_unfilled,
    reconcile_account_fills,
    verdict_for,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    import database.account_orders_db as orders_db
    import database.broker_accounts_db as accounts_db

    accounts_db.init_db()
    orders_db.init_db()
    monkeypatch.setenv("MULTI_ACCOUNT_FILL_RECONCILE_ENABLED", "true")
    yield
    for db, model in ((orders_db, orders_db.AccountOrder),):
        try:
            db.db_session.query(model).delete()
            db.db_session.commit()
        finally:
            db.db_session.remove()


def _kite_order(order_id="CHILD1", status="REJECTED", **over):
    """A Kite orderbook entry in the shape the broker actually returns."""
    order = {
        "order_id": order_id,
        "tradingsymbol": "TIINDIA26AUG2800CE",
        "exchange": "NFO",
        "transaction_type": "BUY",
        "quantity": 200,
        "filled_quantity": 0,
        "price": 77.5,
        "average_price": 0,
        "status": status,
        "status_message": "Insufficient funds. Margin required: 149255.00.",
    }
    order.update(over)
    return order


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_a_rejected_order_is_the_only_thing_that_corrects_a_row():
    assert verdict_for(_kite_order(status="REJECTED"))[0] == "rejected"
    assert verdict_for(_kite_order(status="CANCELLED"))[0] == "rejected"
    assert verdict_for(_kite_order(status="COMPLETE"))[0] == "ok"
    assert verdict_for(_kite_order(status="OPEN"))[0] == "ok"


def test_an_unrecognised_status_leaves_the_row_alone():
    """Guessing the other way would let a parsing gap erase a real trade."""
    assert verdict_for(_kite_order(status="SOME NEW STATE"))[0] == "ok"
    assert verdict_for({})[0] == "ok"


def test_the_brokers_own_reason_is_carried_out():
    _verdict, reason = verdict_for(_kite_order())
    assert "Insufficient funds" in reason


def test_status_matching_is_case_and_spelling_tolerant():
    assert is_terminal_unfilled("REJECTED") and is_terminal_unfilled(" rejected ")
    assert is_terminal_unfilled("CANCELLED") and is_terminal_unfilled("canceled")
    assert not is_terminal_unfilled("COMPLETE") and not is_terminal_unfilled(None)


def test_the_orderbook_index_accepts_both_broker_shapes():
    bare = index_orderbook([_kite_order("A")])
    wrapped = index_orderbook({"data": [_kite_order("A")]})
    assert set(bare) == set(wrapped) == {"A"}
    assert index_orderbook(None) == {}
    assert index_orderbook({"data": "nonsense"}) == {}


# ---------------------------------------------------------------------------
# reconcile_account_fills
# ---------------------------------------------------------------------------
class _StubOrderApi:
    def __init__(self, payload, record=None):
        self.payload = payload
        self.record = record

    def get_order_book(self, auth):
        if self.record is not None:
            self.record.append(auth)
        return self.payload


def _seed(monkeypatch, orderbook, *, token="child:tok", record=None, status="placed"):
    """One child account with one journalled mirror attempt."""
    import database.account_orders_db as orders_db
    import services.account_fill_reconcile as mod

    row = orders_db.record_mirror_attempt(
        account_id=1,
        strategy_name="open15_vol_breakout",
        symbol="TIINDIA25AUG262800CE",
        exchange="NFO",
        action="BUY",
        product="MIS",
        parent_qty=800,
        child_qty=200,
        status=status,
        broker_orderid="CHILD1",
    )
    import database.broker_accounts_db as accounts_db

    monkeypatch.setattr(
        accounts_db, "get_account", lambda _i: {"id": 1, "broker": "zerodha", "display_name": "Kid"}
    )
    monkeypatch.setattr(accounts_db, "auth_name", lambda _i: "acct:1")
    import database.auth_db as auth_db

    monkeypatch.setattr(auth_db, "get_auth_token", lambda _n: token)
    monkeypatch.setattr(mod, "import_module", lambda _n: _StubOrderApi(orderbook, record))
    alerts = []
    monkeypatch.setattr(mod, "_notify", lambda m: alerts.append(m))
    return row, alerts


def _status_of(row_id):
    import database.account_orders_db as orders_db

    return next(r for r in orders_db.list_orders() if r["id"] == row_id)


def test_a_post_ack_rejection_is_corrected_and_alerts(monkeypatch):
    """The whole defect in one test."""
    row, alerts = _seed(monkeypatch, {"data": [_kite_order(status="REJECTED")]})

    result = reconcile_account_fills()
    assert result["corrected"] == 1

    fixed = _status_of(row["id"])
    assert fixed["status"] == "rejected", "a refused order must not stay 'placed'"
    assert "Insufficient funds" in (fixed["error_text"] or "")
    assert alerts and "did NOT happen" in alerts[0]


def test_a_genuine_fill_is_left_alone(monkeypatch):
    row, alerts = _seed(
        monkeypatch, {"data": [_kite_order(status="COMPLETE", filled_quantity=200)]}
    )

    result = reconcile_account_fills()
    assert result == {"status": "ok", "checked": 1, "corrected": 0}
    assert _status_of(row["id"])["status"] == "placed"
    assert alerts == []


def test_an_unreadable_orderbook_corrects_nothing(monkeypatch):
    """A broker hiccup must never be read as a rejection."""
    row, alerts = _seed(monkeypatch, {"status": "error", "message": "gateway down"})

    assert reconcile_account_fills()["corrected"] == 0
    assert _status_of(row["id"])["status"] == "placed"
    assert alerts == []


def test_the_orderbook_is_read_with_the_childs_token(monkeypatch):
    """Not the parent's — a child has no OpenAlgo API key (#497 rule)."""
    seen = []
    _seed(monkeypatch, {"data": [_kite_order(status="COMPLETE")]}, token="child:tok", record=seen)

    reconcile_account_fills()
    assert seen == ["child:tok"]


def test_rows_that_were_never_placed_are_not_touched(monkeypatch):
    """Only `placed` rows are ambiguous; a skip or a known rejection is settled."""
    row, _alerts = _seed(
        monkeypatch, {"data": [_kite_order(status="REJECTED")]}, status="skipped_no_capital"
    )

    assert reconcile_account_fills() == {"status": "ok", "checked": 0, "corrected": 0}
    assert _status_of(row["id"])["status"] == "skipped_no_capital"


def test_reconciling_twice_is_idempotent(monkeypatch):
    """No marker column, so re-running must reach the same verdict harmlessly."""
    row, _alerts = _seed(monkeypatch, {"data": [_kite_order(status="REJECTED")]})

    first = reconcile_account_fills()
    second = reconcile_account_fills()
    assert first["corrected"] == 1
    assert second["corrected"] == 0, "the row is no longer 'placed', so it is not re-checked"
    assert _status_of(row["id"])["status"] == "rejected"


def test_the_pass_can_be_switched_off(monkeypatch):
    row, _alerts = _seed(monkeypatch, {"data": [_kite_order(status="REJECTED")]})
    monkeypatch.setenv("MULTI_ACCOUNT_FILL_RECONCILE_ENABLED", "false")

    assert reconcile_account_fills()["status"] == "disabled"
    assert _status_of(row["id"])["status"] == "placed"


def test_update_status_refuses_an_unknown_state():
    """Correcting a record must not silently substitute another wrong answer.

    `record_mirror_attempt` coerces a bad status to 'error', which is right when
    journalling a live attempt and wrong when repairing history.
    """
    import database.account_orders_db as orders_db

    row = orders_db.record_mirror_attempt(
        account_id=1,
        strategy_name="s",
        symbol="X",
        exchange="NSE",
        action="BUY",
        product="CNC",
        parent_qty=1,
        child_qty=1,
        status="placed",
        broker_orderid="Z1",
    )
    assert orders_db.update_status(row["id"], status="not_a_real_status") is False
    assert _status_of(row["id"])["status"] == "placed"
