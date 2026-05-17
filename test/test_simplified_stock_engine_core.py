import datetime as dt

from services.simplified_stock_engine_core import (
    Candle,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
)


class FixedClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _history():
    start = dt.datetime(2026, 4, 29, 9, 30)
    candles = []
    for i in range(11):
        ts = start + dt.timedelta(minutes=5 * i)
        candles.append(
            Candle(
                ts=ts,
                open=100 + (i % 2),
                high=102 + (i % 2),
                low=99 + (i % 2),
                close=101 + (i % 2),
                volume=600,
                elapsed_pct=1.0,
            )
        )

    candles[-3] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 10),
        open=100,
        high=101,
        low=98,
        close=99,
        volume=100,
        elapsed_pct=1.0,
    )
    candles[-2] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 15),
        open=101,
        high=102,
        low=100,
        close=102,
        volume=800,
        elapsed_pct=1.0,
    )
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


def _engine(now=dt.datetime(2026, 4, 29, 10, 24)):
    config = SimplifiedEngineConfig(
        no_new_openings_time=dt.time(15, 10),
        reference_candle_expiry_seconds=20 * 60,
    )
    engine = SimplifiedStockEngine(config=config, now_provider=FixedClock(now))
    engine.activate_buy_symbol("RELIANCE")
    engine.load_historical_candles("RELIANCE", _history())
    return engine


def test_buy_signal_after_red_open_breakout_with_volume_and_atr():
    engine = _engine()

    signal = engine.on_new_candle(
        "RELIANCE",
        Candle(
            ts=dt.datetime(2026, 4, 29, 10, 20),
            open=100,
            high=102,
            low=99,
            close=101.5,
            volume=300,
            elapsed_pct=0.75,
        ),
    )

    assert signal is not None
    assert signal.action == "BUY"
    assert signal.quantity > 0
    assert signal.symbol == "RELIANCE"
    assert "RELIANCE" in engine.pending_entries


def test_buy_signal_rejected_when_candle_is_not_progressed_enough():
    engine = _engine()

    signal = engine.on_new_candle(
        "RELIANCE",
        Candle(
            ts=dt.datetime(2026, 4, 29, 10, 20),
            open=100,
            high=102,
            low=99,
            close=101.5,
            volume=300,
            elapsed_pct=0.50,
        ),
    )

    assert signal is None
    assert "RELIANCE" not in engine.pending_entries


def test_buy_signal_rejected_when_volume_is_below_reference_multiplier():
    engine = _engine()

    signal = engine.on_new_candle(
        "RELIANCE",
        Candle(
            ts=dt.datetime(2026, 4, 29, 10, 20),
            open=100,
            high=102,
            low=99,
            close=101.5,
            volume=249,
            elapsed_pct=0.75,
        ),
    )

    assert signal is None


def test_no_new_entry_after_cutoff():
    engine = _engine(now=dt.datetime(2026, 4, 29, 15, 11))

    signal = engine.on_new_candle(
        "RELIANCE",
        Candle(
            ts=dt.datetime(2026, 4, 29, 15, 10),
            open=100,
            high=102,
            low=99,
            close=101.5,
            volume=300,
            elapsed_pct=0.75,
        ),
    )

    assert signal is None


def test_confirm_entry_and_stop_loss_exit_signal():
    engine = _engine()
    signal = engine.on_new_candle(
        "RELIANCE",
        Candle(
            ts=dt.datetime(2026, 4, 29, 10, 20),
            open=100,
            high=102,
            low=99,
            close=101.5,
            volume=300,
            elapsed_pct=0.75,
        ),
    )
    assert signal is not None

    position = engine.confirm_entry("RELIANCE", executed_price=101.5)
    assert position is not None
    assert position.qty == signal.quantity

    exits = engine.on_price_update("RELIANCE", position.stop_loss - 0.05)
    assert len(exits) == 1
    assert exits[0].action == "SELL"
    assert exits[0].reason == "stop_loss"


