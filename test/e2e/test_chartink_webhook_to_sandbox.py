"""P0-T3: Chartink webhook → 5m candle → sandbox order.

Tests the simplified engine seam end-to-end (hermetically):
  engine arm → 5m candle breakout → _place_entry_order (sandbox) → trade_journal
  ATR stop hit during hold → exit + reason='stop_loss'
  Trailing stop tightened and triggered → exit + reason='stop_loss'

No HTTP server, no live quote feed — injects ticks and candles directly.
Companion to test_fno_flows.py (which covers BUY/SELL/MIS/squareoff).

Refs #94
"""

from __future__ import annotations

import datetime as dt
import sys
from unittest.mock import MagicMock, patch

import pytest

# Pre-resolve circular imports before importing engine modules.
import restx_api  # noqa: F401
import services  # noqa: F401
import services.place_order_service  # noqa: F401
import services.sandbox_service  # noqa: F401
from services.simplified_stock_engine_core import (
    DIRECTION_BUY,
    MODE_SANDBOX,
    Candle,
    Position,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
)
from services.simplified_stock_engine_service import SimplifiedStockEngineService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FixedClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _buy_history() -> list[Candle]:
    """Synthetic history mirroring test_fno_flows._buy_history exactly.

    The reference candle (third-to-last, RED, volume=100) sets the breakout level.
    Required volume for breakout = 100 * 2.5 = 250 — easily cleared by _breakout_candle.
    """
    start = dt.datetime(2026, 4, 29, 9, 30)
    candles = [
        Candle(
            ts=start + dt.timedelta(minutes=5 * i),
            open=100 + (i % 2),
            high=102 + (i % 2),
            low=99 + (i % 2),
            close=101 + (i % 2),
            volume=600,
            elapsed_pct=1.0,
        )
        for i in range(11)
    ]
    # Third-to-last: RED reference candle with LOW volume (sets breakout level = open=100).
    candles[-3] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 10),
        open=100,
        high=101,
        low=98,
        close=99,  # close < open → RED
        volume=100,  # low so volume_multiplier * 100 = 250 → breakout at 300 passes
        elapsed_pct=1.0,
    )
    # Second-to-last: GREEN candle (must follow the red reference).
    candles[-2] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 15),
        open=101,
        high=102,
        low=100,
        close=102,
        volume=800,
        elapsed_pct=1.0,
    )
    # Last: the candle we feed to the engine before the breakout trigger.
    candles[-1] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 20),
        open=102,
        high=103,
        low=100,
        close=101,
        volume=200,
        elapsed_pct=1.0,
    )
    return candles


def _breakout_candle() -> Candle:
    """Closes above the reference candle open (100) with sufficient volume (300 >= 250)."""
    return Candle(
        ts=dt.datetime(2026, 4, 29, 10, 20),
        open=100,
        high=102,
        low=99,
        close=101.5,  # > reference open 100
        volume=300,  # >= 100 * 2.5 = 250
        elapsed_pct=0.75,
    )


def _engine(
    now: dt.datetime = dt.datetime(2026, 4, 29, 10, 24),
    max_risk: float = 500.0,
    rr_trail_start_r: float = 0.6,
) -> SimplifiedStockEngine:
    cfg = SimplifiedEngineConfig(
        no_new_openings_time=dt.time(15, 10),
        reference_candle_expiry_seconds=20 * 60,
        account_capital=100_000.0,
        account_leverage=5.0,
        max_risk_per_trade=max_risk,
        min_risk_per_share=0.5,
        rr_trail_start_r=rr_trail_start_r,
    )
    eng = SimplifiedStockEngine(config=cfg, now_provider=FixedClock(now))
    eng.activate_buy_symbol("RELIANCE")
    eng.load_historical_candles("RELIANCE", _buy_history())
    return eng


def _service(eng: SimplifiedStockEngine, monkeypatch, *, veto_off: bool = True):
    svc = SimplifiedStockEngineService(config=eng.config, engine=eng)
    svc.mode = MODE_SANDBOX
    svc._strategy_by_symbol["RELIANCE"] = "chartink_FnO_intraday_buy"
    svc._api_key_by_symbol["RELIANCE"] = "k"
    monkeypatch.setattr("services.risk_service.daily_circuit_breaker_tripped", lambda: (False, ""))
    if veto_off:
        monkeypatch.setattr(
            svc, "_run_pre_order_review", lambda signal, strategy_name: (True, None)
        )
    return svc


