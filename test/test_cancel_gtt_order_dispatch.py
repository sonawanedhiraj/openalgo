"""Tests for order-management routing wired into ``cancel_gtt_order_with_auth``.

Issue #440 — GTT management follows the platform analyze overlay
(``resolve_effective_mode``): Analyze ON → 501 (sandbox GTT not implemented),
Analyze OFF → broker. No strategy row or legacy daily_intent participates.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock


def _original():
    return {"trigger_id": "TRG-1", "apikey": "test-api-key"}


def _patch_analyze(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)


def test_cancel_gtt_routes_to_broker_when_analyze_off(monkeypatch):
    from services import cancel_gtt_order_service

    _patch_analyze(monkeypatch)

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


def test_cancel_gtt_returns_501_when_analyze_on(monkeypatch):
    """Analyze ON → sandbox GTT is not implemented, so 501 and no broker call."""
    from services import cancel_gtt_order_service

    _patch_analyze(monkeypatch, analyze=True)

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
    broker_cancel.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_cancel_gtt_reject_response_shape_matches_existing_convention(monkeypatch):
    from services import cancel_gtt_order_service

    # ---- 501 shape (analyze ON) ----
    _patch_analyze(monkeypatch, analyze=True)
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

    # ---- broker shape (analyze OFF) ----
    _patch_analyze(monkeypatch)
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
