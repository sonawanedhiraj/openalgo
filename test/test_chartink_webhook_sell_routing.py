"""Audit-column routing test for the chartink simplified-engine webhook.

The fno-scan-cycle scheduler POSTs both the BUY and SELL legs to the same
webhook URL, distinguished only by the payload's ``scan_name`` field. The
blueprint must route the parsed symbols to ``screener_buy`` or
``screener_sell`` accordingly (defaulting to BUY when scan_name is absent).

The engine itself is mocked — we only assert which audit column the symbols
land in. Fixtures mirror ``test_chartink_webhook_audit.py``.
"""

import json
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_cycle_db(monkeypatch):
    """Point scan_cycle_db at a fresh in-memory SQLite for one test."""
    from database import scan_cycle_db as scdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))

    monkeypatch.setattr(scdb, "engine", test_engine)
    monkeypatch.setattr(scdb, "db_session", test_session)
    scdb.Base.metadata.create_all(test_engine)

    yield scdb

    test_session.remove()
    test_engine.dispose()


@pytest.fixture
def app_with_chartink(fresh_cycle_db):
    """Bare Flask app with the chartink blueprint mounted, limiter bound."""
    from blueprints.chartink import chartink_bp
    from limiter import limiter

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    limiter.init_app(app)
    app.register_blueprint(chartink_bp)
    return app


def _fake_strategy(name="chartink_FnO_intraday_buy"):
    return SimpleNamespace(
        id=42,
        user_id="test_user",
        name=name,
        is_active=True,
        is_intraday=False,  # bypass time-window guards
        start_time="09:15",
        end_time="15:00",
        squareoff_time="15:15",
    )


class _FakeEngineService:
    def process_chartink_webhook(self, user_id, strategy_name, payload):
        return {"status": "success", "message": "test stub"}


def _wire(monkeypatch, syms):
    """Stub strategy lookup, symbol parser, and engine service."""
    from blueprints import chartink as chartink_module

    monkeypatch.setattr(chartink_module, "get_strategy_by_webhook_id", lambda _id: _fake_strategy())
    monkeypatch.setattr(
        "services.simplified_stock_engine_service.parse_chartink_symbols",
        lambda payload: list(syms),
    )
    monkeypatch.setattr(
        "services.simplified_stock_engine_service.get_simplified_stock_engine_service",
        lambda: _FakeEngineService(),
    )


def _post(client, payload):
    return client.post(
        "/chartink/simplified-stock-engine/test-webhook-id",
        data=json.dumps(payload),
        content_type="application/json",
    )


def _cycle(fresh_cycle_db):
    return (
        fresh_cycle_db.db_session.query(fresh_cycle_db.ScanCycle)
        .filter_by(cycle_kind="chartink")
        .first()
    )


def _as_list(col):
    """JSON column → list ([] when NULL or '[]')."""
    return json.loads(col) if col else []


def test_buy_leg_routes_to_screener_buy(app_with_chartink, fresh_cycle_db, monkeypatch):
    _wire(monkeypatch, ["RELIANCE", "INFY"])
    resp = _post(
        app_with_chartink.test_client(),
        {"scan_name": "BUY FnO Intraday Buy 20", "stocks": "RELIANCE,INFY"},
    )
    assert resp.status_code == 200, resp.data

    cycle = _cycle(fresh_cycle_db)
    assert _as_list(cycle.screener_buy) == ["RELIANCE", "INFY"]
    assert _as_list(cycle.screener_sell) == []


def test_sell_leg_routes_to_screener_sell(app_with_chartink, fresh_cycle_db, monkeypatch):
    _wire(monkeypatch, ["TRENT", "SAIL"])
    resp = _post(
        app_with_chartink.test_client(),
        {"scan_name": "SELL FnO Intraday Sell", "stocks": "TRENT,SAIL"},
    )
    assert resp.status_code == 200, resp.data

    cycle = _cycle(fresh_cycle_db)
    assert _as_list(cycle.screener_sell) == ["TRENT", "SAIL"]
    assert _as_list(cycle.screener_buy) == []


def test_missing_scan_name_defaults_to_buy(app_with_chartink, fresh_cycle_db, monkeypatch):
    _wire(monkeypatch, ["TCS"])
    resp = _post(app_with_chartink.test_client(), {"stocks": "TCS"})
    assert resp.status_code == 200, resp.data

    cycle = _cycle(fresh_cycle_db)
    assert _as_list(cycle.screener_buy) == ["TCS"]
    assert _as_list(cycle.screener_sell) == []


def test_mixed_case_sell_routes_to_screener_sell(app_with_chartink, fresh_cycle_db, monkeypatch):
    _wire(monkeypatch, ["NATIONALUM"])
    resp = _post(
        app_with_chartink.test_client(),
        {"scan_name": "sell foo", "stocks": "NATIONALUM"},
    )
    assert resp.status_code == 200, resp.data

    cycle = _cycle(fresh_cycle_db)
    assert _as_list(cycle.screener_sell) == ["NATIONALUM"]
    assert _as_list(cycle.screener_buy) == []
