"""Tests for the orphan-flatten sweep + duplicate-exit echo guard (issue #659).

Two stranding shapes motivated this:

* **Gap A** — the parent entry was ACK'd (fan-out fired, the child filled) and
  later demoted to ``fill='paper'`` (#626); the parent's paper row sends no
  exit, so no child exit mirror ever fires.
* **Gap B** — the child's exit mirror was REJECTED; the parent's retry job
  re-flattens parent rows only, so nothing ever retries the child.

The sweep closes any (account, symbol) whose net PLACED mirror quantity today
is non-zero, capped at what the child actually holds. The echo guard stops a
later parent exit from falling into the opening branch (naked short) after the
sweep already closed the child.

Hermetic: global conftest DB redirect; broker stubbed; no network.
"""

from __future__ import annotations

import datetime as dt

import pytest

from services.account_fanout_service import compute_mirror_net


class _Res:
    def __init__(self, status):
        self.status = status


class _StubBroker:
    """Configurable per-symbol book + capture of placed orders."""

    def __init__(self):
        self.book: dict[str, int] = {}
        self.raise_on_book = False
        self.place_status = 200
        self.placed_orders: list[dict] = []

    def get_open_position(self, symbol, exchange, product, auth):
        if self.raise_on_book:
            raise RuntimeError("book unreadable")
        return str(self.book.get(symbol, 0))

    def place_order_api(self, data, auth):
        self.placed_orders.append(dict(data))
        if self.place_status == 200:
            return _Res(200), {"status": "success"}, f"SWEEP{len(self.placed_orders)}"
        return _Res(400), {"message": "outside price band"}, None


@pytest.fixture
def sweep_env(monkeypatch):
    import database.account_orders_db as orders_db
    import database.broker_accounts_db as accounts_db
    import services.account_fanout_service as svc

    accounts_db.init_db()
    orders_db.init_db()

    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "true")

    notifications: list[str] = []
    monkeypatch.setattr(svc, "_notify_operator", lambda msg: notifications.append(msg))

    stub = _StubBroker()
    monkeypatch.setattr(svc, "import_module", lambda name: stub)

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


STRATEGY = "open15_vol_breakout"


def _make_child(accounts_db, name="child-a", login=True, enabled=True):
    account = accounts_db.add_account(
        display_name=name,
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=100000,
    )
    accounts_db.update_account(account["id"], is_enabled=enabled)
    accounts_db.set_strategies(account["id"], [STRATEGY], capital_per_trade={STRATEGY: 30000})
    if login:
        from database.auth_db import upsert_auth

        upsert_auth(accounts_db.auth_name(account["id"]), "k:tok", "zerodha")
    return account


def _journal(
    orders_db, account_id, action, qty, *, status="placed", symbol="HAL", parent="P1", error=None
):
    return orders_db.record_mirror_attempt(
        account_id=account_id,
        strategy_name=STRATEGY,
        symbol=symbol,
        exchange="NSE",
        action=action,
        product="MIS",
        parent_qty=qty,
        child_qty=qty,
        status=status,
        parent_orderid=parent,
        error_text=error,
    )


# ---------------------------------------------------------------------------
# compute_mirror_net (pure)
# ---------------------------------------------------------------------------


def test_net_aggregates_buys_minus_sells():
    rows = [
        {
            "account_id": 1,
            "symbol": "HAL",
            "exchange": "NSE",
            "product": "MIS",
            "action": "BUY",
            "child_qty": 150,
            "parent_orderid": "P1",
        },
        {
            "account_id": 1,
            "symbol": "HAL",
            "exchange": "NSE",
            "product": "MIS",
            "action": "SELL",
            "child_qty": 60,
            "parent_orderid": "P2",
        },
    ]
    nets = compute_mirror_net(rows)
    assert nets[(1, "HAL", "NSE", "MIS")]["net"] == 90
    assert nets[(1, "HAL", "NSE", "MIS")]["parent_orderid"] == "P2"


