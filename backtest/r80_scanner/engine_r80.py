"""R80 step 2b/3: run the scanner signals through the REAL SimplifiedStockEngine, 1m-driven exits.

Entries: real engine on completed 5m candles (elapsed 1.0). After a symbol is armed, every 1m bar is a
price update: stop checked first against the bar extreme (gap -> bar open), then on_price_update(close)
(trail / global lock / EOD). Exit (a) = engine. Exits (b) stop-or-15:20 and (c) fixed 2R are simulated on
the SAME entries from the 1m path. Charges: Zerodha MIS, legs by side, exchange 0.00307% (#783).
Also records per-trade features for the pre-registered criteria. -> data/trades_r80{suffix}.parquet

  uv run python backtest/r80_scanner/engine_r80.py [--cap5]
"""
import argparse
import datetime as dt
import logging
import sys
from collections import defaultdict
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
logging.disable(logging.WARNING)
from services.simplified_stock_engine_core import (  # noqa: E402
    DIRECTION_BUY,
    Candle,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
)

logging.disable(logging.INFO)
DB = str(HERE / "data" / "bars_r80.duckdb")
CFG = SimplifiedEngineConfig(
    account_capital=20000.0, account_leverage=5.0, max_risk_per_trade=500.0, max_trades_per_day=6,
    no_new_openings_time=dt.time(15, 10), eod_exit_time=dt.time(15, 20), atr_period=14, atr_sl_mult=1.5,
    atr_entry_min_mult=0.5, volume_multiplier=2.5, trail_atr_mult=0.5, sl_confirm_seconds=3.0,
    enable_global_profit_lock=True, mode="disabled")
EOD = dt.time(15, 20)
SIGL = []


def charges_orders(orders):
    """orders: list of (is_buy, value). Zerodha MIS per-order brokerage cap, exchange 0.00307% (#783)."""
    bv = sum(v for b, v in orders if b)
    sv = sum(v for b, v in orders if not b)
    t = bv + sv
    brk = sum(round(min(20.0, 0.0003 * v), 2) for _, v in orders if v > 0)
    c = dict(brk=brk, stt=0.00025 * sv, exch=0.0000307 * t, sebi=0.000001 * t, stamp=0.00003 * bv)
    c["gst"] = 0.18 * (brk + c["exch"] + c["sebi"])
    c["total"] = sum(c.values())
    c["turnover"] = t
    return c


def book(prefix, is_long, entry, q, exits):
    """exits: list of (qty, price). Returns gross/charges/net + components with prefix."""
    sgn = 1 if is_long else -1
    g = sum(sgn * (p - entry) * n for n, p in exits)
    orders = [(is_long, entry * q)] + [(not is_long, p * n) for n, p in exits if n > 0]
    c = charges_orders(orders)
    out = {f"gross_{prefix}": g, f"chg_{prefix}": c["total"], f"net_{prefix}": g - c["total"],
           f"turn_{prefix}": c["turnover"]}
    out.update({f"{k}_{prefix}": c[k] for k in ("brk", "stt", "exch", "sebi", "gst", "stamp")})
    return out


def to5(m):
    g = m.groupby(m.ts.dt.floor("5min")).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                              close=("close", "last"), volume=("volume", "sum")).reset_index()
    return g


