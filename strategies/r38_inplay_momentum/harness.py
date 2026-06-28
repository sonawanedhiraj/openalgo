"""
R38 — NSE In-Play Intraday Momentum (IPM) backtest harness
SCAFFOLD ONLY — this is the spec'd plumbing. Defaults in Config are placeholders;
calibration against actual in-play distribution is mandatory before trusting results.

Source: Dheeraj uploaded `r38_inplay_backtest.pdf` 2026-06-28.
Tracked under Issue #195 + Strategy Registry entry (proposed in PR).
"""
# R38 — NSE In-Play Intraday Momentum (IPM) backtest harness
# ==========================================================
#
# Ports Pradeep Bonde's "in-play" detection to the NSE F&O universe:
#     participation -> RVOL + traded value      (his "9M volume")
#     velocity      -> new-high burst / thrust  (his "60 new highs in <3 min")
#
# This is the PLUMBING. It produces real numbers ONLY against your real
# 1-minute Kite bars. Thresholds in Config are placeholders — calibrate them
# against your own deep-dive distribution before trusting any output.
#
# Run modes
# ---------
#     python r38_inplay_backtest.py --data /path/to/minute_bars --symbols symbols.txt
#     python r38_inplay_backtest.py --smoke  # validates the plumbing on dummy bars
#
# Expected per-symbol data schema (one file per symbol, parquet or csv):
#     columns: datetime (tz-naive IST, 1-min), open, high, low, close, volume
#     filename: <SYMBOL>.parquet (or .csv)

from __future__ import annotations

import argparse
import glob  # noqa: F401
import os
from dataclasses import asdict, dataclass, field  # noqa: F401
from datetime import time

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# Config — every number here is a CALIBRATION TARGET, not a blessed value.
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    # session / time gates (IST)
    session_open: time = time(9, 15)
    no_entry_before: time = time(9, 30)      # skip first 15 min
    no_entry_after: time = time(14, 45)
    force_flat: time = time(15, 15)

    # participation gate
    rvol_window_sessions: int = 20
    rvol_min: float = 2.5
    turnover_min_cr: float = 5.0             # cumulative ₹ crore by entry time

    # velocity gate
    nhb_window: int = 15                     # bars to look back for new-high burst
    nhb_min: int = 5                         # >= this many new session highs in window
    thrust_win: int = 3                      # minutes
    thrust_k: float = 1.5                    # last-N-min return >= k * ATR%

    # trend filter
    use_vwap_filter: bool = True

    # optional pullback entry (Bonde structure). If False, enter on gate bar.
    use_pullback_entry: bool = False
    pb_vol_x: float = 2.0

    # stops / exits
    atr_len: int = 14
    atr_mult: float = 1.8                    # initial stop distance
    scale_target_atr: float = 1.2           # +x*ATR -> scale out
    scale_frac: float = 0.80                # sell this fraction at target
    scale_max_bars: int = 20               # target must hit within N bars

    # risk
    risk_frac: float = 0.005               # 0.5% equity per trade
    max_concurrent: int = 5
    daily_loss_cb_R: float = -2.0          # halt day after this cumulative R

    # costs (round-trip, bps of notional) — SEPARATE these for equity vs futures
    cost_bps: float = 6.0                  # brokerage+STT+exch+GST+stamp+SEBI est.
    slippage_bps: float = 4.0             # bias conservative on ignition bars

    # validation
    min_trades: int = 30                   # reject below this (anti-R37 guard)
    starting_equity: float = 1_000_000.0
    direction: str = "long"               # "long" | "short" | "both"


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, lo, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - lo), (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=1).mean()


def _session_vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (tp * df["volume"]).cumsum()
    vv = df["volume"].cumsum().replace(0, np.nan)
    return pv / vv


def _new_high_burst(high: pd.Series, window: int) -> pd.Series:
    run_max = high.cummax()
    is_new = (high >= run_max - 1e-9).astype(int)
    return is_new.rolling(window, min_periods=1).sum()


def build_session_features(
    day: pd.DataFrame, rvol_baseline: pd.Series | None, cfg: Config
) -> pd.DataFrame:
    """Add per-bar feature columns to one session's bars (sorted by time)."""
    d = day.copy().reset_index(drop=True)
    d["cum_vol"] = d["volume"].cumsum()
    d["cum_turnover_cr"] = (d["close"] * d["volume"]).cumsum() / 1e7  # ₹ cr
    d["atr"] = _atr(d, cfg.atr_len)
    d["atr_pct"] = d["atr"] / d["close"]
    d["vwap"] = _session_vwap(d)
    d["vwap_slope"] = d["vwap"].diff()
    d["nhb"] = _new_high_burst(d["high"], cfg.nhb_window)
    d["thrust"] = d["close"].pct_change(cfg.thrust_win)

    if rvol_baseline is not None:
        # baseline indexed by minute-of-day; align on it
        mod = d["datetime"].dt.strftime("%H:%M")
        base = mod.map(rvol_baseline).replace(0, np.nan)
        d["rvol"] = d["cum_vol"] / base
    else:
        d["rvol"] = np.nan
    return d


