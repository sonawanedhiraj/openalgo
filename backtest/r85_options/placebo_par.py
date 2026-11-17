"""Parallel placebo draws for one candidate/period (central spread). Each worker has its own seed.
uv run python backtest/r85_options/placebo_par.py <cand> <period> <draws> <procs> -> data/placebo_<cand>_<period>.json"""

import json
import sys
from multiprocessing import Pool

import cands as C
import numpy as np


def work(args):
    cand, period, k, seed = args
    C.rng = np.random.default_rng(seed)
    return C.placebo(cand, period, 0.0, k)["vals"]


if __name__ == "__main__":
    cand, period, draws, procs = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    per = [draws // procs + (1 if i < draws % procs else 0) for i in range(procs)]
    with Pool(procs) as p:
        vals = [
            v
            for vv in p.map(work, [(cand, period, k, 1000 + i) for i, k in enumerate(per)])
            for v in vv
        ]
    import pandas as pd

    actual = float(pd.read_parquet(C.E.D / f"trades_{cand}_{period}_central.parquet").net.mean())
    vals = np.array(vals)
    out = {
        "cand": cand,
        "period": period,
        "draws": len(vals),
        "actual_net_per_trade": round(actual, 1),
        "placebo_mean": round(float(vals.mean()), 1),
        "placebo_p95": round(float(np.percentile(vals, 95)), 1),
        "p": round(float((vals >= actual).mean()), 3),
    }
    json.dump(out, open(C.E.D / f"placebo_{cand}_{period}.json", "w"), indent=1)
    print(out, flush=True)
