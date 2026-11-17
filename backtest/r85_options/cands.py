"""R85 candidates O1..O5 + neighbours + placebos, per period. See PREREGISTRATION.md.
  uv run python backtest/r85_options/cands.py <cand[,cand..]|all> <train|validate|holdout> [scen=central] [placebo_draws=0]
-> data/trades_<cand>_<period>_<scen>.parquet, data/daily_<cand>_<period>_<scen>.parquet
"""

import math
import sys
from functools import lru_cache

import engine as E
import numpy as np
import pandas as pd
import sim as M

PERIODS = {
    "train": ("2019-01-01", "2022-12-31"),
    "validate": ("2023-01-01", "2024-06-30"),
    "holdout": ("2024-07-01", "2026-11-13"),
}
rng = np.random.default_rng(85)

SPECS = {
    # id: (kind, params)
    "O1": ("strangle", {"z": 1.0, "pt": 0.5, "sl": 2.0, "n": 20}),
    "O1_z075": ("strangle", {"z": 0.75, "pt": 0.5, "sl": 2.0, "n": 20}),
    "O1_z125": ("strangle", {"z": 1.25, "pt": 0.5, "sl": 2.0, "n": 20}),
    "O1_pt40": ("strangle", {"z": 1.0, "pt": 0.4, "sl": 2.0, "n": 20}),
    "O1_pt60": ("strangle", {"z": 1.0, "pt": 0.6, "sl": 2.0, "n": 20}),
    "O1_sl15": ("strangle", {"z": 1.0, "pt": 0.5, "sl": 1.5, "n": 20}),
    "O1_sl3": ("strangle", {"z": 1.0, "pt": 0.5, "sl": 3.0, "n": 20}),
    "O1_n10": ("strangle", {"z": 1.0, "pt": 0.5, "sl": 2.0, "n": 10}),
    "O1_n40": ("strangle", {"z": 1.0, "pt": 0.5, "sl": 2.0, "n": 40}),
    "O2": ("condor", {"z": 1.0, "zw": 1.75, "pt": 0.5, "n": 20}),
    "O2_z075": ("condor", {"z": 0.75, "zw": 1.75, "pt": 0.5, "n": 20}),
    "O2_z125": ("condor", {"z": 1.25, "zw": 1.75, "pt": 0.5, "n": 20}),
    "O2_w15": ("condor", {"z": 1.0, "zw": 1.5, "pt": 0.5, "n": 20}),
    "O2_w20": ("condor", {"z": 1.0, "zw": 2.0, "pt": 0.5, "n": 20}),
    "O2_pt40": ("condor", {"z": 1.0, "zw": 1.75, "pt": 0.4, "n": 20}),
    "O2_pt60": ("condor", {"z": 1.0, "zw": 1.75, "pt": 0.6, "n": 20}),
    "O3": ("dir", {"z": 1.0, "zw": 1.75, "pt": 0.5, "k": 8}),
    "O3_z075": ("dir", {"z": 0.75, "zw": 1.75, "pt": 0.5, "k": 8}),
    "O3_z125": ("dir", {"z": 1.25, "zw": 1.75, "pt": 0.5, "k": 8}),
    "O3_k5": ("dir", {"z": 1.0, "zw": 1.75, "pt": 0.5, "k": 5}),
    "O3_k12": ("dir", {"z": 1.0, "zw": 1.75, "pt": 0.5, "k": 12}),
    "O4": ("expweek", {"z": 1.0, "pt": 0.5, "sl": 2.0, "n": 20}),
    "O4_z075": ("expweek", {"z": 0.75, "pt": 0.5, "sl": 2.0, "n": 20}),
    "O4_z125": ("expweek", {"z": 1.25, "pt": 0.5, "sl": 2.0, "n": 20}),
    "O5": ("earn", {"z": 1.0, "wing": None}),
    "O5_z075": ("earn", {"z": 0.75, "wing": None}),
    "O5_z125": ("earn", {"z": 1.25, "wing": None}),
    "O5_ic": ("earn", {"z": 1.0, "wing": 1.75}),
}


@lru_cache(maxsize=1)
def liquidity():
    """(date x symbol) prior-60-session median of front-expiry option contracts (all strikes)."""
    q = """with x as (select symbol, date, expiry, contracts, min(expiry) over (partition by symbol, date) fe
           from fo where kind='OPT') select symbol, date, sum(contracts) c from x where expiry = fe group by 1, 2"""
    d = E.con().execute(q).df()
    d["date"] = pd.to_datetime(d.date)
    w = d.pivot(index="date", columns="symbol", values="c").sort_index()
    return w.rolling(60, min_periods=40).median().shift(1)


