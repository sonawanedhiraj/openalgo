"""Tests for realized CAGR / Sharpe / Max-DD computation (issue #568).

Pure maths — no DB, no Flask, no clock. Each test pins one of the guarantees
the dashboard depends on, with the emphasis on the ones that stop a *confident
wrong number* from reaching the UI:

  - insufficient history yields None (not 0.0, which reads as "measured and bad")
  - Sharpe is scale-invariant, so a soft capital basis cannot corrupt it
  - CAGR is withheld on short windows rather than extrapolated
  - drawdown is reported in rupees even when no capital basis exists
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from services.strategy_performance_metrics import (
    MIN_TRADING_DAYS_CAGR,
    MIN_TRADING_DAYS_SHARPE,
    TRADING_DAYS_PER_YEAR,
    compute_realized_metrics,
)

D0 = date(2026, 1, 1)


def _series(pnls: list[float], start: date = D0) -> list[tuple[date, float]]:
    """One entry per consecutive calendar day."""
    return [(start + timedelta(days=i), p) for i, p in enumerate(pnls)]


# ---------------------------------------------------------------------------
# Empty / degenerate input
# ---------------------------------------------------------------------------


def test_empty_series_returns_all_none_not_zero():
    """No trades must render '—', never a '0.0' that reads as a real result."""
    out = compute_realized_metrics([], capital_inr=20000)
    assert out["cagr_pct"] is None
    assert out["sharpe"] is None
    assert out["max_dd_pct"] is None
    assert out["max_dd_inr"] is None
    assert out["trading_days"] == 0
    assert "no closed trades" in out["notes"]


def test_none_entries_are_skipped_not_counted():
    out = compute_realized_metrics(
        [(D0, 100.0), (None, 50.0), (D0 + timedelta(days=1), None)],
        capital_inr=20000,
    )
    assert out["trading_days"] == 1


def test_malformed_series_degrades_instead_of_raising():
    """A bad series must not take down the whole dashboard endpoint."""
    out = compute_realized_metrics([("not-a-pair",)], capital_inr=20000)  # type: ignore[list-item]
    assert out["trading_days"] == 0
    assert out["notes"] == "malformed input series"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_duplicate_dates_are_summed_into_one_trading_day():
    """Callers may pass one entry per TRADE; days are the unit of the series."""
    out = compute_realized_metrics(
        [(D0, 100.0), (D0, 250.0), (D0 + timedelta(days=1), -50.0)],
        capital_inr=10000,
    )
    assert out["trading_days"] == 2
    # 300 net on 10k
    assert out["roc_pct"] == 3.0


def test_ordering_is_irrelevant():
    forward = compute_realized_metrics(_series([10.0, -5.0, 20.0]), capital_inr=1000)
    backward = compute_realized_metrics(
        list(reversed(_series([10.0, -5.0, 20.0]))), capital_inr=1000
    )
    assert forward["max_dd_inr"] == backward["max_dd_inr"]
    assert forward["roc_pct"] == backward["roc_pct"]


# ---------------------------------------------------------------------------
# Max drawdown
# ---------------------------------------------------------------------------


def test_max_drawdown_is_measured_from_the_running_peak():
    # cum: 100, 60, 160, 60  -> peak 160, trough after it 60 => -100
    out = compute_realized_metrics(_series([100.0, -40.0, 100.0, -100.0]), capital_inr=1000)
    assert out["max_dd_inr"] == -100.0
    assert out["max_dd_pct"] == -10.0


def test_drawdown_from_day_one_counts_against_a_zero_peak():
    """A book under water from its first trade has a real drawdown — measuring
    from a peak it never reached would report 0."""
    out = compute_realized_metrics(_series([-500.0, -200.0]), capital_inr=10000)
    assert out["max_dd_inr"] == -700.0


def test_monotonic_gains_have_no_drawdown():
    out = compute_realized_metrics(_series([10.0] * 30), capital_inr=1000)
    assert out["max_dd_inr"] == 0.0


def test_drawdown_in_rupees_survives_a_missing_capital_basis():
    """Sharpe and Max-DD(₹) do not need capital; only the % metrics do."""
    out = compute_realized_metrics(_series([100.0, -300.0] * 15), capital_inr=None)
    assert out["max_dd_inr"] is not None
    assert out["sharpe"] is not None
    assert out["max_dd_pct"] is None
    assert out["cagr_pct"] is None
    assert "positive capital basis" in out["notes"]


@pytest.mark.parametrize("cap", [0, -1, None])
def test_non_positive_capital_is_treated_as_absent(cap):
    out = compute_realized_metrics(_series([10.0] * 25), capital_inr=cap)
    assert out["capital_basis_inr"] is None
    assert out["max_dd_pct"] is None


# ---------------------------------------------------------------------------
# Sharpe
# ---------------------------------------------------------------------------


def test_sharpe_withheld_below_minimum_history():
    out = compute_realized_metrics(_series([10.0, -5.0] * 4), capital_inr=1000)
    assert out["trading_days"] < MIN_TRADING_DAYS_SHARPE
    assert out["sharpe"] is None
    assert f">={MIN_TRADING_DAYS_SHARPE}" in out["notes"]


def test_sharpe_is_scale_invariant_across_capital_bases():
    """The load-bearing property: a soft/wrong capital basis cannot corrupt
    Sharpe, which is why it is reported even when %-metrics are caveated."""
    series = _series([50.0, -20.0, 35.0, -10.0] * 8)
    a = compute_realized_metrics(series, capital_inr=20_000)
    b = compute_realized_metrics(series, capital_inr=10_000_000)
    assert a["sharpe"] == b["sharpe"]
    # ... while the capital-relative metric genuinely differs.
    assert a["max_dd_pct"] != b["max_dd_pct"]


def test_sharpe_undefined_on_zero_variance():
    """A constant series has no volatility; mean/0 must not divide-by-zero."""
    out = compute_realized_metrics(_series([25.0] * 30), capital_inr=1000)
    assert out["sharpe"] is None
    assert "zero variance" in out["notes"]


def test_sharpe_matches_hand_computed_annualisation():
    import math

    pnls = [100.0, -50.0, 75.0, -25.0, 60.0] * 5  # 25 days
    out = compute_realized_metrics(_series(pnls), capital_inr=10_000)
    n = len(pnls)
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1)  # sample stdev
    expected = round((mean / math.sqrt(var)) * math.sqrt(TRADING_DAYS_PER_YEAR), 2)
    assert out["sharpe"] == expected


def test_a_losing_book_reports_negative_sharpe_not_none():
    out = compute_realized_metrics(_series([-100.0, 20.0, -80.0, 10.0] * 8), capital_inr=10_000)
    assert out["sharpe"] is not None and out["sharpe"] < 0


# ---------------------------------------------------------------------------
# CAGR
# ---------------------------------------------------------------------------


def test_cagr_withheld_on_a_short_window():
    """The regression that matters: 41 days of the simplified engine's real
    sandbox P&L annualises to >700%. Withholding beats extrapolating."""
    out = compute_realized_metrics(_series([200.0] * 41), capital_inr=20_000)
    assert out["trading_days"] == 41 < MIN_TRADING_DAYS_CAGR
    assert out["cagr_pct"] is None
    assert "extrapolates noise" in out["notes"]
    # The window return IS honest and still reported: 41 x 200 = 8,200 on
    # 20,000 = +41.0% for the window (vs the >700% a naive annualisation of the
    # same series would print).
    assert out["roc_pct"] == 41.0


def test_cagr_reported_once_history_is_sufficient():
    out = compute_realized_metrics(_series([10.0] * MIN_TRADING_DAYS_CAGR), capital_inr=10_000)
    assert out["cagr_pct"] is not None


def test_cagr_matches_the_compounding_formula():
    n = TRADING_DAYS_PER_YEAR  # exactly one year of trading days
    out = compute_realized_metrics(_series([100.0] * n), capital_inr=10_000)
    # +25,200 on 10,000 over exactly 1 year => 252% growth
    assert out["cagr_pct"] == pytest.approx(252.0, abs=0.5)


def test_cagr_floors_at_minus_100_when_the_book_is_wiped_out():
    """(ending/cap)**(1/years) on a negative base would raise; -100% is the
    honest floor."""
    n = MIN_TRADING_DAYS_CAGR
    out = compute_realized_metrics(_series([-500.0] * n), capital_inr=10_000)
    assert out["cagr_pct"] == -100.0


# ---------------------------------------------------------------------------
# Notional-basis caveat
# ---------------------------------------------------------------------------


def test_notional_capital_flag_is_propagated_with_a_caveat():
    """R56 is explicit that the engine's ₹20,000 is a risk-sizing base, not a
    compounding book. The maths still runs, but the caveat must reach the UI."""
    out = compute_realized_metrics(
        _series([100.0, -50.0] * 15), capital_inr=20_000, capital_is_notional=True
    )
    assert out["capital_basis_is_notional"] is True
    assert "risk-sizing" in out["notes"]


def test_non_notional_basis_carries_no_caveat():
    out = compute_realized_metrics(
        _series([100.0, -50.0] * 15), capital_inr=20_000, capital_is_notional=False
    )
    assert out["capital_basis_is_notional"] is False
    assert "risk-sizing" not in out["notes"]


def test_runtime_notes_are_ascii_safe():
    """Notes are surfaced through logs and a Windows cp1252 console; a non-ASCII
    character there raised UnicodeEncodeError during development."""
    for kwargs in (
        {"capital_inr": None},
        {"capital_inr": 20_000, "capital_is_notional": True},
        {"capital_inr": 20_000},
    ):
        out = compute_realized_metrics(_series([10.0, -5.0] * 3), **kwargs)
        out["notes"].encode("ascii")  # raises if a non-ASCII char slipped in
