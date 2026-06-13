"""Tests for Stage-0 resolver wired into the read services.

Mode-only (B2, 2026-06-12): the resolver only ever returns LIVE or SANDBOX —
there is no SKIP / DISABLED axis. A legacy intent of 'skip' collapses to SANDBOX
and an unconfigured day defaults to SANDBOX, so reads route to the sandbox source
in those cases (consistent with "default sandbox globally": if orders default to
the virtual book, the read endpoints must show that same book). Only an explicit
LIVE config (or live + analyze_mode off) reads from the broker.

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

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_intent_db(monkeypatch):
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


def _patch_modes(monkeypatch, analyze=False):
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: analyze)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")


# ---------------------------------------------------------------------------
# orderbook
# ---------------------------------------------------------------------------


def _patch_orderbook(monkeypatch, broker_funcs):
    from services import orderbook_service

    monkeypatch.setattr(orderbook_service, "import_broker_module", lambda _b: broker_funcs)


def test_orderbook_reads_from_broker_when_live(fresh_intent_db, monkeypatch):
    from services import orderbook_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

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


def test_orderbook_reads_from_sandbox_when_sandbox_intent(fresh_intent_db, monkeypatch):
    from services import orderbook_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

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


def test_orderbook_reads_from_sandbox_when_skip_legacy_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2): legacy intent 'skip' collapses to SANDBOX — reads the
    sandbox book, not the broker."""
    from services import orderbook_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

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


def test_orderbook_reads_from_sandbox_when_no_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2): no intent row → SANDBOX default — reads the sandbox book."""
    from services import orderbook_service

    _patch_modes(monkeypatch)

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


def test_positionbook_routes_by_resolver(fresh_intent_db, monkeypatch):
    from services import positionbook_service
    from services.mode_service import set_daily_intent

    _patch_modes(monkeypatch)
    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")

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


def test_positionbook_skip_routes_to_sandbox(fresh_intent_db, monkeypatch):
    """Mode-only (B2): legacy intent 'skip' collapses to SANDBOX."""
    from services import positionbook_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_get = MagicMock()
    monkeypatch.setattr(
        positionbook_service,
        "import_broker_module",
        lambda _b: {"get_positions": broker_get},
    )
    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "data": []}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_positions", sandbox_mock)

    positionbook_service.get_positionbook_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )
    sandbox_mock.assert_called_once()
    broker_get.assert_not_called()


# ---------------------------------------------------------------------------
# tradebook
# ---------------------------------------------------------------------------


def test_tradebook_routes_by_resolver(fresh_intent_db, monkeypatch):
    from services import tradebook_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

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


def test_tradebook_no_intent_routes_to_sandbox(fresh_intent_db, monkeypatch):
    """Mode-only (B2): no intent row → SANDBOX default."""
    from services import tradebook_service

    _patch_modes(monkeypatch)

    broker_get = MagicMock()
    monkeypatch.setattr(
        tradebook_service,
        "import_broker_module",
        lambda _b: {"get_trade_book": broker_get},
    )
    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "data": []}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_tradebook", sandbox_mock)

    tradebook_service.get_tradebook_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )
    sandbox_mock.assert_called_once()
    broker_get.assert_not_called()


# ---------------------------------------------------------------------------
# holdings
# ---------------------------------------------------------------------------


def test_holdings_routes_by_resolver(fresh_intent_db, monkeypatch):
    from services import holdings_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

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


def test_holdings_skip_routes_to_sandbox(fresh_intent_db, monkeypatch):
    """Mode-only (B2): legacy intent 'skip' collapses to SANDBOX."""
    from services import holdings_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    broker_get = MagicMock()
    monkeypatch.setattr(
        holdings_service,
        "import_broker_module",
        lambda _b: {"get_holdings": broker_get},
    )
    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "data": []}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_holdings", sandbox_mock)

    holdings_service.get_holdings_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )
    sandbox_mock.assert_called_once()
    broker_get.assert_not_called()


# ---------------------------------------------------------------------------
# funds
# ---------------------------------------------------------------------------


def test_funds_routes_by_resolver(fresh_intent_db, monkeypatch):
    from services import funds_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

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


def test_funds_no_intent_routes_to_sandbox(fresh_intent_db, monkeypatch):
    """Mode-only (B2): no intent row → SANDBOX default."""
    from services import funds_service

    _patch_modes(monkeypatch)

    broker_get = MagicMock()
    monkeypatch.setattr(
        funds_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(get_margin_data=broker_get),
    )
    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "data": {}}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_funds", sandbox_mock)

    success, _, status = funds_service.get_funds_with_auth(
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test"},
    )
    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_get.assert_not_called()


# ---------------------------------------------------------------------------
# openposition
# ---------------------------------------------------------------------------


def test_openposition_routes_to_sandbox_when_sandbox(fresh_intent_db, monkeypatch):
    from services import openposition_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

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


def test_openposition_skip_routes_to_sandbox(fresh_intent_db, monkeypatch):
    """Mode-only (B2): legacy intent 'skip' collapses to SANDBOX — reads sandbox
    positions, not the broker positionbook fall-through."""
    from services import openposition_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

    sandbox_mock = MagicMock(return_value=(True, {"status": "success", "data": []}, 200))
    monkeypatch.setattr("services.sandbox_service.sandbox_get_positions", sandbox_mock)
    monkeypatch.setattr(
        "services.openposition_service.socketio.start_background_task",
        lambda *a, **kw: None,
    )

    fake_pb = MagicMock(return_value=(True, {"data": []}, 200))
    monkeypatch.setattr("services.positionbook_service.get_positionbook", fake_pb)

    success, _, status = openposition_service.get_open_position_with_auth(
        {"apikey": "test", "symbol": "INFY", "exchange": "NSE", "product": "MIS"},
        auth_token="dummy",
        broker="zerodha",
        original_data={"apikey": "test", "symbol": "INFY", "exchange": "NSE", "product": "MIS"},
    )

    assert success is True
    sandbox_mock.assert_called_once()
    fake_pb.assert_not_called()


# ---------------------------------------------------------------------------
# orderstatus
# ---------------------------------------------------------------------------


def test_orderstatus_routes_to_sandbox_when_sandbox(fresh_intent_db, monkeypatch):
    from services import orderstatus_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

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


def test_orderstatus_skip_routes_to_sandbox(fresh_intent_db, monkeypatch):
    """Mode-only (B2): legacy intent 'skip' collapses to SANDBOX — order status is
    read from the sandbox book, not the broker orderbook fall-through."""
    from services import orderstatus_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    _patch_modes(monkeypatch)

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
