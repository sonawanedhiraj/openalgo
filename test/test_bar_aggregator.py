"""Tests for services.bar_aggregator.

Covers:
  - Bit-identical parity between the new BarBuilder('5m') and the legacy
    FiveMinuteCandleBuilder (the contract that nothing drifted).
  - 1m and 15m interval bucketing.
  - on_bar callback semantics (every tick after the first).
  - current_bar() snapshot without state advance.
  - close_current_bar(forced=True).
  - MultiIntervalAggregator fan-out across multiple intervals on one symbol.
  - Unknown symbol tick is silently ignored.
  - Optional event-bus publish: on when enabled, silent when disabled.
"""

import datetime as dt
from unittest.mock import MagicMock

import pytest

from services.bar_aggregator import (
    BarBuilder,
    BarReadyEvent,
    FiveMinuteCandleBuilder,
    MultiIntervalAggregator,
    bucket_for_interval,
    interval_to_seconds,
)
from services.simplified_stock_engine_core import Candle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(h: int, m: int, s: int = 0) -> dt.datetime:
    return dt.datetime(2026, 5, 22, h, m, s)


def _legacy_ticks_to_bars(ticks: list[tuple[str, float, int, dt.datetime]]) -> list[tuple[str, Candle]]:
    """Drive the legacy FiveMinuteCandleBuilder and collect every emitted (symbol, candle)."""
    out: list[tuple[str, Candle]] = []
    builder = FiveMinuteCandleBuilder(lambda s, c: out.append((s, c)))
    for sym, px, vol, ts in ticks:
        builder.on_tick(sym, px, vol, ts)
    return out


def _new_ticks_to_bars(ticks: list[tuple[str, float, int, dt.datetime]]) -> list[dict]:
    """Drive a new BarBuilder('5m') with the same tick stream (single symbol)."""
    assert ticks, "need at least one tick"
    symbol = ticks[0][0]
    assert all(s == symbol for s, _, _, _ in ticks), "single-symbol test only"
    out: list[dict] = []
    builder = BarBuilder(symbol, "5m", on_bar=lambda bar: out.append(bar))
    for _sym, px, vol, ts in ticks:
        builder.on_tick({"price": px, "cumulative_volume": vol, "ts": ts})
    return out


# ---------------------------------------------------------------------------
# 1. The big one: bit-identical parity with the legacy builder.
# ---------------------------------------------------------------------------


def test_bar_builder_constructs_5m_bar_from_ticks_value_parity_with_existing():
    """The same tick stream must produce identical OHLCV across both builders."""
    sym = "RELIANCE"
    # Build a tick stream that crosses three 5-min boundaries so we exercise
    # initial-state, in-bucket updates, bucket transitions, and volume deltas
    # (including a flat reading and a small drop clamped to 0).
    ticks = [
        # 09:15 bucket — first tick initializes state, no callback.
        (sym, 100.0, 1_000, _ts(9, 15, 5)),
        (sym, 101.5, 1_100, _ts(9, 16, 0)),   # high update, vol delta 100
        (sym,  99.5, 1_100, _ts(9, 17, 30)),  # low update, no vol delta
        (sym, 100.0, 1_050, _ts(9, 18, 0)),   # cumvol DROPPED — must clamp to 0
        (sym, 102.0, 1_300, _ts(9, 19, 59)),  # close of 09:15 bucket
        # 09:20 bucket — bucket transition; new bucket OHLC = new tick, vol=0.
        (sym,  98.0, 1_400, _ts(9, 20, 30)),
        (sym, 105.0, 1_600, _ts(9, 22, 0)),
        (sym, 103.0, 1_800, _ts(9, 24, 59)),
        # 09:25 bucket — final tick.
        (sym, 110.0, 2_000, _ts(9, 25, 1)),
    ]

    legacy = _legacy_ticks_to_bars(ticks)
    new = _new_ticks_to_bars(ticks)

    # Same number of emits, in the same order.
    assert len(legacy) == len(new), (
        f"emit count drift: legacy={len(legacy)} new={len(new)}"
    )

    for i, ((sym_legacy, candle), bar) in enumerate(zip(legacy, new, strict=True)):
        # Sanity: legacy emits (symbol, Candle); new emits a dict.
        assert sym_legacy == sym
        assert bar["symbol"] == sym
        assert bar["interval"] == "5m"

        # OHLCV + bucket timestamp + elapsed_pct must match exactly.
        assert candle.ts == bar["ts"], f"emit #{i}: ts drift"
        assert candle.open == bar["open"], f"emit #{i}: open drift"
        assert candle.high == bar["high"], f"emit #{i}: high drift"
        assert candle.low == bar["low"], f"emit #{i}: low drift"
        assert candle.close == bar["close"], f"emit #{i}: close drift"
        assert candle.volume == bar["volume"], f"emit #{i}: volume drift"
        assert candle.elapsed_pct == bar["elapsed_pct"], f"emit #{i}: elapsed_pct drift"


