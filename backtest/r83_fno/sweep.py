"""R84: capital sweep over R83's FROZEN strategies — a re-costing, not a new search.
Signals are computed once per (candidate, date) and re-used at every capital level; only sizing, whole-share
rounding, idle cash, DP charges and statutory costs change with capital.
  uv run python backtest/r83_fno/sweep.py cnc <db-copy>   -> data/r84_sweep.csv (A, B1-B5, C1-C5)
  uv run python backtest/r83_fno/sweep.py b6  <db-copy>   -> data/r84_b6_capital.csv
"""

import sqlite3
import sys

import core as K
import numpy as np
import pandas as pd

import strategies as S

CAPS = [20_000, 50_000, 100_000, 200_000, 500_000, 1_000_000, 2_500_000]
SLIPS = [0.0005, 0.0]
PY = {"M": 12, "W": 52, "Q": 4}
ETF_OF = {
    "BANKNIFTY": "BANKBEES",
    "NIFTYIT": "ITBEES",
    "NIFTYPSUBANK": "PSUBNKBEES",
    "NIFTYPHARMA": "PHARMABEES",
    "NIFTYMETAL": "METALIETF",
    "NIFTYPVTBANK": "PVTBANIETF",
    "NIFTYFMCG": "FMCGIETF",
    "NIFTYAUTO": "AUTOBEES",
    "NIFTY HEALTHCARE": "HEALTHIETF",
}
PRIMARY = ["A", "B1", "B2", "B3", "B4", "B5", "C1", "C2", "C3", "C4", "C5"]
# R83 outcome per primary (what is NOT allowed to be resurrected): stage it failed at
R83_STAGE = {
    "A": "holdout",
    "B1": "holdout (>=1L)",
    "B2": "validate",
    "B3": "train",
    "B4": "train",
    "B5": "train",
    "C1": "holdout (>=1L)",
    "C2": "train",
    "C3": "train",
    "C4": "validate",
    "C5": "train",
}


def periods(cand):
    if cand == "A":
        return {"in_sample": K.A_INS, "hold_back": K.A_HOLD_BACK, "hold_fwd": K.A_HOLD_FWD}
    return {"train": K.TRAIN, "validate": K.VALID, "holdout": K.HOLD}


def etf_price_frame():
    """Index open/close scaled to the tradable ETF unit price (median ETF/index ratio over common dates);
    FINNIFTY and OIL&GAS (index proxies, no ETF in the spec) scaled to a ~Rs100 unit."""
    Op, Cl, *_ = K.panel()
    etf = pd.read_parquet(K.D / "etf_daily.parquet")
    etf["date"] = pd.to_datetime(etf.date)
    cols = ["IDX:" + x for x in S.SECTOR_A]
    O2, C2 = Op[cols].copy(), Cl[cols].copy()
    scale = {}
    for x in S.SECTOR_A:
        c = "IDX:" + x
        e = ETF_OF.get(x)
        if e:
            se = etf[etf.symbol == e].set_index("date").close
            j = pd.concat([se, Cl[c]], axis=1, join="inner").dropna()
            k = float((j.iloc[:, 0] / j.iloc[:, 1]).median())
        else:
            k = 100.0 / float(Cl[c].dropna().iloc[-1])
        scale[x] = k
        O2[c], C2[c] = Op[c] * k, Cl[c] * k
    return (O2, C2), scale


def memo(fn):
    cache = {}

    def f(day):
        if day not in cache:
            cache[day] = fn(day)
        return cache[day]

    return f


