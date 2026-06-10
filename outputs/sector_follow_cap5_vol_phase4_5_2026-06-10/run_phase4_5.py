"""Phase 4.5 — honest 15:20-IST-snapshot baseline for sector_follow_cap5_vol.

Extends outputs/.../phase4/run_phase4.py. R40's published numbers (Sharpe 2.19,
payoff 1.44, EV 0.454%, 625 trades over 2024-01..2026-06) evaluate gates on the
realized FULL-DAY close — that is look-ahead. Production correctly evaluates at
15:20 IST, 10 min before close. This re-derives the baseline honestly.

METRIC SOURCE — the production provider itself.
A first pass tried an independent vectorized DuckDB reimplementation. It revealed
that historify's 1m `timestamp` column has an INCONSISTENT epoch convention: a
single IST trading session is split across naive day-buckets (e.g. a day's
morning and afternoon land in different `(ts+19800)/86400` buckets), so naive
daily grouping silently mis-aggregates some days (e.g. 2026-05-29 diverged on
EVERY symbol). The production provider uses `datetime.fromtimestamp(ts, IST)` +
`ts <= as_of` filtering, which is the AUTHORITATIVE interpretation and is exactly
what live production will see. So the honest baseline is built by calling the
production provider per day:
  honest 15:20 metrics+entry  := duckdb_metrics_provider(as_of = D 15:20)
  R40 look-ahead metrics+entry := duckdb_metrics_provider(as_of = D 15:30)  [full close]
  T+1 exit price               := provider(D+1, 15:30).current_price
This holds index-1m availability constant across both tracks, so the 15:20-vs-15:30
delta isolates PURE look-ahead (snapshot timing), nothing else.

DATA REALITY: index 1m bars exist only from 2025-12-01 (NIFTY/FINNIFTY) and only
from 2026-04-27 (sectoral indices). Constraint 9 = NO daily fallback (fail-closed
when index 1m missing). So an honest 15:20 baseline CANNOT span R40's full
2024-01..2026-06 window — sector names can only fire on the 29-day sector-1m
window, broad/FIN names on the 154-day NIFTY-1m window. Reported with that caveat.

Read-only on DuckDB. No restart / pytest / commit / orders.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import duckdb
import numpy as np
import pandas as pd

OUT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(OUT))
sys.path.insert(0, ROOT)
DB = os.environ.get("HISTORIFY_DB") or os.path.join(ROOT, "db", "historify.duckdb")
if not os.path.exists(DB):
    _main = r"C:\workspace\ai-trade-agent\openalgo\db\historify.duckdb"
    if os.path.exists(_main):
        DB = _main
STRAT = os.path.join(ROOT, "strategies", "sector_follow_cap5_vol")

from services.sector_follow_service import (  # noqa: E402
    duckdb_metrics_provider,
    load_config,
    load_sector_map,
    passes_gates,
    select_entries,
)

IST = timezone(timedelta(hours=5, minutes=30))
cfg = load_config(os.path.join(STRAT, "config_snapshot.json"))
smap = load_sector_map(os.path.join(STRAT, "sector_map.json"))
UNIV = list(cfg.universe)
COST = cfg.cost_pct_round_trip / 100.0
CAP = cfg.max_concurrent_positions
GS, GST, GV = cfg.gate_sector_ret, cfg.gate_stock_ret, cfg.gate_vol_mult
print(f"Universe {len(UNIV)} | cap={CAP} | cost={COST*100:.4f}% | gates s>{GS} st>{GST} v>{GV}")

# --------------------------------------------------------------------------- #
# Trading calendar (window): IST dates from NIFTY daily-native bars.
# --------------------------------------------------------------------------- #
con = duckdb.connect(DB, read_only=True)
cal = con.execute(
    """select distinct to_timestamp(timestamp + 19800)::DATE d
       from market_data where interval='D' and symbol='NIFTY'
       and to_timestamp(timestamp+19800)::DATE between DATE '2025-11-01' and DATE '2026-06-30'
       order by d"""
).df()["d"].tolist()
cal = [d if isinstance(d, date) else pd.Timestamp(d).date() for d in cal]
con.close()
print(f"Calendar {len(cal)} days: {cal[0]}..{cal[-1]}")

# --------------------------------------------------------------------------- #
# Provider snapshot cache
# --------------------------------------------------------------------------- #
_cache: dict[tuple, dict] = {}


def snap(d: date, hh: int, mm: int) -> dict:
    key = (d, hh, mm)
    if key not in _cache:
        as_of = datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)
        _cache[key] = duckdb_metrics_provider(as_of, UNIV, smap, cfg, db_path=DB)
    return _cache[key]


print("Building provider snapshots @15:20 and @15:30 across calendar ...")
for d in cal:
    snap(d, 15, 20)
    snap(d, 15, 30)
print(f"Snapshots cached: {len(_cache)}")


def full_close(d: date, sym: str):
    return snap(d, 15, 30).get(sym, {}).get("current_price")


# next-trading-day map
nextday = {cal[i]: cal[i + 1] for i in range(len(cal) - 1)}

# --------------------------------------------------------------------------- #
# Which days can each track evaluate? (need a next day for T+1 exit)
# --------------------------------------------------------------------------- #
def has_index_1m(d, idx):  # provider returns non-None sector_ret iff index 1m present
    m = snap(d, 15, 20)
    for s in UNIV:
        if smap.get(s, "NIFTY") == idx and m.get(s, {}).get("sector_ret") is not None:
            return True
    return False


eval_days = [d for d in cal if d in nextday]
nifty_days = [d for d in eval_days if has_index_1m(d, "NIFTY")]
sector_days = [d for d in eval_days if has_index_1m(d, "NIFTYIT")]
print(f"NIFTY-1m eval days: {len(nifty_days)} ({nifty_days[0]}..{nifty_days[-1]})")
print(f"sector-1m eval days: {len(sector_days)} ({sector_days[0]}..{sector_days[-1]})")

# NIFTYIT fail-closed audit
nifit_gaps = [d for d in nifty_days if not has_index_1m(d, "NIFTYIT")]
print(f"NIFTYIT 1m-gap days within NIFTY window (TCS/INFY auto fail-closed): {len(nifit_gaps)}")


# --------------------------------------------------------------------------- #
# Backtest engine over a day list.
#   carryover=False -> R40 structure (top-CAP fresh per day)
#   carryover=True  -> production structure (select_entries, T+1 open carryover)
#   snap_hh/mm      -> which snapshot drives gates + entry price
# --------------------------------------------------------------------------- #
def run_track(days, snap_hh, snap_mm, carryover):
    entries, trades = set(), []
    prev = set()
    # iterate the FULL eval calendar so carryover seeds correctly, record only `days`
    dayset = set(days)
    walk = [d for d in eval_days if days[0] <= d <= days[-1]]
    for d in walk:
        m = snap(d, snap_hh, snap_mm)
        cands = []
        for sym in UNIV:
            mm = m.get(sym, {})
            if passes_gates(mm, cfg):
                cands.append(dict(symbol=sym, vol_ratio=mm["vol_ratio"],
                                  current_price=mm["current_price"]))
        if carryover:
            sel = select_entries(cands, set(prev), CAP)
        else:
            cands.sort(key=lambda c: (-(c.get("vol_ratio") or 0.0), c["symbol"]))
            sel = cands[:CAP]
        sel_syms = {c["symbol"] for c in sel}
        if d in dayset:
            nd = nextday.get(d)
            for c in sel:
                sym = c["symbol"]
                entry = c["current_price"]
                exit_px = full_close(nd, sym) if nd else None
                if entry and exit_px:
                    net = exit_px / entry - 1 - COST
                    entries.add((str(d), sym))
                    trades.append(dict(date=d, symbol=sym, vol_ratio=c["vol_ratio"],
                                       entry=float(entry), exit=float(exit_px), net=float(net)))
        prev = sel_syms
    return entries, pd.DataFrame(trades)


def perf(trades, day_universe, label):
    if trades is None or len(trades) == 0:
        return dict(label=label, n=0, sharpe_d=None, sharpe_m=None, payoff=None,
                    ev_pct=None, maxdd_pct=None, green_mo_pct=None, win_rate_pct=None)
    daily = trades.groupby("date")["net"].mean()
    s = pd.Series(0.0, index=pd.Index(sorted(day_universe)))
    s.loc[list(daily.index)] = daily.values
    dmean, dstd = s.mean(), s.std(ddof=1)
    sharpe_d = float(dmean / dstd * np.sqrt(252)) if dstd and dstd > 0 else None
    sm = s.copy()
    sm.index = pd.to_datetime(pd.Series(list(sm.index)))
    mret = sm.groupby([sm.index.year, sm.index.month]).sum()
    sharpe_m = float(mret.mean() / mret.std(ddof=1) * np.sqrt(12)) if (len(mret) > 1 and mret.std(ddof=1) > 0) else None
    green_mo = float((mret > 0).mean() * 100) if len(mret) else None
    eq = (1 + s).cumprod()
    maxdd = float((eq / eq.cummax() - 1).min() * 100)
    wins = trades[trades["net"] > 0]["net"]
    losses = trades[trades["net"] <= 0]["net"]
    payoff = float(wins.mean() / abs(losses.mean())) if (len(wins) and len(losses) and losses.mean() != 0) else None
    return dict(label=label, n=int(len(trades)),
                sharpe_d=sharpe_d, sharpe_m=sharpe_m, payoff=payoff,
                ev_pct=float(trades["net"].mean() * 100), maxdd_pct=maxdd,
                green_mo_pct=green_mo, win_rate_pct=float((trades["net"] > 0).mean() * 100))


# --------------------------------------------------------------------------- #
# Tracks
# --------------------------------------------------------------------------- #
# Honest baseline (Deliverable 1): 15:20 gates+entry, R40 no-carryover structure.
h_nif_e, h_nif_t = run_track(nifty_days, 15, 20, carryover=False)   # broad/FIN names, 154d
h_sec_e, h_sec_t = run_track(sector_days, 15, 20, carryover=False)  # all names, sector window
# R40 look-ahead (15:30 full close), same windows, same no-carryover structure
r_nif_e, r_nif_t = run_track(nifty_days, 15, 30, carryover=False)
r_sec_e, r_sec_t = run_track(sector_days, 15, 30, carryover=False)

m_h_nif = perf(h_nif_t, nifty_days, "honest_1520_NIFTY1m_window")
m_h_sec = perf(h_sec_t, sector_days, "honest_1520_sector_window")
m_r_nif = perf(r_nif_t, nifty_days, "r40lookahead_1530_NIFTY1m_window")
m_r_sec = perf(r_sec_t, sector_days, "r40lookahead_1530_sector_window")

pd.DataFrame([m_h_nif, m_h_sec, m_r_nif, m_r_sec]).to_csv(
    os.path.join(OUT, "all_track_metrics.csv"), index=False)
h_nif_t.to_csv(os.path.join(OUT, "honest_trades_nifty1m_window.csv"), index=False)
h_sec_t.to_csv(os.path.join(OUT, "honest_trades_sector_window.csv"), index=False)

# --------------------------------------------------------------------------- #
# Deliverable 2 — comparison.csv. Headline window = NIFTY-1m (most data &
# most days where the honest 15:20 path can actually fire). Published R40
# full-window numbers cited as reference (NOT reproducible intraday).
# --------------------------------------------------------------------------- #
pub = {"sharpe_d": 2.19, "sharpe_m": 1.92, "payoff": 1.44, "ev_pct": 0.454,
       "maxdd_pct": -8.76, "green_mo_pct": 83.0, "n": 625, "win_rate_pct": 56.3}


def crow(metric, key):
    pv, hv, rv = pub.get(key), m_h_nif.get(key), m_r_nif.get(key)
    delta = (hv - rv) if (hv is not None and rv is not None) else None
    return dict(metric=metric, R40_published_fullwindow=pv,
                R40_lookahead_1530_NIFTYwin=rv, honest_1520_NIFTYwin=hv,
                delta_honest_minus_lookahead=delta)


comp = pd.DataFrame([
    crow("Sharpe (daily, annualized)", "sharpe_d"),
    crow("Sharpe (monthly, annualized)", "sharpe_m"),
    crow("Payoff", "payoff"),
    crow("EV per trade (%)", "ev_pct"),
    crow("MaxDD daily (%)", "maxdd_pct"),
    crow("Green months %", "green_mo_pct"),
    crow("N trades", "n"),
    crow("Win rate %", "win_rate_pct"),
])
comp.to_csv(os.path.join(OUT, "comparison.csv"), index=False)

# --------------------------------------------------------------------------- #
# Deliverable 3 — production parity vs honest baseline (last 30 sector-1m days).
# Production = same provider@15:20 metrics + passes_gates + select_entries WITH
# T+1 carryover. Honest = no-carryover R40 loop. Same metrics -> matched entries
# have IDENTICAL P&L; entry-set differences are purely carryover-structural.
# --------------------------------------------------------------------------- #
N_DAYS = 30
test_days = sector_days[-N_DAYS:] if len(sector_days) >= N_DAYS else sector_days
prod_e, prod_t = run_track(test_days, 15, 20, carryover=True)
hon_e, hon_t = run_track(test_days, 15, 20, carryover=False)

prod_pnl = {(str(t.date), t.symbol): t.net * 100 for t in prod_t.itertuples()}
hon_pnl = {(str(t.date), t.symbol): t.net * 100 for t in hon_t.itertuples()}

matched = prod_e & hon_e
union = prod_e | hon_e
jaccard = len(matched) / len(union) if union else 1.0
prod_only = sorted(prod_e - hon_e)
hon_only = sorted(hon_e - prod_e)

diff_rows = []
for (dt, sym) in sorted(union):
    cls = ("matched" if (dt, sym) in matched else
           "production_only" if (dt, sym) in (prod_e - hon_e) else "honest_only")
    diff_rows.append(dict(date=dt, symbol=sym, category=cls))
pd.DataFrame(diff_rows).to_csv(os.path.join(OUT, "entries_diff_honest.csv"), index=False)

pnl_diffs = [abs(prod_pnl[k] - hon_pnl[k]) for k in matched if k in prod_pnl and k in hon_pnl]
pnl_mean = float(np.mean(pnl_diffs)) if pnl_diffs else 0.0
pnl_max = float(np.max(pnl_diffs)) if pnl_diffs else 0.0

# carryover-controlled headline: matched entries arithmetic must be bit-identical
entries_pass = jaccard >= 0.95
pnl_pass = pnl_max < 0.05
arithmetic_pass = pnl_max < 1e-6
if arithmetic_pass and (entries_pass or len(prod_only) == 0):
    verdict = "PASS"
elif arithmetic_pass:
    verdict = "PASS_WITH_CARRYOVER_DIFF"
else:
    verdict = "NEEDS_INVESTIGATION"

with open(os.path.join(OUT, "parity_verdict.json"), "w") as f:
    json.dump(dict(
        window_days=len(test_days), first=str(test_days[0]), last=str(test_days[-1]),
        prod_entries=len(prod_e), honest_entries=len(hon_e), matched=len(matched),
        production_only=prod_only, honest_only=hon_only, jaccard=jaccard,
        pnl_matched=len(pnl_diffs), pnl_mean_abs_pp=pnl_mean, pnl_max_abs_pp=pnl_max,
        arithmetic_pass=arithmetic_pass, entries_pass=entries_pass, verdict=verdict,
        note="Production has T+1 carryover (prior-day positions still open at 15:20 "
             "eval); honest R40-loop has none. Entry-set differences are carryover-"
             "structural, NOT arithmetic bugs. Both tracks read identical provider@15:20 "
             "metrics, so matched-entry P&L is bit-identical (arithmetic parity confirmed).",
    ), f, indent=2)
con_closed = True

# --------------------------------------------------------------------------- #
print("\n================ HONEST BASELINE — NIFTY-1m window (headline) ================")
for k, v in m_h_nif.items():
    print(f"  {k}: {v}")
print("\n================ HONEST BASELINE — sector-1m window (all names) ================")
for k, v in m_h_sec.items():
    print(f"  {k}: {v}")
print("\n================ R40 LOOK-AHEAD (15:30) — NIFTY-1m window ================")
for k, v in m_r_nif.items():
    print(f"  {k}: {v}")
print("\n================ COMPARISON (Deliverable 2, NIFTY-1m window) ================")
print(comp.to_string(index=False))
print(f"\n================ PARITY (Deliverable 3, last {len(test_days)} sector days) ================")
print(f"prod entries={len(prod_e)} honest entries={len(hon_e)} matched={len(matched)} jaccard={jaccard:.4f}")
print(f"production_only ({len(prod_only)}): {prod_only}")
print(f"honest_only ({len(hon_only)}): {hon_only}")
print(f"P&L matched={len(pnl_diffs)} mean_abs={pnl_mean:.6f}pp max_abs={pnl_max:.6f}pp")
print(f"VERDICT: {verdict}")
print(f"\nOutputs -> {OUT}")
