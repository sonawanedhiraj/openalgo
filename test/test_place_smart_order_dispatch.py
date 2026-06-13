"""Tests for Stage-0 resolver wired into ``place_smart_order_with_auth``."""

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


def _smart_payload():
    return {
        "apikey": "test-api-key",
        "strategy": "ut",
        "symbol": "INFY",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": "1",
        "pricetype": "MARKET",
        "product": "MIS",
        "position_size": "10",
    }


def _patch_modes(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")


def test_smart_routes_to_broker_when_live(fresh_intent_db, monkeypatch):
    from services import place_smart_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_smart = MagicMock(return_value=(SimpleNamespace(status=200), {"status": "ok"}, "OID-1"))
    monkeypatch.setattr(
        place_smart_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_smartorder_api=broker_smart),
    )
    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_place_smart_order", sandbox_mock)

    success, _, status = place_smart_order_service.place_smart_order_with_auth(
        _smart_payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_smart_payload(),
    )

    assert success is True
    assert status == 200
    broker_smart.assert_called_once()
    sandbox_mock.assert_not_called()


def test_smart_routes_to_sandbox_when_sandbox_intent(fresh_intent_db, monkeypatch):
    from services import place_smart_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_place_smart_order", sandbox_mock)
    broker_smart = MagicMock()
    monkeypatch.setattr(
        place_smart_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_smartorder_api=broker_smart),
    )

    place_smart_order_service.place_smart_order_with_auth(
        _smart_payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_smart_payload(),
    )

    sandbox_mock.assert_called_once()
    broker_smart.assert_not_called()


def test_smart_routes_to_sandbox_when_live_but_analyze_on(fresh_intent_db, monkeypatch):
    from services import place_smart_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_place_smart_order", sandbox_mock)
    broker_smart = MagicMock()
    monkeypatch.setattr(
        place_smart_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_smartorder_api=broker_smart),
    )

    place_smart_order_service.place_smart_order_with_auth(
        _smart_payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_smart_payload(),
    )

    sandbox_mock.assert_called_once()
    broker_smart.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_smart_routes_to_sandbox_when_skip_legacy_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2): legacy intent 'skip' collapses to SANDBOX, not a rejection."""
    from services import place_smart_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX"}, 200))
    broker_smart = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_place_smart_order", sandbox_mock)
    monkeypatch.setattr(
        place_smart_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_smartorder_api=broker_smart),
    )

    success, response, status = place_smart_order_service.place_smart_order_with_auth(
        _smart_payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_smart_payload(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_smart.assert_not_called()


def test_smart_routes_to_sandbox_when_no_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2): no daily_intent row → SANDBOX default (was DISABLED reject)."""
    from services import place_smart_order_service

    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX"}, 200))
    broker_smart = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_place_smart_order", sandbox_mock)
    monkeypatch.setattr(
        place_smart_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_smartorder_api=broker_smart),
    )

    success, response, status = place_smart_order_service.place_smart_order_with_auth(
        _smart_payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_smart_payload(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_smart.assert_not_called()


def test_smart_reject_response_shape_matches_existing_convention(fresh_intent_db, monkeypatch):
    """Both sandbox and live returns are (bool, dict, int) — same outer shape."""
    from services import place_smart_order_service
    from services.mode_service import set_daily_intent

    _patch_modes(monkeypatch)
    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr(
        "services.sandbox_service.sandbox_place_smart_order",
        MagicMock(return_value=(True, {"status": "success", "orderid": "SBX"}, 200)),
    )
    monkeypatch.setattr(
        place_smart_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_smartorder_api=MagicMock()),
    )
    reject_result = place_smart_order_service.place_smart_order_with_auth(
        _smart_payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_smart_payload(),
    )

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    broker_smart = MagicMock(return_value=(SimpleNamespace(status=200), {"status": "ok"}, "OID"))
    monkeypatch.setattr(
        place_smart_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_smartorder_api=broker_smart),
    )
    success_result = place_smart_order_service.place_smart_order_with_auth(
        _smart_payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_smart_payload(),
    )

    for r in (reject_result, success_result):
        assert isinstance(r, tuple) and len(r) == 3
        assert isinstance(r[0], bool)
        assert isinstance(r[1], dict)
        assert isinstance(r[2], int)
