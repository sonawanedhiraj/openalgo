import json
import os
import statistics as st
import sys

S = os.path.dirname(os.path.abspath(__file__))
feat = json.load(open(os.path.join(S, "features.json")))
real = [f for f in feat if f["fill"] == "real" and f["status"] == "closed"]
other = [f for f in feat if f not in real and f.get("stock_ret_0930_pct") is not None]
print("real", len(real), "other-with-ticks", len(other))

FEATS = [
    "gap_pct",
    "level_vs_prev_pct",
    "trig_vs_prev_pct",
    "beyond_level_pct",
    "vol_ratio",
    "prev_min_vol_ratio",
    "tick_intensity",
    "trig_min_from_open",
    "c15_pos",
    "c15_range_pct",
    "c15_dir_with_side",
    "mom30_pct",
    "mom60_pct",
    "mom_from_c15_pct",
    "secs_since_first_cross",
    "frac_beyond_level_pre",
    "univ_med_ret_to_trig",
    "univ_up_frac",
    "rel_mom_pct",
    "univ_med_ret_hold",
    "ret_60s_pct",
    "ret_180s_pct",
    "mfe_pct",
    "mae_pct",
    "stock_ret_0930_pct",
    "opt_spread_pct",
    "opt_oi_lots",
    "n_news",
]


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def fmt(x):
    return "   -   " if x is None else f"{x:7.2f}"


def table(rows, label, win_key):
    print(f"\n=== {label}: n={len(rows)} ===")
    W = [r for r in rows if win_key(r)]
    L = [r for r in rows if not win_key(r)]
    print(f"winners {len(W)}  losers {len(L)}   (median per feature)")
    print(f"{'feature':26s} {'win':>8s} {'lose':>8s}")
    for k in FEATS:
        print(f"{k:26s} {fmt(med([r.get(k) for r in W]))} {fmt(med([r.get(k) for r in L]))}")


def wr_by_bins(rows, key, edges, label, outcome):
    print(f"\n-- {label}: {key} bins --")
    for lo, hi in zip(edges[:-1], edges[1:], strict=False):
        sub = [r for r in rows if r.get(key) is not None and lo <= r[key] < hi]
        if not sub:
            continue
        w = sum(1 for r in sub if outcome(r) > 0)
        pnl = sum((r.get("net") or 0) for r in sub)
        sr = med([r.get("stock_ret_0930_pct") for r in sub])
        print(
            f"  [{lo:>7}, {hi:>7})  n={len(sub):2d}  WR={w / len(sub):4.0%}  net=Rs{pnl:+8.0f}  med stock ret={sr if sr is None else round(sr, 2)}"
        )


def net(r):
    return r.get("net") or 0


def stock(r):
    return r.get("stock_ret_0930_pct") or 0


for side in ("L", "S"):
    rs = [r for r in real if r["side"] == side]
    table(rs, f"REAL {side} by NET option P&L", lambda r: net(r) > 0)
    table(rs, f"REAL {side} by STOCK move to 09:30", lambda r: stock(r) > 0)

print("\n\n######## BINNED WIN RATES (real, net P&L) ########")
for side in ("L", "S"):
    rs = [r for r in real if r["side"] == side]
    print(f"\n##### side {side} n={len(rs)}")
    wr_by_bins(rs, "trig_min_from_open", [0, 3, 5, 7, 9, 16], side, net)
    wr_by_bins(rs, "vol_ratio", [1.0, 1.55, 1.7, 2.0, 10], side, net)
    wr_by_bins(
        rs, "gap_pct" if side == "L" else "gap_pct", [-5, -1.5, -0.5, 0.5, 1.0, 1.5, 5], side, net
    )
    wr_by_bins(rs, "mom60_pct", [-5, 0, 0.15, 0.3, 5], side, net)
    wr_by_bins(rs, "mom_from_c15_pct", [-5, 0, 0.3, 0.6, 1.0, 5], side, net)
    wr_by_bins(rs, "beyond_level_pct", [-1, 0.02, 0.1, 0.3, 5], side, net)
    wr_by_bins(rs, "c15_pos", [0, 0.33, 0.66, 1.01], side, net)
    wr_by_bins(rs, "secs_since_first_cross", [0, 5, 30, 120, 2000], side, net)
    wr_by_bins(rs, "frac_beyond_level_pre", [0, 0.05, 0.3, 0.7, 1.01], side, net)
    wr_by_bins(rs, "univ_med_ret_to_trig", [-5, -0.3, 0, 0.3, 5], side, net)
    wr_by_bins(rs, "rel_mom_pct", [-5, 0, 0.3, 0.7, 5], side, net)
    wr_by_bins(rs, "tick_intensity", [0, 1.0, 1.5, 2.5, 20], side, net)
    wr_by_bins(rs, "level_vs_prev_pct", [-5, 0, 0.5, 1.0, 2.0, 5], side, net)
    wr_by_bins(rs, "opt_spread_pct", [0, 0.5, 1.0, 2.0, 20], side, net)
    wr_by_bins(rs, "n_news", [0, 1, 2, 5, 100], side, net)
    wr_by_bins(rs, "ret_60s_pct", [-5, -0.1, 0, 0.1, 5], side, net)

print("\n\n######## PER-TRADE TABLE (real) ########")
hdr = "date       sym          s src    net    stk% gap   lvl%  byd%  vr   tmin  m60   mC15  c15p  fb   univ  rel   news"
print(hdr)
for r in real:

    def g(k, w=5, p=2, r=r):
        v = r.get(k)
        return " " * w if v is None else f"{v:{w}.{p}f}"

    print(
        f"{r['trade_date']} {r['symbol']:<12} {r['side']} {r['watch_source'][:4]:4s} {net(r):+7.0f} {g('stock_ret_0930_pct')} {g('gap_pct')} {g('level_vs_prev_pct')} {g('beyond_level_pct', 5, 2)} {g('vol_ratio', 4)} {g('trig_min_from_open', 5, 1)} {g('mom60_pct')} {g('mom_from_c15_pct')} {g('c15_pos', 4)} {g('frac_beyond_level_pre', 4)} {g('univ_med_ret_to_trig')} {g('rel_mom_pct')} {r['n_news']}"
    )

print("\n\n######## NEWS MATCHES (real trades) ########")
for r in real:
    if r["news"]:
        print(
            f"\n{r['trade_date']} {r['symbol']} {r['side']} net={net(r):+.0f} stock={stock(r):+.2f}%"
        )
        for n in r["news"][-8:]:
            print("   ", n[0], n[1], "|", n[2][:140])
