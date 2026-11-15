"""R81 scoring, exactly as pre-registered. Writes data/result_<cand>.json + .md and appends ledger rows.
  uv run python backtest/r81_entry/score.py <cand> <periods comma list, e.g. train,validate or train,validate,holdout>
"""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

NEIGH = {"E1": ["E1_t0.75", "E1_t1.25", "E1_s0.7", "E1_s0.9"], "E2": ["E2_s0.20", "E2_s0.40", "E2_rs0.35", "E2_rs0.65"],
         "E3": ["E3_g0.75", "E3_g1.25", "E3_c10", "E3_c20"], "E4": ["E4_c0.5", "E4_c0.7", "E4_v1.25", "E4_v1.75"],
         "E5": ["E5_er0.30", "E5_er0.50", "E5_m0.35", "E5_m0.65"]}
TWIN = {"E1": ("10:15", "14:30"), "E4": ("10:20", "14:00")}  # timing-placebo windows; others 09:30-14:30 (resolution)
rng = np.random.default_rng(81)
CODE = hashlib.sha1(b"".join(open(Path(__file__).parent / f, "rb").read() for f in
                             ("PREREGISTRATION.md", "common.py", "features.py", "select_entries.py", "run_engine.py",
                              "placebo_table.py", "score.py"))).hexdigest()[:10]


def ndays(period):
    ix = pd.read_parquet(C.D / "index_features.parquet")
    lo, hi = {"train": C.TRAIN, "validate": C.VALID, "holdout": C.HOLD}[period]
    d = ix[(ix["index"] == "NIFTY") & (ix.day >= lo) & (ix.day <= hi)].day.nunique()
    return int(d)


def tstat(x, days):
    x = np.asarray(x, float)
    n = len(x)
    if n < 3:
        return np.nan
    e = x - x.mean()
    S = pd.Series(e).groupby(np.asarray(days)).sum().values
    G = len(S)
    se = np.sqrt((S ** 2).sum() * G / max(G - 1, 1)) / n
    return float(x.mean() / se) if se > 0 else np.nan


def boot(x, days):
    df = pd.DataFrame({"x": np.asarray(x, float), "d": np.asarray(days)})
    g = df.groupby("d").x.agg(["sum", "count"])
    if len(g) < 5:
        return (np.nan, np.nan)
    s, c = g["sum"].values, g["count"].values
    idx = rng.integers(0, len(g), (2000, len(g)))
    return tuple(np.round(np.percentile(s[idx].sum(1) / c[idx].sum(1), [2.5, 97.5]), 1))


def stats(T, nd):
    if len(T) == 0:
        return dict(n=0)
    T = T.sort_values(["day", "time"])
    x = T.net.values
    win, loss = x[x > 0], x[x <= 0]
    cum = np.cumsum(x)
    bps = lambda col: float(1e4 * T[col].sum() / T.notional.sum())
    return dict(
        n=int(len(T)), days_traded=int(T.day.nunique()), trades_per_day=round(len(T) / nd, 2),
        gross_rs=round(T.gross0.mean(), 1), gross_bps=round(bps("gross0"), 2),
        net_rs=round(x.mean(), 1), net_bps=round(bps("net"), 2), net0_rs=round(T.net0.mean(), 1),
        charges_rs=round(T.charges.mean(), 1), notional=round(T.notional.mean(), 0),
        total_net=round(x.sum(), 0), win_pct=round(100 * (x > 0).mean(), 1),
        payoff=round(win.mean() / -loss.mean(), 2) if len(win) and len(loss) and loss.mean() < 0 else None,
        pf=round(win.sum() / -loss.sum(), 2) if loss.sum() < 0 else None,
        t=round(tstat(x, T.day.values), 2), ci=boot(x, T.day.values),
        maxdd=round(float((np.maximum.accumulate(np.r_[0, cum])[1:] - cum).max()), 0),
        breach500=round(100 * (x < -500).mean(), 1), net_b_rs=round(T.net_b.mean(), 1),
        r1_pct=round(100 * T.r1_0.mean(), 1), r2_pct=round(100 * T.r2_0.mean(), 1),
        bar_14bps="CLEARS" if bps("gross0") >= 14 else "BELOW")


def half_years(T):
    if len(T) == 0:
        return {}
    h = pd.to_datetime(T.day).dt.year.astype(str) + np.where(pd.to_datetime(T.day).dt.month <= 6, "H1", "H2")
    return T.groupby(h).net.agg(["count", "mean"]).round(1).apply(lambda r: f"{int(r['count'])}:{r['mean']}", axis=1).to_dict()