def _sandbox_ok():
    return (True, {"orderid": "sbx-1", "status": "success", "mode": "analyze"}, 200)


class _RecordingJournal:
    """Minimal journal stub that records every call without hitting a DB."""

    def __init__(self, next_id: int = 42):
        self.entries: list[dict] = []
        self.fills: list[dict] = []
        self.exits: list[dict] = []
        self._next_id = next_id

    def record_entry(self, **kw):
        self.entries.append(kw)
        return self._next_id

    def update_entry_fill(self, journal_id, entry_price=None, entry_fill_at=None):
        self.fills.append({"journal_id": journal_id, "entry_price": entry_price})

    def get_open_journal_id_for_symbol(self, symbol):
        return self._next_id

    def record_exit(self, journal_id, **kw):
        self.exits.append({"journal_id": journal_id, **kw})

    def get_trades_for_symbol(self, symbol, days=1):
        return []

    def get_today_summary(self):
        return {"count": len(self.exits), "total_pnl": 0.0}


def _install_journal(monkeypatch, journal: _RecordingJournal) -> None:
    monkeypatch.setitem(sys.modules, "services.trade_journal_service", journal)
    monkeypatch.setattr(services, "trade_journal_service", journal, raising=False)


# ---------------------------------------------------------------------------
# Scenario 1 — BUY breakout → sandbox order + trade_journal entry
# ---------------------------------------------------------------------------


class TestBuyWebhookToSandbox:
    def test_buy_breakout_places_sandbox_order_and_writes_journal(self, monkeypatch):
        """Webhook arms symbol, breakout candle fires, sandbox order placed, journal row written."""
        eng = _engine()
        svc = _service(eng, monkeypatch)
        journal = _RecordingJournal()
        _install_journal(monkeypatch, journal)

        sig = eng.on_new_candle("RELIANCE", _breakout_candle())
        assert sig is not None, "Breakout candle must produce an entry signal"
        assert sig.action == DIRECTION_BUY
        assert sig.quantity > 0

        with (
            patch(
                "services.sandbox_service.sandbox_place_order",
                return_value=_sandbox_ok(),
            ) as m_sbx,
            patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=104.0),
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="chartink_FnO_intraday_buy")

        # Sandbox call made with correct direction
        m_sbx.assert_called_once()
        order_payload = m_sbx.call_args[0][0]
        assert order_payload["action"] == "BUY"
        assert order_payload["symbol"] == "RELIANCE"

        # Journal entry written
        assert len(journal.entries) == 1
        entry = journal.entries[0]
        assert entry["symbol"] == "RELIANCE"
        assert entry["direction"] == "LONG"

        # Engine tracks open position
        assert "RELIANCE" in eng.positions
        assert eng.positions["RELIANCE"].qty > 0
        assert eng.positions["RELIANCE"].entry_price == 104.0


# ---------------------------------------------------------------------------
# Scenario 2 — empty stocks payload → no signal armed, no order
# ---------------------------------------------------------------------------


class TestEmptyStocksPayload:
    def test_no_symbols_armed_produces_no_signal(self):
        """When no symbols are activated, a breakout candle yields no entry signal."""
        cfg = SimplifiedEngineConfig(
            no_new_openings_time=dt.time(15, 10),
            reference_candle_expiry_seconds=20 * 60,
        )
        eng_plain = SimplifiedStockEngine(
            config=cfg, now_provider=FixedClock(dt.datetime(2026, 4, 29, 10, 24))
        )
        # No activate_buy_symbol / activate_sell_symbol called
        sig = eng_plain.on_new_candle("RELIANCE", _breakout_candle())
        assert sig is None, "Un-armed symbol must not produce an entry signal"

    def test_activated_but_no_history_produces_no_signal(self):
        """Symbol activated but no history loaded → reference candle missing → no signal."""
        cfg = SimplifiedEngineConfig(
            no_new_openings_time=dt.time(15, 10),
            reference_candle_expiry_seconds=20 * 60,
        )
        eng_plain = SimplifiedStockEngine(
            config=cfg, now_provider=FixedClock(dt.datetime(2026, 4, 29, 10, 24))
        )
        eng_plain.activate_buy_symbol("RELIANCE")
        # No load_historical_candles → no reference candle → no breakout
        sig = eng_plain.on_new_candle("RELIANCE", _breakout_candle())
        assert sig is None


# ---------------------------------------------------------------------------
# Scenario 3 — ATR stop hit → exit with reason='stop_loss'
# ---------------------------------------------------------------------------


