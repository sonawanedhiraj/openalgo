"""R80 steps 3/4: exits a/b/c/h, R-ladder, giveback, long-side hypothesis, pre-registered criteria.

Follows PREREGISTRATION.md (+ Addendum A). Every evaluated cell -> data/variant_ledger{suf}.csv with the doc
17.6 fields. TRAIN = 2024, VALIDATE = 2025-01-01..06-19. Placebo: random same-size subsets (2,000 draws).
CI: day-block bootstrap (2,000). Stress: 5 bps per round trip of turnover.
  uv run python backtest/r80_scanner/score_r80.py [suffix]
"""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
SUF = sys.argv[1] if len(sys.argv) > 1 else ""
t = pd.read_parquet(HERE / "data" / f"trades_r80{SUF}.parquet").sort_values(["day", "entry_ts"]).reset_index(drop=True)
sl = pd.read_parquet(HERE / "data" / f"signal_ladder_r80{SUF}.parquet")
sig = pd.read_parquet(HERE / "data" / "signals_r80.parquet")
t["period"] = np.where(t.day < "2025-01-01", "train", "validate")
sl["period"] = np.where(sl.day < "2025-01-01", "train", "validate")
sig["period"] = np.where(sig.day < "2025-01-01", "train", "validate")
L = t.side == "long"
rng = np.random.default_rng(80)
EXITS = ("a", "b", "c", "h", "h075", "h125")
CODE_HASH = hashlib.sha1(b"".join(open(HERE / f, "rb").read() for f in
                                  ("signals_r80.py", "engine_r80.py", "score_r80.py", "PREREGISTRATION.md"))).hexdigest()[:10]
CONFIG = "cap20k x5, risk500, max6/day, ATR14 sl1.5, vol2.5, elapsed0.70, expiry20m, trail0.6R, gap1.5%, exch0.00307%"
for ex in EXITS:
    # turnover = buy+sell value, so 2.5 bps per side == 0.00025 * turnover
    t[f"stress_{ex}"] = t[f"net_{ex}"] - 0.00025 * t[f"turn_{ex}"]


def lvl_pass(mult):
    room_l = (t.swing5_hi - t.entry) / t.risk
    room_s = (t.entry - t.swing5_lo) / t.risk
    return np.where(L, (t.entry >= t.swing5_hi) | (room_l >= mult), (t.entry <= t.swing5_lo) | (room_s >= mult))


def struct(n):
    return np.where(L, t[f"hh{n}"].fillna(False).astype(bool) & t[f"hl{n}"].fillna(False).astype(bool),
                    t[f"lh{n}"].fillna(False).astype(bool) & t[f"ll{n}"].fillna(False).astype(bool))


dirret = np.where(L, t.nifty_ret, -t.nifty_ret)
F = {"BASELINE": (np.ones(len(t), bool), "base", True),
     "C3_room2R": (lvl_pass(2.0), "C3", True), "C3_room1.5R": (lvl_pass(1.5), "C3", False),
     "C3_room2.5R": (lvl_pass(2.5), "C3", False),
     "C1_struct6": (struct(6), "C1", True), "C1_struct4": (struct(4), "C1", False),
     "C1_struct8": (struct(8), "C1", False),
     "C2_with0.10": (dirret > 0.0010, "C2w", True), "C2_with0.05": (dirret > 0.0005, "C2w", False),
     "C2_with0.20": (dirret > 0.0020, "C2w", False),
     "C2_counter0.10": (dirret < -0.0010, "C2c", True), "C2_counter0.05": (dirret < -0.0005, "C2c", False),
     "C2_counter0.20": (dirret < -0.0020, "C2c", False),
     "C4_ext0.75": (t.ext_atr.values <= 0.75, "C4", True), "C4_ext0.5": (t.ext_atr.values <= 0.5, "C4", False),
     "C4_ext1.0": (t.ext_atr.values <= 1.0, "C4", False)}
NEIGH = {"C3_room2R": ["C3_room1.5R", "C3_room2.5R"], "C1_struct6": ["C1_struct4", "C1_struct8"],
         "C2_with0.10": ["C2_with0.05", "C2_with0.20"], "C2_counter0.10": ["C2_counter0.05", "C2_counter0.20"],
         "C4_ext0.75": ["C4_ext0.5", "C4_ext1.0"]}
PRIMARY_EXITS = ("a", "b", "c", "h")


def boot(x, days):
    df = pd.DataFrame({"x": np.asarray(x, float), "d": np.asarray(days)})
    g = df.groupby("d").x.agg(["sum", "count"])
    if len(g) < 5:
        return (np.nan, np.nan)
    s, c = g["sum"].values, g["count"].values
    idx = rng.integers(0, len(g), (2000, len(g)))
    return tuple(np.round(np.percentile(s[idx].sum(1) / c[idx].sum(1), [2.5, 97.5]), 1))


