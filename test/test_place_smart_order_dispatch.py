"""Tests for the per-strategy dispatch wired into ``place_smart_order_with_auth``.

Issue #440 — UI-driven routing: a smart order fires on the live broker ONLY
when Analyze is off AND the payload's strategy has a ``strategy_mode`` row set
to live. Everything else routes to sandbox (default deny).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_mode_db(monkeypatch):
    """Point strategy_mode_db at a fresh in-memory SQLite for one test."""
    from database import strategy_mode_db as sm

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(sm, "engine", eng)
    monkeypatch.setattr(sm, "db_session", sess)
    sm.Base.query = sess.query_property()
    sm.Base.metadata.create_all(eng)
    yield sm
    sess.remove()
    eng.dispose()


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


def _patch_analyze(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)


def test_smart_routes_to_broker_when_strategy_live(fresh_mode_db, monkeypatch):
    """strategy_mode row='live' + analyze off → broker.place_smartorder_api fires."""
    from services import place_smart_order_service

    fresh_mode_db._set_mode_unchecked("ut", "live", updated_by="op")
    _patch_analyze(monkeypatch)

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


def test_smart_routes_to_sandbox_when_strategy_row_sandbox(fresh_mode_db, monkeypatch):
    """A sandbox-flagged strategy stays sandbox even with analyze off."""
    from services import place_smart_order_service

    fresh_mode_db._set_mode_unchecked("ut", "sandbox", updated_by="op")
    _patch_analyze(monkeypatch)

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


def test_smart_routes_to_sandbox_when_live_but_analyze_on(fresh_mode_db, monkeypatch):
    """Analyze mode is the platform kill switch: a live row cannot beat it."""
    from services import place_smart_order_service

    fresh_mode_db._set_mode_unchecked("ut", "live", updated_by="op")
    _patch_analyze(monkeypatch, analyze=True)

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


def test_smart_routes_to_sandbox_when_no_row_default_denies(fresh_mode_db, monkeypatch):
    """Default deny: no strategy_mode row → sandbox even with analyze off."""
    from services import place_smart_order_service

    _patch_analyze(monkeypatch)

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


def test_smart_retired_global_row_has_no_effect(fresh_mode_db, monkeypatch):
    """A manually re-created __global__ live row must not route anything live."""
    from services import place_smart_order_service

    fresh_mode_db._set_mode_unchecked("__global__", "live", updated_by="op")
    _patch_analyze(monkeypatch)

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


def test_smart_reject_response_shape_matches_existing_convention(fresh_mode_db, monkeypatch):
    """Both sandbox and live returns are (bool, dict, int) — same outer shape."""
    from services import place_smart_order_service

    _patch_analyze(monkeypatch)
    monkeypatch.setattr(
        "services.sandbox_service.sandbox_place_smart_order",
        MagicMock(return_value=(True, {"status": "success", "orderid": "SBX"}, 200)),
    )
    monkeypatch.setattr(
        place_smart_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_smartorder_api=MagicMock()),
    )

    # ---- sandbox shape (no strategy_mode row → default deny) ----
    reject_result = place_smart_order_service.place_smart_order_with_auth(
        _smart_payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_smart_payload(),
    )

    # ---- live shape (live row + analyze off) ----
    fresh_mode_db._set_mode_unchecked("ut", "live", updated_by="op")
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
