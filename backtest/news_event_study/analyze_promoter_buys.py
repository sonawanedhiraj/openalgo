"""R46 promoter open-market-buy insider-disclosure drift study.

R45 found that pooled multi-day PEAD drift is a smallcap-regime artifact (every
reaction/vol-confirm bucket flips sign between halves; see
`docs/research/journal` and MEMORY.md `r45-news-drift-regime-artifact`). R45
flagged the SAST promoter-direction hypothesis as UNTESTED because the SAST
filing corpus only gives DIRECTION (buy/sell/pledge) via a boilerplate-prone
keyword classifier, not a clean, PIT-sourced, value-bucketed signal. This
script tests the sharper hypothesis directly using the dedicated NSE
insider-PIT (Prohibition of Insider Trading, Regulation 7(2)) disclosure feed:
does a PUBLICLY-DISCLOSED promoter open-market purchase predict abnormal
drift, and does it survive a same-universe (not just NIFTY) benchmark?

Inputs
------
1. ``outputs/news_event_study/announcements.duckdb`` table ``insider_pit`` --
   11,061 rows, 2025-07-01..2026-07-07(harvest window); mirrors the NSE
   corporates-pit JSON. Verified real-data enum values (see module docstring
   footnotes for the exploratory queries this was based on):
   ``person_category`` in {'Promoters', 'Promoter Group', '-', 'Other',
   'Employees/Designated Employees', 'Director', 'Immediate relative', 'Key
   Managerial Personnel'}; ``transaction_type`` in {'Buy', 'Sell', 'Pledge',
   'Pledge Revoke', '-', 'Pledge Invoke'}; ``acq_mode`` in {'Market Purchase',
   'Market Sale', 'ESOP', 'Off Market', 'Pledge Creation', '-', 'Gift',
   'Inter-se-Transfer', 'Preferential Offer', 'Revokation of Pledge',
   'Others', 'Conversion of security', 'Scheme of Amalgamation/Merger/
   Demerger/Arrangement', 'Invocation of pledge', 'Public Right', 'Bonus'}.
   These are clean enums -- no regex/keyword classification needed (unlike
   R45's SAST summary_text classifier).
2. ``outputs/news_event_study/prices.duckdb`` table ``prices_daily_fwd`` --
   daily bars for 1,953 symbols + NIFTY, 2025-06-02..2026-07-07.
   ``fetch_fwd_log`` has 131 rows with status='error' -- symbols NSE/insider-
   only that failed to fetch (SME/BSE-only/delisted per the task's framing).
   Events on those symbols are EXCLUDED and counted, not retried.
3. ``outputs/news_event_study/results_r45.duckdb`` table ``event_drift`` --
   used ONLY for the size-matched control: the neutral reaction_bucket
   ('-2..+2%') same-half mean ar_10/ar_20 is the benchmark promoter-buy AR
   must beat (same universe as R45's news events, not just raw NIFTY beta).

Public event-date resolution
-----------------------------
``broadcast_at`` (NSE's ``date`` field -- when the filing was actually
disclosed to the exchange/public) is ALWAYS non-null in the real data (0
nulls across all 11,061 rows) and is often days after the underlying trade
window (``acq_from_date``/``acq_to_date``) due to the T+2 disclosure
requirement -- e.g. a trade on 25-Jun-2025 disclosed 01-Jul-2025 17:16. This
script uses ``broadcast_at`` as THE public date (not ``intimation_date``,
which has 40 nulls in the filtered population and is a step further from
"when the market could have known"). Rule: if ``broadcast_at.time() >=
15:25:00`` -> next calendar day; else same calendar day. That candidate date
is then rolled forward to the next date present in the NIFTY trading
calendar (``prices_daily_fwd`` symbol='NIFTY') if it falls on a non-trading
day (weekend/holiday) -- this is a distinct, unconditional roll from the
per-symbol day0 roll below (a symbol-specific halt/no-1m-data gap), which is
handled separately and identically to R45's ``find_day0_position``.

Distinct-event unit + value dedup
-----------------------------------
"One event per (symbol, event_date)" per the task spec: multiple insider-PIT
filing rows sharing (symbol, event_date) after the above resolution are
collapsed into one event row, ``total_value`` = SUM(sec_val), ``total_qty`` =
SUM(sec_acq_qty), ``n_filings`` = row count. This is necessary in practice:
2,870 raw promoter/buy/market-purchase rows collapse to ~1,795 distinct
(symbol, calendar broadcast-day) pairs even before the trading-day roll.

Drift + AR
----------
Reuses the exact per-event/per-horizon mechanics from ``analyze_drift.py``
(R45): ``prep_symbol_series``, ``find_day0_position`` (per-symbol roll-
forward up to 3 calendar days for a missing/halted day0 bar),
``nifty_close_asof`` (last NIFTY close on/before a target date). HORIZONS
here are {5, 10, 20, 40, 60} trading days (not R45's {1,2,3,5,10,15,20} --
this is a slower, PEAD-style hypothesis so short horizons are less
interesting). For each horizon h this script computes FOUR numbers per
event: raw return and NIFTY-adjusted AR, each from two entry references --
event-day (day0) close, and T+1 open (the earliest actually-tradeable entry,
since the disclosure is only public after day0 close in the base case). The
NIFTY-adjustment term for both entry variants uses the SAME close[day0]->
close[day0+h] market window (documented simplification carried over from
R45 -- avoids relying on NIFTY's own T+1 open).

Penny floor: prev_close (last close strictly before day0) >= 50, same as
R45/R43.

Size-matched control
---------------------
For each half (A: event_date < 2026-01-01, B: >=), this script reads
``results_r45.duckdb``'s ``event_drift`` table's rows with
``reaction_bucket = '-2..+2%'`` (the "nothing happened on the news tape"
neutral bucket -- same universe of underlying stocks/dates as R45, not a
different index) and computes that bucket's mean ar_10/ar_20 for the same
half. The spread reported is: promoter-buy pooled mean AR@10/@20 (this
script's own day0-close AR) MINUS that neutral-bucket mean, per half. A real
promoter effect must clear this same-universe neutral drift, not just raw
NIFTY beta -- this is the exact guard the R45 SAST-hypothesis follow-up was
missing (see MEMORY.md ``r45-news-drift-regime-artifact``).

Sims (pre-declared, no mining)
--------------------------------
S1: value bucket >=25L (>=2,500,000), hold 20 trading days.
S2: value bucket >=25L, hold 40 trading days.
Both: CNC long, POSITION_SIZE=50000, entry T+1 open, 0.25%/leg adverse
slippage, CNC delivery charges via ``simulate.cnc_charges`` (imported, not
re-derived -- same trade-costing convention as R43/R44/R45).

Usage
-----
    uv run python backtest/news_event_study/analyze_promoter_buys.py --validate
    uv run python backtest/news_event_study/analyze_promoter_buys.py --summary
"""

