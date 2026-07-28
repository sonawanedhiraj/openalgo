"""Tests for the multi-account order fan-out (Phase 2, issue #474).

Covers ``services/account_fanout_service`` + ``database/account_orders_db``:

* gating matrix — flag off / unknown mode_key / no eligible children → 0
  scheduled, no journal rows;
* sizing math (``compute_child_qty``) — equity floor, derivative lot floor,
  missing-lotsize refusal, zero-qty result;
* exit asymmetry guard — position-reducing orders take the child's OWN held
  quantity, opening orders scale;
* end-to-end mirror against a stubbed broker module: placed / rejected /
  no-session / zero-qty all journaled with the right status, child order
  carries the scaled quantity, parent payload is never mutated;
* per-child failure isolation — one child's exception never disturbs others.

Hermetic: global conftest DB redirect; broker module stubbed; the executor is
replaced with a synchronous inline runner so assertions are deterministic.
"""

from __future__ import annotations

import pytest

from services.account_fanout_service import compute_child_qty


class _InlineExecutor:
    """Runs submitted work synchronously — deterministic tests."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class _Res:
    def __init__(self, status):
        self.status = status


class _StubBroker:
    """Stands in for broker.zerodha.api.order_api."""

    def __init__(self, place_status=200, open_qty=0, raise_on_place=False):
        self.place_status = place_status
        self.open_qty = open_qty
        self.raise_on_place = raise_on_place
        self.placed_orders = []

    def get_open_position(self, symbol, exchange, product, auth):
        return str(self.open_qty)

    def place_order_api(self, data, auth):
        if self.raise_on_place:
            raise RuntimeError("broker exploded")
        self.placed_orders.append((dict(data), auth))
        if self.place_status == 200:
            return _Res(200), {"status": "success"}, f"CHILD{len(self.placed_orders)}"
        return _Res(400), {"message": "insufficient margin"}, None


@pytest.fixture
def fanout_env(monkeypatch):
    """Flag on, deterministic capital, inline executor, stub broker + notify."""
    import database.account_orders_db as orders_db
    import database.broker_accounts_db as accounts_db
    import services.account_fanout_service as svc

    accounts_db.init_db()
    orders_db.init_db()

    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "true")
    monkeypatch.setenv("PRIMARY_BOOK_CAPITAL", "1000000")
    monkeypatch.setattr(svc, "_get_executor", lambda: _InlineExecutor())

    notifications = []
    monkeypatch.setattr(svc, "_notify_operator", lambda msg: notifications.append(msg))

    stub = _StubBroker()
    monkeypatch.setattr(svc, "import_module", lambda name: stub)
    monkeypatch.setattr(svc, "_lookup_lotsize", lambda symbol, exchange: 75)

    yield svc, accounts_db, orders_db, stub, notifications

    for db in (orders_db, accounts_db):
        try:
            if db is orders_db:
                db.db_session.query(db.AccountOrder).delete()
            else:
                db.db_session.query(db.MultiAccountSettings).delete()
                db.db_session.query(db.AccountStrategy).delete()
                db.db_session.query(db.BrokerAccount).delete()
            db.db_session.commit()
        finally:
            db.db_session.remove()

    import database.auth_db as auth_db_module

    try:
        auth_db_module.db_session.query(auth_db_module.Auth).filter(
            auth_db_module.Auth.name.like("acct:%")
        ).delete(synchronize_session=False)
        auth_db_module.db_session.commit()
    finally:
        auth_db_module.db_session.remove()
    auth_db_module.auth_cache.clear()


def _make_child(accounts_db, capital=250000.0, strategy="sector_follow_cap5_vol", login=True):
    account = accounts_db.add_account(
        display_name=f"child-{capital}-{strategy}-{login}",
        api_key="k",  # pragma: allowlist secret
        api_secret="s",  # pragma: allowlist secret
        capital_inr=capital,
    )
    accounts_db.update_account(account["id"], is_enabled=True)
    accounts_db.set_strategies(account["id"], [strategy])
    if login:
        from database.auth_db import upsert_auth

        upsert_auth(accounts_db.auth_name(account["id"]), "k:tok", "zerodha")
    return account


EQ_ORDER = {
    "symbol": "TATAMOTORS",
    "exchange": "NSE",
    "action": "BUY",
    "product": "CNC",
    "pricetype": "MARKET",
    "quantity": 52,
    "strategy": "sector_follow_cap5_vol",
}


# ---------------------------------------------------------------------------
# compute_child_qty (pure)
# ---------------------------------------------------------------------------


def test_sizing_equity_floor():
    assert compute_child_qty(52, 0.25, "NSE", None, "BUY", 0) == 13
    assert compute_child_qty(52, 0.05, "NSE", None, "BUY", 0) == 2
    assert compute_child_qty(3, 0.25, "NSE", None, "BUY", 0) == 0


def test_sizing_derivative_lot_floor():
    # 2 lots of 75 at 0.5x -> 1 lot; at 0.25x -> 0 (skip, not a 37-share order)
    assert compute_child_qty(150, 0.5, "NFO", 75, "BUY", 0) == 75
    assert compute_child_qty(150, 0.25, "NFO", 75, "BUY", 0) == 0
    # Unknown lotsize on a derivative exchange -> refuse to guess
    assert compute_child_qty(150, 0.5, "NFO", None, "BUY", 0) == 0


def test_exit_guard_uses_child_position():
    # SELL against a long: flatten what the child holds, ignore scaling
    assert compute_child_qty(52, 0.25, "NSE", None, "SELL", 13) == 13
    # BUY covering a short
    assert compute_child_qty(150, 0.5, "NFO", 75, "BUY", -75) == 75
    # SELL with no position -> opening short, scales normally
    assert compute_child_qty(52, 0.25, "NSE", None, "SELL", 0) == 13


# ---------------------------------------------------------------------------
# Gating matrix
# ---------------------------------------------------------------------------


def test_flag_off_is_noop(fanout_env, monkeypatch):
    svc, accounts_db, orders_db, stub, _ = fanout_env
    _make_child(accounts_db)
    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "false")
    assert svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1") == 0
    assert stub.placed_orders == []
    assert orders_db.list_orders() == []


def test_unknown_mode_key_is_noop(fanout_env):
    svc, accounts_db, orders_db, stub, _ = fanout_env
    _make_child(accounts_db)
    assert svc.maybe_fan_out(EQ_ORDER, "not_a_strategy", "zerodha", "P1") == 0
    assert svc.maybe_fan_out(EQ_ORDER, None, "zerodha", "P1") == 0
    assert stub.placed_orders == []


def test_no_eligible_children_is_noop(fanout_env):
    svc, accounts_db, orders_db, stub, _ = fanout_env
    # child exists but selected a DIFFERENT strategy
    _make_child(accounts_db, strategy="futures_follow_cap50")
    assert svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1") == 0
    # and a disabled child never mirrors
    account = _make_child(accounts_db, capital=300000.0)
    accounts_db.update_account(account["id"], is_enabled=False)
    assert svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1") == 0
    assert stub.placed_orders == []


# ---------------------------------------------------------------------------
# End-to-end mirrors against the stub broker
# ---------------------------------------------------------------------------


def test_mirror_placed_and_journaled(fanout_env):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    _make_child(accounts_db, capital=250000.0)

    scheduled = svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P42")
    assert scheduled == 1
    assert len(stub.placed_orders) == 1
    child_payload, child_token = stub.placed_orders[0]
    assert child_payload["quantity"] == 13  # 52 x 0.25
    assert child_token == "k:tok"
    assert EQ_ORDER["quantity"] == 52  # parent payload untouched

    rows = orders_db.list_orders()
    assert len(rows) == 1
    assert rows[0]["status"] == "placed"
    assert rows[0]["child_qty"] == 13
    assert rows[0]["parent_orderid"] == "P42"
    assert rows[0]["broker_orderid"] == "CHILD1"
    assert notifications == []  # success is quiet


def test_mirror_rejected_journals_and_notifies(fanout_env):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    _make_child(accounts_db)
    stub.place_status = 400

    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")
    rows = orders_db.list_orders()
    assert rows[0]["status"] == "rejected"
    assert "insufficient margin" in rows[0]["error_text"]
    assert any("REJECTED" in n for n in notifications)


def test_mirror_skipped_no_session(fanout_env):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    _make_child(accounts_db, login=False)

    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")
    assert stub.placed_orders == []
    assert orders_db.list_orders()[0]["status"] == "skipped_no_session"
    assert any("no broker session" in n for n in notifications)


def test_mirror_skipped_zero_qty(fanout_env):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    _make_child(accounts_db, capital=10000.0)  # factor 0.01 -> 0 shares of 52

    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")
    assert stub.placed_orders == []
    assert orders_db.list_orders()[0]["status"] == "skipped_zero_qty"
    assert any("scales to 0" in n for n in notifications)


def test_exit_uses_child_position_end_to_end(fanout_env):
    svc, accounts_db, orders_db, stub, _ = fanout_env
    _make_child(accounts_db, capital=250000.0)
    stub.open_qty = 13  # the child holds 13 from the earlier entry

    exit_order = dict(EQ_ORDER, action="SELL", quantity=52)
    svc.maybe_fan_out(exit_order, "sector_follow_cap5_vol", "zerodha", "P2")
    child_payload, _ = stub.placed_orders[0]
    assert child_payload["quantity"] == 13  # held qty, not floor(52 x 0.25) coincidence:
    # prove it's the position by changing the held qty
    stub.open_qty = 7
    stub.placed_orders.clear()
    svc.maybe_fan_out(exit_order, "sector_follow_cap5_vol", "zerodha", "P3")
    assert stub.placed_orders[0][0]["quantity"] == 7


def test_one_child_failure_never_disturbs_others(fanout_env, monkeypatch):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    _make_child(accounts_db, capital=250000.0)  # healthy
    _make_child(accounts_db, capital=500000.0, login=False)  # no session

    # Position lookup explodes for everyone — must degrade to scaling, not raise
    monkeypatch.setattr(
        stub, "get_open_position", lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    scheduled = svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")
    assert scheduled == 2
    # healthy child still placed (scaled), broken-session child journaled skip
    statuses = sorted(r["status"] for r in orders_db.list_orders())
    assert statuses == ["placed", "skipped_no_session"]


def test_broker_exception_journals_error(fanout_env):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    _make_child(accounts_db)
    stub.raise_on_place = True

    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")
    row = orders_db.list_orders()[0]
    assert row["status"] == "error"
    assert "broker exploded" in row["error_text"]
    assert any("ERROR" in n for n in notifications)