# ---------------------------------------------------------------------------
# 2. & 3. Interval bucketing.
# ---------------------------------------------------------------------------


def test_bar_builder_1m_interval():
    """1-minute interval closes on every minute boundary."""
    closes: list[dict] = []
    builder = BarBuilder("INFY", "1m", on_bar=lambda b: closes.append(b) if b["elapsed_pct"] == 1.0 else None)

    # 09:15 bucket: init + one in-bucket update.
    builder.on_tick({"price": 100.0, "cumulative_volume": 0, "ts": _ts(9, 15, 10)})
    builder.on_tick({"price": 101.0, "cumulative_volume": 50, "ts": _ts(9, 15, 40)})
    # Cross into 09:16 — closes the 09:15 bar.
    builder.on_tick({"price": 102.0, "cumulative_volume": 60, "ts": _ts(9, 16, 5)})
    # Cross into 09:17 — closes the 09:16 bar.
    builder.on_tick({"price": 103.0, "cumulative_volume": 80, "ts": _ts(9, 17, 1)})

    assert [b["ts"] for b in closes] == [_ts(9, 15), _ts(9, 16)]
    assert closes[0]["open"] == 100.0
    assert closes[0]["high"] == 101.0
    assert closes[0]["close"] == 101.0
    assert closes[0]["volume"] == 50  # one in-bucket delta of 50


def test_bar_builder_15m_interval():
    """15-minute interval closes on :00 / :15 / :30 / :45 boundaries."""
    closes: list[dict] = []
    builder = BarBuilder("HDFC", "15m", on_bar=lambda b: closes.append(b) if b["elapsed_pct"] == 1.0 else None)

    # 09:15–09:29 bucket.
    builder.on_tick({"price": 100.0, "cumulative_volume": 0, "ts": _ts(9, 16)})
    builder.on_tick({"price": 105.0, "cumulative_volume": 200, "ts": _ts(9, 28)})
    # Cross into 09:30 bucket — closes the 09:15 bar.
    builder.on_tick({"price": 110.0, "cumulative_volume": 300, "ts": _ts(9, 30, 1)})
    # Cross into 09:45 — closes the 09:30 bar.
    builder.on_tick({"price": 108.0, "cumulative_volume": 400, "ts": _ts(9, 45, 1)})

    assert [b["ts"] for b in closes] == [_ts(9, 15), _ts(9, 30)]
    assert closes[0]["high"] == 105.0
    assert closes[0]["volume"] == 200


def test_bucket_for_interval_helper_5m_matches_legacy_bucket():
    """bucket_for_interval(ts, 300) must equal FiveMinuteCandleBuilder.bucket(ts)."""
    for h, m in [(9, 15), (9, 17), (9, 19), (10, 0), (10, 4), (15, 29)]:
        ts = _ts(h, m, 30)
        assert bucket_for_interval(ts, 300) == FiveMinuteCandleBuilder.bucket(ts)


def test_interval_to_seconds_supports_documented_intervals():
    assert interval_to_seconds("1m") == 60
    assert interval_to_seconds("5m") == 300
    assert interval_to_seconds("15m") == 900
    assert interval_to_seconds("1h") == 3600
    with pytest.raises(ValueError):
        interval_to_seconds("7m")


# ---------------------------------------------------------------------------
# 4. Callback invocation.
# ---------------------------------------------------------------------------


