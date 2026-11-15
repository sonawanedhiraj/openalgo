"""R81: build entries for every pre-registered candidate variant from the feature files (inputs only).
-> data/entries_<period>.parquet  (variant, cand, day, time, symbol, side, ref, score)

Resolutions (recorded in the report):
- Eligibility per symbol-day: in the PIT F&O list of the previous month-end; prev close 100..5000; ADV20 >= Rs50 cr;
  daily ATR14 available. E2/E5 need >= 28 of the 30 1m bars 09:15..09:44.
- E2 ranks by RS / daily ATR% (the threshold is ATR-scaled); names missing from the sector map are excluded.
- E3 confirmation bar = 09:15 -> 09:25/09:30/09:35 close; entry at that close; at most 2 a day by |gap|.
- E1/E4: at most 3 a day in trigger-time order (ties: higher score).
  uv run python backtest/r81_entry/select_entries.py train|validate|holdout
"""
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

E2V = {"E2": (0.0030, 0.5), "E2_s0.20": (0.0020, 0.5), "E2_s0.40": (0.0040, 0.5), "E2_rs0.35": (0.0030, 0.35), "E2_rs0.65": (0.0030, 0.65)}
E5V = {"E5": (0.40, 0.5), "E5_er0.30": (0.30, 0.5), "E5_er0.50": (0.50, 0.5), "E5_m0.35": (0.40, 0.35), "E5_m0.65": (0.40, 0.65)}
E3V = {"E3": (1.0, "0929", "09:30"), "E3_g0.75": (0.75, "0929", "09:30"), "E3_g1.25": (1.25, "0929", "09:30"),
       "E3_c10": (1.0, "0924", "09:25"), "E3_c20": (1.0, "0934", "09:35")}


def load_all(period):
    lo, hi = {"train": C.TRAIN, "validate": C.VALID, "holdout": C.HOLD}[period]
    U = C.universe_pit()
    fs, es = [], []
    for p in sorted((C.D / "features").glob("*.parquet")):
        sym = p.stem
        f = pd.read_parquet(p)
        f = f[(f.day >= lo) & (f.day <= hi)]
        if f.empty:
            continue
        f["symbol"] = sym
        fs.append(f)
        e = pd.read_parquet(C.D / "events" / f"{sym}.parquet")
        if len(e):
            e = e[(e.day >= lo) & (e.day <= hi)]
            e["symbol"] = sym
            es.append(e)
    F = pd.concat(fs, ignore_index=True)
    F["month"] = pd.to_datetime(F.day).dt.strftime("%Y-%m")
    F["in_u"] = [s in U.get(m, set()) for s, m in zip(F.symbol, F.month)]
    F["elig"] = F.in_u & F.prev_close.between(100, 5000) & (F.adv20 >= 5e8) & F.atr_pct.notna()
    E = pd.concat(es, ignore_index=True) if es else pd.DataFrame()
    return F, E


def at(day, hhmm):
    return dt.datetime.combine(day, dt.time(int(hhmm[:2]), int(hhmm[3:])))


def build(period):
    F, E = load_all(period)
    out = []
    el = F[F.elig].copy()
    # ---- E2
    ix = pd.read_parquet(C.D / "index_features.parquet")
    ix["ret"] = ix.c0944 / ix.open - 1
    ixr = {(r.index, r.day): r.ret for r in ix.itertuples()}
    e2 = el[(el.n_first30 >= 28) & el.c0944.notna()].copy()
    e2["sector"] = e2.symbol.map(C.sector_for)
    e2 = e2[e2.sector.notna()]
    e2["sret"] = [ixr.get((s, d), np.nan) for s, d in zip(e2.sector, e2.day)]
    e2 = e2[e2.sret.notna()]
    e2["ret"] = e2.c0944 / e2.open - 1
    e2["rsn"] = (e2.ret - e2.sret) / e2.atr_pct
    for vn, (ts, tr) in E2V.items():
        for day, g in e2.groupby("day"):
            L = g[(g.sret >= ts) & (g.rsn >= tr)]
            S = g[(g.sret <= -ts) & (g.rsn <= -tr)]
            if len(L):
                r = L.loc[L.rsn.idxmax()]
                out.append(dict(variant=vn, day=day, time=at(day, "09:45"), symbol=r.symbol, side=1, ref=r.c0944, score=r.rsn))
            if len(S):
                r = S.loc[S.rsn.idxmin()]
                out.append(dict(variant=vn, day=day, time=at(day, "09:45"), symbol=r.symbol, side=-1, ref=r.c0944, score=-r.rsn))
    # ---- E5
    e5 = el[(el.n_first30 >= 28) & el.c0944.notna() & (el.path_sum > 0)].copy()
    e5["er"] = (e5.c0944 - e5.open).abs() / e5.path_sum
    e5["mv"] = (e5.c0944 - e5.open).abs() / e5.atr14
    for vn, (er, mv) in E5V.items():
        g = e5[(e5.er >= er) & (e5.mv >= mv)].copy()
        g["sc"] = g.er * g.mv
        for day, gg in g.groupby("day"):
            r = gg.loc[gg.sc.idxmax()]
            out.append(dict(variant=vn, day=day, time=at(day, "09:45"), symbol=r.symbol,
                            side=1 if r.c0944 > r.open else -1, ref=r.c0944, score=r.sc))
    # ---- E3
    e3 = el.copy()
    e3["gap"] = (e3.open - e3.prev_close) / e3.atr14
    for vn, (gth, ccol, tt) in E3V.items():
        cc = e3["c" + ccol]
        ok = (e3.gap.abs() >= gth) & cc.notna() & (((e3.gap > 0) & (cc < e3.open)) | ((e3.gap < 0) & (cc > e3.open)))
        g = e3[ok].assign(cref=cc[ok])
        for day, gg in g.groupby("day"):
            gg = gg.reindex(gg.gap.abs().sort_values(ascending=False).index).head(2)
            for r in gg.itertuples():
                out.append(dict(variant=vn, day=day, time=at(day, tt), symbol=r.symbol, side=-1 if r.gap > 0 else 1,
                                ref=r.cref, score=abs(r.gap)))
    # ---- E1 / E4 events, eligibility + caps
    if len(E):
        ok = set(zip(el.symbol, el.day))
        E = E[[(s, d) in ok for s, d in zip(E.symbol, E.day)]]
        for vn, g in E.groupby("variant"):
            for day, gg in g.groupby("day"):
                gg = gg.sort_values(["time", "score"], ascending=[True, False]).head(3)
                for r in gg.itertuples():
                    out.append(dict(variant=vn, day=day, time=pd.Timestamp(r.time).to_pydatetime(), symbol=r.symbol,
                                    side=int(r.side), ref=r.ref, score=r.score))
    X = pd.DataFrame(out)
    X["cand"] = X.variant.str.split("_").str[0]
    X.to_parquet(C.D / f"entries_{period}.parquet", index=False)
    stats = dict(elig_symbol_days=int(F.elig.sum()), symbol_days=len(F), days=int(F.day.nunique()),
                 e2_unmapped_share=round(1 - len(e2) / max(1, len(el[(el.n_first30 >= 28)])), 3))
    print(period, stats, X.groupby("variant").size().to_dict(), flush=True)
    return stats


if __name__ == "__main__":
    build(sys.argv[1])
