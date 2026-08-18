"""The broker's rejection reason must survive the mapper (issue #626).

On 2026-08-18 Zerodha ACK'd an open15 order and then RMS-rejected it with
``"Insufficient funds. Margin required: 149255.00. Margin available:
122252.80."``. OpenAlgo logged ``Status: rejected`` and nothing else, because
``map_order_data`` kept neither ``status_message`` nor ``filled_quantity``.

That omission is not cosmetic. What the mapper DID keep — ``price`` and
``quantity`` — are the limit and size we *asked for*, and both are fully
populated on an order that never reached the market. Downstream, a rejection
was therefore indistinguishable from a fill by every field available.
"""

from broker.zerodha.mapping.order_data import transform_order_data

# The orderbook pipeline is map_order_data (rewrites the symbol) ->
# transform_order_data (projects the broker payload onto OpenAlgo's field set).
# The projection is where fields are LOST, so that is what these pin.


def _kite_order(**over):
    """A Kite orderbook entry, in the shape the broker actually returns."""
    order = {
        "order_id": "260818190112881",
        "tradingsymbol": "TIINDIA26AUG2800CE",
        "exchange": "NFO",
        "transaction_type": "BUY",
        "order_type": "LIMIT",
        "product": "MIS",
        "quantity": 800,
        "filled_quantity": 0,
        "price": 77.5,
        "trigger_price": 0,
        "average_price": 0,
        "status": "REJECTED",
        "status_message": (
            "Insufficient funds. Margin required: 149255.00. "
            "Margin available: 122252.80. Check orderbook for open orders."
        ),
        "order_timestamp": "2026-08-18 09:19:58",
    }
    order.update(over)
    return order


def test_a_rejection_keeps_the_brokers_own_reason():
    mapped = transform_order_data([_kite_order()])[0]

    assert mapped["order_status"] == "rejected"
    assert "Insufficient funds" in mapped["status_message"]
    assert mapped["filled_quantity"] == 0


def test_the_fields_that_survived_could_not_tell_a_rejection_from_a_fill():
    """Why the two new keys are load-bearing rather than nice-to-have.

    ``price`` and ``quantity`` are what we requested; they read identically on a
    rejected order and a filled one. Only ``filled_quantity`` / ``status_message``
    separate the two.
    """
    rejected = transform_order_data([_kite_order()])[0]
    filled = transform_order_data(
        [_kite_order(status="COMPLETE", filled_quantity=800, average_price=77.0)]
    )[0]

    assert rejected["price"] == filled["price"] == 77.5
    assert rejected["quantity"] == filled["quantity"] == 800
    assert rejected["filled_quantity"] != filled["filled_quantity"]


def test_a_normal_order_reports_an_empty_reason_not_none():
    """A completed order has no message; downstream string handling stays simple."""
    mapped = transform_order_data(
        [_kite_order(status="COMPLETE", filled_quantity=800, status_message=None)]
    )[0]

    assert mapped["status_message"] == ""
    assert mapped["filled_quantity"] == 800