def run_day(con, day, sigs, daily, nifty):
    syms = sorted(sigs.symbol.unique())
    lo = day - dt.timedelta(days=10)
    m = con.execute("select symbol,ts,open,high,low,close,volume from prices_1m where symbol in "
                    f"({','.join('?' * len(syms))}) and ts >= ? and ts < ? order by symbol,ts",
                    [*syms, lo, day + dt.timedelta(days=1)]).df()
    m = m[(m.ts.dt.time >= dt.time(9, 15)) & (m.ts.dt.time <= dt.time(15, 29))]
    sim = {"t": dt.datetime.combine(day, dt.time(9, 15))}
    eng = SimplifiedStockEngine(config=CFG, now_provider=lambda: sim["t"])
    per = {}
    events = []  # (ts, order, kind, sym, payload)
    for s in sigs.itertuples():
        mm = m[m.symbol == s.symbol]
        if mm.empty:
            continue
        fire = pd.Timestamp(s.fire_ts).to_pydatetime()
        c5 = to5(mm)
        hist = [Candle(r.ts.to_pydatetime(), r.open, r.high, r.low, r.close, int(r.volume), 1.0)
                for r in c5.itertuples() if r.ts.to_pydatetime() + dt.timedelta(minutes=5) <= fire]
        today1 = mm[mm.ts.dt.date == day]
        per[s.symbol] = dict(dir=s.direction, fire=fire, m1=today1.reset_index(drop=True), c5=c5,
                             fire_price=float(s.fire_price))
        events.append((fire, 0, "arm", s.symbol, hist))
        for r in today1.itertuples():
            end = r.ts.to_pydatetime() + dt.timedelta(minutes=1)
            if end > fire:
                events.append((end, 1, "m1", s.symbol, r))
        for r in c5[c5.ts.dt.date == day].itertuples():
            end = r.ts.to_pydatetime() + dt.timedelta(minutes=5)
            if end > fire:
                events.append((end, 2, "c5", s.symbol,
                               Candle(r.ts.to_pydatetime(), r.open, r.high, r.low, r.close, int(r.volume), 1.0)))
    events.sort(key=lambda e: (e[0], e[1], e[3]))
    entries = {}
    armed = set()
    for ts, _, kind, sym, p in events:
        if ts.time() > EOD:
            continue
        sim["t"] = ts
        if kind == "arm":
            (eng.activate_buy_symbol if per[sym]["dir"] == DIRECTION_BUY else eng.activate_sell_symbol)(sym)
            if p:
                eng.load_historical_candles(sym, p)
            armed.add(sym)
            per[sym]["atr_arm"] = float(eng._atr_map.get(sym) or 0)
            continue
        if sym not in armed:
            continue
        if kind == "m1":
            pos = eng.positions.get(sym)
            if pos is not None and sym not in eng.pending_exits:
                if pos.qty > 0 and p.low <= pos.stop_loss:
                    px = min(p.open, pos.stop_loss)
                    for ex in eng.on_price_update(sym, px):
                        eng.confirm_exit(ex.symbol, ex.reference_price, ex.reason)
                elif pos.qty < 0 and p.high >= pos.stop_loss:
                    px = max(p.open, pos.stop_loss)
                    for ex in eng.on_price_update(sym, px):
                        eng.confirm_exit(ex.symbol, ex.reference_price, ex.reason)
            for ex in eng.on_price_update(sym, float(p.close)):
                eng.confirm_exit(ex.symbol, ex.reference_price, ex.reason)
        else:  # completed 5m candle -> entry decision at its close
            sim["t"] = ts - dt.timedelta(seconds=1)
            e = eng.on_new_candle(sym, p)
            if e:
                pos = eng.confirm_entry(sym, e.reference_price)
                if pos:
                    entries[(sym, pos.entry_time)] = dict(
                        risk=pos.risk_per_share, stop0=pos.stop_loss, qty=abs(pos.qty), entry=pos.entry_price,
                        atr5=float(eng._atr_map.get(sym) or 0), cvol=p.volume, ctime=ts)
    sim["t"] = dt.datetime.combine(day, EOD)
    for ex in eng.check_eod_exits():
        eng.confirm_exit(ex.symbol, ex.reference_price, "eod")
    out = []
    for t in eng.completed_trades:
        info = entries.get((t.symbol, t.entry_time))
        if info is None:
            continue
        is_long = t.qty > 0
        P = per[t.symbol]
        path = P["m1"][P["m1"].ts >= pd.Timestamp(info["ctime"])]
        path = path[path.ts.dt.time < EOD]
        rec = dict(day=day.isoformat(), symbol=t.symbol, side="long" if is_long else "short",
                   fire_ts=P["fire"], entry_ts=info["ctime"], qty=info["qty"], entry=info["entry"],
                   risk=info["risk"], stop0=info["stop0"], exit_a=t.exit_price, reason_a=t.exit_reason,
                   exit_a_ts=t.exit_time)
        rec.update(book("a", is_long, info["entry"], info["qty"], [(info["qty"], t.exit_price)]))
        rec.update(sim_exits(path, is_long, info))
        rec.update(giveback(path, is_long, info, t.exit_price, t.exit_time))
        rec.update(features(t.symbol, day, info, is_long, P, daily, nifty))
        out.append(rec)
    for sym, P in per.items():
        R = 1.5 * P.get("atr_arm", 0)
        if R <= 0:
            continue
        is_long = P["dir"] == DIRECTION_BUY
        path = P["m1"][(P["m1"].ts >= pd.Timestamp(P["fire"])) & (P["m1"].ts.dt.time < EOD)]
        SIGL.append(dict(day=day.isoformat(), symbol=sym, side="long" if is_long else "short",
                         entered=any(r["symbol"] == sym for r in out),
                         **ladder(path, is_long, P["fire_price"], R, P["fire"])))
    return out


