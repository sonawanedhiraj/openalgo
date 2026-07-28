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
        api_key="k",  # pragma: allowlist secret
        api_secret="s",  # pragma: allowlist secret
        capital_inr=15000,
    )
    adb.set_strategies(
        account["id"],
        ["open15_vol_breakout", "sector_follow_cap5_vol"],
        capital_overrides={"open15_vol_breakout": 50000},
    )
    settings = adb.get_strategy_settings(account["id"])
    assert settings == [
        {"strategy_name": "open15_vol_breakout", "capital_override_inr": 50000.0},
        {"strategy_name": "sector_follow_cap5_vol", "capital_override_inr": None},
    ]

    # Deselecting removes the row AND its override (override only exists while
    # the strategy is selected).
    adb.set_strategies(account["id"], ["sector_follow_cap5_vol"])
    settings = adb.get_strategy_settings(account["id"])
    assert settings == [{"strategy_name": "sector_follow_cap5_vol", "capital_override_inr": None}]

    with pytest.raises(ValueError):
        adb.set_strategies(
            account["id"], ["open15_vol_breakout"], capital_overrides={"open15_vol_breakout": 0}
        )


def test_accounts_for_strategy_carries_override(settings_env):
    adb = settings_env
    account = adb.add_account(
        display_name="FanoutOverrideChild",
        api_key="k",  # pragma: allowlist secret
        api_secret="s",  # pragma: allowlist secret
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
        api_key="k",  # pragma: allowlist secret
        api_secret="s",  # pragma: allowlist secret
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
