"""R83 runner.
uv run python backtest/r83_fno/run.py tv            # TRAIN + VALIDATE for every B candidate/neighbour, all capitals/slips
uv run python backtest/r83_fno/run.py A             # sector ETF rotation: in-sample + backward/forward holdout
uv run python backtest/r83_fno/run.py holdout       # frozen B candidates on the sealed holdout (+ placebo)
"""

import json
import sys

import core as K
import numpy as np
import pandas as pd

import strategies as S

CAPS = [20_000, 100_000, 500_000, 1_000_000]
SLIPS = [0.0, 0.0005, 0.0015]
PY = {"M": 12, "W": 52, "Q": 4}
rng = np.random.default_rng(83)


def run_one(cand, lo, hi, capital, slip):
    freq = S.FREQ.get(cand, "M")
    if cand.startswith("A"):
        Op, Cl, *_ = K.panel()
        cols = ["IDX:" + x for x in S.SECTOR_A]
        m = K.simulate(
            S.REG[cand],
            "M",
            lo,
            hi,
            capital,
            slip,
            cost=K.etf_cost,
            fractional=True,
            price_frame=(Op[cols], Cl[cols]),
        )
        b = K.ew_benchmark("M", lo, hi, names_fn=lambda d: cols)
    else:
        m = K.simulate(S.REG[cand], freq, lo, hi, capital, slip)
        b = K.ew_benchmark(freq, lo, hi)
    m = m.merge(b, on="start", how="inner")
    return m, K.stats(m, PY[freq])


def b6(lo, hi, look=126, skip=21, n=10, slip=0.0005, lot_notional=700_000):
    """Market-neutral futures momentum, returns-based on long-leg notional; futures costs both legs, full roll monthly."""
    Op, Cl, *_ = K.panel()
    days = Cl.index
    ends = K.period_ends("M", lo, hi)
    rows = []
    c_rt = (
        K.fut_cost(lot_notional, True) + K.fut_cost(lot_notional, False)
    ) / lot_notional + 2 * slip
    for a, b in zip(ends[:-1], ends[1:], strict=False):
        el = K.eligible(a)
        s = S.mom_scores(a, el, look, skip)
        if len(s) < 2 * n:
            continue
        L, Sh = list(s.nlargest(n).index), list(s.nsmallest(n).index)
        ea, eb = days[days.get_loc(a) + 1], days[min(days.get_loc(b) + 1, len(days) - 1)]

        def leg(names, ea=ea, eb=eb):
            pa, pb = (
                Op.loc[ea, names],
                Op.loc[eb, names].fillna(Cl.loc[:eb, names].ffill().iloc[-1]),
            )
            return float((pb / pa - 1).replace([np.inf, -np.inf], np.nan).dropna().mean())

        gross = leg(L) - leg(Sh)
        rows.append(
            {
                "start": ea,
                "ret": gross - 2 * c_rt,
                "gross": gross,
                "bench": 0.0,
                "costs": 2 * c_rt,
                "eq_start": 1.0,
                "turnover": 2.0,
            }
        )
    m = pd.DataFrame(rows)
    return m, K.stats(m, 12)


def placebo(cand, lo, hi, draws=1000):
    """Selection placebo: random same-size equal-weight portfolios from the same eligible set each period,
    gross open-to-open excess vs the EW benchmark; compared with the candidate's gross excess computed the same way."""
    Op, Cl, *_ = K.panel()
    days = Cl.index
    freq = S.FREQ.get(cand, "M")
    ends = K.period_ends(freq, lo, hi)
    act, rand = [], []
    for a, b in zip(ends[:-1], ends[1:], strict=False):
        ea, eb = days[days.get_loc(a) + 1], days[min(days.get_loc(b) + 1, len(days) - 1)]
        el = K.eligible(a)
        r = (
            (Op.loc[eb, el].fillna(Cl.loc[:eb, el].ffill().iloc[-1]) / Op.loc[ea, el] - 1)
            .replace([np.inf, -np.inf], np.nan)
            .dropna()
        )
        w = S.REG[cand](a)
        names = [s for s in w if s in r.index]
        if not names:
            continue
        act.append(sum(w[s] * r[s] for s in names) / sum(w[s] for s in names) - r.mean())
        k = len(names)
        rv = r.values
        rand.append(
            np.array([rv[rng.choice(len(rv), k, replace=False)].mean() for _ in range(draws)])
            - r.mean()
        )
    act = np.mean(act)
    rnd = np.mean(np.vstack(rand), axis=0)
    return {
        "actual_gross_excess_per_period": round(float(act), 5),
        "p": round(float((rnd >= act).mean()), 3),
    }


def placebo_ls(lo, hi, look=126, skip=21, n=10, draws=1000):
    """B6 placebo: random disjoint long-n / short-n sets from the same eligible pool each month (gross)."""
    Op, Cl, *_ = K.panel()
    days = Cl.index
    ends = K.period_ends("M", lo, hi)
    act, rand = [], []
    for a, b in zip(ends[:-1], ends[1:], strict=False):
        el = K.eligible(a)
        s = S.mom_scores(a, el, look, skip)
        if len(s) < 2 * n:
            continue
        ea, eb = days[days.get_loc(a) + 1], days[min(days.get_loc(b) + 1, len(days) - 1)]
        r = (
            Op.loc[eb, list(s.index)].fillna(Cl.loc[:eb, list(s.index)].ffill().iloc[-1])
            / Op.loc[ea, list(s.index)]
            - 1
        ).replace([np.inf, -np.inf], np.nan)
        r = r.fillna(r.mean())
        act.append(r[s.nlargest(n).index].mean() - r[s.nsmallest(n).index].mean())
        rv = r.values
        dr = []
        for _ in range(draws):
            p = rng.permutation(len(rv))
            dr.append(rv[p[:n]].mean() - rv[p[n : 2 * n]].mean())
        rand.append(dr)
    a = np.mean(act)
    rnd = np.mean(np.array(rand), axis=0)
    return {"actual_gross_per_month": round(float(a), 5), "p": round(float((rnd >= a).mean()), 3)}


