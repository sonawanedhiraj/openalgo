"""Tests for the multi-account order fan-out (Phase 2 #474, resized by #496).

Sizing model (issue #496): the child is a smaller account trading the same
strategy — OPENING quantity comes from the child's per-(account, strategy)
``capital_per_trade_inr`` and the live price, never from scaling the parent's
quantity. Covers:

* gating matrix — flag off / unknown mode_key / no eligible children → 0
  scheduled, no journal rows;
* ``compute_opening_qty`` — equity floor, derivative lot affordability,
  unaffordable → 0, missing lotsize/price refusal;
* default deny — no per-trade capital set → ``skipped_no_capital``; quote
  failure → ``skipped_no_quote``; exits are NEVER blocked by either;
* exit asymmetry — position-reducing orders flatten the child's OWN holding;
* end-to-end placed/rejected/no-session journaling with ``sizing_price``;
* per-child failure isolation;
* residual-cash resize (issue #690) — the PARENT's open15 flag resizes an
  unaffordable opening mirror to the child's own leftover cash; flag off /
  other strategies / unreadable cash keep the #637 behavior exactly.

Hermetic: global conftest DB redirect; broker + quotes stubbed; inline executor.
"""

from __future__ import annotations

import pytest

from services.account_fanout_service import compute_opening_qty


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class _Res:
    def __init__(self, status):
        self.status = status


class _StubBroker:
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
    """Flag on, stub broker + quote price, inline executor."""
    import database.account_orders_db as orders_db
    import database.broker_accounts_db as accounts_db
    import services.account_fanout_service as svc

    accounts_db.init_db()
    orders_db.init_db()

    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "true")
    monkeypatch.setattr(svc, "_get_executor", lambda: _InlineExecutor())

    notifications = []
    monkeypatch.setattr(svc, "_notify_operator", lambda msg: notifications.append(msg))

    stub = _StubBroker()
    monkeypatch.setattr(svc, "import_module", lambda name: stub)
    monkeypatch.setattr(svc, "_lookup_lotsize", lambda symbol, exchange: 75)
    # Deterministic sizing price (the quote path is unit-tested separately).
    monkeypatch.setattr(svc, "resolve_sizing_price", lambda od: 500.0)

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


def _make_child(
    accounts_db,
    per_trade=15000.0,
    strategy="sector_follow_cap5_vol",
    login=True,
    set_capital=True,
):
    account = accounts_db.add_account(
        display_name=f"child-{per_trade}-{strategy}-{login}-{set_capital}",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=100000,
    )
    accounts_db.update_account(account["id"], is_enabled=True)
    accounts_db.set_strategies(
        account["id"],
        [strategy],
        capital_per_trade={strategy: per_trade} if set_capital else None,
    )
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

OPT_ORDER = {
    "symbol": "NIFTY30JUL2624500CE",
    "exchange": "NFO",
    "action": "BUY",
    "product": "MIS",
    "pricetype": "MARKET",
    "quantity": 75,
    "strategy": "open15_vol_breakout",
}


# ---------------------------------------------------------------------------
# compute_opening_qty (pure)
# ---------------------------------------------------------------------------


def test_opening_qty_equity():
    assert compute_opening_qty(15000, 500.0, "NSE", None) == 30
    assert compute_opening_qty(15000, 14999.0, "NSE", None) == 1
    assert compute_opening_qty(15000, 15001.0, "NSE", None) == 0  # unaffordable


def test_opening_qty_derivative_lots():
    # premium 150 x lot 75 = 11,250/lot: 15k affords 1 lot, 23k affords 2
    assert compute_opening_qty(15000, 150.0, "NFO", 75) == 75
    assert compute_opening_qty(23000, 150.0, "NFO", 75) == 150
    # unaffordable premium -> 0 lots, honest skip
    assert compute_opening_qty(15000, 250.0, "NFO", 75) == 0
    # unknown lotsize / bad price / bad capital -> refuse
    assert compute_opening_qty(15000, 150.0, "NFO", None) == 0
    assert compute_opening_qty(15000, 0, "NFO", 75) == 0
    assert compute_opening_qty(0, 150.0, "NFO", 75) == 0


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
    _make_child(accounts_db, strategy="futures_follow_cap50")
    assert svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1") == 0
    account = _make_child(accounts_db, per_trade=20000.0)
    accounts_db.update_account(account["id"], is_enabled=False)
    assert svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1") == 0
    assert stub.placed_orders == []


