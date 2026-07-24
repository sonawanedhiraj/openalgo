"""Tests for order-management routing wired into ``cancel_order_with_auth``.

Issue #440 — order management is NOT strategy-gated: Analyze ON → sandbox
book only; Analyze OFF → route by where the order actually lives
(``sandbox_order_exists``) — a sandbox orderid cancels on the sandbox book,
anything else falls through to the broker. The broker side-call and sandbox
side-call are both monkeypatched so the tests assert routing without any
broker network calls or sandbox DB writes.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock


def _original_data():
    return {"orderid": "TEST-OID-1", "apikey": "test-api-key"}


def _patch_analyze(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)


def test_cancel_routes_to_broker_when_analyze_off(monkeypatch):
    """Analyze off + orderid not in the sandbox book → broker.cancel_order."""
    from services import cancel_order_service

    _patch_analyze(monkeypatch)
    monkeypatch.setattr("services.sandbox_service.sandbox_order_exists", lambda *a, **k: False)

    broker_cancel = MagicMock(return_value=({"status": "success"}, 200))
    fake_module = SimpleNamespace(cancel_order=broker_cancel)
    monkeypatch.setattr(cancel_order_service, "import_broker_module", lambda _b: fake_module)

    sandbox_called = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_order", sandbox_called)

    success, response, status = cancel_order_service.cancel_order_with_auth(
        "TEST-OID-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original_data(),
    )

    assert success is True
    assert status == 200
    broker_cancel.assert_called_once()
    sandbox_called.assert_not_called()


def test_cancel_routes_to_sandbox_when_analyze_on(monkeypatch):
    """Analyze ON is the platform overlay — cancel never touches the broker."""
    from services import cancel_order_service

    _patch_analyze(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "mode": "analyze"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_order", sandbox_mock)

    broker_called = MagicMock()
    monkeypatch.setattr(
        cancel_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_order=broker_called),
    )

    success, response, status = cancel_order_service.cancel_order_with_auth(
        "TEST-OID-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original_data(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_called.assert_not_called(), "Live broker fired despite analyze_mode=True!"


def test_cancel_routes_to_sandbox_book_order_when_analyze_off(monkeypatch):
    """Analyze off + orderid found in the sandbox book → sandbox cancel, not
    broker (mixed-mode operation, issue #440)."""
    from services import cancel_order_service

    _patch_analyze(monkeypatch)
    monkeypatch.setattr("services.sandbox_service.sandbox_order_exists", lambda *a, **k: True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "mode": "analyze"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_cancel_order", sandbox_mock)

    broker_called = MagicMock()
    monkeypatch.setattr(
        cancel_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_order=broker_called),
    )

    success, response, status = cancel_order_service.cancel_order_with_auth(
        "TEST-OID-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original_data(),
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_called.assert_not_called()


def test_cancel_reject_response_shape_matches_existing_convention(monkeypatch):
    """Both sandbox and live returns are (bool, dict, int) — same outer shape."""
    from services import cancel_order_service

    # ---- sandbox shape (analyze ON) ----
    _patch_analyze(monkeypatch, analyze=True)
    monkeypatch.setattr(
        "services.sandbox_service.sandbox_cancel_order",
        MagicMock(return_value=(True, {"status": "success", "mode": "analyze"}, 200)),
    )
    monkeypatch.setattr(
        cancel_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_order=MagicMock()),
    )

    sandbox_result = cancel_order_service.cancel_order_with_auth(
        "TEST-OID-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original_data(),
    )

    # ---- broker shape (analyze OFF, order not in sandbox book) ----
    _patch_analyze(monkeypatch)
    monkeypatch.setattr("services.sandbox_service.sandbox_order_exists", lambda *a, **k: False)
    broker_cancel = MagicMock(return_value=({"status": "success"}, 200))
    monkeypatch.setattr(
        cancel_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(cancel_order=broker_cancel),
    )
    success_result = cancel_order_service.cancel_order_with_auth(
        "TEST-OID-1",
        auth_token="dummy",
        broker="zerodha",
        original_data=_original_data(),
    )

    for result in (sandbox_result, success_result):
        assert isinstance(result, tuple) and len(result) == 3
        assert isinstance(result[0], bool)
        assert isinstance(result[1], dict)
        assert isinstance(result[2], int)
