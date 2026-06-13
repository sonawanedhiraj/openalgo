"""Tests for Stage-0 resolver wired into ``place_order_with_auth``.

Each test rebinds ``database.daily_intent_db`` to a fresh in-memory SQLite so
nothing touches ``db/openalgo.db``. The broker side-call
(``broker.<name>.api.order_api.place_order_api``) and the sandbox side-call
(``services.sandbox_service.sandbox_place_order``) are both monkeypatched so
tests assert routing without any broker network calls or sandbox DB writes.

The critical case is ``test_place_order_routes_to_sandbox_when_live_but_analyze_on``
— that exercises the bug being fixed: declaring ``daily_intent=live`` but
leaving the global ``analyze_mode`` flag on must still resolve to SANDBOX, not
fire the live broker path.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

# Pre-resolve the restx_api / services.place_order_service circular import
# before any test does ``from services import place_order_service``. The
# project-root conftest no longer eagerly loads restx_api (see conftest.py
# for why), so the tests that participate in the cycle now take care of it
# themselves. ``import sandbox`` in conftest already pinned the project-root
# sandbox package, so this import still resolves submodules correctly even
# after pytest has added test/ to sys.path.
import restx_api  # noqa: E402, F401


@pytest.fixture
def fresh_intent_db(monkeypatch):
    """Point daily_intent_db at a fresh in-memory SQLite for one test."""
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


def _order_payload():
    """Minimal validated order_data passed to place_order_with_auth."""
    return {
        "apikey": "test-api-key",
        "strategy": "unit_test",
        "symbol": "INFY",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": 1,
        "pricetype": "MARKET",
        "product": "MIS",
    }


# ---------------------------------------------------------------------------
# LIVE path
# ---------------------------------------------------------------------------


def test_place_order_routes_to_broker_when_live(fresh_intent_db, monkeypatch):
    """daily_intent='live' + analyze_mode=False → broker.place_order_api fires."""
    from services import place_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    broker_place_order = MagicMock(
        return_value=(SimpleNamespace(status=200), {"status": "ok"}, "OID-LIVE-1")
    )
    fake_broker_module = SimpleNamespace(place_order_api=broker_place_order)
    monkeypatch.setattr(place_order_service, "import_broker_module", lambda _b: fake_broker_module)

    sandbox_called = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_called)

    payload = _order_payload()
    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True
    assert response == {"status": "success", "orderid": "OID-LIVE-1"}
    assert status == 200
    broker_place_order.assert_called_once()
    sandbox_called.assert_not_called()


# ---------------------------------------------------------------------------
# SANDBOX path (explicit intent)
# ---------------------------------------------------------------------------


def test_place_order_routes_to_sandbox_when_sandbox_intent(fresh_intent_db, monkeypatch):
    """daily_intent='sandbox' → sandbox_place_order fires, broker NOT called."""
    from services import place_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    sandbox_mock = MagicMock(
        return_value=(True, {"status": "success", "orderid": "SBX-1", "mode": "analyze"}, 200)
    )
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)

    broker_called = MagicMock()
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_called),
    )

    payload = _order_payload()
    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True
    assert response["status"] == "success"
    assert response["orderid"] == "SBX-1"
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_called.assert_not_called()


# ---------------------------------------------------------------------------
# SANDBOX path (the bug being fixed: live + analyze_on → sandbox)
# ---------------------------------------------------------------------------


def test_place_order_routes_to_sandbox_when_live_but_analyze_on(fresh_intent_db, monkeypatch):
    """THE BUG: daily_intent='live' + analyze_mode=True must conservative-down
    to sandbox, not silently fire on the live broker.
    """
    from services import place_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: True)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    sandbox_mock = MagicMock(
        return_value=(True, {"status": "success", "orderid": "SBX-BUG", "mode": "analyze"}, 200)
    )
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)

    broker_called = MagicMock()
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_called),
    )

    payload = _order_payload()
    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True
    sandbox_mock.assert_called_once()
    broker_called.assert_not_called(), "Live broker fired despite analyze_mode=True!"


# ---------------------------------------------------------------------------
# SKIP path (mode-only: 'skip' is retired — legacy intent collapses to SANDBOX)
# ---------------------------------------------------------------------------


def test_place_order_routes_to_sandbox_when_skip_legacy_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2, 2026-06-12): a legacy daily_intent='skip' no longer rejects
    — it collapses to SANDBOX. External callers are never refused for config."""
    from services import place_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    sandbox_mock = MagicMock(
        return_value=(True, {"status": "success", "orderid": "SBX-SKIP", "mode": "analyze"}, 200)
    )
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    broker_mock = MagicMock()
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_mock),
    )

    payload = _order_payload()
    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True
    assert response["status"] == "success"
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_mock.assert_not_called()


# ---------------------------------------------------------------------------
# No-config path (mode-only: no row → SANDBOX default, never a DISABLED reject)
# ---------------------------------------------------------------------------


def test_place_order_routes_to_sandbox_when_no_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2): no daily_intent row → SANDBOX default (was DISABLED reject)."""
    from services import place_order_service

    # NO set_daily_intent call — table is empty.
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    sandbox_mock = MagicMock(
        return_value=(True, {"status": "success", "orderid": "SBX-DFLT", "mode": "analyze"}, 200)
    )
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    broker_mock = MagicMock()
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_mock),
    )

    payload = _order_payload()
    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Response shape: sandbox and live returns share the 3-tuple convention so
# callers don't need to special-case either path.
# ---------------------------------------------------------------------------


def test_place_order_reject_response_shape_matches_existing_convention(
    fresh_intent_db, monkeypatch
):
    """Both sandbox and live returns are (bool, dict, int) — same outer shape."""
    from services import place_order_service
    from services.mode_service import set_daily_intent

    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)

    # ---- shape for the SANDBOX path (skip collapses to sandbox in mode-only) ----
    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    sandbox_mock = MagicMock(
        return_value=(True, {"status": "success", "orderid": "SBX", "mode": "analyze"}, 200)
    )
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=MagicMock()),
    )

    payload = _order_payload()
    sandbox_result = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    # ---- shape for a successful LIVE order, same call shape ----
    # Re-bind: flip intent to live, ensure broker fires
    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    broker_place_order = MagicMock(
        return_value=(SimpleNamespace(status=200), {"status": "ok"}, "OID-OK")
    )
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place_order),
    )
    success_result = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    # Same outer shape: tuple of length 3, (bool, dict, int).
    for result in (sandbox_result, success_result):
        assert isinstance(result, tuple) and len(result) == 3
        assert isinstance(result[0], bool)
        assert isinstance(result[1], dict)
        assert isinstance(result[2], int)
