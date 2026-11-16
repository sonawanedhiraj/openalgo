"""Freeze k_gap per candidate from its TRAIN S0/X0 run: mean of (fill beyond the initial stop)/R over trades that
exited at or through the initial stop (pre-registered). -> data/kgap_<cand>.json"""

import json
import sys
from pathlib import Path

import pandas as pd

D = Path(__file__).resolve().parent / "data"
cand = sys.argv[1]
T = pd.read_parquet(D / f"trades_{cand}_train_s0x0.parquet")
g = T.gap_R.dropna()
out = {
    "cand": cand,
    "k_gap": round(float(g.mean()), 4),
    "p90": round(float(g.quantile(0.9)), 4),
    "n_stop_exits": int(len(g)),
    "share_gapped": round(float((g > 0).mean()), 3),
    "n_trades": int(len(T)),
}
json.dump(out, open(D / f"kgap_{cand}.json", "w"), indent=1)
print(out)