def placebo(Tp, period, cand):
    """Timing + selection placebo p-values on the stop-or-15:20 lookup (both sides combined)."""
    P = pd.read_parquet(C.D / f"placebo_{period}.parquet")
    P["k"] = list(zip(P.symbol, P.day, P.end, P.side))
    look = dict(zip(P.k, P.net))
    E = Tp.copy()
    E["end"] = pd.to_datetime(E.time).dt.strftime("%H:%M")
    E["v"] = [look.get((s, d, e, sd)) for s, d, e, sd in zip(E.symbol, E.day, E.end, E.side)]
    E = E[E.v.notna()]
    if len(E) < 10:
        return dict(n=len(E))
    actual = E.v.mean()
    lo, hi = TWIN.get(cand, ("09:30", "14:30"))
    by_sd = {k: g for k, g in P[(P.end >= lo) & (P.end <= hi)].groupby(["symbol", "day", "side"])}
    by_dt = {k: g.net.values for k, g in P.groupby(["day", "end", "side"])}
    tim = [by_sd.get((s, d, sd)) for s, d, sd in zip(E.symbol, E.day, E.side)]
    sel = [by_dt.get((d, e, sd)) for d, e, sd in zip(E.day, E.end, E.side)]
    tim_draw, sel_draw = [], []
    for _ in range(1000):
        tim_draw.append(np.mean([g.net.values[rng.integers(len(g))] for g in tim if g is not None and len(g)]))
        sel_draw.append(np.mean([a[rng.integers(len(a))] for a in sel if a is not None and len(a)]))
    tim_draw, sel_draw = np.array(tim_draw), np.array(sel_draw)
    return dict(n=int(len(E)), actual_stop1520=round(float(actual), 1),
                timing_p=round(float((tim_draw >= actual).mean()), 3), timing_pctile=round(100 * float((tim_draw < actual).mean()), 1),
                selection_p=round(float((sel_draw >= actual).mean()), 3), selection_pctile=round(100 * float((sel_draw < actual).mean()), 1))


def main(cand, periods):
    res = dict(cand=cand, code_hash=CODE, periods={}, ledger=[])
    for per in periods:
        T = pd.read_parquet(C.D / f"trades_{cand}_{per}.parquet")
        nd = ndays(per)
        res["periods"][per] = {}
        for vn, g in T.groupby("variant"):
            block = {}
            for side, gg in (("all", g), ("long", g[g.side > 0]), ("short", g[g.side < 0])):
                block[side] = stats(gg, nd)
            block["half_years"] = half_years(g)
            res["periods"][per][vn] = block
            res["ledger"].append(dict(round="R81", variant=vn, cand=cand, period=per, code_hash=CODE,
                                      **{f"{s}_{k}": v for s in ("all", "long", "short") for k, v in block[s].items()},
                                      half_years=json.dumps(block["half_years"])))
        if per in ("validate", "holdout"):
            pv = {}
            for side, gg in (("all", T[T.variant == cand]), ("long", T[(T.variant == cand) & (T.side > 0)]),
                             ("short", T[(T.variant == cand) & (T.side < 0)])):
                pv[side] = placebo(gg, per, cand)
            res["periods"][per]["placebo"] = pv
    # gates
    P = res["periods"]
    g = {}
    pos = lambda per, side="all": P.get(per, {}).get(cand, {}).get(side, {}).get("net_rs", -1) > 0
    trv = pd.concat([pd.read_parquet(C.D / f"trades_{cand}_{p}.parquet") for p in ("train", "validate") if p in P])
    hy = half_years(trv[trv.variant == cand])
    g["half_years_positive"] = f"{sum(float(v.split(':')[1]) > 0 for v in hy.values())}/{len(hy)}"
    g["half_years_ok"] = sum(float(v.split(':')[1]) > 0 for v in hy.values()) >= 7
    g["train_pos"], g["validate_pos"] = pos("train"), pos("validate")
    g["plateau"] = {n: P.get("validate", {}).get(n, {}).get("all", {}).get("net_rs") for n in NEIGH[cand]}
    g["plateau_ok"] = all((v or -1) > 0 for v in g["plateau"].values())
    pl = P.get("validate", {}).get("placebo", {}).get("all", {})
    g["placebo_validate_ok"] = pl.get("timing_p", 1) < 0.05 and pl.get("selection_p", 1) < 0.05
    if "holdout" in P:
        h = P["holdout"][cand]["all"]
        plh = P["holdout"].get("placebo", {}).get("all", {})
        g["holdout_pos"] = h.get("net_rs", -1) > 0
        g["holdout_t"] = h.get("t")
        g["placebo_holdout_ok"] = plh.get("timing_p", 1) < 0.05 and plh.get("selection_p", 1) < 0.05
        base = all([g["train_pos"], g["validate_pos"], g["holdout_pos"], g["half_years_ok"], g["plateau_ok"],
                    g["placebo_validate_ok"], g["placebo_holdout_ok"]])
        t = h.get("t") or 0
        g["verdict"] = ("CONFIRMED (|t|>=2.58, all gates)" if base and t >= 2.58 else
                        "RESULT, not family-wise confirmed (2<=t<2.58, all gates)" if base and t >= 2 else
                        "NOT A RESULT")
    res["gates"] = g
    json.dump(res, open(C.D / f"result_{cand}.json", "w"), indent=1, default=str)
    led = pd.DataFrame(res["ledger"])
    lp = C.D / "variant_ledger.csv"
    if lp.exists():
        old = pd.read_csv(lp)
        old = old[~((old.cand == cand) & old.period.isin(periods))]
        led = pd.concat([old, led], ignore_index=True)
    led.to_csv(lp, index=False)
    print(json.dumps({"gates": g, **{p: {"primary": P[p].get(cand), "placebo": P[p].get("placebo")} for p in P}},
                     indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2].split(","))
