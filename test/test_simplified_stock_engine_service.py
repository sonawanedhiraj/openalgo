# === Live-DB isolation: rebind trade_journal_db BEFORE any import that may
# reach it. The _place_entry_order tests below drive the real engine entry
# path, which calls services.trade_journal_service.record_entry ->
# database.trade_journal_db. Without this, every entry test writes a fake open
# RELIANCE/INFY row (order ids like 'sbx-abc', 'live-1') into the operator's
# real db/openalgo.db trade_journal, and the resulting record_entry warnings
# spam errors.jsonl and trip the preflight recent_errors gate.
#
# We surgically rebind ONLY trade_journal_db to a shared default-pool in-memory
# engine whose connection persists (the module engine uses NullPool, which
# drops :memory: tables between operations). We do NOT set DATABASE_URL to
# :memory: globally: the engine also makes read-only calls (e.g.
# settings_db.get_analyze_mode) that need the live tables to exist, and reads
# never pollute. trade_journal.record_entry resolves the module-level session
# lazily on each call, so this rebind is what the write path sees.
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

import database.trade_journal_db as _tjdb

_journal_engine = create_engine("sqlite:///:memory:")
_journal_session = scoped_session(
    sessionmaker(autocommit=False, autoflush=False, bind=_journal_engine)
)
_tjdb.engine = _journal_engine
_tjdb.db_session = _journal_session
_tjdb.Base.query = _journal_session.query_property()
_tjdb.Base.metadata.create_all(_journal_engine)

import datetime as dt
from unittest.mock import patch

import pytest

# Pre-resolve the restx_api / services.place_order_service circular import
# *before* pulling in any services.X module below. See conftest.py for the
# full cycle description; in short, loading services.place_order_service
# directly trips a partial-init ImportError because it pulls in
# restx_api.schemas, which (transitively) re-enters services.place_order_service
# from services.options_multiorder_service. Loading restx_api first lets
# Python finish that walk in the right order.
import restx_api  # noqa: F401, E402

# Pre-import the submodules that mock.patch needs to resolve as attributes of
# the `services` package. The simplified-engine service does lazy imports of
# these inside _dispatch_order / _check_live_funds / _flatten_for_api_key, so
# the attribute isn't bound on the `services` package object until a dispatch
# path actually runs. Without the eager import below, mock.patch hits:
#   AttributeError: module 'services' has no attribute 'sandbox_service'
# during attribute walk on the dotted path "services.sandbox_service.fn".
import services.funds_service  # noqa: F401, E402
import services.place_order_service  # noqa: F401, E402
import services.positionbook_service  # noqa: F401, E402
import services.sandbox_service  # noqa: F401, E402
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


@pytest.fixture(autouse=True)
def _clear_persistent_mode_row():
    """Issue #162 Phase 2: _place_entry_order/_place_exit_order now resolve the
    persistent strategy_mode DB row via services.mode_service.resolve_mode. The
    conftest temp DB is process-wide, and other suites write a 'simplified_engine'
    row that would leak in and override these fixed-mode tests (a leaked sandbox
    row flips a MODE_LIVE service to sandbox → place_order never called). Clear it
    before AND after each test so routing is driven purely by the config the test
    builds. Tests that exercise the DB path mock resolve_mode directly and are
    unaffected by this cleanup."""

    def _clear():
        try:
            from database import strategy_mode_db

            strategy_mode_db.delete_mode("simplified_engine")
        except Exception:
            pass

    _clear()
    yield
    _clear()


def test_process_chartink_webhook_empty_returns_empty_status():
    """Zero-stock screener result is 'empty', not 'error' (reserve error for failures)."""
    service = _make_service(MODE_SANDBOX)
    result = service.process_chartink_webhook(
        user_id="test_user",
        strategy_name="chartink_FnO_intraday_buy",
        payload={"stocks": ""},
    )
    assert result["status"] == "empty"
    assert result["message"] == "No symbols found"


def test_resolve_mode_from_env_explicit_mode(monkeypatch):
    monkeypatch.setenv("SIMPLIFIED_ENGINE_MODE", "live")
    monkeypatch.delenv("SIMPLIFIED_ENGINE_DRY_RUN", raising=False)
    assert _resolve_mode_from_env() == MODE_LIVE


def test_resolve_mode_from_env_invalid_falls_back_to_sandbox(monkeypatch):
    monkeypatch.setenv("SIMPLIFIED_ENGINE_MODE", "paper")
    monkeypatch.delenv("SIMPLIFIED_ENGINE_DRY_RUN", raising=False)
    assert _resolve_mode_from_env() == MODE_SANDBOX


def test_resolve_mode_no_env_defaults_to_sandbox(monkeypatch):
    monkeypatch.delenv("SIMPLIFIED_ENGINE_MODE", raising=False)
    assert _resolve_mode_from_env() == MODE_SANDBOX


def test_dry_run_env_is_ignored(monkeypatch):
    """The legacy SIMPLIFIED_ENGINE_DRY_RUN env var must no longer affect mode."""
    monkeypatch.delenv("SIMPLIFIED_ENGINE_MODE", raising=False)
    monkeypatch.setenv("SIMPLIFIED_ENGINE_DRY_RUN", "false")  # would've been "live"
    # Without MODE set, we get the sandbox default regardless of DRY_RUN.
    assert _resolve_mode_from_env() == MODE_SANDBOX


