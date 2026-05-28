"""Tick → bar aggregator shared across strategies and the in-house scanner.

This module hosts the original `FiveMinuteCandleBuilder` (extracted verbatim
from `simplified_stock_engine_core`) alongside a generalized interval-aware
implementation (`BarBuilder` and `MultiIntervalAggregator`) for reuse by the
upcoming scanner service and other strategies that need 1m / 5m / 15m / 1h
buckets.

Backward compatibility: `FiveMinuteCandleBuilder` is re-exported from
`services.simplified_stock_engine_core` so every existing caller continues
to work unchanged — including the heavily-used static `bucket()` method.

Conventions for the new API
---------------------------
- `BarBuilder` is single-symbol, single-interval. The callback fires on
  every tick (matching the legacy idiom): mid-bar updates carry
  `elapsed_pct` in [0, 1), and the closing update carries `elapsed_pct=1.0`.
- Tick input is a dict: `{"price": float, "cumulative_volume": int, "ts": dt.datetime}`.
- Bar output is a dict: `{"symbol", "interval", "ts", "open", "high", "low",
  "close", "volume", "elapsed_pct"}`. Dict (not dataclass) so downstream
  consumers can carry extra fields without a schema change.
- `MultiIntervalAggregator` is the manager class. Optional event-bus
  publish on bar close (off by default to avoid accidental coupling).
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from utils.event_bus import Event, bus as _default_bus
from utils.logging import get_logger

logger = get_logger(__name__)


# Interval string → seconds. Extend here when new buckets are needed.
INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}


def interval_to_seconds(interval: str) -> int:
    try:
        return INTERVAL_SECONDS[interval]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported interval {interval!r}; supported: {sorted(INTERVAL_SECONDS)}"
        ) from exc


def bucket_for_interval(ts: dt.datetime, interval_seconds: int) -> dt.datetime:
    """Truncate `ts` to the start of its bucket for the given interval (seconds).

    Mirrors the original FiveMinuteCandleBuilder.bucket() semantics for the
    5-minute case and extends it to other minute / hour intervals.
    """
    if interval_seconds < 3600:
        m = max(interval_seconds // 60, 1)
        return ts.replace(minute=(ts.minute // m) * m, second=0, microsecond=0)
    h = max(interval_seconds // 3600, 1)
    return ts.replace(hour=(ts.hour // h) * h, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Legacy class — extracted verbatim from simplified_stock_engine_core.
# Behavior MUST stay bit-identical; the simplified engine still consumes it.
# ---------------------------------------------------------------------------


# Cached reference to the Candle dataclass to avoid a top-level circular
# import (simplified_stock_engine_core imports FiveMinuteCandleBuilder from
# this module, so we can't import Candle eagerly). Resolved on first use.
_Candle_cls: Any = None


def _candle_cls() -> Any:
    global _Candle_cls
    if _Candle_cls is None:
        from services.simplified_stock_engine_core import Candle  # noqa: PLC0415

        _Candle_cls = Candle
    return _Candle_cls


class FiveMinuteCandleBuilder:
    """Original simplified-engine candle builder, kept here verbatim.

    Single callback for all symbols. Hard-coded 5-minute bucket via the
    static `bucket()` method. Volume is delta-from-cumulative.
    """

    def __init__(
        self,
        on_candle: Callable[[str, Any], None],
        candle_seconds: int = 300,
    ):
        self.on_candle = on_candle
        self.candle_seconds = candle_seconds
        self.current: dict[str, dict] = {}
        self.last_cum_vol: dict[str, int] = {}

    @staticmethod
    def bucket(ts: dt.datetime) -> dt.datetime:
        return ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)

    def on_tick(self, symbol: str, price: float, cumulative_volume: int, ts: dt.datetime) -> None:
        bucket = self.bucket(ts)
        price = float(price)
        cumulative_volume = int(cumulative_volume or 0)

        if symbol not in self.current:
            self.current[symbol] = {
                "bucket": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0,
            }
            self.last_cum_vol[symbol] = cumulative_volume
            return

        current = self.current[symbol]
        if bucket != current["bucket"]:
            self._emit(symbol, 1.0)
            self.current[symbol] = {
                "bucket": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0,
            }
            self.last_cum_vol[symbol] = cumulative_volume
            return

        delta = max(cumulative_volume - self.last_cum_vol.get(symbol, cumulative_volume), 0)
        self.last_cum_vol[symbol] = cumulative_volume
        current["high"] = max(current["high"], price)
        current["low"] = min(current["low"], price)
        current["close"] = price
        current["volume"] += delta

        elapsed = max((ts - current["bucket"]).total_seconds(), 0)
        self._emit(symbol, min(elapsed / float(self.candle_seconds), 0.999))

    def _emit(self, symbol: str, elapsed_pct: float) -> None:
        current = self.current[symbol]
        candle = _candle_cls()(
            ts=current["bucket"],
            open=current["open"],
            high=current["high"],
            low=current["low"],
            close=current["close"],
            volume=int(current["volume"]),
            elapsed_pct=elapsed_pct,
        )
        self.on_candle(symbol, candle)


# ---------------------------------------------------------------------------
# Generalized API — per-symbol per-interval. New consumers should target this.
# ---------------------------------------------------------------------------


@dataclass
class BarReadyEvent(Event):
    """Published by MultiIntervalAggregator when a bar closes.

    Only emitted when the aggregator was constructed with
    `publish_to_event_bus=True`. Subscribers receive closed bars only
    (mid-bar updates are not published — those stay in-process via the
    optional `on_bar` callback).
    """

    symbol: str = ""
    interval: str = ""
    bar: dict = field(default_factory=dict)
    topic: str = "bar_ready"


class BarBuilder:
    """Stateful tick → bar aggregator for ONE (symbol, interval) pair.

    Callback semantics match the legacy FiveMinuteCandleBuilder: `on_bar`
    fires on every tick after the first, with `elapsed_pct` < 1.0 for
    mid-bar updates and 1.0 for the closing tick of a bucket. The first
    tick only initializes state and does not invoke the callback.

    Tick input: `{"price": float, "cumulative_volume": int, "ts": dt.datetime}`.
    Bar output: `{"symbol", "interval", "ts", "open", "high", "low",
    "close", "volume", "elapsed_pct"}`.
    """

    def __init__(
        self,
        symbol: str,
        interval: str,
        on_bar: Callable[[dict], None] | None = None,
    ):
        self.symbol = symbol
        self.interval = interval
        self.interval_seconds = interval_to_seconds(interval)
        self.on_bar = on_bar
        self._current: dict | None = None
        self._last_cum_vol: int | None = None

    def _bucket(self, ts: dt.datetime) -> dt.datetime:
        return bucket_for_interval(ts, self.interval_seconds)

    def on_tick(self, tick: dict) -> None:
        ts = tick["ts"]
        price = float(tick["price"])
        cum_vol = int(tick.get("cumulative_volume") or 0)
        bucket = self._bucket(ts)

        if self._current is None:
            self._current = {
                "bucket": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0,
            }
            self._last_cum_vol = cum_vol
            return

        if bucket != self._current["bucket"]:
            self._emit(1.0)
            self._current = {
                "bucket": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0,
            }
            self._last_cum_vol = cum_vol
            return

        last = self._last_cum_vol if self._last_cum_vol is not None else cum_vol
        delta = max(cum_vol - last, 0)
        self._last_cum_vol = cum_vol
        self._current["high"] = max(self._current["high"], price)
        self._current["low"] = min(self._current["low"], price)
        self._current["close"] = price
        self._current["volume"] += delta

        elapsed = max((ts - self._current["bucket"]).total_seconds(), 0)
        self._emit(min(elapsed / float(self.interval_seconds), 0.999))

    def current_bar(self) -> dict | None:
        """Snapshot of the in-progress bar without advancing state. None if no ticks yet."""
        if self._current is None:
            return None
        return self._snapshot(elapsed_pct=0.0)

    def close_current_bar(self, forced: bool = False) -> dict | None:
        """Force-close the in-progress bar (e.g. EOD).

        Returns the closed bar with elapsed_pct=1.0 and clears state so the
        next tick starts fresh. Returns None if there is no in-progress bar.
        The `forced` flag is informational — the bar dict is the same either
        way. It exists so callers can log intent ("EOD force-close vs.
        natural close").
        """
        if self._current is None:
            return None
        bar = self._snapshot(elapsed_pct=1.0)
        self._current = None
        self._last_cum_vol = None
        return bar

    def _emit(self, elapsed_pct: float) -> None:
        if self.on_bar is None:
            return
        self.on_bar(self._snapshot(elapsed_pct))

    def _snapshot(self, elapsed_pct: float) -> dict:
        c = self._current
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "ts": c["bucket"],
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "volume": int(c["volume"]),
            "elapsed_pct": elapsed_pct,
        }


class MultiIntervalAggregator:
    """Holds N BarBuilder instances keyed by (symbol, interval).

    Construct with the symbols and intervals you want pre-subscribed.
    Add or drop pairs at runtime via `subscribe` / `unsubscribe`.

    Callback:
      - `on_bar_close(symbol, interval, bar)` — fires only on bar close.
        Mid-bar updates are intentionally NOT forwarded to this callback
        because the typical scanner / signal consumer only cares about
        closed bars.

    Event bus:
      - If `publish_to_event_bus=True`, a `BarReadyEvent` is published on
        every bar close after `on_bar_close` runs. Default off so consumers
        opt in explicitly.
    """

    def __init__(
        self,
        symbols: list[str] | None = None,
        intervals: list[str] | None = None,
        on_bar_close: Callable[[str, str, dict], None] | None = None,
        publish_to_event_bus: bool = False,
        bus: Any = None,
    ):
        self._builders: dict[tuple[str, str], BarBuilder] = {}
        self._on_bar_close = on_bar_close
        self._publish = publish_to_event_bus
        self._bus = bus if bus is not None else _default_bus

        for sym in symbols or []:
            for ival in intervals or []:
                self.subscribe(sym, ival)

    # -- subscription management -------------------------------------------

    def subscribe(self, symbol: str, interval: str) -> None:
        key = (symbol, interval)
        if key in self._builders:
            return
        self._builders[key] = BarBuilder(
            symbol,
            interval,
            on_bar=lambda bar, s=symbol, i=interval: self._handle_bar(s, i, bar),
        )

    def unsubscribe(self, symbol: str, interval: str) -> None:
        self._builders.pop((symbol, interval), None)

    def subscriptions(self) -> list[tuple[str, str]]:
        return list(self._builders.keys())

    # -- tick fan-out -------------------------------------------------------

    def on_tick(self, symbol: str, tick: dict) -> None:
        """Route a tick to every BarBuilder subscribed for this symbol.

        Unknown symbols are silently ignored — this is the expected
        behavior when the broker feed includes symbols the aggregator
        was never asked to track.
        """
        for (sym, _ival), builder in self._builders.items():
            if sym == symbol:
                builder.on_tick(tick)

    def current_bar(self, symbol: str, interval: str) -> dict | None:
        builder = self._builders.get((symbol, interval))
        return builder.current_bar() if builder else None

    def close_current_bar(self, symbol: str, interval: str, forced: bool = False) -> dict | None:
        builder = self._builders.get((symbol, interval))
        if builder is None:
            return None
        bar = builder.close_current_bar(forced=forced)
        if bar is not None:
            self._handle_bar(symbol, interval, bar)
        return bar

    # -- internal -----------------------------------------------------------

    def _handle_bar(self, symbol: str, interval: str, bar: dict) -> None:
        # Forward mid-bar updates to nothing, only bar closes to on_bar_close
        # and the event bus. elapsed_pct == 1.0 marks a close.
        is_close = bar.get("elapsed_pct", 0.0) >= 1.0
        if not is_close:
            return
        if self._on_bar_close is not None:
            try:
                self._on_bar_close(symbol, interval, bar)
            except Exception:
                logger.exception(
                    "MultiIntervalAggregator on_bar_close callback raised for %s/%s",
                    symbol,
                    interval,
                )
        if self._publish:
            try:
                self._bus.publish(BarReadyEvent(symbol=symbol, interval=interval, bar=bar))
            except Exception:
                logger.exception(
                    "MultiIntervalAggregator failed to publish bar_ready for %s/%s",
                    symbol,
                    interval,
                )


__all__ = [
    "INTERVAL_SECONDS",
    "interval_to_seconds",
    "bucket_for_interval",
    "FiveMinuteCandleBuilder",
    "BarBuilder",
    "BarReadyEvent",
    "MultiIntervalAggregator",
]
