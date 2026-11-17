"""R85 trade simulator + candidates O1..O5 (see PREREGISTRATION.md). Daily closes only."""

import math

import engine as E
import numpy as np
import pandas as pd

MIN_CONTRACTS = 10  # short leg must have traded >= 10 contracts on the entry day
MIN_OI_LOTS = 50  # and carry >= 50 lots of open interest


class Chains:
    """Per-(symbol, expiry) option chain cache for one window."""

    def __init__(self):
        self.c = {}

    def get(self, sym, exp, d0, d1):
        k = (sym, exp)
        if k not in self.c or self.c[k][0] > d0 or self.c[k][1] < d1:
            df = E.chain(sym, exp, d0, d1)
            self.c[k] = (d0, d1, df)
        df = self.c[k][2]
        return df[(df.date >= d0) & (df.date <= d1)]


def pick_strike(ch_day, opt, target, side_beyond=None):
    """Nearest strike to target among strikes that TRADED that day; side_beyond='down'/'up' forces strictly beyond."""
    x = ch_day[(ch_day.opt == opt) & (ch_day.contracts >= 1)]
    if side_beyond == "down":
        x = x[x.strike < target[1]]
        t = target[0]
    elif side_beyond == "up":
        x = x[x.strike > target[1]]
        t = target[0]
    else:
        t = target
    if x.empty:
        return None
    return x.iloc[(x.strike - t).abs().argsort().iloc[0]]


def run_trade(
    chains, sym, exp, entry, deadline, legs, lot, scen, pt=0.5, sl=2.0, margin_fn=None, ramp=False
):
    """legs: list of (opt, strike, side) side=-1 short / +1 long. Returns trade dict or None (not enterable)."""
    ch = chains.get(sym, exp, entry, deadline)
    days = sorted(ch.date.unique())
    if not days or days[0] != entry:
        return None
    piv = {(r.date, r.opt, r.strike): r for r in ch.itertuples()}
    e_px, costs = [], 0.0
    for opt, K, side in legs:
        r = piv.get((entry, opt, K))
        if r is None or r.contracts < 1 or r.close <= 0:
            return None
        if side < 0 and (r.contracts < MIN_CONTRACTS or r.oi / lot < MIN_OI_LOTS):
            return None
        e_px.append(r.close)
        costs += E.opt_charges(r.close, lot, sell=side < 0) + E.half_spread(r.close, scen) * lot
    credit = -sum(
        side * p for (_, _, side), p in zip(legs, e_px, strict=False)
    )  # >0 for net credit
    path, prev_v, reason, exit_day, max_margin = [], -credit, "time", deadline, 0.0
    spot = E.spot_and_lot()
    for d in days:
        v, pen = 0.0, 0.0
        for opt, K, side in legs:
            r = piv.get((d, opt, K))
            if r is None:
                p, stale = None, True
            else:
                traded = r.contracts >= 1 and r.close > 0
                p, stale = (r.close if traded else r.settle), not traded
            if p is None or (isinstance(p, float) and math.isnan(p)):
                p, stale = 0.0, True
            v += side * p
            pen += E.half_spread(p, scen) * (2 if stale else 1)
        # margin today
        s = spot.loc[(sym, d), "spot"] if (sym, d) in spot.index else np.nan
        m = margin_fn(s) if (margin_fn and not np.isnan(s)) else 0.0
        if ramp and not np.isnan(s):
            k = E.trading_days_between(d, exp)
            if k in E.DELIVERY_RAMP:
                for opt, K, side in legs:
                    itm = (s > K) if opt == "CE" else (s < K)
                    if side < 0 and itm:
                        m += E.DELIVERY_RAMP[k] * s * lot
        max_margin = max(max_margin, m)
        path.append((d, (v - prev_v) * lot))
        prev_v = v
        if d == entry:
            continue
        cost_to_close = -v  # positive = debit to buy back shorts net
        if credit > 0 and pt is not None and cost_to_close <= (1 - pt) * credit:
            reason, exit_day = "target", d
        elif credit > 0 and sl is not None and cost_to_close >= (1 + sl) * credit:
            reason, exit_day = "stop", d
        elif d >= deadline:
            reason, exit_day = "time", d
        else:
            continue
        exit_v, exit_pen = v, pen
        break
    else:
        exit_v, exit_pen, exit_day = v, pen, days[-1]
    for (opt, K, side), _ in zip(legs, e_px, strict=False):
        r = piv.get((exit_day, opt, K))
        p = (
            r.close if r is not None and r.contracts >= 1 else (r.settle if r is not None else 0.0)
        ) or 0.0
        costs += E.opt_charges(p, lot, sell=side > 0)
    costs += exit_pen * lot
    gross = (exit_v + credit) * lot
    pth = pd.DataFrame(path, columns=["date", "mtm"])
    pth = pth[pth.date <= exit_day]
    return {
        "symbol": sym,
        "expiry": exp,
        "entry": entry,
        "exit": exit_day,
        "reason": reason,
        "credit": credit * lot,
        "gross": gross,
        "costs": costs,
        "net": gross - costs,
        "lot": lot,
        "max_margin": max_margin,
        "legs": ";".join(f"{o}{K:g}{'S' if s < 0 else 'L'}" for o, K, s in legs),
        "path": pth,
    }


# ------------------------------------------------------------------ structures
def strangle_legs(ch_day, S, sig, T, z):
    d = S * math.exp(-z * sig * math.sqrt(T))
    u = S * math.exp(z * sig * math.sqrt(T))
    p, c = pick_strike(ch_day, "PE", d), pick_strike(ch_day, "CE", u)
    if p is None or c is None or p.strike >= S or c.strike <= S:
        return None
    return [("PE", float(p.strike), -1), ("CE", float(c.strike), -1)]


def wing(ch_day, S, sig, T, z_long, opt, short_K):
    tgt = S * math.exp((-1 if opt == "PE" else 1) * z_long * sig * math.sqrt(T))
    w = pick_strike(ch_day, opt, (tgt, short_K), side_beyond="down" if opt == "PE" else "up")
    return None if w is None else (opt, float(w.strike), +1)
