"""Charge-model regression tests for compute_zerodha_intraday_charges.

Guards the per-order (NOT per-round-trip) Rs20 brokerage cap. The earlier model
capped brokerage at Rs20 for the whole round trip, under-reporting cost: the
NBCC reconciliation (500 sh, buy 107.65 / sell 106.65, intraday) showed model
Rs20.00 vs Kite Rs32.15 -- a 37.8% under-report. Zerodha actually caps Rs20 per
order, so each leg is charged separately and summed.
"""

import pytest

from services.simplified_stock_engine_core import (
    TradeCharges,
    compute_zerodha_intraday_charges,
)


def test_nbcc_brokerage_per_leg_matches_kite():
    """NBCC trade: per-leg cap must reproduce Kite's /charges/orders numbers.

    Kite reported brokerage Rs32.15 and total charges Rs57.27 for this trade.
    Brokerage now matches exactly (16.15 buy leg + 16.00 sell leg). The total
    is within Rs0.5 of Kite -- the small residual is the exchange/SEBI/GST rate
    approximations baked into the model, which are out of scope for this fix.
    """
    buy_value = 500 * 107.65   # 53825.0
    sell_value = 500 * 106.65  # 53325.0

    charges = compute_zerodha_intraday_charges(buy_value, sell_value)

    assert isinstance(charges, TradeCharges)
    # Headline fix: per-leg Rs20 cap -> Rs32.15, matching Kite exactly.
    assert charges.brokerage == pytest.approx(32.15, abs=0.001)
    # Model total reconciles to Kite's Rs57.27 within Rs0.5.
    assert charges.total == pytest.approx(57.27, abs=0.5)


def test_round_trip_cap_not_collapsed_to_20():
    """Regression: the buggy model clamped brokerage to a single Rs20 round-trip
    cap. With both legs' 0.03% above ~Rs16, the correct charge is well over Rs20.
    """
    charges = compute_zerodha_intraday_charges(500 * 107.65, 500 * 106.65)
    assert charges.brokerage > 20.0


def test_per_leg_cap_applies_independently_on_large_legs():
    """When each leg's 0.03% exceeds Rs20, each leg caps at Rs20 -> Rs40 total."""
    # 0.03% of 1,000,000 = Rs300 per leg, so both legs cap at Rs20.
    charges = compute_zerodha_intraday_charges(1_000_000.0, 1_000_000.0)
    assert charges.brokerage == pytest.approx(40.0, abs=0.001)


def test_small_trade_brokerage_is_percentage_not_capped():
    """Small legs stay on the 0.03% rate (below the Rs20 cap)."""
    # 0.03% of 10,000 = Rs3 per leg -> Rs6 total, uncapped.
    charges = compute_zerodha_intraday_charges(10_000.0, 10_000.0)
    assert charges.brokerage == pytest.approx(6.0, abs=0.001)