def rvol_baseline_for_symbol(sessions: list[pd.DataFrame], cfg: Config) -> dict:
    """median cumulative volume at each minute-of-day over a trailing window.

    Returns {session_date: Series(index=minute_str -> baseline)} so each session
    only uses PRIOR sessions (no look-ahead).
    """
    per_minute = []  # list of (date, Series minute->cumvol)
    for s in sessions:
        s = s.sort_values("datetime")
        cv = s["volume"].cumsum()
        idx = s["datetime"].dt.strftime("%H:%M").values
        per_minute.append((
            s["datetime"].dt.date.iloc[0],
            pd.Series(cv.values, index=idx),
        ))

    baselines = {}
    for i, (date, _) in enumerate(per_minute):
        prior = [ser for (_, ser) in per_minute[max(0, i - cfg.rvol_window_sessions):i]]
        if not prior:
            baselines[date] = None
            continue
        mat = pd.concat(prior, axis=1)
        baselines[date] = mat.median(axis=1)
    return baselines


# --------------------------------------------------------------------------- #
# Signal + per-session simulation
# --------------------------------------------------------------------------- #
def _passes_gates(row, cfg: Config, side: str) -> bool:
    t = row["datetime"].time()
    if not (cfg.no_entry_before <= t <= cfg.no_entry_after):
        return False
    if not (row["rvol"] >= cfg.rvol_min):
        return False
    if not (row["cum_turnover_cr"] >= cfg.turnover_min_cr):
        return False
    velocity = (row["nhb"] >= cfg.nhb_min) or (
        row["thrust"] >= cfg.thrust_k * row["atr_pct"] if side == "long"
        else -row["thrust"] >= cfg.thrust_k * row["atr_pct"]
    )
    if not velocity:
        return False
    if cfg.use_vwap_filter:
        if side == "long" and not (row["close"] > row["vwap"] and row["vwap_slope"] >= 0):
            return False
        if side == "short" and not (row["close"] < row["vwap"] and row["vwap_slope"] <= 0):
            return False
    return True