def maxdd(x):
    cum = np.cumsum(x)
    return float((np.maximum.accumulate(np.r_[0, cum])[1:] - cum).max()) if len(x) else 0.0


def stats(sub, ex):
    x = sub[f"net_{ex}"]
    n = len(x)
    if n == 0:
        return dict(n=0)
    win, loss = x[x > 0].sum(), -x[x <= 0].sum()
    r = dict(n=n, days=sub.day.nunique(), gross=round(sub[f"gross_{ex}"].sum(), 0), net=round(x.sum(), 0),
             exp_net=round(x.mean(), 1), exp_gross=round(sub[f"gross_{ex}"].mean(), 1),
             exp_net_per_day=round(x.sum() / sub.day.nunique(), 1),
             exp_stress=round(sub[f"stress_{ex}"].mean(), 1), win_pct=round(100 * (x > 0).mean(), 1),
             pf=round(win / loss, 2) if loss else None, maxdd=round(maxdd(x.values), 0),
             cap_breaches=int((x < -500).sum()), ci=boot(x, sub.day))
    for k in ("brk", "stt", "exch", "sebi", "gst", "stamp"):
        r[k] = round(sub[f"{k}_{ex}"].sum(), 0)
    for k in ("0_5", "1_0", "1_5", "2_0"):
        r[f"pct_{k}R"] = round(100 * sub[f"r{k}_before_stop"].mean(), 1)
    return r


def placebo(pool, n, val):
    pool = np.asarray(pool, float)
    if n == 0 or n >= len(pool):
        return None
    d = np.array([rng.choice(pool, n, replace=False).mean() for _ in range(2000)])
    return round(float((d >= val).mean()), 4)


ledger = []
for name, (mask, fam, prim) in F.items():
    mask = np.asarray(mask, bool)
    for ex in EXITS:
        exit_prim = ex in PRIMARY_EXITS
        for side in ("all", "long", "short"):
            sm = mask & ((t.side == side).values if side != "all" else True)
            tr, va = t[sm & (t.period == "train").values], t[sm & (t.period == "validate").values]
            btr = t[((t.side == side) if side != "all" else True) & (t.period == "train")]
            bva = t[((t.side == side) if side != "all" else True) & (t.period == "validate")]
            s_tr, s_va = stats(tr, ex), stats(va, ex)
            row = dict(variant=name, family=fam, primary=prim and exit_prim, exit=ex, side=side,
                       code_hash=CODE_HASH, config=CONFIG, split="train 2024 / validate 2025-01..06-19",
                       signals_train=int((sig.period == "train").sum()), signals_val=int((sig.period == "validate").sum()),
                       slippage="base 0; stress 5 bps/round trip")
            for p, s, b in (("train", s_tr, btr), ("val", s_va, bva)):
                for k, v in s.items():
                    row[f"{p}_{k}"] = v
                row[f"{p}_delta"] = round(s.get("exp_net", np.nan) - b[f"net_{ex}"].mean(), 1) if s["n"] else None
            row["placebo_p"] = placebo(bva[f"net_{ex}"], s_va["n"], s_va.get("exp_net")) \
                if (name != "BASELINE" and s_va["n"]) else None
            ledger.append(row)
led = pd.DataFrame(ledger)


def verdict(r):
    if r.variant == "BASELINE" or r.variant not in NEIGH or r.exit not in PRIMARY_EXITS:
        return ""
    nb = led[(led.variant.isin(NEIGH[r.variant])) & (led.exit == r.exit) & (led.side == r.side)]
    td, vd = r.train_delta or 0, r.val_delta or 0
    if td > 0 and vd > 0 and (r.val_exp_net or -1) > 0 and r.val_n >= 30 and (nb.val_delta > 0).all() \
            and r.placebo_p is not None and r.placebo_p < 0.05:
        return "CANDIDATE"
    if td > 0 and vd <= 0:
        return "train-only (noise)"
    if td <= 0 and vd > 0:
        return "validate-only (noise)"
    if td > 0 and vd > 0:
        return "both periods up; fails " + ",".join(
            k for k, ok in (("val_exp>0", (r.val_exp_net or -1) > 0), ("n>=30", r.val_n >= 30),
                            ("plateau", (nb.val_delta > 0).all()),
                            ("placebo", r.placebo_p is not None and r.placebo_p < 0.05)) if not ok)
    return "no improvement"


led["verdict"] = led.apply(verdict, axis=1)
led.insert(0, "row", range(1, len(led) + 1))
led.to_csv(HERE / "data" / f"variant_ledger{SUF}.csv", index=False)