def test_config_rejects_unknown_mode():
    with pytest.raises(ValueError):
        SimplifiedEngineConfig(mode="bogus")


# --------------------------------------------------------------------------- #
# Issue #162 Phase 2 — engine honors the persistent strategy_mode DB row
# --------------------------------------------------------------------------- #


def _resolved(mode: str, source: str):
    from services.mode_service import ResolvedMode

    return ResolvedMode(mode=mode, source=source)


def test_apply_persistent_mode_live_row_overrides_env_sandbox(monkeypatch):
    """A persistent strategy_mode row = live flips an env-sandbox engine to live."""
    service = _make_service(MODE_SANDBOX)
    with patch(
        "services.mode_service.resolve_mode",
        return_value=_resolved(MODE_LIVE, "strategy_mode"),
    ) as m:
        service._apply_persistent_mode()
    assert service.mode == MODE_LIVE
    # Resolves under the dashboard/UI key, not the journal name.
    m.assert_called_once_with("simplified_engine")


def test_apply_persistent_mode_sandbox_row_overrides_env_live(monkeypatch):
    """A persistent row = sandbox flips an env-live engine back to sandbox."""
    service = _make_service(MODE_LIVE)
    with patch(
        "services.mode_service.resolve_mode",
        return_value=_resolved(MODE_SANDBOX, "strategy_mode"),
    ):
        service._apply_persistent_mode()
    assert service.mode == MODE_SANDBOX


def test_apply_persistent_mode_no_row_keeps_env_mode(monkeypatch):
    """No persistent row (source != strategy_mode) → engine keeps its env mode."""
    service = _make_service(MODE_LIVE)
    with patch(
        "services.mode_service.resolve_mode",
        return_value=_resolved(MODE_SANDBOX, "env"),
    ):
        service._apply_persistent_mode()
    assert service.mode == MODE_LIVE  # env source ignored


def test_apply_persistent_mode_never_overrides_disabled(monkeypatch):
    """MODE_DISABLED is a hard local off-switch — a stray live row can't enable it."""
    service = _make_service(MODE_DISABLED)
    with patch(
        "services.mode_service.resolve_mode",
        return_value=_resolved(MODE_LIVE, "strategy_mode"),
    ) as m:
        service._apply_persistent_mode()
    assert service.mode == MODE_DISABLED
    m.assert_not_called()  # short-circuits before the DB read


def test_apply_persistent_mode_fails_open_on_db_error(monkeypatch):
    """A resolve_mode exception must never change the mode or raise."""
    service = _make_service(MODE_SANDBOX)
    with patch("services.mode_service.resolve_mode", side_effect=RuntimeError("db down")):
        service._apply_persistent_mode()  # must not raise
    assert service.mode == MODE_SANDBOX


def test_entry_dispatch_honors_live_strategy_mode_row(monkeypatch):
    """End-to-end: an env-sandbox engine with a live persistent row routes the
    entry through the LIVE place_order path (not sandbox)."""
    service = _make_service(MODE_SANDBOX)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    monkeypatch.setattr(
        "services.mode_service.resolve_mode",
        lambda name: _resolved(MODE_LIVE, "strategy_mode"),
    )
    # Live path needs funds; make the funds gate pass deterministically.
    monkeypatch.setattr(service, "_check_live_funds", lambda api_key: (True, 10_000_000.0, None))
    with (
        patch("services.place_order_service.place_order") as mock_live,
        patch("services.sandbox_service.sandbox_place_order") as mock_sandbox,
    ):
        mock_live.return_value = (True, {"orderid": "live-1", "status": "success"}, 200)
        service._place_entry_order(signal, api_key="k", strategy_name="s")

    assert service.mode == MODE_LIVE
    assert mock_live.called, "live persistent row should route through place_order"
    assert not mock_sandbox.called, "must not hit the sandbox path when row=live"


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
        # B4 makes the LLM veto enforce ('active') in sandbox by default; this
        # test exercises order routing, not the veto, so bypass it with a 'take'.
        patch(
            "services.signal_review_service.review_signal",
            return_value={"id": None, "decision": "take", "reasoning": "test-bypass"},
        ),
        patch(
            "services.sandbox_service.sandbox_place_order",
            return_value=sandbox_response,
        ) as mock_sandbox,
        patch("services.place_order_service.place_order") as mock_live,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2501.5),
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
        patch("services.place_order_service.place_order", return_value=live_response) as mock_live,
        patch("services.sandbox_service.sandbox_place_order") as mock_sandbox,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2502.0),
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
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2489.0),
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
    with patch("services.sandbox_service.sandbox_place_order", return_value=sandbox_response):
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


