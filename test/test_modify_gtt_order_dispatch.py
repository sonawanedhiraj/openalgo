"""Tests for Stage-0 resolver wired into ``modify_gtt_order_with_auth``."""

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
    return {
        "apikey": "test-api-key",
        "trigger_id": "TRG-1",
        "symbol": "INFY",
        "exchange": "NSE",
    }


def _patch_modes(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")


def test_modify_gtt_routes_to_broker_when_live(fresh_intent_db, monkeypatch):
    from services import modify_gtt_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_mod = MagicMock(return_value=({"status": "ok", "trigger_id": "TRG-1"}, 200))
    monkeypatch.setattr(
        modify_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(modify_gtt_order=broker_mod),
    )

    success, _, status = modify_gtt_order_service.modify_gtt_order_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is True
    assert status == 200
    broker_mod.assert_called_once()


def test_modify_gtt_returns_501_when_sandbox_intent(fresh_intent_db, monkeypatch):
    from services import modify_gtt_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_mod = MagicMock()
    monkeypatch.setattr(
        modify_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(modify_gtt_order=broker_mod),
    )

    success, response, status = modify_gtt_order_service.modify_gtt_order_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is False
    assert status == 501
    broker_mod.assert_not_called()


def test_modify_gtt_returns_501_when_live_but_analyze_on(fresh_intent_db, monkeypatch):
    from services import modify_gtt_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch, analyze=True)

    broker_mod = MagicMock()
    monkeypatch.setattr(
        modify_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(modify_gtt_order=broker_mod),
    )

    success, response, status = modify_gtt_order_service.modify_gtt_order_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert status == 501
    broker_mod.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_modify_gtt_routes_to_sandbox_when_skip_legacy_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2): legacy intent 'skip' collapses to SANDBOX — GTT is not
    implemented in sandbox, so it surfaces 501 (not a rejection)."""
    from services import modify_gtt_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_mod = MagicMock()
    monkeypatch.setattr(
        modify_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(modify_gtt_order=broker_mod),
    )

    success, response, status = modify_gtt_order_service.modify_gtt_order_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is False
    assert status == 501
    broker_mod.assert_not_called()


def test_modify_gtt_routes_to_sandbox_when_no_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2): no daily_intent row → SANDBOX default; GTT surfaces 501."""
    from services import modify_gtt_order_service

    _patch_modes(monkeypatch)

    broker_mod = MagicMock()
    monkeypatch.setattr(
        modify_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(modify_gtt_order=broker_mod),
    )

    success, response, status = modify_gtt_order_service.modify_gtt_order_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is False
    assert status == 501
    broker_mod.assert_not_called()


def test_modify_gtt_reject_response_shape_matches_existing_convention(fresh_intent_db, monkeypatch):
    from services import modify_gtt_order_service
    from services.mode_service import set_daily_intent

    _patch_modes(monkeypatch)
    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr(
        modify_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(modify_gtt_order=MagicMock()),
    )
    reject_result = modify_gtt_order_service.modify_gtt_order_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    broker_mod = MagicMock(return_value=({"status": "ok", "trigger_id": "TRG"}, 200))
    monkeypatch.setattr(
        modify_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(modify_gtt_order=broker_mod),
    )
    success_result = modify_gtt_order_service.modify_gtt_order_with_auth(
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
