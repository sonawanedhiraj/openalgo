"""R82 Phase 1: concurrent open positions + peak concurrent notional vs Rs1L buying power (Rs20k x 5 MIS)."""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BP = 100_000.0


def sweep(df, key):
    """df: day, t_in, t_out, notional. Returns per-day max concurrent count and peak notional."""
    out = []
    for d, g in df.groupby(key):
        ev = [(r.t_in, 1, r.notional) for r in g.itertuples()] + [
            (r.t_out, 0, r.notional) for r in g.itertuples()
        ]
        ev.sort(key=lambda e: (e[0], e[1]))  # exits before entries at the same instant
        n = cur = mx = pk = 0
        for _t, kind, v in ev:
            if kind:
                n += 1
                cur += v
            else:
                n -= 1
                cur -= v
            mx, pk = max(mx, n), max(pk, cur)
        out.append({"day": d, "trades": len(g), "max_conc": mx, "peak_notional": pk})
    return pd.DataFrame(out)


def summ(name, T):
    P = sweep(T, "grp")
    r = {
        "source": name,
        "trades": int(len(T)),
        "days": int(len(P)),
        "mean_notional": round(T.notional.mean()),
        "median_notional": round(T.notional.median()),
        "pct_trades_notional_gt_80k": round(100 * (T.notional > 80000).mean(), 1),
        "days_conc_ge2_pct": round(100 * (P.max_conc >= 2).mean(), 1),
        "max_conc_dist": {
            int(k): int(v) for k, v in P.max_conc.value_counts().sort_index().items()
        },
        "days_breach_1L_pct": round(100 * (P.peak_notional > BP * 1.0001).mean(), 1),
        "peak_notional_p50": round(P.peak_notional.median()),
        "peak_notional_p90": round(P.peak_notional.quantile(0.9)),
        "peak_notional_max": round(P.peak_notional.max()),
    }
    # trades that, at entry, would not fit in remaining buying power
    T = T.sort_values("t_in")
    unf = 0
    unf_net = []
    for _g, g in T.groupby("grp"):
        rows = list(g.itertuples())
        for i, x in enumerate(rows):
            used = sum(o.notional for o in rows[:i] if o.t_out > x.t_in)
            if used + x.notional > BP * 1.0001:
                unf += 1
                unf_net.append(getattr(x, "net", np.nan))
    r["trades_unfundable_at_entry"] = unf
    r["trades_unfundable_pct"] = round(100 * unf / len(T), 1)
    if "net" in T:
        r["net_all"] = round(T.net.sum())
        r["net_unfundable"] = round(np.nansum(unf_net))
        r["net_per_trade_all"] = round(T.net.mean(), 1)
        r["net_per_trade_unfundable"] = round(np.nanmean(unf_net), 1) if unf_net else None
    return r, P


res = []
# 1) live sandbox journal (511)
J = pd.read_csv(ROOT / "backtest/equity_doc_audit/engine_trades.csv")
J["t_in"] = pd.to_datetime(J.entry_fill_at.fillna(J.placed_at), format="ISO8601", utc=True)
J["t_out"] = pd.to_datetime(J.exited_at, format="ISO8601", utc=True)
J = J[J.t_out.notna()]
J["grp"] = J.d
r, PJ = summ("live sandbox journal 2026-06..(engine_trades.csv)", J)
res.append(r)
PJ.to_csv(ROOT / "backtest/r82_sizing/data_live_daily.csv", index=False)
# 2) R80 engine replay (in-house scanner, 2024-01..2025-06)
for f in ("trades_r80.parquet",):
    T = pd.read_parquet(ROOT / "backtest/r80_scanner/data" / f)


res = [res[0]]
T = pd.read_parquet(ROOT / "backtest/r80_scanner/data/trades_r80.parquet")
T = T.assign(
    t_in=pd.to_datetime(T.entry_ts),
    t_out=pd.to_datetime(T.exit_a_ts),
    notional=T.qty * T.entry,
    net=T.net_a,
    grp=T.day,
)
r, _ = summ("R80 engine replay, in-house scanner 2024-01..2025-06 (exit A)", T)
res.append(r)
D81 = ROOT / "backtest/r81_entry/data"
for c in ("E1", "E2", "E3", "E4", "E5"):
    parts = []
    for p in ("train", "validate", "holdout"):
        f = D81 / f"trades_{c}_{p}.parquet"
        if f.exists():
            x = pd.read_parquet(f)
            parts.append(x[x.variant == c])
    T = pd.concat(parts)
    T = T.assign(t_in=pd.to_datetime(T.time), t_out=pd.to_datetime(T.exit_time), grp=T.day)
    r, _ = summ(f"R81 {c} 2019-01..2025-06", T)
    res.append(r)
json.dump(
    res, open(ROOT / "backtest/r82_sizing/phase1_concurrency.json", "w"), indent=1, default=str
)
for r in res:
    print(
        r["source"],
        "| trades",
        r["trades"],
        "| days",
        r["days"],
        "| conc>=2 days %",
        r["days_conc_ge2_pct"],
        "| breach %",
        r["days_breach_1L_pct"],
        "| peak p50/p90/max",
        r["peak_notional_p50"],
        r["peak_notional_p90"],
        r["peak_notional_max"],
        "| unfundable %",
        r["trades_unfundable_pct"],
        "| net/trade all vs unfundable",
        r.get("net_per_trade_all"),
        r.get("net_per_trade_unfundable"),
        "| maxconc",
        r["max_conc_dist"],
    )
