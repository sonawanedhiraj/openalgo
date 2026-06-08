"""Unit tests for the sector_rotation_etf signal module.

All tests are marked @pytest.mark.unit for opt-in scope isolation — strategy
tests must not run as part of the default suite (they touch synthetic data only,
never the live DBs, but the marker keeps them isolated per the pytest-pollution
learning).

Run: uv run pytest test/test_sector_rotation_etf.py -v -m unit
"""

import math
from datetime import UTC, date, datetime, timezone

import numpy as np
import pytest

from services.sector_rotation_etf_service import (
    SectorRotationConfig,
    compute_momentum_returns,
    compute_realized_vol,
    compute_rebalance,
    compute_risk_parity_weights,
    compute_target_positions,
    diff_orders,
    select_lowvol_basket,
    select_momentum_basket,
)

pytestmark = pytest.mark.unit


def _series(prices: list[float], start: date = date(2025, 1, 1)) -> list:
    """Build a (date, close) series with consecutive daily dates."""
    out = []
    d = start.toordinal()
    for p in prices:
        out.append((date.fromordinal(d), float(p)))
        d += 1
    return out


def test_compute_momentum_returns_basic():
    # 130 closes: linear ramp 100 -> 100 + 129. 6M (126-bar) lookback.
    prices = [100.0 + i for i in range(130)]
    closes = {"X": _series(prices)}
    out = compute_momentum_returns(closes, lookback_days=126)
    # last close = prices[129] = 229; close 126 bars ago = prices[-127] = prices[3] = 103
    expected = 229.0 / 103.0 - 1.0
    assert out["X"] == pytest.approx(expected, rel=1e-9)


def test_compute_realized_vol_basic():
    # Construct returns with a known daily std, verify sqrt(252) annualization.
    rng = np.random.default_rng(42)
    daily_log = rng.normal(0.0, 0.01, 60)
    prices = [100.0]
    for r in daily_log:
        prices.append(prices[-1] * math.exp(r))
    closes = {"X": _series(prices)}
    out = compute_realized_vol(closes, lookback_days=60)
    expected = float(np.std(daily_log, ddof=1)) * math.sqrt(252)
    assert out["X"] == pytest.approx(expected, rel=1e-9)


def test_select_momentum_basket():
    returns = {"A": 0.5, "B": 0.1, "C": 0.3, "D": 0.3, "E": -0.2}
    # Top-3 by return; C and D tie at 0.3 -> tie broken by symbol asc (C before D).
    assert select_momentum_basket(returns, 3) == ["A", "C", "D"]


def test_select_lowvol_basket():
    vols = {"A": 0.40, "B": 0.10, "C": 0.25, "D": 0.10, "E": 0.50}
    # Bottom-3 by vol; B and D tie at 0.10 -> tie broken by symbol asc (B, D), then C.
    assert select_lowvol_basket(vols, 3) == ["B", "D", "C"]


def test_compute_risk_parity_weights_sums_to_one():
    w_mom, w_lv = compute_risk_parity_weights(0.30, 0.10)
    assert w_mom + w_lv == pytest.approx(1.0)
    # Low-vol leg is calmer -> gets MORE weight (inverse-vol direction).
    assert w_lv > w_mom
    # Degenerate fallback to 50/50.
    assert compute_risk_parity_weights(0.0, 0.10) == (0.5, 0.5)


def test_compute_target_positions_equal_weight_within_leg():
    mom = ["A", "B", "C"]
    lv = ["D", "E", "F"]
    prices = dict.fromkeys(mom + lv, 100.0)
    targets = compute_target_positions(mom, lv, 0.5, 0.5, 300000.0, prices)
    # Momentum leg = 0.5 * 300k = 150k, equal across 3 -> 50k each.
    for s in mom:
        assert targets[s]["target_notional"] == pytest.approx(50000.0)
        assert targets[s]["target_quantity"] == 500  # 50000 / 100
        assert targets[s]["reason"] == "momentum"
    for s in lv:
        assert targets[s]["target_notional"] == pytest.approx(50000.0)
        assert targets[s]["reason"] == "lowvol"


