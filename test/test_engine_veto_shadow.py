"""Tests for the Stage-1 LLM veto layer wired into the simplified engine.

We mock ``signal_review_service.review_signal`` directly so the engine never
touches the bridge or the DB. The mode is driven by ``VETO_LAYER_MODE``.
"""

import datetime as dt
from unittest.mock import patch

import pytest

# Pre-resolve the restx_api / services.place_order_service circular import
# before pulling in services.X below. Without this, services.place_order_service
# imports restx_api.schemas which re-enters services.place_order_service via
# services.options_multiorder_service and trips a partial-init ImportError.
# See conftest.py for the full description of the cycle.
import restx_api  # noqa: F401, E402
import services.place_order_service  # noqa: F401 — eager bind for mock.patch
import services.sandbox_service  # noqa: F401 — eager bind for mock.patch
from services.simplified_stock_engine_core import (
    MODE_SANDBOX,
    EntrySignal,
    SimplifiedEngineConfig,
)
from services.simplified_stock_engine_service import SimplifiedStockEngineService


def _make_entry_signal() -> EntrySignal:
    return EntrySignal(
        symbol="RELIANCE",
        action="BUY",
        quantity=10,
        reference_price=2500.0,
        stop_loss=2490.0,
        risk_per_share=10.0,
        candle_ts=dt.datetime(2026, 5, 17, 10, 30),
        exchange="NSE",
        product="MIS",
        pricetype="MARKET",
    )


def _make_service() -> SimplifiedStockEngineService:
    return SimplifiedStockEngineService(config=SimplifiedEngineConfig(mode=MODE_SANDBOX))


@pytest.fixture(autouse=True)
def reset_review_cache():
    """The veto layer caches per (symbol, source); wipe before each test."""
    from services import signal_review_service as srs

    srs.clear_review_cache()
    yield
    srs.clear_review_cache()


def _stub_review(decision: str, decision_id: int = 42, reasoning: str = "stubbed"):
    """Build a stub ``review_signal`` return value matching the real shape."""
    return {
        "id": decision_id,
        "decision": decision,
        "reasoning": reasoning,
        "confidence": 0.7,
        "latency_ms": 100,
        "claude_session_id": "sid",
        "raw_output": "",
        "enforcement_mode": "shadow",
        "cache_hit": False,
    }


def _sandbox_success_response():
    return (True, {"orderid": "sbx-veto-1", "status": "success", "mode": "analyze"}, 200)


# ---------------------------------------------------------------------------
# shadow mode — decision recorded, never enforced
# ---------------------------------------------------------------------------


def test_shadow_mode_records_decision_but_does_not_block(monkeypatch):
    """LLM says skip, but shadow mode means place_order is still called."""
    monkeypatch.setenv("VETO_LAYER_MODE", "shadow")
    service = _make_service()
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with (
        patch(
            "services.signal_review_service.review_signal",
            return_value=_stub_review("skip"),
        ) as mock_review,
        patch("services.signal_review_service.mark_actually_taken") as mock_mark,
        patch(
            "services.sandbox_service.sandbox_place_order",
            return_value=_sandbox_success_response(),
        ) as mock_sandbox,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2501.5),
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    # Reviewer was consulted exactly once.
    mock_review.assert_called_once()
    assert mock_review.call_args.kwargs["symbol"] == "RELIANCE"
    assert mock_review.call_args.kwargs["source"] == "trend-up"
    # Sandbox order was placed despite the skip recommendation.
    mock_sandbox.assert_called_once()
    # Outcome was reported back to the audit table as actually_taken=True.
    mock_mark.assert_called_with(42, True)
    # Position confirmed at the executed price.
    assert service.engine.positions[signal.symbol].entry_price == 2501.5


def test_shadow_mode_take_decision_also_places_order(monkeypatch):
    """Sanity check: shadow + take still places the order."""
    monkeypatch.setenv("VETO_LAYER_MODE", "shadow")
    service = _make_service()
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with (
        patch(
            "services.signal_review_service.review_signal",
            return_value=_stub_review("take"),
        ),
        patch("services.signal_review_service.mark_actually_taken"),
        patch(
            "services.sandbox_service.sandbox_place_order",
            return_value=_sandbox_success_response(),
        ) as mock_sandbox,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2501.5),
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_sandbox.assert_called_once()


# ---------------------------------------------------------------------------
# active mode — skip short-circuits, take proceeds
# ---------------------------------------------------------------------------