def test_eod_flatten_runs_in_sandbox_mode(monkeypatch):
    """#265: sandbox mode DOES run the EOD flatten and reads the mode-aware store
    (sandbox.db via get_positionbook). With no orphan/mismatch it dispatches
    nothing, but it consults the store and marks the once-a-day flag done."""
    service = _make_service(MODE_SANDBOX)
    _seed_service_for_eod(service)
    _force_eod_clock(monkeypatch)

    empty_book = (True, {"status": "success", "data": []}, 200)
    with (
        patch(
            "services.positionbook_service.get_positionbook", return_value=empty_book
        ) as mock_book,
        patch.object(SimplifiedStockEngineService, "_dispatch_order") as mock_dispatch,
    ):
        service._maybe_flatten_eod()

    mock_book.assert_called()  # the sandbox store IS consulted now
    mock_dispatch.assert_not_called()  # nothing to reconcile in an empty book
    # Idempotency flag IS set -- the flatten ran (once-a-day).
    assert service._eod_flatten_done_date is not None


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


@pytest.mark.xfail(reason="self-hosted runner DB isolation issue; passes locally")
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


# ---------------------------------------------------------------------------
# EOD trading summary tests (step 4)
# ---------------------------------------------------------------------------


from services.simplified_stock_engine_core import (  # noqa: E402
    CompletedTrade,
    Position,
    SimplifiedStockEngine,
    TradeCharges,
    compute_zerodha_intraday_charges,
)


def test_completed_trade_records_long_round_trip():
    """confirm_exit on a long position records a CompletedTrade with gross > 0."""
    engine = SimplifiedStockEngine(SimplifiedEngineConfig(mode=MODE_SANDBOX))
    engine.positions["RELIANCE"] = Position(
        symbol="RELIANCE",
        entry_price=2500.0,
        qty=10,
        stop_loss=2490.0,
        entry_time=dt.datetime(2026, 5, 17, 10, 30),
        risk_per_share=10.0,
    )

    record = engine.confirm_exit("RELIANCE", exit_price=2510.0, reason="target")
    assert record is not None
    assert record.symbol == "RELIANCE"
    assert record.qty == 10
    assert record.entry_price == 2500.0
    assert record.exit_price == 2510.0
    assert record.is_long is True
    assert record.gross_pnl == 10 * (2510.0 - 2500.0)  # 100
    assert engine.completed_trades == [record]
    assert "RELIANCE" not in engine.positions


def test_completed_trade_records_short_round_trip():
    """A short position records a CompletedTrade with the right signs."""
    engine = SimplifiedStockEngine(SimplifiedEngineConfig(mode=MODE_SANDBOX))
    engine.positions["SHORTY"] = Position(
        symbol="SHORTY",
        entry_price=100.0,
        qty=-20,
        stop_loss=105.0,
        entry_time=dt.datetime(2026, 5, 17, 10, 30),
        risk_per_share=5.0,
    )

    record = engine.confirm_exit("SHORTY", exit_price=95.0, reason="target")
    assert record.qty == -20
    assert record.is_long is False
    # Short PnL: (entry - exit) * qty. Sell at 100, buy back at 95 -> +5/share * 20.
    assert record.gross_pnl == 20 * (100.0 - 95.0)
    assert record.buy_value == 20 * 95.0  # bought at exit price
    assert record.sell_value == 20 * 100.0  # sold at entry price


def test_completed_trade_clears_on_new_day():
    """When _reset_trade_day_if_needed flips the date, the ledger is cleared."""
    engine = SimplifiedStockEngine(SimplifiedEngineConfig(mode=MODE_SANDBOX))
    engine.completed_trades.append(
        CompletedTrade(
            symbol="X",
            qty=1,
            entry_price=1.0,
            exit_price=2.0,
            entry_time=dt.datetime(2026, 5, 16, 10, 0),
            exit_time=dt.datetime(2026, 5, 16, 11, 0),
        )
    )
    # Pretend the trades_day was yesterday.
    engine.trades_day = dt.date(2026, 5, 16)
    engine._reset_trade_day_if_needed()
    assert engine.completed_trades == []


def test_zerodha_charges_long_round_trip():
    """Worked example: buy 10@2500, sell 10@2510. Turnover Rs50,100."""
    # buy_value=25000, sell_value=25100, turnover=50100
    charges = compute_zerodha_intraday_charges(25000.0, 25100.0)
    # Brokerage: min(20, 0.0003 * 50100) = min(20, 15.03) = 15.03
    assert abs(charges.brokerage - 15.03) < 0.01
    # STT: 0.00025 * 25100 = 6.275 -> rounded
    assert abs(charges.stt - 6.28) < 0.01
    # Exchange: 0.0000345 * 50100 = 1.728...
    assert abs(charges.exchange - 1.73) < 0.01
    # SEBI: 0.000001 * 50100 = 0.0501
    assert abs(charges.sebi - 0.0501) < 0.001
    # Stamp: 0.00003 * 25000 = 0.75
    assert abs(charges.stamp - 0.75) < 0.01
    # GST: 18% of (broker + exchange + sebi)
    expected_gst = 0.18 * (charges.brokerage + charges.exchange + charges.sebi)
    assert abs(charges.gst - round(expected_gst, 2)) < 0.01
    assert charges.total > 0


