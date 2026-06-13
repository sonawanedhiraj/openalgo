"""Regression tests for the silent-drop audit fixes (2026-06-11).

Covers the aggregation / partial-fill findings that are not already exercised by
the per-module test files:

* P1-3 options multi-order — leg-split partial fill and spread-level aggregation
* P1-2 sandbox execution — fill must not be banked 'complete' if the position
  update raises

(P0-1 basket and P1-4 trade-journal regressions live in
``test_basket_order_dispatch.py`` and ``test_trade_journal_service.py``.)
"""

from types import SimpleNamespace

# Pre-import the restx layer first to establish the canonical module load order.
# restx_api.options_multiorder and services.options_multiorder_service form a
# latent circular import (the restx module imports the service at module level);
# importing the service first in isolation raises ImportError. The full app /
# test suite loads restx first, so this mirrors that order.
import restx_api.options_multiorder  # noqa: F401,E402

# ---------------------------------------------------------------------------
# P1-3 — options multi-order partial fills
# ---------------------------------------------------------------------------


def test_options_leg_split_partial_fill_reports_partial(monkeypatch):
    """A split leg where only SOME sub-orders fill must report status='partial',
    not 'success' (which would mask a directionally-naked, under-filled leg)."""
    from services import options_multiorder_service as oms

    monkeypatch.setattr(oms, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(
        oms,
        "get_option_symbol",
        lambda **kw: (
            True,
            {"symbol": "NIFTY28MAR2420800CE", "exchange": "NFO", "underlying_ltp": 100.0},
            200,
        ),
    )

    # 3 sub-orders: first fills, the other two are rejected.
    sub_order_results = iter(
        [
            {"order_num": 1, "status": "success", "orderid": "A"},
            {"order_num": 2, "status": "error", "message": "rejected"},
            {"order_num": 3, "status": "error", "message": "rejected"},
        ]
    )
    monkeypatch.setattr(
        oms,
        "place_single_split_order_for_leg",
        lambda *a, **k: next(sub_order_results),
    )

    leg = {
        "action": "BUY",
        "option_type": "CE",
        "offset": 0,
        "quantity": 75,
        "splitsize": 25,
    }
    common = {
        "underlying": "NIFTY",
        "exchange": "NFO",
        "expiry_date": "2026-03-28",
        "strike_int": 50,
        "strategy": "ut",
    }

    result = oms.resolve_and_place_leg(
        leg,
        common,
        api_key="k",
        leg_index=0,
        total_legs=1,
        auth_token="t",
        broker="zerodha",
    )

    assert result["status"] == "partial"
    assert result["successful_orders"] == 1
    assert result["total_split_orders"] == 3


def test_options_spread_partial_legs_reports_partial(monkeypatch):
    """The spread-level response must reflect actual per-leg outcomes — some legs
    filled, some rejected => top-level status='partial', not 'success'."""
    from services import options_multiorder_service as oms

    monkeypatch.setattr(oms, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(oms, "get_underlying_ltp", lambda *a, **k: (True, 100.0, None))

    leg_calls = {"n": 0}

    def _fake_leg(
        leg,
        common_data,
        api_key,
        orig_idx,
        total_legs,
        auth_token,
        broker,
        underlying_ltp,
        leg_quote_cache=None,
    ):
        leg_calls["n"] += 1
        status = "success" if leg_calls["n"] == 1 else "error"
        return {"leg": orig_idx + 1, "symbol": f"SYM{orig_idx}", "status": status}

    monkeypatch.setattr(oms, "resolve_and_place_leg", _fake_leg)

    data = {
        "underlying": "NIFTY",
        "exchange": "NFO",
        "expiry_date": "2026-03-28",
        "strike_int": 50,
        "strategy": "ut",
        "legs": [
            {"action": "BUY", "option_type": "CE", "offset": 0, "quantity": 50},
            {"action": "BUY", "option_type": "PE", "offset": 0, "quantity": 50},
        ],
    }

    success, response, status = oms.process_multiorder_with_auth(
        data,
        auth_token="t",
        broker="zerodha",
        api_key="k",
        original_data=data,
    )

    assert response["status"] == "partial"
    assert response["successful_legs"] == 1
    assert response["failed_legs"] == 1


def test_options_spread_all_legs_rejected_reports_error(monkeypatch):
    """Every leg rejected => top-level status='error', never 'success'."""
    from services import options_multiorder_service as oms

    monkeypatch.setattr(oms, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(oms, "get_underlying_ltp", lambda *a, **k: (True, 100.0, None))
    monkeypatch.setattr(
        oms,
        "resolve_and_place_leg",
        lambda leg, common_data, api_key, orig_idx, *a, **k: {
            "leg": orig_idx + 1,
            "status": "error",
            "message": "rejected",
        },
    )

    data = {
        "underlying": "NIFTY",
        "exchange": "NFO",
        "expiry_date": "2026-03-28",
        "strike_int": 50,
        "strategy": "ut",
        "legs": [
            {"action": "BUY", "option_type": "CE", "offset": 0, "quantity": 50},
            {"action": "SELL", "option_type": "PE", "offset": 0, "quantity": 50},
        ],
    }

    success, response, status = oms.process_multiorder_with_auth(
        data,
        auth_token="t",
        broker="zerodha",
        api_key="k",
        original_data=data,
    )

    assert response["status"] == "error"
    assert response["successful_legs"] == 0


# ---------------------------------------------------------------------------
# P1-2 — sandbox fill must not be banked 'complete' if position update fails
# ---------------------------------------------------------------------------


def test_sandbox_fill_not_banked_complete_when_position_update_raises(monkeypatch):
    """If _update_position raises, the order must end 'rejected' and must NEVER
    have been committed in the 'complete' state — preventing a banked fill with
    no matching position (silent-drop audit P1-2)."""
    from sandbox import execution_engine as ee

    committed_states = []

    order = SimpleNamespace(
        orderid="O1",
        user_id="u",
        symbol="INFY",
        exchange="NSE",
        action="BUY",
        quantity=1,
        product="MIS",
        strategy="ut",
        order_status="open",
        average_price=None,
        filled_quantity=0,
        pending_quantity=1,
        update_timestamp=None,
        rejection_reason=None,
        margin_blocked=0,
    )

    class _FakeSession:
        def add(self, *a, **k):
            pass

        def commit(self):
            committed_states.append(order.order_status)

        def rollback(self):
            pass

    monkeypatch.setattr(ee, "db_session", _FakeSession())

    engine = ee.ExecutionEngine()
    monkeypatch.setattr(engine, "_generate_trade_id", lambda: "T1")
    monkeypatch.setattr(engine, "_publish_fill_event", lambda **k: None)

    def _boom(o, price):
        raise RuntimeError("contract-value lookup failed")

    monkeypatch.setattr(engine, "_update_position", _boom)

    engine._execute_order(order, execution_price=100.0)

    # Order ends rejected, and no commit ever persisted 'complete'.
    assert order.order_status == "rejected"
    assert "complete" not in committed_states