def test_net_keys_are_per_account_and_symbol():
    rows = [
        {
            "account_id": 1,
            "symbol": "HAL",
            "exchange": "NSE",
            "product": "MIS",
            "action": "BUY",
            "child_qty": 10,
            "parent_orderid": None,
        },
        {
            "account_id": 2,
            "symbol": "SAIL",
            "exchange": "NSE",
            "product": "MIS",
            "action": "SELL",
            "child_qty": 5,
            "parent_orderid": None,
        },
        {
            "account_id": 1,
            "symbol": "HAL",
            "exchange": "NSE",
            "product": "MIS",
            "action": "NOPE",
            "child_qty": 99,
            "parent_orderid": None,
        },  # ignored
    ]
    nets = compute_mirror_net(rows)
    assert nets[(1, "HAL", "NSE", "MIS")]["net"] == 10
    assert nets[(2, "SAIL", "NSE", "MIS")]["net"] == -5


# ---------------------------------------------------------------------------
# todays_placed_rows (DB helper)
# ---------------------------------------------------------------------------


def test_todays_placed_rows_filters_status_strategy_and_day(sweep_env):
    svc, accounts_db, orders_db, stub, _ = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150)
    _journal(orders_db, child["id"], "SELL", 150, status="rejected")  # excluded
    orders_db.record_mirror_attempt(
        account_id=child["id"],
        strategy_name="simplified_engine",
        symbol="HAL",
        exchange="NSE",
        action="BUY",
        product="MIS",
        parent_qty=1,
        child_qty=1,
        status="placed",
    )  # other strategy — excluded
    stale = _journal(orders_db, child["id"], "BUY", 999)
    try:
        row = (
            orders_db.db_session.query(orders_db.AccountOrder)
            .filter(orders_db.AccountOrder.id == stale["id"])
            .first()
        )
        row.created_at = dt.datetime.utcnow() - dt.timedelta(days=2)
        orders_db.db_session.commit()
    finally:
        orders_db.db_session.remove()

    rows = orders_db.todays_placed_rows(STRATEGY)
    assert [(r["action"], r["child_qty"]) for r in rows] == [("BUY", 150)]


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def test_gap_a_stranded_entry_is_flattened_and_idempotent(sweep_env):
    """Placed BUY, no exit, child holds it → one SELL, then nothing."""
    svc, accounts_db, orders_db, stub, notifications = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150)
    stub.book["HAL"] = 150

    assert svc.flatten_stranded_child_mirrors(STRATEGY, reason="test gap A") == 1
    assert len(stub.placed_orders) == 1
    order = stub.placed_orders[0]
    assert (order["action"], order["quantity"], order["pricetype"], order["product"]) == (
        "SELL",
        150,
        "MARKET",
        "MIS",
    )
    rows = orders_db.todays_placed_rows(STRATEGY)
    assert [(r["action"], r["child_qty"]) for r in rows] == [("BUY", 150), ("SELL", 150)]
    assert "orphan_flatten: test gap A" in rows[-1]["error_text"]
    assert any("Orphan flatten" in n for n in notifications)

    # Idempotent: the sweep's own row nets the key to 0.
    stub.book["HAL"] = 0
    assert svc.flatten_stranded_child_mirrors(STRATEGY, reason="again") == 0
    assert len(stub.placed_orders) == 1


def test_gap_b_rejected_child_exit_is_retried(sweep_env):
    """Placed BUY + REJECTED SELL → net is still long → re-SELL."""
    svc, accounts_db, orders_db, stub, _ = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150)
    _journal(orders_db, child["id"], "SELL", 150, status="rejected")
    stub.book["HAL"] = 150

    assert svc.flatten_stranded_child_mirrors(STRATEGY, reason="test gap B") == 1
    assert stub.placed_orders[0]["action"] == "SELL"
    assert stub.placed_orders[0]["quantity"] == 150


def test_round_tripped_mirrors_are_left_alone(sweep_env):
    svc, accounts_db, orders_db, stub, notifications = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150)
    _journal(orders_db, child["id"], "SELL", 150)

    assert svc.flatten_stranded_child_mirrors(STRATEGY) == 0
    assert stub.placed_orders == [] and notifications == []


def test_affirmative_flat_book_sends_nothing(sweep_env):
    """Net says long but the child never actually filled → no order."""
    svc, accounts_db, orders_db, stub, _ = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150)
    stub.book["HAL"] = 0

    assert svc.flatten_stranded_child_mirrors(STRATEGY) == 0
    assert stub.placed_orders == []
    # and no extra journal row was written
    assert len(orders_db.todays_placed_rows(STRATEGY)) == 1


