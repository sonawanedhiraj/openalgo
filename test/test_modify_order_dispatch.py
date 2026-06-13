"""Tests for Stage-0 resolver wired into ``modify_order_with_auth``."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_intent_db(monkeypatch):
    from database import daily_intent_db as dim

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))

    monkeypatch.setattr(dim, "engine", test_engine)
    monkeypatch.setattr(dim, "db_session", test_session)
    dim.Base.metadata.create_all(test_engine)

    yield dim

    test_session.remove()
    test_engine.dispose()


def _order_data():
    return {
        "apikey": "test-api-key",
        "orderid": "OID-1",
        "symbol": "INFY",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": 1,
        "pricetype": "LIMIT",
        "product": "MIS",
        "price": 100.0,
    }


def _patch_modes(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")


def test_modify_routes_to_broker_when_live(fresh_intent_db, monkeypatch):
    from services import modify_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_mod = MagicMock(return_value=({"status": "success"}, 200))
    monkeypatch.setattr(
        modify_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(modify_order=broker_mod),
    )
    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_modify_order", sandbox_mock)

    success, _, status = modify_order_service.modify_order_with_auth(
        _order_data(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_order_data(),
    )

    assert success is True
    assert status == 200
    broker_mod.assert_called_once()
    sandbox_mock.assert_not_called()


def test_modify_routes_to_sandbox_when_sandbox_intent(fresh_intent_db, monkeypatch):
    from services import modify_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX-1"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_modify_order", sandbox_mock)
    broker_mod = MagicMock()
    monkeypatch.setattr(
        modify_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(modify_order=broker_mod),
    )

    modify_order_service.modify_order_with_auth(
        _order_data(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_order_data(),
    )

    sandbox_mock.assert_called_once()
    broker_mod.assert_not_called()


def test_modify_routes_to_sandbox_when_live_but_analyze_on(fresh_intent_db, monkeypatch):
    from services import modify_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX-1"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_modify_order", sandbox_mock)
    broker_mod = MagicMock()
    monkeypatch.setattr(
        modify_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(modify_order=broker_mod),
    )

    modify_order_service.modify_order_with_auth(
        _order_data(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_order_data(),
    )

    sandbox_mock.assert_called_once()
    broker_mod.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_modify_rejects_when_skip(fresh_intent_db, monkeypatch):
    from services import modify_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock()
    broker_mod = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_modify_order", sandbox_mock)
    monkeypatch.setattr(
        modify_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(modify_order=broker_mod),
    )

    success, response, status = modify_order_service.modify_order_with_auth(
        _order_data(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_order_data(),
    )

    assert success is False
    assert response["status"] == "rejected"
    assert response["reason"] == "operator_intent_skip"
    assert status == 200
    sandbox_mock.assert_not_called()
    broker_mod.assert_not_called()


def test_modify_rejects_when_disabled(fresh_intent_db, monkeypatch):
    from services import modify_order_service

    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock()
    broker_mod = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_modify_order", sandbox_mock)
    monkeypatch.setattr(
        modify_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(modify_order=broker_mod),
    )

    success, response, status = modify_order_service.modify_order_with_auth(
        _order_data(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_order_data(),
    )

    assert success is False
    assert response["status"] == "rejected"
    assert response["reason"] == "no_daily_intent"
    assert status == 200
    sandbox_mock.assert_not_called()
    broker_mod.assert_not_called()


def test_modify_reject_response_shape_matches_existing_convention(fresh_intent_db, monkeypatch):
    from services import modify_order_service
    from services.mode_service import set_daily_intent

    _patch_modes(monkeypatch)
    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.sandbox_service.sandbox_modify_order", MagicMock())
    monkeypatch.setattr(
        modify_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(modify_order=MagicMock()),
    )
    reject_result = modify_order_service.modify_order_with_auth(
        _order_data(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_order_data(),
    )

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    broker_mod = MagicMock(return_value=({"status": "success"}, 200))
    monkeypatch.setattr(
        modify_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(modify_order=broker_mod),
    )
    success_result = modify_order_service.modify_order_with_auth(
        _order_data(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_order_data(),
    )

    for r in (reject_result, success_result):
        assert isinstance(r, tuple) and len(r) == 3
        assert isinstance(r[0], bool)
        assert isinstance(r[1], dict)
        assert isinstance(r[2], int)
