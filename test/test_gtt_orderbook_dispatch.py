"""Tests for the read-path routing wired into ``get_gtt_orderbook_with_auth``.

Issue #440 — reads follow the platform analyze overlay
(``resolve_effective_mode``): Analyze ON → 501 (sandbox GTT read not
implemented), Analyze OFF → broker.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock


def _patch_analyze(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)


def test_gtt_orderbook_reads_from_broker_when_analyze_off(monkeypatch):
    from services import gtt_orderbook_service

    _patch_analyze(monkeypatch)

    broker_get = MagicMock(return_value=({"triggers": []}, 200))
    monkeypatch.setattr(
        gtt_orderbook_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(get_gtt_book=broker_get),
    )

    success, _, status = gtt_orderbook_service.get_gtt_orderbook_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )

    assert success is True
    assert status == 200
    broker_get.assert_called_once()


def test_gtt_orderbook_returns_501_when_analyze_on(monkeypatch):
    """Analyze ON → sandbox GTT read not implemented — 501, no broker call."""
    from services import gtt_orderbook_service

    _patch_analyze(monkeypatch, analyze=True)

    broker_get = MagicMock()
    monkeypatch.setattr(
        gtt_orderbook_service,
        "import_broker_gtt_module",
        lambda _b: SimpleNamespace(get_gtt_book=broker_get),
    )

    success, response, status = gtt_orderbook_service.get_gtt_orderbook_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )

    assert success is False
    assert status == 501
    assert response["mode"] == "analyze"
    broker_get.assert_not_called()