def test_unreadable_book_still_squares_off_capped_at_net(sweep_env):
    """The #626 asymmetry: believed filled → unreadable book still exits."""
    svc, accounts_db, orders_db, stub, _ = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150)
    stub.raise_on_book = True

    assert svc.flatten_stranded_child_mirrors(STRATEGY) == 1
    assert stub.placed_orders[0]["quantity"] == 150


def test_book_sign_mismatch_is_alert_only(sweep_env):
    svc, accounts_db, orders_db, stub, notifications = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150)
    stub.book["HAL"] = -50  # child is SHORT while our mirrors say long

    assert svc.flatten_stranded_child_mirrors(STRATEGY) == 0
    assert stub.placed_orders == []
    assert any("disagrees" in n for n in notifications)


def test_qty_capped_at_the_smaller_of_net_and_book(sweep_env):
    """A child's unrelated same-symbol holding is not ours to close."""
    svc, accounts_db, orders_db, stub, _ = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150)
    stub.book["HAL"] = 250  # 100 of these are the child's own business

    svc.flatten_stranded_child_mirrors(STRATEGY)
    assert stub.placed_orders[0]["quantity"] == 150

    # and the partial-fill shape: the book holds LESS than we placed
    stub.placed_orders.clear()
    _journal(orders_db, child["id"], "BUY", 150, symbol="SAIL")
    stub.book["SAIL"] = 90
    svc.flatten_stranded_child_mirrors(STRATEGY, symbols=["SAIL"])
    assert stub.placed_orders[0]["quantity"] == 90


def test_master_switch_off_is_alert_only(sweep_env, monkeypatch):
    svc, accounts_db, orders_db, stub, notifications = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150)
    stub.book["HAL"] = 150
    monkeypatch.setattr(svc, "is_multi_account_enabled", lambda: False)

    assert svc.flatten_stranded_child_mirrors(STRATEGY) == 0
    assert stub.placed_orders == []
    assert any("DISABLED" in n for n in notifications)


def test_symbols_filter_scopes_the_sweep(sweep_env):
    svc, accounts_db, orders_db, stub, _ = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150, symbol="HAL")
    _journal(orders_db, child["id"], "BUY", 200, symbol="SAIL")
    stub.book.update({"HAL": 150, "SAIL": 200})

    assert svc.flatten_stranded_child_mirrors(STRATEGY, symbols=["HAL"]) == 1
    assert len(stub.placed_orders) == 1
    assert stub.placed_orders[0]["symbol"] == "HAL"


def test_no_session_is_journaled_and_alerted(sweep_env):
    svc, accounts_db, orders_db, stub, notifications = sweep_env
    child = _make_child(accounts_db, login=False)
    _journal(orders_db, child["id"], "BUY", 150)
    stub.book["HAL"] = 150

    assert svc.flatten_stranded_child_mirrors(STRATEGY) == 0
    assert stub.placed_orders == []
    assert any("no broker session" in n for n in notifications)


def test_account_no_longer_eligible_is_alert_only(sweep_env):
    svc, accounts_db, orders_db, stub, notifications = sweep_env
    child = _make_child(accounts_db, enabled=False)
    _journal(orders_db, child["id"], "BUY", 150)
    stub.book["HAL"] = 150

    assert svc.flatten_stranded_child_mirrors(STRATEGY) == 0
    assert stub.placed_orders == []
    assert any("no longer enabled" in n for n in notifications)


def test_rejected_sweep_order_is_journaled_and_alerted(sweep_env):
    svc, accounts_db, orders_db, stub, notifications = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150)
    stub.book["HAL"] = 150
    stub.place_status = 400

    assert svc.flatten_stranded_child_mirrors(STRATEGY, reason="r") == 1
    rows = orders_db.list_orders()
    rejected = [r for r in rows if r["status"] == "rejected"]
    assert len(rejected) == 1
    assert "orphan_flatten" in rejected[0]["error_text"]
    assert "outside price band" in rejected[0]["error_text"]
    assert any("Orphan flatten REJECTED" in n for n in notifications)


