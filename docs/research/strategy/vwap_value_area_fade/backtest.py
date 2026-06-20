"""VWAP Value-Area Fade — backtest harness.

Self-contained. Loads NIFTY 1m bars from a local DuckDB snapshot (avoids the
cross-process lock the live app holds on ``db/historify.duckdb``), resamples to
5m, computes session VWAP + ±1σ bands (equal-weighted because the index has
zero volume — see the spec), simulates fades per the mechanical rules in
``2026-06-20_strategy_spec.md``, and writes the per-trade journal +
equity-curve CSV + summary metrics.

Run with::

    uv run python docs/research/strategy/vwap_value_area_fade/backtest.py

It prints summary stats to stdout and writes::

    docs/research/strategy/vwap_value_area_fade/trades.csv
    docs/research/strategy/vwap_value_area_fade/equity_curve.csv
    docs/research/strategy/vwap_value_area_fade/summary.json
    docs/research/strategy/vwap_value_area_fade/buy_and_hold_equity.csv

No external dependencies beyond what is already in ``pyproject.toml``
(``duckdb``, ``pandas``, ``numpy``).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------- #
# Config
# ---------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
DUCKDB_PATH = Path(__file__).resolve().parents[4] / ".cache" / "duckdb_snap" / "historify.duckdb"
OUT_DIR = Path(__file__).resolve().parent

SYMBOL = "NIFTY"
EXCHANGE = "NSE_INDEX"
INTERVAL_RAW = "1m"

SESSION_START = time(9, 15)
SESSION_END = time(15, 30)
NO_TRADE_UNTIL = time(9, 30)  # rule R5 — first 15 min skipped
EOD_FLAT_TIME = time(15, 14)  # rule R6 / EOD cap

REJECT_MARGIN_POINTS = 2.0  # close must be this far back inside the band
MIN_BAR_RANGE_POINTS = 5.0  # rule R2 — kills doji bars
WICK_RATIO_MIN = 0.50  # wick beyond band ≥ this fraction of bar range
STOP_BUFFER_POINTS = 2.0  # stop placed this many points beyond wick
TIME_STOP_MINUTES = 60  # rule R6
MAX_LOSSES_PER_DAY = 2  # rule R4

STARTING_CAPITAL = 1_000_000.0  # ₹10L
RISK_PER_TRADE_FRAC = 0.01  # 1% of capital
NIFTY_LOT_SIZE = 75
MAX_LOTS = 5  # sizing cap

# Run windows
FULL_START = date(2024, 1, 1)
FULL_END = date(2026, 6, 19)
YTD2026_START = date(2026, 1, 1)
YTD2026_END = date(2026, 6, 19)


# ---------------------------------------------------------------------------- #
# Data
# ---------------------------------------------------------------------------- #
def load_1m_bars(from_d: date, to_d: date) -> pd.DataFrame:
    """Pull NIFTY 1m bars for the half-open day range [from_d, to_d+1)."""
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    # convert IST date bounds to UTC epoch seconds
    from_ts = int(datetime.combine(from_d, time(0, 0)).replace(tzinfo=IST).timestamp())
    to_ts = int(
        datetime.combine(to_d + timedelta(days=1), time(0, 0)).replace(tzinfo=IST).timestamp()
    )
    df = con.execute(
        """
        SELECT timestamp, open, high, low, close
        FROM market_data
        WHERE symbol = ? AND exchange = ? AND interval = ?
          AND timestamp >= ? AND timestamp < ?
        ORDER BY timestamp
        """,
        [SYMBOL, EXCHANGE, INTERVAL_RAW, from_ts, to_ts],
    ).fetchdf()
    con.close()
    if df.empty:
        return df
    df["ts_ist"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
    df["date"] = df["ts_ist"].dt.date
    df["time"] = df["ts_ist"].dt.time
    return df


def resample_5m(df1m: pd.DataFrame) -> pd.DataFrame:
    """Resample 1m → 5m within each session.

    The 09:15 bar covers 09:15–09:19 inclusive; the 09:20 bar covers 09:20–09:24,
    etc. We label each 5m bar by its OPENING IST time.
    """
    if df1m.empty:
        return df1m
    rows = []
    for d, dg in df1m.groupby("date", sort=True):
        dg = dg.sort_values("ts_ist").reset_index(drop=True)
        # Snap each 1m bar to its 5m bucket by floor-dividing minute-since-09:15
        dg["min_since_open"] = (
            dg["ts_ist"].dt.hour * 60
            + dg["ts_ist"].dt.minute
            - (SESSION_START.hour * 60 + SESSION_START.minute)
        )
        # only keep regular-session minutes
        dg = dg[(dg["min_since_open"] >= 0) & (dg["min_since_open"] < 375)].copy()
        dg["bucket"] = dg["min_since_open"] // 5
        agg = (
            dg.groupby("bucket")
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                ts_ist=("ts_ist", "first"),
            )
            .reset_index()
        )
        agg["date"] = d
        rows.append(agg)
    out = pd.concat(rows, ignore_index=True)
    out["time"] = out["ts_ist"].dt.time
    out["tp"] = (out["high"] + out["low"] + out["close"]) / 3.0
    return out


def attach_session_vwap_bands(df5m: pd.DataFrame) -> pd.DataFrame:
    """Per-session cumulative VWAP and ±1σ bands (equal-weighted — index vol=0)."""
    df5m = df5m.sort_values(["date", "bucket"]).copy()
    df5m["cum_tp"] = df5m.groupby("date")["tp"].cumsum()
    df5m["cum_tp2"] = df5m.groupby("date")["tp"].transform(lambda s: (s * s).cumsum())
    df5m["cum_n"] = df5m.groupby("date").cumcount() + 1
    df5m["vwap"] = df5m["cum_tp"] / df5m["cum_n"]
    var = df5m["cum_tp2"] / df5m["cum_n"] - df5m["vwap"] ** 2
    df5m["sigma"] = np.sqrt(np.clip(var.values, 0.0, None))
    df5m["upper"] = df5m["vwap"] + df5m["sigma"]
    df5m["lower"] = df5m["vwap"] - df5m["sigma"]
    return df5m


# ---------------------------------------------------------------------------- #
# Trade simulator
# ---------------------------------------------------------------------------- #
@dataclass
class Trade:
    entry_date: str
    entry_time: str
    exit_date: str
    exit_time: str
    side: str
    entry_price: float
    exit_price: float
    stop_price: float
    vwap_at_entry: float
    upper_at_entry: float
    lower_at_entry: float
    bar_range: float
    wick_size: float
    stop_points: float
    points: float
    lots: int
    pnl_inr: float
    exit_reason: str
    holding_minutes: int


def _exit_short(
    rows: pd.DataFrame, idx_entry: int, entry: float, stop: float, entry_dt: datetime
) -> tuple[int, float, str]:
    """Walk forward bar-by-bar from idx_entry+1; return (exit_idx, exit_price, reason)."""
    for j in range(idx_entry + 1, len(rows)):
        row = rows.iloc[j]
        # EOD cap
        if row["time"] >= EOD_FLAT_TIME:
            return j, float(row["close"]), "eod_flat"
        # time stop (60 min)
        bar_dt = datetime.combine(row["date"], row["time"]).replace(tzinfo=IST)
        if (bar_dt - entry_dt).total_seconds() >= TIME_STOP_MINUTES * 60:
            return j, float(row["close"]), "time_stop"
        # stop hit?
        stop_hit = row["high"] >= stop
        target_hit = row["low"] <= row["vwap"]
        # same-bar conflict — pessimistic: stop wins for a short fade
        if stop_hit:
            return j, float(stop), "stop"
        if target_hit:
            return j, float(row["vwap"]), "target"
    # ran off end (last bar of df → forced EOD)
    j = len(rows) - 1
    return j, float(rows.iloc[j]["close"]), "eod_flat"


def _exit_long(
    rows: pd.DataFrame, idx_entry: int, entry: float, stop: float, entry_dt: datetime
) -> tuple[int, float, str]:
    for j in range(idx_entry + 1, len(rows)):
        row = rows.iloc[j]
        if row["time"] >= EOD_FLAT_TIME:
            return j, float(row["close"]), "eod_flat"
        bar_dt = datetime.combine(row["date"], row["time"]).replace(tzinfo=IST)
        if (bar_dt - entry_dt).total_seconds() >= TIME_STOP_MINUTES * 60:
            return j, float(row["close"]), "time_stop"
        stop_hit = row["low"] <= stop
        target_hit = row["high"] >= row["vwap"]
        if stop_hit:
            return j, float(stop), "stop"
        if target_hit:
            return j, float(row["vwap"]), "target"
    j = len(rows) - 1
    return j, float(rows.iloc[j]["close"]), "eod_flat"


def simulate_day(day_df: pd.DataFrame, capital: float) -> list[Trade]:
    """Walk a single day's 5m bars in order and emit Trade objects."""
    trades: list[Trade] = []
    if len(day_df) < 4:
        return trades
    day_df = day_df.reset_index(drop=True)
    losses_today = 0
    in_position_until = -1  # index of bar after which we can take a new trade
    for i in range(1, len(day_df)):
        if losses_today >= MAX_LOSSES_PER_DAY:
            break
        if i <= in_position_until:
            continue
        row = day_df.iloc[i]
        prev = day_df.iloc[i - 1]
        # Time gates
        if row["time"] < NO_TRADE_UNTIL:
            continue
        if row["time"] >= EOD_FLAT_TIME:
            break
        # Need a valid sigma
        if not np.isfinite(row["sigma"]) or row["sigma"] <= 0:
            continue
        # Prior bar must have closed INSIDE the value area
        if not (prev["lower"] < prev["close"] < prev["upper"]):
            continue
        bar_range = float(row["high"] - row["low"])
        if bar_range < MIN_BAR_RANGE_POINTS:
            continue
        # ---- Upper band rejection (SHORT) ----
        if row["high"] >= row["upper"] and row["close"] <= row["upper"] - REJECT_MARGIN_POINTS:
            upper_wick = float(row["high"] - max(row["open"], row["close"]))
            if upper_wick >= WICK_RATIO_MIN * bar_range:
                entry = float(row["close"])
                stop = float(row["high"] + STOP_BUFFER_POINTS)
                stop_pts = stop - entry
                if stop_pts <= 0:
                    continue
                risk_inr = capital * RISK_PER_TRADE_FRAC
                lots = max(1, int(risk_inr // (stop_pts * NIFTY_LOT_SIZE)))
                lots = min(lots, MAX_LOTS)
                entry_dt = datetime.combine(row["date"], row["time"]).replace(tzinfo=IST)
                j, exit_px, reason = _exit_short(day_df, i, entry, stop, entry_dt)
                exit_row = day_df.iloc[j]
                pts = entry - exit_px  # short: profit if exit < entry
                pnl = pts * lots * NIFTY_LOT_SIZE
                hold_min = int(
                    (
                        datetime.combine(exit_row["date"], exit_row["time"]).replace(tzinfo=IST)
                        - entry_dt
                    ).total_seconds()
                    // 60
                )
                trades.append(
                    Trade(
                        entry_date=str(row["date"]),
                        entry_time=row["time"].strftime("%H:%M"),
                        exit_date=str(exit_row["date"]),
                        exit_time=exit_row["time"].strftime("%H:%M"),
                        side="SHORT",
                        entry_price=entry,
                        exit_price=float(exit_px),
                        stop_price=stop,
                        vwap_at_entry=float(row["vwap"]),
                        upper_at_entry=float(row["upper"]),
                        lower_at_entry=float(row["lower"]),
                        bar_range=bar_range,
                        wick_size=upper_wick,
                        stop_points=stop_pts,
                        points=pts,
                        lots=lots,
                        pnl_inr=pnl,
                        exit_reason=reason,
                        holding_minutes=hold_min,
                    )
                )
                if pnl < 0:
                    losses_today += 1
                in_position_until = j
                continue
        # ---- Lower band rejection (LONG) ----
        if row["low"] <= row["lower"] and row["close"] >= row["lower"] + REJECT_MARGIN_POINTS:
            lower_wick = float(min(row["open"], row["close"]) - row["low"])
            if lower_wick >= WICK_RATIO_MIN * bar_range:
                entry = float(row["close"])
                stop = float(row["low"] - STOP_BUFFER_POINTS)
                stop_pts = entry - stop
                if stop_pts <= 0:
                    continue
                risk_inr = capital * RISK_PER_TRADE_FRAC
                lots = max(1, int(risk_inr // (stop_pts * NIFTY_LOT_SIZE)))
                lots = min(lots, MAX_LOTS)
                entry_dt = datetime.combine(row["date"], row["time"]).replace(tzinfo=IST)
                j, exit_px, reason = _exit_long(day_df, i, entry, stop, entry_dt)
                exit_row = day_df.iloc[j]
                pts = exit_px - entry
                pnl = pts * lots * NIFTY_LOT_SIZE
                hold_min = int(
                    (
                        datetime.combine(exit_row["date"], exit_row["time"]).replace(tzinfo=IST)
                        - entry_dt
                    ).total_seconds()
                    // 60
                )
                trades.append(
                    Trade(
                        entry_date=str(row["date"]),
                        entry_time=row["time"].strftime("%H:%M"),
                        exit_date=str(exit_row["date"]),
                        exit_time=exit_row["time"].strftime("%H:%M"),
                        side="LONG",
                        entry_price=entry,
                        exit_price=float(exit_px),
                        stop_price=stop,
                        vwap_at_entry=float(row["vwap"]),
                        upper_at_entry=float(row["upper"]),
                        lower_at_entry=float(row["lower"]),
                        bar_range=bar_range,
                        wick_size=lower_wick,
                        stop_points=stop_pts,
                        points=pts,
                        lots=lots,
                        pnl_inr=pnl,
                        exit_reason=reason,
                        holding_minutes=hold_min,
                    )
                )
                if pnl < 0:
                    losses_today += 1
                in_position_until = j
    return trades


def run_backtest(from_d: date, to_d: date) -> tuple[list[Trade], pd.DataFrame, pd.DataFrame]:
    print(f"Loading 1m bars {from_d} -> {to_d}...")
    raw = load_1m_bars(from_d, to_d)
    if raw.empty:
        return [], pd.DataFrame(), pd.DataFrame()
    print(f"  loaded {len(raw):,} 1m bars")
    df5 = resample_5m(raw)
    df5 = attach_session_vwap_bands(df5)
    print(f"  built {len(df5):,} 5m bars across {df5['date'].nunique()} sessions")
    # Simulate
    all_trades: list[Trade] = []
    capital = STARTING_CAPITAL  # constant sizing (headline)
    for _d, dg in df5.groupby("date", sort=True):
        all_trades.extend(simulate_day(dg, capital))
    print(f"  {len(all_trades):,} trades")
    trade_df = pd.DataFrame([asdict(t) for t in all_trades])
    return all_trades, trade_df, df5


# ---------------------------------------------------------------------------- #
# Metrics
# ---------------------------------------------------------------------------- #
def build_equity_curve(trade_df: pd.DataFrame, all_dates: list[date]) -> pd.DataFrame:
    """One row per trading session: date, daily_pnl, equity (cumulative)."""
    if trade_df.empty:
        ec = pd.DataFrame({"date": all_dates, "daily_pnl": 0.0})
    else:
        # use entry_date for attribution (intraday strategy → exit_date == entry_date)
        daily = trade_df.groupby("entry_date")["pnl_inr"].sum().to_dict()
        ec = pd.DataFrame({"date": all_dates})
        ec["date_str"] = ec["date"].astype(str)
        ec["daily_pnl"] = ec["date_str"].map(daily).fillna(0.0)
        ec = ec.drop(columns=["date_str"])
    ec["equity"] = STARTING_CAPITAL + ec["daily_pnl"].cumsum()
    return ec


def buy_and_hold_curve(df5m: pd.DataFrame, all_dates: list[date]) -> pd.DataFrame:
    """Buy 1 NIFTY future (75 mult) at first session open, hold to last close."""
    # take first close of each session
    daily = (
        df5m.groupby("date")
        .agg(first_close=("close", "first"), last_close=("close", "last"))
        .reset_index()
    )
    daily = daily.sort_values("date").reset_index(drop=True)
    if daily.empty:
        return pd.DataFrame()
    entry = float(daily.iloc[0]["first_close"])
    # equity = starting_capital + (close_t - entry) * 75 * lots
    # size: 1 lot starting position (₹10L / (entry * 75) ≈ lots; we hold 1 lot for clean comparison)
    lots = 1
    daily["equity"] = STARTING_CAPITAL + (daily["last_close"] - entry) * NIFTY_LOT_SIZE * lots
    return daily[["date", "equity"]]


def compute_metrics(
    equity_df: pd.DataFrame, trade_df: pd.DataFrame, from_d: date, to_d: date
) -> dict:
    if equity_df.empty:
        return {}
    eq = equity_df["equity"].to_numpy()
    daily_pnl = (
        equity_df["daily_pnl"].to_numpy()
        if "daily_pnl" in equity_df.columns
        else np.diff(eq, prepend=STARTING_CAPITAL)
    )
    # CAGR
    n_days = max(1, (to_d - from_d).days)
    yrs = n_days / 365.25
    final = eq[-1]
    total_ret = final / STARTING_CAPITAL - 1.0
    cagr = (
        (final / STARTING_CAPITAL) ** (1.0 / yrs) - 1.0 if yrs > 0 and final > 0 else float("nan")
    )
    # Sharpe / Sortino on daily ret (PnL / starting capital, since constant sizing)
    dret = daily_pnl / STARTING_CAPITAL
    sharpe = (
        float(np.mean(dret) / np.std(dret, ddof=1) * math.sqrt(252))
        if dret.std(ddof=1) > 0
        else float("nan")
    )
    downside = dret[dret < 0]
    sortino = (
        float(np.mean(dret) / np.std(downside, ddof=1) * math.sqrt(252))
        if downside.std(ddof=1) > 0
        else float("nan")
    )
    # MaxDD
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    max_dd = float(dd.min())
    # Trade stats
    if trade_df.empty:
        win_rate = avg_r = pf = trades_pm = monthly_green = 0.0
        n_trades = 0
        avg_win = avg_loss = 0.0
    else:
        n_trades = len(trade_df)
        wins = trade_df[trade_df["pnl_inr"] > 0]
        losses = trade_df[trade_df["pnl_inr"] < 0]
        win_rate = len(wins) / n_trades
        avg_win = float(wins["pnl_inr"].mean()) if len(wins) else 0.0
        avg_loss = float(losses["pnl_inr"].mean()) if len(losses) else 0.0
        # avg-R = avg win / abs(avg loss) is one common defn; we'll report payoff
        # ratio. Per-trade R uses initial risk (≈₹10k).
        trade_df = trade_df.copy()
        trade_df["risk_inr"] = trade_df["stop_points"] * trade_df["lots"] * NIFTY_LOT_SIZE
        trade_df["R"] = trade_df["pnl_inr"] / trade_df["risk_inr"]
        avg_r = float(trade_df["R"].mean())
        gross_win = float(wins["pnl_inr"].sum())
        gross_loss = float(-losses["pnl_inr"].sum())
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
        # trades / month
        days_span = max(1, (to_d - from_d).days)
        months = days_span / 30.4375
        trades_pm = n_trades / months
        # monthly green%
        mom = trade_df.copy()
        mom["month"] = pd.to_datetime(mom["entry_date"]).dt.to_period("M")
        m_pnl = mom.groupby("month")["pnl_inr"].sum()
        monthly_green = float((m_pnl > 0).mean())
    return {
        "from": str(from_d),
        "to": str(to_d),
        "n_days": int(n_days),
        "years": round(yrs, 3),
        "total_return_pct": round(total_ret * 100, 3),
        "cagr_pct": round(cagr * 100, 3) if not math.isnan(cagr) else None,
        "sharpe_annual": round(sharpe, 3) if not math.isnan(sharpe) else None,
        "sortino_annual": round(sortino, 3) if not math.isnan(sortino) else None,
        "max_dd_pct": round(max_dd * 100, 3),
        "n_trades": int(n_trades),
        "win_rate_pct": round(win_rate * 100, 2),
        "avg_R": round(avg_r, 3) if isinstance(avg_r, float) else avg_r,
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "trades_per_month": round(trades_pm, 2),
        "monthly_green_pct": round(monthly_green * 100, 2),
        "avg_win_inr": round(avg_win, 2),
        "avg_loss_inr": round(avg_loss, 2),
        "final_equity_inr": round(float(eq[-1]), 2),
    }


def by_exit_reason(trade_df: pd.DataFrame) -> dict:
    if trade_df.empty:
        return {}
    g = trade_df.groupby("exit_reason").agg(
        n=("pnl_inr", "size"),
        sum=("pnl_inr", "sum"),
        mean=("pnl_inr", "mean"),
        win_rate=("pnl_inr", lambda s: (s > 0).mean() * 100),
    )
    return g.round(2).to_dict(orient="index")


def by_side(trade_df: pd.DataFrame) -> dict:
    if trade_df.empty:
        return {}
    g = trade_df.groupby("side").agg(
        n=("pnl_inr", "size"),
        sum=("pnl_inr", "sum"),
        win_rate=("pnl_inr", lambda s: (s > 0).mean() * 100),
    )
    return g.round(2).to_dict(orient="index")


# ---------------------------------------------------------------------------- #
# Main
# ---------------------------------------------------------------------------- #
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_results = {}

    # ---- Headline: full window ----
    trades_full, tdf_full, df5_full = run_backtest(FULL_START, FULL_END)
    all_dates_full = sorted(df5_full["date"].unique().tolist())
    eq_full = build_equity_curve(tdf_full, all_dates_full)
    metrics_full = compute_metrics(eq_full, tdf_full, FULL_START, FULL_END)
    metrics_full["by_exit_reason"] = by_exit_reason(tdf_full)
    metrics_full["by_side"] = by_side(tdf_full)
    bh_full = buy_and_hold_curve(df5_full, all_dates_full)
    # B&H metrics
    if not bh_full.empty:
        bh_pnl = np.diff(bh_full["equity"].to_numpy(), prepend=STARTING_CAPITAL)
        bh_metrics = compute_metrics(
            pd.DataFrame(
                {"date": bh_full["date"], "daily_pnl": bh_pnl, "equity": bh_full["equity"]}
            ),
            pd.DataFrame(),
            FULL_START,
            FULL_END,
        )
        metrics_full["buy_and_hold"] = bh_metrics

    all_results["full_window"] = metrics_full

    # ---- 2026 YTD slice ----
    # reuse the full DF — filter to 2026
    tdf_ytd = (
        tdf_full[pd.to_datetime(tdf_full["entry_date"]).dt.year == 2026].copy()
        if not tdf_full.empty
        else tdf_full
    )
    df5_ytd = df5_full[df5_full["date"].map(lambda d: YTD2026_START <= d <= YTD2026_END)]
    all_dates_ytd = sorted(df5_ytd["date"].unique().tolist())
    eq_ytd = build_equity_curve(tdf_ytd, all_dates_ytd)
    # rebase equity to STARTING_CAPITAL for the YTD view
    eq_ytd["equity"] = STARTING_CAPITAL + eq_ytd["daily_pnl"].cumsum()
    metrics_ytd = compute_metrics(eq_ytd, tdf_ytd, YTD2026_START, YTD2026_END)
    metrics_ytd["by_exit_reason"] = by_exit_reason(tdf_ytd)
    bh_ytd = buy_and_hold_curve(df5_ytd, all_dates_ytd)
    if not bh_ytd.empty:
        bh_pnl_ytd = np.diff(bh_ytd["equity"].to_numpy(), prepend=STARTING_CAPITAL)
        bh_metrics_ytd = compute_metrics(
            pd.DataFrame(
                {"date": bh_ytd["date"], "daily_pnl": bh_pnl_ytd, "equity": bh_ytd["equity"]}
            ),
            pd.DataFrame(),
            YTD2026_START,
            YTD2026_END,
        )
        metrics_ytd["buy_and_hold"] = bh_metrics_ytd
    all_results["ytd_2026"] = metrics_ytd

    # ---- Charges sensitivity (₹50/lot/leg + 0.025% STT sell-side) ----
    def with_charges(tdf: pd.DataFrame) -> pd.DataFrame:
        if tdf.empty:
            return tdf
        out = tdf.copy()
        # brokerage: ₹50 per lot per leg, both legs
        brokerage = 50 * out["lots"] * 2
        # STT: 0.025% × notional, sell-side only
        sell_notional = (
            np.where(out["side"] == "SHORT", out["entry_price"], out["exit_price"])
            * out["lots"]
            * NIFTY_LOT_SIZE
        )
        stt = 0.00025 * sell_notional
        out["charges"] = brokerage + stt
        out["pnl_inr"] = out["pnl_inr"] - out["charges"]
        return out

    tdf_full_charged = with_charges(tdf_full)
    eq_full_charged = build_equity_curve(tdf_full_charged, all_dates_full)
    metrics_full_charged = compute_metrics(eq_full_charged, tdf_full_charged, FULL_START, FULL_END)
    all_results["full_window_with_charges"] = metrics_full_charged

    # ---- Write outputs ----
    tdf_full.to_csv(OUT_DIR / "trades.csv", index=False)
    print(f"  -> wrote {OUT_DIR / 'trades.csv'} ({len(tdf_full)} rows)")
    eq_full[["date", "equity"]].to_csv(OUT_DIR / "equity_curve.csv", index=False)
    print(f"  -> wrote {OUT_DIR / 'equity_curve.csv'}")
    if not bh_full.empty:
        bh_full.to_csv(OUT_DIR / "buy_and_hold_equity.csv", index=False)
        print(f"  -> wrote {OUT_DIR / 'buy_and_hold_equity.csv'}")
    with open(OUT_DIR / "summary.json", "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"  -> wrote {OUT_DIR / 'summary.json'}")

    # ---- Print ----
    print("\n========== FULL WINDOW ==========")
    print(json.dumps(metrics_full, indent=2, default=str))
    print("\n========== 2026 YTD ==========")
    print(json.dumps(metrics_ytd, indent=2, default=str))
    print("\n========== FULL WINDOW + CHARGES ==========")
    print(json.dumps(metrics_full_charged, indent=2, default=str))


if __name__ == "__main__":
    main()
