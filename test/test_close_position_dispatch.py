"""Tests for Stage-0 resolver wired into ``close_position_with_auth``."""

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
    return {"apikey": "test-api-key", "symbol": "INFY", "exchange": "NSE", "product": "MIS"}


def _patch_modes(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")


def test_close_routes_to_broker_when_live(fresh_intent_db, monkeypatch):
    from services import close_position_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_close = MagicMock(return_value=({"status": "ok"}, 200))
    monkeypatch.setattr(
        close_position_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(close_all_positions=broker_close),
    )
    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_close_position", sandbox_mock)

    success, _, status = close_position_service.close_position_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is True
    assert status == 200
    broker_close.assert_called_once()
    sandbox_mock.assert_not_called()


def test_close_routes_to_sandbox_when_sandbox_intent(fresh_intent_db, monkeypatch):
    from services import close_position_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_close_position", sandbox_mock)
    broker_close = MagicMock()
    monkeypatch.setattr(
        close_position_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(close_all_positions=broker_close),
    )

    close_position_service.close_position_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    sandbox_mock.assert_called_once()
    broker_close.assert_not_called()


def test_close_routes_to_sandbox_when_live_but_analyze_on(fresh_intent_db, monkeypatch):
    from services import close_position_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_close_position", sandbox_mock)
    broker_close = MagicMock()
    monkeypatch.setattr(
        close_position_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(close_all_positions=broker_close),
    )

    close_position_service.close_position_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    sandbox_mock.assert_called_once()
    broker_close.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_close_routes_to_sandbox_when_skip_legacy_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2): legacy intent 'skip' collapses to SANDBOX, not a rejection."""
    from services import close_position_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success"}, 200))
    broker_close = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_close_position", sandbox_mock)
    monkeypatch.setattr(
        close_position_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(close_all_positions=broker_close),
    )

    success, response, status = close_position_service.close_position_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_close.assert_not_called()


def test_close_routes_to_sandbox_when_no_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2): no daily_intent row → SANDBOX default (was DISABLED reject)."""
    from services import close_position_service

    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success"}, 200))
    broker_close = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_close_position", sandbox_mock)
    monkeypatch.setattr(
        close_position_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(close_all_positions=broker_close),
    )

    success, response, status = close_position_service.close_position_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_close.assert_not_called()


def test_close_reject_response_shape_matches_existing_convention(fresh_intent_db, monkeypatch):
    """Both sandbox and live returns are (bool, dict, int) — same outer shape."""
    from services import close_position_service
    from services.mode_service import set_daily_intent

    _patch_modes(monkeypatch)
    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr(
        "services.sandbox_service.sandbox_close_position",
        MagicMock(return_value=(True, {"status": "success"}, 200)),
    )
    monkeypatch.setattr(
        close_position_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(close_all_positions=MagicMock()),
    )
    reject_result = close_position_service.close_position_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    broker_close = MagicMock(return_value=({"status": "ok"}, 200))
    monkeypatch.setattr(
        close_position_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(close_all_positions=broker_close),
    )
    success_result = close_position_service.close_position_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    for r in (reject_result, success_result):
        assert isinstance(r, tuple) and len(r) == 3
        assert isinstance(r[0], bool)
        assert isinstance(r[1], dict)
        assert isinstance(r[2], int)
