"""Multi-account Phase 4 hardening (issue #478).

Two concerns:

1. **Seam integration** — drive the REAL ``place_order_with_auth`` (not the
   fan-out service in isolation) with a stubbed broker module and pinned mode
   resolution, proving:
   * LIVE-resolved + broker-accepted parent → fan-out fires, child journaled;
   * sandbox-resolved parent → the seam is never reached;
   * broker-REJECTED parent → no fan-out (no naked child positions).
2. **Rejected-entry exit guard (partial-rejection drill)** — a parent EXIT for
   a position the child never got (entry rejected/skipped) must journal
   ``skipped_no_position``, NOT scale into a fresh naked position; a genuine
   opening short (no journal history) still scales; an exit after a PLACED
   entry still flattens the held qty.

Hermetic: global conftest DB redirect; broker + sandbox + notify stubbed;
executor inlined.
"""

from __future__ import annotations

import pytest

# Pre-resolve the restx_api / services.place_order_service circular import
# before the fixture does ``import services.place_order_service`` (same
# pattern + rationale as test_place_order_dispatch.py).
import restx_api  # noqa: E402, F401


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class _Res:
    def __init__(self, status):
        self.status = status


class _StubBroker:
    def __init__(self, place_status=200, open_qty=0):
        self.place_status = place_status
        self.open_qty = open_qty
        self.placed_orders = []

    def get_open_position(self, symbol, exchange, product, auth):
        return str(self.open_qty)

    def place_order_api(self, data, auth):
        self.placed_orders.append((dict(data), auth))
        if self.place_status == 200:
            return _Res(200), {"status": "success"}, f"OID{len(self.placed_orders)}"
        return _Res(400), {"message": "rejected by broker"}, None


EQ_ORDER = {
    "symbol": "TATAMOTORS",
    "exchange": "NSE",
    "action": "BUY",
    "product": "CNC",
    "pricetype": "MARKET",
    "quantity": 52,
    "strategy": "sector_follow_cap5_vol",
}


