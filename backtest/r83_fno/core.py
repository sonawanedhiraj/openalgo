"""R83 core: point-in-time daily panel, cost models (Kite-verified), portfolio simulator. See PREREGISTRATION.md."""

import datetime as dt
import json
from functools import lru_cache
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
D = HERE / "data"
R81 = ROOT / "backtest/r81_entry/data"
R82 = ROOT / "backtest/r82_sizing/data"

TRAIN = (dt.date(2019, 1, 1), dt.date(2022, 12, 31))
VALID = (dt.date(2023, 1, 1), dt.date(2024, 6, 30))
HOLD = (dt.date(2024, 7, 1), dt.date(2026, 11, 13))
A_INS = (dt.date(2022, 8, 1), dt.date(2026, 6, 30))
A_HOLD_BACK = (dt.date(2019, 1, 1), dt.date(2022, 7, 31))
A_HOLD_FWD = (dt.date(2026, 7, 1), dt.date(2026, 11, 13))
DP = 15.34


# ------------------------------------------------------------------ costs (per order, rupees)
def cnc_cost(value, buy):
    exch, sebi = 0.0000307 * value, 0.000001 * value
    gst = 0.18 * (exch + sebi)
    return 0.001 * value + (0.00015 * value if buy else 0.0) + exch + sebi + gst


def etf_cost(value, buy):
    exch, sebi = 0.0000307 * value, 0.000001 * value
    gst = 0.18 * (exch + sebi)
    return (0.00015 * value if buy else 0.00001 * value) + exch + sebi + gst


def fut_cost(value, buy):
    brk = min(20.0, 0.0003 * value)
    exch, sebi = 0.0000183 * value, 0.000001 * value
    gst = 0.18 * (brk + exch + sebi)
    return brk + exch + sebi + gst + (0.00002 * value if buy else 0.0005 * value)


# ------------------------------------------------------------------ data
@lru_cache(maxsize=1)
def panel():
    """Wide frames (date x symbol): open, close, value (close*volume). Union of R81 and R82-forward daily bars."""
    parts = []
    for p in (R81 / "bars_r81.duckdb", R82 / "bars_fwd.duckdb"):
        c = duckdb.connect(str(p), read_only=True)
        df = c.execute("select symbol, bar_date, open, close, volume from d1").df()
        c.close()
        df["src"] = 0 if "r81" in p.name else 1
        parts.append(df)
    df = pd.concat(parts).sort_values("src").drop_duplicates(["symbol", "bar_date"], keep="last")
    df["bar_date"] = pd.to_datetime(df.bar_date)
    Op = df.pivot(index="bar_date", columns="symbol", values="open").sort_index()
    Cl = df.pivot(index="bar_date", columns="symbol", values="close").sort_index()
    V = (
        (df.assign(v=df.close * df.volume))
        .pivot(index="bar_date", columns="symbol", values="v")
        .sort_index()
    )
    nifty_days = Cl["IDX:NIFTY"].dropna().index
    Op, Cl, V = Op.reindex(nifty_days), Cl.reindex(nifty_days), V.reindex(nifty_days)
    # corporate-action artifacts: |ret| > 35% on one day -> treat as adjustment (return 0)
    r = Cl / Cl.shift() - 1
    art = (r.abs() > 0.35) & Cl.columns.to_series().str[:4].ne("IDX:").values
    return Op, Cl, V, int(art.values.sum()), art


@lru_cache(maxsize=1)
def daily_returns():
    Op, Cl, V, _, art = panel()
    r = Cl / Cl.ffill().shift() - 1
    return r.mask(art, 0.0)


@lru_cache(maxsize=1)
def universe():
    u = {k: set(v["stocks"]) for k, v in json.load(open(R81 / "universe_pit.json")).items()}
    u.update({k: set(v["stocks"]) for k, v in json.load(open(R82 / "universe_fwd.json")).items()})
    return u


def eligible(day):
    return list(_eligible(day))


@lru_cache(maxsize=4096)
def _eligible(day):
    """PIT F&O names eligible at signal date `day` (Timestamp)."""
    Op, Cl, V, _, _ = panel()
    mk = (day + pd.Timedelta(days=5)).strftime(
        "%Y-%m"
    )  # trading month: list of M-1 month-end (prereg)
    names = [s for s in universe().get(mk, set()) if s in Cl.columns]
    loc = Cl.index.get_loc(day)
    if loc < 252:
        return []
    hist = Cl.iloc[: loc + 1][names]
    n_ok = hist.notna().sum() >= 252
    px = hist.iloc[-1]
    adv = V.iloc[max(0, loc - 19) : loc + 1][names].median()
    ok = n_ok & (px >= 50) & (adv >= 1e8)
    return tuple(sorted(ok.index[ok.fillna(False)]))


def period_ends(freq, lo, hi):
    """Signal dates: last trading day of each month/week inside [lo, hi] whose next trading day exists."""
    Op, Cl, *_ = panel()
    idx = Cl.index[
        (Cl.index >= pd.Timestamp(lo) - pd.Timedelta(days=40)) & (Cl.index <= pd.Timestamp(hi))
    ]
    s = pd.Series(idx, index=idx)
    key = {"M": idx.to_period("M"), "W": idx.to_period("W-FRI"), "Q": idx.to_period("Q")}[freq]
    ends = s.groupby(key).max().values
    return [pd.Timestamp(x) for x in ends]


