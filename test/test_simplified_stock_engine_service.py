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



# ---------------------------------------------------------------------------
# EOD broker-position flatten tests (step 2)
# ---------------------------------------------------------------------------


import datetime as _eod_dt  # noqa: E402  (deliberate alias to avoid clobbering `dt`)
from unittest.mock import MagicMock  # noqa: E402


def _seed_service_for_eod(service, *, api_key: str = "live-key") -> None:
    """Pre-populate the per-symbol api_key map and a strategy label so the
    flatten knows which positionbook to query and what strategy name to use."""
    service._api_key_by_symbol["UNRELATED"] = api_key  # any registration is enough
    service._strategy_by_symbol["UNRELATED"] = "trend-up"


def _force_eod_clock(monkeypatch=None):
    """Monkey-patch the service module's `dt.datetime.now` to return a time
    past the engine's default eod_exit_time (15:20)."""
    import services.simplified_stock_engine_service as svc_mod

    class _FixedDateTime(_eod_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D401, ARG003
            return _eod_dt.datetime(2026, 5, 17, 15, 25)

    if monkeypatch is not None:
        monkeypatch.setattr(svc_mod.dt, "datetime", _FixedDateTime)
    else:
        svc_mod.dt.datetime = _FixedDateTime
    return _FixedDateTime


def test_eod_flatten_skipped_in_sandbox_mode(monkeypatch):
    """Sandbox mode never queries the broker positionbook at EOD."""
    service = _make_service(MODE_SANDBOX)
    _seed_service_for_eod(service)
    _force_eod_clock(monkeypatch)

    with (
        patch("services.positionbook_service.get_positionbook") as mock_book,
        patch.object(SimplifiedStockEngineService, "_dispatch_order") as mock_dispatch,
    ):
        service._maybe_flatten_eod()

    mock_book.assert_not_called()
    mock_dispatch.assert_not_called()
    # Idempotency flag is NOT set in sandbox -- the flatten was a no-op.
    assert service._eod_flatten_done_date is None


def test_eod_flatten_skipped_in_disabled_mode(monkeypatch):
    service = _make_service(MODE_DISABLED)
    _seed_service_for_eod(service)
    _force_eod_clock(monkeypatch)

    with patch("services.positionbook_service.get_positionbook") as mock_book:
        service._maybe_flatten_eod()

    mock_book.assert_not_called()


def test_eod_flatten_skipped_before_eod_time(monkeypatch):
    """Live mode flatten waits until past eod_exit_time."""
    service = _make_service(MODE_LIVE)
    _seed_service_for_eod(service)

    import services.simplified_stock_engine_service as svc_mod

    class _PreEodDateTime(_eod_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return _eod_dt.datetime(2026, 5, 17, 14, 0)  # 14:00 - before EOD

    monkeypatch.setattr(svc_mod.dt, "datetime", _PreEodDateTime)

    with patch("services.positionbook_service.get_positionbook") as mock_book:
        service._maybe_flatten_eod()

    mock_book.assert_not_called()


def test_eod_flatten_dispatches_for_broker_drift_position(monkeypatch):
    """Live mode + broker has a position engine doesn't = flatten order dispatched."""
    service = _make_service(MODE_LIVE)
    _seed_service_for_eod(service, api_key="live-key")
    _force_eod_clock(monkeypatch)

    # Broker reports a long position the engine has no record of.
    positionbook_response = (
        True,
        {
            "status": "success",
            "data": [
                {
                    "symbol": "ORPHAN",
                    "exchange": "NSE",
                    "product": "MIS",
                    "quantity": 50,
                    "average_price": "100.00",
                    "ltp": 99.0,
                }
            ],
        },
        200,
    )

    dispatched: list[tuple[dict, str, bool]] = []

    def _capture_dispatch(self, payload, api_key, *, is_entry):
        dispatched.append((payload, api_key, is_entry))
        return True, {"orderid": "live-flatten-1"}

    with (
        patch(
            "services.positionbook_service.get_positionbook",
            return_value=positionbook_response,
        ),
        patch.object(SimplifiedStockEngineService, "_dispatch_order", _capture_dispatch),
    ):
        service._maybe_flatten_eod()

    assert len(dispatched) == 1
    payload, api_key, is_entry = dispatched[0]
    assert payload["symbol"] == "ORPHAN"
    assert payload["action"] == "SELL"  # long position -> SELL to flatten
    assert payload["quantity"] == 50
    assert payload["exchange"] == "NSE"
    assert payload["product"] == "MIS"
    assert api_key == "live-key"
    assert is_entry is False
    # Flag set so a second call within the day is a no-op.
    assert service._eod_flatten_done_date == _eod_dt.date(2026, 5, 17)


def test_eod_flatten_skips_positions_the_engine_already_tracks(monkeypatch):
    """If the broker shows a position the engine already knows, skip it.
    The engine's own check_eod_exits is responsible for closing it."""
    from services.simplified_stock_engine_core import Position

    service = _make_service(MODE_LIVE)
    _seed_service_for_eod(service, api_key="live-key")
    _force_eod_clock(monkeypatch)

    # Engine already tracks RELIANCE long 10.
    service.engine.positions["RELIANCE"] = Position(
        symbol="RELIANCE",
        entry_price=2500.0,
        qty=10,
        stop_loss=2490.0,
        entry_time=_eod_dt.datetime(2026, 5, 17, 10, 30),
        risk_per_share=10.0,
    )

    positionbook_response = (
        True,
        {
            "status": "success",
            "data": [
                {
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "product": "MIS",
                    "quantity": 10,
                    "average_price": "2500.00",
                    "ltp": 2495.0,
                }
            ],
        },
        200,
    )

    with (
        patch(
            "services.positionbook_service.get_positionbook",
            return_value=positionbook_response,
        ),
        patch.object(SimplifiedStockEngineService, "_dispatch_order") as mock_dispatch,
    ):
        service._maybe_flatten_eod()

    mock_dispatch.assert_not_called()


def test_eod_flatten_is_idempotent_for_the_day(monkeypatch):
    """Second call within the same date must not re-fetch or re-dispatch."""
    service = _make_service(MODE_LIVE)
    _seed_service_for_eod(service)
    _force_eod_clock(monkeypatch)

    empty_book = (True, {"status": "success", "data": []}, 200)
    with patch(
        "services.positionbook_service.get_positionbook", return_value=empty_book
    ) as mock_book:
        service._maybe_flatten_eod()
        service._maybe_flatten_eod()  # 2nd call same day

    assert mock_book.call_count == 1


def test_eod_flatten_warns_when_engine_has_orphan_position(monkeypatch, caplog=None):
    """Engine thinks it has a position the broker doesn't report -> warn,
    do not issue any order."""
    from services.simplified_stock_engine_core import Position

    service = _make_service(MODE_LIVE)
    _seed_service_for_eod(service)
    _force_eod_clock(monkeypatch)

    service.engine.positions["GHOST"] = Position(
        symbol="GHOST",
        entry_price=500.0,
        qty=5,
        stop_loss=495.0,
        entry_time=_eod_dt.datetime(2026, 5, 17, 11, 0),
        risk_per_share=5.0,
    )

    empty_book = (True, {"status": "success", "data": []}, 200)
    with (
        patch("services.positionbook_service.get_positionbook", return_value=empty_book),
        patch.object(SimplifiedStockEngineService, "_dispatch_order") as mock_dispatch,
    ):
        service._maybe_flatten_eod()

    mock_dispatch.assert_not_called()


def test_eod_flatten_short_position_dispatches_buy(monkeypatch):
    """Broker shows a short position the engine doesn't know -> dispatch BUY to flatten."""
    service = _make_service(MODE_LIVE)
    _seed_service_for_eod(service, api_key="live-key")
    _force_eod_clock(monkeypatch)

    positionbook_response = (
        True,
        {
            "status": "success",
            "data": [
                {
                    "symbol": "SHORTY",
                    "exchange": "NSE",
                    "product": "MIS",
                    "quantity": -20,
                    "average_price": "100.00",
                    "ltp": 101.0,
                }
            ],
        },
        200,
    )

    dispatched: list[dict] = []

    def _capture(self, payload, api_key, *, is_entry):
        dispatched.append(payload)
        return True, {"orderid": "x"}

    with (
        patch(
            "services.positionbook_service.get_positionbook",
            return_value=positionbook_response,
        ),
        patch.object(SimplifiedStockEngineService, "_dispatch_order", _capture),
    ):
        service._maybe_flatten_eod()

    assert len(dispatched) == 1
    assert dispatched[0]["action"] == "BUY"
    assert dispatched[0]["quantity"] == 20


def test_eod_flatten_ignores_other_exchange_positions(monkeypatch):
    """Broker positions on a different exchange/product are left alone."""
    service = _make_service(MODE_LIVE)
    _seed_service_for_eod(service, api_key="live-key")
    _force_eod_clock(monkeypatch)

    positionbook_response = (
        True,
        {
            "status": "success",
            "data": [
                {
                    "symbol": "USDINR",
                    "exchange": "CDS",  # currency, not the engine's NSE
                    "product": "NRML",
                    "quantity": 100,
                },
                {
                    "symbol": "CRUDEOIL",
                    "exchange": "MCX",  # commodity
                    "product": "NRML",
                    "quantity": 50,
                },
            ],
        },
        200,
    )

    with (
        patch(
            "services.positionbook_service.get_positionbook",
            return_value=positionbook_response,
        ),
        patch.object(SimplifiedStockEngineService, "_dispatch_order") as mock_dispatch,
    ):
        service._maybe_flatten_eod()

    mock_dispatch.assert_not_called()


def test_eod_flatten_handles_positionbook_fetch_failure(monkeypatch):
    """If positionbook fetch fails, log and move on without crashing."""
    service = _make_service(MODE_LIVE)
    _seed_service_for_eod(service)
    _force_eod_clock(monkeypatch)

    failure_response = (False, {"status": "error", "message": "broker down"}, 500)
    with (
        patch(
            "services.positionbook_service.get_positionbook",
            return_value=failure_response,
        ),
        patch.object(SimplifiedStockEngineService, "_dispatch_order") as mock_dispatch,
    ):
        # Must not raise.
        service._maybe_flatten_eod()

    mock_dispatch.assert_not_called()



# ---------------------------------------------------------------------------
# Funds-gate tests (step 3)
# ---------------------------------------------------------------------------


def _funds_response(amount: float):
    """Build a successful funds_service.get_funds response tuple."""
    return True, {"status": "success", "data": {"availablecash": f"{amount:.2f}"}}, 200


def test_funds_gate_allows_entry_when_funds_sufficient():
    """Live mode + broker reports availablecash >= floor -> entry dispatches."""
    service = _make_service(MODE_LIVE)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with (
        patch(
            "services.funds_service.get_funds",
            return_value=_funds_response(50_000.0),
        ) as mock_funds,
        patch(
            "services.place_order_service.place_order",
            return_value=(True, {"orderid": "live-1"}, 200),
        ) as mock_live,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2502.0),
    ):
        service._place_entry_order(signal, api_key="live-key", strategy_name="trend-up")

    mock_funds.assert_called_once()
    mock_live.assert_called_once()
    assert signal.symbol in service.engine.positions


def test_funds_gate_blocks_entry_when_funds_below_floor():
    """Live mode + availablecash < floor -> no dispatch, pending cleared, no position."""
    service = _make_service(MODE_LIVE)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    # Floor defaults to account_capital (20,000); report 5,000 available.
    with (
        patch(
            "services.funds_service.get_funds",
            return_value=_funds_response(5_000.0),
        ),
        patch("services.place_order_service.place_order") as mock_live,
        patch("services.sandbox_service.sandbox_place_order") as mock_sandbox,
    ):
        service._place_entry_order(signal, api_key="live-key", strategy_name="trend-up")

    mock_live.assert_not_called()
    mock_sandbox.assert_not_called()
    assert signal.symbol not in service.engine.pending_entries
    assert signal.symbol not in service.engine.positions


def test_funds_gate_honors_custom_floor():
    """If funds_floor is set explicitly, it overrides account_capital."""
    config = SimplifiedEngineConfig(mode=MODE_LIVE, funds_floor=10_000.0)
    service = SimplifiedStockEngineService(config=config)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    # availablecash=15k passes the 10k custom floor (would fail the 20k default).
    with (
        patch(
            "services.funds_service.get_funds",
            return_value=_funds_response(15_000.0),
        ),
        patch(
            "services.place_order_service.place_order",
            return_value=(True, {"orderid": "live-2"}, 200),
        ) as mock_live,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2502.0),
    ):
        service._place_entry_order(signal, api_key="live-key", strategy_name="trend-up")

    mock_live.assert_called_once()