def ladder(path, is_long, e, R, t0):
    sgn = 1 if is_long else -1
    stop = e - sgn * R
    o, h, lo, c = path.open.values, path.high.values, path.low.values, path.close.values
    ts = path.ts.values
    reach = {}
    stop_i = None
    mfe = 0.0
    for i in range(len(c)):
        if (is_long and lo[i] <= stop) or (not is_long and h[i] >= stop):
            stop_i = i
            break
        mfe = max(mfe, sgn * ((h[i] if is_long else lo[i]) - e) / R)
        for k in (0.5, 1.0, 1.5, 2.0):
            if k not in reach and mfe >= k:
                reach[k] = (pd.Timestamp(ts[i]) + pd.Timedelta(minutes=1) - pd.Timestamp(t0)).total_seconds() / 60
    out = dict(stopped=stop_i is not None, mfe_R=mfe)
    for k in (0.5, 1.0, 1.5, 2.0):
        kk = str(k).replace(".", "_")
        out[f"r{kk}_before_stop"] = k in reach
        out[f"t{kk}_min"] = reach.get(k, np.nan)
        out[f"rev{kk}"] = (k in reach) and stop_i is not None
    return out


def hybrid(path, is_long, e, R, Q, pk=1.0, tk=1.0):
    sgn = 1 if is_long else -1
    stop = e - sgn * R
    q1 = Q // 2
    left = Q
    exits = []
    done = False
    best = e
    o, h, lo, c = path.open.values, path.high.values, path.low.values, path.close.values
    for i in range(len(c)):
        if (is_long and lo[i] <= stop) or (not is_long and h[i] >= stop):
            exits.append((left, min(o[i], stop) if is_long else max(o[i], stop)))
            left = 0
            break
        if not done and sgn * ((h[i] if is_long else lo[i]) - e) >= pk * R:
            done = True
            if q1 > 0:
                exits.append((q1, e + sgn * pk * R))
                left -= q1
        if done:
            best = max(best, c[i]) if is_long else min(best, c[i])
            cand = best - sgn * tk * R
            stop = max(stop, e, cand) if is_long else min(stop, e, cand)
    if left > 0:
        exits.append((left, c[-1] if len(c) else e))
    return exits


def sim_exits(path, is_long, info):
    e, R, q = info["entry"], info["risk"], info["qty"]
    sgn = 1 if is_long else -1
    stop = e - sgn * R
    tgt = e + sgn * 2 * R
    o, h, lo, c = path.open.values, path.high.values, path.low.values, path.close.values
    xb = xc = None
    for i in range(len(c)):
        if (is_long and lo[i] <= stop) or (not is_long and h[i] >= stop):
            px = min(o[i], stop) if is_long else max(o[i], stop)
            xb = px if xb is None else xb
            xc = px if xc is None else xc
            break
        if xc is None and ((is_long and h[i] >= tgt) or (not is_long and lo[i] <= tgt)):
            xc = tgt
    last = c[-1] if len(c) else e
    xb = last if xb is None else xb
    xc = last if xc is None else xc
    res = {}
    res.update(book("b", is_long, e, q, [(q, xb)]))
    res.update(book("c", is_long, e, q, [(q, xc)]))
    for tag, pk in (("h", 1.0), ("h075", 0.75), ("h125", 1.25)):
        res.update(book(tag, is_long, e, q, hybrid(path, is_long, e, R, q, pk, 1.0)))
    res.update(ladder(path, is_long, e, R, info["ctime"]))
    return res


