"""Tests for the per-strategy dispatch wired into ``process_basket_order_with_auth``.

Issue #440 — UI-driven routing: a basket fires on the live broker ONLY when the
Analyze/Live navbar toggle is on Live AND the basket's strategy has a
``strategy_mode`` row set to live. Everything else — sandbox row, no row,
Analyze ON — routes to sandbox (default deny).
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


def _patch_analyze(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)


def _stub_multiquotes(monkeypatch):
    """Avoid touching quotes_service in the sandbox branch."""
    from services import quotes_service as qs

    monkeypatch.setattr(qs, "get_multiquotes", lambda **kw: (False, {"message": "skipped"}, 500))


def test_basket_routes_to_broker_when_strategy_live(fresh_mode_db, monkeypatch):
    """strategy_mode row='live' + analyze off → broker.place_order_api fires."""
    from services import basket_order_service

    fresh_mode_db._set_mode_unchecked("ut", "live", updated_by="op")
    _patch_analyze(monkeypatch)

    broker_place = MagicMock(return_value=(SimpleNamespace(status=200), {"status": "ok"}, "OID-1"))
    monkeypatch.setattr(
        basket_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place),
    )
    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)

    success, _, status = basket_order_service.process_basket_order_with_auth(
        _basket(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_basket(),
    )

    assert success is True
    assert status == 200
    broker_place.assert_called()
    sandbox_mock.assert_not_called()


def test_basket_routes_to_sandbox_when_strategy_row_sandbox(fresh_mode_db, monkeypatch):
    """A sandbox-flagged strategy stays sandbox even with analyze off AND other
    strategies live — the per-strategy protection at the heart of #440."""
    from services import basket_order_service

    fresh_mode_db._set_mode_unchecked("other_strategy", "live", updated_by="op")
    fresh_mode_db._set_mode_unchecked("ut", "sandbox", updated_by="op")
    _patch_analyze(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX-1"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    broker_place = MagicMock()
    monkeypatch.setattr(
        basket_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place),
    )
    _stub_multiquotes(monkeypatch)

    success, _, status = basket_order_service.process_basket_order_with_auth(
        _basket(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_basket(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called()
    broker_place.assert_not_called()


def test_basket_routes_to_sandbox_when_live_but_analyze_on(fresh_mode_db, monkeypatch):
    """Analyze mode is the platform kill switch: a live row cannot beat it."""
    from services import basket_order_service

    fresh_mode_db._set_mode_unchecked("ut", "live", updated_by="op")
    _patch_analyze(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX-1"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    broker_place = MagicMock()
    monkeypatch.setattr(
        basket_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place),
    )
    _stub_multiquotes(monkeypatch)

    basket_order_service.process_basket_order_with_auth(
        _basket(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_basket(),
    )

    sandbox_mock.assert_called()
    broker_place.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_basket_routes_to_sandbox_when_no_row_default_denies(fresh_mode_db, monkeypatch):
    """Default deny: no strategy_mode row for the label → sandbox even with
    analyze off (issue #440)."""
    from services import basket_order_service

    _patch_analyze(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX-1"}, 200))
    broker_place = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    monkeypatch.setattr(
        basket_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place),
    )
    _stub_multiquotes(monkeypatch)

    success, response, status = basket_order_service.process_basket_order_with_auth(
        _basket(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_basket(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called()
    broker_place.assert_not_called()


def test_basket_retired_global_row_has_no_effect(fresh_mode_db, monkeypatch):
    """A manually re-created __global__ live row must not route anything live."""
    from services import basket_order_service

    fresh_mode_db._set_mode_unchecked("__global__", "live", updated_by="op")
    _patch_analyze(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX-1"}, 200))
    broker_place = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    monkeypatch.setattr(
        basket_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place),
    )
    _stub_multiquotes(monkeypatch)

    success, response, status = basket_order_service.process_basket_order_with_auth(
        _basket(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_basket(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called()
    broker_place.assert_not_called()


def _multi_basket(symbols):
    return {
        "apikey": "test-api-key",
        "strategy": "ut",
        "orders": [
            {
                "symbol": s,
                "exchange": "NSE",
                "action": "BUY",
                "quantity": 1,
                "pricetype": "MARKET",
                "product": "MIS",
            }
            for s in symbols
        ],
    }


def test_basket_all_orders_failed_reports_error(fresh_mode_db, monkeypatch):
    """Silent-drop P0-1: a basket where the broker rejects EVERY order must
    NOT report top-level status="success"."""
    from services import basket_order_service

    fresh_mode_db._set_mode_unchecked("ut", "live", updated_by="op")
    _patch_analyze(monkeypatch)

    def _reject(order_data, auth_token):
        return SimpleNamespace(status=400), {"message": "rejected"}, None

    monkeypatch.setattr(
        basket_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=_reject),
    )

    basket = _multi_basket(["INFY", "SBIN"])
    success, response, status = basket_order_service.process_basket_order_with_auth(
        basket,
        auth_token="dummy",
        broker="zerodha",
        original_data=basket,
    )

    assert response["status"] == "error"
    assert response["successful_orders"] == 0
    assert response["failed_orders"] == 2
    assert response["total_orders"] == 2


def test_basket_partial_fill_reports_partial(fresh_mode_db, monkeypatch):
    """Silent-drop P0-1: a basket where some orders fill and some are rejected
    must report top-level status="partial"."""
    from services import basket_order_service

    fresh_mode_db._set_mode_unchecked("ut", "live", updated_by="op")
    _patch_analyze(monkeypatch)

    def _mixed(order_data, auth_token):
        if order_data["symbol"] == "INFY":
            return SimpleNamespace(status=200), {"status": "ok"}, "OID-1"
        return SimpleNamespace(status=400), {"message": "rejected"}, None

    monkeypatch.setattr(
        basket_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=_mixed),
    )

    basket = _multi_basket(["INFY", "SBIN", "TCS"])
    success, response, status = basket_order_service.process_basket_order_with_auth(
        basket,
        auth_token="dummy",
        broker="zerodha",
        original_data=basket,
    )

    assert response["status"] == "partial"
    assert response["successful_orders"] == 1
    assert response["failed_orders"] == 2


def test_basket_reject_response_shape_matches_existing_convention(fresh_mode_db, monkeypatch):
    """Both sandbox and live returns are (bool, dict, int) — same outer shape."""
    from services import basket_order_service

    _patch_analyze(monkeypatch)
    monkeypatch.setattr(
        "services.sandbox_service.sandbox_place_order",
        MagicMock(return_value=(True, {"status": "success", "orderid": "SBX-1"}, 200)),
    )
    monkeypatch.setattr(
        basket_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=MagicMock()),
    )
    _stub_multiquotes(monkeypatch)

    # ---- sandbox shape (no strategy_mode row → default deny) ----
    reject_result = basket_order_service.process_basket_order_with_auth(
        _basket(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_basket(),
    )

    # ---- live shape (live row + analyze off) ----
    fresh_mode_db._set_mode_unchecked("ut", "live", updated_by="op")
    broker_place = MagicMock(return_value=(SimpleNamespace(status=200), {"status": "ok"}, "OID-1"))
    monkeypatch.setattr(
        basket_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place),
    )
    success_result = basket_order_service.process_basket_order_with_auth(
        _basket(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_basket(),
    )

    for r in (reject_result, success_result):
        assert isinstance(r, tuple) and len(r) == 3
        assert isinstance(r[0], bool)
        assert isinstance(r[1], dict)
        assert isinstance(r[2], int)
