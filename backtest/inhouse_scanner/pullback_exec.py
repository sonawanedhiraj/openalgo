"""Pullback-style execution on the filtered scanner signals.

R56 showed the engine's ATR-stop + RR-trailing DESTROYS the mid-band/afternoon
edge (win 58.7% signal-level → 42.3% through the engine; 60% of trades die on
`stop_loss_intracandle`). That is the intraday_pullback_top2 learning verbatim:
*exit management ALL rejected — hold full-size to EOD; stops shake winners out of
the pullbacks the edge is built on.*

This simulates the pullback execution instead: **enter at the signal's fire price,
hold to the same-day close, with a WIDE (or no) intraday stop** — using the real
1m price path (min-low between fire and close) to fill stops. Sweeps stop width so
we can see the "wide/no stop is best" claim directly. Charges via the real
`compute_zerodha_intraday_charges` on a fixed ₹50k notional; returns also reported
as sizing-independent %/trade with split-half.

Run:  uv run python -m backtest.inhouse_scanner.pullback_exec --signals signals_i4.parquet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.simplified_stock_engine_core import compute_zerodha_intraday_charges  # noqa: E402

DB_PATH = REPO_ROOT / "outputs" / "tod_volume_gate" / "prices.duckdb"
OUT_DIR = REPO_ROOT / "backtest" / "inhouse_scanner"
NOTIONAL = 50_000.0
HALF = "2026-01-01"
STOPS = [None, 0.010, 0.015, 0.020, 0.030]  # None = hold-to-close, no stop


def build_paths(sig: pd.DataFrame) -> pd.DataFrame:
    """For each signal, the min-low and same-day close AFTER the fire bar."""
    con = duckdb.connect(str(DB_PATH), read_only=True)
    rows = []
    for r in sig.itertuples():
        fire = pd.Timestamp(r.fire_ts)
        day = fire.date().isoformat()
        q = con.execute(
            "SELECT min(low) ml, last(close ORDER BY ts) c FROM prices_1m "
            "WHERE symbol=? AND CAST(ts AS DATE)=? AND ts >= ? AND strftime(ts,'%H:%M')<='15:29'",
            [r.symbol, day, fire.to_pydatetime()],
        ).fetchone()
        rows.append({"min_low": q[0], "eod_close": q[1]})
    con.close()
    out = sig.copy().reset_index(drop=True)
    pth = pd.DataFrame(rows)
    out["min_low"] = pth["min_low"]
    out["eod_close"] = pth["eod_close"]
    return out


def simulate(df: pd.DataFrame, stop: float | None, slip_bps_side: float = 0.0) -> pd.DataFrame:
    entry0 = df["fire_price"].to_numpy(float)
    close = df["eod_close"].to_numpy(float)
    mlow = df["min_low"].to_numpy(float)
    # Slippage: pay UP on the buy, receive DOWN on the sell (bps per side).
    slip = slip_bps_side / 100.0 / 100.0
    entry = entry0 * (1.0 + slip)
    if stop is None:
        exit_px = close * (1.0 - slip)
        stopped = np.zeros(len(df), bool)
    else:
        stop_px = entry0 * (1.0 - stop)
        stopped = mlow <= stop_px
        exit_px = np.where(stopped, stop_px, close) * (1.0 - slip)
    qty = np.maximum((NOTIONAL / entry).astype(int), 1)
    buy_val = entry * qty
    sell_val = exit_px * qty
    gross = sell_val - buy_val
    charges = np.array(
        [compute_zerodha_intraday_charges(b, s).total for b, s in zip(buy_val, sell_val, strict=True)]
    )
    net = gross - charges
    r = df.copy()
    r["ret_gross"] = exit_px / entry - 1.0
    r["net_pnl"] = net
    r["gross_pnl"] = gross
    r["charges"] = charges
    r["stopped"] = stopped
    return r


def stats(r: pd.DataFrame, label: str) -> None:
    n = len(r)
    net = r["net_pnl"].sum()
    win = (r["net_pnl"] >= 0).mean() * 100
    rg = r["ret_gross"] * 100
    h1 = r[r["day"] < HALF]["net_pnl"].sum()
    h2 = r[r["day"] >= HALF]["net_pnl"].sum()
    r = r.copy()
    r["ym"] = r["day"].str.slice(0, 7)
    monthly = r.groupby("ym")["net_pnl"].sum()
    green = int((monthly > 0).sum())
    print(
        f"  {label:20} n={n:4}  net=₹{net:>9,.0f}  win={win:4.1f}%  "
        f"grossret={rg.mean():+.3f}%/t  stops={int(r['stopped'].sum()):3}  "
        f"green {green}/{len(monthly)}  | H1 ₹{h1:>7,.0f}  H2 ₹{h2:>7,.0f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default="signals_i4.parquet")
    args = ap.parse_args()
    sig = pd.read_parquet(OUT_DIR / args.signals)
    print(f"pullback-exec on {args.signals}: {len(sig)} signals, ₹{NOTIONAL:,.0f} notional/trade")
    df = build_paths(sig)
    df = df[df["eod_close"].notna() & df["min_low"].notna()].reset_index(drop=True)
    print(f"  priced: {len(df)}\n")
    print("  execution              trades       net      win   gross%/t  stops  green      halves")
    for stop in STOPS:
        lbl = "hold-to-close" if stop is None else f"stop {stop * 100:.1f}%"
        stats(simulate(df, stop), lbl)

    # Honesty test: slippage sweep at the best stop (1.0%). intraday_pullback's
    # open-risk #1 is unmodeled slippage (~0.05%/side → PF drifts).
    print("\n  slippage sweep @ stop 1.0% (per-side bps on entry AND exit):")
    for slip in [0.0, 2.5, 5.0, 7.5, 10.0]:
        stats(simulate(df, 0.010, slip_bps_side=slip), f"slip {slip:.1f}bps/side")
    print(
        "\n  (charges ≈ MIS intraday on ₹50k; 'gross%/t' is sizing-independent. "
        "Edge is real iff net>0 AND both halves>0 AND green months are a majority.)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
