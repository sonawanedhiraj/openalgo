"""Issue #497 — the position READ path must resolve the same book the order
WRITE path chose.

#440 made ``resolve_effective_mode()`` the platform analyze overlay only. Every
in-repo strategy that reads back its OWN positions through ``get_positionbook``
was still resolving the book from that overlay, so with Analyze OFF a
``sandbox`` strategy wrote its orders into ``sandbox.db`` and then read the LIVE
broker book. ``futures_follow_cap50`` saw an empty position list, rehydrated 0
positions, and stopped firing T+1 exits entirely for four trading days — 7 NIFTY
lots stranded at 110% of the book against a 50% cap.

The invariant these tests pin: **an order placed under
``resolve_order_mode(K)`` is read back under ``resolve_order_mode(K)``.**

They exercise the REAL routing decision inside ``get_positionbook_with_auth``.
They deliberately do NOT mock ``get_positionbook`` — mocking that call is
precisely what let the original bug through the existing suite.
"""

from unittest.mock import patch

import pytest

from services.positionbook_service import get_positionbook_with_auth

_SANDBOX_BOOK = (
    True,
    {
        "status": "success",
        "data": [
            {
                "symbol": "NIFTY25AUG26FUT",
                "exchange": "NFO",
                "product": "NRML",
                "quantity": 455,
                "average_price": "24234.07",
            }
        ],
    },
    200,
)


def _broker_funcs():
    """Broker module stub whose positionbook is EMPTY — the real-world shape.

    The live Zerodha book holds no NIFTY futures for a sandbox-mode strategy,
    which is why the misrouted read returned nothing instead of erroring.
    """
    return {
        "get_positions": lambda auth_token: [],
        "map_position_data": lambda d: d,
        "transform_positions_data": lambda d: d,
    }


@pytest.fixture
def routing(monkeypatch):
    """Drive the two visible controls: the navbar Analyze toggle and the
    per-strategy strategy_mode row."""

    state = {"analyze": False, "modes": {}}

    def fake_get_mode(strategy_name):
        mode = state["modes"].get(strategy_name)
        return {"mode": mode} if mode else None

    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: state["analyze"])
    monkeypatch.setattr("database.strategy_mode_db.get_mode", fake_get_mode)
    # Env fall-through must not leak a mode into resolve_mode().
    monkeypatch.delenv("FUTURES_FOLLOW_MODE", raising=False)
    return state


def _call(mode_key=None):
    with (
        patch(
            "services.sandbox_service.sandbox_get_positions", return_value=_SANDBOX_BOOK
        ) as sandbox,
        patch(
            "services.positionbook_service.import_broker_module", return_value=_broker_funcs()
        ) as broker,
    ):
        ok, resp, status = get_positionbook_with_auth(
            "auth-token",
            "zerodha",
            {"apikey": "k"},
            mode_key=mode_key,
        )
    return ok, resp, status, sandbox, broker


# --------------------------------------------------------------------------- #
# The regression — this is the test that fails on the pre-fix tree
# --------------------------------------------------------------------------- #
def test_sandbox_strategy_reads_sandbox_book_when_analyze_off(routing):
    """#497: Analyze OFF + strategy_mode='sandbox' MUST read the sandbox book.

    Pre-fix this fell through to ``resolve_effective_mode()``, which returns
    LIVE whenever Analyze is off — so the read hit the broker book, came back
    empty, and the T+1 exit squared off nothing.
    """
    routing["analyze"] = False
    routing["modes"]["futures_follow_cap50"] = "sandbox"

    ok, resp, _status, sandbox, broker = _call(mode_key="futures_follow_cap50")

    sandbox.assert_called_once()
    broker.assert_not_called()
    assert ok is True
    assert [p["symbol"] for p in resp["data"]] == ["NIFTY25AUG26FUT"]


def test_live_strategy_reads_broker_book_when_analyze_off(routing):
    """A genuinely live strategy still reads the real broker book."""
    routing["analyze"] = False
    routing["modes"]["sector_follow_cap5_vol"] = "live"

    _ok, _resp, _status, sandbox, broker = _call(mode_key="sector_follow_cap5_vol")

    broker.assert_called_once()
    sandbox.assert_not_called()


def test_analyze_toggle_forces_sandbox_even_for_a_live_strategy(routing):
    """Analyze ON is the platform kill switch — it outranks the strategy row."""
    routing["analyze"] = True
    routing["modes"]["sector_follow_cap5_vol"] = "live"

    _ok, _resp, _status, sandbox, broker = _call(mode_key="sector_follow_cap5_vol")

    sandbox.assert_called_once()
    broker.assert_not_called()


def test_unknown_strategy_defaults_to_sandbox(routing):
    """Default deny: no strategy_mode row → sandbox, matching resolve_order_mode."""
    routing["analyze"] = False

    _ok, _resp, _status, sandbox, broker = _call(mode_key="never_configured")

    sandbox.assert_called_once()
    broker.assert_not_called()


# --------------------------------------------------------------------------- #
# Back-compat: mode_key omitted keeps the analyze-overlay behavior for UI reads
# --------------------------------------------------------------------------- #
def test_no_mode_key_analyze_off_reads_broker_book(routing):
    """UI read decorations are unchanged: Analyze OFF shows the live book."""
    routing["analyze"] = False

    _ok, _resp, _status, sandbox, broker = _call(mode_key=None)

    broker.assert_called_once()
    sandbox.assert_not_called()


def test_no_mode_key_analyze_on_reads_sandbox_book(routing):
    """UI read decorations are unchanged: Analyze ON shows the sandbox book."""
    routing["analyze"] = True

    _ok, _resp, _status, sandbox, broker = _call(mode_key=None)

    sandbox.assert_called_once()
    broker.assert_not_called()