def giveback(path, is_long, info, exit_px, exit_ts):
    e, R = info["entry"], info["risk"]
    sgn = 1 if is_long else -1
    p = path[path.ts + pd.Timedelta(minutes=1) <= pd.Timestamp(exit_ts)]
    peak = max(0.0, float((sgn * ((p.high if is_long else p.low) - e) / R).max())) if len(p) else 0.0
    real = sgn * (exit_px - e) / R
    return dict(peak_R_a=peak, real_R_a=real, giveback_R_a=peak - real)


def features(sym, day, info, is_long, P, daily, nifty):
    sgn = 1 if is_long else -1
    e, R = info["entry"], info["risk"]
    d = daily.get(sym)
    f = {}
    if d is not None:
        prev = d[d.bar_date < pd.Timestamp(day)]
        p5 = prev.tail(5)
        f["swing5_hi"], f["swing5_lo"] = p5.high.max(), p5.low.min()
        p3 = prev.tail(3)
        f["swing3_hi"], f["swing3_lo"] = p3.high.max(), p3.low.min()
        tr = pd.concat([prev.high - prev.low, (prev.high - prev.close.shift()).abs(),
                        (prev.low - prev.close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
        f["datr14"] = float(atr.iloc[-1]) if len(atr) else np.nan
        f["pdh"], f["pdl"], f["pdc"] = prev.high.iloc[-1], prev.low.iloc[-1], prev.close.iloc[-1]
    today = P["m1"]
    f["day_open"] = float(today.open.iloc[0]) if len(today) else np.nan
    c5 = P["c5"]
    b = c5[(c5.ts.dt.date == day) & (c5.ts < pd.Timestamp(info["ctime"]))]
    hb = c5[c5.ts < pd.Timestamp(info["ctime"])].tail(8)
    for n in (4, 6, 8):
        w = hb.tail(n)
        if len(w) == n:
            A, B = w.iloc[: n // 2], w.iloc[n // 2:]
            f[f"hh{n}"] = bool(B.high.max() > A.high.max())
            f[f"hl{n}"] = bool(B.low.min() > A.low.min())
            f[f"lh{n}"] = bool(B.high.max() < A.high.max())
            f[f"ll{n}"] = bool(B.low.min() < A.low.min())
    f["n5_today_before"] = len(b)
    nf = nifty[(nifty.ts.dt.date == day)]
    if len(nf):
        o = nf.open.iloc[0]
        upto = nf[nf.ts + pd.Timedelta(minutes=1) <= pd.Timestamp(info["ctime"])]
        f["nifty_ret"] = float(upto.close.iloc[-1] / o - 1) if len(upto) else np.nan
    f["ext_atr"] = sgn * (e - f["day_open"]) / f["datr14"] if f.get("datr14") else np.nan
    f["R_pct"] = R / e
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap5", action="store_true")
    a = ap.parse_args()
    sig = pd.read_parquet(HERE / "data" / "signals_r80.parquet")
    sig["fire_ts"] = pd.to_datetime(sig.fire_ts)
    if a.cap5:
        sig = sig.sort_values("fire_ts").groupby("day").head(5)
    con = duckdb.connect(DB, read_only=True)
    dl = con.execute("select * from prices_daily order by symbol,bar_date").df()
    dl["bar_date"] = pd.to_datetime(dl.bar_date)
    daily = {s: g.reset_index(drop=True) for s, g in dl.groupby("symbol")}
    nifty = con.execute("select ts,open,close from prices_1m where symbol='__NIFTY' order by ts").df()
    rows = []
    days = sorted(sig.day.unique())
    for i, d in enumerate(days, 1):
        rows += run_day(con, dt.date.fromisoformat(d), sig[sig.day == d], daily, nifty)
        if i % 40 == 0:
            print(f"{i}/{len(days)} trades={len(rows)}", flush=True)
    df = pd.DataFrame(rows)
    out = HERE / "data" / f"trades_r80{'_cap5' if a.cap5 else ''}.parquet"
    df.to_parquet(out, index=False)
    pd.DataFrame(SIGL).to_parquet(HERE / "data" / f"signal_ladder_r80{'_cap5' if a.cap5 else ''}.parquet", index=False)
    print("DONE", len(df), out, flush=True)
    for k in ("a", "b", "c", "h"):
        print(k, df.groupby("side")[f"net_{k}"].agg(["count", "mean", "sum"]).round(1).to_dict())


if __name__ == "__main__":
    main()
