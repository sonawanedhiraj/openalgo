"""Phase 4 — shadow-replay parity check for sector_follow_cap5_vol.

Extends outputs/r40_sector_follow_capped_2026-06-10/run_r40.py (and phase05/
run_phase05.py V_SF_STATIC30, the Sharpe-2.19 parity baseline).

GOAL: for the last 30 trading days, do the PRODUCTION decision functions
(services.sector_follow_service.duckdb_metrics_provider + passes_gates +
select_entries) reproduce the R40-style backtest's entries and P&L?

Two tracks compared, both restricted to the same 30 days:

  PRODUCTION  — for each day D, reconstruct the live 15:20-IST state by calling
                the production metrics provider with as_of = D 15:20 IST (reads
                1m bars up to 15:20; sector return = mapped index 1m; volume =
                partial-day sum vs prior-day full-day average). Gates via the
                production passes_gates; selection via production select_entries
                with open_positions = symbols entered the prior trading day
                (T+1 carryover, max-5 concurrent, vol_ratio tiebreaker).

  R40         — the backtest harness logic: full-day bars (1m aggregated to
                daily for stocks; daily-native for indices), per-entry-day gate
                screen, top-5 by vol_ratio. NO open-position carryover (the
                backtest deploys up to 5 fresh entries every day).

Both use the SAME post-Phase-3 sector_map.json and the SAME static-30 universe
from config_snapshot.json. P&L for BOTH tracks uses the full-day D close ->
full-day D+1 close minus the 0.0857% round-trip cost, so the P&L track isolates
arithmetic/cost parity on matched entries (the entry DECISION is where 15:20-vs-
EOD effects live).

Read-only on DuckDB. No restart / pytest / commit / orders.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import duckdb
import numpy as np
import pandas as pd

OUT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(OUT))
sys.path.insert(0, ROOT)
# db/ is gitignored — it lives only in the primary worktree. Allow an override.
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
IST_OFF = 19800
N_DAYS = 30

cfg = load_config(os.path.join(STRAT, "config_snapshot.json"))
smap = load_sector_map(os.path.join(STRAT, "sector_map.json"))
UNIV = list(cfg.universe)
COST = cfg.cost_pct_round_trip / 100.0
CAP = cfg.max_concurrent_positions
print(f"Universe: {len(UNIV)} stocks | cap={CAP} | cost={COST*100:.4f}% round-trip")
print(f"Sector map (production, post-Phase-3): RELIANCE->{smap.get('RELIANCE')} "
      f"DIXON->{smap.get('DIXON')}")

con = duckdb.connect(DB, read_only=True)

# --------------------------------------------------------------------------- #
# Full-day bar frames (R40-style): stocks aggregated from 1m, indices native D
# --------------------------------------------------------------------------- #
def daily_from_1m(symbols):
    q = f"""select symbol, ((timestamp+{IST_OFF})/86400)::BIGINT di,
            arg_min(open,timestamp) o, max(high) h, min(low) l,
            arg_max(close,timestamp) c, sum(volume) v
            from market_data where interval='1m' and symbol in ({','.join('?'*len(symbols))})
            group by symbol, di order by symbol, di"""
    df = con.execute(q, symbols).df()
    df['date'] = pd.to_datetime(df['di'] * 86400, unit='s').dt.date
    return df


def daily_native(symbols):
    q = f"""select symbol, ((timestamp+{IST_OFF})/86400)::BIGINT di,
            open o, high h, low l, close c, volume v
            from market_data where interval='D' and symbol in ({','.join('?'*len(symbols))})"""
    df = con.execute(q, symbols).df()
    df['date'] = pd.to_datetime(df['di'] * 86400, unit='s').dt.date
    return df


index_syms = sorted({smap.get(s, 'NIFTY') for s in UNIV} | {'NIFTY'})
stk = daily_from_1m(UNIV)
idx = daily_native(index_syms).sort_values(['symbol', 'date'])
idx['ret'] = idx.groupby('symbol')['c'].pct_change()
idx_ret = idx.set_index(['symbol', 'date'])['ret']

# Per-stock full-day metrics: ret, avgvol20, vol_ratio, sector_ret
stk = stk.sort_values(['symbol', 'date']).reset_index(drop=True)
stk['ret'] = stk.groupby('symbol')['c'].pct_change()
stk['avgvol20'] = stk.groupby('symbol')['v'].transform(lambda s: s.rolling(20).mean())
stk['vol_ratio'] = stk['v'] / stk['avgvol20']
stk['sector'] = stk['symbol'].map(lambda s: smap.get(s, 'NIFTY'))
stk['sret'] = stk.apply(lambda r: idx_ret.get((r['sector'], r['date']), np.nan), axis=1)
# next-day close for T+1 exit P&L
stk['next_c'] = stk.groupby('symbol')['c'].shift(-1)
stk['next_date'] = stk.groupby('symbol')['date'].shift(-1)

# fast lookup: (symbol, date) -> row
stk_idx = stk.set_index(['symbol', 'date'])

# --------------------------------------------------------------------------- #
# Trading-day calendar + the 30-day test window
# --------------------------------------------------------------------------- #
avail = sorted(stk['date'].dropna().unique())
# a day D is eligible only if a later trading day exists (need D+1 for exit)
elig = [d for i, d in enumerate(avail) if i < len(avail) - 1]
test_days = elig[-N_DAYS:]
test_set = set(test_days)
first_iter_idx = max(0, avail.index(test_days[0]) - 1)  # one prior day to seed carryover
iter_days = avail[first_iter_idx: avail.index(test_days[-1]) + 1]
print(f"Trading days available: {len(avail)} ({avail[0]} .. {avail[-1]})")
print(f"Test window: {len(test_days)} days ({test_days[0]} .. {test_days[-1]})")

# --------------------------------------------------------------------------- #
# R40 track — full-day gate screen + top-5 vol_ratio, no carryover
# --------------------------------------------------------------------------- #
r40_rows = []        # per-day per-stock decision detail
r40_entries = set()  # (date, symbol)
for d in test_days:
    day_cands = []
    for sym in UNIV:
        try:
            r = stk_idx.loc[(sym, d)]
        except KeyError:
            continue
        sret, sret_ok = r['sret'], (r['sret'] > 0.01)
        ret_ok = r['ret'] > 0.005
        vol_ok = (r['v'] > r['avgvol20'])
        valid = not (pd.isna(r['avgvol20']) or pd.isna(r['sret']) or pd.isna(r['ret']))
        passed = bool(valid and sret_ok and ret_ok and vol_ok)
        net = (r['next_c'] / r['c'] - 1 - COST) if not pd.isna(r['next_c']) else np.nan
        row = dict(date=str(d), symbol=sym,
                   sector=r['sector'], sector_ret=float(sret) if not pd.isna(sret) else None,
                   stock_ret=float(r['ret']) if not pd.isna(r['ret']) else None,
                   vol_ratio=float(r['vol_ratio']) if not pd.isna(r['vol_ratio']) else None,
                   gate_sector=bool(sret_ok and not pd.isna(sret)),
                   gate_stock=bool(ret_ok and not pd.isna(r['ret'])),
                   gate_vol=bool(vol_ok and not pd.isna(r['avgvol20'])),
                   passed=passed,
                   entry_close=float(r['c']),
                   exit_close=float(r['next_c']) if not pd.isna(r['next_c']) else None,
                   net_pct=float(net * 100) if not pd.isna(net) else None,
                   selected=False)
        if passed:
            day_cands.append(row)
        r40_rows.append(row)
    # top-5 by vol_ratio desc (mergesort stable), symbol asc as tiebreak-of-tiebreak
    day_cands.sort(key=lambda x: (-(x['vol_ratio'] or 0.0), x['symbol']))
    for row in day_cands[:CAP]:
        row['selected'] = True
        r40_entries.add((row['date'], row['symbol']))

r40_df = pd.DataFrame(r40_rows)

# --------------------------------------------------------------------------- #
# PRODUCTION track — production code at 15:20 IST, with T+1 carryover
# --------------------------------------------------------------------------- #
prod_rows = []
prod_entries = set()
prev_entries = set()  # symbols entered the prior processed trading day
for d in iter_days:
    as_of = datetime(d.year, d.month, d.day, 15, 20, tzinfo=IST)
    metrics = duckdb_metrics_provider(as_of, UNIV, smap, cfg, db_path=DB)
    open_syms = set(prev_entries)
    candidates = []
    for sym, m in metrics.items():
        passed = passes_gates(m, cfg)
        if passed:
            candidates.append({
                "symbol": sym, "vol_ratio": m["vol_ratio"],
                "stock_ret": m["stock_ret"], "sector_ret": m["sector_ret"],
                "current_price": m["current_price"],
            })
    sel = select_entries(candidates, open_syms, CAP)
    sel_syms = {c["symbol"] for c in sel}

    if d in test_set:
        for sym in UNIV:
            m = metrics.get(sym, {})
            sret, sret_v = m.get("sector_ret"), m.get("sector_ret")
            ret_v, vol_v = m.get("stock_ret"), m.get("vol_ratio")
            # full-day close for P&L (matches R40 / task step 4)
            try:
                fr = stk_idx.loc[(sym, d)]
                entry_close = float(fr['c'])
                exit_close = float(fr['next_c']) if not pd.isna(fr['next_c']) else None
            except KeyError:
                entry_close = exit_close = None
            net = ((exit_close / entry_close - 1 - COST) * 100
                   if entry_close and exit_close else None)
            prod_rows.append(dict(
                date=str(d), symbol=sym, sector=smap.get(sym, 'NIFTY'),
                sector_ret=float(sret_v) if sret_v is not None else None,
                stock_ret=float(ret_v) if ret_v is not None else None,
                vol_ratio=float(vol_v) if vol_v is not None else None,
                gate_sector=bool(sret_v is not None and sret_v > cfg.gate_sector_ret),
                gate_stock=bool(ret_v is not None and ret_v > cfg.gate_stock_ret),
                gate_vol=bool(vol_v is not None and vol_v > cfg.gate_vol_mult),
                passed=bool(passes_gates(m, cfg)),
                price_1520=float(m.get("current_price")) if m.get("current_price") else None,
                entry_close=entry_close, exit_close=exit_close,
                net_pct=net,
                open_at_eval=bool(sym in open_syms),
                selected=bool(sym in sel_syms),
            ))
            if sym in sel_syms:
                prod_entries.add((str(d), sym))
    prev_entries = sel_syms

prod_df = pd.DataFrame(prod_rows)

# --------------------------------------------------------------------------- #
# Track 1 — entries diff
# --------------------------------------------------------------------------- #
matched = prod_entries & r40_entries
prod_only = prod_entries - r40_entries
r40_only = r40_entries - prod_entries
union = prod_entries | r40_entries
jaccard = len(matched) / len(union) if union else 1.0

diff_rows = []
for (dt, sym) in sorted(union):
    cls = ("matched" if (dt, sym) in matched else
           "production_only" if (dt, sym) in prod_only else "r40_only")
    diff_rows.append(dict(date=dt, symbol=sym, category=cls))
entries_diff = pd.DataFrame(diff_rows)

# --------------------------------------------------------------------------- #
# Track 2 — P&L diff on matched entries
# --------------------------------------------------------------------------- #
prod_pnl = prod_df.set_index(['date', 'symbol'])['net_pct']
r40_pnl = r40_df.set_index(['date', 'symbol'])['net_pct']
pnl_rows = []
for (dt, sym) in sorted(matched):
    p = prod_pnl.get((dt, sym))
    r = r40_pnl.get((dt, sym))
    if p is None or r is None or pd.isna(p) or pd.isna(r):
        continue
    pnl_rows.append(dict(date=dt, symbol=sym, prod_net_pct=float(p),
                         r40_net_pct=float(r), abs_diff_pp=abs(float(p) - float(r))))
pnl_diff = pd.DataFrame(pnl_rows)

# --------------------------------------------------------------------------- #
# Save CSVs + print summary
# --------------------------------------------------------------------------- #
prod_df.to_csv(os.path.join(OUT, "production_decisions.csv"), index=False)
r40_df.to_csv(os.path.join(OUT, "r40_decisions.csv"), index=False)
entries_diff.to_csv(os.path.join(OUT, "entries_diff.csv"), index=False)
pnl_diff.to_csv(os.path.join(OUT, "pnl_diff.csv"), index=False)

mean_abs = pnl_diff['abs_diff_pp'].mean() if len(pnl_diff) else 0.0
max_abs = pnl_diff['abs_diff_pp'].max() if len(pnl_diff) else 0.0

print("\n================ TRACK 1: ENTRIES ================")
print(f"production entries: {len(prod_entries)}  r40 entries: {len(r40_entries)}")
print(f"matched: {len(matched)}  production_only: {len(prod_only)}  r40_only: {len(r40_only)}")
print(f"Jaccard: {jaccard:.4f}")
print("\nproduction_only (false positives):")
for x in sorted(prod_only):
    print("   ", x)
print("r40_only (false negatives):")
for x in sorted(r40_only):
    print("   ", x)

print("\n================ TRACK 2: P&L ================")
print(f"matched-with-pnl trades: {len(pnl_diff)}")
print(f"mean abs diff: {mean_abs:.4f}pp   max abs diff: {max_abs:.4f}pp")
if len(pnl_diff):
    worst = pnl_diff.sort_values('abs_diff_pp', ascending=False).head(5)
    print(worst.to_string(index=False))

# verdict
ENTRY_GATE = 0.95
PNL_GATE = 0.05
entries_pass = jaccard >= ENTRY_GATE
pnl_pass = (len(pnl_diff) == 0) or (max_abs < PNL_GATE)
if entries_pass and pnl_pass:
    verdict = "PASS"
elif jaccard >= 0.80 or max_abs < 0.5:
    verdict = "NEEDS_INVESTIGATION"
else:
    verdict = "FAIL"
print(f"\nVERDICT: {verdict}  (entries_pass={entries_pass}, pnl_pass={pnl_pass})")

import json  # noqa: E402
with open(os.path.join(OUT, "verdict.json"), "w") as f:
    json.dump(dict(
        test_days=len(test_days), first_day=str(test_days[0]), last_day=str(test_days[-1]),
        prod_entries=len(prod_entries), r40_entries=len(r40_entries),
        matched=len(matched), production_only=len(prod_only), r40_only=len(r40_only),
        jaccard=jaccard, pnl_matched=len(pnl_diff),
        pnl_mean_abs_pp=mean_abs, pnl_max_abs_pp=max_abs, verdict=verdict,
    ), f, indent=2)
con.close()
print(f"\nOutputs -> {OUT}")