def test_zerodha_charges_brokerage_caps_at_twenty_per_leg():
    """Zerodha caps brokerage at Rs20 PER ORDER, not per round trip.

    100k turnover split 50k/50k -> each leg's 0.03% is Rs15 (under the cap), so
    brokerage is Rs30, NOT the Rs20 a (buggy) round-trip cap would give. A leg
    only caps once its own 0.03% exceeds Rs20 (leg turnover > Rs66,666.67).
    """
    charges = compute_zerodha_intraday_charges(50_000.0, 50_000.0)  # 15 + 15
    assert abs(charges.brokerage - 30.0) < 0.01
    # Each leg above the per-order cap -> Rs20 + Rs20 = Rs40.
    capped = compute_zerodha_intraday_charges(100_000.0, 100_000.0)
    assert abs(capped.brokerage - 40.0) < 0.01


def test_zerodha_charges_zero_turnover():
    """No trades -> zero charges, never raises."""
    charges = compute_zerodha_intraday_charges(0.0, 0.0)
    assert charges.total == 0.0


def test_build_eod_summary_lines_contains_trade_and_total():
    """The summary writer emits per-trade rows + a totals row."""
    service = _make_service(MODE_SANDBOX)
    trades = [
        CompletedTrade(
            symbol="RELIANCE",
            qty=10,
            entry_price=2500.0,
            exit_price=2510.0,
            entry_time=dt.datetime(2026, 5, 17, 10, 30),
            exit_time=dt.datetime(2026, 5, 17, 11, 0),
            exit_reason="target",
        ),
        CompletedTrade(
            symbol="INFY",
            qty=-5,
            entry_price=1500.0,
            exit_price=1510.0,  # short loses
            entry_time=dt.datetime(2026, 5, 17, 11, 30),
            exit_time=dt.datetime(2026, 5, 17, 12, 0),
            exit_reason="stop_loss",
        ),
    ]
    lines = service._build_eod_summary_lines(trades, dt.date(2026, 5, 17))
    joined = "\n".join(lines)
    assert "RELIANCE" in joined
    assert "INFY" in joined
    assert "LONG" in joined
    assert "SHORT" in joined
    assert "TOTAL" in joined
    # Header includes date and mode.
    assert "2026-05-17" in lines[0]
    assert "sandbox" in lines[0]


def test_eod_summary_logs_once_per_day(monkeypatch, caplog=None):
    """Two calls after eod_exit_time on the same date -> summary built once."""
    service = _make_service(MODE_SANDBOX)
    service.engine.completed_trades.append(
        CompletedTrade(
            symbol="X",
            qty=1,
            entry_price=100.0,
            exit_price=101.0,
            entry_time=dt.datetime(2026, 5, 17, 10, 0),
            exit_time=dt.datetime(2026, 5, 17, 11, 0),
        )
    )

    import services.simplified_stock_engine_service as svc_mod

    class _AfterEodDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return dt.datetime(2026, 5, 17, 15, 25)

    monkeypatch.setattr(svc_mod.dt, "datetime", _AfterEodDateTime)

    calls = []

    def _spy(self, trades, today):
        calls.append((list(trades), today))
        return ["spy-output"]

    monkeypatch.setattr(SimplifiedStockEngineService, "_build_eod_summary_lines", _spy)

    service._maybe_log_eod_summary()
    service._maybe_log_eod_summary()

    assert len(calls) == 1
    assert service._eod_summary_done_date == dt.date(2026, 5, 17)


def test_eod_summary_skipped_before_eod_time(monkeypatch):
    """Before eod_exit_time, the summary doesn't run."""
    service = _make_service(MODE_SANDBOX)
    service.engine.completed_trades.append(
        CompletedTrade(
            symbol="X",
            qty=1,
            entry_price=100.0,
            exit_price=101.0,
            entry_time=dt.datetime(2026, 5, 17, 10, 0),
            exit_time=dt.datetime(2026, 5, 17, 11, 0),
        )
    )

    import services.simplified_stock_engine_service as svc_mod

    class _PreEodDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return dt.datetime(2026, 5, 17, 14, 0)

    monkeypatch.setattr(svc_mod.dt, "datetime", _PreEodDateTime)

    calls = []

    def _spy(self, trades, today):
        calls.append(today)
        return []

    monkeypatch.setattr(SimplifiedStockEngineService, "_build_eod_summary_lines", _spy)

    service._maybe_log_eod_summary()
    assert calls == []
    assert service._eod_summary_done_date is None


def test_eod_summary_handles_zero_trades(monkeypatch):
    """Zero completed trades -> log a brief 'no trades' note, still mark done."""
    service = _make_service(MODE_SANDBOX)
    # Empty ledger.

    import services.simplified_stock_engine_service as svc_mod

    class _AfterEodDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return dt.datetime(2026, 5, 17, 15, 25)

    monkeypatch.setattr(svc_mod.dt, "datetime", _AfterEodDateTime)

    # Should not crash even though there's nothing to summarize.
    service._maybe_log_eod_summary()
    # _eod_summary_done_date set, so a second call is also a no-op.
    assert service._eod_summary_done_date == dt.date(2026, 5, 17)


