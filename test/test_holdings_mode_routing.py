"""Issue #752 — the HOLDINGS read path must resolve the same book the strategy's
orders were written to (the #497 rule, one service over).

A CNC position bought by a sandbox strategy moves from sandbox positions into
sandbox HOLDINGS at the 03:00 session expiry, so ``cas_auction_reversal``'s T+1
exit reads holdings. With the analyze overlay (Analyze OFF → LIVE) it would read
the empty broker holdings. These tests drive the REAL routing decision inside
``get_holdings_with_auth`` — they deliberately do not mock ``get_holdings``.
"""

from unittest.mock import patch

import pytest

from services.holdings_service import get_holdings_with_auth

_SANDBOX_HOLDINGS = (
    True,
    {
        "status": "success",
        "data": {
            "holdings": [{"symbol": "SBIN", "exchange": "NSE", "product": "CNC", "quantity": 52}],
            "statistics": {},
        },
    },
    200,
)


def _broker_funcs():
    return {
        "get_holdings": lambda auth_token: [],
        "map_portfolio_data": lambda d: d,
        "calculate_portfolio_statistics": lambda d: {},
        "transform_holdings_data": lambda d: d,
    }


@pytest.fixture
def routing(monkeypatch):
    state = {"analyze": False, "modes": {}}

    def fake_get_mode(strategy_name):
        mode = state["modes"].get(strategy_name)
        return {"mode": mode} if mode else None

    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: state["analyze"])
    monkeypatch.setattr("database.strategy_mode_db.get_mode", fake_get_mode)
    return state


def _call(mode_key=None):
    with (
        patch(
            "services.sandbox_service.sandbox_get_holdings", return_value=_SANDBOX_HOLDINGS
        ) as sandbox,
        patch(
            "services.holdings_service.import_broker_module", return_value=_broker_funcs()
        ) as broker,
    ):
        ok, resp, status = get_holdings_with_auth(
            "auth-token", "zerodha", {"apikey": "k"}, mode_key=mode_key
        )
    return ok, resp, status, sandbox, broker


def test_sandbox_strategy_reads_sandbox_holdings_when_analyze_off(routing):
    routing["modes"]["cas_auction_reversal"] = "sandbox"
    ok, resp, _s, sandbox, broker = _call(mode_key="cas_auction_reversal")
    sandbox.assert_called_once()
    broker.assert_not_called()
    assert ok and resp["data"]["holdings"][0]["symbol"] == "SBIN"


def test_unconfigured_strategy_defaults_to_sandbox_holdings(routing):
    _ok, _r, _s, sandbox, broker = _call(mode_key="cas_auction_reversal")
    sandbox.assert_called_once()
    broker.assert_not_called()


def test_live_strategy_reads_broker_holdings(routing):
    routing["modes"]["cas_auction_reversal"] = "live"
    _ok, _r, _s, sandbox, broker = _call(mode_key="cas_auction_reversal")
    broker.assert_called_once()
    sandbox.assert_not_called()


def test_analyze_on_forces_sandbox_even_for_live_strategy(routing):
    routing["analyze"] = True
    routing["modes"]["cas_auction_reversal"] = "live"
    _ok, _r, _s, sandbox, broker = _call(mode_key="cas_auction_reversal")
    sandbox.assert_called_once()
    broker.assert_not_called()


def test_no_mode_key_keeps_overlay_behavior(routing):
    routing["analyze"] = False
    _ok, _r, _s, sandbox, broker = _call(mode_key=None)
    broker.assert_called_once()
    sandbox.assert_not_called()