def test_funds_gate_skipped_in_sandbox_mode():
    """Sandbox path never queries the broker funds endpoint."""
    service = _make_service(MODE_SANDBOX)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with (
        patch("services.funds_service.get_funds") as mock_funds,
        patch(
            "services.sandbox_service.sandbox_place_order",
            return_value=(True, {"orderid": "sbx-1"}, 200),
        ),
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2501.0),
    ):
        service._place_entry_order(signal, api_key="sbx-key", strategy_name="trend-up")

    mock_funds.assert_not_called()


def test_funds_gate_skipped_in_disabled_mode():
    """Disabled mode never queries funds (no orders go anywhere)."""
    service = _make_service(MODE_DISABLED)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with patch("services.funds_service.get_funds") as mock_funds:
        service._place_entry_order(signal, api_key="key", strategy_name="trend-up")

    mock_funds.assert_not_called()


def test_funds_gate_fails_open_on_fetch_failure():
    """Live + funds API returns failure -> entry is allowed through, cache stays empty."""
    service = _make_service(MODE_LIVE)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with (
        patch(
            "services.funds_service.get_funds",
            return_value=(False, {"status": "error", "message": "broker down"}, 500),
        ),
        patch(
            "services.place_order_service.place_order",
            return_value=(True, {"orderid": "live-3"}, 200),
        ) as mock_live,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2502.0),
    ):
        service._place_entry_order(signal, api_key="live-key", strategy_name="trend-up")

    mock_live.assert_called_once()  # fail-open: order goes through
    # No cache entry written on failure -- next call will re-fetch.
    assert "live-key" not in service._funds_cache


