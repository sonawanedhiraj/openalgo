"""Strategy registry. Strategies register themselves at import time.

Usage from engine bootstrap::

    from strategies import registered_strategies

    for name, strategy_cls in registered_strategies().items():
        ...

Adding a new strategy is a three-step process:

1. Create ``strategies/<your_strategy>/strategy.py`` defining a subclass of
   :class:`strategies.base.BaseStrategy`.
2. Decorate the subclass with ``@register("<your_strategy>")``.
3. Add ``from strategies.<your_strategy> import strategy  # noqa: F401`` at
   the bottom of this file so importing :mod:`strategies` triggers the
   self-registration side effect.

The registry exists so Stage 1.7 (the regime profile work) and the
upcoming multi-strategy router can enumerate available strategies without
hard-coding their import paths. Today only ``trending_equity_intraday``
is registered; it's a thin adapter around the legacy
:class:`services.simplified_stock_engine_core.SimplifiedStockEngine`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategies.base import BaseStrategy


_registry: dict[str, type["BaseStrategy"]] = {}


def register(name: str):
    """Decorator that registers a :class:`BaseStrategy` subclass by name.

    Raises ``ValueError`` if a strategy with the same name was already
    registered — registration collisions are usually a real bug (typo, or
    two strategies fighting over the same slot) and silently letting the
    second one win would make the failure mode hard to debug.
    """

    def decorator(cls):
        if name in _registry:
            raise ValueError(f"Strategy '{name}' already registered")
        _registry[name] = cls
        return cls

    return decorator


def registered_strategies() -> dict[str, type["BaseStrategy"]]:
    """Snapshot of the current registry. Callers get a defensive copy so they
    can iterate without worrying that a future ``register`` mutates it under
    them."""
    return dict(_registry)


def get_strategy(name: str) -> type["BaseStrategy"] | None:
    """Return the registered strategy class for ``name`` or ``None``."""
    return _registry.get(name)


# Triggers self-registration on import. Keep this at the bottom so the
# registry helpers above are fully bound before the strategy module pulls
# them in.
from strategies.trending_equity_intraday import strategy as _trending_equity_intraday  # noqa: F401, E402
