"""Tests for Stage-0 resolver wired into ``place_gtt_order_with_auth``."""

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


def _payload():
    return {
        "apikey": "test-api-key",
        "strategy": "ut",
        "symbol": "INFY",
        "exchange": "NSE",
        "trigger_type": "SINGLE",
        "trigger_price": 100.0,
        "action": "BUY",
        "quantity": 1,
        "pricetype": "LIMIT",
        "product": "CNC",
        "price": 100.0,
    }


def _patch_modes(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")


def test_place_gtt_routes_to_broker_when_live(fresh_intent_db, monkeypatch):
    from services import place_gtt_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_place = MagicMock(
        return_value=(SimpleNamespace(status=200), {"status": "ok"}, "TRG-1")
    )
    monkeypatch.setattr(
        place_gtt_order_service, "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=broker_place),
    )

    success, _, status = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(), auth_token="dummy", broker="zerodha", original_data=_payload(),
    )

    assert success is True
    assert status == 200
    broker_place.assert_called_once()


def test_place_gtt_returns_501_when_sandbox_intent(fresh_intent_db, monkeypatch):
    """Sandbox GTT not implemented — expect 501, not broker call."""
    from services import place_gtt_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_place = MagicMock()
    monkeypatch.setattr(
        place_gtt_order_service, "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=broker_place),
    )

    success, response, status = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(), auth_token="dummy", broker="zerodha", original_data=_payload(),
    )

    assert success is False
    assert status == 501
    assert response["mode"] == "analyze"
    broker_place.assert_not_called()


def test_place_gtt_returns_501_when_live_but_analyze_on(fresh_intent_db, monkeypatch):
    from services import place_gtt_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch, analyze=True)

    broker_place = MagicMock()
    monkeypatch.setattr(
        place_gtt_order_service, "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=broker_place),
    )

    success, response, status = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(), auth_token="dummy", broker="zerodha", original_data=_payload(),
    )

    assert success is False
    assert status == 501
    broker_place.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_place_gtt_rejects_when_skip(fresh_intent_db, monkeypatch):
    from services import place_gtt_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_place = MagicMock()
    monkeypatch.setattr(
        place_gtt_order_service, "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=broker_place),
    )

    success, response, status = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(), auth_token="dummy", broker="zerodha", original_data=_payload(),
    )

    assert success is False
    assert response["status"] == "rejected"
    assert response["reason"] == "operator_intent_skip"
    assert status == 200
    broker_place.assert_not_called()


def test_place_gtt_rejects_when_disabled(fresh_intent_db, monkeypatch):
    from services import place_gtt_order_service

    _patch_modes(monkeypatch)

    broker_place = MagicMock()
    monkeypatch.setattr(
        place_gtt_order_service, "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=broker_place),
    )

    success, response, status = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(), auth_token="dummy", broker="zerodha", original_data=_payload(),
    )

    assert success is False
    assert response["status"] == "rejected"
    assert response["reason"] == "no_daily_intent"
    assert status == 200
    broker_place.assert_not_called()


def test_place_gtt_reject_response_shape_matches_existing_convention(fresh_intent_db, monkeypatch):
    from services import place_gtt_order_service
    from services.mode_service import set_daily_intent

    _patch_modes(monkeypatch)
    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr(
        place_gtt_order_service, "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=MagicMock()),
    )
    reject_result = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(), auth_token="dummy", broker="zerodha", original_data=_payload(),
    )

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    broker_place = MagicMock(
        return_value=(SimpleNamespace(status=200), {"status": "ok"}, "TRG-1")
    )
    monkeypatch.setattr(
        place_gtt_order_service, "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=broker_place),
    )
    success_result = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(), auth_token="dummy", broker="zerodha", original_data=_payload(),
    )

    for r in (reject_result, success_result):
        assert isinstance(r, tuple) and len(r) == 3
        assert isinstance(r[0], bool)
        assert isinstance(r[1], dict)
        assert isinstance(r[2], int)
