import json
import os
import sqlite3
import statistics as st

S = os.path.dirname(os.path.abspath(__file__))
feat = json.load(open(os.path.join(S, "features.json")))
real = [f for f in feat if f["fill"] == "real" and f["status"] == "closed"]
other = [f for f in feat if f not in real and f.get("stock_ret_0930_pct") is not None]


def net(r):
    return r.get("net") or 0


def stock(r):
    return r.get("stock_ret_0930_pct") or 0


def checks(r):
    """Each check is knowable AT the trigger. Returns dict name->bool (None if feature missing)."""
    side = r["side"]
    g = r.get
    out = {}
    # common to both sides
    out["early"] = g("trig_min_from_open") is not None and g("trig_min_from_open") <= 7.0
    out["clean_vol"] = g("vol_ratio") is not None and g("vol_ratio") < 1.55
    out["fresh"] = (
        g("secs_since_first_cross") is not None and g("secs_since_first_cross") < 120
    ) or (g("frac_beyond_level_pre") is not None and g("frac_beyond_level_pre") < 0.7)
    out["market_ok"] = g("univ_med_ret_to_trig") is not None and g("univ_med_ret_to_trig") > -0.3
    out["spread_ok"] = g("opt_spread_pct") is None or g("opt_spread_pct") < 2.0
    if side == "L":
        out["level_ext"] = g("level_vs_prev_pct") is not None and g("level_vs_prev_pct") >= 1.0
        out["gap_band"] = g("gap_pct") is not None and 0.5 <= g("gap_pct") <= 1.5
    else:
        out["gap_band"] = g("gap_pct") is not None and g("gap_pct") > -0.5
        out["c15_low"] = g("c15_pos") is not None and g("c15_pos") < 0.33
    return out


def grade(r):
    ch = checks(r)
    n = sum(1 for v in ch.values() if v)
    tot = len(ch)
    # hard veto: a long into a broad-weak tape, or any trigger after 09:24
    if r["side"] == "L" and not ch["market_ok"]:
        return "C", n, ch
    if (r.get("trig_min_from_open") or 0) > 9:
        return "C", n, ch
    if n >= tot - 1:
        return "A", n, ch
    if n >= tot - 3:
        return "B", n, ch
    return "C", n, ch


def report(rows, label, money=True):
    print(f"\n=== {label} n={len(rows)} ===")
    for side in ("L", "S", "ALL"):
        rs = [r for r in rows if side == "ALL" or r["side"] == side]
        print(f"  side {side}")
        for gr in ("A", "B", "C"):
            sub = [r for r in rs if grade(r)[0] == gr]
            if not sub:
                print(f"    {gr}: n=0")
                continue
            wr_stock = sum(1 for r in sub if stock(r) > 0) / len(sub)
            line = f"    {gr}: n={len(sub):2d}  stockWR={wr_stock:4.0%}  med stock={st.median([stock(r) for r in sub]):+.2f}%  mean stock={st.mean([stock(r) for r in sub]):+.2f}%"
            if money:
                wr = sum(1 for r in sub if net(r) > 0) / len(sub)
                line += f"  netWR={wr:4.0%}  net=Rs{sum(net(r) for r in sub):+8.0f}  avg=Rs{sum(net(r) for r in sub) / len(sub):+6.0f}"
            print(line)


report(real, "REAL fills (in-sample, rules derived here)")
report(
    other,
    "OTHER cohort (shadow/sim/paper/rejected with ticks) — stock basis, pseudo-holdout",
    money=False,
)

# sim/shadow/paper rows carry a modelled pnl too
priced = [r for r in other if r.get("pnl") is not None]
print(f"\n(other cohort with modelled pnl: {len(priced)})")
for gr in ("A", "B", "C"):
    sub = [r for r in priced if grade(r)[0] == gr]
    if sub:
        print(
            f"   {gr}: n={len(sub)} modelled net=Rs{sum(net(r) for r in sub):+.0f} WR={sum(1 for r in sub if net(r) > 0) / len(sub):.0%}"
        )

print("\n\n=== per-trade grades (real) ===")
for r in real:
    gr, n, ch = grade(r)
    fails = [k for k, v in ch.items() if not v]
    print(
        f"{r['trade_date']} {r['symbol']:<12} {r['side']} {gr} {n}/{len(ch)} net={net(r):+7.0f} stk={stock(r):+.2f}%  fails={fails}"
    )

print("\n=== per-trade grades (other) ===")
for r in other:
    gr, n, ch = grade(r)
    fails = [k for k, v in ch.items() if not v]
    print(
        f"{r['trade_date']} {r['symbol']:<12} {r['side']} {r['fill'] or '-':6s} {r['reason'] or '':28s} {gr} {n}/{len(ch)} stk={stock(r):+.2f}% pnl={r.get('pnl')}  fails={fails}"
    )

# single-check power tables both cohorts
print("\n=== single-check win rates (stock basis) ===")
for side in ("L", "S"):
    print(f" side {side}")
    for name in checks(real[0]).keys() | checks([r for r in real if r["side"] == "S"][0]).keys():
        for lab, rows in (("real", real), ("other", other)):
            rs = [r for r in rows if r["side"] == side and name in checks(r)]
            if not rs:
                continue
            p = [r for r in rs if checks(r)[name]]
            q = [r for r in rs if not checks(r)[name]]

            def wr(x):
                return (
                    f"{sum(1 for r in x if stock(r) > 0) / len(x):4.0%} (n={len(x):2d}, med {st.median([stock(r) for r in x]):+.2f})"
                    if x
                    else "  -  "
                )

            print(f"   {name:10s} {lab:5s} pass {wr(p)}   fail {wr(q)}")