def liquid_names(day, n):
    L = liquidity()
    if day not in L.index:
        return []
    row = L.loc[day].dropna()
    return list(row.nlargest(n).index)


@lru_cache(maxsize=1)
def monthly_expiries():
    f = E.futures()
    f = f[f.symbol == "RELIANCE"]
    return sorted(set(f.expiry))


def cycles(lo, hi):
    ex = monthly_expiries()
    cal = E.calendar()
    out = []
    for a, b in zip(ex[:-1], ex[1:], strict=False):
        nxt = cal[cal > a]
        if not len(nxt):
            continue
        start = nxt[0]
        if not (pd.Timestamp(lo) <= start <= pd.Timestamp(hi)):
            continue
        out.append((start, b, E.day_offset(b, E.EXIT_BEFORE_EXPIRY)))
    return out


def sigma(sym, day):
    v = E.realised_vol()
    if sym not in v.columns or day not in v.index:
        return np.nan
    return float(v.loc[day, sym])


@lru_cache(maxsize=1)
def mom61():
    E.realised_vol()  # puts backtest/r83_fno on sys.path
    import core as K

    _, Cl, *_ = K.panel()
    return Cl.shift(21) / Cl.shift(126) - 1


def ctx(sym, day):
    sl = E.spot_and_lot()
    if (sym, day) not in sl.index:
        return None
    S, lot = sl.loc[(sym, day)]
    sg = sigma(sym, day)
    if not (S > 0 and lot > 0 and sg > 0):
        return None
    return float(S), int(lot), sg


def open_structure(chains, kind, p, sym, exp, entry, deadline, scen, side=None):
    c = ctx(sym, entry)
    if c is None:
        return None
    S, lot, sg = c
    T = max(E.trading_days_between(entry, exp), 1) / 252
    ch = chains.get(sym, exp, entry, entry)
    if ch.empty:
        return None
    if kind in ("strangle", "expweek", "earn"):
        legs = M.strangle_legs(ch, S, sg, T, p["z"])
        if legs is None:
            return None
        if kind == "earn" and p.get("wing"):
            wp = M.wing(ch, S, sg, T, p["wing"], "PE", legs[0][1])
            wc = M.wing(ch, S, sg, T, p["wing"], "CE", legs[1][1])
            if wp is None or wc is None:
                return None
            legs = legs + [wp, wc]
            w = max(legs[0][1] - wp[1], wc[1] - legs[1][1])
            mf = lambda s, w=w: E.margin_spread(w, lot)  # noqa: E731
        else:
            Kp, Kc = legs[0][1], legs[1][1]
            mf = lambda s, Kp=Kp, Kc=Kc: E.margin_strangle(s, Kp, Kc, lot)  # noqa: E731
        pt = None if kind == "earn" else p["pt"]
        sl = None if kind == "earn" else p["sl"]
        return M.run_trade(
            chains,
            sym,
            exp,
            entry,
            deadline,
            legs,
            lot,
            scen,
            pt=pt,
            sl=sl,
            margin_fn=mf,
            ramp=(kind == "expweek"),
        )
    if kind == "condor":
        legs = M.strangle_legs(ch, S, sg, T, p["z"])
        if legs is None:
            return None
        wp = M.wing(ch, S, sg, T, p["zw"], "PE", legs[0][1])
        wc = M.wing(ch, S, sg, T, p["zw"], "CE", legs[1][1])
        if wp is None or wc is None:
            return None
        w = max(legs[0][1] - wp[1], wc[1] - legs[1][1])
        return M.run_trade(
            chains,
            sym,
            exp,
            entry,
            deadline,
            legs + [wp, wc],
            lot,
            scen,
            pt=p["pt"],
            sl=None,
            margin_fn=lambda s: E.margin_spread(w, lot),
        )
    if kind == "dir":
        opt = "PE" if side > 0 else "CE"
        legs = M.strangle_legs(ch, S, sg, T, p["z"])
        if legs is None:
            return None
        short = legs[0] if opt == "PE" else legs[1]
        wg = M.wing(ch, S, sg, T, p["zw"], opt, short[1])
        if wg is None:
            return None
        w = abs(short[1] - wg[1])
        return M.run_trade(
            chains,
            sym,
            exp,
            entry,
            deadline,
            [short, wg],
            lot,
            scen,
            pt=p["pt"],
            sl=None,
            margin_fn=lambda s: E.margin_spread(w, lot),
        )
    return None


