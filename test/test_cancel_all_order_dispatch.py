"""Tests for Stage-0 resolver wired into ``cancel_all_orders_with_auth``."""

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


def _payload():
    return {"apikey": "test-api-key"}


def _patch_modes(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")


def test_cancel_all_routes_to_broker_when_live(fresh_intent_db, monkeypatch):
    from services import cancel_all_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_api = MagicMock(return_value=(["OID1"], []))
    monkeypatch.setattr(
        cancel_all_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_all_orders_api=broker_api),
    )
    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_all_orders", sandbox_mock)

    success, _, status = cancel_all_order_service.cancel_all_orders_with_auth(
        {"apikey": "test"},
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is True
    assert status == 200
    broker_api.assert_called_once()
    sandbox_mock.assert_not_called()


def test_cancel_all_routes_to_sandbox_when_sandbox_intent(fresh_intent_db, monkeypatch):
    from services import cancel_all_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"canceled_count": 1}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_all_orders", sandbox_mock)
    broker_api = MagicMock()
    monkeypatch.setattr(
        cancel_all_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_all_orders_api=broker_api),
    )

    cancel_all_order_service.cancel_all_orders_with_auth(
        {"apikey": "test"},
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    sandbox_mock.assert_called_once()
    broker_api.assert_not_called()


def test_cancel_all_routes_to_sandbox_when_live_but_analyze_on(fresh_intent_db, monkeypatch):
    from services import cancel_all_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"canceled_count": 1}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_all_orders", sandbox_mock)
    broker_api = MagicMock()
    monkeypatch.setattr(
        cancel_all_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_all_orders_api=broker_api),
    )

    cancel_all_order_service.cancel_all_orders_with_auth(
        {"apikey": "test"},
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    sandbox_mock.assert_called_once()
    broker_api.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_cancel_all_rejects_when_skip(fresh_intent_db, monkeypatch):
    from services import cancel_all_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock()
    broker_api = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_all_orders", sandbox_mock)
    monkeypatch.setattr(
        cancel_all_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_all_orders_api=broker_api),
    )

    success, response, status = cancel_all_order_service.cancel_all_orders_with_auth(
        {"apikey": "test"},
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is False
    assert response["status"] == "rejected"
    assert response["reason"] == "operator_intent_skip"
    assert "skip" in response["message"].lower()
    assert status == 200
    sandbox_mock.assert_not_called()
    broker_api.assert_not_called()


def test_cancel_all_rejects_when_disabled(fresh_intent_db, monkeypatch):
    from services import cancel_all_order_service

    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock()
    broker_api = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_all_orders", sandbox_mock)
    monkeypatch.setattr(
        cancel_all_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_all_orders_api=broker_api),
    )

    success, response, status = cancel_all_order_service.cancel_all_orders_with_auth(
        {"apikey": "test"},
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is False
    assert response["status"] == "rejected"
    assert response["reason"] == "no_daily_intent"
    assert status == 200
    sandbox_mock.assert_not_called()
    broker_api.assert_not_called()


def test_cancel_all_reject_response_shape_matches_existing_convention(fresh_intent_db, monkeypatch):
    from services import cancel_all_order_service
    from services.mode_service import set_daily_intent

    _patch_modes(monkeypatch)
    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_all_orders", MagicMock())
    monkeypatch.setattr(
        cancel_all_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_all_orders_api=MagicMock()),
    )

    reject_result = cancel_all_order_service.cancel_all_orders_with_auth(
        {"apikey": "test"},
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    broker_api = MagicMock(return_value=(["OID"], []))
    monkeypatch.setattr(
        cancel_all_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_all_orders_api=broker_api),
    )
    success_result = cancel_all_order_service.cancel_all_orders_with_auth(
        {"apikey": "test"},
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    for r in (reject_result, success_result):
        assert isinstance(r, tuple) and len(r) == 3
        assert isinstance(r[0], bool)
        assert isinstance(r[1], dict)
        assert isinstance(r[2], int)
