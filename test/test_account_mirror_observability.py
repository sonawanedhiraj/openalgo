"""Tests for multi-account observability (Phase 3, issue #476).

Covers the pure message builders in ``account_mirror_summary_service``, the
``today_mirrors`` counts in ``overview()``, the ``/mirror_orders`` endpoint,
and the fire-time gates (flag off / non-trading day → silent no-op).

Hermetic: global conftest DB redirect.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from flask import Flask

from services.account_mirror_summary_service import build_eod_summary, build_login_reminder


@pytest.fixture
def accounts_env():
    import database.account_orders_db as orders_db
    import database.broker_accounts_db as accounts_db

    accounts_db.init_db()
    orders_db.init_db()
    yield accounts_db, orders_db
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


def _account(
    id_=1, name="Dad", enabled=True, strategies=("sector_follow_cap5_vol",), connected=False
):
    return {
        "id": id_,
        "display_name": name,
        "broker_client_id": "AB1234",
        "is_enabled": enabled,
        "strategies": list(strategies),
        "connected": connected,
    }


# ---------------------------------------------------------------------------
# Login reminder builder
# ---------------------------------------------------------------------------


def test_login_reminder_lists_only_actionable_accounts():
    accounts = [
        _account(1, "NeedsLogin", connected=False),
        _account(2, "AlreadyIn", connected=True),
        _account(3, "Disabled", enabled=False, connected=False),
        _account(4, "NoStrategies", strategies=(), connected=False),
    ]
    message = build_login_reminder(accounts)
    assert "NeedsLogin" in message
    assert "AlreadyIn" not in message
    assert "Disabled" not in message
    assert "NoStrategies" not in message
    assert "/accounts" in message


def test_login_reminder_none_when_all_connected():
    assert build_login_reminder([_account(connected=True)]) is None
    assert build_login_reminder([]) is None


def test_login_reminder_nudge_wording():
    message = build_login_reminder([_account()], nudge=True)
    assert "LAST CALL" in message
    assert "15:20" in message


# ---------------------------------------------------------------------------
# EOD summary builder
# ---------------------------------------------------------------------------


def test_eod_summary_aggregates_per_account():
    rows = [
        {"account_id": 1, "status": "placed"},
        {"account_id": 1, "status": "placed"},
        {"account_id": 1, "status": "skipped_zero_qty"},
        {"account_id": 2, "status": "rejected"},
    ]
    accounts = [_account(1, "Dad"), _account(2, "Spouse")]
    message = build_eod_summary(rows, accounts)
    assert "Dad: 2 placed, 1 skipped" in message
    assert "Spouse: 0 placed, 1 REJECTED/error" in message


def test_eod_summary_none_when_no_activity():
    assert build_eod_summary([], [_account()]) is None


# ---------------------------------------------------------------------------
# Fire-time gates
# ---------------------------------------------------------------------------


def test_jobs_silent_when_flag_off(accounts_env, monkeypatch):
    import services.account_mirror_summary_service as svc

    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "false")
    sent = []
    monkeypatch.setattr(svc, "_notify", lambda m: sent.append(m))
    monkeypatch.setattr(svc, "_is_trading_day_today", lambda: True)

    svc._login_reminder_job()
    svc._eod_summary_job()
    assert sent == []


def test_jobs_silent_on_non_trading_day(accounts_env, monkeypatch):
    import services.account_mirror_summary_service as svc

    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "true")
    sent = []
    monkeypatch.setattr(svc, "_notify", lambda m: sent.append(m))
    monkeypatch.setattr(svc, "_is_trading_day_today", lambda: False)

    svc._login_reminder_job()
    svc._eod_summary_job()
    assert sent == []


def test_reminder_job_fires_for_disconnected_child(accounts_env, monkeypatch):
    import services.account_mirror_summary_service as svc

    accounts_db, _ = accounts_env
    account = accounts_db.add_account(
        display_name="ReminderChild",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=100000,
    )
    accounts_db.update_account(account["id"], is_enabled=True)
    accounts_db.set_strategies(account["id"], ["sector_follow_cap5_vol"])

    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "true")
    sent = []
    monkeypatch.setattr(svc, "_notify", lambda m: sent.append(m))
    monkeypatch.setattr(svc, "_is_trading_day_today", lambda: True)

    svc._login_reminder_job()
    assert len(sent) == 1 and "ReminderChild" in sent[0]


# ---------------------------------------------------------------------------
# overview() today_mirrors + /mirror_orders endpoint
# ---------------------------------------------------------------------------


def test_overview_carries_today_mirror_counts(accounts_env):
    accounts_db, orders_db = accounts_env
    from services.broker_accounts_service import overview

    account = accounts_db.add_account(
        display_name="StatsChild",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=100000,
    )
    for status in ("placed", "placed", "skipped_no_session", "error"):
        orders_db.record_mirror_attempt(
            account_id=account["id"],
            strategy_name="sector_follow_cap5_vol",
            symbol="INFY",
            exchange="NSE",
            action="BUY",
            parent_qty=10,
            child_qty=1,
            status=status,
        )

    row = next(a for a in overview()["accounts"] if a["id"] == account["id"])
    assert row["today_mirrors"] == {"placed": 2, "skipped": 1, "failed": 1}


def test_mirror_orders_endpoint(accounts_env):
    accounts_db, orders_db = accounts_env
    from blueprints.broker_accounts import broker_accounts_bp

    account = accounts_db.add_account(
        display_name="EndpointChild",
        api_key="childapikey000001",  # pragma: allowlist secret
        api_secret="childapisecret000001",  # pragma: allowlist secret
        capital_inr=100000,
    )
    orders_db.record_mirror_attempt(
        account_id=account["id"],
        strategy_name="sector_follow_cap5_vol",
        symbol="TATAMOTORS",
        exchange="NSE",
        action="SELL",
        parent_qty=52,
        child_qty=13,
        status="placed",
        broker_orderid="X1",
    )

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"  # pragma: allowlist secret
    app.register_blueprint(broker_accounts_bp)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "tester"

    resp = client.get("/broker_accounts/api/mirror_orders")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["date"] == datetime.utcnow().strftime("%Y-%m-%d")
    assert len(data["orders"]) == 1
    assert data["orders"][0]["account_name"] == "EndpointChild"
    assert data["orders"][0]["broker_orderid"] == "X1"

    assert client.get("/broker_accounts/api/mirror_orders?date=garbage").status_code == 400
    anonymous = app.test_client()
    assert anonymous.get("/broker_accounts/api/mirror_orders").status_code == 401
