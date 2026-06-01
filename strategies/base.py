"""Abstract base class for trading strategies registered in :mod:`strategies`.

The hook set is shaped around the existing
:class:`services.simplified_stock_engine_core.SimplifiedStockEngine` so the
``trending_equity_intraday`` adapter can forward calls 1:1 with no behavior
drift. Specifically:

* :meth:`on_scan_hit` mirrors the engine's
  :meth:`activate_buy_symbol` / :meth:`activate_sell_symbol`.
* :meth:`on_bar` mirrors :meth:`SimplifiedStockEngine.on_new_candle` and
  returns an optional ``EntrySignal``.
* :meth:`on_tick` mirrors :meth:`SimplifiedStockEngine.on_price_update`
  and returns a list of ``ExitSignal``\\s.
* :meth:`seed_history` mirrors :meth:`load_historical_candles` for the
  morning warmup path.

Stage 1.7's regime-profile layer will read from :meth:`regime_profile` to
let the LLM veto reviewer make strategy-aware decisions; until then it
defaults to an empty dict so existing strategies don't have to opt in.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.simplified_stock_engine_core import (
        Candle,
        EntrySignal,
        ExitSignal,
    )


class BaseStrategy(ABC):
    """Contract every registered strategy must implement.

    The hooks are intentionally narrow — they cover the lifecycle the
    simplified engine already drives (scan-hit → bar close → tick → exit).
    A strategy that needs richer semantics (e.g. multi-leg orchestration)
    should compose the orchestration above the hooks rather than overload
    them.
    """

    #: Subclasses override this to match their registry key. Kept as a
    #: class attribute so callers can inspect ``strategy_cls.name`` without
    #: instantiating.
    name: str = "base"

    #: EOD policy. Intraday strategies are flattened at ``eod_exit_time`` by
    #: the watchdog (services/eod_watchdog_service.py). Positional / overnight
    #: strategies set ``intraday=False`` and are skipped by the watchdog.
    intraday: bool = True

    #: IST clock time ``HH:MM`` at which the EOD watchdog should fire for this
    #: strategy. Only consulted when ``intraday`` is True. If unset / invalid,
    #: callers fall back to ``SIMPLIFIED_ENGINE_EOD_EXIT_TIME`` from the env
    #: (default 15:20). Per-strategy override lets future surfaces (options
    #: writers that need to roll earlier, etc.) declare their own cutoff.
    eod_exit_time: str = "15:20"

    @abstractmethod
    def on_scan_hit(self, symbol: str, direction: str) -> None:
        """Called when a scanner (Chartink, custom screener, manual webhook)
        flags ``symbol`` for entry consideration. ``direction`` is the
        symmetric ``BUY`` / ``SELL`` constant from
        :mod:`services.simplified_stock_engine_core`."""

    @abstractmethod
    def seed_history(self, symbol: str, candles: list["Candle"]) -> None:
        """Populate ``symbol``'s history (typically the last N 5-minute
        candles) before live ticks start arriving. Used by the morning
        warmup path so the first live candle has a reference set."""

    @abstractmethod
    def on_bar(self, symbol: str, candle: "Candle") -> "EntrySignal | None":
        """Called when a new bar for ``symbol`` closes (or progresses past
        the elapsed-pct entry threshold). Returns an ``EntrySignal`` to
        schedule an entry, or ``None`` if no action is warranted."""

    @abstractmethod
    def on_tick(self, symbol: str, price: float) -> list["ExitSignal"]:
        """Called for every accepted price tick on ``symbol``. Returns the
        list of exits the strategy wants to schedule on the back of this
        tick — stop-loss hits, RR trailing exits, EOD flattens, etc."""

    @abstractmethod
    def confirm_entry(self, symbol: str, executed_price: float | None) -> Any:
        """Promote a pending entry into an open position after the broker /
        sandbox reports the fill. ``executed_price`` defaults to the
        signal's reference price when ``None``."""

    @abstractmethod
    def confirm_exit(
        self,
        symbol: str,
        exit_price: float | None,
        reason: str | None,
    ) -> Any:
        """Close the position for ``symbol`` and append the round-trip to
        the strategy's completed-trades ledger."""

    @abstractmethod
    def clear_pending_entry(self, symbol: str) -> None:
        """Drop a pending entry without confirming it (rejected order,
        veto, funds gate fail)."""

    @abstractmethod
    def clear_pending_exit(self, symbol: str) -> None:
        """Drop a pending exit without confirming it (rejected order, SL
        confirmation re-evaluated false)."""

    # ------------------------------------------------------------------
    # Optional hooks with default implementations
    # ------------------------------------------------------------------

    def regime_profile(self) -> dict[str, Any]:
        """Return a strategy-specific regime profile for Stage 1.7's veto
        reviewer. Defaults to an empty dict — strategies that haven't
        opted in are simply scored against a neutral profile."""
        return {}

    def now(self) -> dt.datetime:
        """Hook for strategies that need a controllable clock (e.g. for
        deterministic tests). Default uses wall-clock ``datetime.now()``."""
        return dt.datetime.now()
