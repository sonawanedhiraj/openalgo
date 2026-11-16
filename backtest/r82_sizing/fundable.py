"""R82 Phase 1b: replay each book first-come with a Rs1L buying-power ledger (accepted positions only):
a trade is taken only if its notional fits in Rs1L minus notional of ACCEPTED positions still open."""

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BP = 100_000.0


def greedy(T):
    T = T.sort_values(["grp", "t_in"])
    keep = []
    for _, g in T.groupby("grp"):
        open_ = []
        for r in g.itertuples():
            open_ = [o for o in open_ if o[0] > r.t_in]
            used = sum(o[1] for o in open_)
            ok = used + r.notional <= BP * 1.0001
            keep.append(ok)
            if ok:
                open_.append((r.t_out, r.notional))
    T = T.copy()
    T["fundable"] = keep
    return T


def bps(x):
    return round(1e4 * x.net.sum() / x.notional.sum(), 2)


def load():
    J = pd.read_csv(ROOT / "backtest/equity_doc_audit/engine_trades.csv")
    J["t_in"] = pd.to_datetime(J.entry_fill_at.fillna(J.placed_at), format="ISO8601", utc=True)
    J["t_out"] = pd.to_datetime(J.exited_at, format="ISO8601", utc=True)
    J = J[J.t_out.notna()]
    J["grp"] = J.d
    yield "live sandbox journal (511)", J
    T = pd.read_parquet(ROOT / "backtest/r80_scanner/data/trades_r80.parquet")
    yield (
        "R80 replay (1,655)",
        T.assign(
            t_in=pd.to_datetime(T.entry_ts),
            t_out=pd.to_datetime(T.exit_a_ts),
            notional=T.qty * T.entry,
            net=T.net_a,
            grp=T.day,
        ),
    )
    for c in ("E1", "E2", "E3", "E4", "E5"):
        for p in ("train", "validate", "holdout"):
            x = pd.read_parquet(ROOT / f"backtest/r81_entry/data/trades_{c}_{p}.parquet")
            x = x[x.variant == c]
            yield (
                f"R81 {c} {p}",
                x.assign(t_in=pd.to_datetime(x.time), t_out=pd.to_datetime(x.exit_time), grp=x.day),
            )


out = []
for name, T in load():
    G = greedy(T)
    A, S = G[G.fundable], G[~G.fundable]
    out.append(
        {
            "book": name,
            "n": len(G),
            "n_fundable": int(len(A)),
            "pct_skipped": round(100 * len(S) / len(G), 1),
            "net_trade_all": round(G.net.mean(), 1),
            "net_trade_fundable": round(A.net.mean(), 1),
            "net_trade_skipped": round(S.net.mean(), 1) if len(S) else None,
            "net_bps_all": bps(G),
            "net_bps_fundable": bps(A),
            "total_all": round(G.net.sum()),
            "total_fundable": round(A.net.sum()),
        }
    )
D = pd.DataFrame(out)
D.to_csv(ROOT / "backtest/r82_sizing/phase1_fundable.csv", index=False)
print(D.to_string(index=False))
