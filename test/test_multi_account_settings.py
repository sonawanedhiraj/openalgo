"""UI-configurable multi-account settings (issue #484).

Resolution contract: the ``multi_account_settings`` DB row WINS; the env vars
(`MULTI_ACCOUNT_ENABLED`, `PRIMARY_BOOK_CAPITAL`) are only the first-read seed
and the fallback while no row exists. UI changes apply at fire time — no
restart. Hermetic via the global conftest DB redirect.
"""

from __future__ import annotations

import pytest
from flask import Flask


@pytest.fixture
def settings_env(monkeypatch):
    import database.account_orders_db as _orders_db
    import database.broker_accounts_db as adb

    adb.init_db()
    _orders_db.init_db()
    yield adb
    import database.account_orders_db as orders_db

    try:
        orders_db.db_session.query(orders_db.AccountOrder).delete()
        orders_db.db_session.commit()
    finally:
        orders_db.db_session.remove()
    try:
        adb.db_session.query(adb.MultiAccountSettings).delete()
        adb.db_session.query(adb.AccountStrategy).delete()
        adb.db_session.query(adb.BrokerAccount).delete()
        adb.db_session.commit()
    finally:
        adb.db_session.remove()


def test_first_read_seeds_from_env(settings_env, monkeypatch):
    adb = settings_env
    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "true")
    monkeypatch.setenv("PRIMARY_BOOK_CAPITAL", "2000000")

    settings = adb.get_multi_account_settings()
    assert settings["enabled"] is True
    assert settings["primary_book_capital"] == 2_000_000.0
    assert settings["updated_by"] == "env-seed"


def test_db_row_wins_over_env(settings_env, monkeypatch):
    adb = settings_env
    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "false")
    adb.set_multi_account_settings(enabled=True, updated_by="test")

    # Env says false; the UI-written row says true — row wins.
    from services.broker_accounts_service import is_multi_account_enabled

    assert is_multi_account_enabled() is True

    # And the flip back applies immediately, still ignoring env.
    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "true")
    adb.set_multi_account_settings(enabled=False, updated_by="test")
    assert is_multi_account_enabled() is False


def test_partial_update_and_validation(settings_env):
    adb = settings_env
    adb.set_multi_account_settings(enabled=True, updated_by="test")
    settings = adb.set_multi_account_settings(primary_book_capital=500000, updated_by="test")
    assert settings["enabled"] is True  # untouched by the partial update
    assert settings["primary_book_capital"] == 500_000.0
    with pytest.raises(ValueError):
        adb.set_multi_account_settings(primary_book_capital=0)


def test_fanout_capital_consults_db(settings_env):
    adb = settings_env
    from services.account_fanout_service import _primary_book_capital

    adb.set_multi_account_settings(primary_book_capital=500000, updated_by="test")
    assert _primary_book_capital() == 500_000.0
    adb.set_multi_account_settings(primary_book_capital=1000000, updated_by="test")
    assert _primary_book_capital() == 1_000_000.0  # no restart needed


def test_settings_endpoint(settings_env):
    from blueprints.broker_accounts import broker_accounts_bp

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"  # pragma: allowlist secret
    app.register_blueprint(broker_accounts_bp)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "tester"

    resp = client.put(
        "/broker_accounts/api/settings",
        json={"enabled": True, "primary_book_capital": 750000},
    )
    assert resp.status_code == 200
    settings = resp.get_json()["settings"]
    assert settings["enabled"] is True
    assert settings["primary_book_capital"] == 750_000.0
    assert settings["updated_by"] == "ui:tester"

    # Reflected in the overview payload immediately.
    resp = client.get("/broker_accounts/api")
    data = resp.get_json()
    assert data["multi_account_enabled"] is True
    assert data["primary_book_capital"] == 750_000.0

    assert (
        client.put("/broker_accounts/api/settings", json={"primary_book_capital": "x"}).status_code
        == 400
    )
    assert client.put("/broker_accounts/api/settings", json={}).status_code == 400
    anonymous = app.test_client()
    assert anonymous.put("/broker_accounts/api/settings", json={"enabled": True}).status_code == 401


# ---------------------------------------------------------------------------
# Per-selected-strategy capital overrides (issue #486)
# ---------------------------------------------------------------------------


