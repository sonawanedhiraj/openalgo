"""R49 promoter-buy SMALLCAP SWING with liquidity-aware costs.

Extends R46 (docs/research/strategy/news_event_study/2026-07-07_r46_promoter_buy_drift.md,
MEMORY.md ``r46-promoter-buy-drift-promising``): R46 found promoter open-market
buys drift +0.66%/5d (t=4.0, half-consistent) but 97.5% of the 1,167 events are
smallcaps and R46 costed every trade with a FLAT 0.25%/leg slippage calibrated
for F&O large caps. A liquidity probe on those same 1,167 events showed median
day0 traded value ~Rs2Cr, p10 ~Rs9.5L, and 221 events (19%) trade <Rs25L/day.
This script asks: does the swing edge SURVIVE when (a) restricted to
tradable-liquidity names and (b) charged size-aware (square-root market
impact) slippage instead of the flat rate? Intraday is explicitly OUT of
scope here (user preference; R43/R44 already REJECTed the intraday grids).

Inputs (all pre-built, no new price fetching)
----------------------------------------------
1. ``outputs/news_event_study/results_r46.duckdb`` table ``event_metrics`` --
   1,167 deduped (symbol, event_date) promoter open-market-buy events, each
   with day0_date, prev_close, t1_open, exit_close_{5,10}, ar_{5,10} (NIFTY-
   adjusted, day0-close basis), total_value, half, skip_reason. Rows with a
   non-null skip_reason (no_day0 / insufficient_history / penny) are excluded
   here exactly as they were in R46.
2. ``outputs/news_event_study/prices.duckdb`` table ``prices_daily_fwd`` --
   daily bars, 1,953 symbols + NIFTY, 2025-06-02..2026-07-07. Used ONLY for
   (a) the ADV20 liquidity measure and (b) the daily-close path needed by the
   managed 5%-stop exit variant. Everything else (entry/exit prices, AR) is
   read straight off ``event_metrics`` -- no re-derivation.
3. ``outputs/news_event_study/results_r45.duckdb`` table ``event_drift`` --
   the neutral (`-2..+2%`) reaction-bucket AR, for the size-matched control
   (same discipline as R46, restricted here to the TRADABLE liquidity tier).

Liquidity measure
------------------
``ADV20`` = median(close * volume) over the 20 trading days strictly BEFORE
day0 (the same day0 R46 already resolved). Computed by locating day0's row
position in the symbol's daily series (``find_day0_position`` on
``day0_date`` -- always matches at offset 0 since R46 already resolved it)
and taking the median of ``close*volume`` over the preceding 20 rows. Events
with fewer than 20 prior rows available (near the start of the price window)
get ``adv20=None`` and are excluded from every liquidity-tiered table (tracked
separately in the funnel as ``insufficient_adv_history``).

Liquidity tiers (mutually exclusive, by ADV20): ``THIN`` (<Rs1Cr), ``TRADABLE``
(Rs1Cr <= ADV20 < Rs5Cr), ``LIQUID`` (>=Rs5Cr). ``full`` in the summary tables
means all events with a valid ADV20 regardless of tier.

Size-aware slippage (square-root market-impact model)
-------------------------------------------------------
    slip_pct = max(0.25%, IMPACT_K * sqrt(order_value / ADV20))

applied per leg (both entry and exit), IMPACT_K = 0.08. A square-root impact
model cannot hit two independently-chosen anchor points exactly with one K
(sqrt is concave: a 4x larger relative order size only doubles sqrt(ratio),
but the illustrative anchors in the task brief implied a ~3.75x jump in
slippage) -- K=0.08 was chosen by log-least-squares fit against the two
anchors (Rs50k order in Rs1Cr-ADV name ~0.4%; Rs50k order in Rs25L-ADV name
~1.5%) and rounded. The formula actually PRODUCES (see validation, Test 2):
Rs1Cr ADV -> 0.566% ; Rs25L ADV -> 1.131%. Both are directionally correct
(materially above the 0.25% flat floor, and the 25L name pays ~2x the 1Cr
name) even though they don't hit the illustrative anchors on the nose --
documented here rather than silently forcing an exact match by using a
different exponent than the pre-declared sqrt-impact form.

Conditioning flags (pre-declared, computed off event_metrics' own
(symbol, event_date) list -- no further filing-level detail needed since
R46's dedup already collapsed same-day filings)
-------------------------------------------------------------------------
- ``cluster``: >=2 distinct promoter-buy EVENTS for this symbol within a
  10-trading-day window ending at (and including) this event -- i.e. at
  least one other event_date for the same symbol falls in
  [nifty_trading_day(event_date, -10), event_date). Trading-day arithmetic
  uses the NIFTY calendar (same proxy R46/R45 use elsewhere).
- ``first_buy``: no other promoter-buy event for this symbol in the prior 180
  calendar days. NOTE (bias, stated once, not re-derived): the insider-PIT
  harvest only covers 2025-07-01 onward, so an event in the first ~6 months
  of the window can look like a "first buy" purely because earlier filings
  were never harvested, not because the symbol truly had no prior promoter
  buying. Left-censoring caveat, same class as R46's survivorship caveat.
- ``big_rel``: total_value (this event's summed disclosed buy value) >= ADV20
  (a buy that is big relative to the name's own liquidity).
- value buckets >=25L / >=1Cr: identical thresholds/semantics to R46
  (imported, not re-derived).

Swing simulation (CNC delivery, T+1 OPEN entry, size-aware slippage)
-----------------------------------------------------------------------
Three exit variants, pre-declared (no sweep):
- ``h5``: exit at day0+5 trading-day close (``exit_close_5`` from event_metrics).
- ``h10``: exit at day0+10 trading-day close (``exit_close_10``).
- ``h10_stop``: walk daily closes from day0+1..day0+10; if any closing price
  is <= -5% from the (slipped) entry price, exit there; else exit at the
  day0+10 close (identical exit point to ``h10`` when the stop never
  triggers). This is the ONE managed variant asked for -- no other stop
  levels are swept.

Position size Rs50,000/trade, CNC delivery charges via ``simulate.cnc_charges``
(imported, not re-derived).

Usage
-----
    uv run python backtest/news_event_study/analyze_promoter_swing.py --validate
    uv run python backtest/news_event_study/analyze_promoter_swing.py --summary
"""