# ---------------------------------------------------------------------------
# Capital-based sizing end-to-end
# ---------------------------------------------------------------------------


def test_mirror_sized_by_capital_not_parent_qty(fanout_env):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    _make_child(accounts_db, per_trade=15000.0)

    # price stubbed at 500 -> 15000/500 = 30 shares, independent of parent's 52
    assert svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P42") == 1
    child_payload, child_token = stub.placed_orders[0]
    assert child_payload["quantity"] == 30
    assert child_token == "k:tok"
    assert EQ_ORDER["quantity"] == 52  # parent payload untouched

    rows = orders_db.list_orders()
    assert rows[0]["status"] == "placed"
    assert rows[0]["child_qty"] == 30
    assert rows[0]["sizing_price"] == 500.0
    assert rows[0]["parent_orderid"] == "P42"
    assert notifications == []


def test_option_mirror_affordability(fanout_env, monkeypatch):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    import services.account_fanout_service as svc_mod

    _make_child(accounts_db, per_trade=15000.0, strategy="open15_vol_breakout")

    # premium 150 x 75 = 11,250/lot -> 1 lot
    monkeypatch.setattr(svc_mod, "resolve_sizing_price", lambda od: 150.0)
    svc.maybe_fan_out(OPT_ORDER, "open15_vol_breakout", "zerodha", "P1")
    assert stub.placed_orders[0][0]["quantity"] == 75
    assert orders_db.list_orders()[0]["sizing_price"] == 150.0

    # premium spikes to 250 -> 18,750/lot -> unaffordable -> honest skip
    stub.placed_orders.clear()
    monkeypatch.setattr(svc_mod, "resolve_sizing_price", lambda od: 250.0)
    svc.maybe_fan_out(OPT_ORDER, "open15_vol_breakout", "zerodha", "P2")
    assert stub.placed_orders == []
    assert orders_db.list_orders()[0]["status"] == "skipped_zero_qty"
    assert any("cannot afford" in n for n in notifications)


def test_no_capital_set_skips_loudly(fanout_env):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    _make_child(accounts_db, set_capital=False)

    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")
    assert stub.placed_orders == []
    assert orders_db.list_orders()[0]["status"] == "skipped_no_capital"
    assert any("no per-trade capital" in n for n in notifications)


def test_quote_failure_skips_loudly(fanout_env, monkeypatch):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    import services.account_fanout_service as svc_mod

    _make_child(accounts_db)
    monkeypatch.setattr(svc_mod, "resolve_sizing_price", lambda od: None)

    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")
    assert stub.placed_orders == []
    assert orders_db.list_orders()[0]["status"] == "skipped_no_quote"
    assert any("no price available" in n for n in notifications)


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


# ---------------------------------------------------------------------------
# Exits — position-true, never blocked by capital/quote availability
# ---------------------------------------------------------------------------


def test_exit_flattens_child_position_ignoring_capital(fanout_env, monkeypatch):
    svc, accounts_db, orders_db, stub, _ = fanout_env
    import services.account_fanout_service as svc_mod

    # Child holds 30; no capital set AND quotes down — the exit must still go.
    _make_child(accounts_db, set_capital=False)
    stub.open_qty = 30
    monkeypatch.setattr(svc_mod, "resolve_sizing_price", lambda od: None)

    exit_order = dict(EQ_ORDER, action="SELL")
    svc.maybe_fan_out(exit_order, "sector_follow_cap5_vol", "zerodha", "P2")
    assert stub.placed_orders[0][0]["quantity"] == 30
    assert orders_db.list_orders()[0]["status"] == "placed"

    # And the flatten tracks the ACTUAL held qty.
    stub.open_qty = 7
    stub.placed_orders.clear()
    svc.maybe_fan_out(exit_order, "sector_follow_cap5_vol", "zerodha", "P3")
    assert stub.placed_orders[0][0]["quantity"] == 7