def simulate_session(
    d: pd.DataFrame, cfg: Config, equity: float, side: str
) -> dict | None:
    """One symbol, one session, at most one trade. Returns a trade dict or None."""
    n = len(d)
    entry_i = None
    for i in range(cfg.atr_len, n):
        if d.iloc[i]["datetime"].time() > cfg.no_entry_after:
            break
        if pd.isna(d.iloc[i]["rvol"]):
            continue
        if _passes_gates(d.iloc[i], cfg, side):
            entry_i = i
            break

    if entry_i is None:
        return None

    # optional pullback entry: wait for a lower-volume bar then a break of its high
    if cfg.use_pullback_entry:
        pb = None
        for j in range(entry_i + 1, min(entry_i + cfg.nhb_window, n - 1)):
            if d.iloc[j]["volume"] < d.iloc[j - 1]["volume"]:
                pb = j
                break
        if pb is None:
            return None
        trig = None
        for j in range(pb + 1, n):
            if (
                side == "long"
                and d.iloc[j]["high"] > d.iloc[pb]["high"]
                and d.iloc[j]["volume"] >= cfg.pb_vol_x * d.iloc[pb]["volume"]
            ):
                trig = j
                break
            if (
                side == "short"
                and d.iloc[j]["low"] < d.iloc[pb]["low"]
                and d.iloc[j]["volume"] >= cfg.pb_vol_x * d.iloc[pb]["volume"]
            ):
                trig = j
                break
        if trig is None:
            return None
        entry_i = trig

    e = d.iloc[entry_i]
    entry = e["close"]
    atr = max(e["atr"], 1e-6)
    stop = entry - cfg.atr_mult * atr if side == "long" else entry + cfg.atr_mult * atr
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return None

    qty = (cfg.risk_frac * equity) / stop_dist
    target = (
        entry + cfg.scale_target_atr * atr
        if side == "long"
        else entry - cfg.scale_target_atr * atr
    )

    scaled = False
    realized = 0.0
    rem = qty
    exit_price = entry
    cost_rt = (cfg.cost_bps + cfg.slippage_bps) / 1e4

    for k in range(entry_i + 1, n):
        bar = d.iloc[k]

        # force-flat
        if bar["datetime"].time() >= cfg.force_flat:
            exit_price = bar["close"]
            realized += rem * (
                (exit_price - entry) if side == "long" else (entry - exit_price)
            )
            rem = 0.0
            break

        # stop
        hit_stop = (bar["low"] <= stop) if side == "long" else (bar["high"] >= stop)
        if hit_stop:
            exit_price = stop
            realized += rem * (
                (exit_price - entry) if side == "long" else (entry - exit_price)
            )
            rem = 0.0
            break

        # scale at target within window
        if (not scaled) and (k - entry_i) <= cfg.scale_max_bars:
            hit_tgt = (bar["high"] >= target) if side == "long" else (bar["low"] <= target)
            if hit_tgt:
                cut = cfg.scale_frac * qty
                realized += cut * (
                    (target - entry) if side == "long" else (entry - target)
                )
                rem -= cut
                scaled = True
                stop = entry  # move remainder to breakeven

        # trail remainder on vwap loss after scaling
        if scaled:
            vwap_loss = (
                (bar["close"] < bar["vwap"]) if side == "long"
                else (bar["close"] > bar["vwap"])
            )
            if vwap_loss:
                exit_price = bar["close"]
                realized += rem * (
                    (exit_price - entry) if side == "long" else (entry - exit_price)
                )
                rem = 0.0
                break

    if rem > 0:  # ran off the end
        exit_price = d.iloc[-1]["close"]
        realized += rem * (
            (exit_price - entry) if side == "long" else (entry - exit_price)
        )

    gross = realized
    notional = qty * entry
    costs = notional * cost_rt
    pnl = gross - costs
    R = pnl / (cfg.risk_frac * equity)

    return {
        "date": e["datetime"].date(),
        "entry_time": e["datetime"].time().strftime("%H:%M"),
        "side": side,
        "entry": round(entry, 2),
        "qty": round(qty, 2),
        "pnl": pnl,
        "R": R,
        "costs": costs,
    }


# --------------------------------------------------------------------------- #
# Portfolio loop
# --------------------------------------------------------------------------- #
def run_backtest(data_by_symbol: dict[str, pd.DataFrame], cfg: Config) -> pd.DataFrame:
    sides = {"long": ["long"], "short": ["short"], "both": ["long", "short"]}[cfg.direction]

    # group every symbol into sessions, precompute rvol baselines
    sym_sessions = {}
    sym_baselines = {}
    for sym, df in data_by_symbol.items():
        df = df.sort_values("datetime").copy()
        df["d"] = df["datetime"].dt.date
        sessions = [g.drop(columns="d") for _, g in df.groupby("d")]
        sym_sessions[sym] = sessions
        sym_baselines[sym] = rvol_baseline_for_symbol(sessions, cfg)

    # collect candidate trades day by day
    all_dates = sorted({
        s["datetime"].dt.date.iloc[0]
        for sess in sym_sessions.values()
        for s in sess
    })
    trades = []
    equity = cfg.starting_equity

    for date in all_dates:
        day_trades = []
        for sym, sessions in sym_sessions.items():
            sess = next(
                (s for s in sessions if s["datetime"].dt.date.iloc[0] == date), None
            )
            if sess is None or len(sess) < cfg.atr_len + 2:
                continue
            base = sym_baselines[sym].get(date)
            feat = build_session_features(sess, base, cfg)
            for side in sides:
                tr = simulate_session(feat, cfg, equity, side)
                if tr:
                    tr["symbol"] = sym
                    day_trades.append(tr)
                    break  # one side per symbol/day

        # cap concurrency, apply daily circuit breaker on cumulative R
        day_trades.sort(key=lambda x: x["entry_time"])
        cum_R = 0.0
        taken = 0
        for tr in day_trades:
            if taken >= cfg.max_concurrent:
                break
            if cum_R <= cfg.daily_loss_cb_R:
                break
            trades.append(tr)
            cum_R += tr["R"]
            equity += tr["pnl"]
            taken += 1

    return pd.DataFrame(trades)


