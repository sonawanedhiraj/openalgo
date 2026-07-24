"""Tests for the read-path routing wired into the read services.

Issue #440 — reads follow the platform analyze overlay only
(``resolve_effective_mode``): Analyze ON → sandbox source, Analyze OFF →
broker source. No strategy row, legacy daily_intent, or hidden global flag
participates. ``orderstatus`` additionally routes a sandbox-book orderid to
the sandbox source even with Analyze OFF (mixed-mode operation).

Tested services:
- orderbook
- positionbook
- tradebook
- holdings
- openposition
- funds
- orderstatus
"""

from types import SimpleNamespace
from unittest.mock import MagicMock


def _patch_analyze(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)


# ---------------------------------------------------------------------------
# orderbook
# ---------------------------------------------------------------------------


def _patch_orderbook(monkeypatch, broker_funcs):
    from services import orderbook_service

    monkeypatch.setattr(orderbook_service, "import_broker_module", lambda _b: broker_funcs)


def test_orderbook_reads_from_broker_when_analyze_off(monkeypatch):
    from services import orderbook_service

    _patch_analyze(monkeypatch)

    broker_get = MagicMock(return_value=[])
    _patch_orderbook(
        monkeypatch,
        {
            "get_order_book": broker_get,
            "map_order_data": lambda order_data: [],
            "calculate_order_statistics": lambda x: {},
            "transform_order_data": lambda x: [],
        },
    )
    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_get_orderbook", sandbox_mock)

    success, _, status = orderbook_service.get_orderbook_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )

    assert success is True
    assert status == 200
    broker_get.assert_called_once()
    sandbox_mock.assert_not_called()


def test_orderbook_reads_from_sandbox_when_analyze_on(monkeypatch):
    from services import orderbook_service

    _patch_analyze(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "data": []}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_orderbook", sandbox_mock)
    broker_get = MagicMock()
    _patch_orderbook(monkeypatch, {"get_order_book": broker_get})

    success, _, status = orderbook_service.get_orderbook_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_get.assert_not_called()


# ---------------------------------------------------------------------------
# positionbook
# ---------------------------------------------------------------------------


def test_positionbook_reads_from_sandbox_when_analyze_on(monkeypatch):
    from services import positionbook_service

    _patch_analyze(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "data": []}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_positions", sandbox_mock)
    broker_get = MagicMock()
    monkeypatch.setattr(
        positionbook_service,
        "import_broker_module",
        lambda _b: {"get_positions": broker_get},
    )

    success, _, _ = positionbook_service.get_positionbook_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )
    assert success is True
    sandbox_mock.assert_called_once()
    broker_get.assert_not_called()


def test_positionbook_reads_from_broker_when_analyze_off(monkeypatch):
    from services import positionbook_service

    _patch_analyze(monkeypatch)

    broker_get = MagicMock(return_value=[])
    monkeypatch.setattr(
        positionbook_service,
        "import_broker_module",
        lambda _b: {
            "get_positions": broker_get,
            "map_position_data": lambda x: [],
            "transform_positions_data": lambda x: [],
        },
    )
    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_get_positions", sandbox_mock)

    success, _, status = positionbook_service.get_positionbook_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )
    assert success is True
    assert status == 200
    broker_get.assert_called_once()
    sandbox_mock.assert_not_called()


# ---------------------------------------------------------------------------
# tradebook
# ---------------------------------------------------------------------------


def test_tradebook_reads_from_sandbox_when_analyze_on(monkeypatch):
    from services import tradebook_service

    _patch_analyze(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "data": []}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_tradebook", sandbox_mock)
    broker_get = MagicMock()
    monkeypatch.setattr(
        tradebook_service,
        "import_broker_module",
        lambda _b: {"get_trade_book": broker_get},
    )

    tradebook_service.get_tradebook_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )
    sandbox_mock.assert_called_once()
    broker_get.assert_not_called()


def test_tradebook_reads_from_broker_when_analyze_off(monkeypatch):
    from services import tradebook_service

    _patch_analyze(monkeypatch)

    broker_get = MagicMock(return_value=[])
    monkeypatch.setattr(
        tradebook_service,
        "import_broker_module",
        lambda _b: {
            "get_trade_book": broker_get,
            "map_trade_data": lambda trade_data: [],
            "transform_tradebook_data": lambda x: [],
        },
    )
    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_get_tradebook", sandbox_mock)

    success, _, status = tradebook_service.get_tradebook_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )
    assert success is True
    assert status == 200
    broker_get.assert_called_once()
    sandbox_mock.assert_not_called()


# ---------------------------------------------------------------------------
# holdings
# ---------------------------------------------------------------------------


def test_holdings_reads_from_sandbox_when_analyze_on(monkeypatch):
    from services import holdings_service

    _patch_analyze(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "data": []}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_holdings", sandbox_mock)
    broker_get = MagicMock()
    monkeypatch.setattr(
        holdings_service,
        "import_broker_module",
        lambda _b: {"get_holdings": broker_get},
    )

    holdings_service.get_holdings_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )
    sandbox_mock.assert_called_once()
    broker_get.assert_not_called()


