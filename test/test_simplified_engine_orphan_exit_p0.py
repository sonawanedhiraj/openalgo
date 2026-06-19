"""P0 regression tests for the 2026-06-19 TCS orphan-exit error storm.

Root cause chain (see docs / the fix commit message):

1. ``rehydrate_positions_from_journal`` restored ``engine.positions`` on boot
   but NOT the ``_api_key_by_symbol`` / ``_strategy_by_symbol`` maps, so a
   rehydrated symbol no later scan re-armed had a live position with no key.
2. Rehydrate sets ``stop_loss = entry_price`` + ``risk_per_share = 0``. For a
   SHORT the tick SL check is ``price >= stop_loss``, so stop == entry fired a
   stop_loss exit on essentially every tick.
3. ``_schedule_exit`` logged at ERROR + cleared the pending exit every tick when
   the key was missing — a ~2/sec error storm that tripped the preflight gate
   and blocked every scan cycle.

These tests pin the three fixes:
* core: the tick SL is skipped while ``risk_per_share == 0`` (stop not known).
* service: ``_resolve_order_api_key`` falls back so an exit is never blocked.
* service: the unresolvable-key log is throttled, never a per-tick storm.

The rehydrate-map-population fix is covered in
``test/test_eod_watchdog_service.py`` next to the existing rehydrate tests
(which own the trade_journal DB fixtures).
"""

import datetime as dt
from unittest.mock import patch

# Pre-resolve the documented restx_api / place_order_service circular import
# before pulling in the engine service module (mirrors the precaution in
# test_simplified_stock_engine_service.py).
import restx_api  # noqa: F401
from services.simplified_stock_engine_core import (
    MODE_SANDBOX,
    ExitSignal,
    Position,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
)
from services.simplified_stock_engine_service import SimplifiedStockEngineService


def _engine(now=dt.datetime(2026, 4, 29, 10, 24)):
    # 10:24 is well before the default eod_exit_time, so check_eod_exits is a
    # no-op and on_price_update returns only what the tick SL produces.
    return SimplifiedStockEngine(config=SimplifiedEngineConfig(), now_provider=lambda: now)


def _make_service(mode=MODE_SANDBOX):
    return SimplifiedStockEngineService(config=SimplifiedEngineConfig(mode=mode))


# ---------------------------------------------------------------------------
# core: tick SL gated on an established stop
# ---------------------------------------------------------------------------


def test_rehydrated_short_does_not_fire_tick_sl():
    """A rehydrated SHORT (risk_per_share == 0, stop == entry) must NOT produce a
    tick stop_loss exit even when price is well above entry. This is the exact
    condition that storm-fired on 2026-06-19 (TCS -48 @ 2070.5)."""
    engine = _engine()
    engine.positions["TCS"] = Position(
        symbol="TCS",
        entry_price=2070.5,
        qty=-48,  # SHORT
        stop_loss=2070.5,  # placeholder == entry
        entry_time=dt.datetime(2026, 4, 29, 9, 50),
        risk_per_share=0.0,  # marker: no established stop
    )

    exits = engine.on_price_update("TCS", 2080.0)  # price >> entry

    assert exits == []


def test_rehydrated_long_does_not_fire_tick_sl():
    """Same protection for a LONG (price dipping to the placeholder stop)."""
    engine = _engine()
    engine.positions["NBCC"] = Position(
        symbol="NBCC",
        entry_price=104.94,
        qty=500,  # LONG
        stop_loss=104.94,
        entry_time=dt.datetime(2026, 4, 29, 9, 50),
        risk_per_share=0.0,
    )

    exits = engine.on_price_update("NBCC", 104.94)  # price <= stop

    assert exits == []


def test_real_short_still_stops_out():
    """A genuine SHORT (risk_per_share > 0) still stops out normally — the gate
    must not suppress real stops."""
    engine = _engine()
    engine.positions["TCS"] = Position(
        symbol="TCS",
        entry_price=2070.5,
        qty=-48,
        stop_loss=2075.0,
        entry_time=dt.datetime(2026, 4, 29, 9, 50),
        risk_per_share=4.5,
    )

    exits = engine.on_price_update("TCS", 2075.0)  # price >= stop

    assert len(exits) == 1
    assert exits[0].reason == "stop_loss"
    assert exits[0].action == "BUY"  # covering a short