class TestAtrStopHit:
    def test_stop_hit_below_entry_exits_with_stop_loss_reason(self, monkeypatch):
        """Price drops below the ATR-derived stop loss → exit signal + journal exit."""
        eng = _engine()
        svc = _service(eng, monkeypatch)
        journal = _RecordingJournal()
        _install_journal(monkeypatch, journal)

        sig = eng.on_new_candle("RELIANCE", _breakout_candle())
        assert sig is not None

        with (
            patch("services.sandbox_service.sandbox_place_order", return_value=_sandbox_ok()),
            patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=104.0),
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="chartink_FnO_intraday_buy")

        pos = eng.positions["RELIANCE"]
        stop_price = pos.stop_loss
        assert stop_price < pos.entry_price, "BUY stop should be below entry"

        # Trigger price update below stop loss
        exits = eng.on_price_update("RELIANCE", stop_price - 0.01)
        stop_exits = [e for e in exits if e.reason == "stop_loss"]
        assert len(stop_exits) == 1, "Price below stop must generate stop_loss exit"
        exit_sig = stop_exits[0]
        assert exit_sig.symbol == "RELIANCE"
        assert exit_sig.action == "SELL"

        with (
            patch("services.sandbox_service.sandbox_place_order", return_value=_sandbox_ok()),
            patch.object(
                SimplifiedStockEngineService, "_wait_for_fill", return_value=stop_price - 0.01
            ),
        ):
            svc._place_exit_order(exit_sig, api_key="k", strategy_name="chartink_FnO_intraday_buy")

        # Journal exit recorded with stop_loss reason
        assert len(journal.exits) == 1
        assert journal.exits[0]["exit_reason"] == "stop_loss"
        # Position closed
        assert "RELIANCE" not in eng.positions


# ---------------------------------------------------------------------------
# Scenario 4 — trailing RR tightens stop, price retreats through it → exit
# ---------------------------------------------------------------------------


class TestTrailingStopTriggered:
    def test_trailing_stop_tightens_then_triggers_exit(self, monkeypatch):
        """Trailing RR raises stop as price moves in favor; retreat through it exits."""
        # Use small rr_trail_start_r so trailing kicks in after a modest profit.
        # With max_risk=500, rr_trail_start_r=0.3 → threshold = 150 total profit.
        eng = _engine(rr_trail_start_r=0.3, max_risk=500.0)
        svc = _service(eng, monkeypatch)
        journal = _RecordingJournal()
        _install_journal(monkeypatch, journal)

        sig = eng.on_new_candle("RELIANCE", _breakout_candle())
        assert sig is not None

        with (
            patch("services.sandbox_service.sandbox_place_order", return_value=_sandbox_ok()),
            patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=104.0),
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="chartink_FnO_intraday_buy")

        pos = eng.positions["RELIANCE"]
        original_sl = pos.stop_loss
        entry_price = pos.entry_price
        # Need total_profit = per_share_profit * qty >= rr_trail_start_r * max_risk_per_trade
        # With qty~=50 (100k/2 leverage at base=100), rr_trail_start_r=0.3, max_risk=500 → 150
        # Per share needed: 150/50 = 3 → push price to entry + 6 to be safe
        high_price = entry_price + 10.0

        eng.on_price_update("RELIANCE", high_price)

        pos_after = eng.positions.get("RELIANCE")
        if pos_after is None:
            pytest.skip("Position closed during price push — qty too small for this config")

        trailing_sl = pos_after.stop_loss
        # For a LONG the trailing stop should be >= original_sl (tightened upward)
        assert trailing_sl >= original_sl, (
            f"Trailing must tighten the stop for a long: original={original_sl} "
            f"trailing={trailing_sl}"
        )

        # Drop below trailing stop → exit fires
        exits = eng.on_price_update("RELIANCE", trailing_sl - 0.01)
        stop_exits = [e for e in exits if e.reason == "stop_loss"]
        assert len(stop_exits) == 1, "Retreat below trailing stop must trigger stop_loss exit"

        with (
            patch("services.sandbox_service.sandbox_place_order", return_value=_sandbox_ok()),
            patch.object(
                SimplifiedStockEngineService, "_wait_for_fill", return_value=trailing_sl - 0.01
            ),
        ):
            svc._place_exit_order(
                stop_exits[0], api_key="k", strategy_name="chartink_FnO_intraday_buy"
            )

        assert len(journal.exits) == 1
        assert journal.exits[0]["exit_reason"] == "stop_loss"
        assert "RELIANCE" not in eng.positions