def test_short_cover_flattens_short(fanout_env):
    svc, accounts_db, orders_db, stub, _ = fanout_env
    _make_child(accounts_db)
    stub.open_qty = -30

    cover = dict(EQ_ORDER, action="BUY")
    svc.maybe_fan_out(cover, "sector_follow_cap5_vol", "zerodha", "P1")
    assert stub.placed_orders[0][0]["quantity"] == 30


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_one_child_failure_never_disturbs_others(fanout_env, monkeypatch):
    svc, accounts_db, orders_db, stub, _ = fanout_env
    _make_child(accounts_db, per_trade=15000.0)  # healthy
    _make_child(accounts_db, per_trade=20000.0, login=False)  # no session

    monkeypatch.setattr(
        stub, "get_open_position", lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    scheduled = svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")
    assert scheduled == 2
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


# ---------------------------------------------------------------------------
# issue #637 — the child must check ITS OWN balance before an opening mirror
#
# #626 fixed this on the parent: the slot budget was never compared with the
# account balance, so the Nth order was refused for insufficient funds. The
# child had the identical hole one account over — it sized every mirror against
# `capital_per_trade_inr` alone, and the parent's clamp cannot help, because the
# child is a different account with a different balance and its own per-trade
# size.
# ---------------------------------------------------------------------------


def _with_cash(monkeypatch, amount, *, record=None):
    """Stub the child's funds read; optionally record the args it was given."""
    import services.account_fanout_service as svc_mod

    def _read(broker, token):
        if record is not None:
            record.append((broker, token))
        return amount

    monkeypatch.setattr(svc_mod, "read_child_cash", _read)


def test_an_unaffordable_opening_mirror_places_nothing(fanout_env, monkeypatch):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    _make_child(accounts_db, per_trade=15000.0)
    # price 500 -> 30 shares -> Rs15,000 needed, Rs9,000 available
    _with_cash(monkeypatch, 9000.0)

    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")

    assert stub.placed_orders == [], "no order may be sent that the child cannot pay for"
    row = orders_db.list_orders()[0]
    assert row["status"] == "skipped_insufficient_funds"
    assert row["child_qty"] == 0
    assert "15,000" in (row["error_text"] or "") and "9,000" in (row["error_text"] or "")
    assert any("only" in n for n in notifications)


def test_an_affordable_mirror_is_placed_normally(fanout_env, monkeypatch):
    svc, accounts_db, orders_db, stub, _n = fanout_env
    _make_child(accounts_db, per_trade=15000.0)
    _with_cash(monkeypatch, 500_000.0)

    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")

    assert len(stub.placed_orders) == 1
    assert orders_db.list_orders()[0]["status"] == "placed"


def test_an_exit_is_never_gated_on_cash(fanout_env, monkeypatch):
    """The load-bearing carve-out.

    A funds blip must never strand a live child position. The reducing branch
    already bypasses capital and quote lookups; the balance check has to sit
    inside the opening branch for exactly the same reason.
    """
    svc, accounts_db, orders_db, stub, _n = fanout_env
    _make_child(accounts_db, per_trade=15000.0)
    stub.open_qty = 30  # the child holds a position to flatten
    _with_cash(monkeypatch, 0.0)

    exit_order = {**EQ_ORDER, "action": "SELL"}
    svc.maybe_fan_out(exit_order, "sector_follow_cap5_vol", "zerodha", "P1")

    assert len(stub.placed_orders) == 1, "a broke child must still be able to flatten"
    assert orders_db.list_orders()[0]["status"] == "placed"


def test_an_unreadable_balance_fails_open(fanout_env, monkeypatch):
    """None is "unknown", never 0.

    The broker still enforces the real limit, and since #637 a refusal is
    recorded honestly — so failing open costs a rejection, whereas failing
    closed would silently stop mirroring altogether.
    """
    svc, accounts_db, orders_db, stub, _n = fanout_env
    _make_child(accounts_db, per_trade=15000.0)
    _with_cash(monkeypatch, None)

    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")

    assert len(stub.placed_orders) == 1
    assert orders_db.list_orders()[0]["status"] == "placed"


def test_the_balance_is_read_with_the_childs_own_token(fanout_env, monkeypatch):
    """#626 got this exact axis wrong once — a funds read answered for another book.

    Asserted on the token actually handed to the funds call, not on a mocked
    return value; mocking only the return is what would hide a parent-token read.
    """
    svc, accounts_db, _orders_db, _stub, _n = fanout_env
    _make_child(accounts_db, per_trade=15000.0)
    seen = []
    _with_cash(monkeypatch, 500_000.0, record=seen)

    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")

    assert seen, "the funds read must actually happen"
    broker, token = seen[0]
    assert broker == "zerodha"
    assert token == "k:tok", "the CHILD's session token (upserted as acct:<id>)"


def test_the_check_can_be_switched_off(fanout_env, monkeypatch):
    svc, accounts_db, _orders_db, stub, _n = fanout_env
    monkeypatch.setenv("MULTI_ACCOUNT_FUNDS_CHECK", "false")
    _make_child(accounts_db, per_trade=15000.0)
    _with_cash(monkeypatch, 1.0)

    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")

    assert len(stub.placed_orders) == 1, "with the flag off, the pre-#637 behaviour returns"


def test_the_sized_value_is_compared_not_the_raw_per_trade_cap(fanout_env, monkeypatch):
    """`compute_opening_qty` floors, so the real cost is <= capital_per_trade.

    Comparing the raw cap would refuse orders the child can actually afford —
    here Rs15,000 configured, Rs14,500 genuinely needed, Rs14,600 in the account.
    """
    svc, accounts_db, orders_db, stub, _n = fanout_env
    import services.account_fanout_service as svc_mod

    _make_child(accounts_db, per_trade=15000.0)
    monkeypatch.setattr(svc_mod, "resolve_sizing_price", lambda od: 725.0)  # 20 sh = 14,500
    _with_cash(monkeypatch, 14_600.0)

    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")

    assert len(stub.placed_orders) == 1, "14,500 fits in 14,600; the raw 15,000 cap would not"
    assert orders_db.list_orders()[0]["status"] == "placed"


def test_can_afford_is_pure_and_fails_open():
    from services.account_fanout_service import can_afford

    assert can_afford(100.0, 100.0), "exactly affordable is affordable"
    assert not can_afford(100.01, 100.0)
    assert can_afford(1_000_000.0, None), "unknown balance never blocks"


# ---------------------------------------------------------------------------
# issue #690 — the parent's open15 residual-sizing flag extends to children
#
# With the PARENT's `residual_sizing_enabled` ON, an opening mirror the child
# cannot afford at full `capital_per_trade_inr` is resized to the child's OWN
# leftover cash (minus the shared reserve) instead of skipped. No per-child
# knob; flag OFF (or any other strategy) is byte-for-byte the #637 behavior.
# ---------------------------------------------------------------------------

O15_EQ_ORDER = {
    **EQ_ORDER,
    "product": "MIS",
    "strategy": "open15_vol_breakout",
}


def _with_residual(monkeypatch, enabled, reserve_pct=0.0):
    """Stub the parent-flag read (its resolution is tested separately below)."""
    import services.account_fanout_service as svc_mod

    monkeypatch.setattr(svc_mod, "open15_residual_params", lambda: (enabled, reserve_pct))


def test_flag_on_resizes_to_the_childs_own_cash(fanout_env, monkeypatch):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    _make_child(accounts_db, per_trade=15000.0, strategy="open15_vol_breakout")
    # full size: 30 sh @500 = Rs15,000; cash Rs9,000; reserve 10% -> budget
    # Rs8,100 -> 16 shares placed instead of a skip
    _with_cash(monkeypatch, 9000.0)
    _with_residual(monkeypatch, True, reserve_pct=10.0)

    svc.maybe_fan_out(O15_EQ_ORDER, "open15_vol_breakout", "zerodha", "P1")

    assert len(stub.placed_orders) == 1
    assert stub.placed_orders[0][0]["quantity"] == 16
    row = orders_db.list_orders()[0]
    assert row["status"] == "placed"
    assert row["child_qty"] == 16
    # labelled, never mistaken for a full-size row (the #643 rule)
    assert "residual_sized" in (row["error_text"] or "")
    assert "8,100" in row["error_text"] and "15,000" in row["error_text"]
    assert notifications == []


def test_flag_on_derivative_residual_floors_to_lots(fanout_env, monkeypatch):
    svc, accounts_db, orders_db, stub, _n = fanout_env
    import services.account_fanout_service as svc_mod

    _make_child(accounts_db, per_trade=30000.0, strategy="open15_vol_breakout")
    # premium 150 x lot 75 = Rs11,250/lot; full cap affords 2 lots (Rs22,500)
    # but cash is Rs15,000 -> residual affords exactly 1 lot
    monkeypatch.setattr(svc_mod, "resolve_sizing_price", lambda od: 150.0)
    _with_cash(monkeypatch, 15000.0)
    _with_residual(monkeypatch, True)

    svc.maybe_fan_out(OPT_ORDER, "open15_vol_breakout", "zerodha", "P1")

    assert stub.placed_orders[0][0]["quantity"] == 75
    assert orders_db.list_orders()[0]["status"] == "placed"


def test_flag_on_but_residual_cannot_afford_one_unit_still_skips(fanout_env, monkeypatch):
    svc, accounts_db, orders_db, stub, notifications = fanout_env
    import services.account_fanout_service as svc_mod

    _make_child(accounts_db, per_trade=15000.0, strategy="open15_vol_breakout")
    # Rs8,000 cash against an Rs11,250 lot -> the residual buys nothing either
    monkeypatch.setattr(svc_mod, "resolve_sizing_price", lambda od: 150.0)
    _with_cash(monkeypatch, 8000.0)
    _with_residual(monkeypatch, True)

    svc.maybe_fan_out(OPT_ORDER, "open15_vol_breakout", "zerodha", "P1")

    assert stub.placed_orders == []
    row = orders_db.list_orders()[0]
    assert row["status"] == "skipped_insufficient_funds"
    # the message says WHICH constraint bound (the #643 rule)
    assert "residual" in (row["error_text"] or "")
    assert "1 lot" in row["error_text"]
    assert any("only" in n for n in notifications)


def test_flag_off_keeps_the_637_skip_exactly(fanout_env, monkeypatch):
    svc, accounts_db, orders_db, stub, _n = fanout_env
    _make_child(accounts_db, per_trade=15000.0, strategy="open15_vol_breakout")
    _with_cash(monkeypatch, 9000.0)
    _with_residual(monkeypatch, False)

    svc.maybe_fan_out(O15_EQ_ORDER, "open15_vol_breakout", "zerodha", "P1")

    assert stub.placed_orders == []
    row = orders_db.list_orders()[0]
    assert row["status"] == "skipped_insufficient_funds"
    assert "residual" not in (row["error_text"] or "")


def test_other_strategies_never_take_the_residual_path(fanout_env, monkeypatch):
    """The flag is an open15 concept; a sector_follow mirror must not read it."""
    svc, accounts_db, orders_db, stub, _n = fanout_env
    import services.account_fanout_service as svc_mod

    _make_child(accounts_db, per_trade=15000.0)
    _with_cash(monkeypatch, 9000.0)

    def _boom():
        raise AssertionError("open15_residual_params must not be consulted")

    monkeypatch.setattr(svc_mod, "open15_residual_params", _boom)
    svc.maybe_fan_out(EQ_ORDER, "sector_follow_cap5_vol", "zerodha", "P1")

    assert stub.placed_orders == []
    assert orders_db.list_orders()[0]["status"] == "skipped_insufficient_funds"


def test_unreadable_cash_never_reaches_the_residual_path(fanout_env, monkeypatch):
    """None cash fails open to the FULL size — a residual of "unknown" would
    silently halve positions, the exact drift #643's parent ledger forbids."""
    svc, accounts_db, orders_db, stub, _n = fanout_env
    _make_child(accounts_db, per_trade=15000.0, strategy="open15_vol_breakout")
    _with_cash(monkeypatch, None)
    _with_residual(monkeypatch, True)

    svc.maybe_fan_out(O15_EQ_ORDER, "open15_vol_breakout", "zerodha", "P1")

    assert stub.placed_orders[0][0]["quantity"] == 30, "full size, not a guess"
    assert "residual" not in (orders_db.list_orders()[0]["error_text"] or "")


def test_residual_params_resolve_through_the_parents_own_definition(monkeypatch):
    """Stored config wins, None falls to the env seed, failure fails safe.

    The fan-out resolves through ``resolve_residual_params`` — the same helper
    ``resolve_day_config`` builds the parent's day config from — so the two
    sides cannot drift.
    """
    import database.open15_breakout_db as o15_db
    from services.account_fanout_service import open15_residual_params

    # stored row wins (an explicit false beats a true env seed and vice versa)
    monkeypatch.setenv("OPEN15_RESIDUAL_SIZING", "false")
    monkeypatch.setattr(
        o15_db,
        "get_config",
        lambda: {"residual_sizing_enabled": True, "residual_reserve_pct": 5.0},
    )
    assert open15_residual_params() == (True, 5.0)

    # no row -> env seeds (reserve clamped by the parent's own clamp)
    monkeypatch.setenv("OPEN15_RESIDUAL_SIZING", "true")
    monkeypatch.setenv("OPEN15_RESIDUAL_RESERVE_PCT", "99")
    monkeypatch.setattr(o15_db, "get_config", lambda: None)
    assert open15_residual_params() == (True, 25.0)

    # a broken read must not start resizing child orders
    def _raise():
        raise RuntimeError("db down")

    monkeypatch.setattr(o15_db, "get_config", _raise)
    assert open15_residual_params() == (False, 0.0)
