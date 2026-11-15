"""R81 feature pass: inputs only (no outcomes). Per symbol -> data/features/<sym>.parquet (one row per day) and
data/events/<sym>.parquet (E1/E4 first-trigger events per pre-registered variant). Index features -> data/index_features.parquet.

Resolutions of spec ambiguity (recorded in the report):
- daily ATR14 = Wilder on settled daily bars <= D-1; ATR% = ATR / prev close; ADV20 = mean(close*volume) D-20..D-1.
- E1 VWAP = cumulative 1m typical price*volume from 09:15; 'share of 5m closes on the trade side since 09:30' counts
  completed 5m bars starting >= 09:30 up to AND including the trigger bar; trigger window = bars ENDING 10:15..14:30.
- E4 first-hour range = 1m bars 09:15..10:14; 20-day norms need 20 prior sessions (min_periods=20); slot volume
  norm = mean of the same 5m slot over the prior 20 sessions; trigger window = bars ENDING 10:20..14:00;
  one trigger per symbol-day (earliest side).
- E5 path efficiency over the sequence [open 09:15, closes 09:15..09:44] (30 steps).
  uv run python backtest/r81_entry/features.py [N_PROCS]
"""
import datetime as dt
import os
import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

FEAT = C.D / "features"
EVT = C.D / "events"
E1_VARIANTS = {"E1": (1.0, 0.8), "E1_t0.75": (0.75, 0.8), "E1_t1.25": (1.25, 0.8), "E1_s0.7": (1.0, 0.7), "E1_s0.9": (1.0, 0.9)}
E4_VARIANTS = {"E4": (0.6, 1.5), "E4_c0.5": (0.5, 1.5), "E4_c0.7": (0.7, 1.5), "E4_v1.25": (0.6, 1.25), "E4_v1.75": (0.6, 1.75)}


def daily_features(d):
    d = d.sort_values("bar_date").reset_index(drop=True)
    pc = d.close.shift()
    tr = pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    f = pd.DataFrame({"day": pd.to_datetime(d.bar_date).dt.date, "prev_close": d.close.shift(),
                      "atr14": atr.shift(), "adv20": (d.close * d.volume).rolling(20, min_periods=20).mean().shift()})
    f["atr_pct"] = f.atr14 / f.prev_close
    return f


def day_block(g):
    """g: one day's 1m bars. returns dict of day features + 5m frame."""
    g = g.reset_index(drop=True)
    tm = g.ts.dt.strftime("%H:%M").values
    idx = {t: i for i, t in enumerate(tm)}
    f = {"n1m": len(g)}
    if "09:15" not in idx:
        return f, None
    o = float(g.open.iloc[0])
    f["open"] = o
    for k in ("09:24", "09:29", "09:34", "09:44"):
        f["c" + k.replace(":", "")] = float(g.close.iloc[idx[k]]) if k in idx else np.nan
    first30 = g[(tm >= "09:15") & (tm <= "09:44")]
    f["n_first30"] = len(first30)
    seq = np.r_[o, first30.close.values]
    f["path_sum"] = float(np.abs(np.diff(seq)).sum())
    fh = g[(tm >= "09:15") & (tm <= "10:14")]
    f["fh_high"], f["fh_low"] = (float(fh.high.max()), float(fh.low.min())) if len(fh) >= 50 else (np.nan, np.nan)
    tp = (g.high + g.low + g.close) / 3
    g = g.assign(cpv=(tp * g.volume).cumsum(), cv=g.volume.cumsum(), b5=g.ts.dt.floor("5min"))
    b = g.groupby("b5").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                            volume=("volume", "sum"), cpv=("cpv", "last"), cv=("cv", "last"), n=("open", "size")).reset_index()
    b["vwap"] = b.cpv / b.cv.replace(0, np.nan)
    b["end"] = b.b5 + pd.Timedelta(minutes=5)
    return f, b