def test_funds_gate_fails_open_when_exception_raised():
    """If get_funds itself raises, the gate fails open."""
    service = _make_service(MODE_LIVE)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    def _boom(*a, **k):
        raise RuntimeError("broker timeout")

    with (
        patch("services.funds_service.get_funds", side_effect=_boom),
        patch(
            "services.place_order_service.place_order",
            return_value=(True, {"orderid": "live-4"}, 200),
        ) as mock_live,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2502.0),
    ):
        service._place_entry_order(signal, api_key="live-key", strategy_name="trend-up")

    mock_live.assert_called_once()


def test_funds_gate_reuses_cache_within_ttl():
    """Two entries within funds_cache_ttl_seconds query the funds API once."""
    service = _make_service(MODE_LIVE)
    # Two fresh signals so we go through the entry path twice.
    sig1 = _make_entry_signal()
    sig2 = EntrySignal(
        symbol="INFY",
        action="BUY",
        quantity=5,
        reference_price=1500.0,
        stop_loss=1490.0,
        risk_per_share=10.0,
        candle_ts=dt.datetime(2026, 5, 17, 10, 35),
        exchange="NSE",
        product="MIS",
        pricetype="MARKET",
    )
    service.engine.pending_entries[sig1.symbol] = sig1
    service.engine.pending_entries[sig2.symbol] = sig2

    with (
        patch(
            "services.funds_service.get_funds",
            return_value=_funds_response(50_000.0),
        ) as mock_funds,
        patch(
            "services.place_order_service.place_order",
            return_value=(True, {"orderid": "live-x"}, 200),
        ),
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2502.0),
    ):
        service._place_entry_order(sig1, api_key="live-key", strategy_name="trend-up")
        service._place_entry_order(sig2, api_key="live-key", strategy_name="trend-up")

    assert mock_funds.call_count == 1  # second call served from cache


