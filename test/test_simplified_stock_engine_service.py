import datetime as dt
from unittest.mock import patch

import pytest

from services.simplified_stock_engine_core import (
    MODE_DISABLED,
    MODE_LIVE,
    MODE_SANDBOX,
    Candle,
    EntrySignal,
    ExitSignal,
    SimplifiedEngineConfig,
)
from services.simplified_stock_engine_service import (
    SimplifiedStockEngineService,
    _resolve_mode_from_env,
    normalize_chartink_symbol,
    parse_chartink_symbols,
)


def test_parse_chartink_symbols_normalizes_and_deduplicates():
    payload = {
        "stocks": "NSE:RELIANCE, INFY.NS, TCS-EQ",
        "symbol": "RELIANCE",
        "nsecode": ["WIPRO"],
    }

    assert parse_chartink_symbols(payload) == ["RELIANCE", "INFY", "TCS", "WIPRO"]


def test_normalize_chartink_symbol_removes_common_chartink_suffixes():
    assert normalize_chartink_symbol("NSE:RELIANCE-EQ") == "RELIANCE"
    assert normalize_chartink_symbol("infy.ns") == "INFY"


def test_history_row_to_candle_accepts_epoch_millis():
    row = {
        "timestamp": 1_777_433_100_000,
        "open": 100,
        "high": 102,
        "low": 99,
        "close": 101,
        "volume": 1000,
    }

    candle = SimplifiedStockEngineService._row_to_candle(row)

    assert isinstance(candle, Candle)
    assert candle.elapsed_pct == 1.0
    assert candle.ts.second == 0
    assert candle.ts.microsecond == 0


def test_history_row_to_candle_accepts_datetime_string():
    row = {
        "datetime": "2026-04-29T10:17:00+05:30",
        "open": 100,
        "high": 102,
        "low": 99,
        "close": 101,
        "volume": 1000,
    }

    candle = SimplifiedStockEngineService._row_to_candle(row)

    assert candle.ts == dt.datetime(2026, 4, 29, 10, 15)



# ---------------------------------------------------------------------------
# Mode resolution + dispatch tests
# ---------------------------------------------------------------------------


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


def _make_exit_signal() -> ExitSignal:
    return ExitSignal(
        symbol="RELIANCE",
        action="SELL",
        quantity=10,
        reason="stop_loss",
        reference_price=2490.0,
        exchange="NSE",
        product="MIS",
        pricetype="MARKET",
    )


def _make_service(mode: str) -> SimplifiedStockEngineService:
    """Build a service with the given mode, isolated from env vars."""
    config = SimplifiedEngineConfig(mode=mode)
    return SimplifiedStockEngineService(config=config)


def test_resolve_mode_from_env_explicit_mode(monkeypatch):
    monkeypatch.setenv("SIMPLIFIED_ENGINE_MODE", "live")
    monkeypatch.delenv("SIMPLIFIED_ENGINE_DRY_RUN", raising=False)
    assert _resolve_mode_from_env() == MODE_LIVE


def test_resolve_mode_from_env_invalid_falls_back_to_sandbox(monkeypatch):
    monkeypatch.setenv("SIMPLIFIED_ENGINE_MODE", "paper")
    monkeypatch.delenv("SIMPLIFIED_ENGINE_DRY_RUN", raising=False)
    assert _resolve_mode_from_env() == MODE_SANDBOX


def test_resolve_mode_dry_run_true_maps_to_sandbox(monkeypatch):
    monkeypatch.delenv("SIMPLIFIED_ENGINE_MODE", raising=False)
    monkeypatch.setenv("SIMPLIFIED_ENGINE_DRY_RUN", "true")
    assert _resolve_mode_from_env() == MODE_SANDBOX


def test_resolve_mode_dry_run_false_maps_to_live(monkeypatch):
    monkeypatch.delenv("SIMPLIFIED_ENGINE_MODE", raising=False)
    monkeypatch.setenv("SIMPLIFIED_ENGINE_DRY_RUN", "false")
    assert _resolve_mode_from_env() == MODE_LIVE


def test_resolve_mode_no_env_defaults_to_sandbox(monkeypatch):
    monkeypatch.delenv("SIMPLIFIED_ENGINE_MODE", raising=False)
    monkeypatch.delenv("SIMPLIFIED_ENGINE_DRY_RUN", raising=False)
    assert _resolve_mode_from_env() == MODE_SANDBOX


def test_config_rejects_unknown_mode():
    with pytest.raises(ValueError):
        SimplifiedEngineConfig(mode="bogus")