def symbol_pass(sym):
    out_f, out_e = FEAT / f"{sym}.parquet", EVT / f"{sym}.parquet"
    if out_f.exists() and out_e.exists():
        return sym, "cached"
    dl = C.load_daily(sym)
    if dl.empty:
        return sym, "no daily"
    df = daily_features(dl).set_index("day")
    m = C.load_1m(sym, dt.date(2018, 12, 1), C.HOLD[1])
    if m.empty:
        return sym, "no 1m"
    m["day"] = m.ts.dt.date
    rows, evs, fhr, slotv = [], [], {}, {}
    days = sorted(m.day.unique())
    for day, g in m.groupby("day", sort=True):
        f, b = day_block(g)
        f["day"] = day
        rows.append(f)
        if b is None:
            continue
        fhr[day] = f["fh_high"] - f["fh_low"]
        slotv[day] = dict(zip(b.b5.dt.strftime("%H:%M"), b.volume))
        if day not in df.index or pd.isna(df.loc[day, "atr_pct"]):
            continue
        pc, ap = df.loc[day, "prev_close"], df.loc[day, "atr_pct"]
        # ---- E1 events
        b2 = b[b.b5.dt.strftime("%H:%M") >= "09:30"].copy()
        b2["above"] = (b2.close > b2.vwap).cumsum() / np.arange(1, len(b2) + 1)
        b2["below"] = (b2.close < b2.vwap).cumsum() / np.arange(1, len(b2) + 1)
        b2["ret"] = b2.close / pc - 1
        et = b2.end.dt.strftime("%H:%M")
        w = b2[(et >= "10:15") & (et <= "14:30") & (b2.n == 5)]
        for vn, (k, s) in E1_VARIANTS.items():
            L = w[(w.ret >= k * ap) & (w.above >= s) & (w.low <= w.vwap * 1.001) & (w.close > w.vwap)]
            S = w[(w.ret <= -k * ap) & (w.below >= s) & (w.high >= w.vwap * 0.999) & (w.close < w.vwap)]
            cand = []
            if len(L):
                r = L.iloc[0]; cand.append((r.end, 1, r.close, r.ret / ap))
            if len(S):
                r = S.iloc[0]; cand.append((r.end, -1, r.close, -r.ret / ap))
            if cand:
                e = min(cand, key=lambda x: x[0])
                evs.append(dict(variant=vn, day=day, time=e[0], side=e[1], ref=float(e[2]), score=float(e[3])))
        # ---- E4 events (norms filled below, after the loop, because they need prior days)
        evs.append(dict(variant="_E4bars", day=day, time=None, side=0, ref=np.nan, score=np.nan,
                        _bars=b[["b5", "end", "close", "volume", "n"]].to_dict("list")))
    F = pd.DataFrame(rows).set_index("day").join(df, how="left")
    # E4: compression & slot-volume norms from the prior 20 sessions
    fh_series = pd.Series(fhr).sort_index()
    fh_norm = fh_series.rolling(20, min_periods=20).mean().shift()
    F["fh_norm"] = fh_norm.reindex(F.index)
    slot_df = pd.DataFrame(slotv).T.sort_index()
    slot_norm = slot_df.rolling(20, min_periods=20).mean().shift()
    final_ev = []
    for e in evs:
        if e["variant"] != "_E4bars":
            final_ev.append(e)
            continue
        day = e["day"]
        if day not in F.index or pd.isna(F.loc[day, "fh_norm"]) or pd.isna(F.loc[day, "fh_high"]):
            continue
        hi, lo, fn = F.loc[day, "fh_high"], F.loc[day, "fh_low"], F.loc[day, "fh_norm"]
        b = pd.DataFrame(e["_bars"])
        b["slot"] = pd.to_datetime(b.b5).dt.strftime("%H:%M")
        et = pd.to_datetime(b.end).dt.strftime("%H:%M")
        b = b[(et >= "10:20") & (et <= "14:00") & (b.n == 5)]
        if day not in slot_norm.index:
            continue
        sn = slot_norm.loc[day]
        b["vnorm"] = b.slot.map(sn)
        for vn, (cc, mm) in E4_VARIANTS.items():
            if (hi - lo) > cc * fn:
                continue
            hit = b[((b.close > hi) | (b.close < lo)) & (b.volume >= mm * b.vnorm)]
            if len(hit):
                r = hit.iloc[0]
                final_ev.append(dict(variant=vn, day=day, time=pd.Timestamp(r.end), side=1 if r.close > hi else -1,
                                     ref=float(r.close), score=float((hi - lo) / fn)))
    F.reset_index().to_parquet(out_f, index=False)
    E = pd.DataFrame(final_ev) if final_ev else pd.DataFrame(columns=["variant", "day", "time", "side", "ref", "score"])
    E.to_parquet(out_e, index=False)
    return sym, f"days={len(F)} events={len(E)}"


def index_pass():
    import json
    out = C.D / "index_features.parquet"
    sectors = json.load(open(C.HERE.parents[1] / "strategies" / "intraday_pullback_top2" / "sector_map.json"))["backfilled_sectors"]
    rows = []
    for name in sectors + ["NIFTY"]:
        m = C.load_index_1m(name, dt.date(2018, 12, 1), C.HOLD[1])
        if m.empty:
            print("no index 1m", name, flush=True)
            continue
        m["day"] = m.ts.dt.date
        for day, g in m.groupby("day"):
            tm = g.ts.dt.strftime("%H:%M")
            c = g.close[tm == "09:44"]
            if (tm == "09:15").any() and len(c):
                rows.append(dict(index=name, day=day, open=float(g.open.iloc[0]), c0944=float(c.iloc[0])))
    pd.DataFrame(rows).to_parquet(out, index=False)
    print("index features", len(rows), flush=True)


if __name__ == "__main__":
    FEAT.mkdir(parents=True, exist_ok=True)
    EVT.mkdir(parents=True, exist_ok=True)
    import json
    syms = sorted(json.load(open(C.D / "tokens_pit.json"))["resolved"])
    if len(sys.argv) > 2:
        syms = sys.argv[2].split(",")
    index_pass() if len(sys.argv) <= 2 else None
    with Pool(int(sys.argv[1]) if len(sys.argv) > 1 else 4) as p:
        for i, (s, msg) in enumerate(p.imap_unordered(symbol_pass, syms), 1):
            print(f"{i}/{len(syms)} {s} {msg}", flush=True)
    print("DONE", flush=True)
