"""R81 placebo lookup (R67 method): for every ELIGIBLE symbol-day in a period and every completed 5m close from
09:30 to 14:30, the net Rs of a long and a short entered at that close under 'initial stop (1.5 x continuous 5m
Wilder ATR14, min Rs1) else 15:19 close', engine sizing min(Rs1L/price, Rs500/risk), 2.4 bps slippage per side,
corrected charges. Candidates' own entries are priced from the same table, so the comparison is like for like.
-> data/placebo_<period>.parquet
  uv run python backtest/r81_entry/placebo_table.py <period> [N_PROCS]
"""
import datetime as dt
import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

ENDS = [f"{h:02d}:{m:02d}" for h in range(9, 15) for m in range(0, 60, 5) if "09:30" <= f"{h:02d}:{m:02d}" <= "14:30"]


def net_rs(dirn, q, e, x):
    ef, xf = e * (1 + dirn * C.SLIP), x * (1 - dirn * C.SLIP)
    bv, sv = (ef * q, xf * q) if dirn > 0 else (xf * q, ef * q)
    return dirn * (xf - ef) * q - C.charges_orders([(True, bv), (False, sv)])["total"]


def work(args):
    sym, days, period = args
    lo = min(days) - dt.timedelta(days=12)
    m = C.load_1m(sym, lo, max(days))
    if m.empty:
        return []
    m["day"] = m.ts.dt.date
    g5 = m.groupby(m.ts.dt.floor("5min")).agg(high=("high", "max"), low=("low", "min"), close=("close", "last")).reset_index()
    pc = g5.close.shift()
    tr = np.maximum.reduce([g5.high - g5.low, (g5.high - pc).abs(), (g5.low - pc).abs()])
    tr = pd.Series(tr).fillna(g5.high - g5.low)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    g5["end"] = g5.ts + pd.Timedelta(minutes=5)
    atr_at = dict(zip(g5.end, atr))
    close_at = dict(zip(g5.end, g5.close))
    out = []
    dset = set(days)
    for day, g in m.groupby("day"):
        if day not in dset:
            continue
        g = g[g.ts.dt.time < dt.time(15, 20)].reset_index(drop=True)
        if len(g) < 300:
            continue
        o, h, lw, c = g.open.values, g.high.values, g.low.values, g.close.values
        tsv = g.ts.values
        for hhmm in ENDS:
            end = pd.Timestamp(dt.datetime.combine(day, dt.time(int(hhmm[:2]), int(hhmm[3:]))))
            if end not in close_at:
                continue
            e, a = close_at[end], atr_at[end]
            R = max(1.5 * a, 1.0)
            q = int(min(100000 // e, 500 // R))
            if q <= 0:
                continue
            s = int(np.searchsorted(tsv, end.to_datetime64()))
            if s >= len(c):
                continue
            for dirn in (1, -1):
                stop = e - dirn * R
                hit = np.nonzero(lw[s:] <= stop)[0] if dirn > 0 else np.nonzero(h[s:] >= stop)[0]
                if len(hit):
                    j = s + hit[0]
                    x = min(o[j], stop) if dirn > 0 else max(o[j], stop)
                else:
                    x = c[-1]
                out.append((sym, day, hhmm, dirn, net_rs(dirn, q, e, x)))
    return out


if __name__ == "__main__":
    period = sys.argv[1]
    from select_entries import load_all
    F, _ = load_all(period)
    el = F[F.elig]
    jobs = [(s, sorted(g.day.unique()), period) for s, g in el.groupby("symbol")]
    rows = []
    with Pool(int(sys.argv[2]) if len(sys.argv) > 2 else 4) as p:
        for i, r in enumerate(p.imap_unordered(work, jobs), 1):
            rows += r
            if i % 25 == 0:
                print(f"{i}/{len(jobs)} rows={len(rows)}", flush=True)
    P = pd.DataFrame(rows, columns=["symbol", "day", "end", "side", "net"])
    P.to_parquet(C.D / f"placebo_{period}.parquet", index=False)
    print("DONE", len(P), flush=True)