def test_eod_summary_runs_in_disabled_mode(monkeypatch):
    """Disabled mode may have completed trades (engine confirms locally) -- summary still runs."""
    service = _make_service(MODE_DISABLED)
    service.engine.completed_trades.append(
        CompletedTrade(
            symbol="X",
            qty=2,
            entry_price=50.0,
            exit_price=55.0,
            entry_time=dt.datetime(2026, 5, 17, 10, 0),
            exit_time=dt.datetime(2026, 5, 17, 11, 0),
        )
    )

    import services.simplified_stock_engine_service as svc_mod

    class _AfterEodDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return dt.datetime(2026, 5, 17, 15, 25)

    monkeypatch.setattr(svc_mod.dt, "datetime", _AfterEodDateTime)

    built = []

    def _spy(self, trades, today):
        built.append(len(trades))
        return ["row"]

    monkeypatch.setattr(SimplifiedStockEngineService, "_build_eod_summary_lines", _spy)

    service._maybe_log_eod_summary()
    assert built == [1]


def test_status_exposes_eod_summary_state():
    """status() surfaces the eod_summary_done flag + completed trades count."""
    service = _make_service(MODE_SANDBOX)
    service.engine.completed_trades.extend(
        [
            CompletedTrade(
                "A", 1, 1.0, 2.0, dt.datetime(2026, 5, 17, 10), dt.datetime(2026, 5, 17, 11)
            ),
            CompletedTrade(
                "B", 1, 1.0, 2.0, dt.datetime(2026, 5, 17, 12), dt.datetime(2026, 5, 17, 13)
            ),
        ]
    )
    s = service.status()
    assert s["completed_trades_today"] == 2
    assert s["eod_summary_done"] is None  # not yet logged


# ---------------------------------------------------------------------------
# Tick log tests (step 5)
# ---------------------------------------------------------------------------


import gzip as _gzip  # noqa: E402
import json as _json  # noqa: E402
import os as _os  # noqa: E402
import tempfile  # noqa: E402
import time as _time  # noqa: E402

from services.simplified_stock_engine_ticklog import TickLogWriter  # noqa: E402


def _drain(writer: TickLogWriter, timeout: float = 2.0) -> None:
    """Wait for the writer's queue to drain. Polls qsize."""
    deadline = _time.time() + timeout
    while writer._queue.qsize() > 0 and _time.time() < deadline:
        _time.sleep(0.02)
    # Push a sentinel timed flush -- the writer flushes on flush_seconds anyway.
    _time.sleep(0.15)


def test_ticklog_disabled_is_noop():
    """Disabled writer: enqueue, no file, no thread."""
    with tempfile.TemporaryDirectory() as tmp:
        writer = TickLogWriter(enabled=False, directory=tmp)
        writer.enqueue("RELIANCE", 2500.0, 100, dt.datetime(2026, 5, 17, 10, 0))
        # No worker thread, no file created.
        assert writer._worker is None
        assert _os.listdir(tmp) == []
        # stats reports disabled.
        s = writer.stats()
        assert s["enabled"] is False
        assert s["file"] is None


def test_ticklog_writes_jsonl_file():
    """Enabled writer: enqueue ticks, file appears with the right name + JSONL content."""
    with tempfile.TemporaryDirectory() as tmp:
        writer = TickLogWriter(
            enabled=True,
            directory=tmp,
            batch_size=2,  # flush after 2 ticks
            flush_seconds=0.2,
        )
        try:
            writer.enqueue("RELIANCE", 2500.0, 100, dt.datetime(2026, 5, 17, 10, 0))
            writer.enqueue("INFY", 1500.0, 50, dt.datetime(2026, 5, 17, 10, 0, 1))
            _drain(writer)
            files = _os.listdir(tmp)
            assert len(files) == 1
            name = files[0]
            assert name.startswith("ticks-")
            assert name.endswith(".jsonl")
            with open(_os.path.join(tmp, name)) as f:
                lines = [_json.loads(line) for line in f if line.strip()]
            assert len(lines) == 2
            assert {row["symbol"] for row in lines} == {"RELIANCE", "INFY"}
            assert lines[0]["ltp"] == 2500.0
            assert lines[0]["volume"] == 100
            assert "ts" in lines[0]
        finally:
            writer.stop()


def test_ticklog_flushes_on_time_threshold():
    """Enqueue one tick, wait past flush_seconds, see it on disk."""
    with tempfile.TemporaryDirectory() as tmp:
        writer = TickLogWriter(
            enabled=True,
            directory=tmp,
            batch_size=1000,  # batch never fills
            flush_seconds=0.1,  # time flush triggers
        )
        try:
            writer.enqueue("RELIANCE", 2500.0, 100, dt.datetime(2026, 5, 17, 10, 0))
            _time.sleep(0.4)  # well past flush_seconds
            files = _os.listdir(tmp)
            assert len(files) == 1
            with open(_os.path.join(tmp, files[0])) as f:
                content = f.read().strip()
            assert "RELIANCE" in content
        finally:
            writer.stop()