def test_bar_builder_on_bar_close_callback_invoked():
    """Callback fires on every tick after the first; closing emit carries elapsed_pct=1.0."""
    calls: list[dict] = []
    builder = BarBuilder("ITC", "5m", on_bar=lambda b: calls.append(b))

    builder.on_tick({"price": 100.0, "cumulative_volume": 0, "ts": _ts(9, 15)})  # init, no emit
    builder.on_tick({"price": 101.0, "cumulative_volume": 10, "ts": _ts(9, 16)})  # mid-bar
    builder.on_tick({"price": 102.0, "cumulative_volume": 20, "ts": _ts(9, 20)})  # closes 09:15

    assert len(calls) == 2
    # First call is mid-bar update of the 09:15 bucket.
    assert calls[0]["ts"] == _ts(9, 15)
    assert 0.0 < calls[0]["elapsed_pct"] < 1.0
    # Second call closes the 09:15 bucket with elapsed_pct=1.0.
    assert calls[1]["ts"] == _ts(9, 15)
    assert calls[1]["elapsed_pct"] == 1.0


# ---------------------------------------------------------------------------
# 5. current_bar() is read-only.
# ---------------------------------------------------------------------------


def test_bar_builder_partial_bar_via_current_bar():
    """current_bar() returns in-progress bar without advancing state or firing callback."""
    callback = MagicMock()
    builder = BarBuilder("AXIS", "5m", on_bar=callback)

    builder.on_tick({"price": 100.0, "cumulative_volume": 0, "ts": _ts(9, 15)})
    builder.on_tick({"price": 105.0, "cumulative_volume": 50, "ts": _ts(9, 17)})

    callback.reset_mock()
    snap1 = builder.current_bar()
    snap2 = builder.current_bar()

    assert snap1 == snap2  # idempotent
    assert snap1["ts"] == _ts(9, 15)
    assert snap1["open"] == 100.0
    assert snap1["high"] == 105.0
    assert snap1["close"] == 105.0
    assert snap1["volume"] == 50
    callback.assert_not_called()  # current_bar() must not trigger emit


def test_bar_builder_current_bar_none_before_any_tick():
    builder = BarBuilder("WIPRO", "5m")
    assert builder.current_bar() is None


# ---------------------------------------------------------------------------
# 6. close_current_bar(forced=True).
# ---------------------------------------------------------------------------


def test_bar_builder_close_current_bar_force():
    """Force-close returns the partial bar at elapsed_pct=1.0 and resets state."""
    builder = BarBuilder("LT", "5m")

    builder.on_tick({"price": 100.0, "cumulative_volume": 0, "ts": _ts(9, 15)})
    builder.on_tick({"price": 110.0, "cumulative_volume": 30, "ts": _ts(9, 17)})

    bar = builder.close_current_bar(forced=True)

    assert bar is not None
    assert bar["ts"] == _ts(9, 15)
    assert bar["open"] == 100.0
    assert bar["high"] == 110.0
    assert bar["volume"] == 30
    assert bar["elapsed_pct"] == 1.0

    # State cleared — current_bar() is None until next tick.
    assert builder.current_bar() is None
    # Calling close again returns None (nothing to close).
    assert builder.close_current_bar(forced=True) is None


# ---------------------------------------------------------------------------
# 7. MultiIntervalAggregator fan-out.
# ---------------------------------------------------------------------------


def test_multi_interval_aggregator_fans_out_to_subscribers():
    """One tick stream feeds both (RELIANCE, 5m) and (RELIANCE, 15m); both close correctly."""
    closes: list[tuple[str, str, dict]] = []
    agg = MultiIntervalAggregator(
        symbols=["RELIANCE"],
        intervals=["5m", "15m"],
        on_bar_close=lambda sym, ival, bar: closes.append((sym, ival, bar)),
    )

    # 09:15 — init both.
    agg.on_tick("RELIANCE", {"price": 100.0, "cumulative_volume": 0, "ts": _ts(9, 15)})
    # 09:20 — closes 5m@09:15 only.
    agg.on_tick("RELIANCE", {"price": 102.0, "cumulative_volume": 50, "ts": _ts(9, 20)})
    # 09:25 — closes 5m@09:20.
    agg.on_tick("RELIANCE", {"price": 104.0, "cumulative_volume": 100, "ts": _ts(9, 25)})
    # 09:30 — closes both 5m@09:25 AND 15m@09:15.
    agg.on_tick("RELIANCE", {"price": 106.0, "cumulative_volume": 150, "ts": _ts(9, 30)})

    intervals_closed = [(ival, b["ts"]) for _sym, ival, b in closes]
    assert intervals_closed == [
        ("5m", _ts(9, 15)),
        ("5m", _ts(9, 20)),
        ("5m", _ts(9, 25)),
        ("15m", _ts(9, 15)),
    ]


