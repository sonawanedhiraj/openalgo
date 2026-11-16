"""R82 scoring + pre-registered selection.
uv run python backtest/r82_sizing/score_r82.py tv            -> data/tv_summary.csv, data/frozen.json, ledger
uv run python backtest/r82_sizing/score_r82.py holdout       -> data/holdout_summary.csv, gates, ledger
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
D = HERE / "data"
sys.path.insert(0, str(HERE))
rng = np.random.default_rng(82)
SELECTABLE_EXEC = ["X0", "X1", "XL5", "XLi5", "XS"]
SELECTABLE_SIZE = ["S0L", "A500", "A750", "A1000", "APCT"]
SIMPLE = {
    "X0": 0,
    "X1": 1,
    "XS": 2,
    "XL5": 3,
    "XLi5": 4,
    "S0L": 0,
    "A500": 1,
    "A750": 2,
    "A1000": 3,
    "APCT": 4,
}


def tstat(x, days):
    x = np.asarray(x, float)
    if len(x) < 3:
        return np.nan
    e = x - x.mean()
    S = pd.Series(e).groupby(np.asarray(days)).sum().values
    G = len(S)
    se = np.sqrt((S**2).sum() * G / max(G - 1, 1)) / len(x)
    return float(x.mean() / se) if se > 0 else np.nan


def boot(x, days):
    df = pd.DataFrame({"x": np.asarray(x, float), "d": np.asarray(days)})
    g = df.groupby("d").x.agg(["sum", "count"])
    if len(g) < 5:
        return (np.nan, np.nan)
    s, c = g["sum"].values, g["count"].values
    idx = rng.integers(0, len(g), (2000, len(g)))
    return tuple(np.round(np.percentile(s[idx].sum(1) / c[idx].sum(1), [2.5, 97.5]), 1))


def stats(T, ndays):
    if len(T) == 0:
        return {"n": 0}
    T = T.sort_values(["day", "fill_ts"])
    x = T.net.values
    win, loss = x[x > 0], x[x <= 0]
    cum = np.cumsum(x)
    N = T.notional.sum()
    return {
        "n": int(len(T)),
        "per_day": round(len(T) / max(ndays, 1), 2),
        "notional": round(T.notional.mean()),
        "gross_noslip_bps": round(1e4 * T.gross_noslip.sum() / N, 2),
        "bar_bps": round(1e4 * (T.charges.sum() + T.slip_rs.sum()) / N, 2),
        "charges_bps": round(1e4 * T.charges.sum() / N, 2),
        "slip_bps": round(1e4 * T.slip_rs.sum() / N, 2),
        "net_rs": round(x.mean(), 1),
        "net_bps": round(1e4 * x.sum() / N, 2),
        "total": round(x.sum()),
        "win_pct": round(100 * (x > 0).mean(), 1),
        "payoff": round(win.mean() / -loss.mean(), 2)
        if len(win) and len(loss) and loss.mean() < 0
        else None,
        "t": round(tstat(x, T.day.values), 2),
        "ci": boot(x, T.day.values),
        "maxdd": round(float((np.maximum.accumulate(np.r_[0, cum])[1:] - cum).max())),
        "breach_budget_pct": round(100 * (x < -T.budget.values).mean(), 1),
        "breach500_pct": round(100 * (x < -500).mean(), 1),
    }


def half_years(T):
    if len(T) == 0:
        return {}
    d = pd.to_datetime(T.day)
    h = d.dt.year.astype(str) + np.where(d.dt.month <= 6, "H1", "H2")
    return {k: round(v, 1) for k, v in T.groupby(h.values).net.mean().items()}


def load(cand, period):
    T = pd.read_parquet(D / f"trades_{cand}_{period}_grid.parquet")
    S = pd.read_parquet(D / f"signals_{cand}_{period}_grid.parquet")
    return T, S


def ndays(cand, period):
    import run_r82 as R

    s = R.signals_for(cand, period)
    lo, hi = R.PERIODS[cand][period]
    return len(pd.bdate_range(lo, hi)) if cand == "BASE" else max(1, s.day.nunique())


def missed_analysis(T, S, cfg):
    """For limit/next-open configs: signals not filled vs what X0 (same sizing) made on the same signal."""
    rule, exe = cfg.split(".")
    if exe in ("X0", "XS"):
        return {}
    s = S[(S.cfg == cfg) & S.skip.isna()]
    x0 = T[T.cfg == f"{rule}.X0"]
    k0 = {
        (str(a), b, str(c)): n
        for a, b, c, n in zip(x0.day, x0.symbol, x0.signal_ts, x0.net, strict=False)
    }
    miss = s[~s.filled]
    mv = [
        k0.get((str(a), b, str(c)))
        for a, b, c in zip(miss.day, miss.symbol, miss.signal_ts, strict=False)
    ]
    mv = [v for v in mv if v is not None]
    filled = T[T.cfg == cfg]
    fv = [
        k0.get((str(a), b, str(c)))
        for a, b, c in zip(filled.day, filled.symbol, filled.signal_ts, strict=False)
    ]
    fv = [v for v in fv if v is not None]
    return {
        "fill_rate_pct": round(100 * s.filled.mean(), 1) if len(s) else None,
        "missed_n": int(len(miss)),
        "missed_matched_to_x0": len(mv),
        "missed_x0_net_rs": round(float(np.mean(mv)), 1) if mv else None,
        "missed_x0_win_pct": round(100 * float(np.mean(np.array(mv) > 0)), 1) if mv else None,
        "filled_x0_net_rs": round(float(np.mean(fv)), 1) if fv else None,
    }


def summarize(cand, period):
    T, S = load(cand, period)
    nd = ndays(cand, period)
    rows = []
    for cfg, g in T.groupby("cfg"):
        rule, exe = cfg.split(".")
        for side, gg in (("all", g), ("long", g[g.side > 0]), ("short", g[g.side < 0])):
            if side != "all" and cand == "E3S":
                continue
            r = {
                "cand": cand,
                "period": period,
                "cfg": cfg,
                "sizing": rule,
                "exec": exe,
                "side": side,
                **stats(gg, nd),
            }
            if side == "all":
                s = S[S.cfg == cfg]
                r["signals"] = int(len(s))
                r["skip_bp_pct"] = round(100 * (s.skip == "bp").mean(), 1) if len(s) else None
                r["skip_minviable_pct"] = (
                    round(100 * (s.skip == "min_viable").mean(), 1) if len(s) else None
                )
                r["skip_budget_pct"] = (
                    round(100 * (s.skip == "budget").mean(), 1) if len(s) else None
                )
                fu = g.used_ratio.dropna()
                r["used_ratio_median"] = round(float(fu.median()), 3) if len(fu) else None
                r["half_years"] = json.dumps(half_years(g))
                r.update(missed_analysis(T, S, cfg))
            rows.append(r)
    return pd.DataFrame(rows)


def ledger_append(df):
    lp = D / "variant_ledger_r82.csv"
    if lp.exists():
        old = pd.read_csv(lp)
        keys = set(zip(df.cand, df.period, strict=False))
        old = old[[(a, b) not in keys for a, b in zip(old.cand, old.period, strict=False)]]
        df = pd.concat([old, df], ignore_index=True)
    df.to_csv(lp, index=False)


def select(tv):
    a = tv[tv.side == "all"]
    frozen = {}
    for cand in ("BASE", "E3S"):
        t = a[(a.cand == cand) & (a.period == "train")].set_index("cfg")
        v = a[(a.cand == cand) & (a.period == "validate")].set_index("cfg")
        unreachable = []
        for B in ("A500", "A750", "A1000"):
            u = t.loc[f"{B}.X0", "used_ratio_median"] if f"{B}.X0" in t.index else None
            if u is None or u < 0.9:
                unreachable.append(B)
        base_train = t.loc["S0L.X0", "net_rs"]
        pool = []
        for cfg in v.index:
            rule, exe = cfg.split(".")
            if rule not in SELECTABLE_SIZE or exe not in SELECTABLE_EXEC or rule in unreachable:
                continue
            if cfg not in t.index or t.loc[cfg, "net_rs"] < base_train:
                continue
            pool.append((v.loc[cfg, "net_rs"], -SIMPLE[rule] - SIMPLE[exe], cfg))
        pool.sort(reverse=True)
        frozen[cand] = {
            "selected": pool[0][2] if pool else "S0L.X0",
            "selected_validate_net_rs": float(pool[0][0]) if pool else None,
            "unreachable_budgets": unreachable,
            "train_S0L_X0_net_rs": float(base_train),
            "pool_size": len(pool),
            "top5": [(c, float(n)) for n, _, c in pool[:5]],
        }
    return frozen


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "tv":
        parts = [summarize("BASE", p) for p in ("train", "validate")] + [
            summarize("E3S", p) for p in ("train", "validate", "is3")
        ]
        tv = pd.concat(parts, ignore_index=True)
        tv.to_csv(D / "tv_summary.csv", index=False)
        ledger_append(tv)
        fr = select(tv)
        json.dump(fr, open(D / "frozen.json", "w"), indent=1)
        print(json.dumps(fr, indent=1))
    else:
        h = pd.concat(
            [summarize("BASE", "holdout"), summarize("E3S", "holdout")], ignore_index=True
        )
        h.to_csv(D / "holdout_summary.csv", index=False)
        ledger_append(h)
        print(
            h[h.side == "all"][
                ["cand", "cfg", "n", "net_rs", "t", "ci", "gross_noslip_bps", "bar_bps"]
            ].to_string()
        )
