"""Tests for Stage-0 resolver wired into ``cancel_order_with_auth``.

Mirrors ``test_place_order_dispatch.py`` for the cancel-order write path. The
broker side-call and sandbox side-call are both monkeypatched so the tests
assert routing without any broker network calls or sandbox DB writes.
"""

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


def _original_data():
    return {"orderid": "TEST-OID-1", "apikey": "test-api-key"}


def test_cancel_routes_to_broker_when_live(fresh_intent_db, monkeypatch):
    from services import cancel_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    broker_cancel = MagicMock(return_value=({"status": "success"}, 200))
    fake_module = SimpleNamespace(cancel_order=broker_cancel)
    monkeypatch.setattr(cancel_order_service, "import_broker_module", lambda _b: fake_module)

    sandbox_called = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_order", sandbox_called)

    success, response, status = cancel_order_service.cancel_order_with_auth(
        "TEST-OID-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original_data(),
    )

    assert success is True
    assert status == 200
    broker_cancel.assert_called_once()
    sandbox_called.assert_not_called()


def test_cancel_routes_to_sandbox_when_sandbox_intent(fresh_intent_db, monkeypatch):
    from services import cancel_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "mode": "analyze"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_order", sandbox_mock)

    broker_called = MagicMock()
    monkeypatch.setattr(
        cancel_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_order=broker_called),
    )

    success, response, status = cancel_order_service.cancel_order_with_auth(
        "TEST-OID-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original_data(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_called.assert_not_called()


def test_cancel_routes_to_sandbox_when_live_but_analyze_on(fresh_intent_db, monkeypatch):
    """daily_intent='live' + analyze_mode=True must resolve to SANDBOX."""
    from services import cancel_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: True)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "mode": "analyze"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_order", sandbox_mock)

    broker_called = MagicMock()
    monkeypatch.setattr(
        cancel_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_order=broker_called),
    )

    cancel_order_service.cancel_order_with_auth(
        "TEST-OID-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original_data(),
    )

    sandbox_mock.assert_called_once()
    broker_called.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_cancel_rejects_when_skip(fresh_intent_db, monkeypatch):
    from services import cancel_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    sandbox_mock = MagicMock()
    broker_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_order", sandbox_mock)
    monkeypatch.setattr(
        cancel_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_order=broker_mock),
    )

    success, response, status = cancel_order_service.cancel_order_with_auth(
        "TEST-OID-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original_data(),
    )

    assert success is False
    assert response["status"] == "rejected"
    assert response["reason"] == "operator_intent_skip"
    assert "skip" in response["message"].lower()
    assert status == 200
    sandbox_mock.assert_not_called()
    broker_mock.assert_not_called()


def test_cancel_rejects_when_disabled(fresh_intent_db, monkeypatch):
    from services import cancel_order_service

    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    sandbox_mock = MagicMock()
    broker_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_order", sandbox_mock)
    monkeypatch.setattr(
        cancel_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_order=broker_mock),
    )

    success, response, status = cancel_order_service.cancel_order_with_auth(
        "TEST-OID-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original_data(),
    )

    assert success is False
    assert response["status"] == "rejected"
    assert response["reason"] == "no_daily_intent"
    assert "daily_intent" in response["message"]
    assert status == 200
    sandbox_mock.assert_not_called()
    broker_mock.assert_not_called()


def test_cancel_reject_response_shape_matches_existing_convention(fresh_intent_db, monkeypatch):
    from services import cancel_order_service
    from services.mode_service import set_daily_intent

    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_order", MagicMock())
    monkeypatch.setattr(
        cancel_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_order=MagicMock()),
    )

    reject_result = cancel_order_service.cancel_order_with_auth(
        "TEST-OID-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original_data(),
    )

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    broker_cancel = MagicMock(return_value=({"status": "success"}, 200))
    monkeypatch.setattr(
        cancel_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_order=broker_cancel),
    )
    success_result = cancel_order_service.cancel_order_with_auth(
        "TEST-OID-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original_data(),
    )

    assert isinstance(reject_result, tuple) and len(reject_result) == 3
    assert isinstance(success_result, tuple) and len(success_result) == 3
    assert isinstance(reject_result[0], bool) and isinstance(success_result[0], bool)
    assert isinstance(reject_result[1], dict) and isinstance(success_result[1], dict)
    assert isinstance(reject_result[2], int) and isinstance(success_result[2], int)