def test_holdings_reads_from_broker_when_analyze_off(monkeypatch):
    """Routing only: the broker source is chosen, sandbox never consulted."""
    from services import holdings_service

    _patch_analyze(monkeypatch)

    broker_get = MagicMock(return_value=[])
    monkeypatch.setattr(
        holdings_service,
        "import_broker_module",
        lambda _b: {
            "get_holdings": broker_get,
            "map_portfolio_data": lambda x: [],
            "calculate_portfolio_statistics": lambda x: {},
            "transform_holdings_data": lambda x: [],
        },
    )
    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_get_holdings", sandbox_mock)

    holdings_service.get_holdings_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )
    broker_get.assert_called_once()
    sandbox_mock.assert_not_called()


# ---------------------------------------------------------------------------
# funds
# ---------------------------------------------------------------------------


def test_funds_reads_from_sandbox_when_analyze_on(monkeypatch):
    from services import funds_service

    _patch_analyze(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "data": {}}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_funds", sandbox_mock)
    broker_get = MagicMock()
    monkeypatch.setattr(
        funds_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(get_margin_data=broker_get),
    )

    funds_service.get_funds_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )
    sandbox_mock.assert_called_once()
    broker_get.assert_not_called()


def test_funds_reads_from_broker_when_analyze_off(monkeypatch):
    from services import funds_service

    _patch_analyze(monkeypatch)

    broker_get = MagicMock(return_value={})
    monkeypatch.setattr(
        funds_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(get_margin_data=broker_get),
    )
    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_get_funds", sandbox_mock)

    success, _, status = funds_service.get_funds_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )
    assert success is True
    assert status == 200
    broker_get.assert_called_once()
    sandbox_mock.assert_not_called()


# ---------------------------------------------------------------------------
# openposition
# ---------------------------------------------------------------------------


def test_openposition_reads_from_sandbox_when_analyze_on(monkeypatch):
    from services import openposition_service

    _patch_analyze(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "data": []}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_positions", sandbox_mock)
    # socketio is not initialised in tests — stub start_background_task
    monkeypatch.setattr(
        "services.openposition_service.socketio.start_background_task",
        lambda *a, **kw: None,
    )

    success, _, status = openposition_service.get_open_position_with_auth(
        {"apikey": "test", "symbol": "INFY", "exchange": "NSE", "product": "MIS"},
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test", "symbol": "INFY", "exchange": "NSE", "product": "MIS"},
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()


def test_openposition_reads_from_positionbook_when_analyze_off(monkeypatch):
    """Analyze OFF → the live positionbook fall-through, not the sandbox book."""
    from services import openposition_service

    _patch_analyze(monkeypatch)

    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_get_positions", sandbox_mock)

    fake_pb = MagicMock(return_value=(True, {"data": []}, 200))
    monkeypatch.setattr("services.positionbook_service.get_positionbook", fake_pb)

    success, response, status = openposition_service.get_open_position_with_auth(
        {"apikey": "test", "symbol": "INFY", "exchange": "NSE", "product": "MIS"},
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test", "symbol": "INFY", "exchange": "NSE", "product": "MIS"},
    )

    assert success is True
    assert status == 200
    assert response["quantity"] == 0
    fake_pb.assert_called_once()
    sandbox_mock.assert_not_called()


# ---------------------------------------------------------------------------
# orderstatus
# ---------------------------------------------------------------------------


def test_orderstatus_reads_from_sandbox_when_analyze_on(monkeypatch):
    from services import orderstatus_service

    _patch_analyze(monkeypatch, analyze=True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_order_status", sandbox_mock)

    success, _, _ = orderstatus_service.get_order_status_with_auth(
        {"orderid": "OID-1"},
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test", "orderid": "OID-1"},
    )

    assert success is True
    sandbox_mock.assert_called_once()


def test_orderstatus_routes_sandbox_book_order_when_analyze_off(monkeypatch):
    """Analyze OFF + orderid in the sandbox book → sandbox status, not the
    broker orderbook fall-through (mixed-mode operation, issue #440)."""
    from services import orderstatus_service

    _patch_analyze(monkeypatch)
    monkeypatch.setattr("services.sandbox_service.sandbox_order_exists", lambda *a, **k: True)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success"}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_order_status", sandbox_mock)

    fake_ob = MagicMock(return_value=(False, {"message": "stub"}, 500))
    monkeypatch.setattr("services.orderbook_service.get_orderbook", fake_ob)

    orderstatus_service.get_order_status_with_auth(
        {"orderid": "OID-1"},
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test", "orderid": "OID-1"},
    )

    sandbox_mock.assert_called_once()
    fake_ob.assert_not_called()


def test_orderstatus_reads_from_orderbook_when_analyze_off(monkeypatch):
    """Analyze OFF + orderid not in the sandbox book → broker orderbook path."""
    from services import orderstatus_service

    _patch_analyze(monkeypatch)
    monkeypatch.setattr("services.sandbox_service.sandbox_order_exists", lambda *a, **k: False)

    sandbox_mock = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_get_order_status", sandbox_mock)

    fake_ob = MagicMock(return_value=(False, {"message": "stub"}, 500))
    monkeypatch.setattr("services.orderbook_service.get_orderbook", fake_ob)

    orderstatus_service.get_order_status_with_auth(
        {"orderid": "OID-1"},
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test", "orderid": "OID-1"},
    )

    fake_ob.assert_called_once()
    sandbox_mock.assert_not_called()