def test_disabled_mode_skips_order_dispatch():
    """In disabled mode, no order is sent but engine state advances locally."""
    service = _make_service(MODE_DISABLED)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal  # arm pending

    with (
        patch("services.sandbox_service.sandbox_place_order") as mock_sandbox,
        patch("services.place_order_service.place_order") as mock_live,
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="test")

    mock_sandbox.assert_not_called()
    mock_live.assert_not_called()
    # Position was created from the reference price (engine confirmed locally).
    assert signal.symbol in service.engine.positions
    assert service.engine.positions[signal.symbol].entry_price == signal.reference_price


def test_sandbox_mode_routes_entry_to_sandbox_place_order():
    """In sandbox mode, entries hit sandbox_place_order, never live place_order."""
    service = _make_service(MODE_SANDBOX)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    sandbox_response = (True, {"orderid": "sbx-abc", "status": "success", "mode": "analyze"}, 200)
    with (
        patch(
            "services.sandbox_service.sandbox_place_order",
            return_value=sandbox_response,
        ) as mock_sandbox,
        patch("services.place_order_service.place_order") as mock_live,
        patch.object(
            SimplifiedStockEngineService, "_wait_for_fill", return_value=2501.5
        ),
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_live.assert_not_called()
    mock_sandbox.assert_called_once()
    # Inspect the payload that was forwarded to sandbox.
    call_args, call_kwargs = mock_sandbox.call_args
    forwarded_payload = call_args[0]
    assert forwarded_payload["symbol"] == "RELIANCE"
    assert forwarded_payload["action"] == "BUY"
    assert forwarded_payload["quantity"] == 10
    assert forwarded_payload["exchange"] == "NSE"
    assert forwarded_payload["product"] == "MIS"
    assert forwarded_payload["pricetype"] == "MARKET"
    assert forwarded_payload["strategy"] == "trend-up"
    assert call_kwargs["api_key"] == "test-key"
    # Position recorded at the executed price returned by _wait_for_fill.
    assert service.engine.positions[signal.symbol].entry_price == 2501.5


def test_live_mode_routes_entry_to_place_order():
    """In live mode, entries hit place_order_service.place_order."""
    service = _make_service(MODE_LIVE)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    live_response = (True, {"orderid": "live-xyz", "status": "success"}, 200)
    with (
        patch(
            "services.place_order_service.place_order", return_value=live_response
        ) as mock_live,
        patch("services.sandbox_service.sandbox_place_order") as mock_sandbox,
        patch.object(
            SimplifiedStockEngineService, "_wait_for_fill", return_value=2502.0
        ),
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_sandbox.assert_not_called()
    mock_live.assert_called_once()
    assert service.engine.positions[signal.symbol].entry_price == 2502.0


def test_sandbox_mode_routes_exit_to_sandbox_place_order():
    """Exits in sandbox mode also go through sandbox_place_order, not live."""
    service = _make_service(MODE_SANDBOX)
    signal = _make_exit_signal()
    service.engine.pending_exits[signal.symbol] = signal
    # Seed a position so confirm_exit has something to close.
    from services.simplified_stock_engine_core import Position

    service.engine.positions[signal.symbol] = Position(
        symbol=signal.symbol,
        entry_price=2500.0,
        qty=10,
        stop_loss=2490.0,
        entry_time=dt.datetime(2026, 5, 17, 10, 30),
        risk_per_share=10.0,
    )

    sandbox_response = (True, {"orderid": "sbx-exit", "status": "success", "mode": "analyze"}, 200)
    with (
        patch(
            "services.sandbox_service.sandbox_place_order",
            return_value=sandbox_response,
        ) as mock_sandbox,
        patch("services.place_order_service.place_order") as mock_live,
        patch.object(
            SimplifiedStockEngineService, "_wait_for_fill", return_value=2489.0
        ),
    ):
        service._place_exit_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_live.assert_not_called()
    mock_sandbox.assert_called_once()
    assert signal.symbol not in service.engine.positions


def test_sandbox_rejection_clears_pending_entry():
    """If sandbox rejects the order, the engine's pending entry is cleared."""
    service = _make_service(MODE_SANDBOX)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    sandbox_response = (
        False,
        {"status": "error", "message": "insufficient funds"},
        400,
    )
    with patch(
        "services.sandbox_service.sandbox_place_order", return_value=sandbox_response
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    # Pending entry must be cleared so the engine does not get stuck.
    assert signal.symbol not in service.engine.pending_entries
    # No position created.
    assert signal.symbol not in service.engine.positions


def test_mode_label_reflects_engine_mode():
    """The status label tracks the engine mode, not legacy dry_run."""
    assert _make_service(MODE_DISABLED)._mode_label() == "disabled"
    assert _make_service(MODE_SANDBOX)._mode_label() == "sandbox"
    # Live mode's label depends on the global analyze_mode flag; we do not
    # exercise that here to avoid touching the live settings DB.