def test_one_broken_key_never_stops_the_rest(sweep_env, monkeypatch):
    svc, accounts_db, orders_db, stub, _ = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150, symbol="HAL")
    _journal(orders_db, child["id"], "BUY", 200, symbol="SAIL")
    stub.book.update({"HAL": 150, "SAIL": 200})

    real = svc._flatten_one_stranded_key

    def explode_on_hal(**kwargs):
        if kwargs["symbol"] == "HAL":
            raise RuntimeError("boom")
        return real(**kwargs)

    monkeypatch.setattr(svc, "_flatten_one_stranded_key", explode_on_hal)
    assert svc.flatten_stranded_child_mirrors(STRATEGY) == 1
    assert stub.placed_orders[0]["symbol"] == "SAIL"


# ---------------------------------------------------------------------------
# Duplicate-exit echo guard in the normal mirror path
# ---------------------------------------------------------------------------


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def _fan_out_exit(svc, action="SELL", symbol="HAL"):
    order = {
        "symbol": symbol,
        "exchange": "NSE",
        "action": action,
        "product": "MIS",
        "pricetype": "MARKET",
        "quantity": 100,
        "strategy": STRATEGY,
    }
    return svc.maybe_fan_out(order, STRATEGY, "zerodha", "P9")


def test_echo_guard_blocks_duplicate_exit_after_sweep(sweep_env, monkeypatch):
    """Sweep closed the child; the parent's later exit must NOT open a short."""
    svc, accounts_db, orders_db, stub, notifications = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150)
    _journal(orders_db, child["id"], "SELL", 150)  # the sweep's own row
    stub.book["HAL"] = 0  # child is flat now
    monkeypatch.setattr(svc, "_get_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(svc, "resolve_sizing_price", lambda od: 500.0)

    assert _fan_out_exit(svc) == 1  # scheduled, but the guard must skip it
    assert stub.placed_orders == []
    rows = orders_db.list_orders()
    assert rows[0]["status"] == "skipped_no_position"
    assert any("Duplicate exit echo" in n for n in notifications)


def test_echo_guard_blocks_exit_after_a_partial_capped_sweep(sweep_env, monkeypatch):
    """Child part-filled 90 of 150; the sweep closed the 90 (net stays +60).
    The parent's later exit must still be recognized as an echo via the sweep
    row's marker, not sized from capital into a naked short."""
    svc, accounts_db, orders_db, stub, _ = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 150)
    _journal(orders_db, child["id"], "SELL", 90, error="orphan_flatten: partial")
    stub.book["HAL"] = 0
    monkeypatch.setattr(svc, "_get_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(svc, "resolve_sizing_price", lambda od: 500.0)

    _fan_out_exit(svc)
    assert stub.placed_orders == []
    assert orders_db.list_orders()[0]["status"] == "skipped_no_position"


def test_echo_guard_lets_a_second_entry_through_while_net_open(sweep_env, monkeypatch):
    """A same-direction repeat with the net still open is a genuine second
    opening (the affordability re-mirror shape) — not an echo."""
    svc, accounts_db, orders_db, stub, _ = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "BUY", 60)  # net +60, no sweep row
    stub.book["HAL"] = 0
    monkeypatch.setattr(svc, "_get_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(svc, "resolve_sizing_price", lambda od: 500.0)

    _fan_out_exit(svc, action="BUY")
    assert len(stub.placed_orders) == 1 and stub.placed_orders[0]["action"] == "BUY"


def test_echo_guard_lets_a_fresh_entry_through(sweep_env, monkeypatch):
    """T+1 shape: today's last row is an exit (SELL); a new BUY entry is NOT
    an echo and must still scale normally."""
    svc, accounts_db, orders_db, stub, _ = sweep_env
    child = _make_child(accounts_db)
    _journal(orders_db, child["id"], "SELL", 150)  # this morning's T+1 exit
    stub.book["HAL"] = 0
    monkeypatch.setattr(svc, "_get_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(svc, "resolve_sizing_price", lambda od: 500.0)

    _fan_out_exit(svc, action="BUY")
    assert len(stub.placed_orders) == 1
    assert stub.placed_orders[0]["action"] == "BUY"
    assert stub.placed_orders[0]["quantity"] == 60  # 30000 / 500
