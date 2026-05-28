"""Tests for Stage-0 resolver wired into ``process_basket_order_with_auth``."""

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
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )

    monkeypatch.setattr(dim, "engine", test_engine)
    monkeypatch.setattr(dim, "db_session", test_session)
    dim.Base.metadata.create_all(test_engine)

    yield dim

    test_session.remove()
    test_engine.dispose()


def _basket():
    return {
        "apikey": "test-api-key",
        "strategy": "ut",
        "orders": [
            {
                "symbol": "INFY",
                "exchange": "NSE",
                "action": "BUY",
                "quantity": 1,
                "pricetype": "MARKET",
                "product": "MIS",
            }
        ],
    }


def _patch_modes(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")


def test_basket_routes_to_broker_when_live(fresh_intent_db, monkeypatch):
    from services import basket_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_place = MagicMock(
        return_value=(SimpleNamespace(status=200), {"status": "ok"}, "OID-1")
    )
    monkeypatch.setattr(
        basket_order_service, "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place),
    )
    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)

    success, _, status = basket_order_service.process_basket_order_with_auth(
        _basket(), auth_token="dummy", broker="zerodha", original_data=_basket(),
    )

    assert success is True
    assert status == 200
    broker_place.assert_called()
    sandbox_mock.assert_not_called()


def test_basket_routes_to_sandbox_when_sandbox_intent(fresh_intent_db, monkeypatch):
    from services import basket_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX-1"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    broker_place = MagicMock()
    monkeypatch.setattr(
        basket_order_service, "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place),
    )
    # Avoid touching quotes_service in the analyze branch
    from services import quotes_service as qs
    monkeypatch.setattr(
        qs, "get_multiquotes",
        lambda **kw: (False, {"message": "skipped"}, 500),
    )

    success, _, status = basket_order_service.process_basket_order_with_auth(
        _basket(), auth_token="dummy", broker="zerodha", original_data=_basket(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called()
    broker_place.assert_not_called()


def test_basket_routes_to_sandbox_when_live_but_analyze_on(fresh_intent_db, monkeypatch):
    from services import basket_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX-1"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    broker_place = MagicMock()
    monkeypatch.setattr(
        basket_order_service, "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place),
    )
    from services import quotes_service as qs
    monkeypatch.setattr(
        qs, "get_multiquotes",
        lambda **kw: (False, {"message": "skipped"}, 500),
    )

    basket_order_service.process_basket_order_with_auth(
        _basket(), auth_token="dummy", broker="zerodha", original_data=_basket(),
    )

    sandbox_mock.assert_called()
    broker_place.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_basket_rejects_when_skip(fresh_intent_db, monkeypatch):
    from services import basket_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock()
    broker_place = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    monkeypatch.setattr(
        basket_order_service, "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place),
    )

    success, response, status = basket_order_service.process_basket_order_with_auth(
        _basket(), auth_token="dummy", broker="zerodha", original_data=_basket(),
    )

    assert success is False
    assert response["status"] == "rejected"
    assert response["reason"] == "operator_intent_skip"
    assert status == 200
    sandbox_mock.assert_not_called()
    broker_place.assert_not_called()


def test_basket_rejects_when_disabled(fresh_intent_db, monkeypatch):
    from services import basket_order_service

    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock()
    broker_place = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    monkeypatch.setattr(
        basket_order_service, "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place),
    )

    success, response, status = basket_order_service.process_basket_order_with_auth(
        _basket(), auth_token="dummy", broker="zerodha", original_data=_basket(),
    )

    assert success is False
    assert response["status"] == "rejected"
    assert response["reason"] == "no_daily_intent"
    assert status == 200
    sandbox_mock.assert_not_called()
    broker_place.assert_not_called()


def test_basket_reject_response_shape_matches_existing_convention(fresh_intent_db, monkeypatch):
    from services import basket_order_service
    from services.mode_service import set_daily_intent

    _patch_modes(monkeypatch)
    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", MagicMock())
    monkeypatch.setattr(
        basket_order_service, "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=MagicMock()),
    )
    reject_result = basket_order_service.process_basket_order_with_auth(
        _basket(), auth_token="dummy", broker="zerodha", original_data=_basket(),
    )

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    broker_place = MagicMock(
        return_value=(SimpleNamespace(status=200), {"status": "ok"}, "OID-1")
    )
    monkeypatch.setattr(
        basket_order_service, "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place),
    )
    success_result = basket_order_service.process_basket_order_with_auth(
        _basket(), auth_token="dummy", broker="zerodha", original_data=_basket(),
    )

    for r in (reject_result, success_result):
        assert isinstance(r, tuple) and len(r) == 3
        assert isinstance(r[0], bool)
        assert isinstance(r[1], dict)
        assert isinstance(r[2], int)
