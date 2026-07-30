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
# Per-selected-strategy capital-per-trade (issue #496)
# ---------------------------------------------------------------------------


def test_capital_per_trade_crud(settings_env):
    adb = settings_env
    account = adb.add_account(
        display_name="PerTradeChild",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=100000,
    )
    adb.set_strategies(
        account["id"],
        ["open15_vol_breakout", "sector_follow_cap5_vol"],
        capital_per_trade={"open15_vol_breakout": 15000},
    )
    settings = adb.get_strategy_settings(account["id"])
    assert settings == [
        {"strategy_name": "open15_vol_breakout", "capital_per_trade_inr": 15000.0},
        {"strategy_name": "sector_follow_cap5_vol", "capital_per_trade_inr": None},
    ]

    # Deselecting removes the row AND its per-trade capital with it.
    adb.set_strategies(account["id"], ["sector_follow_cap5_vol"])
    assert adb.get_strategy_settings(account["id"]) == [
        {"strategy_name": "sector_follow_cap5_vol", "capital_per_trade_inr": None}
    ]

    with pytest.raises(ValueError):
        adb.set_strategies(
            account["id"], ["open15_vol_breakout"], capital_per_trade={"open15_vol_breakout": 0}
        )


def test_accounts_for_strategy_carries_per_trade(settings_env):
    adb = settings_env
    account = adb.add_account(
        display_name="FanoutPerTradeChild",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=100000,
    )
    adb.update_account(account["id"], is_enabled=True)
    adb.set_strategies(
        account["id"],
        ["open15_vol_breakout", "sector_follow_cap5_vol"],
        capital_per_trade={"open15_vol_breakout": 15000},
    )
    assert adb.accounts_for_strategy("open15_vol_breakout")[0]["capital_per_trade_inr"] == 15000.0
    assert adb.accounts_for_strategy("sector_follow_cap5_vol")[0]["capital_per_trade_inr"] is None


def test_strategies_endpoint_accepts_capital_per_trade(settings_env):
    from blueprints.broker_accounts import broker_accounts_bp

    adb = settings_env
    account = adb.add_account(
        display_name="EndpointPerTradeChild",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=100000,
    )

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"  # pragma: allowlist secret
    app.register_blueprint(broker_accounts_bp)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "tester"

    resp = client.post(
        f"/broker_accounts/api/{account['id']}/strategies",
        json={
            "strategies": ["open15_vol_breakout"],
            "capital_per_trade": {"open15_vol_breakout": 15000, "ignored_strategy": 999},
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["strategy_settings"] == [
        {"strategy_name": "open15_vol_breakout", "capital_per_trade_inr": 15000.0}
    ]

    resp = client.post(
        f"/broker_accounts/api/{account['id']}/strategies",
        json={
            "strategies": ["open15_vol_breakout"],
            "capital_per_trade": {"open15_vol_breakout": "not-a-number"},
        },
    )
    assert resp.status_code == 400
