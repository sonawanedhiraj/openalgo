"""Issue #502 — past-year retest of open15_vol_breakout on the PRODUCTION rules.

Supersedes the R58/R60 options numbers. The old year-long BS run
(`run_bs_backtest.py`) used its own `find_signal`, whose baseline INCLUDES the
09:15 bar — i.e. the pre-#502 gate that fires at roughly half the correct rate.
Its trade set was therefore not the strategy that is deployed. This run rebuilds
the signal to mirror the shipped `Open15Core` exactly and re-prices from there.

Production parity (all imported, none reimplemented):
  - day config  : services.open15_breakout_service.resolve_day_config
  - baseline    : 09:15 EXCLUDED, mirroring Open15Core._roll_minutes (#502)
  - selection   : top-3 gainers LONG / top-3 losers SHORT by 09:15 open / prev
                  settled daily close - 1
  - entry window: 09:16..no_entry_after (09:29); exit at exit_time (09:30)
  - daily cap   : max_trades, first-come by trigger minute (mirrors _enter)
  - stock costs : mis_round_trip_charges
  - option costs: option_round_trip_charges  + an explicit bid-ask sweep
  - contract    : ATM strike from the entry price, current-month (last-Tuesday)

Option premiums are Black-Scholes (bs.py) with prior-day bhavcopy ATM IV and the
R58 fallback chain — real premiums are impossible beyond ~1 month because expired
contracts are purged from the master contract. BS was validated against real
premiums at corr 0.89 / sign-agreement 92% / RMSE 3.0pp with a ~1pp/trade
conservative bias (R58 issue #424), so it is a directional instrument: trust the
sign and the ordering, not the third significant figure.

Bid-ask is the dominant unknown and is NOT a measurement — results are reported
across a sweep (0 / 0.5 / 1 / 2 % per side) so the sensitivity is visible rather
than buried in one assumed number.

Usage (needs OpenAlgo stopped — historify.duckdb is exclusively locked):
    BT_FROM=2025-08-01 BT_TO=2026-07-31 PYTHONPATH=. \
    uv run python backtest/options_open15/year_502_run.py
"""

from __future__ import annotations

import datetime
import json
import os
import statistics as st

import duckdb

from backtest.options_open15 import bs
from backtest.options_open15.run_bs_backtest import (
    _D,
    T_years,
    current_month_expiry,
    load_all,
    load_contract_meta,
    load_iv_history,
    monthly_expiries,
    prev_close,
    resolve_iv,
    rv60_asof,
)
from services.open15_breakout_service import mis_round_trip_charges, resolve_day_config
from services.open15_option_shadow import option_round_trip_charges

HIST_DB = "db/historify.duckdb"
R = 0.065
DATE_FROM = os.getenv("BT_FROM", "2025-08-01")
DATE_TO = os.getenv("BT_TO", "2026-07-31")
SPREADS = [0.0, 0.005, 0.01, 0.02]
OUT = os.getenv("BT_OUT", "")

CFG = resolve_day_config(
    {
        "margin_per_slot": float(os.getenv("BT_MARGIN_PER_SLOT", 60000)),
        "vol_mult": 1.5,
        "max_trades": int(os.getenv("BT_MAX_TRADES", 3)),
        "instrument": "atm_option",
    },
    0.0,
)
VOL_MULT, MAX_TRADES = CFG["vol_mult"], CFG["max_trades"]
NOTIONAL, SLOT = CFG["notional"], CFG["margin_effective"]
CAPITAL = SLOT * MAX_TRADES
TOP_N = int(os.getenv("BT_TOP_N", 3))
F, ENTRY_TO, EXIT_MIN = 555, 569, 570


def scan_trigger(bars, side):
    """Bar-level mirror of the shipped Open15Core trigger.

    Baseline = mean of COMPLETED minute volumes from 09:16 on. The 09:15 minute
    is excluded exactly as `_roll_minutes` now does (#502) — that is the single
    behavioural difference from the superseded `run_bs_backtest.find_signal`.
    """
    bymin = {b[0]: b for b in bars}
    b0 = bymin.get(F)
    if b0 is None:
        return None
    h1, l1 = b0[2], b0[3]
    prior = []
    for m in range(F + 1, ENTRY_TO + 1):
        b = bymin.get(m)
        if b is None:
            prior.append(0)
            continue
        base = (sum(prior) / len(prior)) if prior else 0.0
        broke = (b[2] > h1) if side == "L" else (b[3] < l1)
        if broke and base > 0 and (b[5] or 0) / base >= VOL_MULT:
            return m, bymin, (h1 if side == "L" else l1)
        prior.append(b[5] or 0)
    return None


def px_at(bymin, minute):
    if minute in bymin:
        return bymin[minute][1]
    return bymin[minute - 1][4] if (minute - 1) in bymin else None


