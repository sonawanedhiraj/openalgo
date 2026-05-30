"""``trending_equity_intraday`` — Stage 1.5 adapter for the simplified engine.

This module is **a thin forwarder**. The actual trading logic lives in
:class:`services.simplified_stock_engine_core.SimplifiedStockEngine`; we
wrap it so the new :class:`strategies.base.BaseStrategy` abstraction
covers it without changing any runtime behavior. Every hook below
delegates to the wrapped engine — no rebadging, no rule tweaks. If
behavior here diverges from the engine, that's a bug.

The accompanying ``__init__.py`` triggers the registration side effect
when :mod:`strategies` is imported.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from strategies import register
from strategies.base import BaseStrategy

if TYPE_CHECKING:
    from services.simplified_stock_engine_core import (
        Candle,
        EntrySignal,
        ExitSignal,
        Position,
        SimplifiedStockEngine,
    )


_STRATEGY_NAME = "trending_equity_intraday"


@register(_STRATEGY_NAME)
class TrendingEquityIntradayStrategy(BaseStrategy):
    """Adapter that exposes :class:`SimplifiedStockEngine` through
    :class:`BaseStrategy`.

    The wrapped engine is supplied at construction time. Tests pass a mock
    or a fresh engine; the live bootstrap (in
    :func:`services.simplified_stock_engine_service.get_simplified_stock_engine_service`)
    still owns the singleton — this adapter does not replace that wiring.
    See Stage 1.5 item 2 in the handoff doc for the migration plan.
    """

    name = _STRATEGY_NAME

    def __init__(self, engine: "SimplifiedStockEngine"):
        self._engine = engine

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def engine(self) -> "SimplifiedStockEngine":
        """Read-only access to the wrapped engine for hooks the
        BaseStrategy contract doesn't (yet) cover."""
        return self._engine

    # ------------------------------------------------------------------
    # BaseStrategy hooks — straight passthrough to the legacy engine
    # ------------------------------------------------------------------

    def on_scan_hit(self, symbol: str, direction: str) -> None:
        from services.simplified_stock_engine_core import DIRECTION_BUY, DIRECTION_SELL

        if direction == DIRECTION_BUY:
            self._engine.activate_buy_symbol(symbol)
        elif direction == DIRECTION_SELL:
            self._engine.activate_sell_symbol(symbol)
        else:
            raise ValueError(
                f"Unsupported direction {direction!r}; "
                f"expected {DIRECTION_BUY} or {DIRECTION_SELL}"
            )

    def seed_history(self, symbol: str, candles: list["Candle"]) -> None:
        self._engine.load_historical_candles(symbol, candles)

    def on_bar(self, symbol: str, candle: "Candle") -> "EntrySignal | None":
        return self._engine.on_new_candle(symbol, candle)

    def on_tick(self, symbol: str, price: float) -> list["ExitSignal"]:
        return self._engine.on_price_update(symbol, price)

    def confirm_entry(
        self, symbol: str, executed_price: float | None = None
    ) -> "Position | None":
        return self._engine.confirm_entry(symbol, executed_price)

    def confirm_exit(
        self,
        symbol: str,
        exit_price: float | None = None,
        reason: str | None = None,
    ) -> Any:
        return self._engine.confirm_exit(symbol, exit_price=exit_price, reason=reason)

    def clear_pending_entry(self, symbol: str) -> None:
        self._engine.clear_pending_entry(symbol)

    def clear_pending_exit(self, symbol: str) -> None:
        self._engine.clear_pending_exit(symbol)
