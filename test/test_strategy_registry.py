"""Tests for the Stage 1.5 strategy registry.

The registry exists so future code can enumerate available strategies
without hard-coding their import paths. These tests pin down:

* Self-registration: importing :mod:`strategies` is enough to populate
  the registry with ``trending_equity_intraday``.
* ``register`` decorator semantics: duplicate names raise.
* :class:`BaseStrategy` is abstract.
* The ``trending_equity_intraday`` adapter implements every abstract
  hook and forwards them 1:1 to the wrapped engine (the actual zero-
  behavior-change guarantee we need for the refactor).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_registry_has_trending_equity_intraday_after_import():
    """Smoke test: importing the package self-registers the strategy."""
    from strategies import registered_strategies

    assert "trending_equity_intraday" in registered_strategies()


def test_get_strategy_returns_subclass_of_base():
    from strategies import get_strategy
    from strategies.base import BaseStrategy

    cls = get_strategy("trending_equity_intraday")
    assert cls is not None
    assert issubclass(cls, BaseStrategy)


def test_get_strategy_returns_none_for_unknown_name():
    from strategies import get_strategy

    assert get_strategy("nope_not_registered") is None


def test_registered_strategies_returns_defensive_copy():
    """Callers should be able to mutate the returned dict without disturbing
    the underlying registry — that's why ``registered_strategies()`` is a
    ``dict()`` copy rather than a direct reference."""
    from strategies import registered_strategies

    snapshot = registered_strategies()
    snapshot["junk"] = object  # mutate the snapshot
    # Re-fetch — the real registry must be unchanged.
    fresh = registered_strategies()
    assert "junk" not in fresh


def test_register_decorator_rejects_duplicates():
    from strategies import register
    from strategies.base import BaseStrategy

    # First registration succeeds.
    @register("dup_test_strategy")
    class _First(BaseStrategy):
        name = "dup_test_strategy"

        def on_scan_hit(self, symbol, direction): ...
        def seed_history(self, symbol, candles): ...
        def on_bar(self, symbol, candle): return None
        def on_tick(self, symbol, price): return []
        def confirm_entry(self, symbol, executed_price=None): return None
        def confirm_exit(self, symbol, exit_price=None, reason=None): return None
        def clear_pending_entry(self, symbol): ...
        def clear_pending_exit(self, symbol): ...

    with pytest.raises(ValueError, match="already registered"):

        @register("dup_test_strategy")
        class _Second(BaseStrategy):
            name = "dup_test_strategy"

            def on_scan_hit(self, symbol, direction): ...
            def seed_history(self, symbol, candles): ...
            def on_bar(self, symbol, candle): return None
            def on_tick(self, symbol, price): return []
            def confirm_entry(self, symbol, executed_price=None): return None
            def confirm_exit(self, symbol, exit_price=None, reason=None): return None
            def clear_pending_entry(self, symbol): ...
            def clear_pending_exit(self, symbol): ...


def test_base_strategy_is_abstract():
    """Instantiating BaseStrategy directly must raise TypeError because the
    abstract hooks have no default implementations."""
    from strategies.base import BaseStrategy

    with pytest.raises(TypeError):
        BaseStrategy()  # type: ignore[abstract]


def test_trending_equity_strategy_implements_all_hooks():
    """Every abstract method on BaseStrategy must be overridden — otherwise
    the adapter would be implicitly abstract and uninstantiable."""
    from strategies.base import BaseStrategy
    from strategies.trending_equity_intraday.strategy import (
        TrendingEquityIntradayStrategy,
    )

    abstract_methods = BaseStrategy.__abstractmethods__
    assert abstract_methods  # sanity: there are some abstract hooks
    overridden = TrendingEquityIntradayStrategy.__abstractmethods__
    assert overridden == frozenset(), (
        f"Subclass still has abstract methods: {overridden}"
    )

    # Every abstract method name must appear on the subclass with its own
    # implementation (not inherited as still-abstract).
    for name in abstract_methods:
        assert hasattr(TrendingEquityIntradayStrategy, name)


def test_trending_equity_strategy_name_matches_registry_key():
    from strategies import get_strategy
    from strategies.trending_equity_intraday.strategy import (
        TrendingEquityIntradayStrategy,
    )

    assert TrendingEquityIntradayStrategy.name == "trending_equity_intraday"
    assert get_strategy("trending_equity_intraday") is TrendingEquityIntradayStrategy


def test_trending_equity_strategy_forwards_on_scan_hit_to_engine():
    """on_scan_hit must dispatch to activate_buy_symbol /
    activate_sell_symbol on the wrapped engine."""
    from strategies.trending_equity_intraday.strategy import (
        TrendingEquityIntradayStrategy,
    )

    engine = MagicMock()
    strategy = TrendingEquityIntradayStrategy(engine)

    strategy.on_scan_hit("RELIANCE", "BUY")
    engine.activate_buy_symbol.assert_called_once_with("RELIANCE")
    engine.activate_sell_symbol.assert_not_called()

    strategy.on_scan_hit("INFY", "SELL")
    engine.activate_sell_symbol.assert_called_once_with("INFY")


def test_trending_equity_strategy_rejects_unknown_direction():
    from strategies.trending_equity_intraday.strategy import (
        TrendingEquityIntradayStrategy,
    )

    strategy = TrendingEquityIntradayStrategy(MagicMock())
    with pytest.raises(ValueError, match="Unsupported direction"):
        strategy.on_scan_hit("FOO", "HOLD")


def test_trending_equity_strategy_forwards_on_bar_to_engine():
    from strategies.trending_equity_intraday.strategy import (
        TrendingEquityIntradayStrategy,
    )

    engine = MagicMock()
    engine.on_new_candle.return_value = "fake-entry-signal"
    strategy = TrendingEquityIntradayStrategy(engine)

    candle = object()
    result = strategy.on_bar("RELIANCE", candle)

    engine.on_new_candle.assert_called_once_with("RELIANCE", candle)
    assert result == "fake-entry-signal"


def test_trending_equity_strategy_forwards_on_tick_to_engine():
    """on_tick must dispatch to on_price_update with the same args and
    return its result verbatim."""
    from strategies.trending_equity_intraday.strategy import (
        TrendingEquityIntradayStrategy,
    )

    engine = MagicMock()
    engine.on_price_update.return_value = ["exit1", "exit2"]
    strategy = TrendingEquityIntradayStrategy(engine)

    result = strategy.on_tick("RELIANCE", 2500.5)

    engine.on_price_update.assert_called_once_with("RELIANCE", 2500.5)
    assert result == ["exit1", "exit2"]


def test_trending_equity_strategy_forwards_confirm_entry_and_exit():
    from strategies.trending_equity_intraday.strategy import (
        TrendingEquityIntradayStrategy,
    )

    engine = MagicMock()
    strategy = TrendingEquityIntradayStrategy(engine)

    strategy.confirm_entry("RELIANCE", executed_price=2501.0)
    engine.confirm_entry.assert_called_once_with("RELIANCE", 2501.0)

    strategy.confirm_exit("RELIANCE", exit_price=2495.5, reason="stop_loss")
    engine.confirm_exit.assert_called_once_with(
        "RELIANCE", exit_price=2495.5, reason="stop_loss"
    )


def test_trending_equity_strategy_forwards_seed_history_and_clear_pending():
    from strategies.trending_equity_intraday.strategy import (
        TrendingEquityIntradayStrategy,
    )

    engine = MagicMock()
    strategy = TrendingEquityIntradayStrategy(engine)
    candles = [object(), object()]

    strategy.seed_history("RELIANCE", candles)
    engine.load_historical_candles.assert_called_once_with("RELIANCE", candles)

    strategy.clear_pending_entry("RELIANCE")
    engine.clear_pending_entry.assert_called_once_with("RELIANCE")

    strategy.clear_pending_exit("INFY")
    engine.clear_pending_exit.assert_called_once_with("INFY")


def test_trending_equity_strategy_exposes_engine_via_property():
    """The adapter intentionally surfaces the wrapped engine so callers
    that need behavior the BaseStrategy contract doesn't (yet) cover can
    still reach it. Stage 1.7's regime profile and the scheduler's
    completed-trades reader rely on this escape hatch."""
    from strategies.trending_equity_intraday.strategy import (
        TrendingEquityIntradayStrategy,
    )

    engine = MagicMock()
    strategy = TrendingEquityIntradayStrategy(engine)
    assert strategy.engine is engine


def test_regime_profile_defaults_to_empty():
    """Subclasses that haven't opted in to Stage 1.7 should get an empty
    profile rather than a NotImplementedError."""
    from strategies.trending_equity_intraday.strategy import (
        TrendingEquityIntradayStrategy,
    )

    strategy = TrendingEquityIntradayStrategy(MagicMock())
    assert strategy.regime_profile() == {}