# ---------------------------------------------------------------------------
# SELL / short side
# ---------------------------------------------------------------------------


def _sell_history():
    """History where the last bucket is the lowest-volume green candle.

    The reference picker chooses the lowest-volume green/doji candle, so the
    most recent bucket in this fixture is the green reference for the SELL
    flow (open=99, close=100, vol=200).
    """
    start = dt.datetime(2026, 4, 29, 9, 30)
    candles = []
    for i in range(11):
        ts = start + dt.timedelta(minutes=5 * i)
        candles.append(
            Candle(
                ts=ts,
                open=100 + (i % 2),
                high=102 + (i % 2),
                low=99 + (i % 2),
                close=101 + (i % 2),
                volume=600,
                elapsed_pct=1.0,
            )
        )

    candles[-3] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 10),
        open=100,
        high=103,
        low=99,
        close=102,
        volume=900,
        elapsed_pct=1.0,
    )
    candles[-2] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 15),
        open=101,
        high=102,
        low=98,
        close=99,
        volume=800,
        elapsed_pct=1.0,
    )
    candles[-1] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 20),
        open=99,
        high=101,
        low=98,
        close=100,
        volume=200,
        elapsed_pct=1.0,
    )
    return candles


def _sell_engine(now=dt.datetime(2026, 4, 29, 10, 24)):
    config = SimplifiedEngineConfig(
        no_new_openings_time=dt.time(15, 10),
        reference_candle_expiry_seconds=20 * 60,
    )
    engine = SimplifiedStockEngine(config=config, now_provider=FixedClock(now))
    engine.activate_sell_symbol("RELIANCE")
    engine.load_historical_candles("RELIANCE", _sell_history())
    return engine


def test_activate_sell_clears_red_reference():
    engine = _engine()
    assert "RELIANCE" in engine.red_candles
    engine.activate_sell_symbol("RELIANCE")
    assert "RELIANCE" not in engine.red_candles


def test_sell_signal_after_green_open_breakdown_with_volume_and_atr():
    engine = _sell_engine()
    # Reference green candle: open=99, vol=200 -> required vol=500 with mult=2.5.
    # Live candle must close BELOW 99 to qualify as a SELL breakout.
    signal = engine.on_new_candle(
        "RELIANCE",
        Candle(
            ts=dt.datetime(2026, 4, 29, 10, 20),
            open=99,
            high=99.5,
            low=96,
            close=97.5,
            volume=600,
            elapsed_pct=0.75,
        ),
    )

    assert signal is not None
    assert signal.action == "SELL"
    assert signal.quantity > 0
    assert signal.symbol == "RELIANCE"
    assert "RELIANCE" in engine.pending_entries


def test_sell_signal_rejected_when_close_above_green_open():
    engine = _sell_engine()
    signal = engine.on_new_candle(
        "RELIANCE",
        Candle(
            ts=dt.datetime(2026, 4, 29, 10, 20),
            open=99,
            high=100.5,
            low=98.5,
            close=99.5,  # not below green_open=99
            volume=600,
            elapsed_pct=0.75,
        ),
    )

    assert signal is None


def test_short_position_stop_loss_triggers_buy_to_cover():
    engine = _sell_engine()
    signal = engine.on_new_candle(
        "RELIANCE",
        Candle(
            ts=dt.datetime(2026, 4, 29, 10, 20),
            open=99,
            high=99.5,
            low=96,
            close=97.5,
            volume=600,
            elapsed_pct=0.75,
        ),
    )
    assert signal is not None

    position = engine.confirm_entry("RELIANCE", executed_price=97.5)
    assert position is not None
    assert position.qty < 0  # short

    # Price spikes above stop_loss -> should generate BUY-to-cover exit.
    exits = engine.on_price_update("RELIANCE", position.stop_loss + 0.05)
    assert len(exits) == 1
    assert exits[0].action == "BUY"
    assert exits[0].reason == "stop_loss"