from __future__ import annotations

import argparse
import sys
from datetime import date as date_cls
from datetime import time as time_cls
from datetime import timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_drift import (  # noqa: E402
    find_day0_position,
    nifty_close_asof,
    prep_symbol_series,
)
from simulate import (  # noqa: E402
    ADVERSE_SLIPPAGE,
    cnc_charges,
    connect_announcements_readonly,
    connect_prices_readonly,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_ANNOUNCEMENTS = "outputs/news_event_study/announcements.duckdb"
DEFAULT_PRICES = "outputs/news_event_study/prices.duckdb"
DEFAULT_R45 = "outputs/news_event_study/results_r45.duckdb"
DEFAULT_OUT = "outputs/news_event_study/results_r46.duckdb"

HORIZONS = [5, 10, 20, 40, 60]
INDEX_SYMBOL = "NIFTY"

PUBLIC_CUTOFF_TIME = time_cls(15, 25, 0)
PENNY_FLOOR = 50.0
HALF_SPLIT_DATE = date_cls(2026, 1, 1)
POSITION_SIZE = 50000.0

VALUE_BUCKET_25L = 2_500_000.0
VALUE_BUCKET_1CR = 10_000_000.0

PROMOTER_CATEGORIES = ("Promoters", "Promoter Group")
BUY_TRANSACTION = "Buy"
MARKET_PURCHASE_MODE = "Market Purchase"

NEUTRAL_REACTION_BUCKET = "-2..+2%"


# --------------------------------------------------------------------------
# Event-date resolution
# --------------------------------------------------------------------------


def resolve_public_event_date(broadcast_at: pd.Timestamp, nifty_dates: np.ndarray) -> date_cls:
    """Public disclosure date -> event_date, per module docstring rule:
    >=15:25 IST rolls to next calendar day; then roll forward (if needed) to
    the next date present in the NIFTY trading calendar. `nifty_dates` must
    be sorted ascending np.datetime64 values."""
    raw_date = broadcast_at.date()
    if broadcast_at.time() >= PUBLIC_CUTOFF_TIME:
        candidate = raw_date + timedelta(days=1)
    else:
        candidate = raw_date

    cand64 = np.datetime64(candidate)
    idx = int(np.searchsorted(nifty_dates, cand64, side="left"))
    if idx < len(nifty_dates) and nifty_dates[idx] == cand64:
        return candidate
    if idx >= len(nifty_dates):
        # Beyond the available NIFTY calendar (recent data) -- return as-is;
        # pricing will simply fail to find day0 and the event is skipped.
        return candidate
    return pd.Timestamp(nifty_dates[idx]).date()


# --------------------------------------------------------------------------
# Loading + funnel + dedup
# --------------------------------------------------------------------------


def load_insider_pit(ann_con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = ann_con.execute(
        """
        SELECT
            record_hash,
            symbol,
            person_category,
            transaction_type,
            acq_mode,
            sec_acq_qty,
            sec_val,
            acq_from_date,
            acq_to_date,
            intimation_date,
            broadcast_at
        FROM insider_pit
        """
    ).df()
    df["broadcast_at"] = pd.to_datetime(df["broadcast_at"])
    return df


def compute_funnel(df: pd.DataFrame, covered_symbols: set[str]) -> dict:
    total = len(df)
    promoter = df[df["person_category"].isin(PROMOTER_CATEGORIES)]
    buy = promoter[promoter["transaction_type"] == BUY_TRANSACTION]
    mp = buy[buy["acq_mode"] == MARKET_PURCHASE_MODE]
    priced = mp[mp["symbol"].isin(covered_symbols)]
    uncovered = mp[~mp["symbol"].isin(covered_symbols)]
    return {
        "total": total,
        "promoter": len(promoter),
        "buy": len(buy),
        "market_purchase": len(mp),
        "priced": len(priced),
        "uncovered_rows": len(uncovered),
        "uncovered_symbols": sorted(uncovered["symbol"].unique().tolist()),
        "priced_df": priced,
    }


def dedup_events(priced_df: pd.DataFrame, nifty_dates: np.ndarray) -> pd.DataFrame:
    """Resolve event_date per row, then collapse to one row per
    (symbol, event_date), summing sec_val/sec_acq_qty."""
    if priced_df.empty:
        return priced_df.assign(event_date=[], total_value=[], total_qty=[], n_filings=[])

    df = priced_df.copy()
    df["event_date"] = df["broadcast_at"].apply(
        lambda ts: resolve_public_event_date(ts, nifty_dates)
    )

    grouped = df.groupby(["symbol", "event_date"], as_index=False).agg(
        total_value=("sec_val", "sum"),
        total_qty=("sec_acq_qty", "sum"),
        n_filings=("record_hash", "count"),
        first_broadcast_at=("broadcast_at", "min"),
    )
    grouped["half"] = grouped["event_date"].apply(lambda d: "A" if d < HALF_SPLIT_DATE else "B")
    return grouped


def value_bucket_mask(events: pd.DataFrame, bucket: str) -> pd.Series:
    if bucket == "all":
        return pd.Series(True, index=events.index)
    if bucket == ">=25L":
        return events["total_value"] >= VALUE_BUCKET_25L
    if bucket == ">=1Cr":
        return events["total_value"] >= VALUE_BUCKET_1CR
    raise ValueError(f"unknown bucket {bucket}")


# --------------------------------------------------------------------------
# Price loading
# --------------------------------------------------------------------------


def load_prices_by_symbol(
    prices_con: duckdb.DuckDBPyConnection, symbols: list[str]
) -> dict[str, pd.DataFrame]:
    if not symbols:
        return {}
    prices_con.register("symlist_r46", pd.DataFrame({"symbol": symbols}))
    df = prices_con.execute(
        """
        SELECT p.symbol, p.bar_date, p.open, p.high, p.low, p.close, p.volume
        FROM prices_daily_fwd p
        JOIN symlist_r46 s ON p.symbol = s.symbol
        ORDER BY p.symbol, p.bar_date
        """
    ).df()
    prices_con.unregister("symlist_r46")
    if df.empty:
        return {}
    df["bar_date"] = pd.to_datetime(df["bar_date"]).dt.date
    out = {}
    for sym, g in df.groupby("symbol"):
        out[sym] = prep_symbol_series(g)
    return out


def load_nifty_series(prices_con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = prices_con.execute(
        f"""
        SELECT bar_date, close
        FROM prices_daily_fwd
        WHERE symbol = '{INDEX_SYMBOL}'
        ORDER BY bar_date
        """
    ).df()
    df["bar_date"] = pd.to_datetime(df["bar_date"]).dt.date
    return df.drop_duplicates(subset=["bar_date"]).sort_values("bar_date").reset_index(drop=True)


def load_covered_symbols(prices_con: duckdb.DuckDBPyConnection) -> set[str]:
    df = prices_con.execute("SELECT DISTINCT symbol FROM fetch_fwd_log WHERE status = 'ok'").df()
    return set(df["symbol"].tolist())


# --------------------------------------------------------------------------
# Per-event metric computation (pure -- unit-testable on synthetic fixtures)
# --------------------------------------------------------------------------


def compute_event_metrics_r46(
    symbol_series: pd.DataFrame,
    nifty_dates: np.ndarray,
    nifty_closes: np.ndarray,
    event_date: date_cls,
) -> dict:
    """Core pure computation for one deduped promoter-buy event. Mirrors
    analyze_drift.compute_event_metrics but with R46's own HORIZONS list and
    four numbers per horizon: raw_h / ar_h (from day0 close) and
    raw_open_h / ar_open_h (from T+1 open)."""

    result: dict = {
        "day0_date": None,
        "prev_close": None,
        "day0_close": None,
        "t1_open": None,
        "skip_reason": None,
    }
    for h in HORIZONS:
        result[f"raw_{h}"] = None
        result[f"ar_{h}"] = None
        result[f"raw_open_{h}"] = None
        result[f"ar_open_{h}"] = None
        result[f"exit_close_{h}"] = None  # raw price, for sims

    day0_pos = find_day0_position(symbol_series, event_date)
    if day0_pos is None:
        result["skip_reason"] = "no_day0"
        return result

    day0_row = symbol_series.iloc[day0_pos]
    day0_date = pd.Timestamp(day0_row["bar_date"]).date()
    result["day0_date"] = day0_date

    if day0_pos < 1:
        result["skip_reason"] = "insufficient_history"
        return result

    prev_close = float(symbol_series.iloc[day0_pos - 1]["close"])
    result["prev_close"] = prev_close

    if prev_close < PENNY_FLOOR:
        result["skip_reason"] = "penny"
        return result

    day0_close = float(day0_row["close"])
    result["day0_close"] = day0_close

    n = len(symbol_series)
    t1_pos = day0_pos + 1
    t1_open = float(symbol_series.iloc[t1_pos]["open"]) if t1_pos < n else None
    result["t1_open"] = t1_open

    nifty_close_day0 = nifty_close_asof(nifty_dates, nifty_closes, day0_date)

    for h in HORIZONS:
        target_pos = day0_pos + h
        if target_pos >= n:
            continue  # truncated: beyond available data
        target_close = float(symbol_series.iloc[target_pos]["close"])
        target_date = pd.Timestamp(symbol_series.iloc[target_pos]["bar_date"]).date()
        result[f"exit_close_{h}"] = target_close

        raw_h = (target_close / day0_close) - 1.0
        result[f"raw_{h}"] = raw_h

        if t1_open is not None and t1_open > 0:
            result[f"raw_open_{h}"] = (target_close / t1_open) - 1.0

        nifty_close_h = nifty_close_asof(nifty_dates, nifty_closes, target_date)
        if nifty_close_day0 is None or nifty_close_h is None:
            continue  # can't market-adjust; AR variants stay None

        mkt_adj = (nifty_close_h / nifty_close_day0) - 1.0
        result[f"ar_{h}"] = raw_h - mkt_adj
        if t1_open is not None and t1_open > 0:
            result[f"ar_open_{h}"] = (target_close / t1_open - 1.0) - mkt_adj

    return result


def build_event_metrics_table(
    events: pd.DataFrame,
    prices_by_symbol: dict[str, pd.DataFrame],
    nifty_series: pd.DataFrame,
) -> pd.DataFrame:
    nifty_dates = np.array([np.datetime64(d) for d in nifty_series["bar_date"]])
    nifty_closes = nifty_series["close"].values.astype(float)

    rows = []
    for ev in events.itertuples(index=False):
        symbol = ev.symbol
        event_date = ev.event_date
        symbol_series = prices_by_symbol.get(symbol)

        base = {
            "symbol": symbol,
            "event_date": event_date,
            "half": ev.half,
            "total_value": ev.total_value,
            "total_qty": ev.total_qty,
            "n_filings": ev.n_filings,
        }
        if symbol_series is None or symbol_series.empty:
            base["skip_reason"] = "no_day0"
            metrics = compute_event_metrics_r46(
                pd.DataFrame(), nifty_dates, nifty_closes, event_date
            )
            metrics.pop("skip_reason", None)
            base.update(metrics)
            rows.append(base)
            continue

        metrics = compute_event_metrics_r46(symbol_series, nifty_dates, nifty_closes, event_date)
        base.update(metrics)
        rows.append(base)

    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Validation fixture (synthetic -- no DB access)
# --------------------------------------------------------------------------


def _make_synthetic_prices(
    symbol: str,
    start: date_cls,
    n_days: int,
    closes: list[float],
    open_overrides: dict[int, float] | None = None,
) -> pd.DataFrame:
    """Build a symbol_series-shaped DataFrame skipping Sat/Sun, given a list
    of close prices (open==prev close, high/low padded, volume=1000).
    `open_overrides` maps row index -> explicit open price, so a test can
    inject a genuine overnight gap (open != prior close) at a chosen bar --
    without it, open[i]==close[i-1] always and the T+1-open AR variant would
    be indistinguishable from the day0-close variant in any hand-worked test."""
    dates = []
    d = start
    while len(dates) < n_days:
        if d.weekday() < 5:  # Mon-Fri
            dates.append(d)
        d += timedelta(days=1)
    open_overrides = open_overrides or {}
    rows = []
    prev = closes[0] * 0.99
    for i, (dt, c) in enumerate(zip(dates, closes, strict=True)):
        o = open_overrides.get(i, prev)
        rows.append(
            {
                "bar_date": dt,
                "open": o,
                "high": max(o, c) * 1.001,
                "low": min(o, c) * 0.999,
                "close": c,
                "volume": 1000,
            }
        )
        prev = c
    return pd.DataFrame(rows)


def run_validation() -> None:
    print("=== R46 validation (synthetic fixture) ===")

    # ---- Fixture: 30 trading days starting Mon 2025-09-01, TESTSYM + NIFTY
    start = date_cls(2025, 9, 1)
    assert start.weekday() == 0, "fixture assumes 2025-09-01 is a Monday"  # nosec B101 - validation harness, not runtime
    n_days = 30
    testsym_closes = [100.0 + i * 0.5 for i in range(n_days)]  # steadily rising
    nifty_closes = [20000.0 + i * 10.0 for i in range(n_days)]  # steady market drift
    # Inject a genuine overnight gap at index 3 (T+1 of the day0=index2 event
    # tested below) so t1_open != day0_close -- otherwise, with the default
    # open[i]==close[i-1] construction, the T+1-open AR variant would be
    # numerically identical to the day0-close variant and Test 3 wouldn't
    # actually exercise the distinct formula.
    t1_gap_open = testsym_closes[2] * 1.02  # +2% gap-up open vs day0 close

    testsym_series = prep_symbol_series(
        _make_synthetic_prices(
            "TESTSYM", start, n_days, testsym_closes, open_overrides={3: t1_gap_open}
        )
    )
    nifty_series = prep_symbol_series(_make_synthetic_prices("NIFTY", start, n_days, nifty_closes))
    nifty_dates = np.array([np.datetime64(d) for d in nifty_series["bar_date"]])
    nifty_closes_arr = nifty_series["close"].values.astype(float)

    # ---- Test 1: event-date rolling ----
    # (a) broadcast before 15:25 on a trading day -> same day
    ts_a = pd.Timestamp("2025-09-03 11:00:00")
    ed_a = resolve_public_event_date(ts_a, nifty_dates)
    assert ed_a == date_cls(2025, 9, 3), f"expected same-day roll, got {ed_a}"  # nosec B101 - validation harness, not runtime

    # (b) broadcast at/after 15:25 -> next calendar day (which is a trading day)
    ts_b = pd.Timestamp("2025-09-03 15:30:00")
    ed_b = resolve_public_event_date(ts_b, nifty_dates)
    assert ed_b == date_cls(2025, 9, 4), f"expected next-day roll, got {ed_b}"  # nosec B101 - validation harness, not runtime

    # (c) broadcast on a Friday after cutoff -> next calendar day is Saturday
    # (non-trading) -> rolls to Monday
    ts_c = pd.Timestamp("2025-09-05 16:00:00")  # Friday
    ed_c = resolve_public_event_date(ts_c, nifty_dates)
    assert ed_c == date_cls(2025, 9, 8), f"expected roll over weekend to Monday, got {ed_c}"  # nosec B101 - validation harness, not runtime

    # (d) broadcast on a Saturday itself (non-trading raw date), before cutoff
    # time doesn't matter -> still rolls to Monday
    ts_d = pd.Timestamp("2025-09-06 10:00:00")  # Saturday
    ed_d = resolve_public_event_date(ts_d, nifty_dates)
    assert ed_d == date_cls(2025, 9, 8), f"expected Saturday->Monday roll, got {ed_d}"  # nosec B101 - validation harness, not runtime

    print(
        "[PASS] Test 1: event-date rolling (same-day / next-day / weekend-roll / non-trading-raw-date)"
    )

    # ---- Test 2: dedup + value-sum ----
    fake_rows = pd.DataFrame(
        {
            "record_hash": ["h1", "h2", "h3"],
            "symbol": ["TESTSYM", "TESTSYM", "TESTSYM"],
            "person_category": ["Promoters"] * 3,
            "transaction_type": ["Buy"] * 3,
            "acq_mode": ["Market Purchase"] * 3,
            "sec_acq_qty": [1000, 2000, 500],
            "sec_val": [1_000_000.0, 2_000_000.0, 500_000.0],
            "acq_from_date": [start] * 3,
            "acq_to_date": [start] * 3,
            "intimation_date": [start] * 3,
            "broadcast_at": [ts_a, ts_a, pd.Timestamp("2025-09-10 10:00:00")],
        }
    )
    deduped = dedup_events(fake_rows, nifty_dates)
    row1 = deduped[deduped["event_date"] == date_cls(2025, 9, 3)]
    assert len(row1) == 1, "expected exactly one deduped row for 2025-09-03"  # nosec B101 - validation harness, not runtime
    assert row1["n_filings"].iloc[0] == 2, "expected 2 filings collapsed into 1 event"  # nosec B101 - validation harness, not runtime
    assert row1["total_value"].iloc[0] == 3_000_000.0, "expected summed value 1M+2M"  # nosec B101 - validation harness, not runtime
    assert row1["total_qty"].iloc[0] == 3000, "expected summed qty 1000+2000"  # nosec B101 - validation harness, not runtime
    row2 = deduped[deduped["event_date"] == date_cls(2025, 9, 10)]
    assert len(row2) == 1 and row2["n_filings"].iloc[0] == 1  # nosec B101 - validation harness, not runtime
    assert row2["total_value"].iloc[0] == 500_000.0  # nosec B101 - validation harness, not runtime
    print("[PASS] Test 2: dedup collapses same (symbol, event_date) filings and sums value/qty")

    # ---- Test 3: hand-worked AR example ----
    # Event day0 = 2025-09-03 (index 2 in the fixture, 0-indexed from Mon
    # 2025-09-01). testsym_closes[2] = 101.0 (day0 close).
    # h=5 -> target index 2+5=7 -> testsym_closes[7] = 100 + 7*0.5 = 103.5
    # t1_open = open of index 3 = prev close of index 2 = 101.0 (by
    # construction, open[i] = close[i-1]).
    event_date = date_cls(2025, 9, 3)
    metrics = compute_event_metrics_r46(testsym_series, nifty_dates, nifty_closes_arr, event_date)

    day0_pos = 2  # 2025-09-01 (Mon, idx0), 09-02 (idx1), 09-03 (idx2)
    prev_close_expected = testsym_closes[day0_pos - 1]  # idx1 = 100.5
    day0_close_expected = testsym_closes[day0_pos]  # idx2 = 101.0
    h = 5
    target_pos = day0_pos + h  # 7
    target_close_expected = testsym_closes[target_pos]  # 103.5
    t1_open_expected = t1_gap_open  # open[3] explicitly overridden to a +2% gap vs close[2]

    raw_h_expected = (target_close_expected / day0_close_expected) - 1.0
    nifty_day0_expected = nifty_closes[day0_pos]  # 20020.0
    nifty_h_expected = nifty_closes[target_pos]  # 20070.0
    mkt_adj_expected = (nifty_h_expected / nifty_day0_expected) - 1.0
    ar_h_expected = raw_h_expected - mkt_adj_expected
    raw_open_h_expected = (target_close_expected / t1_open_expected) - 1.0
    ar_open_h_expected = raw_open_h_expected - mkt_adj_expected

    assert metrics["prev_close"] == prev_close_expected  # nosec B101 - validation harness, not runtime
    assert metrics["day0_close"] == day0_close_expected  # nosec B101 - validation harness, not runtime
    assert abs(metrics["t1_open"] - t1_open_expected) < 1e-9  # nosec B101 - validation harness, not runtime
    assert abs(metrics[f"raw_{h}"] - raw_h_expected) < 1e-9  # nosec B101 - validation harness, not runtime
    assert abs(metrics[f"ar_{h}"] - ar_h_expected) < 1e-9, (  # nosec B101 - validation harness, not runtime
        f"ar_{h} mismatch: got {metrics[f'ar_{h}']}, expected {ar_h_expected}"
    )
    assert abs(metrics[f"raw_open_{h}"] - raw_open_h_expected) < 1e-9  # nosec B101 - validation harness, not runtime
    assert abs(metrics[f"ar_open_{h}"] - ar_open_h_expected) < 1e-9, (  # nosec B101 - validation harness, not runtime
        f"ar_open_{h} mismatch: got {metrics[f'ar_open_{h}']}, expected {ar_open_h_expected}"
    )
    print(
        f"[PASS] Test 3: hand-worked AR@{h}: raw={raw_h_expected:.5f} "
        f"ar={ar_h_expected:.5f} raw_open={raw_open_h_expected:.5f} "
        f"ar_open={ar_open_h_expected:.5f} (all match function output)"
    )

    # ---- Test 4: penny floor + truncation sanity ----
    penny_series = _make_synthetic_prices(
        "PENNY", start, n_days, [10.0 + i * 0.1 for i in range(n_days)]
    )
    penny_series = prep_symbol_series(penny_series)
    penny_metrics = compute_event_metrics_r46(
        penny_series, nifty_dates, nifty_closes_arr, event_date
    )
    assert penny_metrics["skip_reason"] == "penny", "expected penny-floor skip on sub-50 prev_close"  # nosec B101 - validation harness, not runtime

    long_h = 60
    late_event_date = testsym_series.iloc[-3]["bar_date"]  # near the end of the fixture
    trunc_metrics = compute_event_metrics_r46(
        testsym_series, nifty_dates, nifty_closes_arr, late_event_date
    )
    assert trunc_metrics[f"ar_{long_h}"] is None, (  # nosec B101 - validation harness, not runtime
        "expected h=60 to be truncated (beyond available data)"
    )
    print("[PASS] Test 4: penny floor skip + horizon truncation beyond available data")

    print("\n=== All validation checks passed ===\n")


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


def build_ar_table(metrics: pd.DataFrame) -> pd.DataFrame:
    """population x horizon x (pooled/A/B) -> n, mean_ar_pct, tstat.
    Uses the day0-close AR (ar_h) as the headline AR column."""
    rows = []
    for bucket in ["all", ">=25L", ">=1Cr"]:
        mask = value_bucket_mask(metrics, bucket)
        sub = metrics[mask]
        for h in HORIZONS:
            col = f"ar_{h}"
            for scope, scope_df in [
                ("pooled", sub),
                ("A", sub[sub["half"] == "A"]),
                ("B", sub[sub["half"] == "B"]),
            ]:
                s = scope_df[col].dropna()
                rows.append(
                    {
                        "population": bucket,
                        "horizon": h,
                        "scope": scope,
                        "n": len(s),
                        "mean_ar_pct": s.mean() * 100.0 if len(s) else None,
                        "tstat": _tstat(s),
                    }
                )
    return pd.DataFrame(rows)


def neutral_control_spread(metrics: pd.DataFrame, r45_path: str) -> pd.DataFrame:
    r45_con = duckdb.connect(r45_path, read_only=True)
    neutral = r45_con.execute(
        f"""
        SELECT half, AVG(ar_10) AS neutral_ar_10, AVG(ar_20) AS neutral_ar_20, COUNT(*) AS n_neutral
        FROM event_drift
        WHERE reaction_bucket = '{NEUTRAL_REACTION_BUCKET}'
        GROUP BY half
        """
    ).df()
    r45_con.close()

    rows = []
    all_events = metrics[value_bucket_mask(metrics, "all")]
    for half in ["A", "B"]:
        half_df = all_events[all_events["half"] == half]
        promoter_ar10 = half_df["ar_10"].dropna().mean() if len(half_df) else None
        promoter_ar20 = half_df["ar_20"].dropna().mean() if len(half_df) else None
        neutral_row = neutral[neutral["half"] == half]
        neutral_ar10 = (
            float(neutral_row["neutral_ar_10"].iloc[0]) if not neutral_row.empty else None
        )
        neutral_ar20 = (
            float(neutral_row["neutral_ar_20"].iloc[0]) if not neutral_row.empty else None
        )
        rows.append(
            {
                "half": half,
                "promoter_ar10_pct": promoter_ar10 * 100.0 if promoter_ar10 is not None else None,
                "neutral_ar10_pct": neutral_ar10 * 100.0 if neutral_ar10 is not None else None,
                "spread_10_pct": (promoter_ar10 - neutral_ar10) * 100.0
                if promoter_ar10 is not None and neutral_ar10 is not None
                else None,
                "promoter_ar20_pct": promoter_ar20 * 100.0 if promoter_ar20 is not None else None,
                "neutral_ar20_pct": neutral_ar20 * 100.0 if neutral_ar20 is not None else None,
                "spread_20_pct": (promoter_ar20 - neutral_ar20) * 100.0
                if promoter_ar20 is not None and neutral_ar20 is not None
                else None,
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Sims
# --------------------------------------------------------------------------


def _simulate_trade(entry_open: float, exit_close: float) -> dict | None:
    if entry_open is None or exit_close is None or pd.isna(entry_open) or pd.isna(exit_close):
        return None
    if entry_open <= 0:
        return None
    entry_px = entry_open * (1 + ADVERSE_SLIPPAGE)
    exit_px = exit_close * (1 - ADVERSE_SLIPPAGE)
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


def run_sims(metrics: pd.DataFrame) -> pd.DataFrame:
    sub = metrics[value_bucket_mask(metrics, ">=25L")]
    sims = [("S1_25L_hold20", 20), ("S2_25L_hold40", 40)]
    out_rows = []
    for name, h in sims:
        cand = sub[sub["t1_open"].notna() & sub[f"exit_close_{h}"].notna()]
        trades = []
        for row in cand.itertuples(index=False):
            t = _simulate_trade(row.t1_open, getattr(row, f"exit_close_{h}"))
            if t is not None:
                t["half"] = row.half
                trades.append(t)
        tdf = pd.DataFrame(trades)
        if tdf.empty:
            out_rows.append({"sim": name, "scope": "pooled", "n": 0})
            continue
        for scope, sdf in [
            ("pooled", tdf),
            ("A", tdf[tdf["half"] == "A"]),
            ("B", tdf[tdf["half"] == "B"]),
        ]:
            if sdf.empty:
                out_rows.append({"sim": name, "scope": scope, "n": 0})
                continue
            out_rows.append(
                {
                    "sim": name,
                    "scope": scope,
                    "n": len(sdf),
                    "hit_rate_pct": 100.0 * (sdf["net_pnl"] > 0).mean(),
                    "avg_net_ret_pct": sdf["net_ret_pct"].mean(),
                    "median_net_ret_pct": sdf["net_ret_pct"].median(),
                    "total_net_pnl": sdf["net_pnl"].sum(),
                }
            )
    return pd.DataFrame(out_rows)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run(
    announcements_path: str,
    prices_path: str,
    r45_path: str,
    out_path: str,
    print_summary: bool,
) -> dict:
    ann_con = connect_announcements_readonly(announcements_path)
    prices_con = connect_prices_readonly(prices_path)

    print("[run] loading insider_pit...")
    raw = load_insider_pit(ann_con)
    print(f"[run] {len(raw)} total insider_pit rows")

    print("[run] loading covered-symbol set from fetch_fwd_log...")
    covered_symbols = load_covered_symbols(prices_con)
    covered_symbols.add(INDEX_SYMBOL)
    print(f"[run] {len(covered_symbols)} covered symbols (status='ok', + NIFTY)")

    funnel = compute_funnel(raw, covered_symbols)
    print(
        f"[run] funnel: total={funnel['total']} -> promoter={funnel['promoter']} "
        f"-> buy={funnel['buy']} -> market_purchase={funnel['market_purchase']} "
        f"-> priced={funnel['priced']} (uncovered_rows={funnel['uncovered_rows']}, "
        f"uncovered_symbols={len(funnel['uncovered_symbols'])})"
    )

    print("[run] loading NIFTY series...")
    nifty_series = load_nifty_series(prices_con)
    nifty_dates = np.array([np.datetime64(d) for d in nifty_series["bar_date"]])

    events = dedup_events(funnel["priced_df"], nifty_dates)
    print(f"[run] {len(events)} distinct (symbol, event_date) events after dedup")

    symbols_needed = sorted(events["symbol"].unique().tolist())
    print(f"[run] loading prices_daily_fwd for {len(symbols_needed)} symbols + NIFTY...")
    prices_by_symbol = load_prices_by_symbol(prices_con, symbols_needed)

    metrics = build_event_metrics_table(events, prices_by_symbol, nifty_series)
    print(f"[run] writing event_metrics ({len(metrics)} rows) to {out_path}...")

    out_con = duckdb.connect(out_path)
    out_con.execute("DROP TABLE IF EXISTS event_metrics")
    out_con.register("event_metrics_df", metrics)
    out_con.execute("CREATE TABLE event_metrics AS SELECT * FROM event_metrics_df")
    out_con.close()

    ann_con.close()
    prices_con.close()

    ar_table = None
    control_table = None
    sims_table = None
    if print_summary:
        ar_table = build_ar_table(metrics)
        control_table = neutral_control_spread(metrics, r45_path)
        sims_table = run_sims(metrics)

        pd.set_option("display.max_rows", 300)
        pd.set_option("display.width", 200)

        print("\n=== Funnel ===")
        print(
            f"total={funnel['total']} promoter={funnel['promoter']} buy={funnel['buy']} "
            f"market_purchase={funnel['market_purchase']} priced={funnel['priced']} "
            f"after_dedup={len(events)}"
        )
        print(
            f"uncovered (SME/BSE-only/delisted, excluded not retried): "
            f"{funnel['uncovered_rows']} rows across {len(funnel['uncovered_symbols'])} symbols"
        )

        print("\n=== AR table (population x horizon x pooled/A/B) ===")
        print(ar_table.sort_values(["population", "horizon", "scope"]).to_string(index=False))

        print("\n=== Size-matched control (promoter AR - R45 neutral-bucket AR, same half) ===")
        print(control_table.to_string(index=False))

        print("\n=== Sims ===")
        print(sims_table.to_string(index=False))

    return {
        "funnel": funnel,
        "n_events": len(events),
        "metrics": metrics,
        "ar_table": ar_table,
        "control_table": control_table,
        "sims_table": sims_table,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="R46 promoter-buy insider-disclosure drift study")
    parser.add_argument("--announcements", default=DEFAULT_ANNOUNCEMENTS)
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
        announcements_path=args.announcements,
        prices_path=args.prices,
        r45_path=args.r45,
        out_path=args.out,
        print_summary=args.summary,
    )


if __name__ == "__main__":
    main()
