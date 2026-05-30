"""Trending equity intraday strategy package.

Re-exports :mod:`strategies.trending_equity_intraday.strategy` so the
project-level :mod:`strategies` registry can trigger registration with a
single ``from strategies.trending_equity_intraday import strategy``.
"""

from strategies.trending_equity_intraday import strategy as strategy  # noqa: F401