def cnc():
    pf, scale = etf_price_frame()
    rows, bench_cache = [], {}
    for cand in PRIMARY:
        freq = S.FREQ.get(cand, "M")
        sig = memo(S.REG[cand])
        for per, (lo, hi) in periods(cand).items():
            bk = (cand == "A", freq, per)
            if bk not in bench_cache:
                if cand == "A":
                    cols = ["IDX:" + x for x in S.SECTOR_A]
                    bench_cache[bk] = K.ew_benchmark(
                        "M", lo, hi, names_fn=lambda d, cols=cols: cols
                    )
                else:
                    bench_cache[bk] = K.ew_benchmark(freq, lo, hi)
            b = bench_cache[bk]
            for cap in CAPS:
                for slip in SLIPS:
                    if cand == "A":
                        m = K.simulate(
                            sig,
                            "M",
                            lo,
                            hi,
                            cap,
                            slip,
                            cost=K.etf_cost,
                            fractional=False,
                            price_frame=pf,
                        )
                    else:
                        m = K.simulate(sig, freq, lo, hi, cap, slip)
                    m = m.merge(b, on="start", how="inner")
                    st = K.stats(m, PY[freq])
                    rows.append(
                        {
                            "cand": cand,
                            "period": per,
                            "capital": cap,
                            "slip_bps": slip * 1e4,
                            **st,
                            "dp_pct_ann": round(
                                100 * float((m.dp / m.eq_start).mean()) * PY[freq], 2
                            ),
                            "idle_cash_pct": round(100 * float(m.cash_frac.mean()), 1),
                            "names_held_pct": round(
                                100
                                * float((m.n_held_target / m.n_target.replace(0, np.nan)).mean()),
                                1,
                            ),
                            "r83_failed_at": R83_STAGE[cand],
                        }
                    )
            print(cand, per, "done", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(K.D / "r84_sweep.csv", index=False)
    print("ETF unit scale:", {k: round(v, 5) for k, v in scale.items()})


def b6(dbcopy):
    """Margin needed to hold B6 (1 lot each of 10 long + 10 short stock futures) at each holdout month, using the
    CURRENT master-contract lot sizes (historical lot sizes not on disk) and a 20% margin rate (optimistic;
    stock-future SPAN+exposure is typically 18-35%) + 10% MTM buffer."""
    sq = sqlite3.connect(f"file:{dbcopy}?mode=ro", uri=True)
    lots = pd.read_sql(
        "select symbol, lotsize, expiry from symtoken where exchange='NFO' and instrumenttype='FUT'",
        sq,
    )
    lots["exp"] = pd.to_datetime(lots.expiry, format="%d-%b-%y", errors="coerce")
    lots = (
        lots.sort_values("exp")
        .groupby(lots.symbol.str.replace(r"\d{2}[A-Z]{3}\d{2}FUT$", "", regex=True))
        .lotsize.first()
    )
    Op, Cl, *_ = K.panel()
    ren = S.R81C.REN
    out = []
    for per, (lo, hi) in (("train", K.TRAIN), ("validate", K.VALID), ("holdout", K.HOLD)):
        for a in K.period_ends("M", lo, hi):
            el = K.eligible(a)
            s = S.mom_scores(a, el, 126, 21)
            if len(s) < 20:
                continue
            names = list(s.nlargest(10).index) + list(s.nsmallest(10).index)
            px = Cl.loc[a, names]
            ls = pd.Series({n: lots.get(n, lots.get(ren.get(n, ""), np.nan)) for n in names})
            notional = (px * ls).dropna()
            out.append(
                {
                    "period": per,
                    "date": a.date(),
                    "n_priced": len(notional),
                    "gross_notional": float(notional.sum()),
                    "median_lot_notional": float(notional.median()),
                    "margin_needed": float(1.1 * 0.20 * notional.sum() * 20 / len(notional))
                    if len(notional)
                    else np.nan,
                }
            )
    df = pd.DataFrame(out)
    df.to_csv(K.D / "r84_b6_capital.csv", index=False)
    print(df.groupby("period")[["median_lot_notional", "margin_needed"]].median().round(0))
    print(
        "months viable at Rs25L:",
        round(100 * float((df.margin_needed <= 2_500_000).mean()), 1),
        "%",
    )


if __name__ == "__main__":
    {"cnc": lambda: cnc(), "b6": lambda: b6(sys.argv[2])}[sys.argv[1]]()
