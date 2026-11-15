"""R81: run pre-registered entries through the UNCHANGED SimplifiedStockEngine (mode=disabled).

Per day one engine (shared caps / profit lock). Entry: engine ATR from its own load_historical_candles over the
prior ~7 days of 5m bars up to the signal bar; risk = max(1.5*ATR, Rs1); qty = engine.calculate_qty; EntrySignal
-> confirm_entry at the signal-bar close. Then every 1m bar: stop checked first against the bar extreme (gap ->
bar open), then on_price_update(close); every completed 5m bar updates the engine ATR via on_new_candle (symbol not
armed, so it can never open a second entry). EOD 15:20. Fills: 2.4 bps adverse per side on entry and exit
(accounting only; the engine's own decisions use unslipped prices); zero-slip numbers from the same run.
Secondary (reported, never used to choose): initial stop else hold to the 15:19 close.
  uv run python backtest/r81_entry/run_engine.py <period> [cand1,cand2,...]
"""
import datetime as dt
import logging
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
logging.disable(logging.INFO)
import common as C  # noqa: E402
from services.simplified_stock_engine_core import (  # noqa: E402
    DIRECTION_BUY, DIRECTION_SELL, Candle, EntrySignal, SimplifiedEngineConfig, SimplifiedStockEngine)

CFG = SimplifiedEngineConfig(
    account_capital=20000.0, account_leverage=5.0, max_risk_per_trade=500.0, max_trades_per_day=6,
    no_new_openings_time=dt.time(15, 10), eod_exit_time=dt.time(15, 20), atr_period=14, atr_sl_mult=1.5,
    atr_entry_min_mult=0.5, volume_multiplier=2.5, trail_atr_mult=0.5, sl_confirm_seconds=3.0,
    enable_global_profit_lock=True, mode="disabled")
EOD = dt.time(15, 20)


def to5(m):
    g = m.groupby(m.ts.dt.floor("5min")).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                              close=("close", "last"), volume=("volume", "sum"), n=("open", "size")).reset_index()
    g["end"] = g.ts + pd.Timedelta(minutes=5)
    return g


def candle(r):
    return Candle(pd.Timestamp(r.ts).to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close),
                  int(r.volume or 0), 1.0)


def book(dirn, q, e, x, slip):
    ef, xf = e * (1 + dirn * slip), x * (1 - dirn * slip)
    g = dirn * (xf - ef) * q
    bv, sv = (ef * q, xf * q) if dirn > 0 else (xf * q, ef * q)
    c = C.charges_orders([(True, bv), (False, sv)])
    return g, c


def side_sim(path, dirn, e, R):
    """stop-or-15:19 close + R ladder on the 1m path after entry."""
    stop = e - dirn * R
    o, h, lo, c = path.open.values, path.high.values, path.low.values, path.close.values
    ts = path.ts.values
    x, stopped, mfe, reach = None, False, 0.0, {}
    for i in range(len(c)):
        if (dirn > 0 and lo[i] <= stop) or (dirn < 0 and h[i] >= stop):
            x = min(o[i], stop) if dirn > 0 else max(o[i], stop)
            stopped = True
            break
        mfe = max(mfe, dirn * ((h[i] if dirn > 0 else lo[i]) - e) / R)
        for k in (0.5, 1.0, 1.5, 2.0):
            if k not in reach and mfe >= k:
                reach[k] = i
    if x is None:
        x = c[-1] if len(c) else e
    return x, stopped, mfe, reach


def prepare(entries):
    """Group by symbol, load 1m once; returns per-entry dict of hist 5m candles, today 1m path, today 5m closes."""
    prep = {}
    for sym, g in entries.groupby("symbol"):
        a, b = g.day.min() - dt.timedelta(days=12), g.day.max()
        m = C.load_1m(sym, a, b)
        if m.empty:
            continue
        m["day"] = m.ts.dt.date
        m5 = to5(m)
        m5["day"] = m5.ts.dt.date
        days = sorted(m.day.unique())
        for r in g.itertuples():
            if r.day not in days:
                continue
            i = days.index(r.day)
            d0 = days[max(0, i - 7)]
            t = pd.Timestamp(r.time)
            hist = m5[(m5.day >= d0) & (m5.end <= t)]
            p1 = m[(m.day == r.day) & (m.ts >= t) & (m.ts.dt.time < EOD)]
            p5 = m5[(m5.day == r.day) & (m5.end > t) & (m5.end.dt.time <= EOD)]
            prep[r.Index] = (hist, p1.reset_index(drop=True), p5.reset_index(drop=True))
    return prep


