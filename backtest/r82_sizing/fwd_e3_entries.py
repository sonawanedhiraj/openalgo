"""E3 entries on the forward window (2025-06-20..2026-11-13) with R81's frozen feature code and E3 rule, PIT universe
from NSE month-end lists (data/universe_fwd.json). -> data/entries_e3_fwd.parquet (all E3 variants, both sides;
R82 uses side == -1 only)."""

import datetime as dt
import json
import sys
from multiprocessing import Pool
from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "r81_entry"))
import features as FE  # noqa: E402
from select_entries import E3V, at  # noqa: E402

D = HERE / "data"
LO, HI = dt.date(2025, 6, 20), dt.date(2026, 11, 13)


def work(sym):
    con = duckdb.connect(str(D / "bars_fwd.duckdb"), read_only=True)
    dl = con.execute(
        "select bar_date,open,high,low,close,volume from d1 where symbol=? order by bar_date", [sym]
    ).df()
    m = con.execute(
        "select ts,open,high,low,close,volume from m1 where symbol=? and ts>=? order by ts",
        [sym, dt.datetime(2025, 6, 20)],
    ).df()
    con.close()
    if dl.empty or m.empty:
        return []
    df = FE.daily_features(dl).set_index("day")
    t = m.ts.dt.time
    m = m[(t >= dt.time(9, 15)) & (t <= dt.time(15, 29))]
    rows = []
    for day, g in m.groupby(m.ts.dt.date):
        f, _ = FE.day_block(g)
        if day in df.index:
            f.update(df.loc[day].to_dict())
        f.update(day=day, symbol=sym)
        rows.append(f)
    return rows


def main():
    U = {k: set(v["stocks"]) for k, v in json.load(open(D / "universe_fwd.json")).items()}
    syms = sorted(json.load(open(D / "tokens_fwd.json"))["resolved"])
    with Pool(6) as p:
        F = pd.DataFrame([r for rr in p.imap_unordered(work, syms) for r in rr])
    F = F[(F.day >= LO) & (F.day <= HI)]
    F["month"] = pd.to_datetime(F.day).dt.strftime("%Y-%m")
    F["in_u"] = [s in U.get(mo, set()) for s, mo in zip(F.symbol, F.month, strict=False)]
    F["elig"] = F.in_u & F.prev_close.between(100, 5000) & (F.adv20 >= 5e8) & F.atr_pct.notna()
    F.to_parquet(D / "features_fwd.parquet", index=False)
    el = F[F.elig].copy()
    el["gap"] = (el.open - el.prev_close) / el.atr14
    out = []
    for vn, (gth, ccol, tt) in E3V.items():
        cc = el["c" + ccol]
        ok = (
            (el.gap.abs() >= gth)
            & cc.notna()
            & (((el.gap > 0) & (cc < el.open)) | ((el.gap < 0) & (cc > el.open)))
        )
        g = el[ok].assign(cref=cc[ok])
        for day, gg in g.groupby("day"):
            gg = gg.reindex(gg.gap.abs().sort_values(ascending=False).index).head(2)
            for r in gg.itertuples():
                out.append(
                    {
                        "variant": vn,
                        "day": day,
                        "time": at(day, tt),
                        "symbol": r.symbol,
                        "side": -1 if r.gap > 0 else 1,
                        "ref": r.cref,
                        "score": abs(r.gap),
                    }
                )
    X = pd.DataFrame(out)
    X["cand"] = "E3"
    X.to_parquet(D / "entries_e3_fwd.parquet", index=False)
    print(
        "symbol-days",
        len(F),
        "eligible",
        int(F.elig.sum()),
        "days",
        F.day.nunique(),
        "in_u share",
        round(F.in_u.mean(), 3),
        X.groupby(["variant", "side"]).size().to_dict(),
    )


if __name__ == "__main__":
    main()