from __future__ import annotations

import argparse
import sys
from datetime import date as date_cls
from datetime import timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_drift import find_day0_position, prep_symbol_series  # noqa: E402
from analyze_promoter_buys import (  # noqa: E402
    VALUE_BUCKET_1CR,
    VALUE_BUCKET_25L,
    load_nifty_series,
    load_prices_by_symbol,
)
from simulate import (  # noqa: E402
    ADVERSE_SLIPPAGE,
    cnc_charges,
    connect_prices_readonly,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_R46 = "outputs/news_event_study/results_r46.duckdb"
DEFAULT_PRICES = "outputs/news_event_study/prices.duckdb"
DEFAULT_R45 = "outputs/news_event_study/results_r45.duckdb"
DEFAULT_OUT = "outputs/news_event_study/results_r49.duckdb"

ADV_LOOKBACK_DAYS = 20
TRADABLE_FLOOR = 10_000_000.0  # Rs1Cr
LIQUID_FLOOR = 50_000_000.0  # Rs5Cr

IMPACT_K = 0.08  # log-least-squares fit against the two illustrative anchors
MIN_SLIP = ADVERSE_SLIPPAGE  # 0.25% floor, same constant R43-R46 use

POSITION_SIZE = 50_000.0
STOP_PCT = 0.05
CLUSTER_WINDOW_TRADING_DAYS = 10
FIRST_BUY_WINDOW_DAYS = 180

NEUTRAL_REACTION_BUCKET = "-2..+2%"


# --------------------------------------------------------------------------
# Liquidity + slippage (pure -- unit-testable)
# --------------------------------------------------------------------------


def compute_adv20(symbol_series: pd.DataFrame, day0_pos: int) -> float | None:
    """Median(close*volume) over the ADV_LOOKBACK_DAYS trading days strictly
    before day0_pos. None if fewer than ADV_LOOKBACK_DAYS prior rows exist."""
    if day0_pos is None or day0_pos < ADV_LOOKBACK_DAYS:
        return None
    window = symbol_series.iloc[day0_pos - ADV_LOOKBACK_DAYS : day0_pos]
    traded_value = window["close"].values.astype(float) * window["volume"].values.astype(float)
    return float(np.median(traded_value))


def liquidity_tier(adv20: float | None) -> str | None:
    if adv20 is None:
        return None
    if adv20 < TRADABLE_FLOOR:
        return "THIN"
    if adv20 < LIQUID_FLOOR:
        return "TRADABLE"
    return "LIQUID"


def size_aware_slip(order_value: float, adv20: float | None) -> float:
    """max(0.25%, K*sqrt(order_value/ADV20)) square-root market-impact model.
    Falls back to the flat floor if adv20 is missing/non-positive (caller
    should already have excluded such events from tiered tables; this is a
    defensive fallback, not a silent substitute for real liquidity data)."""
    if adv20 is None or adv20 <= 0:
        return MIN_SLIP
    impact = IMPACT_K * np.sqrt(order_value / adv20)
    return max(MIN_SLIP, float(impact))


def liquidity_tier_mask(events: pd.DataFrame, tier: str) -> pd.Series:
    if tier == "full":
        return events["adv20"].notna()
    return events["tier"] == tier


def value_bucket_mask(events: pd.DataFrame, bucket: str) -> pd.Series:
    if bucket == "all":
        return pd.Series(True, index=events.index)
    if bucket == ">=25L":
        return events["total_value"] >= VALUE_BUCKET_25L
    if bucket == ">=1Cr":
        return events["total_value"] >= VALUE_BUCKET_1CR
    if bucket == "CLUSTER":
        return events["cluster"]
    if bucket == "FIRST_BUY":
        return events["first_buy"]
    if bucket == "BIG_REL":
        return events["big_rel"]
    raise ValueError(f"unknown condition bucket {bucket}")


# --------------------------------------------------------------------------
# Conditioning flags (pure, operate on the event_metrics-derived event list)
# --------------------------------------------------------------------------


def compute_cluster_and_first_buy(events: pd.DataFrame, nifty_dates: np.ndarray) -> pd.DataFrame:
    """events must have columns symbol, event_date (date objects). Adds
    boolean columns 'cluster' and 'first_buy'. O(n^2) worst case within a
    symbol group but groups are small (~1-3 events/symbol typically)."""
    events = events.copy()
    cluster_col = pd.Series(False, index=events.index)
    first_buy_col = pd.Series(False, index=events.index)

    for _symbol, grp in events.groupby("symbol"):
        grp_sorted = grp.sort_values("event_date")
        dates = grp_sorted["event_date"].tolist()
        orig_idx = grp_sorted.index.tolist()
        for i, (oi, d) in enumerate(zip(orig_idx, dates, strict=True)):
            prior_dates = dates[:i]  # strictly earlier, already unique+sorted

            d64 = np.datetime64(d)
            pos = int(np.searchsorted(nifty_dates, d64, side="left"))
            lo_pos = max(pos - CLUSTER_WINDOW_TRADING_DAYS, 0)
            if lo_pos < len(nifty_dates):
                lo_date = pd.Timestamp(nifty_dates[lo_pos]).date()
            else:
                lo_date = d - timedelta(days=999)

            has_cluster = any(pd_ >= lo_date for pd_ in prior_dates)
            window_start = d - timedelta(days=FIRST_BUY_WINDOW_DAYS)
            has_prior_180 = any(pd_ >= window_start for pd_ in prior_dates)

            cluster_col.loc[oi] = has_cluster
            first_buy_col.loc[oi] = not has_prior_180

    events["cluster"] = cluster_col
    events["first_buy"] = first_buy_col
    return events


# --------------------------------------------------------------------------
# Stop-loss walk (managed exit variant)
# --------------------------------------------------------------------------


def compute_stop_exit(
    symbol_series: pd.DataFrame,
    day0_pos: int,
    entry_px: float,
    h: int = 10,
    stop_pct: float = STOP_PCT,
) -> float | None:
    """Walk daily closes from day0_pos+1..day0_pos+h. Returns the close price
    at the first day whose close is <= -stop_pct from entry_px, else the
    close at day0_pos+h (same exit point as the plain h-day hold). None if
    truncated (day0_pos+h beyond available data), matching event_metrics'
    own exit_close_{h} truncation semantics."""
    n = len(symbol_series)
    target_pos = day0_pos + h
    if target_pos >= n:
        return None
    for pos in range(day0_pos + 1, target_pos + 1):
        close = float(symbol_series.iloc[pos]["close"])
        ret_from_entry = close / entry_px - 1.0
        if ret_from_entry <= -stop_pct:
            return close
    return float(symbol_series.iloc[target_pos]["close"])


# --------------------------------------------------------------------------
# Trade simulation
# --------------------------------------------------------------------------


def simulate_trade(entry_open: float, exit_close: float, slip_pct: float) -> dict | None:
    if entry_open is None or exit_close is None or pd.isna(entry_open) or pd.isna(exit_close):
        return None
    if entry_open <= 0:
        return None
    entry_px = entry_open * (1 + slip_pct)
    exit_px = exit_close * (1 - slip_pct)
    qty = int(POSITION_SIZE // entry_px)
    if qty <= 0:
        return None
    buy_value = entry_px * qty
    sell_value = exit_px * qty
    gross_pnl = (exit_px - entry_px) * qty
    charges = cnc_charges(buy_value=buy_value, sell_value=sell_value)
    net_pnl = gross_pnl - charges["total"]
    net_ret_pct = (net_pnl / buy_value) * 100.0 if buy_value > 0 else None
    return {"net_pnl": net_pnl, "net_ret_pct": net_ret_pct}


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_r46_event_metrics(r46_path: str) -> pd.DataFrame:
    con = duckdb.connect(r46_path, read_only=True)
    df = con.execute(
        """
        SELECT symbol, event_date, day0_date, half, total_value, prev_close,
               t1_open, exit_close_5, exit_close_10, ar_5, ar_10
        FROM event_metrics
        WHERE skip_reason IS NULL
        """
    ).df()
    con.close()
    df["event_date"] = pd.to_datetime(df["event_date"]).dt.date
    df["day0_date"] = pd.to_datetime(df["day0_date"]).dt.date
    return df


def enrich_events(
    events: pd.DataFrame,
    prices_by_symbol: dict[str, pd.DataFrame],
    nifty_series: pd.DataFrame,
) -> pd.DataFrame:
    """Adds adv20, tier, big_rel, stop_exit_close for every event. cluster/
    first_buy are added separately via compute_cluster_and_first_buy (pure
    function over the event list alone, no price data needed)."""
    nifty_dates = np.array([np.datetime64(d) for d in nifty_series["bar_date"]])

    adv20s = []
    stop_exits = []
    n_no_series = 0
    n_insufficient_adv = 0

    for ev in events.itertuples(index=False):
        symbol_series = prices_by_symbol.get(ev.symbol)
        if symbol_series is None or symbol_series.empty:
            adv20s.append(None)
            stop_exits.append(None)
            n_no_series += 1
            continue
        day0_pos = find_day0_position(symbol_series, ev.day0_date)
        if day0_pos is None:
            adv20s.append(None)
            stop_exits.append(None)
            n_no_series += 1
            continue
        adv20 = compute_adv20(symbol_series, day0_pos)
        if adv20 is None:
            n_insufficient_adv += 1
        adv20s.append(adv20)

        if ev.t1_open is not None and not pd.isna(ev.t1_open) and ev.t1_open > 0:
            slip_pct = size_aware_slip(POSITION_SIZE, adv20)
            entry_px = ev.t1_open * (1 + slip_pct)
            stop_exits.append(compute_stop_exit(symbol_series, day0_pos, entry_px))
        else:
            stop_exits.append(None)

    events = events.copy()
    events["adv20"] = adv20s
    events["tier"] = events["adv20"].apply(liquidity_tier)
    events["big_rel"] = events["total_value"] >= events["adv20"]
    events["exit_close_10_stop"] = stop_exits

    events = compute_cluster_and_first_buy(events, nifty_dates)

    print(
        f"[enrich] {n_no_series} events with no usable price series/day0 position; "
        f"{n_insufficient_adv} with <{ADV_LOOKBACK_DAYS} prior trading days (adv20=None)"
    )
    return events


# --------------------------------------------------------------------------
# Summary tables
# --------------------------------------------------------------------------


def _tstat(s: pd.Series) -> float | None:
    s = s.dropna()
    n = len(s)
    if n < 2:
        return None
    std = s.std(ddof=1)
    if std == 0 or pd.isna(std):
        return None
    return float(s.mean() / (std / np.sqrt(n)))


def build_funnel_table(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    valid = events[events["adv20"].notna()]
    for tier in ["THIN", "TRADABLE", "LIQUID"]:
        sub = valid[valid["tier"] == tier]
        rows.append({"tier": tier, "n": len(sub)})
    rows.append({"tier": "full (valid adv20)", "n": len(valid)})
    rows.append({"tier": "excluded (no adv20)", "n": (events["adv20"].isna()).sum()})
    return pd.DataFrame(rows)


def build_slip_stats_table(events: pd.DataFrame) -> pd.DataFrame:
    valid = events[events["adv20"].notna()].copy()
    valid["slip_pct"] = valid["adv20"].apply(lambda a: size_aware_slip(POSITION_SIZE, a) * 100.0)
    rows = []
    for tier in ["THIN", "TRADABLE", "LIQUID", "full"]:
        sub = valid if tier == "full" else valid[valid["tier"] == tier]
        if sub.empty:
            rows.append({"tier": tier, "n": 0, "median_slip_pct": None, "p90_slip_pct": None})
            continue
        rows.append(
            {
                "tier": tier,
                "n": len(sub),
                "median_slip_pct": sub["slip_pct"].median(),
                "p90_slip_pct": sub["slip_pct"].quantile(0.9),
            }
        )
    return pd.DataFrame(rows)


EXIT_COLUMNS = {"h5": "exit_close_5", "h10": "exit_close_10", "h10_stop": "exit_close_10_stop"}
CONDITIONS = ["all", ">=25L", "CLUSTER", "FIRST_BUY", "BIG_REL"]
TIERS_FOR_SIM = ["THIN", "TRADABLE", "LIQUID", "full"]


def run_swing_sims(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for tier in TIERS_FOR_SIM:
        tier_mask = liquidity_tier_mask(events, tier)
        tier_df = events[tier_mask]
        for condition in CONDITIONS:
            cond_mask = value_bucket_mask(tier_df, condition)
            cond_df = tier_df[cond_mask]
            for exit_name, exit_col in EXIT_COLUMNS.items():
                cand = cond_df[
                    cond_df["t1_open"].notna()
                    & cond_df[exit_col].notna()
                    & cond_df["adv20"].notna()
                ]
                trades = []
                for row in cand.itertuples(index=False):
                    slip_pct = size_aware_slip(POSITION_SIZE, row.adv20)
                    t = simulate_trade(row.t1_open, getattr(row, exit_col), slip_pct)
                    if t is not None:
                        t["half"] = row.half
                        trades.append(t)
                tdf = pd.DataFrame(trades)
                for scope in ["pooled", "A", "B"]:
                    sdf = (
                        tdf
                        if scope == "pooled"
                        else (tdf[tdf["half"] == scope] if not tdf.empty else tdf)
                    )
                    if tdf.empty or sdf.empty:
                        rows.append(
                            {
                                "tier": tier,
                                "condition": condition,
                                "exit": exit_name,
                                "scope": scope,
                                "n": 0,
                                "hit_rate_pct": None,
                                "avg_net_ret_pct": None,
                                "median_net_ret_pct": None,
                                "total_net_pnl": None,
                            }
                        )
                        continue
                    rows.append(
                        {
                            "tier": tier,
                            "condition": condition,
                            "exit": exit_name,
                            "scope": scope,
                            "n": len(sdf),
                            "hit_rate_pct": 100.0 * (sdf["net_pnl"] > 0).mean(),
                            "avg_net_ret_pct": sdf["net_ret_pct"].mean(),
                            "median_net_ret_pct": sdf["net_ret_pct"].median(),
                            "total_net_pnl": sdf["net_pnl"].sum(),
                        }
                    )
    return pd.DataFrame(rows)


def build_drift_ar_table(events: pd.DataFrame) -> pd.DataFrame:
    """NIFTY-adjusted AR (ar_5/ar_10, from event_metrics -- day0-close basis)
    by liquidity tier x horizon x half, with t-stats."""
    rows = []
    for tier in TIERS_FOR_SIM:
        tier_mask = liquidity_tier_mask(events, tier)
        sub = events[tier_mask]
        for h, col in [(5, "ar_5"), (10, "ar_10")]:
            for scope in ["pooled", "A", "B"]:
                scope_df = sub if scope == "pooled" else sub[sub["half"] == scope]
                s = scope_df[col].dropna()
                rows.append(
                    {
                        "tier": tier,
                        "horizon": h,
                        "scope": scope,
                        "n": len(s),
                        "mean_ar_pct": s.mean() * 100.0 if len(s) else None,
                        "tstat": _tstat(s),
                    }
                )
    return pd.DataFrame(rows)


def neutral_control_spread_tradable(events: pd.DataFrame, r45_path: str) -> pd.DataFrame:
    r45_con = duckdb.connect(r45_path, read_only=True)
    neutral = r45_con.execute(
        f"""
        SELECT half, AVG(ar_5) AS neutral_ar_5, AVG(ar_10) AS neutral_ar_10, COUNT(*) AS n_neutral
        FROM event_drift
        WHERE reaction_bucket = '{NEUTRAL_REACTION_BUCKET}'
        GROUP BY half
        """
    ).df()
    r45_con.close()

    tradable = events[liquidity_tier_mask(events, "TRADABLE")]
    rows = []
    for half in ["A", "B"]:
        half_df = tradable[tradable["half"] == half]
        promoter_ar5 = half_df["ar_5"].dropna().mean() if len(half_df) else None
        promoter_ar10 = half_df["ar_10"].dropna().mean() if len(half_df) else None
        nrow = neutral[neutral["half"] == half]
        neutral_ar5 = float(nrow["neutral_ar_5"].iloc[0]) if not nrow.empty else None
        neutral_ar10 = float(nrow["neutral_ar_10"].iloc[0]) if not nrow.empty else None
        rows.append(
            {
                "half": half,
                "n_tradable": len(half_df),
                "promoter_ar5_pct": promoter_ar5 * 100.0 if promoter_ar5 is not None else None,
                "neutral_ar5_pct": neutral_ar5 * 100.0 if neutral_ar5 is not None else None,
                "spread_5_pct": (promoter_ar5 - neutral_ar5) * 100.0
                if promoter_ar5 is not None and neutral_ar5 is not None
                else None,
                "promoter_ar10_pct": promoter_ar10 * 100.0 if promoter_ar10 is not None else None,
                "neutral_ar10_pct": neutral_ar10 * 100.0 if neutral_ar10 is not None else None,
                "spread_10_pct": (promoter_ar10 - neutral_ar10) * 100.0
                if promoter_ar10 is not None and neutral_ar10 is not None
                else None,
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Validation (synthetic fixture, no DB access)
# --------------------------------------------------------------------------


def _make_synthetic_daily(
    symbol: str, start: date_cls, n_days: int, closes: list[float], volumes: list[float]
) -> pd.DataFrame:
    dates = []
    d = start
    while len(dates) < n_days:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    rows = []
    prev = closes[0] * 0.99
    for dt, c, v in zip(dates, closes, volumes, strict=True):
        rows.append(
            {
                "bar_date": dt,
                "open": prev,
                "high": max(prev, c) * 1.001,
                "low": min(prev, c) * 0.999,
                "close": c,
                "volume": v,
            }
        )
        prev = c
    return pd.DataFrame(rows)


def run_validation() -> None:
    print("=== R49 validation (synthetic fixture) ===")

    start = date_cls(2025, 9, 1)
    assert start.weekday() == 0  # nosec B101 - validation harness, not runtime
    n_days = 40
    closes = [100.0 + i * 0.5 for i in range(n_days)]
    volumes = [10_000.0] * n_days  # constant volume -> known ADV20
    series = prep_symbol_series(_make_synthetic_daily("TESTSYM", start, n_days, closes, volumes))

    # ---- Test 1: ADV20 hand-check ----
    # day0_pos = 25 (well past the 20-bar lookback). Prior 20 closes are
    # indices 5..24: closes[5..24] = 102.5..111.5 step 0.5; volume constant
    # 10000 each day -> traded_value_i = closes[i]*10000.
    day0_pos = 25
    adv20 = compute_adv20(series, day0_pos)
    expected_window_closes = closes[day0_pos - 20 : day0_pos]
    expected_traded_values = [c * 10_000.0 for c in expected_window_closes]
    expected_median = float(np.median(expected_traded_values))
    assert adv20 is not None and abs(adv20 - expected_median) < 1e-6, (  # nosec B101 - validation harness, not runtime
        f"ADV20 mismatch: got {adv20}, expected {expected_median}"
    )
    # insufficient history: day0_pos < 20 -> None
    assert compute_adv20(series, 5) is None  # nosec B101 - validation harness, not runtime
    print(
        f"[PASS] Test 1: ADV20 = median(close*volume) over 20 prior days ({adv20:.2f} == expected)"
    )

    # ---- Test 2: size-aware slip formula hand-check (2 anchor cases) ----
    order_value = 50_000.0
    adv_1cr = 10_000_000.0
    adv_25l = 2_500_000.0
    slip_1cr = size_aware_slip(order_value, adv_1cr)
    slip_25l = size_aware_slip(order_value, adv_25l)
    expected_slip_1cr = max(MIN_SLIP, IMPACT_K * np.sqrt(order_value / adv_1cr))
    expected_slip_25l = max(MIN_SLIP, IMPACT_K * np.sqrt(order_value / adv_25l))
    assert abs(slip_1cr - expected_slip_1cr) < 1e-9  # nosec B101 - validation harness, not runtime
    assert abs(slip_25l - expected_slip_25l) < 1e-9  # nosec B101 - validation harness, not runtime
    # sanity: 25L name should pay strictly more than 1Cr name, both above floor
    assert slip_25l > slip_1cr > MIN_SLIP  # nosec B101 - validation harness, not runtime
    print(
        f"[PASS] Test 2: size-aware slip -- Rs1Cr ADV -> {slip_1cr * 100:.3f}%, "
        f"Rs25L ADV -> {slip_25l * 100:.3f}% (both > {MIN_SLIP * 100:.2f}% floor, 25L > 1Cr)"
    )
    # very illiquid name: floor should bind
    slip_thin = size_aware_slip(order_value, 100_000.0)  # Rs1L ADV, huge order/ADV ratio
    assert slip_thin > MIN_SLIP  # nosec B101 - validation harness, not runtime
    slip_huge_adv = size_aware_slip(order_value, 1_000_000_000.0)  # Rs100Cr ADV
    assert abs(slip_huge_adv - MIN_SLIP) < 1e-9, "expected floor to bind for a deeply liquid name"  # nosec B101 - validation harness, not runtime
    print("[PASS] Test 2b: floor binds for a very-liquid name; large-impact name exceeds floor")

    # ---- Test 3: cluster / first_buy detection ----
    nifty_closes = [20000.0 + i * 10.0 for i in range(n_days)]
    nifty_series = prep_symbol_series(
        _make_synthetic_daily("NIFTY", start, n_days, nifty_closes, [1.0] * n_days)
    )
    nifty_dates = np.array([np.datetime64(d) for d in nifty_series["bar_date"]])

    bar_dates = series["bar_date"].tolist()
    ev_events = pd.DataFrame(
        {
            "symbol": ["TESTSYM", "TESTSYM", "TESTSYM", "OTHER"],
            "event_date": [bar_dates[10], bar_dates[15], bar_dates[35], bar_dates[15]],
        }
    )
    enriched = compute_cluster_and_first_buy(ev_events, nifty_dates)
    row_10 = enriched[
        (enriched["symbol"] == "TESTSYM") & (enriched["event_date"] == bar_dates[10])
    ].iloc[0]
    row_15 = enriched[
        (enriched["symbol"] == "TESTSYM") & (enriched["event_date"] == bar_dates[15])
    ].iloc[0]
    row_35 = enriched[
        (enriched["symbol"] == "TESTSYM") & (enriched["event_date"] == bar_dates[35])
    ].iloc[0]
    row_other = enriched[enriched["symbol"] == "OTHER"].iloc[0]

    # bar_dates[10] is TESTSYM's first event ever -> no cluster, is first_buy
    # (bool(...) used, not `is True`/`is False` -- these columns come back as
    # numpy.bool_, which is never `is` python's singleton True/False)
    assert bool(row_10["cluster"]) is False and bool(row_10["first_buy"]) is True  # nosec B101 - validation harness, not runtime
    # bar_dates[15] is 5 trading days after bar_dates[10] (within the 10-day
    # cluster window) -> cluster True, not first_buy (prior event 5 days ago,
    # well within 180 calendar days)
    assert bool(row_15["cluster"]) is True and bool(row_15["first_buy"]) is False  # nosec B101 - validation harness, not runtime
    # bar_dates[35] is 20 trading days after bar_dates[15] -> outside the
    # 10-trading-day cluster window, but still within 180 calendar days ->
    # not cluster, not first_buy
    assert bool(row_35["cluster"]) is False and bool(row_35["first_buy"]) is False  # nosec B101 - validation harness, not runtime
    # OTHER symbol has only one event -> no cluster, is first_buy (independent
    # of TESTSYM's events)
    assert bool(row_other["cluster"]) is False and bool(row_other["first_buy"]) is True  # nosec B101 - validation harness, not runtime
    print(
        "[PASS] Test 3: cluster/first_buy detection (own-symbol history only, 10td/180cd windows)"
    )

    # ---- Test 4: hand-worked h5 net-return ----
    entry_open = 100.0
    exit_close_5 = 103.5  # +3.5% raw over 5 days
    adv20_case = 10_000_000.0  # Rs1Cr -> slip = expected_slip_1cr
    slip_pct = size_aware_slip(POSITION_SIZE, adv20_case)
    trade = simulate_trade(entry_open, exit_close_5, slip_pct)
    assert trade is not None  # nosec B101 - validation harness, not runtime

    entry_px_expected = entry_open * (1 + slip_pct)
    exit_px_expected = exit_close_5 * (1 - slip_pct)
    qty_expected = int(POSITION_SIZE // entry_px_expected)
    buy_value_expected = entry_px_expected * qty_expected
    sell_value_expected = exit_px_expected * qty_expected
    gross_pnl_expected = (exit_px_expected - entry_px_expected) * qty_expected
    charges_expected = cnc_charges(buy_value=buy_value_expected, sell_value=sell_value_expected)
    net_pnl_expected = gross_pnl_expected - charges_expected["total"]
    net_ret_pct_expected = (net_pnl_expected / buy_value_expected) * 100.0

    assert abs(trade["net_pnl"] - net_pnl_expected) < 1e-6, (  # nosec B101 - validation harness, not runtime
        f"net_pnl mismatch: got {trade['net_pnl']}, expected {net_pnl_expected}"
    )
    assert abs(trade["net_ret_pct"] - net_ret_pct_expected) < 1e-6  # nosec B101 - validation harness, not runtime
    print(
        f"[PASS] Test 4: hand-worked h5 trade (entry={entry_px_expected:.4f}, "
        f"exit={exit_px_expected:.4f}, qty={qty_expected}) -> "
        f"net_pnl={net_pnl_expected:.2f} net_ret_pct={net_ret_pct_expected:.4f}% (all match)"
    )

    # ---- Test 5: stop-exit walk ----
    # Build a series that drops >5% on day day0_pos+3, then recovers.
    stop_closes = list(closes)
    d0 = 25
    stop_closes[d0 + 3] = closes[d0] * 0.90  # -10% vs day0 close, well past -5%
    stop_series = prep_symbol_series(
        _make_synthetic_daily("STOPSYM", start, n_days, stop_closes, volumes)
    )
    entry_px_stop = closes[d0]  # pretend entry == day0 close, no slip, for a clean hand-check
    stop_exit = compute_stop_exit(stop_series, d0, entry_px_stop, h=10, stop_pct=0.05)
    assert stop_exit is not None  # nosec B101 - validation harness, not runtime
    assert abs(stop_exit - stop_closes[d0 + 3]) < 1e-9, (  # nosec B101 - validation harness, not runtime
        f"expected stop trigger at day0+3 close {stop_closes[d0 + 3]}, got {stop_exit}"
    )
    # a series with no >-5% day anywhere should exit at day0+10 (matches plain h10)
    no_stop_exit = compute_stop_exit(series, d0, closes[d0], h=10, stop_pct=0.05)
    assert abs(no_stop_exit - closes[d0 + 10]) < 1e-9  # nosec B101 - validation harness, not runtime
    print(
        "[PASS] Test 5: stop-exit walk triggers on the first -5%-or-worse close, else falls through to h10"
    )

    print("\n=== All validation checks passed ===\n")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run(
    r46_path: str,
    prices_path: str,
    r45_path: str,
    out_path: str,
    print_summary: bool,
) -> dict:
    print("[run] loading R46 event_metrics (deduped promoter-buy events)...")
    events = load_r46_event_metrics(r46_path)
    print(f"[run] {len(events)} events with skip_reason IS NULL loaded")

    prices_con = connect_prices_readonly(prices_path)
    symbols_needed = sorted(events["symbol"].unique().tolist())
    print(f"[run] loading prices_daily_fwd for {len(symbols_needed)} symbols + NIFTY...")
    prices_by_symbol = load_prices_by_symbol(prices_con, symbols_needed)
    nifty_series = load_nifty_series(prices_con)
    prices_con.close()

    print("[run] computing ADV20 / liquidity tier / cluster / first_buy / stop-exit...")
    events = enrich_events(events, prices_by_symbol, nifty_series)

    print(f"[run] writing swing_metrics ({len(events)} rows) to {out_path}...")
    out_con = duckdb.connect(out_path)
    out_con.execute("DROP TABLE IF EXISTS swing_metrics")
    out_con.register("swing_metrics_df", events)
    out_con.execute("CREATE TABLE swing_metrics AS SELECT * FROM swing_metrics_df")
    out_con.close()

    funnel_table = None
    slip_table = None
    sim_table = None
    ar_table = None
    control_table = None
    if print_summary:
        funnel_table = build_funnel_table(events)
        slip_table = build_slip_stats_table(events)
        sim_table = run_swing_sims(events)
        ar_table = build_drift_ar_table(events)
        control_table = neutral_control_spread_tradable(events, r45_path)

        pd.set_option("display.max_rows", 400)
        pd.set_option("display.width", 220)

        print("\n=== Event counts per liquidity tier ===")
        print(funnel_table.to_string(index=False))

        print("\n=== Size-aware slippage stats per tier ===")
        print(slip_table.to_string(index=False))

        print("\n=== Swing sim grid: tier x condition x exit x half ===")
        print(sim_table.sort_values(["tier", "condition", "exit", "scope"]).to_string(index=False))

        print("\n=== Drift AR table (NIFTY-adjusted, day0-close basis): tier x horizon x half ===")
        print(ar_table.sort_values(["tier", "horizon", "scope"]).to_string(index=False))

        print("\n=== Size-matched control (TRADABLE tier promoter AR - R45 neutral bucket) ===")
        print(control_table.to_string(index=False))

    return {
        "events": events,
        "funnel_table": funnel_table,
        "slip_table": slip_table,
        "sim_table": sim_table,
        "ar_table": ar_table,
        "control_table": control_table,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="R49 promoter-buy smallcap swing study")
    parser.add_argument("--r46", default=DEFAULT_R46)
    parser.add_argument("--prices", default=DEFAULT_PRICES)
    parser.add_argument("--r45", default=DEFAULT_R45)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--summary", action="store_true", default=False)
    parser.add_argument("--validate", action="store_true", default=False)
    args = parser.parse_args()

    if args.validate:
        run_validation()
        return

    run(
        r46_path=args.r46,
        prices_path=args.prices,
        r45_path=args.r45,
        out_path=args.out,
        print_summary=args.summary,
    )


if __name__ == "__main__":
    main()