def test_compute_target_positions_overlap_summed():
    # A is in both legs -> notionals summed, reason "both".
    targets = compute_target_positions(
        ["A", "B", "C"], ["A", "D", "E"], 0.5, 0.5, 300000.0,
        dict.fromkeys("ABCDE", 100.0),
    )
    assert targets["A"]["target_notional"] == pytest.approx(100000.0)
    assert targets["A"]["reason"] == "both"


def test_diff_orders_no_change():
    targets = {"A": {"target_quantity": 100, "target_notional": 10000.0, "reason": "momentum"}}
    current = {"A": 100}
    assert diff_orders(current, targets, {"A": 100.0}) == []


def test_diff_orders_full_rebalance():
    targets = {
        "A": {"target_quantity": 100, "target_notional": 10000.0, "reason": "momentum"},
        "B": {"target_quantity": 50, "target_notional": 5000.0, "reason": "lowvol"},
    }
    orders = diff_orders({}, targets, {"A": 100.0, "B": 100.0})
    assert all(o.side == "BUY" for o in orders)
    assert {o.symbol: o.quantity for o in orders} == {"A": 100, "B": 50}


def test_diff_orders_rotation():
    # OLD held, gone from target -> SELL (exit). NEW in target -> BUY. Sells first.
    targets = {"NEW": {"target_quantity": 80, "target_notional": 8000.0, "reason": "momentum"}}
    current = {"OLD": 40}
    orders = diff_orders(current, targets, {"OLD": 100.0, "NEW": 100.0})
    assert orders[0].side == "SELL" and orders[0].symbol == "OLD"
    assert orders[0].reason == "exit"
    assert orders[1].side == "BUY" and orders[1].symbol == "NEW"


def test_compute_rebalance_end_to_end_smoke(tmp_path):
    import duckdb

    db_path = str(tmp_path / "hist.duckdb")
    con = duckdb.connect(db_path)
    con.execute(
        """
        CREATE TABLE market_data (
            symbol VARCHAR, exchange VARCHAR, interval VARCHAR,
            timestamp BIGINT, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume BIGINT
        )
        """
    )
    symbols = ["E1", "E2", "E3", "E4"]
    base_ord = date(2025, 1, 1).toordinal()
    rng = np.random.default_rng(7)
    rows = []
    for idx, sym in enumerate(symbols):
        price = 100.0 + idx * 10  # distinct drift per symbol
        drift = 0.001 * (idx + 1)
        for day in range(200):
            d = date.fromordinal(base_ord + day)
            ts = int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())
            price *= math.exp(drift + rng.normal(0, 0.01))
            rows.append((sym, "NSE", "D", ts, price, price, price, price, 1000))
    con.executemany(
        "INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?,?)", rows
    )
    con.close()

    config = SectorRotationConfig(
        universe=symbols,
        momentum_lookback_days=126,
        lowvol_lookback_days=60,
        momentum_top_n=3,
        lowvol_bottom_n=3,
        capital_inr=300000.0,
    )
    result = compute_rebalance(config, date(2025, 7, 19), {}, db_path=db_path)

    for key in (
        "asof_date",
        "momentum_basket",
        "lowvol_basket",
        "momentum_weight",
        "lowvol_weight",
        "target_positions",
        "rebalance_orders",
        "diagnostics",
    ):
        assert key in result
    assert len(result["momentum_basket"]) == 3
    assert len(result["lowvol_basket"]) == 3
    assert result["momentum_weight"] + result["lowvol_weight"] == pytest.approx(1.0, abs=1e-3)
    # Fresh book -> all orders are BUYs.
    assert all(o["side"] == "BUY" for o in result["rebalance_orders"])