def tv():
    rows = []
    batch = sys.argv[2] if len(sys.argv) > 2 else "B"
    cands = [k for k in S.REG if k.startswith(batch)]
    for cand in cands:
        for per, (lo, hi) in (("train", K.TRAIN), ("validate", K.VALID)):
            for cap in CAPS:
                for slip in SLIPS:
                    if slip != 0.0005 and cap != 20_000:
                        continue
                    m, st = run_one(cand, lo, hi, cap, slip)
                    hy = K.half_years(m) if (cap == 20_000 and slip == 0.0005) else {}
                    rows.append(
                        dict(
                            cand=cand,
                            period=per,
                            capital=cap,
                            slip_bps=slip * 1e4,
                            **st,
                            half_years=json.dumps(hy),
                        )
                    )
            print(cand, per, "done", flush=True)
    for per, (lo, hi) in (("train", K.TRAIN), ("validate", K.VALID)) if batch == "B" else ():
        for nm, kw in (
            ("B6", {}),
            ("B6_3-1", {"look": 63}),
            ("B6_12-1", {"look": 252}),
            ("B6_n5", {"n": 5}),
        ):
            m, st = b6(lo, hi, **kw)
            rows.append(
                dict(
                    cand=nm,
                    period=per,
                    capital=None,
                    slip_bps=5.0,
                    **st,
                    half_years=json.dumps(K.half_years(m)),
                )
            )
    df = pd.DataFrame(rows)
    df.to_csv(K.D / f"tv_{batch}.csv", index=False)
    print(
        df[(df.capital.isin([20_000, None]) | df.capital.isna()) & (df.slip_bps == 5.0)][
            [
                "cand",
                "period",
                "capital",
                "cagr",
                "sharpe",
                "maxdd",
                "excess_ann",
                "t_excess",
                "abs_ann",
                "costs_pct_ann",
            ]
        ].to_string()
    )


def run_A():
    rows = []
    for cand in [k for k in S.REG if k.startswith("A")]:
        for per, (lo, hi) in (
            ("in_sample", K.A_INS),
            ("hold_back", K.A_HOLD_BACK),
            ("hold_fwd", K.A_HOLD_FWD),
        ):
            for cap in CAPS:
                for slip in SLIPS:
                    if slip != 0.0005 and cap != 20_000:
                        continue
                    m, st = run_one(cand, lo, hi, cap, slip)
                    rows.append(dict(cand=cand, period=per, capital=cap, slip_bps=slip * 1e4, **st))
                    if cand == "A" and cap == 20_000 and slip == 0.0005:
                        m.to_csv(K.D / f"A_{per}_monthly.csv", index=False)
        print(cand, "done", flush=True)
    df = pd.DataFrame(rows)
    # combined holdout (backward + forward) for the primary spec
    parts = [
        pd.read_csv(K.D / f"A_{p}_monthly.csv", parse_dates=["start"])
        for p in ("hold_back", "hold_fwd")
    ]
    comb = pd.concat(parts)
    st = K.stats(comb, 12)
    df = pd.concat(
        [
            df,
            pd.DataFrame(
                [dict(cand="A", period="hold_combined", capital=20_000, slip_bps=5.0, **st)]
            ),
        ]
    )
    df.to_csv(K.D / "A.csv", index=False)
    print(
        df[(df.capital == 20_000) & (df.slip_bps == 5.0)][
            [
                "cand",
                "period",
                "periods",
                "cagr",
                "sharpe",
                "maxdd",
                "excess_ann",
                "t_excess",
                "abs_ann",
                "bench_ann",
                "costs_pct_ann",
            ]
        ].to_string()
    )
    print(
        df[(df.cand == "A") & (df.slip_bps == 5.0)][
            ["period", "capital", "cagr", "sharpe", "excess_ann", "t_excess", "costs_pct_ann"]
        ].to_string()
    )


def holdout():
    fr = json.load(open(K.D / "frozen.json"))
    rows = []
    for cand in fr["to_holdout"]:
        if cand.startswith("B6"):
            kw = {"B6": {}, "B6_3-1": {"look": 63}, "B6_12-1": {"look": 252}, "B6_n5": {"n": 5}}[
                cand
            ]
            m, st = b6(*K.HOLD, **kw)
            pl = placebo_ls(*K.HOLD, **kw)
            hy = K.half_years(m)
            m.to_csv(K.D / f"hold_{cand}.csv", index=False)
            rows.append(
                dict(
                    cand=cand,
                    capital=None,
                    slip_bps=5.0,
                    **st,
                    placebo_p=pl["p"],
                    half_years=json.dumps(hy),
                )
            )
            print(cand, st, pl, flush=True)
            continue
        for cap in CAPS:
            for slip in SLIPS:
                if slip != 0.0005 and cap != 20_000:
                    continue
                m, st = run_one(cand, *K.HOLD, cap, slip)
                rows.append(dict(cand=cand, capital=cap, slip_bps=slip * 1e4, **st))
                if slip == 0.0005:
                    m.to_csv(K.D / f"hold_{cand}_{cap}.csv", index=False)
        pl = placebo(cand, *K.HOLD)
        rows[-1].update(placebo_p=pl["p"])
        print(cand, pl, flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(K.D / "holdout.csv", index=False)
    print(df.to_string())


if __name__ == "__main__":
    {"tv": tv, "A": run_A, "holdout": holdout}[sys.argv[1]]()
