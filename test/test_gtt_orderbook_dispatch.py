"""Tests for Stage-0 resolver wired into ``get_gtt_orderbook_with_auth`` (read path)."""

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


def _patch_modes(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")


def test_gtt_orderbook_reads_from_broker_when_live(fresh_intent_db, monkeypatch):
    from services import gtt_orderbook_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_get = MagicMock(return_value=({"triggers": []}, 200))
    monkeypatch.setattr(
        gtt_orderbook_service, "import_broker_gtt_module",
        lambda _b: SimpleNamespace(get_gtt_book=broker_get),
    )

    success, _, status = gtt_orderbook_service.get_gtt_orderbook_with_auth(
        auth_token="dummy", broker="zerodha", original_data={"apikey": "test"},
    )

    assert success is True
    assert status == 200
    broker_get.assert_called_once()


def test_gtt_orderbook_returns_501_when_sandbox_intent(fresh_intent_db, monkeypatch):
    """Sandbox GTT read not implemented — 501 surfaced as expected."""
    from services import gtt_orderbook_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_get = MagicMock()
    monkeypatch.setattr(
        gtt_orderbook_service, "import_broker_gtt_module",
        lambda _b: SimpleNamespace(get_gtt_book=broker_get),
    )

    success, response, status = gtt_orderbook_service.get_gtt_orderbook_with_auth(
        auth_token="dummy", broker="zerodha", original_data={"apikey": "test"},
    )

    assert success is False
    assert status == 501
    assert response["mode"] == "analyze"
    broker_get.assert_not_called()


def test_gtt_orderbook_reads_from_broker_when_skip_or_disabled(fresh_intent_db, monkeypatch):
    """For reads, SKIP and DISABLED fall through to the broker (no order rejection)."""
    from services import gtt_orderbook_service
    from services.mode_service import set_daily_intent

    _patch_modes(monkeypatch)

    broker_get = MagicMock(return_value=({"triggers": []}, 200))
    monkeypatch.setattr(
        gtt_orderbook_service, "import_broker_gtt_module",
        lambda _b: SimpleNamespace(get_gtt_book=broker_get),
    )

    # DISABLED (no intent row)
    success, _, status = gtt_orderbook_service.get_gtt_orderbook_with_auth(
        auth_token="dummy", broker="zerodha", original_data={"apikey": "test"},
    )
    assert success is True
    assert status == 200
    assert broker_get.call_count == 1

    # SKIP
    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    success, _, status = gtt_orderbook_service.get_gtt_orderbook_with_auth(
        auth_token="dummy", broker="zerodha", original_data={"apikey": "test"},
    )
    assert success is True
    assert status == 200
    assert broker_get.call_count == 2