@pytest.fixture
def seam_env(monkeypatch):
    """Real place_order_with_auth wiring with everything external stubbed."""
    import database.account_orders_db as orders_db
    import database.broker_accounts_db as accounts_db
    import services.account_fanout_service as fanout
    import services.place_order_service as pos
    import services.synthetic_market_order_service as synth
    from database.auth_db import upsert_auth
    from services.mode_service import EffectiveMode

    accounts_db.init_db()
    orders_db.init_db()

    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "true")
    monkeypatch.setenv("PRIMARY_BOOK_CAPITAL", "1000000")

    parent_stub = _StubBroker()
    child_stub = _StubBroker()

    # Parent path: pinned LIVE, passthrough price-safety, stub broker module.
    monkeypatch.setattr(pos, "resolve_order_mode", lambda key: EffectiveMode.LIVE)
    monkeypatch.setattr(synth, "ensure_live_safe_pricetype", lambda od, tok, br: (od, None, None))
    monkeypatch.setattr(pos, "import_broker_module", lambda broker: parent_stub)

    # Fan-out internals: inline executor, stub child broker, quiet notify.
    monkeypatch.setattr(fanout, "_get_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(fanout, "import_module", lambda n: child_stub)
    notifications: list[str] = []
    monkeypatch.setattr(fanout, "_notify_operator", lambda m: notifications.append(m))

    # One connected, enabled child sized at ₹15,000 per trade (issue #496);
    # sizing price stubbed at 500 → 30 shares per mirror.
    monkeypatch.setattr(fanout, "resolve_sizing_price", lambda od: 500.0)
    account = accounts_db.add_account(
        display_name="SeamChild",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=250000,
    )
    accounts_db.update_account(account["id"], is_enabled=True)
    accounts_db.set_strategies(
        account["id"],
        ["sector_follow_cap5_vol"],
        capital_per_trade={"sector_follow_cap5_vol": 15000},
    )
    upsert_auth(accounts_db.auth_name(account["id"]), "k:tok", "zerodha")

    yield pos, fanout, orders_db, accounts_db, parent_stub, child_stub, account, notifications

    try:
        orders_db.db_session.query(orders_db.AccountOrder).delete()
        orders_db.db_session.commit()
    finally:
        orders_db.db_session.remove()
    try:
        accounts_db.db_session.query(accounts_db.MultiAccountSettings).delete()
        accounts_db.db_session.query(accounts_db.AccountStrategy).delete()
        accounts_db.db_session.query(accounts_db.BrokerAccount).delete()
        accounts_db.db_session.commit()
    finally:
        accounts_db.db_session.remove()
    import database.auth_db as auth_db_module

    try:
        auth_db_module.db_session.query(auth_db_module.Auth).filter(
            auth_db_module.Auth.name.like("acct:%")
        ).delete(synchronize_session=False)
        auth_db_module.db_session.commit()
    finally:
        auth_db_module.db_session.remove()
    auth_db_module.auth_cache.clear()


def _place(pos, order=EQ_ORDER, mode_key="sector_follow_cap5_vol"):
    return pos.place_order_with_auth(
        dict(order),
        auth_token="primary:tok",
        broker="zerodha",
        original_data=dict(order),
        emit_event=False,
        mode_key=mode_key,
    )


# ---------------------------------------------------------------------------
# Seam integration
# ---------------------------------------------------------------------------


def test_live_accepted_parent_triggers_fanout(seam_env):
    pos, _fanout, orders_db, _adb, parent_stub, child_stub, account, _n = seam_env

    success, response, status = _place(pos)
    assert success is True and status == 200
    # Parent went to the parent broker stub, child mirror to the child stub.
    assert len(parent_stub.placed_orders) == 1
    assert len(child_stub.placed_orders) == 1
    assert child_stub.placed_orders[0][0]["quantity"] == 30  # 15000 / 500
    rows = orders_db.list_orders()
    assert len(rows) == 1 and rows[0]["status"] == "placed"
    assert rows[0]["parent_orderid"] == "OID1"


def test_rejected_parent_never_fans_out(seam_env):
    pos, _fanout, orders_db, _adb, parent_stub, child_stub, _acc, _n = seam_env
    parent_stub.place_status = 400

    success, response, status = _place(pos)
    assert success is False
    assert child_stub.placed_orders == []
    assert orders_db.list_orders() == []


def test_sandbox_parent_never_reaches_seam(seam_env, monkeypatch):
    pos, _fanout, orders_db, _adb, _parent, child_stub, _acc, _n = seam_env
    from services.mode_service import EffectiveMode

    monkeypatch.setattr(pos, "resolve_order_mode", lambda key: EffectiveMode.SANDBOX)
    sandbox_calls = []

    import services.sandbox_service as sandbox_service

    monkeypatch.setattr(
        sandbox_service,
        "sandbox_place_order",
        lambda od, key, orig, prefetched_quote=None: (
            sandbox_calls.append(od) or (True, {"status": "success", "orderid": "SB1"}, 200)
        ),
    )

    order = dict(EQ_ORDER, apikey="test_key")  # pragma: allowlist secret
    success, response, status = pos.place_order_with_auth(
        dict(order),
        auth_token="primary:tok",
        broker="zerodha",
        original_data=order,
        emit_event=False,
        mode_key="sector_follow_cap5_vol",
    )
    assert success is True
    assert len(sandbox_calls) == 1
    assert child_stub.placed_orders == []
    assert orders_db.list_orders() == []


# ---------------------------------------------------------------------------
# Rejected-entry exit guard (partial-rejection drill)
# ---------------------------------------------------------------------------


def test_exit_after_rejected_entry_is_skipped_not_shorted(seam_env):
    pos, _fanout, orders_db, _adb, parent_stub, child_stub, account, notifications = seam_env

    # Day 1, entry: broker rejects the child's BUY (e.g. margin).
    child_stub.place_status = 400
    _place(pos, dict(EQ_ORDER, action="BUY"))
    assert orders_db.list_orders()[0]["status"] == "rejected"

    # Exit: parent SELLs; the child is flat. Must SKIP, not open a scaled short.
    child_stub.place_status = 200
    child_stub.placed_orders.clear()
    _place(pos, dict(EQ_ORDER, action="SELL"))
    statuses = [r["status"] for r in orders_db.list_orders()]
    assert statuses[0] == "skipped_no_position"
    assert child_stub.placed_orders == []
    assert any("nothing to exit" in n for n in notifications)


def test_exit_after_placed_entry_flattens_held_qty(seam_env):
    pos, _fanout, orders_db, _adb, _parent, child_stub, _acc, _n = seam_env

    _place(pos, dict(EQ_ORDER, action="BUY"))  # entry places (30 mirrored)
    child_stub.open_qty = 30
    child_stub.placed_orders.clear()

    _place(pos, dict(EQ_ORDER, action="SELL"))
    assert child_stub.placed_orders[0][0]["quantity"] == 30
    assert orders_db.list_orders()[0]["status"] == "placed"


def test_genuine_short_entry_still_scales(seam_env):
    pos, _fanout, orders_db, _adb, _parent, child_stub, _acc, _n = seam_env

    # No journal history, child flat → a parent SELL is a real short entry.
    _place(pos, dict(EQ_ORDER, action="SELL"))
    assert child_stub.placed_orders[0][0]["quantity"] == 30
    assert orders_db.list_orders()[0]["status"] == "placed"