def test_strategy_capital_override_crud(settings_env):
    adb = settings_env
    account = adb.add_account(
        display_name="OverrideChild",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=15000,
    )
    adb.set_strategies(
        account["id"],
        ["open15_vol_breakout", "sector_follow_cap5_vol"],
        capital_overrides={"open15_vol_breakout": 50000},
    )
    settings = adb.get_strategy_settings(account["id"])
    assert settings == [
        {
            "strategy_name": "open15_vol_breakout",
            "capital_override_inr": 50000.0,
            "min_one_lot": False,
        },
        {
            "strategy_name": "sector_follow_cap5_vol",
            "capital_override_inr": None,
            "min_one_lot": False,
        },
    ]

    # Deselecting removes the row AND its override (override only exists while
    # the strategy is selected).
    adb.set_strategies(account["id"], ["sector_follow_cap5_vol"])
    settings = adb.get_strategy_settings(account["id"])
    assert settings == [
        {
            "strategy_name": "sector_follow_cap5_vol",
            "capital_override_inr": None,
            "min_one_lot": False,
        }
    ]

    with pytest.raises(ValueError):
        adb.set_strategies(
            account["id"], ["open15_vol_breakout"], capital_overrides={"open15_vol_breakout": 0}
        )


def test_accounts_for_strategy_carries_override(settings_env):
    adb = settings_env
    account = adb.add_account(
        display_name="FanoutOverrideChild",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=15000,
    )
    adb.update_account(account["id"], is_enabled=True)
    adb.set_strategies(
        account["id"],
        ["open15_vol_breakout", "sector_follow_cap5_vol"],
        capital_overrides={"open15_vol_breakout": 50000},
    )
    with_override = adb.accounts_for_strategy("open15_vol_breakout")
    assert with_override[0]["capital_override_inr"] == 50000.0
    without = adb.accounts_for_strategy("sector_follow_cap5_vol")
    assert without[0]["capital_override_inr"] is None


def test_fanout_factor_uses_override(settings_env, monkeypatch):
    """The mirror factor for a strategy WITH an override sizes from it; the
    same account's other strategy still sizes from base capital."""
    import services.account_fanout_service as svc
    from database.auth_db import upsert_auth

    adb = settings_env
    adb.set_multi_account_settings(enabled=True, primary_book_capital=1_000_000, updated_by="test")
    account = adb.add_account(
        display_name="FactorChild",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=15000,
    )
    adb.update_account(account["id"], is_enabled=True)
    adb.set_strategies(
        account["id"],
        ["open15_vol_breakout", "sector_follow_cap5_vol"],
        capital_overrides={"open15_vol_breakout": 50000},
    )
    upsert_auth(adb.auth_name(account["id"]), "k:tok", "zerodha")

    class _Res:
        status = 200

    class _Stub:
        placed = []

        def get_open_position(self, *a):
            return "0"

        def place_order_api(self, data, auth):
            _Stub.placed.append(dict(data))
            return _Res(), {"status": "success"}, "OID1"

    class _Inline:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(svc, "_get_executor", lambda: _Inline())
    monkeypatch.setattr(svc, "import_module", lambda n: _Stub())
    monkeypatch.setattr(svc, "_notify_operator", lambda m: None)

    order = {
        "symbol": "TATAMOTORS",
        "exchange": "NSE",
        "action": "BUY",
        "product": "MIS",
        "quantity": 200,
    }
    # open15 with override 50k -> factor 0.05 -> 10 shares
    svc.maybe_fan_out(dict(order), "open15_vol_breakout", "zerodha", "P1")
    assert _Stub.placed[-1]["quantity"] == 10
    # sector_follow without override -> base 15k -> factor 0.015 -> 3 shares
    svc.maybe_fan_out(dict(order), "sector_follow_cap5_vol", "zerodha", "P2")
    assert _Stub.placed[-1]["quantity"] == 3

    # cleanup child auth row
    import database.auth_db as auth_db_module

    auth_db_module.db_session.query(auth_db_module.Auth).filter(
        auth_db_module.Auth.name.like("acct:%")
    ).delete(synchronize_session=False)
    auth_db_module.db_session.commit()
    auth_db_module.db_session.remove()
    auth_db_module.auth_cache.clear()


# ---------------------------------------------------------------------------
# Min-1-lot toggle (issue #490)
# ---------------------------------------------------------------------------


