"""R82 harness: the UNCHANGED SimplifiedStockEngine (mode=disabled) under the pre-registered sizing rules (Phase 2)
and execution variants (Phase 4). Sizing and execution live ONLY here (engine.calculate_qty is replaced on the
instance; fills are decided by the harness and passed to confirm_entry). See PREREGISTRATION.md.

  uv run python backtest/r82_sizing/run_r82.py <BASE|E3S> <period> <cfg1,cfg2,...|grid> [procs]
  -> data/trades_<cand>_<period>_<cfg>.parquet   (filled trades)
     data/signals_<cand>_<period>_<cfg>.parquet  (every entry signal: filled or missed, with its sizing inputs)
cfg = <sizing>.<exec>, e.g. A500.XL5
"""

import datetime as dt
import json
import logging
import math
import sys
from multiprocessing import Pool
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "r81_entry"))
sys.path.insert(0, str(HERE.parents[1]))
logging.disable(logging.WARNING)
import common as C  # noqa: E402

from services.simplified_stock_engine_core import (  # noqa: E402
    DIRECTION_BUY,
    DIRECTION_SELL,
    Candle,
    EntrySignal,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
)

logging.disable(logging.INFO)
D = HERE / "data"
FWD_DB = D / "bars_fwd.duckdb"
FWD_LO = dt.date(2025, 6, 20)
EOD = dt.time(15, 20)
BP = 100_000.0
SLIP = 0.00024
PERIODS = {
    "BASE": {
        "train": (dt.date(2024, 1, 1), dt.date(2024, 12, 31)),
        "validate": (dt.date(2025, 1, 1), dt.date(2025, 6, 19)),
        "holdout": (dt.date(2025, 6, 20), dt.date(2026, 7, 6)),
    },
    "E3S": {
        "train": C.TRAIN,
        "validate": C.VALID,
        "is3": C.HOLD,
        "holdout": (FWD_LO, dt.date(2026, 11, 13)),
    },
}
SIZINGS = ["S0", "S0L", "A500", "A750", "A1000", "APCT", "A500_nomin", "A750_nomin", "A1000_nomin"]
EXECS = ["X0", "X1", "XL2", "XL5", "XL10", "XLi5", "XS"]


def base_cfg(budget):
    return SimplifiedEngineConfig(
        account_capital=20000.0,
        account_leverage=5.0,
        max_risk_per_trade=float(budget),
        max_trades_per_day=6,
        no_new_openings_time=dt.time(15, 10),
        eod_exit_time=EOD,
        atr_period=14,
        atr_sl_mult=1.5,
        atr_entry_min_mult=0.5,
        volume_multiplier=2.5,
        trail_atr_mult=0.5,
        sl_confirm_seconds=3.0,
        enable_global_profit_lock=True,
        mode="disabled",
    )


# ---------------------------------------------------------------- data
_fwd = None


def _fcon():
    global _fwd
    if _fwd is None:
        _fwd = duckdb.connect(str(FWD_DB), read_only=True)
    return _fwd


def load_1m(sym, a, b):
    parts = []
    if a < FWD_LO:
        m = C.load_1m(sym, a, min(b, FWD_LO - dt.timedelta(days=1)))
        parts.append(m)
    if b >= FWD_LO or (a < FWD_LO and (not parts or parts[0].empty)):
        lo = a if (not parts or parts[0].empty) else FWD_LO
        m = (
            _fcon()
            .execute(
                "select ts,open,high,low,close,volume from m1 where symbol=? and ts>=? and ts<? order by ts",
                [
                    sym,
                    dt.datetime.combine(lo, dt.time()),
                    dt.datetime.combine(b + dt.timedelta(days=1), dt.time()),
                ],
            )
            .df()
        )
        t = m.ts.dt.time
        parts.append(m[(t >= dt.time(9, 15)) & (t <= dt.time(15, 29))])
    parts = [p for p in parts if len(p)]
    if not parts:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    return (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates("ts")
        .sort_values("ts")
        .reset_index(drop=True)
    )


def to5(m):
    g = (
        m.groupby(m.ts.dt.floor("5min"))
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
    )
    g["end"] = g.ts + pd.Timedelta(minutes=5)
    return g


def candle(r):
    return Candle(
        pd.Timestamp(r.ts).to_pydatetime(),
        float(r.open),
        float(r.high),
        float(r.low),
        float(r.close),
        int(r.volume or 0),
        1.0,
    )


# ---------------------------------------------------------------- sizing
def rt_charges(v):
    return C.charges_orders([(True, v), (False, v)])["total"]