def test_active_mode_blocks_on_skip(monkeypatch):
    """In active mode, decision='skip' must short-circuit before place_order."""
    monkeypatch.setenv("VETO_LAYER_MODE", "active")
    service = _make_service()
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with (
        patch(
            "services.signal_review_service.review_signal",
            return_value=_stub_review("skip", reasoning="negative breadth"),
        ),
        patch("services.signal_review_service.mark_actually_taken") as mock_mark,
        patch("services.sandbox_service.sandbox_place_order") as mock_sandbox,
        patch("services.place_order_service.place_order") as mock_live,
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_sandbox.assert_not_called()
    mock_live.assert_not_called()
    # Pending entry is cleared so the engine doesn't get stuck.
    assert signal.symbol not in service.engine.pending_entries
    # No position created.
    assert signal.symbol not in service.engine.positions
    # Audit row updated with actually_taken=False.
    mock_mark.assert_called_with(42, False)


def test_active_mode_proceeds_on_take(monkeypatch):
    monkeypatch.setenv("VETO_LAYER_MODE", "active")
    service = _make_service()
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with (
        patch(
            "services.signal_review_service.review_signal",
            return_value=_stub_review("take", reasoning="regime aligned"),
        ),
        patch("services.signal_review_service.mark_actually_taken") as mock_mark,
        patch(
            "services.sandbox_service.sandbox_place_order",
            return_value=_sandbox_success_response(),
        ) as mock_sandbox,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2501.5),
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_sandbox.assert_called_once()
    mock_mark.assert_called_with(42, True)
    assert service.engine.positions[signal.symbol].entry_price == 2501.5


# ---------------------------------------------------------------------------
# off mode — reviewer not called at all
# ---------------------------------------------------------------------------


def test_off_mode_does_not_call_reviewer(monkeypatch):
    monkeypatch.setenv("VETO_LAYER_MODE", "off")
    service = _make_service()
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with (
        patch("services.signal_review_service.review_signal") as mock_review,
        patch("services.signal_review_service.mark_actually_taken") as mock_mark,
        patch(
            "services.sandbox_service.sandbox_place_order",
            return_value=_sandbox_success_response(),
        ) as mock_sandbox,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2501.5),
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_review.assert_not_called()
    mock_mark.assert_not_called()
    mock_sandbox.assert_called_once()


# ---------------------------------------------------------------------------
# Fail-open behaviours
# ---------------------------------------------------------------------------


def test_review_exception_fails_open_to_take(monkeypatch):
    """If the reviewer raises, the order still goes out (sandbox in this test)."""
    monkeypatch.setenv("VETO_LAYER_MODE", "active")
    service = _make_service()
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with (
        patch(
            "services.signal_review_service.review_signal",
            side_effect=RuntimeError("reviewer module unreachable"),
        ),
        patch("services.signal_review_service.mark_actually_taken") as mock_mark,
        patch(
            "services.sandbox_service.sandbox_place_order",
            return_value=_sandbox_success_response(),
        ) as mock_sandbox,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2501.5),
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_sandbox.assert_called_once()
    # decision_id is None when the reviewer raised; mark_* must be skipped.
    mock_mark.assert_not_called()


def test_unknown_veto_mode_falls_back_to_shadow(monkeypatch):
    """VETO_LAYER_MODE=garbage should resolve to shadow (never enforce)."""
    monkeypatch.setenv("VETO_LAYER_MODE", "yolo")
    service = _make_service()
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with (
        patch(
            "services.signal_review_service.review_signal",
            return_value=_stub_review("skip"),
        ) as mock_review,
        patch("services.signal_review_service.mark_actually_taken"),
        patch(
            "services.sandbox_service.sandbox_place_order",
            return_value=_sandbox_success_response(),
        ) as mock_sandbox,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2501.5),
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    # Reviewer was called (shadow mode), but the skip was not enforced.
    mock_review.assert_called_once()
    mock_sandbox.assert_called_once()


def test_default_veto_mode_is_shadow(monkeypatch):
    """No VETO_LAYER_MODE env at all → shadow by default, never blocks."""
    monkeypatch.delenv("VETO_LAYER_MODE", raising=False)
    service = _make_service()
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with (
        patch(
            "services.signal_review_service.review_signal",
            return_value=_stub_review("skip"),
        ),
        patch("services.signal_review_service.mark_actually_taken"),
        patch(
            "services.sandbox_service.sandbox_place_order",
            return_value=_sandbox_success_response(),
        ) as mock_sandbox,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2501.5),
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_sandbox.assert_called_once()


# ---------------------------------------------------------------------------
# Order-failure outcome reporting
# ---------------------------------------------------------------------------


def test_order_failure_marks_actually_taken_false(monkeypatch):
    """If the broker rejects, actually_taken must be False on the audit row."""
    monkeypatch.setenv("VETO_LAYER_MODE", "shadow")
    service = _make_service()
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    sandbox_failure = (False, {"status": "error", "message": "no funds"}, 400)

    with (
        patch(
            "services.signal_review_service.review_signal",
            return_value=_stub_review("take"),
        ),
        patch("services.signal_review_service.mark_actually_taken") as mock_mark,
        patch("services.sandbox_service.sandbox_place_order", return_value=sandbox_failure),
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_mark.assert_called_with(42, False)
    assert signal.symbol not in service.engine.positions