def results_events(lo, hi):
    r = pd.read_parquet(E.D / "results_dates.parquet")
    r = r[(r.period == "Quarterly") & r.ts.notna()]
    r = r[(r.ts >= pd.Timestamp(lo)) & (r.ts <= pd.Timestamp(hi) + pd.Timedelta(days=1))]
    cal = E.calendar()
    out = []
    for sym, ts in (
        r.groupby("symbol").ts.min().items()
        if False
        else r[["symbol", "ts"]].drop_duplicates().itertuples(index=False)
    ):
        d = pd.Timestamp(ts.date())
        if (
            ts.time() < pd.Timestamp("15:30").time()
        ):  # public during the session -> reaction day = d
            pre = cal[cal < d]
            post = cal[cal >= d]
        else:  # after the close -> reaction next session
            pre = cal[cal <= d]
            post = cal[cal > d]
        if len(pre) and len(post):
            out.append((sym, pre[-1], post[0]))
    return sorted(set(out), key=lambda x: x[1])


def run_cand(cand, period, scen, entry_override=None, names_override=None):
    kind, p = SPECS[cand]
    lo, hi = PERIODS[period]
    chains = M.Chains()
    trades = []
    if kind in ("strangle", "condor", "dir", "expweek"):
        for start, exp, deadline in cycles(lo, hi):
            entry = start
            if kind == "expweek":
                entry, deadline = E.day_offset(exp, 5), E.day_offset(exp, 1)
            if entry_override is not None:
                entry, deadline = entry_override(start, exp, entry, deadline)
            if kind == "dir":
                names = names_override(entry) if names_override else None
                pool = liquid_names(entry, 40)
                mm = mom61()
                if entry not in mm.index:
                    continue
                m = mm.loc[entry].reindex(pool).dropna()
                if names is None:
                    longs, shorts = list(m.nlargest(p["k"]).index), list(m.nsmallest(p["k"]).index)
                else:
                    longs, shorts = names
                todo = [(s, +1) for s in longs] + [(s, -1) for s in shorts]
            else:
                todo = [
                    (s, None)
                    for s in (
                        names_override(entry) if names_override else liquid_names(entry, p["n"])
                    )
                ]
            for sym, side in todo:
                t = open_structure(chains, kind, p, sym, exp, entry, deadline, scen, side)
                if t is not None:
                    t["side"] = side
                    trades.append(t)
            chains.c.clear()
    else:  # earnings
        for sym, pre, post in results_events(lo, hi):
            if names_override is None and sym not in liquid_names(pre, 40):
                continue
            ex = [x for x in monthly_expiries() if E.day_offset(x, E.EXIT_BEFORE_EXPIRY) >= post]
            if not ex:
                continue
            exp = ex[0]
            if entry_override is not None:
                pre, post = entry_override(sym, pre, post)
            t = open_structure(chains, kind, p, sym, exp, pre, post, scen)
            if t is not None:
                trades.append(t)
            if len(chains.c) > 200:
                chains.c.clear()
    return trades


def summarize(trades, period):
    if not trades:
        return {"n": 0}, pd.DataFrame(), pd.Series(dtype=float)
    T = pd.DataFrame([{k: v for k, v in t.items() if k != "path"} for t in trades])
    daily = pd.concat([t["path"] for t in trades]).groupby("date").mtm.sum()
    # entry/exit costs land on entry/exit day
    for t in trades:
        daily.loc[t["entry"]] = daily.get(t["entry"], 0.0) - 0.5 * t["costs"]
        daily.loc[t["exit"]] = daily.get(t["exit"], 0.0) - 0.5 * t["costs"]
    daily = daily.sort_index()
    cal = E.calendar()
    open_m = pd.Series(0.0, index=cal[(cal >= T.entry.min()) & (cal <= T.exit.max())])
    for r in T.itertuples():
        open_m[(open_m.index >= r.entry) & (open_m.index <= r.exit)] += r.max_margin
    cap = float(open_m.max())
    x = T.net.values
    g = T.groupby("entry").net.sum()
    yrs = max(
        (pd.Timestamp(PERIODS[period][1]) - pd.Timestamp(PERIODS[period][0])).days / 365.25, 0.25
    )
    cum = daily.cumsum()
    mon = daily.groupby(daily.index.to_period("M")).sum()
    tcl = (
        float(
            np.mean(x)
            / (
                np.std(g.values - np.mean(x) * T.groupby("entry").size().values, ddof=1)
                * math.sqrt(len(g))
                / len(x)
            )
        )
        if len(g) > 2
        else np.nan
    )
    st = {
        "n": int(len(T)),
        "cycles": int(T.entry.nunique()),
        "net_per_trade": round(float(x.mean()), 1),
        "t_cluster": round(tcl, 2),
        "win_pct": round(100 * float((x > 0).mean()), 1),
        "credit_per_trade": round(float(T.credit.mean()), 1),
        "costs_per_trade": round(float(T.costs.mean()), 1),
        "gross_per_trade": round(float(T.gross.mean()), 1),
        "total_net": round(float(x.sum())),
        "capital_peak_margin": round(cap),
        "median_margin_per_trade": round(float(T.max_margin.median())),
        "annual_net": round(float(x.sum()) / yrs),
        "ret_on_capital_pct": round(100 * float(x.sum()) / yrs / cap, 2) if cap else None,
        "sharpe_daily": round(float(daily.mean() / daily.std() * math.sqrt(252)), 2)
        if daily.std() > 0
        else None,
        "maxdd_pct_cap": round(100 * float((cum - cum.cummax()).min()) / cap, 1) if cap else None,
        "worst_day": round(float(daily.min())),
        "worst_day_date": str(daily.idxmin().date()),
        "worst_month": round(float(mon.min())),
        "worst_month_label": str(mon.idxmin()),
        "loss_p1": round(float(np.percentile(x, 1))),
        "loss_p5": round(float(np.percentile(x, 5))),
        "worst_trade": round(float(x.min())),
        "best_trade": round(float(x.max())),
        "reasons": T.reason.value_counts().to_dict(),
    }
    return st, T, daily


