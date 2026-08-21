"""Tests for order-management routing wired into ``modify_order_with_auth``.

Issue #440 — order management is NOT strategy-gated: Analyze ON → sandbox
book only; Analyze OFF → route by where the order actually lives
(``sandbox_order_exists``) — a sandbox orderid modifies on the sandbox book,
anything else falls through to the broker.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock


def _order_data():
    return {
        "apikey": "test-api-key",
        "orderid": "OID-1",
        "symbol": "INFY",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": 1,
        "pricetype": "LIMIT",
        "product": "MIS",
        "price": 100.0,
    }


def _patch_analyze(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)


def test_modify_routes_to_broker_when_analyze_off(monkeypatch):
    """Analyze off + orderid not in the sandbox book → broker.modify_order."""
    from services import modify_order_service

    _patch_analyze(monkeypatch)
    monkeypatch.setattr("services.sandbox_service.sandbox_order_exists", lambda *a, **k: False)

    broker_mod = MagicMock(return_value=({"status": "success"}, 200))
    monkeypatch.setattr(
        modify_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(modify_order=broker_mod),
    )
    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_modify_order", sandbox_mock)

    success, _, status = modify_order_service.modify_order_with_auth(
        _order_data(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_order_data(),
    )

    assert success is True
    assert status == 200
    broker_mod.assert_called_once()
    sandbox_mock.assert_not_called()


def test_modify_routes_to_sandbox_when_analyze_on(monkeypatch):
    """Analyze ON is the platform overlay — modify never touches the broker."""
    from services import modify_order_service

    _patch_analyze(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX-1"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_modify_order", sandbox_mock)
    broker_mod = MagicMock()
    monkeypatch.setattr(
        modify_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(modify_order=broker_mod),
    )

    modify_order_service.modify_order_with_auth(
        _order_data(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_order_data(),
    )

    sandbox_mock.assert_called_once()
    broker_mod.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_modify_routes_to_sandbox_book_order_when_analyze_off(monkeypatch):
    """Analyze off + orderid found in the sandbox book → sandbox modify, not
    broker (mixed-mode operation, issue #440)."""
    from services import modify_order_service

    _patch_analyze(monkeypatch)
    monkeypatch.setattr("services.sandbox_service.sandbox_order_exists", lambda *a, **k: True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "orderid": "SBX-1"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_modify_order", sandbox_mock)
    broker_mod = MagicMock()
    monkeypatch.setattr(
        modify_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(modify_order=broker_mod),
    )

    success, response, status = modify_order_service.modify_order_with_auth(
        _order_data(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_order_data(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_mod.assert_not_called()


def test_modify_reject_response_shape_matches_existing_convention(monkeypatch):
    """Both sandbox and live returns are (bool, dict, int) — same outer shape."""
    from services import modify_order_service

    # ---- sandbox shape (analyze ON) ----
    _patch_analyze(monkeypatch, analyze=True)
    monkeypatch.setattr(
        "services.sandbox_service.sandbox_modify_order",
        MagicMock(return_value=(True, {"status": "success", "orderid": "SBX-1"}, 200)),
    )
    monkeypatch.setattr(
        modify_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(modify_order=MagicMock()),
    )
    reject_result = modify_order_service.modify_order_with_auth(
        _order_data(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_order_data(),
    )

    # ---- broker shape (analyze OFF, order not in sandbox book) ----
    _patch_analyze(monkeypatch)
    monkeypatch.setattr("services.sandbox_service.sandbox_order_exists", lambda *a, **k: False)
    broker_mod = MagicMock(return_value=({"status": "success"}, 200))
    monkeypatch.setattr(
        modify_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(modify_order=broker_mod),
    )
    success_result = modify_order_service.modify_order_with_auth(
        _order_data(),
        auth_token="dummy",
        broker="zerodha",
        original_data=_order_data(),
    )

    for r in (reject_result, success_result):
        assert isinstance(r, tuple) and len(r) == 3
        assert isinstance(r[0], bool)
        assert isinstance(r[1], dict)
        assert isinstance(r[2], int)