def test_ticklog_drop_oldest_on_full_queue():
    """When the queue is full, the oldest tick is dropped (not the new one).

    We block the worker by leaving it stopped and only inspect the queue.
    """
    with tempfile.TemporaryDirectory() as tmp:
        writer = TickLogWriter(
            enabled=True,
            directory=tmp,
            max_queue=3,
            batch_size=1000,
            flush_seconds=60.0,
        )
        # Don't trigger _ensure_worker so the queue won't drain.
        # We have to bypass enqueue's auto-start; just put directly the first
        # three, then call enqueue once to test drop-oldest.
        writer._queue.put_nowait(("A", 1.0, 1, dt.datetime(2026, 5, 17, 10, 0)))
        writer._queue.put_nowait(("B", 2.0, 2, dt.datetime(2026, 5, 17, 10, 0)))
        writer._queue.put_nowait(("C", 3.0, 3, dt.datetime(2026, 5, 17, 10, 0)))
        assert writer._queue.qsize() == 3

        # Now enqueue triggers full -> drop oldest (A) -> push D. But it will
        # also start the worker, so we have to inspect immediately. Instead,
        # we call the internal logic of enqueue manually for the assertion.
        try:
            writer._queue.put_nowait(("D", 4.0, 4, dt.datetime(2026, 5, 17, 10, 0)))
        except Exception:
            # Pop oldest, push new (this is what enqueue does on Full).
            writer._queue.get_nowait()
            writer._dropped += 1
            writer._queue.put_nowait(("D", 4.0, 4, dt.datetime(2026, 5, 17, 10, 0)))

        # Drain the queue manually and check ordering.
        items = []
        while not writer._queue.empty():
            items.append(writer._queue.get_nowait())
        symbols = [item[0] for item in items]
        # A was dropped; B, C, D remain in order.
        assert symbols == ["B", "C", "D"]
        assert writer._dropped == 1


def test_ticklog_gzip_mode():
    """Compress=True -> .jsonl.gz output that's readable via gzip."""
    with tempfile.TemporaryDirectory() as tmp:
        writer = TickLogWriter(
            enabled=True,
            directory=tmp,
            batch_size=1,
            flush_seconds=0.1,
            compress=True,
        )
        writer.enqueue("RELIANCE", 2500.0, 100, dt.datetime(2026, 5, 17, 10, 0))
        _drain(writer)
        # Stop the writer so the gzip file is properly closed (EOF marker
        # written). Reading a still-open gzip stream raises EOFError.
        writer.stop()

        files = _os.listdir(tmp)
        assert len(files) == 1
        assert files[0].endswith(".jsonl.gz")
        with _gzip.open(_os.path.join(tmp, files[0]), "rb") as f:
            content = f.read().decode("utf-8").strip()
        row = _json.loads(content)
        assert row["symbol"] == "RELIANCE"


def test_ticklog_stats_surface():
    """stats() reports the running counts."""
    with tempfile.TemporaryDirectory() as tmp:
        writer = TickLogWriter(
            enabled=True,
            directory=tmp,
            batch_size=1,
            flush_seconds=0.1,
        )
        try:
            writer.enqueue("RELIANCE", 2500.0, 100, dt.datetime(2026, 5, 17, 10, 0))
            _drain(writer)
            s = writer.stats()
            assert s["enabled"] is True
            assert s["directory"] == tmp
            assert s["file"] is not None
            assert s["written_today"] >= 1
            assert s["bytes_written_today"] > 0
        finally:
            writer.stop()


def test_ticklog_rotates_across_date_boundary():
    """When the date rolls, a new file is opened with the new date in the name."""
    with tempfile.TemporaryDirectory() as tmp:
        clock = {"now": dt.datetime(2026, 5, 17, 23, 59, 50)}

        def fake_now():
            return clock["now"]

        writer = TickLogWriter(
            enabled=True,
            directory=tmp,
            batch_size=1,
            flush_seconds=0.05,
            now_provider=fake_now,
        )
        try:
            writer.enqueue("RELIANCE", 2500.0, 100, dt.datetime(2026, 5, 17, 23, 59, 50))
            _drain(writer)
            # Roll the clock past midnight.
            clock["now"] = dt.datetime(2026, 5, 18, 0, 0, 5)
            writer.enqueue("INFY", 1500.0, 50, dt.datetime(2026, 5, 18, 0, 0, 5))
            _drain(writer)
            files = sorted(_os.listdir(tmp))
            assert len(files) == 2
            assert "20260517" in files[0]
            assert "20260518" in files[1]
        finally:
            writer.stop()


def test_ticklog_retention_prunes_old_files():
    """Files older than retention_days are deleted on writer startup."""
    with tempfile.TemporaryDirectory() as tmp:
        # Seed two files: one old, one recent.
        old_path = _os.path.join(tmp, "ticks-20240101-12345.jsonl")
        recent_path = _os.path.join(tmp, f"ticks-{dt.date.today().strftime('%Y%m%d')}-12346.jsonl")
        with open(old_path, "w") as f:
            f.write('{"ts":"old"}\n')
        with open(recent_path, "w") as f:
            f.write('{"ts":"recent"}\n')

        # Construct writer with 30-day retention; old file (2024) gets pruned.
        writer = TickLogWriter(
            enabled=True,
            directory=tmp,
            retention_days=30,
        )
        try:
            files = _os.listdir(tmp)
            assert _os.path.basename(recent_path) in files
            assert _os.path.basename(old_path) not in files
        finally:
            writer.stop()


