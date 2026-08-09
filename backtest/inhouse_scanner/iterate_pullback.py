"""Apply the intraday_pullback_top2 learnings as an ITERATION LADDER on the R56
scanner signals, to test whether a disciplined subset carries a forward edge.

Background: R56 showed the raw scanner signals have ~zero aggregate intraday
edge. intraday_pullback_top2's validated edge is *selection discipline* on the
same kind of signal — mid-strength band [+1.0,+2.5%) gainers (extended gainers
mean-revert), NIFTY-regime + momentum gated, sector-green, top-2, morning window,
hold-to-EOD. The simplified engine already IS a pullback-breakout entry, so the
levers to port are the selection filters, not the entry mechanic.

This harness works at the SIGNAL-FORWARD-RETURN level (fast, decisive) — it does
not run the engine. For each iteration it reports, LONG-signed:
  ret_to_close  — fire_price → same-day close
  ret_to_t1     — fire_price → next-day close  (the R56 overnight edge)
with split-half (H1/H2) means + win% so a spurious whole-sample number is caught.

NIFTY-regime / nf_mom levers need NIFTY intraday and are handled separately
(they require a NIFTY 1m fetch that prices.duckdb excludes) — this file covers the
data-free levers: long-only, mid-strength band cap, morning window, top-N/day.

Run:  uv run python -m backtest.inhouse_scanner.iterate_pullback
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DB_PATH = REPO_ROOT / "outputs" / "tod_volume_gate" / "prices.duckdb"
OUT_DIR = REPO_ROOT / "backtest" / "inhouse_scanner"
HALF_SPLIT = "2026-01-01"  # H1 = 2025-06..2025-12, H2 = 2026-01..2026-07


def load_signals_with_forward() -> pd.DataFrame:
    sig = pd.read_parquet(OUT_DIR / "signals.parquet")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    sameday = con.execute(
        "SELECT symbol, CAST(ts AS DATE) d, last(close ORDER BY ts) c FROM prices_1m "
        "WHERE strftime(ts,'%H:%M') BETWEEN '09:15' AND '15:29' GROUP BY symbol, d"
    ).df()
    sameday["d"] = sameday["d"].astype(str)
    sc = {(r.symbol, r.d): r.c for r in sameday.itertuples()}
    daily = con.execute(
        "SELECT symbol, CAST(bar_date AS DATE) d, close FROM prices_daily ORDER BY symbol, d"
    ).df()
    daily["d"] = daily["d"].astype(str)
    by_sym = {s: g.reset_index(drop=True) for s, g in daily.groupby("symbol")}
    con.close()

    def t1(sym, day):
        g = by_sym.get(sym)
        if g is None:
            return np.nan
        idx = g.index[g["d"] > day]
        return float(g.loc[idx[0], "close"]) if len(idx) else np.nan

    sig["sgn"] = np.where(sig["direction"] == "BUY", 1.0, -1.0)
    sig["close_px"] = [sc.get((r.symbol, r.day), np.nan) for r in sig.itertuples()]
    sig["t1_px"] = [t1(r.symbol, r.day) for r in sig.itertuples()]
    sig["ret_to_close"] = sig["sgn"] * (sig["close_px"] / sig["fire_price"] - 1.0)
    sig["ret_to_t1"] = sig["sgn"] * (sig["t1_px"] / sig["fire_price"] - 1.0)
    sig["gap_pct"] = sig["ret_at_fire"] * 100.0 * sig["sgn"]  # +ve = how far in-favour at fire
    sig["fire_hhmm"] = pd.to_datetime(sig["fire_ts"]).dt.strftime("%H:%M")
    sig["half"] = np.where(sig["day"] < HALF_SPLIT, "H1", "H2")
    return sig


def edge(sub: pd.DataFrame, col: str) -> str:
    r = sub[col].dropna()
    if len(r) == 0:
        return "n=0"
    h1 = sub[sub["half"] == "H1"][col].dropna()
    h2 = sub[sub["half"] == "H2"][col].dropna()
    return (
        f"mean={r.mean() * 100:+.3f}%  win={(r > 0).mean() * 100:4.1f}%  n={len(r):4}  "
        f"| H1 {h1.mean() * 100:+.3f}% (n={len(h1)})  H2 {h2.mean() * 100:+.3f}% (n={len(h2)})"
    )


def report(name: str, sub: pd.DataFrame) -> None:
    print(f"\n[{name}]  {len(sub)} signals")
    print(f"    ret_to_close : {edge(sub, 'ret_to_close')}")
    print(f"    ret_to_t1    : {edge(sub, 'ret_to_t1')}")


def topn_per_day(sub: pd.DataFrame, n: int, rank_col: str, asc: bool) -> pd.DataFrame:
    return (
        sub.sort_values(["day", rank_col], ascending=[True, asc])
        .groupby("day", group_keys=False)
        .head(n)
    )


def main() -> int:
    sig = load_signals_with_forward()
    print("=" * 78)
    print("  PULLBACK-LEVER ITERATION LADDER on R56 scanner signals (signal-level edge)")
    print(f"  {len(sig)} signals | split {HALF_SPLIT} | LONG-signed forward returns")
    print("=" * 78)

    report("I0  baseline — ALL signals (R56)", sig)

    longs = sig[sig["direction"] == "BUY"].copy()
    report("I1  long-only (drop SELL)", longs)

    # I2 — mid-strength band cap. Pullback edge = [+1.0,+2.5%). Scanner gate is
    # >+1.5% (no upper cap). Slice by gap-at-fire to find where edge lives.
    print("\n--- I2: gap-at-fire band buckets (long-only) ---")
    bands = [(1.5, 2.0), (1.5, 2.5), (2.0, 3.0), (2.5, 5.0), (3.0, 100.0), (5.0, 100.0)]
    for lo, hi in bands:
        b = longs[(longs["gap_pct"] >= lo) & (longs["gap_pct"] < hi)]
        report(f"I2  band [{lo},{hi})%", b)

    # I3 — morning window. Pullback edge is 09:30–11:00; scanner fires late.
    print("\n--- I3: time-of-day window (long-only) ---")
    for lbl, lo, hi in [
        ("morning 09:30-11:00", "09:30", "11:00"),
        ("11:00-13:00", "11:00", "13:00"),
        ("13:00-15:00", "13:00", "15:00"),
        ("after 15:00", "15:00", "15:59"),
    ]:
        w = longs[(longs["fire_hhmm"] >= lo) & (longs["fire_hhmm"] < hi)]
        report(f"I3  {lbl}", w)

    # Band × window grid — locate the joint edge cell (long-only).
    print("\n--- band x window grid (long-only, ret_to_close mean% | H1 | H2 | win% | n) ---")
    win_defs = [
        ("morn 0930-1100", "09:30", "11:00"),
        ("noon 1100-1300", "11:00", "13:00"),
        ("aft 1300-1500", "13:00", "15:00"),
    ]
    band_defs = [(1.5, 2.5), (2.5, 3.0), (3.0, 100.0)]
    for lo, hi in band_defs:
        for wl, wa, wb in win_defs:
            c = longs[
                (longs["gap_pct"] >= lo)
                & (longs["gap_pct"] < hi)
                & (longs["fire_hhmm"] >= wa)
                & (longs["fire_hhmm"] < wb)
            ]
            r = c["ret_to_close"].dropna()
            if len(r) < 20:
                print(f"    band[{lo},{hi}) {wl}: n={len(r)} (thin)")
                continue
            h1 = c[c["half"] == "H1"]["ret_to_close"].dropna()
            h2 = c[c["half"] == "H2"]["ret_to_close"].dropna()
            print(
                f"    band[{lo},{hi}) {wl}: {r.mean() * 100:+.3f}%  "
                f"H1 {h1.mean() * 100:+.3f}%  H2 {h2.mean() * 100:+.3f}%  "
                f"win {(r > 0).mean() * 100:.1f}%  n={len(r)}"
            )

    # I4 — the data-selected joint cut: mid-band AND afternoon (the good window
    # for late-firing scanner signals; mirrors pullback's afternoon[13:00,15:00)).
    print("\n--- I4: mid-band [1.5,2.5) AND afternoon [13:00,15:00) ---")
    mid_aft = longs[
        (longs["gap_pct"] >= 1.5)
        & (longs["gap_pct"] < 2.5)
        & (longs["fire_hhmm"] >= "13:00")
        & (longs["fire_hhmm"] < "15:00")
    ]
    report("I4  mid-band AND afternoon", mid_aft)

    # I5 — top-2/day on I4. Pullback ranks strongest gainer 'desc'; test both.
    if len(mid_aft):
        report(
            "I5  I4 + top-2/day (gap desc=strongest)", topn_per_day(mid_aft, 2, "gap_pct", False)
        )
        report("I5  I4 + top-2/day (gap asc=weakest)", topn_per_day(mid_aft, 2, "gap_pct", True))

    # I6 — widen band to [1.5,3.0) with afternoon — more trades, still capped.
    print("\n--- I6: band [1.5,3.0) AND afternoon [13:00,15:00) ---")
    wide_aft = longs[
        (longs["gap_pct"] >= 1.5)
        & (longs["gap_pct"] < 3.0)
        & (longs["fire_hhmm"] >= "13:00")
        & (longs["fire_hhmm"] < "15:00")
    ]
    report("I6  band[1.5,3.0) AND afternoon", wide_aft)
    if len(wide_aft):
        report("I6  + top-2/day (gap desc)", topn_per_day(wide_aft, 2, "gap_pct", False))

    print("\n" + "=" * 78)
    print("  Read: a real edge shows the SAME sign in H1 and H2 with win% > 50 and")
    print("  a mean comfortably above ~0.06% (round-trip MIS cost). t1 = overnight.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
