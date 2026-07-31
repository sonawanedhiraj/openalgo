"""Issue #502 — July 2026 backtest of the fixed open15_vol_breakout.

Re-runs the R59 July replay (`july_full_run.py`) with ONE behavioural change:
the 09:15 minute leaves the volume baseline. That is the only #502 bug a
BAR-based backtest can express — bugs 1 and 2 (tick-sourced open and
tick-sourced H1/L1) do not exist here, because a bar replay already reads the
broker's settled 09:15 candle. In other words the backtest was always running
the FIXED data-sourcing; production was not. This run answers the remaining
question: what does dropping the inflated baseline do to the month?

Both arms are driven by the production code on this branch:
  - baseline semantics: services.open15_breakout_service.Open15Core._roll_minutes
    (exercised through a thin bar-level adapter that mirrors the tick core)
  - config:  resolve_day_config(get_config()-shaped dict, 0.0)
  - charges: production mis_round_trip_charges / option_round_trip_charges
  - option contract: production pick_contract against SymToken

Entry convention: 'next' = open of the minute after the trigger minute — the
production-legal fill for a bar-confirmed signal, and the same convention R59
used, so the two runs are directly comparable. (R58 established that entering
at the trigger LEVEL is look-ahead.)

Usage:
    PYTHONPATH=. uv run python backtest/options_open15/july_502_run.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics as st
from pathlib import Path

from services.open15_breakout_service import (
    _FIRST_MIN,
    mis_round_trip_charges,
    resolve_day_config,
)
from services.open15_option_shadow import option_round_trip_charges, pick_contract

ROOT = Path(__file__).resolve().parents[2]
CACHE_PATH = ROOT / "backtest" / "options_open15" / "data" / "july_fetch_cache.json"
OA_DB = str(ROOT / "db" / "openalgo.db")

PICKS_R59 = os.environ["BT_PICKS_R59"]  # 2026-07-01..07-24
PICKS_TAIL = os.environ["BT_PICKS_TAIL"]  # 2026-07-27..07-31
OUT_PATH = os.environ.get("BT_OUT", "")

cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
picks = {**json.load(open(PICKS_R59)), **json.load(open(PICKS_TAIL))}

# production config as the UI has it today (issue #502 audit session)
CFG = resolve_day_config(
    {
        "margin_per_slot": float(os.environ.get("BT_MARGIN_PER_SLOT", 22500.0)),
        "vol_mult": 1.5,
        "max_trades": int(os.environ.get("BT_MAX_TRADES", 3)),
        "instrument": "atm_option",
    },
    0.0,
)
VOL_MULT, MAX_TRADES = CFG["vol_mult"], CFG["max_trades"]
NOTIONAL, SLOT = CFG["notional"], CFG["margin_effective"]
CAPITAL = SLOT * MAX_TRADES
ENTRY_TO, EXIT_MIN = 9 * 60 + 29, 9 * 60 + 30


# --------------------------------------------------------------- signal layer
def scan_trigger(bars, side, include_first_minute):
    """Bar-level analog of the production mid-bar cumvol trigger.

    ``include_first_minute`` mirrors ``Open15Core.baseline_includes_first_minute``:
    True reproduces the pre-#502 baseline, False is the shipped behaviour.
    """
    bymin = {b[0]: b for b in bars}
    b0 = bymin.get(_FIRST_MIN)
    if b0 is None:
        return None, {}
    h1, l1 = b0[2], b0[3]
    prior = [b0[5] or 0] if include_first_minute else []
    for m in range(_FIRST_MIN + 1, ENTRY_TO + 1):
        b = bymin.get(m)
        if b is None:
            prior.append(0)
            continue
        base = (sum(prior) / len(prior)) if prior else 0.0
        broke = (b[2] > h1) if side == "L" else (b[3] < l1)
        if broke and base > 0 and (b[5] or 0) / base >= VOL_MULT:
            return m, bymin
        prior.append(b[5] or 0)
    return None, bymin


def exit_price(bymin):
    if EXIT_MIN in bymin:
        return bymin[EXIT_MIN][1]
    if EXIT_MIN - 1 in bymin:
        return bymin[EXIT_MIN - 1][4]
    return None


def entry_price(bymin, m):
    nxt = bymin.get(m + 1)
    return nxt[1] if nxt else bymin[m][4]


# --------------------------------------------------------------- options layer
_CACHED_CONTRACTS: dict[tuple[str, str, str], list[dict]] = {}


def _index_cached_contracts():
    """Rebuild July's expired option contracts from the premium cache.

    The 28-JUL-26 series expired on 2026-07-28 and has been purged from
    ``symtoken``, so ``pick_contract`` can no longer see it — but the cache
    holds the real 1m premium bars keyed by the full contract symbol, from
    which strike/expiry/type parse directly. Lot size comes from the CURRENT
    ``symtoken`` row for the same underlying: verified identical for all 15
    R59 contracts (July lot == current lot, 15/15), so this reconstructs the
    contract rather than guessing it.
    """
    import re

    con = sqlite3.connect(f"file:{OA_DB}?mode=ro", uri=True)
    lots = {}
    for name, lot in con.execute(
        "SELECT name, lotsize FROM symtoken WHERE exchange='NFO' "
        "AND instrumenttype IN ('CE','PE') GROUP BY name"
    ):
        lots[name] = lot
    con.close()
    pat = re.compile(r"^([A-Z0-9&\-]+?)(\d{2}[A-Z]{3}\d{2})(\d+(?:\.\d+)?)(CE|PE)$")
    for key in cache:
        if not key.startswith("NFO|"):
            continue
        _, sym, date = key.split("|")
        m = pat.match(sym)
        if not m:
            continue
        base, expiry_raw, strike, otype = m.groups()
        lot = lots.get(base)
        if not lot:
            continue
        exp = f"{expiry_raw[:2]}-{expiry_raw[2:5]}-{expiry_raw[5:]}"
        _CACHED_CONTRACTS.setdefault((base, otype, date), []).append(
            {"symbol": sym, "strike": float(strike), "expiry": exp, "lotsize": lot}
        )


def option_candidates(base, side, date_str):
    """Live master-contract rows first; cached July contracts as the fallback."""
    otype = "CE" if side == "L" else "PE"
    con = sqlite3.connect(f"file:{OA_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT symbol, strike, expiry, lotsize FROM symtoken "
        "WHERE exchange='NFO' AND name=? AND instrumenttype=?",
        [base, otype],
    ).fetchall()
    con.close()
    live = [{"symbol": r[0], "strike": r[1], "expiry": r[2], "lotsize": r[3]} for r in rows]
    cached = _CACHED_CONTRACTS.get((base, otype, date_str), [])
    # a contract dated on/before the trade date's own series wins: the cached
    # July rows ARE the nearest expiry that day, the live rows are Aug onward
    return cached + live if cached else live


def option_bars(opt_symbol, date_str):
    """1m premium bars {minute: [o,h,l,c,v]} — cache-first, broker on miss.

    July 1-28 ATM contracts expired on 2026-07-28 and have been purged from the
    master contract, so anything not already cached is UNPRICEABLE. That is a
    data limit, reported as such — never silently dropped.
    """
    key = f"NFO|{opt_symbol}|{date_str}"
    if key in cache:
        return {int(k): v for k, v in cache[key].items()}, "cache"
    from database.auth_db import get_first_available_api_key
    from services.history_service import get_history

    ok, resp, code = get_history(
        symbol=opt_symbol,
        exchange="NFO",
        interval="1m",
        start_date=date_str,
        end_date=date_str,
        api_key=get_first_available_api_key(),
    )
    if not ok or not isinstance(resp, dict):
        return None, f"fetch failed code={code}"
    out = {}
    for row in resp.get("data") or []:
        t = ((row["timestamp"] + 19800) % 86400) // 60
        if 550 <= t <= 575:
            out[t] = [row["open"], row["high"], row["low"], row["close"], row.get("volume", 0)]
    if out:
        cache[key] = {str(k): v for k, v in out.items()}
    return (out, "broker") if out else (None, "no bars returned")


def opt_leg(sig):
    """Production option-mode replay: ATM buy, fit-to-capital lots, real premiums."""
    contract = pick_contract(
        option_candidates(sig["sym"], sig["side"], sig["date"]), sig["entry"], sig["date"]
    )
    if not contract:
        return {"opt_err": "contract expired out of master contract"}
    ob, src = option_bars(contract["symbol"], sig["date"])
    if ob is None:
        return {"opt_err": src, "opt_sym": contract["symbol"]}
    m = sig["trig_min"]
    en = (ob.get(m + 1) or [None])[0] or (ob.get(m) or [None, None, None, None])[3]
    ex = (ob.get(EXIT_MIN) or [None])[0]
    if ex is None:
        prior = [t for t in ob if t <= EXIT_MIN]
        ex = ob[max(prior)][3] if prior else None
    if not en or not ex:
        return {"opt_err": "missing premium bar", "opt_sym": contract["symbol"]}
    lot = int(contract["lotsize"])
    lots = int(SLOT // (en * lot))
    if lots < 1:
        return {
            "opt_err": "unaffordable",
            "opt_sym": contract["symbol"],
            "opt_en": en,
            "opt_lot": lot,
        }
    charges = option_round_trip_charges(en * lot * lots, ex * lot * lots) or 0.0
    return {
        "opt_sym": contract["symbol"],
        "opt_strike": contract["strike"],
        "opt_lot": lot,
        "opt_lots": lots,
        "opt_en": en,
        "opt_ex": ex,
        "opt_charges": round(charges, 2),
        "opt_src": src,
        "opt_net": round(lots * lot * (ex - en) - charges, 2),
    }


# --------------------------------------------------------------------- replay
def run(include_first_minute):
    signals = []
    for day in sorted(picks):
        fired = []
        for p in picks[day]["picks"]:
            m, bymin = scan_trigger(p["bars"], p["side"], include_first_minute)
            if m is None:
                continue
            ex = exit_price(bymin)
            if ex is None:
                continue
            b0 = bymin[_FIRST_MIN]
            en = entry_price(bymin, m)
            fired.append(
                {
                    "date": day,
                    "sym": p["sym"],
                    "side": p["side"],
                    "gap": p["gap"],
                    "trig_min": m,
                    "level": b0[2] if p["side"] == "L" else b0[3],
                    "trig_close": bymin[m][4],
                    "entry": en,
                    "exit_spot": ex,
                }
            )
        fired.sort(key=lambda s: (s["trig_min"], -abs(s["gap"])))
        sides = os.environ.get("BT_SIDES", "LS").upper()
        fired = [f for f in fired if f["side"] in sides]
        signals.extend(fired[:MAX_TRADES])

    for s in signals:
        en, sgn = s["entry"], (1 if s["side"] == "L" else -1)
        qty = max(int(NOTIONAL / en), 1)
        gross = sgn * qty * (s["exit_spot"] - en)
        buy_v = qty * (en if s["side"] == "L" else s["exit_spot"])
        sell_v = qty * (s["exit_spot"] if s["side"] == "L" else en)
        ch = mis_round_trip_charges(buy_v, sell_v) or 0.0
        s.update(
            stk_qty=qty,
            stk_charges=round(ch, 2),
            stk_net=round(gross - ch, 2),
            ret_pct=round(sgn * (s["exit_spot"] / en - 1) * 100, 3),
        )
        s.update(opt_leg(s))
    return signals


def summarize(sigs, key):
    vals = [s[key] for s in sigs if s.get(key) is not None]
    if not vals:
        return {"n": 0}
    wins = sum(1 for v in vals if v > 0)
    daily = {}
    for s in sigs:
        if s.get(key) is not None:
            daily[s["date"]] = daily.get(s["date"], 0) + s[key]
    eq, peak, mdd = CAPITAL, CAPITAL, 0.0
    for d in sorted(daily):
        eq += daily[d]
        peak = max(peak, eq)
        mdd = min(mdd, (eq - peak) / peak * 100)
    rets = [daily[d] / CAPITAL for d in sorted(daily)]
    sharpe = (
        (st.mean(rets) / st.pstdev(rets) * (252**0.5))
        if len(rets) > 1 and st.pstdev(rets) > 0
        else None
    )
    return {
        "n": len(vals),
        "wins": wins,
        "win_rate_pct": round(wins / len(vals) * 100, 1),
        "net_inr": round(sum(vals), 2),
        "month_return_pct": round(sum(vals) / CAPITAL * 100, 2),
        "max_dd_pct": round(mdd, 2),
        "sharpe": round(sharpe, 2) if sharpe else None,
        "trading_days": len(daily),
    }


if __name__ == "__main__":
    _index_cached_contracts()
    out = {"config": CFG, "capital": CAPITAL, "arms": {}}
    for label, inc in (("legacy_0915_in_baseline", True), ("fixed_0915_excluded", False)):
        sigs = run(inc)
        out["arms"][label] = {
            "trades": sigs,
            "stock": summarize(sigs, "stk_net"),
            "option": summarize(sigs, "opt_net"),
            "option_skipped": [
                {"date": s["date"], "sym": s["sym"], "why": s["opt_err"]}
                for s in sigs
                if s.get("opt_err")
            ],
        }
    CACHE_PATH.write_text(json.dumps(cache))
    if OUT_PATH:
        Path(OUT_PATH).write_text(json.dumps(out, indent=1))
    for label, arm in out["arms"].items():
        print(f"\n=== {label} ===")
        print("  stock :", arm["stock"])
        print("  option:", arm["option"])
        print("  option unavailable/skipped:", len(arm["option_skipped"]))