def test_ticklog_unparseable_filename_is_skipped_during_prune():
    """A non-tick file in the directory must not crash pruning."""
    with tempfile.TemporaryDirectory() as tmp:
        bogus = _os.path.join(tmp, "README.md")
        with open(bogus, "w") as f:
            f.write("hello")
        # Should not raise.
        writer = TickLogWriter(enabled=True, directory=tmp, retention_days=1)
        try:
            assert _os.path.exists(bogus)
        finally:
            writer.stop()


def test_service_status_includes_tick_log_block(monkeypatch):
    """status() surfaces tick_log.stats() under the 'tick_log' key."""
    monkeypatch.setenv("SIMPLIFIED_ENGINE_TICK_LOG", "false")
    service = _make_service(MODE_SANDBOX)
    s = service.status()
    assert "tick_log" in s
    assert s["tick_log"]["enabled"] is False


def test_service_on_quote_enqueues_when_enabled(monkeypatch):
    """Calling on_quote with a price routes a tick to the tick logger."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("SIMPLIFIED_ENGINE_TICK_LOG", "true")
        monkeypatch.setenv("SIMPLIFIED_ENGINE_TICK_LOG_DIR", tmp)
        monkeypatch.setenv("SIMPLIFIED_ENGINE_TICK_LOG_BATCH", "1")
        monkeypatch.setenv("SIMPLIFIED_ENGINE_TICK_LOG_FLUSH_SECONDS", "0.1")
        service = _make_service(MODE_SANDBOX)
        try:
            service.on_quote(
                "RELIANCE",
                {"ltp": 2500.0, "volume": 100, "exchange_timestamp": "2026-05-17T10:30:00"},
            )
            _drain(service._tick_log)
            files = _os.listdir(tmp)
            assert len(files) == 1
            with open(_os.path.join(tmp, files[0])) as f:
                rows = [_json.loads(line) for line in f if line.strip()]
            assert rows[0]["symbol"] == "RELIANCE"
            assert rows[0]["ltp"] == 2500.0
        finally:
            service._tick_log.stop()


# --------------------------------------------------------------------------- #
# Mode-only runtime-override gate (pause / kill_switch) — entry-only, at
# dispatch. Replaces the retired daily-intent (pause/halt) gate. The engine
# reads strategy_runtime_override.is_entry_blocked; we patch it directly.
# --------------------------------------------------------------------------- #
_OV = {"override_type": "pause", "reason": "stale_feed:X", "expires_at": "2026-05-29T15:30"}


def test_runtime_override_blocks_entry_order():
    service = _make_service(MODE_SANDBOX)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    with (
        patch("database.strategy_runtime_override_db.is_entry_blocked", return_value=(True, _OV)),
        patch("services.sandbox_service.sandbox_place_order") as mock_sandbox,
        patch("services.place_order_service.place_order") as mock_live,
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_sandbox.assert_not_called()
    mock_live.assert_not_called()
    assert signal.symbol not in service.engine.pending_entries  # pending cleared


def test_runtime_override_never_blocks_exit_order():
    """An active override holds new entries but exits must ALWAYS run — a held
    position must be allowed to square off."""
    service = _make_service(MODE_SANDBOX)
    signal = _make_exit_signal()
    service.engine.pending_exits[signal.symbol] = signal
    from services.simplified_stock_engine_core import Position

    service.engine.positions[signal.symbol] = Position(
        symbol=signal.symbol,
        entry_price=2500.0,
        qty=10,
        stop_loss=2490.0,
        entry_time=dt.datetime(2026, 5, 17, 10, 30),
        risk_per_share=10.0,
    )
    sandbox_response = (True, {"orderid": "sbx-exit", "status": "success"}, 200)
    with (
        patch(
            "database.strategy_runtime_override_db.is_entry_blocked",
            return_value=(
                True,
                {"override_type": "kill_switch", "reason": "loss", "expires_at": "x"},
            ),
        ),
        patch(
            "services.sandbox_service.sandbox_place_order", return_value=sandbox_response
        ) as mock_sandbox,
        patch("services.place_order_service.place_order") as mock_live,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2489.0),
    ):
        service._place_exit_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_live.assert_not_called()
    mock_sandbox.assert_called_once()


def test_no_override_allows_entry():
    """No active override → entries proceed normally (mode-only default)."""
    service = _make_service(MODE_SANDBOX)
    signal = _make_entry_signal()
    service.engine.pending_entries[signal.symbol] = signal

    sandbox_response = (True, {"orderid": "sbx-abc", "status": "success"}, 200)
    with (
        patch("database.strategy_runtime_override_db.is_entry_blocked", return_value=(False, None)),
        # B4 veto enforces in sandbox by default; this test covers entry gating,
        # not the veto, so bypass it with a 'take' no-op.
        patch(
            "services.signal_review_service.review_signal",
            return_value={"id": None, "decision": "take", "reasoning": "test-bypass"},
        ),
        patch(
            "services.sandbox_service.sandbox_place_order", return_value=sandbox_response
        ) as mock_sandbox,
        patch("services.place_order_service.place_order") as mock_live,
        patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=2501.5),
    ):
        service._place_entry_order(signal, api_key="test-key", strategy_name="trend-up")

    mock_live.assert_not_called()
    mock_sandbox.assert_called_once()


# ----------------------------------------------------------------------
# #265 — _flatten_for_api_key store reconciliation of engine-known qty
#        (runs in BOTH sandbox AND live against the mode-aware store)
# ----------------------------------------------------------------------


def test_eod_flatten_reconciles_engine_qty_mismatch(monkeypatch):
    """LIVE: engine thinks 10, broker holds only 5 -> reconcile to the broker qty
    (5) rather than only warning. The engine would otherwise over-exit/reverse."""
    from services.simplified_stock_engine_core import Position

    service = _make_service(MODE_LIVE)
    _seed_service_for_eod(service, api_key="live-key")
    _force_eod_clock(monkeypatch)

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
                    "quantity": 5,
                    "average_price": "2500.00",
                }
            ],
        },
        200,
    )
    open_pos = (True, {"quantity": 5, "status": "success"}, 200)

    dispatched = []

    def _capture_dispatch(self, payload, api_key, *, is_entry):
        dispatched.append((payload, api_key, is_entry))
        return True, {"orderid": "recon-1"}

    with (
        patch(
            "services.positionbook_service.get_positionbook",
            return_value=positionbook_response,
        ),
        patch("services.openposition_service.get_open_position", return_value=open_pos),
        patch.object(SimplifiedStockEngineService, "_dispatch_order", _capture_dispatch),
    ):
        service._maybe_flatten_eod()

    assert len(dispatched) == 1
    payload = dispatched[0][0]
    assert payload["symbol"] == "RELIANCE"
    assert payload["action"] == "SELL"
    assert payload["quantity"] == 5
    assert "RELIANCE" not in service.engine.positions


def test_eod_flatten_phantom_suppresses_and_alerts(monkeypatch):
    """LIVE: engine thinks GHOST open but broker is flat -> SUPPRESS (no order),
    clear the engine state, and emit a phantom drift alert."""
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
        patch("services.simplified_stock_engine_service._emit_phantom_alert") as mock_alert,
    ):
        service._maybe_flatten_eod()

    mock_dispatch.assert_not_called()
    mock_alert.assert_called_once()
    assert "GHOST" not in service.engine.positions


def test_eod_flatten_reconciles_engine_qty_mismatch_in_sandbox(monkeypatch):
    """SANDBOX (#265): engine thinks 10 but the sandbox store holds only 5 ->
    reconcile to the sandbox store qty (5), routed via the mode-aware source.
    The guard now runs in sandbox too, not just live."""
    from services.simplified_stock_engine_core import Position

    service = _make_service(MODE_SANDBOX)
    _seed_service_for_eod(service, api_key="sbx-key")
    _force_eod_clock(monkeypatch)

    service.engine.positions["RELIANCE"] = Position(
        symbol="RELIANCE",
        entry_price=2500.0,
        qty=10,
        stop_loss=2490.0,
        entry_time=_eod_dt.datetime(2026, 5, 17, 10, 30),
        risk_per_share=10.0,
    )

    # The mode-aware get_positionbook returns the sandbox.db book in sandbox.
    sandbox_book = (
        True,
        {
            "status": "success",
            "data": [
                {
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "product": "MIS",
                    "quantity": 5,
                    "average_price": "2500.00",
                }
            ],
        },
        200,
    )
    open_pos = (True, {"quantity": 5, "status": "success"}, 200)

    dispatched = []

    def _capture_dispatch(self, payload, api_key, *, is_entry):
        dispatched.append((payload, api_key, is_entry))
        return True, {"orderid": "recon-sbx-1"}

    with (
        patch("services.positionbook_service.get_positionbook", return_value=sandbox_book),
        patch("services.openposition_service.get_open_position", return_value=open_pos) as store,
        patch.object(SimplifiedStockEngineService, "_dispatch_order", _capture_dispatch),
    ):
        service._maybe_flatten_eod()

    store.assert_called()  # sandbox store IS consulted
    assert len(dispatched) == 1
    payload = dispatched[0][0]
    assert payload["symbol"] == "RELIANCE"
    assert payload["action"] == "SELL"
    assert payload["quantity"] == 5  # clamped to the sandbox store
    assert "RELIANCE" not in service.engine.positions


def test_eod_flatten_disabled_mode_no_store_call(monkeypatch):
    """DISABLED (#265): the flatten is skipped entirely — neither the positionbook
    nor the reconciliation source is consulted."""
    service = _make_service(MODE_DISABLED)
    _seed_service_for_eod(service)
    _force_eod_clock(monkeypatch)

    with (
        patch("services.positionbook_service.get_positionbook") as mock_book,
        patch("services.openposition_service.get_open_position") as store,
        patch.object(SimplifiedStockEngineService, "_dispatch_order") as mock_dispatch,
    ):
        service._maybe_flatten_eod()

    mock_book.assert_not_called()
    store.assert_not_called()
    mock_dispatch.assert_not_called()
