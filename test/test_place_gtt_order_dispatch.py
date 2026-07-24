"""Tests for the per-strategy dispatch wired into ``place_gtt_order_with_auth``.

Issue #440 — UI-driven routing: GTT placement creates exposure, so it goes
through ``resolve_order_mode(order_data['strategy'])``. LIVE (live row +
Analyze off) → broker; every SANDBOX resolution surfaces 501 (GTT is not
implemented in the sandbox book).
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


def _patch_analyze(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)


def test_place_gtt_routes_to_broker_when_strategy_live(fresh_mode_db, monkeypatch):
    """strategy_mode row='live' + analyze off → broker.place_gtt_order fires."""
    from services import place_gtt_order_service

    fresh_mode_db._set_mode_unchecked("ut", "live", updated_by="op")
    _patch_analyze(monkeypatch)

    broker_place = MagicMock(return_value=(SimpleNamespace(status=200), {"status": "ok"}, "TRG-1"))
    monkeypatch.setattr(
        place_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=broker_place),
    )

    success, _, status = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is True
    assert status == 200
    broker_place.assert_called_once()


def test_place_gtt_returns_501_when_strategy_row_sandbox(fresh_mode_db, monkeypatch):
    """Sandbox GTT not implemented — a sandbox-flagged strategy gets 501,
    never a broker call, even with analyze off."""
    from services import place_gtt_order_service

    fresh_mode_db._set_mode_unchecked("ut", "sandbox", updated_by="op")
    _patch_analyze(monkeypatch)

    broker_place = MagicMock()
    monkeypatch.setattr(
        place_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=broker_place),
    )

    success, response, status = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is False
    assert status == 501
    assert response["mode"] == "analyze"
    broker_place.assert_not_called()


def test_place_gtt_returns_501_when_live_but_analyze_on(fresh_mode_db, monkeypatch):
    """Analyze mode is the platform kill switch: a live row cannot beat it."""
    from services import place_gtt_order_service

    fresh_mode_db._set_mode_unchecked("ut", "live", updated_by="op")
    _patch_analyze(monkeypatch, analyze=True)

    broker_place = MagicMock()
    monkeypatch.setattr(
        place_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=broker_place),
    )

    success, response, status = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is False
    assert status == 501
    broker_place.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_place_gtt_returns_501_when_no_row_default_denies(fresh_mode_db, monkeypatch):
    """Default deny: no strategy_mode row → SANDBOX; GTT surfaces 501."""
    from services import place_gtt_order_service

    _patch_analyze(monkeypatch)

    broker_place = MagicMock()
    monkeypatch.setattr(
        place_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=broker_place),
    )

    success, response, status = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is False
    assert status == 501
    assert response["mode"] == "analyze"
    broker_place.assert_not_called()


def test_place_gtt_retired_global_row_has_no_effect(fresh_mode_db, monkeypatch):
    """A manually re-created __global__ live row must not route anything live."""
    from services import place_gtt_order_service

    fresh_mode_db._set_mode_unchecked("__global__", "live", updated_by="op")
    _patch_analyze(monkeypatch)

    broker_place = MagicMock()
    monkeypatch.setattr(
        place_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=broker_place),
    )

    success, response, status = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is False
    assert status == 501
    assert response["mode"] == "analyze"
    broker_place.assert_not_called()


def test_place_gtt_reject_response_shape_matches_existing_convention(fresh_mode_db, monkeypatch):
    from services import place_gtt_order_service

    _patch_analyze(monkeypatch)
    monkeypatch.setattr(
        place_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=MagicMock()),
    )

    # ---- 501 shape (no strategy_mode row → default deny) ----
    reject_result = place_gtt_order_service.place_gtt_order_with_auth(
        _payload(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    # ---- live shape (live row + analyze off) ----
    fresh_mode_db._set_mode_unchecked("ut", "live", updated_by="op")
    broker_place = MagicMock(return_value=(SimpleNamespace(status=200), {"status": "ok"}, "TRG-1"))
    monkeypatch.setattr(
        place_gtt_order_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(place_gtt_order=broker_place),
    )
    success_result = place_gtt_order_service.place_gtt_order_with_auth(
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