def size(rule, P, R, BL, k_gap, budget):
    """returns (qty, q_budget, used_ratio, skip_reason)."""
    if rule == "S0":
        return int(min(BP // P, 500 // R)), None, None, None
    if rule == "S0L":
        q = int(min(max(BL, 0) // P, 500 // R))
        return (q, None, None, None) if q >= 1 else (0, None, None, "bp")
    B = budget
    per = R + 2 * SLIP * P + k_gap * R
    q = int(B // per)
    while q > 0 and q * per + rt_charges(q * P) > B:
        q -= 1
    qB = q
    if qB < 1:
        return 0, 0, None, "budget"
    qc = int(min(qB, max(BL, 0) // P))
    used = (qc * per + rt_charges(qc * P)) / B if qc > 0 else 0.0
    if qc < 1:
        return 0, qB, used, "bp"
    if not rule.endswith("_nomin") and qc < math.ceil(0.5 * qB):
        return 0, qB, used, "min_viable"
    return qc, qB, used, None


def rule_budget(rule):
    if rule.startswith("A") and rule != "APCT":
        return float(rule[1:].split("_")[0])
    return 500.0


# ---------------------------------------------------------------- one day, one config
def run_day(cand, day, day_data, cfg, k_gap, equity=None, excluded=frozenset()):
    rule, exe = cfg.split(".")
    budget = rule_budget(rule)
    if rule == "APCT":
        if equity is None or equity <= 0:
            return [], []
        budget = 0.025 * equity
    sim = {"t": dt.datetime.combine(day, dt.time(9, 15))}
    eng = SimplifiedStockEngine(
        config=base_cfg(budget if rule.startswith("A") and rule != "APCT" else 500.0),
        now_provider=lambda: sim["t"],
    )
    orders = {}  # sym -> dict(kind, L, expire, signal, sig_rec)
    sigrecs, fills = [], {}
    wait = {"XL2": 2, "XL5": 5, "XL10": 10, "XLi5": 5}.get(exe)

    def reserved():
        r = sum(abs(p.qty) * p.entry_price for p in eng.positions.values())
        return r + sum(o["signal"].quantity * o["signal"].reference_price for o in orders.values())

    def sizer(price, risk, sym, ts):
        q, qB, used, why = size(rule, price, risk, BP - reserved(), k_gap, budget)
        sigrecs.append(
            {
                "day": day,
                "symbol": sym,
                "signal_ts": ts,
                "ref": price,
                "risk": risk,
                "q": q,
                "q_budget": qB,
                "used_ratio": used,
                "skip": why,
                "bl": BP - reserved(),
                "budget": budget,
                "filled": False,
            }
        )
        return q

    def place(sym, sig, ts):
        rec = sigrecs[-1]
        rec["side"] = 1 if sig.action == DIRECTION_BUY else -1
        if exe in ("X0", "XS"):
            pos = eng.confirm_entry(sym, sig.reference_price)
            if pos:
                rec["filled"] = True
                fills[sym] = {"rec": rec, "fill": sig.reference_price, "eslip": SLIP, "fill_ts": ts}
            return
        if exe == "X1":
            orders[sym] = {"kind": "next", "signal": sig, "rec": rec, "start": ts}
            return
        side = rec["side"]
        L = sig.reference_price * (1 - side * 0.0005) if exe == "XLi5" else sig.reference_price
        orders[sym] = {
            "kind": "limit",
            "L": L,
            "expire": ts + dt.timedelta(minutes=wait),
            "signal": sig,
            "rec": rec,
            "start": ts,
        }

    def on_bar(sym, b, end):
        """1m bar b (label b.ts, ends at `end`). Fill pending order first, then stop check, then price update."""
        o = orders.get(sym)
        if o is not None and pd.Timestamp(b.ts).to_pydatetime() >= o["start"]:
            sig = o["signal"]
            if o["kind"] == "next":
                orders.pop(sym)
                eng.pending_entries[sym] = sig
                sim["t"] = pd.Timestamp(b.ts).to_pydatetime()
                if eng.confirm_entry(sym, float(b.open)):
                    o["rec"]["filled"] = True
                    fills[sym] = {
                        "rec": o["rec"],
                        "fill": float(b.open),
                        "eslip": SLIP,
                        "fill_ts": sim["t"],
                    }
                sim["t"] = end
            else:
                side = o["rec"]["side"]
                hit = (b.low < o["L"]) if side > 0 else (b.high > o["L"])
                if hit:
                    orders.pop(sym)
                    eng.pending_entries[sym] = sig
                    if eng.confirm_entry(sym, float(o["L"])):
                        o["rec"]["filled"] = True
                        fills[sym] = {
                            "rec": o["rec"],
                            "fill": float(o["L"]),
                            "eslip": 0.0,
                            "fill_ts": end,
                        }
                elif end >= o["expire"]:
                    orders.pop(sym)
                    eng.clear_pending_entry(sym)
        pos = eng.positions.get(sym)
        if pos is not None and sym not in eng.pending_exits:
            if pos.qty > 0 and b.low <= pos.stop_loss:
                for ex in eng.on_price_update(
                    sym,
                    min(b.open, pos.stop_loss)
                    if sym in fills and fills[sym]["fill_ts"] < end
                    else pos.stop_loss,
                ):
                    eng.confirm_exit(ex.symbol, ex.reference_price, ex.reason)
            elif pos.qty < 0 and b.high >= pos.stop_loss:
                for ex in eng.on_price_update(
                    sym,
                    max(b.open, pos.stop_loss)
                    if sym in fills and fills[sym]["fill_ts"] < end
                    else pos.stop_loss,
                ):
                    eng.confirm_exit(ex.symbol, ex.reference_price, ex.reason)
        if sym in eng.positions:
            for ex in eng.on_price_update(sym, float(b.close)):
                eng.confirm_exit(ex.symbol, ex.reference_price, ex.reason)

    # replace sizing on the INSTANCE (engine code untouched)
    cur = {}
    eng.calculate_qty = lambda price, risk: (sizer(price, risk, cur["sym"], cur["ts"]), 0.0, 0, 0)

    events = []
    for sym, dd in day_data["syms"].items():
        if exe == "XS" and sym in excluded:
            continue
        for k, payload in dd["events"]:
            events.append((payload[0], payload[1], k, sym, payload[2]))
    events.sort(key=lambda e: (e[0], e[1], e[3]))
    armed = set()
    for ts, _, kind, sym, p in events:
        if ts.time() > EOD:
            continue
        sim["t"] = ts
        dd = day_data["syms"][sym]
        if kind == "arm":
            (eng.activate_buy_symbol if dd["dir"] == DIRECTION_BUY else eng.activate_sell_symbol)(
                sym
            )
            if p:
                eng.load_historical_candles(sym, p)
            armed.add(sym)
        elif kind == "inject":
            if ts.time() >= eng.config.no_new_openings_time:
                continue
            eng._reset_trade_day_if_needed()
            if (
                eng.trades_today >= eng.config.max_trades_per_day
                or sym in eng.positions
                or sym in orders
            ):
                continue
            eng.load_historical_candles(sym, p["hist"])
            armed.add(sym)
            atr = float(eng._atr_map.get(sym) or 0.0)
            if atr <= 0:
                continue
            risk = max(atr * 1.5, eng.config.min_risk_per_share)
            cur.update(sym=sym, ts=ts)
            q = eng.calculate_qty(p["ref"], risk)[0]
            if q <= 0:
                continue
            act = DIRECTION_BUY if p["side"] > 0 else DIRECTION_SELL
            sig = EntrySignal(
                sym,
                act,
                int(q),
                float(p["ref"]),
                round(float(p["ref"]) - p["side"] * risk, 2),
                float(risk),
                ts,
                "NSE",
                "MIS",
                "MARKET",
            )
            eng.pending_entries[sym] = (
                sig  # stays pending (blocks re-entry, live-like) until filled/cancelled
            )
            place(sym, sig, ts)
        elif sym in armed:
            if kind == "m1":
                on_bar(sym, p, ts)
            elif kind == "c5":
                sim["t"] = ts - dt.timedelta(seconds=1)
                cur.update(sym=sym, ts=ts)
                if cand == "BASE":
                    e = eng.on_new_candle(sym, p)
                    if e:
                        place(sym, e, ts)
                elif sym in eng.positions:
                    eng.on_new_candle(sym, p)
                sim["t"] = ts
    sim["t"] = dt.datetime.combine(day, EOD)
    for ex in eng.check_eod_exits():
        eng.confirm_exit(ex.symbol, ex.reference_price, "eod")
    out = []
    for t in eng.completed_trades:
        f = fills.get(t.symbol)
        if f is None:
            continue
        rec = f["rec"]
        dirn = 1 if t.qty > 0 else -1
        q = abs(t.qty)
        e, x = f["fill"], t.exit_price
        ef, xf = e * (1 + dirn * f["eslip"]), x * (1 - dirn * SLIP)
        bv, sv = (ef * q, xf * q) if dirn > 0 else (xf * q, ef * q)
        c = C.charges_orders([(True, bv), (False, sv)])
        g = dirn * (xf - ef) * q
        g0 = dirn * (x - e) * q
        stop0 = e - dirn * rec["risk"]
        out.append(
            {
                "cand": cand,
                "cfg": cfg,
                "day": day,
                "symbol": t.symbol,
                "side": dirn,
                "signal_ts": rec["signal_ts"],
                "ref": rec["ref"],
                "fill": e,
                "fill_ts": f["fill_ts"],
                "qty": q,
                "risk": rec["risk"],
                "notional": q * e,
                "exit": x,
                "exit_time": t.exit_time,
                "reason": t.exit_reason,
                "gross": g,
                "gross_noslip": g0,
                "charges": c["total"],
                "net": g - c["total"],
                "slip_rs": g0 - g,
                "budget": rec["budget"],
                "used_ratio": rec["used_ratio"],
                "gap_R": max(0.0, dirn * (stop0 - x)) / rec["risk"]
                if dirn * (x - stop0) <= 0
                else np.nan,
            }
        )
    return out, sigrecs


# ---------------------------------------------------------------- day data
def day_data_base(day, sigs):
    syms = sorted(sigs.symbol.unique())
    D_ = {"syms": {}}
    for s in sigs.itertuples():
        m = load_1m(s.symbol, day - dt.timedelta(days=10), day)
        if m.empty or s.symbol in D_["syms"]:
            continue
        fire = pd.Timestamp(s.fire_ts).to_pydatetime()
        c5 = to5(m)
        hist = [
            candle(r)
            for r in c5.itertuples()
            if r.ts.to_pydatetime() + dt.timedelta(minutes=5) <= fire
        ]
        today = m[m.ts.dt.date == day]
        ev = [("arm", (fire, 0, hist))]
        for r in today.itertuples():
            end = r.ts.to_pydatetime() + dt.timedelta(minutes=1)
            if end > fire:
                ev.append(("m1", (end, 1, r)))
        for r in c5[c5.ts.dt.date == day].itertuples():
            end = r.ts.to_pydatetime() + dt.timedelta(minutes=5)
            if end > fire:
                ev.append(("c5", (end, 2, candle(r))))
        D_["syms"][s.symbol] = {"dir": s.direction, "events": ev}
    D_["n_sig_syms"] = len(syms)
    return D_


def day_data_e3(day, ents):
    D_ = {"syms": {}}
    for r in ents.itertuples():
        t = pd.Timestamp(r.time).to_pydatetime()
        m = load_1m(r.symbol, day - dt.timedelta(days=12), day)
        if m.empty:
            continue
        m["day"] = m.ts.dt.date
        days = sorted(m.day.unique())
        if day not in days:
            continue
        d0 = days[max(0, days.index(day) - 7)]
        c5 = to5(m)
        hist = [candle(x) for x in c5[(c5.ts.dt.date >= d0) & (c5.end <= t)].itertuples()]
        today = m[(m.day == day) & (m.ts >= t) & (m.ts.dt.time < EOD)]
        ev = [("inject", (t, 0, {"hist": hist, "ref": float(r.ref), "side": int(r.side)}))]
        for x in today.itertuples():
            ev.append(("m1", (x.ts.to_pydatetime() + dt.timedelta(minutes=1), 1, x)))
        for x in c5[(c5.ts.dt.date == day) & (c5.end > t) & (c5.end.dt.time <= EOD)].itertuples():
            ev.append(("c5", (x.end.to_pydatetime(), 2, candle(x))))
        D_["syms"][r.symbol] = {"dir": None, "events": ev}
    return D_


def signals_for(cand, period, variant="E3"):
    lo, hi = PERIODS[cand][period]
    if cand == "BASE":
        f = HERE.parents[0] / (
            "r80_scanner/data/signals_r80.parquet"
            if period != "holdout"
            else "inhouse_scanner/signals.parquet"
        )
        s = pd.read_parquet(f)
        s["day"] = pd.to_datetime(s.day).dt.date
        s["fire_ts"] = pd.to_datetime(s.fire_ts)
        return s[(s.day >= lo) & (s.day <= hi)]
    if period == "holdout":
        e = pd.read_parquet(D / "entries_e3_fwd.parquet")
    else:
        e = pd.read_parquet(C.D / f"entries_{'holdout' if period == 'is3' else period}.parquet")
    e = e[(e.variant == variant) & (e.side < 0)]
    return e[(e.day >= lo) & (e.day <= hi)]


G = {}


def _init(cand, k_gap, excluded):
    G.update(cand=cand, k_gap=k_gap, excluded=excluded)


def work(args):
    day, sub, cfgs = args
    dd = day_data_base(day, sub) if G["cand"] == "BASE" else day_data_e3(day, sub)
    T, S = [], []
    for cfg in cfgs:
        t, s = run_day(G["cand"], day, dd, cfg, G["k_gap"], excluded=G["excluded"])
        T += t
        S += [dict(x, cfg=cfg) for x in s]
    return T, S


def run_apct(cand, period, execs, k_gap, excluded, sig):
    T, S = [], []
    for exe in execs:
        cfg = f"APCT.{exe}"
        eq = 20000.0
        ruin = None
        for day, sub in sig.groupby("day"):
            if eq <= 0:
                ruin = ruin or day
                break
            dd = day_data_base(day, sub) if cand == "BASE" else day_data_e3(day, sub)
            t, s = run_day(cand, day, dd, cfg, k_gap, equity=eq, excluded=excluded)
            eq += sum(x["net"] for x in t)
            for x in t:
                x["equity_after_day"] = eq
            T += t
            S += [dict(x, cfg=cfg) for x in s]
        json.dump(
            {
                "cfg": cfg,
                "period": period,
                "final_equity": eq,
                "ruin_day": str(ruin) if ruin else None,
            },
            open(D / f"apct_{cand}_{period}_{exe}.json", "w"),
        )
    return T, S


def main(cand, period, cfgs, procs=6, tag=None, variant="E3"):
    sig = signals_for(cand, period, variant)
    kg_path = D / f"kgap_{cand}.json"
    k_gap = json.load(open(kg_path))["k_gap"] if kg_path.exists() else 0.0
    ex_path = D / f"spread_excluded_{cand}.json"
    excluded = frozenset(json.load(open(ex_path))["excluded"]) if ex_path.exists() else frozenset()
    if (
        any(c.startswith("A") and not c.startswith("APCT") or c.startswith("APCT") for c in cfgs)
        and not kg_path.exists()
    ):
        raise SystemExit("k_gap not frozen yet: run S0.X0 on train and freeze_kgap.py first")
    if any(c.endswith(".XS") for c in cfgs) and not ex_path.exists():
        raise SystemExit("spread exclusion list not frozen yet: run spread_ar.py first")
    par = [c for c in cfgs if not c.startswith("APCT")]
    apct = [c.split(".")[1] for c in cfgs if c.startswith("APCT")]
    T, S = [], []
    if par:
        jobs = [(d, g, par) for d, g in sig.groupby("day")]
        with Pool(procs, initializer=_init, initargs=(cand, k_gap, excluded)) as p:
            for i, (t, s) in enumerate(p.imap_unordered(work, jobs, chunksize=2), 1):
                T += t
                S += s
                if i % 50 == 0:
                    print(f"{cand} {period} {i}/{len(jobs)} trades={len(T)}", flush=True)
    if apct:
        t, s = run_apct(cand, period, apct, k_gap, excluded, sig)
        T += t
        S += s
    TT, SS = pd.DataFrame(T), pd.DataFrame(S)
    tag = tag or "grid"
    TT.to_parquet(D / f"trades_{cand}_{period}_{tag}.parquet", index=False)
    SS.to_parquet(D / f"signals_{cand}_{period}_{tag}.parquet", index=False)
    if len(TT):
        print(
            TT.groupby("cfg")
            .agg(
                n=("net", "size"),
                net=("net", "mean"),
                gross=("gross", "mean"),
                notional=("notional", "mean"),
            )
            .round(1)
            .to_string(),
            flush=True,
        )


if __name__ == "__main__":
    cand, period, spec = sys.argv[1], sys.argv[2], sys.argv[3]
    procs = int(sys.argv[4]) if len(sys.argv) > 4 else 6
    tag = sys.argv[5] if len(sys.argv) > 5 else None
    variant = sys.argv[6] if len(sys.argv) > 6 else "E3"
    if spec == "grid":
        cfgs = [
            f"{s}.{e}" for s in SIZINGS for e in EXECS if not (s.endswith("_nomin") and e != "X0")
        ]
    else:
        cfgs = spec.split(",")
    main(cand, period, cfgs, procs, tag, variant)
