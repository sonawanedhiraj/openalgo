"""Tests for the same-symbol same-day stop-out re-entry block.

The engine already exposes a candle-count cooldown (cooldown_candles). The
same-day block is layered on top: when a stop-loss fires, the symbol is
blocked from re-entering for the rest of the trading day (until
same_day_block_end_time, default 15:30). Operators can opt out by setting
RISK_SAME_DAY_STOPOUT_BLOCK=false in .env.

Each test drives _is_entry_window_open directly with a wall-clock provider
so the test is fully deterministic.
"""

import datetime as dt

from services.simplified_stock_engine_core import (
    Candle,
    Position,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
)


class FixedClock:
    """Mutable wall-clock for tests."""

    def __init__(self, now: dt.datetime):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def _make_engine(now: dt.datetime, *, block_enabled: bool = True) -> SimplifiedStockEngine:
    config = SimplifiedEngineConfig(
        no_new_openings_time=dt.time(15, 10),
        cooldown_candles=0,  # isolate same-day block from candle cooldown
        same_day_stopout_block=block_enabled,
        same_day_block_end_time=dt.time(15, 30),
    )
    engine = SimplifiedStockEngine(config=config, now_provider=FixedClock(now))
    engine.activate_buy_symbol("RELIANCE")
    return engine


def _entry_candle(ts: dt.datetime, elapsed: float = 0.8) -> Candle:
    return Candle(
        ts=ts,
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.5,
        volume=600,
        elapsed_pct=elapsed,
    )


def _seed_open_position(
    engine: SimplifiedStockEngine, symbol: str, entry_time: dt.datetime
) -> None:
    """Plant an open Position so confirm_exit has something to close."""
    engine.positions[symbol] = Position(
        symbol=symbol,
        entry_price=100.0,
        qty=10,
        stop_loss=99.0,
        entry_time=entry_time,
        risk_per_share=1.0,
    )


# ---------------------------------------------------------------------------
# Case 1 — fresh symbol, no prior stop-out → entry window open.
# ---------------------------------------------------------------------------


def test_fresh_symbol_is_not_blocked():
    now = dt.datetime(2026, 5, 29, 10, 0)
    engine = _make_engine(now)
    candle = _entry_candle(dt.datetime(2026, 5, 29, 10, 0))

    assert engine._is_entry_window_open("RELIANCE", candle) is True


# ---------------------------------------------------------------------------
# Case 2 — symbol exited for a non-stop reason (target/eod) → no block.
# ---------------------------------------------------------------------------


def test_non_stop_exit_does_not_set_block():
    now = dt.datetime(2026, 5, 29, 11, 30)
    engine = _make_engine(now)

    _seed_open_position(engine, "RELIANCE", dt.datetime(2026, 5, 29, 9, 45))
    engine.confirm_exit("RELIANCE", exit_price=102.0, reason="eod")

    assert "RELIANCE" not in engine._same_day_blocked_until

    # Entry attempt at 14:00 the same day must still be allowed.
    engine.now_provider.now = dt.datetime(2026, 5, 29, 14, 0)
    candle = _entry_candle(dt.datetime(2026, 5, 29, 14, 0))
    assert engine._is_entry_window_open("RELIANCE", candle) is True


# ---------------------------------------------------------------------------
# Case 3 — stop-loss at 11:30 → blocked until 15:30; next entry at 14:00 refused.
# ---------------------------------------------------------------------------


def test_stop_loss_blocks_re_entry_for_rest_of_day():
    stop_time = dt.datetime(2026, 5, 29, 11, 30)
    engine = _make_engine(stop_time)

    _seed_open_position(engine, "RELIANCE", dt.datetime(2026, 5, 29, 9, 45))
    engine.confirm_exit("RELIANCE", exit_price=99.0, reason="stop_loss")

    blocked_until = engine._same_day_blocked_until.get("RELIANCE")
    assert blocked_until == dt.datetime(2026, 5, 29, 15, 30)

    # Attempt re-entry at 14:00 same day → must be refused.
    engine.now_provider.now = dt.datetime(2026, 5, 29, 14, 0)
    candle = _entry_candle(dt.datetime(2026, 5, 29, 14, 0))
    assert engine._is_entry_window_open("RELIANCE", candle) is False


# ---------------------------------------------------------------------------
# Case 4 — after 15:30 (treated as a fresh day rollover here for clarity),
# the block clears and re-entry proceeds normally.
# ---------------------------------------------------------------------------


def test_block_expires_after_session_end():
    stop_time = dt.datetime(2026, 5, 29, 11, 30)
    engine = _make_engine(stop_time)

    _seed_open_position(engine, "RELIANCE", dt.datetime(2026, 5, 29, 9, 45))
    engine.confirm_exit("RELIANCE", exit_price=99.0, reason="stop_loss")

    # Next trading morning. _reset_trade_day_if_needed clears the dict; we
    # simulate that by advancing the clock and triggering the reset.
    engine.now_provider.now = dt.datetime(2026, 5, 30, 9, 30)
    engine._reset_trade_day_if_needed()

    assert "RELIANCE" not in engine._same_day_blocked_until
    candle = _entry_candle(dt.datetime(2026, 5, 30, 9, 30))
    assert engine._is_entry_window_open("RELIANCE", candle) is True


# ---------------------------------------------------------------------------
# Case 5 — RISK_SAME_DAY_STOPOUT_BLOCK=false → block is ignored.
# ---------------------------------------------------------------------------


def test_opt_out_ignores_block():
    stop_time = dt.datetime(2026, 5, 29, 11, 30)
    engine = _make_engine(stop_time, block_enabled=False)

    _seed_open_position(engine, "RELIANCE", dt.datetime(2026, 5, 29, 9, 45))
    engine.confirm_exit("RELIANCE", exit_price=99.0, reason="stop_loss")

    # When the gate is off, confirm_exit doesn't even populate the block dict.
    assert "RELIANCE" not in engine._same_day_blocked_until

    engine.now_provider.now = dt.datetime(2026, 5, 29, 14, 0)
    candle = _entry_candle(dt.datetime(2026, 5, 29, 14, 0))
    assert engine._is_entry_window_open("RELIANCE", candle) is True


# ---------------------------------------------------------------------------
# Service wiring: env var resolves into the config field.
# ---------------------------------------------------------------------------


def test_env_var_resolves_default_true(monkeypatch):
    from services.simplified_stock_engine_service import config_from_env

    monkeypatch.delenv("RISK_SAME_DAY_STOPOUT_BLOCK", raising=False)
    cfg = config_from_env()
    assert cfg.same_day_stopout_block is True


def test_env_var_can_disable(monkeypatch):
    from services.simplified_stock_engine_service import config_from_env

    monkeypatch.setenv("RISK_SAME_DAY_STOPOUT_BLOCK", "false")
    cfg = config_from_env()
    assert cfg.same_day_stopout_block is False