# ---- R-threshold ladder (17.2) on engine entries and on all armed signals ----
lad = {}
for popname, P in (("engine_entries", t), ("armed_signals", sl)):
    for side in ("long", "short"):
        for per in ("train", "validate"):
            s = P[(P.side == side) & (P.period == per)]
            rows = {}
            for k in ("0_5", "1_0", "1_5", "2_0"):
                hit = s[f"r{k}_before_stop"].astype(bool)
                rows[k.replace("_", ".") + "R"] = dict(
                    n=len(s), reached_pct=round(100 * hit.mean(), 1),
                    median_min=round(float(s.loc[hit, f"t{k}_min"].median()), 0) if hit.any() else None,
                    reverse_to_stop_pct=round(100 * s.loc[hit, f"rev{k}"].astype(bool).mean(), 1) if hit.any() else None)
            rows["stop_first_pct"] = round(100 * s.stopped.astype(bool).mean(), 1) if len(s) else None
            lad[f"{popname}|{side}|{per}"] = rows
# ---- giveback on exit (a) ----
gb = {}
for side in ("long", "short", "all"):
    for per in ("train", "validate"):
        s = t[((t.side == side) if side != "all" else True) & (t.period == per)]
        gb[f"{side}|{per}"] = dict(n=len(s), peak_R_median=round(s.peak_R_a.median(), 2),
                                   realised_R_median=round(s.real_R_a.median(), 2),
                                   giveback_R=dict(zip(("p50", "p75", "p90"), np.round(s.giveback_R_a.quantile([.5, .75, .9]).values, 2))),
                                   giveback_R_mean=round(s.giveback_R_a.mean(), 2),
                                   among_peak_ge_1R_giveback_median=round(s[s.peak_R_a >= 1].giveback_R_a.median(), 2),
                                   n_peak_ge_1R=int((s.peak_R_a >= 1).sum()))
# ---- long-side removal hypothesis (17.5) ----
ls = {}
for ex in PRIMARY_EXITS:
    for per in ("train", "validate"):
        s = t[t.period == per]
        days = sorted(s.day.unique())
        both = s.groupby("day")[f"net_{ex}"].sum().reindex(days, fill_value=0)
        sho = s[s.side == "short"].groupby("day")[f"net_{ex}"].sum().reindex(days, fill_value=0)
        diff = (sho - both).values  # = minus the long P&L per day
        idx = rng.integers(0, len(diff), (2000, len(diff)))
        ci = np.round(np.percentile(diff[idx].mean(1), [2.5, 97.5]), 1)
        ls[f"{ex}|{per}"] = dict(days=len(days), both_per_day=round(both.mean(), 1), short_only_per_day=round(sho.mean(), 1),
                                 diff_per_day=round(diff.mean(), 1), diff_ci=tuple(ci))
for ex in PRIMARY_EXITS:
    tr, va = ls[f"{ex}|train"], ls[f"{ex}|validate"]
    ls[f"{ex}|verdict"] = "short-only better (confirmed)" if (tr["diff_per_day"] > 0 and va["diff_per_day"] > 0
                                                              and va["diff_ci"][0] > 0) else "not confirmed"
out = dict(code_hash=CODE_HASH, n_ledger_rows=len(led), n_primary_rows=int(led.primary.sum()),
           counts=dict(trades=len(t), signals=len(sig), trading_days=int(sig.day.nunique()),
                       by_period_side={"|".join(k): int(v) for k, v in t.groupby(["period", "side"]).size().items()},
                       signals_by_period_dir={"|".join(k): int(v) for k, v in sig.groupby(["period", "direction"]).size().items()},
                       signals_per_day=round(len(sig) / sig.day.nunique(), 2),
                       trades_per_day=round(len(t) / sig.day.nunique(), 2),
                       trade_days=int(t.day.nunique())),
           ladder=lad, giveback=gb, long_side=ls)
json.dump(out, open(HERE / "data" / f"score{SUF}.json", "w"), indent=1, default=str)
pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 30)
print(json.dumps(out["counts"], default=str))
base = led[led.variant == "BASELINE"][["exit", "side", "train_n", "train_exp_gross", "train_exp_net", "train_ci",
                                       "val_n", "val_exp_gross", "val_exp_net", "val_ci", "val_exp_stress", "val_pf", "val_maxdd"]]
print(base.to_string())
for k, v in lad.items():
    print("LADDER", k, v)
for k, v in gb.items():
    print("GIVEBACK", k, v)
for k, v in ls.items():
    print("LONGSIDE", k, v)
print(led[(led.verdict != "")][["variant", "exit", "side", "train_n", "train_delta", "val_n", "val_exp_net",
                                 "val_delta", "placebo_p", "verdict"]].to_string())
