"""Tests for order-management routing wired into ``cancel_all_orders_with_auth``.

Issue #440 — cancel-all is protective, so under mixed-mode operation it sweeps
BOTH books: the sandbox book always (when an apikey is available), and the
broker book too when Analyze is off. With Analyze ON the sandbox sweep result
is returned and the broker is never touched; with Analyze OFF the sandbox
canceled/failed lists are merged into the broker response.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock


def _payload():
    return {"apikey": "test-api-key"}


def _patch_analyze(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)


def _sandbox_sweep_result():
    return (
        True,
        {
            "status": "success",
            "canceled_count": 1,
            "failed_count": 1,
            "canceled_orders": ["SBX-OID-1"],
            "failed_cancellations": ["SBX-OID-2"],
        },
        200,
    )


def test_cancel_all_sweeps_both_books_when_analyze_off(monkeypatch):
    """Analyze off → sandbox sweep runs first, then the broker sweep; the
    response folds the sandbox canceled/failed lists into the broker's."""
    from services import cancel_all_order_service

    _patch_analyze(monkeypatch)

    broker_api = MagicMock(return_value=(["OID1"], ["OID2"]))
    monkeypatch.setattr(
        cancel_all_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_all_orders_api=broker_api),
    )
    sandbox_mock = MagicMock(return_value=_sandbox_sweep_result())
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_all_orders", sandbox_mock)

    success, response, status = cancel_all_order_service.cancel_all_orders_with_auth(
        {"apikey": "test"},  # pragma: allowlist secret
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is True
    assert status == 200
    broker_api.assert_called_once()
    sandbox_mock.assert_called_once()
    # Broker results come first, sandbox-book results are merged in after.
    assert response["canceled_orders"] == ["OID1", "SBX-OID-1"]
    assert response["failed_cancellations"] == ["OID2", "SBX-OID-2"]


def test_cancel_all_sandbox_only_when_analyze_on(monkeypatch):
    """Analyze ON → the sandbox sweep result is returned; broker never touched."""
    from services import cancel_all_order_service

    _patch_analyze(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=_sandbox_sweep_result())
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_all_orders", sandbox_mock)
    broker_api = MagicMock()
    monkeypatch.setattr(
        cancel_all_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_all_orders_api=broker_api),
    )

    success, response, status = cancel_all_order_service.cancel_all_orders_with_auth(
        {"apikey": "test"},  # pragma: allowlist secret
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_api.assert_not_called(), "Live broker fired despite analyze_mode=True!"
    assert response["canceled_orders"] == ["SBX-OID-1"]


def test_cancel_all_reject_response_shape_matches_existing_convention(monkeypatch):
    """Both sandbox-only and mixed-mode returns are (bool, dict, int)."""
    from services import cancel_all_order_service

    monkeypatch.setattr(
        "services.sandbox_service.sandbox_cancel_all_orders",
        MagicMock(return_value=_sandbox_sweep_result()),
    )
    broker_api = MagicMock(return_value=(["OID"], []))
    monkeypatch.setattr(
        cancel_all_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_all_orders_api=broker_api),
    )

    # ---- sandbox-only shape (analyze ON) ----
    _patch_analyze(monkeypatch, analyze=True)
    sandbox_result = cancel_all_order_service.cancel_all_orders_with_auth(
        {"apikey": "test"},  # pragma: allowlist secret
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    # ---- mixed-mode shape (analyze OFF → both books) ----
    _patch_analyze(monkeypatch)
    merged_result = cancel_all_order_service.cancel_all_orders_with_auth(
        {"apikey": "test"},  # pragma: allowlist secret
        auth_token="dummy",
        broker="zerodha",
        original_data=_payload(),
    )

    for r in (sandbox_result, merged_result):
        assert isinstance(r, tuple) and len(r) == 3
        assert isinstance(r[0], bool)
        assert isinstance(r[1], dict)
        assert isinstance(r[2], int)