# ------------------------------------------------------------------ simulator (integer shares, CNC/ETF)
def simulate(
    signal_fn, freq, lo, hi, capital, slip=0.0005, cost=cnc_cost, fractional=False, price_frame=None
):
    """Rebalance at the next open after each signal date. Returns per-period frame:
    start, end, ret (net), gross_ret, costs, n_names, turnover."""
    Op, Cl, *_ = panel()
    if price_frame is not None:
        Op, Cl = price_frame
    ends = list(period_ends(freq, lo, hi))
    days = Cl.index
    cash, sh = float(capital), {}
    rows = []
    prev_exec, prev_eq, _gross_acc = None, None, 0.0
    for d in ends:
        loc = days.get_loc(d)
        if loc + 1 >= len(days):
            break
        ex = days[loc + 1]
        if ex > pd.Timestamp(hi) + pd.Timedelta(days=7):
            break
        op = Op.loc[ex]
        cl_prev = Cl.iloc[: loc + 1].ffill().iloc[-1]
        px = op.fillna(cl_prev)
        eq = cash + sum(
            q * px.get(s, np.nan) if not np.isnan(px.get(s, np.nan)) else q * cl_prev.get(s, 0)
            for s, q in sh.items()
        )
        if prev_exec is not None and prev_eq:
            rows[-1].update(end=ex, ret=eq / prev_eq - 1)
        w = signal_fn(d)
        if w is None:
            w = {}
        costs = 0.0
        dp = 0.0
        tgt = {}
        for s, wt in w.items():
            p = px.get(s, np.nan)
            if np.isnan(p) or p <= 0:
                continue
            q = wt * eq / (p * (1 + slip))
            tgt[s] = q if fractional else int(q)
        turnover = 0.0
        # sells
        for s in list(sh):
            q0, q1 = sh[s], tgt.get(s, 0)
            if q1 < q0:
                p = px.get(s, np.nan)
                if np.isnan(p):
                    p = cl_prev.get(s, 0)
                v = (q0 - q1) * p * (1 - slip)
                c = cost(v, False) + DP
                dp += DP
                cash += v - c
                costs += c + (q0 - q1) * p * slip
                turnover += v
                sh[s] = q1
                if sh[s] <= 0:
                    del sh[s]
        # buys
        for s, q1 in tgt.items():
            q0 = sh.get(s, 0)
            if q1 > q0:
                p = px[s] * (1 + slip)
                v = (q1 - q0) * p
                c = cost(v, True)
                if v + c > cash:
                    q = int((cash / (1 + 0.0013)) / p) if not fractional else cash / (1.0013 * p)
                    if q <= 0:
                        continue
                    v = q * p
                    c = cost(v, True)
                    q1 = q0 + q
                cash -= v + c
                costs += c + (q1 - q0) * px[s] * slip
                turnover += v
                sh[s] = q1
        rows.append(
            {
                "start": ex,
                "signal": d,
                "eq_start": eq,
                "costs": costs,
                "turnover": turnover,
                "n_names": len(sh),
                "names": ",".join(sorted(sh)),
                "n_target": len(w),
                "n_held_target": sum(1 for x in w if sh.get(x, 0) > 0),
                "cash_frac": cash / eq if eq else np.nan,
                "dp": dp,
            }
        )
        prev_exec, prev_eq = ex, eq
    if rows and "ret" not in rows[-1]:
        rows.pop()
    return pd.DataFrame(rows)


def ew_benchmark(freq, lo, hi, names_fn=None):
    """Equal-weight eligible universe, open-to-open between consecutive execution dates, no costs."""
    Op, Cl, *_ = panel()
    days = Cl.index
    out = []
    ends = period_ends(freq, lo, hi)
    for a, b in zip(ends[:-1], ends[1:], strict=False):
        ea, eb = days[days.get_loc(a) + 1], days[min(days.get_loc(b) + 1, len(days) - 1)]
        names = names_fn(a) if names_fn else eligible(a)
        pa, pb = Op.loc[ea, names], Op.loc[eb, names].fillna(Cl.loc[:eb, names].ffill().iloc[-1])
        r = (pb / pa - 1).replace([np.inf, -np.inf], np.nan).dropna()
        out.append({"start": ea, "bench": float(r.mean())})
    return pd.DataFrame(out)


def stats(m, per_year):
    """m: frame with ret, bench columns (per period). Returns metric dict."""
    m = m.dropna(subset=["ret", "bench"])
    x = m.ret.values
    ex = (m.ret - m.bench).values
    n = len(x)
    eq = np.cumprod(1 + x)
    dd = float((eq / np.maximum.accumulate(eq) - 1).min()) if n else np.nan
    yrs = n / per_year
    return {
        "periods": n,
        "cagr": round(100 * (eq[-1] ** (1 / yrs) - 1), 2) if n else None,
        "sharpe": round(float(np.mean(x) / np.std(x, ddof=1) * np.sqrt(per_year)), 2)
        if n > 2
        else None,
        "maxdd": round(100 * dd, 1),
        "excess_ann": round(100 * float(np.mean(ex)) * per_year, 2),
        "t_excess": round(float(np.mean(ex) / (np.std(ex, ddof=1) / np.sqrt(n))), 2)
        if n > 2
        else None,
        "abs_ann": round(100 * float(np.mean(x)) * per_year, 2),
        "t_abs": round(float(np.mean(x) / (np.std(x, ddof=1) / np.sqrt(n))), 2) if n > 2 else None,
        "bench_ann": round(100 * float(np.mean(m.bench)) * per_year, 2),
        "costs_pct_ann": round(100 * float((m.costs / m.eq_start).mean()) * per_year, 2),
        "turnover_pct": round(100 * float((m.turnover / m.eq_start).mean()), 1),
    }


def half_years(m):
    m = m.dropna(subset=["ret", "bench"])
    h = m.start.dt.year.astype(str) + np.where(m.start.dt.month <= 6, "H1", "H2")
    return (m.ret - m.bench).groupby(h.values).mean().round(4).to_dict()