# --------------------------------------------------------------------------- #
# Metrics — V_BLD_B comparable
# --------------------------------------------------------------------------- #
def metrics(trades: pd.DataFrame, cfg: Config) -> dict:
    if trades.empty:
        return {"trades": 0, "verdict": "NO TRADES"}

    daily = trades.groupby("date")["pnl"].sum()
    ret = daily / cfg.starting_equity
    sharpe = (ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else float("nan")

    wins = trades[trades["pnl"] > 0]["pnl"]
    losses = trades[trades["pnl"] <= 0]["pnl"]
    win_rate = len(wins) / len(trades)
    payoff = (
        (wins.mean() / abs(losses.mean()))
        if len(losses) and losses.mean() != 0
        else float("nan")
    )

    eq = cfg.starting_equity + daily.cumsum()
    max_dd = ((eq - eq.cummax()) / eq.cummax()).min()

    monthly = daily.copy()
    monthly.index = pd.to_datetime(monthly.index)
    by_month = monthly.resample("ME").sum()
    green_months = (by_month > 0).mean() if len(by_month) else float("nan")

    passes = (
        len(trades) >= cfg.min_trades
        and sharpe >= 1.41
        and green_months >= 0.70
        and payoff >= 1.67
    )

    return {
        "trades": int(len(trades)),
        "win_rate": round(win_rate, 3),
        "payoff_ratio": round(payoff, 3),
        "sharpe_ann": round(sharpe, 3),
        "green_months": round(float(green_months), 3),
        "max_drawdown": round(float(max_dd), 4),
        "net_pnl": round(float(trades["pnl"].sum()), 2),
        "avg_R": round(float(trades["R"].mean()), 3),
        "verdict": "CLEARS V_BLD_B" if passes else "REJECTED (see gate)",
    }


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #
def load_data(data_dir: str, symbols: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for sym in symbols:
        for ext, reader in ((".parquet", pd.read_parquet), (".csv", pd.read_csv)):
            path = os.path.join(data_dir, f"{sym}{ext}")
            if os.path.exists(path):
                df = reader(path)
                df["datetime"] = pd.to_datetime(df["datetime"])
                out[sym] = df[["datetime", "open", "high", "low", "close", "volume"]]
                break
    return out


def _make_smoke_data(
    n_days: int = 25, n_min: int = 375, n_sym: int = 3, seed: int = 7
) -> dict[str, pd.DataFrame]:
    """Dummy bars to validate plumbing only. NOT a backtest — results meaningless."""
    rng = np.random.default_rng(seed)
    data = {}
    base = pd.Timestamp("2025-01-01 09:15")
    for s in range(n_sym):
        frames = []
        for d in range(n_days):
            day0 = base + pd.Timedelta(days=d)
            t = pd.date_range(day0, periods=n_min, freq="1min")
            price = 1000 + np.cumsum(rng.normal(0, 1.2, n_min))
            if rng.random() < 0.5:  # inject an afternoon ignition some days
                k = rng.integers(60, 300)
                price[k:] += np.linspace(0, rng.uniform(8, 25), n_min - k)
            high = price + rng.uniform(0, 1.5, n_min)
            low = price - rng.uniform(0, 1.5, n_min)
            vol = rng.uniform(1e4, 5e4, n_min) * (1 + (price - price[0]).clip(min=0) / 5)
            frames.append(pd.DataFrame({
                "datetime": t,
                "open": price,
                "high": high,
                "low": low,
                "close": price,
                "volume": vol,
            }))
        data[f"SMOKE{s}"] = pd.concat(frames, ignore_index=True)
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="dir of <SYMBOL>.parquet|.csv minute bars")
    ap.add_argument("--symbols", help="text file, one symbol per line")
    ap.add_argument("--smoke", action="store_true", help="validate plumbing on dummy bars")
    ap.add_argument("--direction", default="long", choices=["long", "short", "both"])
    args = ap.parse_args()

    cfg = Config(direction=args.direction)

    if args.smoke:
        data = _make_smoke_data()
        print(">> SMOKE MODE: dummy bars, plumbing check only — metrics are MEANINGLESS\n")
    else:
        if not (args.data and args.symbols):
            ap.error("provide --data and --symbols, or use --smoke")
        syms = [line.strip() for line in open(args.symbols) if line.strip()]
        data = load_data(args.data, syms)
        if not data:
            ap.error("no symbol files loaded — check schema/paths")

    trades = run_backtest(data, cfg)
    m = metrics(trades, cfg)

    print("Config:", {
        k: v for k, v in asdict(cfg).items()
        if k in ("rvol_min", "nhb_min", "thrust_k", "atr_mult", "direction")
    })
    print("\n=== R38 In-Play Momentum — results ===")
    for k, v in m.items():
        print(f"  {k:>14}: {v}")

    if not trades.empty:
        out = "r38_trades.csv"
        trades.to_csv(out, index=False)
        print(f"\n  trade log -> {out} ({len(trades)} rows)")


if __name__ == "__main__":
    main()