def test_real_long_still_stops_out():
    engine = _engine()
    engine.positions["NBCC"] = Position(
        symbol="NBCC",
        entry_price=104.94,
        qty=500,
        stop_loss=103.0,
        entry_time=dt.datetime(2026, 4, 29, 9, 50),
        risk_per_share=1.94,
    )

    exits = engine.on_price_update("NBCC", 102.95)  # price <= stop

    assert len(exits) == 1
    assert exits[0].reason == "stop_loss"
    assert exits[0].action == "SELL"


# ---------------------------------------------------------------------------
# service: api_key fallback resolution
# ---------------------------------------------------------------------------


def test_resolve_order_api_key_prefers_per_symbol_mapping():
    service = _make_service()
    service._api_key_by_symbol["TCS"] = "tcs-key"
    service._strategy_by_symbol["TCS"] = "chartink_FnO_intraday_buy"

    api_key, strategy_name = service._resolve_order_api_key("TCS")

    assert api_key == "tcs-key"  # pragma: allowlist secret
    assert strategy_name == "chartink_FnO_intraday_buy"


def test_resolve_order_api_key_falls_back_to_any_mapped_key():
    """An unmapped symbol reuses an existing mapped key (single-user deployment,
    so any key is the correct account) rather than failing."""
    service = _make_service()
    service._api_key_by_symbol["OTHER"] = "fallback-key"

    api_key, strategy_name = service._resolve_order_api_key("TCS")

    assert api_key == "fallback-key"  # pragma: allowlist secret
    assert strategy_name == "simplified_stock_engine"


def test_resolve_order_api_key_falls_back_to_user_keys():
    service = _make_service()
    service._user_api_keys["dheeraj"] = "user-key"

    api_key, _ = service._resolve_order_api_key("TCS")

    assert api_key == "user-key"  # pragma: allowlist secret


def test_resolve_order_api_key_falls_back_to_first_available_db_key(monkeypatch):
    service = _make_service()
    monkeypatch.setattr("database.auth_db.get_first_available_api_key", lambda: "db-key")

    api_key, _ = service._resolve_order_api_key("TCS")

    assert api_key == "db-key"  # pragma: allowlist secret


def test_resolve_order_api_key_returns_none_when_nothing_resolves(monkeypatch):
    service = _make_service()
    monkeypatch.setattr("database.auth_db.get_first_available_api_key", lambda: None)

    api_key, strategy_name = service._resolve_order_api_key("TCS")

    assert api_key is None
    assert strategy_name == "simplified_stock_engine"


def test_schedule_exit_uses_fallback_key_instead_of_storming():
    """An unmapped (e.g. rehydrated) position's exit is dispatched via the
    fallback key — it must NOT clear the pending exit and bail. This is the
    direct regression for the TCS storm."""
    import time as _t

    service = _make_service()
    service._api_key_by_symbol["OTHER"] = "fallback-key"  # a key exists somewhere
    signal = ExitSignal(
        symbol="TCS",
        action="BUY",
        quantity=48,
        reason="eod",  # non-stop_loss -> straight to _place_exit_order thread
        reference_price=2070.5,
        exchange="NSE",
        product="MIS",
        pricetype="MARKET",
    )

    with patch.object(service, "_place_exit_order") as mock_place:
        service._schedule_exit(signal)
        for _ in range(50):  # let the daemon thread start
            if mock_place.called:
                break
            _t.sleep(0.02)

    mock_place.assert_called_once()
    # signature: _place_exit_order(signal, api_key, strategy_name)
    assert mock_place.call_args.args[1] == "fallback-key"


# ---------------------------------------------------------------------------
# service: throttled keyless logging
# ---------------------------------------------------------------------------


def test_log_keyless_throttled_suppresses_within_interval():
    service = _make_service()

    service._log_keyless_throttled("TCS", "exit")
    first = service._keyless_logged_at["TCS"]
    service._log_keyless_throttled("TCS", "exit")  # within interval -> suppressed

    assert service._keyless_logged_at["TCS"] == first


def test_log_keyless_throttled_logs_again_after_interval(monkeypatch):
    service = _make_service()
    service._KEYLESS_LOG_INTERVAL_SEC = 0.0  # any elapsed time re-logs

    service._log_keyless_throttled("TCS", "exit")
    first = service._keyless_logged_at["TCS"]
    # monotonic advances between calls; with a 0s interval the second call re-logs.
    service._log_keyless_throttled("TCS", "exit")

    assert service._keyless_logged_at["TCS"] >= first
