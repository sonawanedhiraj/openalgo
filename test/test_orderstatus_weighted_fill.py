"""Order-status fill price: weighted average, never the first partial (#641).

On 2026-08-19 two open15 option orders filled across multiple partial trades
(19 tradebook rows for 4 orders). ``get_order_status_with_auth`` derived each
order's fill from the tradebook with a loop that ``break``-ed on the FIRST
matching trade, so the first partial's price was published as the whole order's
fill — the reconciled day P&L was Rs912.50 off the broker's own number.

Two-layer fix, both pinned here:

1. When the broker mapper preserves the orderbook's own ``average_price``
   (Zerodha does as of #641), that number IS the volume-weighted average and is
   used directly — no tradebook call at all.
2. When it doesn't (30+ other brokers), the tradebook fallback volume-weights
   ALL trades belonging to the order.
"""

from services.mode_service import EffectiveMode
from services.orderstatus_service import get_order_status_with_auth


def _order(orderid="260819190169554", status="complete", **over):
    order = {
        "orderid": orderid,
        "symbol": "CGPOWER25AUG26880PE",
        "exchange": "NFO",
        "action": "SELL",
        "quantity": 3400,
        "price": 0.0,
        "pricetype": "MARKET",
        "product": "MIS",
        "order_status": status,
        "timestamp": "2026-08-19 09:30:00",
    }
    order.update(over)
    return order


def _wire(monkeypatch, order, trades):
    """Point the service's two data seams at canned books; count the calls."""
    import services.orderbook_service as ob
    import services.orderstatus_service as osvc

    calls = {"tradebook": 0}
    monkeypatch.setattr(osvc, "resolve_effective_mode", lambda: EffectiveMode.LIVE)
    monkeypatch.setattr(
        ob,
        "get_orderbook",
        lambda auth_token, broker: (True, {"status": "success", "data": [order]}, 200),
    )

    def _tradebook(auth_token, broker):
        calls["tradebook"] += 1
        return True, {"status": "success", "data": trades}, 200

    monkeypatch.setattr(osvc, "get_tradebook", _tradebook)
    return calls


def _call(orderid="260819190169554"):
    ok, resp, code = get_order_status_with_auth(
        {"orderid": orderid},
        auth_token="t",
        broker="zerodha",
        original_data={"strategy": "open15_vol_breakout", "orderid": orderid},
    )
    assert ok and code == 200
    return resp["data"]


def test_the_orderbooks_own_weighted_average_wins_without_a_tradebook_call(monkeypatch):
    """Layer 1: the broker already did the volume-weighting — use it."""
    calls = _wire(monkeypatch, _order(average_price=21.1375), trades=[])

    data = _call()

    assert data["average_price"] == 21.1375
    assert calls["tradebook"] == 0, "no second broker round-trip when the answer is present"


def test_the_tradebook_fallback_volume_weights_every_partial(monkeypatch):
    """Layer 2: the 2026-08-19 CGPOWER shape — two partials, one order.

    The old first-match ``break`` would report 21.25 (the first partial).
    Weighted: (2550*21.25 + 850*21.05) / 3400 = 21.2.
    """
    trades = [
        {"orderid": "999", "quantity": 100, "average_price": 50.0},  # someone else's
        {"orderid": "260819190169554", "quantity": 2550, "average_price": 21.25},
        {"orderid": "260819190169554", "quantity": 850, "average_price": 21.05},
    ]
    calls = _wire(monkeypatch, _order(average_price=0), trades=trades)

    data = _call()

    assert data["average_price"] == 21.2, "weighted across ALL partials, not the first"
    assert calls["tradebook"] == 1


def test_a_mapper_without_average_price_still_falls_back(monkeypatch):
    """Most brokers' mappers drop the field entirely — absence means fallback."""
    trades = [{"orderid": "X-1", "quantity": 300, "average_price": 103.5}]
    _wire(monkeypatch, _order(orderid="X-1"), trades=trades)  # no average_price key

    assert _call("X-1")["average_price"] == 103.5


def test_a_rejected_order_is_never_priced(monkeypatch):
    """The #626 rule survives: a rejection has no fill, whatever any field says."""
    calls = _wire(
        monkeypatch,
        _order(status="rejected", price=77.5, average_price=77.5),  # hostile fields
        trades=[{"orderid": "260819190169554", "quantity": 800, "average_price": 77.0}],
    )

    data = _call()

    assert data["average_price"] == 0.0
    assert calls["tradebook"] == 0
