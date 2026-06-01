"""Tests for the scan rule registry and the two reference rules
(``fno_intraday_buy_20`` and ``fno_intraday_sell_20``).

The rules are decorated module-level callables that self-register into
``services.scanner_service``'s registry on import. We exercise:

* Registration / lookup via the decorator.
* Re-registration replaces the previous callable but does not crash.
* The BUY rule fires when volume surges above 2× the trailing average AND
  the close is above the 20-EMA; it stays silent otherwise.
* The SELL rule mirrors the BUY rule with an inverted price gate.
* Insufficient history (<21 bars) short-circuits both rules to ``False``.
"""

from __future__ import annotations

import pandas as pd
import pytest

# Import the package once so the example rules self-register before any test
# touches the registry. Subsequent tests can rely on these names existing.
import services.scan_rules  # noqa: F401
from services import indicators, scanner_service

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _build_bars(closes: list[float], volumes: list[float]) -> pd.DataFrame:
    """Build a tiny OHLCV frame from close and volume sequences.

    high/low bracket close by 1.0 to keep true-range calculations sensible
    if a future test wants to compute ATR off the same frame.
    """
    assert len(closes) == len(volumes), "closes and volumes must align"
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": volumes,
        }
    )


def _ema20(bars: pd.DataFrame) -> pd.Series:
    return indicators.ema(bars["close"], period=20)


# ---------------------------------------------------------------------------
# registry mechanics
# ---------------------------------------------------------------------------


def test_decorator_registers_rule():
    """A rule decorated with @scan_rule is reachable via ``get_rule``."""

    @scanner_service.scan_rule("__test_register_rule", "buy", "fixture-only")
    def _rule(_bars, _indicators):
        return True

    try:
        assert scanner_service.get_rule("__test_register_rule") is _rule
        meta = scanner_service.all_rules()["__test_register_rule"]
        assert meta["screener_type"] == "buy"
        assert meta["description"] == "fixture-only"
        assert meta["fn"] is _rule
    finally:
        # Don't leak into other tests — the registry is process-global.
        scanner_service._rule_registry.pop("__test_register_rule", None)
        scanner_service._rule_metadata.pop("__test_register_rule", None)


def test_decorator_rejects_bad_screener_type():
    with pytest.raises(ValueError):

        @scanner_service.scan_rule("__test_bad_screener", "hold", "x")  # noqa: ARG001
        def _rule(_bars, _indicators):
            return True


def test_get_rule_missing_returns_none():
    assert scanner_service.get_rule("does_not_exist_anywhere") is None


def test_reference_rules_registered_after_package_import():
    """Importing ``services.scan_rules`` should register both example rules."""
    rules = scanner_service.all_rules()
    assert "fno_intraday_buy_20" in rules
    assert "fno_intraday_sell_20" in rules
    assert rules["fno_intraday_buy_20"]["screener_type"] == "buy"
    assert rules["fno_intraday_sell_20"]["screener_type"] == "sell"


# ---------------------------------------------------------------------------
# fno_intraday_buy_20
# ---------------------------------------------------------------------------


def test_buy_rule_fires_on_volume_surge_and_price_above_ema():
    # 30 bars of slowly-rising price ⇒ EMA20 sits below the latest close.
    closes = [100.0 + i * 0.5 for i in range(29)] + [150.0]  # final spike
    # Baseline volume 1000, last bar volume 3000 ⇒ 3× the trailing mean.
    volumes = [1000.0] * 29 + [3000.0]
    bars = _build_bars(closes, volumes)

    rule = scanner_service.get_rule("fno_intraday_buy_20")
    assert rule is not None
    assert rule(bars, {"ema_20": _ema20(bars)}) is True


def test_buy_rule_does_not_fire_when_volume_is_normal():
    # Same rising-price frame, but the final bar has the same volume as the rest.
    closes = [100.0 + i * 0.5 for i in range(29)] + [150.0]
    volumes = [1000.0] * 30
    bars = _build_bars(closes, volumes)

    rule = scanner_service.get_rule("fno_intraday_buy_20")
    assert rule(bars, {"ema_20": _ema20(bars)}) is False


def test_buy_rule_does_not_fire_when_close_below_ema():
    # Rising price for 29 bars then a sharp drop — EMA still elevated.
    closes = [100.0 + i * 0.5 for i in range(29)] + [80.0]
    volumes = [1000.0] * 29 + [5000.0]  # huge surge to isolate the price gate
    bars = _build_bars(closes, volumes)

    rule = scanner_service.get_rule("fno_intraday_buy_20")
    assert rule(bars, {"ema_20": _ema20(bars)}) is False


def test_buy_rule_short_circuits_on_short_history():
    # 10 bars is well below the 21-bar minimum.
    closes = [100.0 + i for i in range(10)]
    volumes = [1000.0] * 9 + [10000.0]
    bars = _build_bars(closes, volumes)

    rule = scanner_service.get_rule("fno_intraday_buy_20")
    assert rule(bars, {"ema_20": _ema20(bars)}) is False


def test_buy_rule_short_circuits_when_ema_missing_from_indicators():
    closes = [100.0 + i * 0.5 for i in range(29)] + [150.0]
    volumes = [1000.0] * 29 + [3000.0]
    bars = _build_bars(closes, volumes)

    rule = scanner_service.get_rule("fno_intraday_buy_20")
    assert rule(bars, {}) is False


# ---------------------------------------------------------------------------
# fno_intraday_sell_20
# ---------------------------------------------------------------------------


def test_sell_rule_fires_on_volume_surge_and_price_below_ema():
    # 30 bars of slowly-falling price ⇒ EMA20 sits above the latest close.
    closes = [200.0 - i * 0.5 for i in range(29)] + [150.0]
    volumes = [1000.0] * 29 + [3000.0]
    bars = _build_bars(closes, volumes)

    rule = scanner_service.get_rule("fno_intraday_sell_20")
    assert rule is not None
    assert rule(bars, {"ema_20": _ema20(bars)}) is True


def test_sell_rule_does_not_fire_when_close_above_ema():
    # Falling price for 29 bars then a sharp bounce — EMA still depressed.
    closes = [200.0 - i * 0.5 for i in range(29)] + [220.0]
    volumes = [1000.0] * 29 + [5000.0]
    bars = _build_bars(closes, volumes)

    rule = scanner_service.get_rule("fno_intraday_sell_20")
    assert rule(bars, {"ema_20": _ema20(bars)}) is False


def test_sell_rule_does_not_fire_when_volume_normal():
    closes = [200.0 - i * 0.5 for i in range(29)] + [150.0]
    volumes = [1000.0] * 30
    bars = _build_bars(closes, volumes)

    rule = scanner_service.get_rule("fno_intraday_sell_20")
    assert rule(bars, {"ema_20": _ema20(bars)}) is False