def run():
    con = duckdb.connect(HIST_DB, read_only=True)
    meta = load_contract_meta()
    universe = sorted(meta)
    byday_iv, ratios = load_iv_history()
    expiries = monthly_expiries(DATE_FROM, DATE_TO)
    sess, dailies = load_all(con, universe, DATE_FROM, DATE_TO)
    con.close()
    days = sorted({d for s in universe for d in sess[s]})
    print(f"universe {len(universe)}   trading days {len(days)}   {DATE_FROM}..{DATE_TO}")

    iv_src = {"bhavcopy_side": 0, "bhavcopy_mean": 0, "stock_ratio": 0, "global": 0}
    trades = []
    for day in days:
        dstr = _D(day).isoformat()
        cand = []
        for s in universe:
            bars = sorted(sess[s].get(day, []))
            if not bars:
                continue
            bymin = {b[0]: b for b in bars}
            if F not in bymin:
                continue
            pc = prev_close(dailies.get(s, []), day)
            if not pc:
                continue
            cand.append((bymin[F][1] / pc - 1, s, bars))
        if len(cand) < 50:
            continue
        cand.sort(key=lambda x: x[0])
        picks = [(c, "S") for c in cand[:TOP_N] if c[0] < 0]
        picks += [(c, "L") for c in cand[-TOP_N:] if c[0] > 0]

        fired = []
        for (gap, s, bars), side in picks:
            hit = scan_trigger(bars, side)
            if not hit:
                continue
            m, bymin, level = hit
            en = px_at(bymin, m + 1)  # production-legal fill: next-minute open
            ex = px_at(bymin, EXIT_MIN)
            if not en or not ex:
                continue
            fired.append(
                {
                    "date": dstr,
                    "sym": s,
                    "side": side,
                    "gap": gap * 100,
                    "trig": m,
                    "level": level,
                    "entry": en,
                    "exit": ex,
                }
            )
        fired.sort(key=lambda x: (x["trig"], -abs(x["gap"])))
        fired = fired[:MAX_TRADES]  # production daily cap, first-come

        for t in fired:
            s, side, en, ex = t["sym"], t["side"], t["entry"], t["exit"]
            sgn = 1 if side == "L" else -1
            # ---- stock leg (production sizing + charge model) ----
            qty = max(int(NOTIONAL / en), 1)
            gross = sgn * qty * (ex - en)
            buy_v = qty * (en if side == "L" else ex)
            sell_v = qty * (ex if side == "L" else en)
            ch = mis_round_trip_charges(buy_v, sell_v) or 0.0
            t.update(
                ret_pct=sgn * (ex / en - 1) * 100,
                stk_qty=qty,
                stk_charges=round(ch, 2),
                stk_net=round(gross - ch, 2),
            )
            # ---- option leg (BS, ATM from the ENTRY price as production does) ----
            sig_date = datetime.date.fromisoformat(dstr)
            exp = current_month_expiry(sig_date, expiries)
            if not exp:
                continue
            step, lot = meta[s]["step"], meta[s]["lot"]
            K = round(en / step) * step
            iv = resolve_iv(
                byday_iv, ratios, s, sig_date, side == "L", rv60_asof(dailies[s], day), iv_src
            )
            if not iv:
                continue
            pen = bs.bs_price(en, K, T_years(dstr, t["trig"], exp), R, iv, side == "L")
            pex = bs.bs_price(ex, K, T_years(dstr, EXIT_MIN, exp), R, iv, side == "L")
            if pen <= 0.05:
                continue
            lots = int(SLOT // (pen * lot))  # production fit-to-capital sizing
            if lots < 1:
                t["opt_err"] = "unaffordable"
                continue
            q = lots * lot
            t.update(
                opt_K=K,
                opt_lot=lot,
                opt_lots=lots,
                opt_iv=round(iv * 100, 1),
                opt_en=round(pen, 2),
                opt_ex=round(pex, 2),
                opt_qty=q,
                opt_dte=(exp - sig_date).days,
            )
            for sp in SPREADS:
                b_, s_ = pen * (1 + sp), pex * (1 - sp)
                c_ = option_round_trip_charges(b_ * q, s_ * q) or 0.0
                t[f"opt_net_{sp}"] = round((s_ - b_) * q - c_, 2)
        trades.extend(fired)
    print(f"IV sources: {iv_src}")
    return trades


def block(tr, label, key):
    v = [r for r in tr if r.get(key) is not None]
    if not v:
        return f"  {label:>7}: n=0"
    n = [r[key] for r in v]
    w = sum(1 for x in n if x > 0)
    return (
        f"  {label:>7}: n={len(v):>4d}  win {w / len(v) * 100:>4.1f}%  "
        f"total Rs{sum(n):>+11,.0f}  mean Rs{st.mean(n):>+8,.0f}  median Rs{st.median(n):>+7,.0f}"
    )


if __name__ == "__main__":
    tr = run()
    L = [t for t in tr if t["side"] == "L"]
    S = [t for t in tr if t["side"] == "S"]
    print(f"\n{'=' * 92}\nPAST YEAR ON PRODUCTION RULES — {DATE_FROM}..{DATE_TO}")
    print(
        f"slot Rs{SLOT:,.0f} x max_trades {MAX_TRADES} = Rs{CAPITAL:,.0f} capital, "
        f"top_n {TOP_N}, vol_mult {VOL_MULT}\n{'=' * 92}"
    )
    print(
        f"signals: {len(tr)}  (long {len(L)} / short {len(S)})  "
        f"over {len({t['date'] for t in tr})} trading days\n"
    )
    print("STOCK LEG (production notional + charge model)")
    for lab, g in (("ALL", tr), ("LONG", L), ("SHORT", S)):
        print(block(g, lab, "stk_net"))
    for sp in SPREADS:
        print(f"\nOPTION LEG — bid-ask {sp * 100:.1f}%/side")
        for lab, g in (("ALL", tr), ("LONG", L), ("SHORT", S)):
            print(block(g, lab, f"opt_net_{sp}"))
    if OUT:
        json.dump(tr, open(OUT, "w"), indent=1)
        print(f"\nper-trade dump -> {OUT}")
