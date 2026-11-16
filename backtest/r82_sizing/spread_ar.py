"""Abdi-Ranaldo (2017) bar-based effective-spread proxy from 1m bars (09:30-15:15), TRAIN period only.
Per symbol (pooled products), by 30-min bucket and by liquidity (traded value) quintile. Worst-quintile symbols
-> data/spread_excluded_<cand>.json (frozen before any XS test). A PROXY: no real equity fills exist.
  uv run python backtest/r82_sizing/spread_ar.py BASE|E3S [day_step]"""

import datetime as dt
import json
import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_r82 as R  # noqa: E402

STEP = 1


def work(args):
    sym, days, step = args
    m = R.load_1m(sym, min(days), max(days))
    if m.empty:
        return []
    m = m[m.ts.dt.date.isin(set(days[::step]))]
    t = m.ts.dt.strftime("%H:%M")
    m = m[(t >= "09:30") & (t <= "15:15")].copy()
    if m.empty:
        return []
    m["day"] = m.ts.dt.date
    c = np.log(m.close.values)
    eta = (np.log(m.high.values) + np.log(m.low.values)) / 2
    same = np.r_[m.day.values[1:] == m.day.values[:-1], False]
    prod = np.where(same, (c - eta) * (c - np.r_[eta[1:], np.nan]), np.nan)
    m["prod"] = prod
    m["bucket"] = m.ts.dt.floor("30min").dt.strftime("%H:%M")
    m["val"] = m.close * m.volume
    rows = []
    for (d, b), g in m.groupby(["day", "bucket"]):
        p = g["prod"].dropna()
        if len(p):
            rows.append((sym, d, b, float(p.sum()), int(len(p)), float(g.val.sum())))
    return rows


def main(cand):
    lo, hi = R.PERIODS[cand]["train"]
    if cand == "BASE":
        s = R.signals_for("BASE", "train")
        syms = sorted(s.symbol.unique())
    else:
        s = R.signals_for("E3S", "train")
        syms = sorted(s.symbol.unique())
    days = [d.date() for d in pd.bdate_range(lo, hi)]
    with Pool(6) as p:
        rows = [r for rr in p.imap_unordered(work, [(x, days, STEP) for x in syms]) for r in rr]
    X = pd.DataFrame(rows, columns=["symbol", "day", "bucket", "psum", "n", "val"])
    per = X.groupby("symbol").agg(
        psum=("psum", "sum"), n=("n", "sum"), val=("val", "sum"), days=("day", "nunique")
    )
    per["ar_bps"] = 1e4 * np.sqrt(np.maximum(4 * per.psum / per.n, 0))
    per["adv"] = per.val / per.days
    per["adv_q"] = pd.qcut(per.adv.rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
    tod = X.groupby("bucket").agg(psum=("psum", "sum"), n=("n", "sum"))
    tod["ar_bps"] = 1e4 * np.sqrt(np.maximum(4 * tod.psum / tod.n, 0))
    liq = per.groupby("adv_q", observed=True).agg(
        ar_bps_median=("ar_bps", "median"), n=("ar_bps", "size"), adv_median=("adv", "median")
    )
    cut = per.ar_bps.quantile(0.8)
    excl = sorted(per.index[per.ar_bps > cut])
    json.dump(
        {
            "cand": cand,
            "train": [str(lo), str(hi)],
            "cut_bps": round(float(cut), 2),
            "excluded": excl,
            "n_symbols": int(len(per)),
        },
        open(R.D / f"spread_excluded_{cand}.json", "w"),
        indent=1,
    )
    per.round(3).to_csv(R.D / f"spread_ar_{cand}_symbols.csv")
    rep = {
        "cand": cand,
        "median_symbol_ar_bps": round(float(per.ar_bps.median()), 2),
        "cut_bps": round(float(cut), 2),
        "by_time_of_day_bps": tod.ar_bps.round(2).to_dict(),
        "by_liquidity_quintile": {
            str(k): {kk: round(float(vv), 2) for kk, vv in v.items()}
            for k, v in liq.to_dict("index").items()
        },
        "n_excluded": len(excl),
    }
    json.dump(rep, open(R.D / f"spread_ar_{cand}.json", "w"), indent=1)
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    STEP = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    main(sys.argv[1])