def test_multi_interval_aggregator_subscribe_unsubscribe_dynamic():
    agg = MultiIntervalAggregator()
    assert agg.subscriptions() == []
    agg.subscribe("INFY", "5m")
    agg.subscribe("INFY", "15m")
    assert ("INFY", "5m") in agg.subscriptions()
    assert ("INFY", "15m") in agg.subscriptions()
    agg.unsubscribe("INFY", "5m")
    assert ("INFY", "5m") not in agg.subscriptions()
    assert ("INFY", "15m") in agg.subscriptions()


# ---------------------------------------------------------------------------
# 8. Unknown symbol — silent.
# ---------------------------------------------------------------------------


def test_multi_interval_aggregator_unknown_symbol_ignored():
    closes: list = []
    agg = MultiIntervalAggregator(
        symbols=["RELIANCE"], intervals=["5m"],
        on_bar_close=lambda s, i, b: closes.append((s, i, b)),
    )

    # Ticks for SBIN — never subscribed. Should be a no-op, no crash, no emit.
    agg.on_tick("SBIN", {"price": 500.0, "cumulative_volume": 0, "ts": _ts(9, 15)})
    agg.on_tick("SBIN", {"price": 510.0, "cumulative_volume": 100, "ts": _ts(9, 20)})

    assert closes == []
    assert agg.current_bar("SBIN", "5m") is None


# ---------------------------------------------------------------------------
# 9. & 10. Event-bus publish toggle.
# ---------------------------------------------------------------------------


def test_multi_interval_publishes_bar_ready_event_when_enabled():
    """publish_to_event_bus=True → bar close publishes a BarReadyEvent."""
    mock_bus = MagicMock()
    agg = MultiIntervalAggregator(
        symbols=["TCS"],
        intervals=["5m"],
        publish_to_event_bus=True,
        bus=mock_bus,
    )

    agg.on_tick("TCS", {"price": 3500.0, "cumulative_volume": 0, "ts": _ts(9, 15)})
    agg.on_tick("TCS", {"price": 3510.0, "cumulative_volume": 100, "ts": _ts(9, 17)})  # mid-bar — no publish
    agg.on_tick("TCS", {"price": 3520.0, "cumulative_volume": 200, "ts": _ts(9, 20)})  # closes 09:15 — publishes

    assert mock_bus.publish.call_count == 1, "expected exactly one publish on bar close"
    event = mock_bus.publish.call_args.args[0]
    assert isinstance(event, BarReadyEvent)
    assert event.topic == "bar_ready"
    assert event.symbol == "TCS"
    assert event.interval == "5m"
    assert event.bar["ts"] == _ts(9, 15)
    assert event.bar["open"] == 3500.0
    assert event.bar["close"] == 3510.0  # close is the LAST in-bucket price, not the boundary-crossing tick
    assert event.bar["elapsed_pct"] == 1.0


def test_multi_interval_does_not_publish_when_disabled():
    """Default publish_to_event_bus=False → no event bus calls even on bar close."""
    mock_bus = MagicMock()
    agg = MultiIntervalAggregator(
        symbols=["TCS"],
        intervals=["5m"],
        publish_to_event_bus=False,  # explicit
        bus=mock_bus,
    )

    agg.on_tick("TCS", {"price": 3500.0, "cumulative_volume": 0, "ts": _ts(9, 15)})
    agg.on_tick("TCS", {"price": 3520.0, "cumulative_volume": 100, "ts": _ts(9, 20)})

    mock_bus.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Defensive: an exception inside on_bar_close must not break the aggregator
# (subscriber failures should be isolated so one bad listener can't break others).
# ---------------------------------------------------------------------------


def test_multi_interval_on_bar_close_exception_does_not_kill_aggregator():
    def angry_cb(sym, ival, bar):
        raise RuntimeError("simulated downstream failure")

    agg = MultiIntervalAggregator(
        symbols=["INFY"], intervals=["5m"], on_bar_close=angry_cb,
    )

    agg.on_tick("INFY", {"price": 100.0, "cumulative_volume": 0, "ts": _ts(9, 15)})
    # Closes 09:15 — callback throws but the aggregator continues.
    agg.on_tick("INFY", {"price": 102.0, "cumulative_volume": 50, "ts": _ts(9, 20)})
    # Next tick must still work.
    agg.on_tick("INFY", {"price": 104.0, "cumulative_volume": 100, "ts": _ts(9, 25)})

    snap = agg.current_bar("INFY", "5m")
    assert snap is not None
    assert snap["ts"] == _ts(9, 25)
