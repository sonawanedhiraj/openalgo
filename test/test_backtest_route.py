"""Integration tests for the ``/backtest`` Flask routes.

The orchestrator (``run_backtest``) is mocked so each test stays focused
on the route's request/response contract — not on bar replay correctness
(covered in ``test_backtest_engine.py``).
"""

from __future__ import annotations

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def app_with_backtest(monkeypatch):
    from blueprints.backtest import backtest_bp
    from database import backtest_db as bdb

    test_engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )
    monkeypatch.setattr(bdb, "engine", test_engine)
    monkeypatch.setattr(bdb, "db_session", test_session)
    bdb.Base.metadata.create_all(test_engine)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(backtest_bp)

    yield app

    test_session.remove()
    test_engine.dispose()


def test_post_run_with_mocked_orchestrator(app_with_backtest, monkeypatch):
    from services import backtest_service

    captured: dict[str, object] = {}

    def fake_run_backtest(**kwargs):
        captured.update(kwargs)
        return 42

    monkeypatch.setattr(backtest_service, "run_backtest", fake_run_backtest)

    client = app_with_backtest.test_client()
    resp = client.post(
        "/backtest/run",
        json={
            "symbols": ["SBIN", "INFY"],
            "from_date": "2026-05-01",
            "to_date": "2026-05-15",
            "interval": "5m",
            "atr_sl_mult": 1.5,
            "rule_names": ["fno_intraday_buy_20"],
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "success"
    assert data["run_id"] == 42
    # The route must forward optional kwargs as-is.
    assert captured["symbols"] == ["SBIN", "INFY"]
    assert captured["from_date"] == "2026-05-01"
    assert captured["interval"] == "5m"
    assert captured["atr_sl_mult"] == 1.5
    assert captured["rule_names"] == ["fno_intraday_buy_20"]


def test_post_run_missing_required_fields_returns_400(app_with_backtest):
    client = app_with_backtest.test_client()
    resp = client.post(
        "/backtest/run",
        json={"symbols": ["SBIN"]},  # missing from_date, to_date
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert "missing required field" in data["message"]


def test_post_run_rejects_non_string_symbols(app_with_backtest):
    client = app_with_backtest.test_client()
    resp = client.post(
        "/backtest/run",
        json={
            "symbols": [123, 456],
            "from_date": "2026-05-01",
            "to_date": "2026-05-15",
        },
    )
    assert resp.status_code == 400
    assert "list of strings" in resp.get_json()["message"]


def test_get_run_details(app_with_backtest):
    from services import backtest_service

    run_id = backtest_service.create_run(
        strategy_name="trending_equity_intraday",
        rule_names=["fno_intraday_buy_20"],
        symbols=["SBIN"],
        from_date="2026-05-01",
        to_date="2026-05-15",
        interval="5m",
        config={"atr_sl_mult": 1.5},
    )

    client = app_with_backtest.test_client()
    resp = client.get(f"/backtest/{run_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["id"] == run_id
    assert data["strategy_name"] == "trending_equity_intraday"
    assert data["status"] == "running"


def test_get_run_details_unknown_id_returns_404(app_with_backtest):
    client = app_with_backtest.test_client()
    resp = client.get("/backtest/9999")
    assert resp.status_code == 404


def test_get_run_trades(app_with_backtest):
    from services import backtest_service

    run_id = backtest_service.create_run(
        strategy_name="s",
        rule_names=[],
        symbols=["SBIN"],
        from_date="2026-05-01",
        to_date="2026-05-15",
        interval="5m",
        config={},
    )
    backtest_service.record_trade(
        run_id=run_id,
        symbol="SBIN",
        direction="LONG",
        entry_at="2026-05-01T09:30:00+05:30",
        entry_price=600.0,
        entry_reason="fno_intraday_buy_20",
        quantity=10,
        atr_at_entry=0.5,
        sl_price=599.0,
        target_price=601.5,
    )

    client = app_with_backtest.test_client()
    resp = client.get(f"/backtest/{run_id}/trades")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["run_id"] == run_id
    assert len(data["trades"]) == 1
    t = data["trades"][0]
    assert t["symbol"] == "SBIN"
    assert t["entry_reason"] == "fno_intraday_buy_20"
    assert t["quantity"] == 10


def test_get_recent_runs(app_with_backtest):
    from services import backtest_service

    for name in ("first", "second", "third"):
        backtest_service.create_run(
            strategy_name=name,
            rule_names=[],
            symbols=[],
            from_date="2026-05-01",
            to_date="2026-05-01",
            interval="5m",
            config={},
        )

    client = app_with_backtest.test_client()
    resp = client.get("/backtest/recent?limit=5")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["limit"] == 5
    # Newest first.
    assert [r["strategy_name"] for r in data["runs"]][:3] == ["third", "second", "first"]
