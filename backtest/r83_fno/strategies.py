"""R83 candidate signal functions — exactly the pre-registered specs (PREREGISTRATION.md). Each returns a
callable signal(day) -> {symbol: weight} using data up to and including `day`'s close."""

import sys

import core as K
import numpy as np
import pandas as pd

sys.path.insert(0, str(K.ROOT / "backtest/r81_entry"))
import common as R81C  # noqa: E402

SECTOR_A = [
    "BANKNIFTY",
    "NIFTYIT",
    "NIFTYPSUBANK",
    "NIFTYPHARMA",
    "NIFTYMETAL",
    "NIFTYPVTBANK",
    "NIFTYFMCG",
    "NIFTYAUTO",
    "NIFTY HEALTHCARE",
    "FINNIFTY",
    "NIFTY OIL AND GAS",
]
SECTOR_B = [
    "BANKNIFTY",
    "FINNIFTY",
    "NIFTY CONSR DURBL",
    "NIFTY HEALTHCARE",
    "NIFTY OIL AND GAS",
    "NIFTYAUTO",
    "NIFTYENERGY",
    "NIFTYFMCG",
    "NIFTYIT",
    "NIFTYMEDIA",
    "NIFTYMETAL",
    "NIFTYPHARMA",
    "NIFTYPSUBANK",
    "NIFTYPVTBANK",
    "NIFTYREALTY",
]


def _hist(day, cols, n):
    Op, Cl, *_ = K.panel()
    loc = Cl.index.get_loc(day)
    return Cl.iloc[max(0, loc - n) : loc + 1][cols]


def mom_scores(day, names, look, skip):
    h = _hist(day, names, look + 1)
    if len(h) < look + 1:
        return pd.Series(dtype=float)
    return (h.iloc[-1 - skip] / h.iloc[0] - 1).dropna()


def vol_scores(day, names, look):
    r = K.daily_returns()
    loc = r.index.get_loc(day)
    x = r.iloc[loc - look + 1 : loc + 1][names]
    s = x.std()
    return s[x.notna().sum() >= int(0.9 * look)].dropna()


def eq(names):
    return {s: 1.0 / len(names) for s in names} if names else {}


def B1(look=126, skip=21, n=10):
    def f(day):
        s = mom_scores(day, K.eligible(day), look, skip)
        return eq(list(s.nlargest(n).index))

    return f


def B2(look=60, n=10):
    def f(day):
        s = vol_scores(day, K.eligible(day), look)
        return eq(list(s.nsmallest(n).index))

    return f


def rp_weights(day, legs, rp=True):
    """legs: list of name lists; inverse-vol (annualised 60d mean member vol) between legs."""
    legs = [x for x in legs if x]
    if not legs:
        return {}
    if rp:
        v = vol_scores(day, sorted({s for x in legs for s in x}), 60)
        lv = np.array([v.reindex(x).mean() for x in legs])
        lw = (1 / lv) / (1 / lv).sum()
    else:
        lw = np.full(len(legs), 1 / len(legs))
    w = {}
    for x, wt in zip(legs, lw, strict=False):
        for s in x:
            w[s] = w.get(s, 0) + wt / len(x)
    return w


def B3(n=10, rp=True):
    def f(day):
        el = K.eligible(day)
        m = list(mom_scores(day, el, 126, 21).nlargest(n).index)
        lv = list(vol_scores(day, el, 60).nsmallest(n).index)
        return rp_weights(day, [m, lv], rp)

    return f


def B4(n_sec=3, n_stk=3, sec_look=126):
    def f(day):
        idx = ["IDX:" + x for x in SECTOR_B]
        h = _hist(day, idx, sec_look + 1)
        sr = (h.iloc[-1] / h.iloc[0] - 1).dropna().nlargest(n_sec)
        el = K.eligible(day)
        ms = mom_scores(day, el, 126, 21)
        pick = []
        for sec in sr.index:
            sec = sec[4:]
            members = [s for s in ms.index if R81C.sector_for(s) == sec]
            pick += list(ms.reindex(members).nlargest(n_stk).index)
        return eq(pick)

    return f


def B5(look=5, n=10):
    def f(day):
        el = K.eligible(day)
        h = _hist(day, el, look + 1)
        r = (h.iloc[-1] / h.iloc[0] - 1).dropna()
        return eq(list(r.nsmallest(n).index))

    return f


def A(look=126, n=3, lv_look=60, rp=True):
    def f(day):
        idx = ["IDX:" + x for x in SECTOR_A]
        h = _hist(day, idx, max(look, lv_look) + 1)
        m = (h.iloc[-1] / h.iloc[-1 - look] - 1).dropna().nlargest(n)
        r = h.pct_change().iloc[-lv_look:]
        v = r.std().dropna()
        lv = v.nsmallest(n)
        legs = [list(m.index), list(lv.index)]
        lvol = np.array([v.reindex(x).mean() for x in legs])
        lw = (1 / lvol) / (1 / lvol).sum() if rp else np.array([0.5, 0.5])
        w = {}
        for x, wt in zip(legs, lw, strict=False):
            for s in x:
                w[s] = w.get(s, 0) + wt / len(x)
        return w

    return f


def A_bench(day):
    return eq(["IDX:" + x for x in SECTOR_A])


REG = {
    "A": A(),
    "A_m63": A(look=63),
    "A_m252": A(look=252),
    "A_n2": A(n=2),
    "A_n4": A(n=4),
    "B1": B1(),
    "B1_3-1": B1(63, 21),
    "B1_12-1": B1(252, 21),
    "B1_n5": B1(n=5),
    "B1_n20": B1(n=20),
    "B2": B2(),
    "B2_20d": B2(20),
    "B2_120d": B2(120),
    "B2_n5": B2(n=5),
    "B2_n20": B2(n=20),
    "B3": B3(),
    "B3_5050": B3(rp=False),
    "B3_n5": B3(n=5),
    "B3_n20": B3(n=20),
    "B4": B4(),
    "B4_s2": B4(n_sec=2),
    "B4_k2": B4(n_stk=2),
    "B4_63": B4(sec_look=63),
    "B5": B5(),
    "B5_10d": B5(10),
    "B5_n5": B5(n=5),
    "B5_n20": B5(n=20),
}
FREQ = {k: ("W" if k.startswith("B5") else "M") for k in REG}