def run(entries):
    entries = entries.reset_index(drop=True)
    prep = prepare(entries)
    rows = []
    for (cand_v, day), g in entries.groupby(["variant", "day"]):
        sim = {"t": dt.datetime.combine(day, dt.time(9, 15))}
        eng = SimplifiedStockEngine(config=CFG, now_provider=lambda: sim["t"])
        ev = []
        for r in g.itertuples():
            if r.Index not in prep:
                continue
            hist, p1, p5 = prep[r.Index]
            ev.append((pd.Timestamp(r.time).to_pydatetime(), 0, r.Index, None))
            for b in p1.itertuples():
                ev.append((pd.Timestamp(b.ts).to_pydatetime() + dt.timedelta(minutes=1), 1, r.Index, b))
            for b in p5.itertuples():
                ev.append((pd.Timestamp(b.end).to_pydatetime(), 2, r.Index, b))
        ev.sort(key=lambda e: (e[0], e[1], e[2]))
        info = {}
        for ts, kind, k, b in ev:
            r = entries.loc[k]
            sym = r.symbol
            sim["t"] = ts
            if kind == 0:
                if ts.time() >= CFG.no_new_openings_time:
                    continue
                eng._reset_trade_day_if_needed()
                if eng.trades_today >= CFG.max_trades_per_day or sym in eng.positions:
                    continue
                hist = prep[k][0]
                eng.load_historical_candles(sym, [candle(x) for x in hist.itertuples()])
                atr = float(eng._atr_map.get(sym) or 0.0)
                if atr <= 0:
                    continue
                risk = max(atr * CFG.atr_sl_mult, CFG.min_risk_per_share)
                qty, *_ = eng.calculate_qty(float(r.ref), risk)
                if qty <= 0:
                    continue
                act = DIRECTION_BUY if r.side > 0 else DIRECTION_SELL
                eng.pending_entries[sym] = EntrySignal(sym, act, int(qty), float(r.ref),
                                                       round(float(r.ref) - r.side * risk, 2), float(risk),
                                                       pd.Timestamp(r.time).to_pydatetime(), "NSE", "MIS", "MARKET")
                pos = eng.confirm_entry(sym, float(r.ref))
                if pos:
                    info[sym] = dict(k=k, risk=risk, qty=int(qty))
                continue
            if sym not in info:
                continue
            if kind == 1:
                pos = eng.positions.get(sym)
                if pos is not None and sym not in eng.pending_exits:
                    if pos.qty > 0 and b.low <= pos.stop_loss:
                        for ex in eng.on_price_update(sym, min(b.open, pos.stop_loss)):
                            eng.confirm_exit(ex.symbol, ex.reference_price, ex.reason)
                    elif pos.qty < 0 and b.high >= pos.stop_loss:
                        for ex in eng.on_price_update(sym, max(b.open, pos.stop_loss)):
                            eng.confirm_exit(ex.symbol, ex.reference_price, ex.reason)
                if sym in eng.positions:
                    for ex in eng.on_price_update(sym, float(b.close)):
                        eng.confirm_exit(ex.symbol, ex.reference_price, ex.reason)
            elif kind == 2 and sym in eng.positions:
                sim["t"] = ts - dt.timedelta(seconds=1)
                eng.on_new_candle(sym, Candle(pd.Timestamp(b.ts).to_pydatetime(), float(b.open), float(b.high),
                                              float(b.low), float(b.close), int(b.volume or 0), 1.0))
        sim["t"] = dt.datetime.combine(day, EOD)
        for ex in eng.check_eod_exits():
            eng.confirm_exit(ex.symbol, ex.reference_price, "eod")
        for t in eng.completed_trades:
            if t.symbol not in info:
                continue
            it = info[t.symbol]
            r = entries.loc[it["k"]]
            q, dirn, e = it["qty"], int(r.side), float(r.ref)
            g1, c1 = book(dirn, q, e, t.exit_price, C.SLIP)
            g0, c0 = book(dirn, q, e, t.exit_price, 0.0)
            p1 = prep[it["k"]][1]
            xb, stopped, mfe, reach = side_sim(p1, dirn, e, it["risk"])
            gb, cb = book(dirn, q, e, xb, C.SLIP)
            rec = dict(variant=cand_v, cand=r.cand, day=day, time=r.time, symbol=t.symbol, side=dirn, ref=e,
                       qty=q, risk=it["risk"], notional=q * e, exit=t.exit_price, reason=t.exit_reason,
                       exit_time=t.exit_time, gross=g1, charges=c1["total"], net=g1 - c1["total"],
                       gross0=g0, net0=g0 - c0["total"], turnover=c1["turnover"],
                       net_b=gb - cb["total"], stopped=stopped, mfe_R=mfe)
            rec.update({f"chg_{x}": c1[x] for x in ("brk", "stt", "exch", "sebi", "stamp", "gst")})
            rec.update({f"r{str(x).replace('.', '_')}": x in reach for x in (0.5, 1.0, 1.5, 2.0)})
            rows.append(rec)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    period = sys.argv[1]
    E = pd.read_parquet(C.D / f"entries_{period}.parquet")
    cands = sys.argv[2].split(",") if len(sys.argv) > 2 else sorted(E.cand.unique())
    for cd in cands:
        out = C.D / f"trades_{cd}_{period}.parquet"
        if out.exists():
            print("cached", out.name, flush=True)
            continue
        T = run(E[E.cand == cd])
        T.to_parquet(out, index=False)
        print(cd, period, len(T), "trades", T.groupby("variant").net.mean().round(1).to_dict() if len(T) else {}, flush=True)
