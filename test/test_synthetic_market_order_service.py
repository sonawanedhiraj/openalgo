"""Tests for the stock-option synthetic-market LIMIT conversion (issue #438).

Zerodha RMS blocks MARKET / SL-M orders on individual-stock options (OPTSTK)
and commodity options. ``ensure_live_safe_pricetype`` converts those to a
protective LIMIT (BUY above LTP / SELL below LTP, tick-rounded) on the LIVE
dispatch path only. These tests monkeypatch the symbol-info lookup and the
LTP fetch so no DB or broker access happens.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import restx_api  # noqa: F401  — pre-resolve the restx/services circular import
from database.token_db_enhanced import SymbolData
from services import synthetic_market_order_service as smos


def _symbol_data(symbol, exchange, instrumenttype, tick_size=0.05, underlying=None):
    return SymbolData(
        symbol=symbol,
        brsymbol=symbol,
        name=underlying or symbol,
        exchange=exchange,
        brexchange=exchange,
        token="12345",
        instrumenttype=instrumenttype,
        tick_size=tick_size,
        underlying=underlying,
    )


@pytest.fixture
def stock_option_env(monkeypatch):
    """RELIANCE call option on NFO with LTP 100.0 and tick 0.05."""
    monkeypatch.setattr(
        smos,
        "get_symbol_info",
        lambda s, e: _symbol_data(s, e, "CE", underlying="RELIANCE"),
    )
    monkeypatch.setattr(smos, "_fetch_ltp", lambda *a: 100.0)


def _order(symbol="RELIANCE28AUG251400CE", exchange="NFO", action="BUY", pricetype="MARKET"):
    return {
        "symbol": symbol,
        "exchange": exchange,
        "action": action,
        "pricetype": pricetype,
        "product": "MIS",
        "quantity": "500",
    }


# ---------------------------------------------------------------------------
# MARKET → LIMIT conversion
# ---------------------------------------------------------------------------


def test_buy_market_stock_option_converts_to_limit_above_ltp(stock_option_env):
    converted, info, error = smos.ensure_live_safe_pricetype(_order(), "tok", "zerodha")

    assert error is None
    assert converted["pricetype"] == "LIMIT"
    # 100 * 1.05 = 105.00, already on tick
    assert converted["price"] == pytest.approx(105.0)
    assert converted["price"] > 100.0
    assert info["original_pricetype"] == "MARKET"
    assert info["reference_price"] == pytest.approx(100.0)


def test_sell_market_stock_option_converts_to_limit_below_ltp(stock_option_env):
    converted, info, error = smos.ensure_live_safe_pricetype(
        _order(action="SELL"), "tok", "zerodha"
    )

    assert error is None
    assert converted["pricetype"] == "LIMIT"
    assert converted["price"] == pytest.approx(95.0)
    assert converted["price"] < 100.0


def test_limit_price_rounds_to_tick_buy_up_sell_down(monkeypatch):
    monkeypatch.setattr(
        smos,
        "get_symbol_info",
        lambda s, e: _symbol_data(s, e, "PE", underlying="TCS"),
    )
    monkeypatch.setattr(smos, "_fetch_ltp", lambda *a: 33.33)

    buy, _, _ = smos.ensure_live_safe_pricetype(_order(action="BUY"), "tok", "zerodha")
    sell, _, _ = smos.ensure_live_safe_pricetype(_order(action="SELL"), "tok", "zerodha")

    # 33.33 * 1.05 = 34.9965 → ceil to 35.00; 33.33 * 0.95 = 31.6635 → floor to 31.65
    assert buy["price"] == pytest.approx(35.00)
    assert sell["price"] == pytest.approx(31.65)
    for price in (buy["price"], sell["price"]):
        assert round(price / 0.05) * 0.05 == pytest.approx(price)


def test_sell_of_cheap_option_never_floors_to_zero(monkeypatch):
    monkeypatch.setattr(
        smos,
        "get_symbol_info",
        lambda s, e: _symbol_data(s, e, "CE", underlying="IDEA"),
    )
    monkeypatch.setattr(smos, "_fetch_ltp", lambda *a: 0.05)

    sell, _, error = smos.ensure_live_safe_pricetype(_order(action="SELL"), "tok", "zerodha")

    assert error is None
    assert sell["price"] >= 0.05  # clamped to one tick, not 0


# ---------------------------------------------------------------------------
# SL-M → SL conversion (priced off trigger, no LTP fetch)
# ---------------------------------------------------------------------------


def test_slm_stock_option_converts_to_sl_off_trigger(stock_option_env, monkeypatch):
    fetch = MagicMock()
    monkeypatch.setattr(smos, "_fetch_ltp", fetch)

    order = _order(pricetype="SL-M")
    order["trigger_price"] = 80.0
    converted, info, error = smos.ensure_live_safe_pricetype(order, "tok", "zerodha")

    assert error is None
    assert converted["pricetype"] == "SL"
    assert converted["price"] == pytest.approx(84.0)  # 80 * 1.05
    assert converted["trigger_price"] == 80.0  # untouched
    fetch.assert_not_called()


def test_slm_without_trigger_is_rejected(stock_option_env):
    order = _order(pricetype="SL-M")
    order["trigger_price"] = 0
    converted, info, error = smos.ensure_live_safe_pricetype(order, "tok", "zerodha")

    assert error is not None and "SL-M" in error
    assert converted["pricetype"] == "SL-M"  # unchanged — caller must not place it


# ---------------------------------------------------------------------------
# Passthrough cases — nothing else is touched
# ---------------------------------------------------------------------------


def test_index_option_passes_through(monkeypatch):
    monkeypatch.setattr(
        smos,
        "get_symbol_info",
        lambda s, e: _symbol_data(s, e, "CE", underlying="NIFTY"),
    )
    fetch = MagicMock()
    monkeypatch.setattr(smos, "_fetch_ltp", fetch)

    order = _order(symbol="NIFTY28AUG2524000CE")
    converted, info, error = smos.ensure_live_safe_pricetype(order, "tok", "zerodha")

    assert (converted, info, error) == (order, None, None)
    fetch.assert_not_called()


def test_stock_future_passes_through(monkeypatch):
    monkeypatch.setattr(
        smos,
        "get_symbol_info",
        lambda s, e: _symbol_data(s, e, "FUT", underlying="RELIANCE"),
    )
    order = _order(symbol="RELIANCE28AUG25FUT")
    converted, info, error = smos.ensure_live_safe_pricetype(order, "tok", "zerodha")

    assert (converted, info, error) == (order, None, None)


def test_nse_equity_passes_through_without_symbol_lookup(monkeypatch):
    lookup = MagicMock()
    monkeypatch.setattr(smos, "get_symbol_info", lookup)

    order = _order(symbol="RELIANCE", exchange="NSE")
    converted, info, error = smos.ensure_live_safe_pricetype(order, "tok", "zerodha")

    assert (converted, info, error) == (order, None, None)
    lookup.assert_not_called()


def test_limit_order_on_stock_option_passes_through(stock_option_env):
    order = _order(pricetype="LIMIT")
    order["price"] = 101.5
    converted, info, error = smos.ensure_live_safe_pricetype(order, "tok", "zerodha")

    assert (converted, info, error) == (order, None, None)


def test_mcx_commodity_option_converts(monkeypatch):
    monkeypatch.setattr(
        smos,
        "get_symbol_info",
        lambda s, e: _symbol_data(s, e, "CE", tick_size=0.1, underlying="CRUDEOIL"),
    )
    monkeypatch.setattr(smos, "_fetch_ltp", lambda *a: 250.0)

    order = _order(symbol="CRUDEOIL19AUG255600CE", exchange="MCX")
    converted, info, error = smos.ensure_live_safe_pricetype(order, "tok", "zerodha")

    assert error is None
    assert converted["pricetype"] == "LIMIT"
    assert converted["price"] == pytest.approx(262.5)  # 250 * 1.05, tick 0.1


# ---------------------------------------------------------------------------
# Failure paths — loud, never silent
# ---------------------------------------------------------------------------


def test_missing_ltp_rejects_with_clear_error(stock_option_env, monkeypatch):
    monkeypatch.setattr(smos, "_fetch_ltp", lambda *a: None)

    order = _order()
    converted, info, error = smos.ensure_live_safe_pricetype(order, "tok", "zerodha")

    assert error is not None
    assert "LTP" in error and "not placed" in error
    assert converted["pricetype"] == "MARKET"  # unchanged — caller must reject


def test_unexpected_internal_error_fails_open(monkeypatch):
    def _boom(s, e):
        raise RuntimeError("symbol cache exploded")

    monkeypatch.setattr(smos, "get_symbol_info", _boom)

    order = _order()
    converted, info, error = smos.ensure_live_safe_pricetype(order, "tok", "zerodha")

    # Fail open: pass through unchanged (same behavior as pre-fix) — never block flow.
    assert (converted, info, error) == (order, None, None)


def test_flag_off_disables_conversion(stock_option_env, monkeypatch):
    monkeypatch.setenv("OPTSTK_SYNTHETIC_MARKET_ENABLED", "false")

    order = _order()
    converted, info, error = smos.ensure_live_safe_pricetype(order, "tok", "zerodha")

    assert (converted, info, error) == (order, None, None)


def test_buffer_pct_env_override(stock_option_env, monkeypatch):
    monkeypatch.setenv("OPTSTK_SYNTHETIC_MARKET_BUFFER_PCT", "10")

    converted, info, error = smos.ensure_live_safe_pricetype(_order(), "tok", "zerodha")

    assert error is None
    assert converted["price"] == pytest.approx(110.0)
    assert info["buffer_pct"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Classification fallbacks (symbol info missing from the master contract)
# ---------------------------------------------------------------------------


def test_classification_falls_back_to_symbol_suffix_when_info_missing(monkeypatch):
    monkeypatch.setattr(smos, "get_symbol_info", lambda s, e: None)

    assert smos.is_market_blocked_option("RELIANCE28AUG251400CE", "NFO") is True
    assert smos.is_market_blocked_option("NIFTY28AUG2524000CE", "NFO") is False
    assert smos.is_market_blocked_option("RELIANCE28AUG25FUT", "NFO") is False
    assert smos.is_market_blocked_option("RELIANCE", "NSE") is False


# ---------------------------------------------------------------------------
# Wiring — place_order_with_auth LIVE path converts / rejects before dispatch
# ---------------------------------------------------------------------------


@pytest.fixture
def _live_mode(monkeypatch):
    from services.mode_service import EffectiveMode

    # Issue #440: dispatch is per-strategy via resolve_order_mode(mode_key).
    monkeypatch.setattr(
        "services.place_order_service.resolve_order_mode",
        lambda _key: EffectiveMode.LIVE,
    )


def _stock_option_payload():
    return {
        "apikey": "test-api-key",  # pragma: allowlist secret
        "strategy": "unit_test",
        "symbol": "RELIANCE28AUG251400CE",
        "exchange": "NFO",
        "action": "BUY",
        "quantity": 500,
        "pricetype": "MARKET",
        "product": "MIS",
    }


def test_live_place_order_dispatches_converted_limit(_live_mode, monkeypatch):
    from services import place_order_service

    monkeypatch.setattr(
        smos,
        "get_symbol_info",
        lambda s, e: _symbol_data(s, e, "CE", underlying="RELIANCE"),
    )
    monkeypatch.setattr(smos, "_fetch_ltp", lambda *a: 100.0)

    broker_place_order = MagicMock(
        return_value=(SimpleNamespace(status=200), {"status": "ok"}, "OID-438")
    )
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place_order),
    )

    payload = _stock_option_payload()
    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="tok",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True and status == 200
    dispatched = broker_place_order.call_args[0][0]
    assert dispatched["pricetype"] == "LIMIT"
    assert dispatched["price"] == pytest.approx(105.0)


def test_live_place_order_rejects_when_ltp_unavailable(_live_mode, monkeypatch):
    from services import place_order_service

    monkeypatch.setattr(
        smos,
        "get_symbol_info",
        lambda s, e: _symbol_data(s, e, "CE", underlying="RELIANCE"),
    )
    monkeypatch.setattr(smos, "_fetch_ltp", lambda *a: None)

    broker_place_order = MagicMock()
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place_order),
    )

    payload = _stock_option_payload()
    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="tok",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is False and status == 400
    assert response["status"] == "error"
    assert "LTP" in response["message"]
    broker_place_order.assert_not_called()
