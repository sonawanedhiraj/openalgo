"""Tests for Stage-0 resolver wired into ``cancel_gtt_order_with_auth``."""

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


def _original():
    return {"trigger_id": "TRG-1", "apikey": "test-api-key"}


def _patch_modes(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")


def test_cancel_gtt_routes_to_broker_when_live(fresh_intent_db, monkeypatch):
    from services import cancel_gtt_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_cancel = MagicMock(return_value=({"status": "ok"}, 200))
    monkeypatch.setattr(
        cancel_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(cancel_gtt_order=broker_cancel),
    )

    success, _, status = cancel_gtt_order_service.cancel_gtt_order_with_auth(
        "TRG-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original(),
    )

    assert success is True
    assert status == 200
    broker_cancel.assert_called_once()


def test_cancel_gtt_returns_501_when_sandbox_intent(fresh_intent_db, monkeypatch):
    from services import cancel_gtt_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_cancel = MagicMock()
    monkeypatch.setattr(
        cancel_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(cancel_gtt_order=broker_cancel),
    )

    success, _, status = cancel_gtt_order_service.cancel_gtt_order_with_auth(
        "TRG-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original(),
    )

    assert success is False
    assert status == 501
    broker_cancel.assert_not_called()


def test_cancel_gtt_returns_501_when_live_but_analyze_on(fresh_intent_db, monkeypatch):
    from services import cancel_gtt_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch, analyze=True)

    broker_cancel = MagicMock()
    monkeypatch.setattr(
        cancel_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(cancel_gtt_order=broker_cancel),
    )

    success, _, status = cancel_gtt_order_service.cancel_gtt_order_with_auth(
        "TRG-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original(),
    )

    assert status == 501
    broker_cancel.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_cancel_gtt_routes_to_sandbox_when_skip_legacy_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2): legacy intent 'skip' collapses to SANDBOX — GTT is not
    implemented in sandbox, so it surfaces 501 (not a rejection)."""
    from services import cancel_gtt_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_cancel = MagicMock()
    monkeypatch.setattr(
        cancel_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(cancel_gtt_order=broker_cancel),
    )

    success, response, status = cancel_gtt_order_service.cancel_gtt_order_with_auth(
        "TRG-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original(),
    )

    assert success is False
    assert status == 501
    broker_cancel.assert_not_called()


def test_cancel_gtt_routes_to_sandbox_when_no_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2): no daily_intent row → SANDBOX default; GTT surfaces 501."""
    from services import cancel_gtt_order_service

    _patch_modes(monkeypatch)

    broker_cancel = MagicMock()
    monkeypatch.setattr(
        cancel_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(cancel_gtt_order=broker_cancel),
    )

    success, response, status = cancel_gtt_order_service.cancel_gtt_order_with_auth(
        "TRG-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original(),
    )

    assert success is False
    assert status == 501
    broker_cancel.assert_not_called()


def test_cancel_gtt_reject_response_shape_matches_existing_convention(fresh_intent_db, monkeypatch):
    from services import cancel_gtt_order_service
    from services.mode_service import set_daily_intent

    _patch_modes(monkeypatch)
    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr(
        cancel_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(cancel_gtt_order=MagicMock()),
    )
    reject_result = cancel_gtt_order_service.cancel_gtt_order_with_auth(
        "TRG-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original(),
    )

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    broker_cancel = MagicMock(return_value=({"status": "ok"}, 200))
    monkeypatch.setattr(
        cancel_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(cancel_gtt_order=broker_cancel),
    )
    success_result = cancel_gtt_order_service.cancel_gtt_order_with_auth(
        "TRG-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original(),
    )

    for r in (reject_result, success_result):
        assert isinstance(r, tuple) and len(r) == 3
        assert isinstance(r[0], bool)
        assert isinstance(r[1], dict)
        assert isinstance(r[2], int)