def test_compute_child_qty_min_one_lot():
    from services.account_fanout_service import compute_child_qty

    # 1-lot parent, factor < 1: default skips, flag rounds UP to 1 lot.
    assert compute_child_qty(75, 0.667, "NFO", 75, "BUY", 0) == 0
    assert compute_child_qty(75, 0.667, "NFO", 75, "BUY", 0, min_one_lot=True) == 75
    # Multi-lot parents still scale down normally (flag changes nothing).
    assert compute_child_qty(225, 0.667, "NFO", 75, "BUY", 0, min_one_lot=True) == 150
    # Equity is untouched by the flag (lot concept only).
    assert compute_child_qty(3, 0.25, "NSE", None, "BUY", 0, min_one_lot=True) == 0
    # Unknown lotsize still refuses even with the flag.
    assert compute_child_qty(75, 0.667, "NFO", None, "BUY", 0, min_one_lot=True) == 0
    # Exit guard path (position-reducing) is unaffected: qty = held.
    assert compute_child_qty(150, 0.667, "NFO", 75, "SELL", 75, min_one_lot=True) == 75


def test_min_one_lot_persistence_and_fanout_read(settings_env):
    adb = settings_env
    account = adb.add_account(
        display_name="LotChild",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=15000,
    )
    adb.update_account(account["id"], is_enabled=True)
    adb.set_strategies(
        account["id"],
        ["open15_vol_breakout", "sector_follow_cap5_vol"],
        capital_overrides={"open15_vol_breakout": 666667},
        min_one_lot={"open15_vol_breakout": True},
    )
    settings = adb.get_strategy_settings(account["id"])
    assert settings[0] == {
        "strategy_name": "open15_vol_breakout",
        "capital_override_inr": 666667.0,
        "min_one_lot": True,
    }
    assert settings[1]["min_one_lot"] is False

    rows = adb.accounts_for_strategy("open15_vol_breakout")
    assert rows[0]["min_one_lot"] is True
    assert adb.accounts_for_strategy("sector_follow_cap5_vol")[0]["min_one_lot"] is False

    # Deselect wipes the flag with the row (same rule as the capital override).
    adb.set_strategies(account["id"], ["sector_follow_cap5_vol"])
    assert all(not s["min_one_lot"] for s in adb.get_strategy_settings(account["id"]))


def test_min_one_lot_end_to_end_option_mirror(settings_env, monkeypatch):
    """1-lot parent option order mirrors as 1 lot with the flag, skips without."""
    import services.account_fanout_service as svc
    from database.auth_db import upsert_auth

    adb = settings_env
    adb.set_multi_account_settings(enabled=True, primary_book_capital=1_000_000, updated_by="t")
    account = adb.add_account(
        display_name="OptChild",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=15000,
    )
    adb.update_account(account["id"], is_enabled=True)
    upsert_auth(adb.auth_name(account["id"]), "k:tok", "zerodha")

    class _Res:
        status = 200

    class _Stub:
        placed = []

        def get_open_position(self, *a):
            return "0"

        def place_order_api(self, data, auth):
            _Stub.placed.append(dict(data))
            return _Res(), {"status": "success"}, "OID1"

    class _Inline:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(svc, "_get_executor", lambda: _Inline())
    monkeypatch.setattr(svc, "import_module", lambda n: _Stub())
    monkeypatch.setattr(svc, "_notify_operator", lambda m: None)
    monkeypatch.setattr(svc, "_lookup_lotsize", lambda s, e: 75)

    option_order = {
        "symbol": "NIFTY30JUL2624500CE",
        "exchange": "NFO",
        "action": "BUY",
        "product": "MIS",
        "quantity": 75,  # 1 lot on the parent
    }

    # Without the flag: factor 0.667 floors to 0 lots -> skipped.
    adb.set_strategies(
        account["id"], ["open15_vol_breakout"], capital_overrides={"open15_vol_breakout": 666667}
    )
    svc.maybe_fan_out(dict(option_order), "open15_vol_breakout", "zerodha", "P1")
    assert _Stub.placed == []
    from database.account_orders_db import list_orders

    assert list_orders()[0]["status"] == "skipped_zero_qty"

    # With the flag: mirrors 1 lot.
    adb.set_strategies(
        account["id"],
        ["open15_vol_breakout"],
        capital_overrides={"open15_vol_breakout": 666667},
        min_one_lot={"open15_vol_breakout": True},
    )
    svc.maybe_fan_out(dict(option_order), "open15_vol_breakout", "zerodha", "P2")
    assert len(_Stub.placed) == 1
    assert _Stub.placed[0]["quantity"] == 75
    assert list_orders()[0]["status"] == "placed"

    import database.auth_db as auth_db_module

    auth_db_module.db_session.query(auth_db_module.Auth).filter(
        auth_db_module.Auth.name.like("acct:%")
    ).delete(synchronize_session=False)
    auth_db_module.db_session.commit()
    auth_db_module.db_session.remove()
    auth_db_module.auth_cache.clear()
