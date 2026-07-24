"""Tests for order-management routing wired into ``modify_gtt_order_with_auth``.

Issue #440 — GTT management follows the platform analyze overlay
(``resolve_effective_mode``): Analyze ON → 501 (sandbox GTT not implemented),
Analyze OFF → broker. No strategy row or legacy daily_intent participates.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock


def _payload():
    return {
        "apikey": "test-api-key",
        "trigger_id": "TRG-1",
        "symbol": "INFY",
        "exchange": "NSE",
    }


def _patch_analyze(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)


def test_modify_gtt_routes_to_broker_when_analyze_off(monkeypatch):
    from services import modify_gtt_order_service

    _patch_analyze(monkeypatch)

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


def test_modify_gtt_returns_501_when_analyze_on(monkeypatch):
    """Analyze ON → sandbox GTT is not implemented, so 501 and no broker call."""
    from services import modify_gtt_order_service

    _patch_analyze(monkeypatch, analyze=True)

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
    broker_mod.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_modify_gtt_reject_response_shape_matches_existing_convention(monkeypatch):
    from services import modify_gtt_order_service

    # ---- 501 shape (analyze ON) ----
    _patch_analyze(monkeypatch, analyze=True)
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

    # ---- broker shape (analyze OFF) ----
    _patch_analyze(monkeypatch)
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