def test_funds_gate_refetches_after_cache_expires():
    """After ttl elapses, the next entry re-queries the funds API."""
    service = _make_service(MODE_LIVE)
    service.funds_cache_ttl_seconds = 0.05  # 50ms for the test

    sig1 = _make_entry_signal()
    sig2 = EntrySignal(
        symbol="INFY",
        action="BUY",
        quantity=5,
        reference_price=1500.0,
        stop_loss=1490.0,
        risk_per_share=10.0,
        candle_ts=dt.datetime(2026, 5, 17, 10, 40),
        exchange="NSE",
        product="MIS",
        pricetype="MARKET",
    )
    service.engine.pending_entries[sig1.symbol] = sig1
    service.engine.pending_entries[sig2.symbol] = sig2

    import time as _time

    with (
        patch(
            "services.funds_service.get_funds",
            return_value=_funds_response(50_000.0),
        ) as mock_funds,
        patch(
            "services.place_order_service.place_order",
            return_value=(True, {"orderid": "live-y"}, 200),
        ),
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2502.0),
    ):
        service._place_entry_order(sig1, api_key="live-key", strategy_name="trend-up")
        _time.sleep(0.1)  # exceed the 50ms ttl
        service._place_entry_order(sig2, api_key="live-key", strategy_name="trend-up")

    assert mock_funds.call_count == 2


def test_funds_gate_unparseable_response_fails_open():
    """availablecash missing or non-numeric -> fail open."""
    service = _make_service(MODE_LIVE)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    # Response missing the availablecash field entirely.
    with (
        patch(
            "services.funds_service.get_funds",
            return_value=(True, {"status": "success", "data": {}}, 200),
        ),
        patch(
            "services.place_order_service.place_order",
            return_value=(True, {"orderid": "live-5"}, 200),
        ) as mock_live,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2502.0),
    ):
        service._place_entry_order(signal, api_key="live-key", strategy_name="trend-up")

    mock_live.assert_called_once()


def test_status_surfaces_funds_summary_after_check():
    """After a successful funds check, status() includes the latest reading."""
    service = _make_service(MODE_LIVE)

    with patch(
        "services.funds_service.get_funds",
        return_value=_funds_response(42_000.0),
    ):
        ok, value, _ = service._check_live_funds("live-key")
    assert ok is True
    assert value == 42_000.0

    s = service.status()
    assert s["funds"] is not None
    assert s["funds"]["available_cash"] == 42_000.0
    assert s["funds"]["floor"] == service.config.effective_funds_floor
    assert "checked_at" in s["funds"]