def placebo(cand, period, actual, draws):
    kind, p = SPECS[cand]
    vals = []
    for _ in range(draws):
        if kind in ("strangle", "condor"):

            def eo(start, exp, entry, deadline):
                cal = E.calendar()
                pool = cal[(cal >= start) & (cal <= E.day_offset(exp, 10))]
                return (pool[rng.integers(len(pool))] if len(pool) else entry), deadline

            tr = run_cand(cand, period, "central", entry_override=eo)
        elif kind == "expweek":

            def eo(start, exp, entry, deadline):
                cal = E.calendar()
                pool = cal[(cal >= start) & (cal <= E.day_offset(exp, 10))]
                e = pool[rng.integers(len(pool))] if len(pool) else entry
                return e, cal[min(cal.get_loc(e) + 4, len(cal) - 1)]

            tr = run_cand(cand, period, "central", entry_override=eo)
        elif kind == "dir":

            def no(entry):
                pool = liquid_names(entry, 40)
                pick = (
                    list(rng.choice(pool, 2 * p["k"], replace=False))
                    if len(pool) >= 2 * p["k"]
                    else pool
                )
                return pick[: p["k"]], pick[p["k"] :]

            tr = run_cand(cand, period, "central", names_override=no)
        else:

            def eo(sym, pre, post):
                cal = E.calendar()
                i = rng.integers(60, len(cal) - 2)
                return cal[i], cal[i + 1]

            tr = run_cand(cand, period, "central", entry_override=eo)
        if tr:
            vals.append(np.mean([t["net"] for t in tr]))
    vals = np.array(vals)
    return {
        "vals": vals.tolist(),
        "draws": int(len(vals)),
        "p": round(float((vals >= actual).mean()), 3) if len(vals) else None,
        "placebo_mean": round(float(vals.mean()), 1) if len(vals) else None,
    }


if __name__ == "__main__":
    import json

    cands = list(SPECS) if sys.argv[1] == "all" else sys.argv[1].split(",")
    period = sys.argv[2]
    scen = sys.argv[3] if len(sys.argv) > 3 else "central"
    draws = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    tag = sys.argv[5] if len(sys.argv) > 5 else ""
    rows = []
    for c in cands:
        tr = run_cand(c, period, scen)
        st, T, daily = summarize(tr, period)
        if len(T):
            T.to_parquet(E.D / f"trades_{c}_{period}_{scen}{tag}.parquet", index=False)
            daily.rename("pnl").to_frame().to_parquet(
                E.D / f"daily_{c}_{period}_{scen}{tag}.parquet"
            )
            hy = (
                T.assign(
                    h=T.entry.dt.year.astype(str) + np.where(T.entry.dt.month <= 6, "H1", "H2")
                )
                .groupby("h")
                .net.mean()
                .round(1)
                .to_dict()
            )
            st["half_years"] = json.dumps(hy)
        if draws and st.get("n"):
            st.update(
                {
                    f"placebo_{k}": v
                    for k, v in placebo(c, period, st["net_per_trade"], draws).items()
                    if k != "vals"
                }
            )
        rows.append({"cand": c, "period": period, "scen": scen, **st})
        print(
            c,
            period,
            scen,
            {
                k: st.get(k)
                for k in (
                    "n",
                    "net_per_trade",
                    "t_cluster",
                    "capital_peak_margin",
                    "ret_on_capital_pct",
                )
            },
            flush=True,
        )
    pd.DataFrame(rows).to_csv(
        E.D / f"summary_{period}_{scen}{tag}_{sys.argv[1].replace(',', '-')[:40]}.csv", index=False
    )
