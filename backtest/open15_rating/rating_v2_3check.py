import json
import os
import statistics as st

S = os.path.dirname(os.path.abspath(__file__))
feat = json.load(open(os.path.join(S, "features.json")))
real = [f for f in feat if f["fill"] == "real" and f["status"] == "closed"]
other = [f for f in feat if f not in real and f.get("stock_ret_0930_pct") is not None]


def net(r):
    return r.get("net") or 0


def stock(r):
    return r.get("stock_ret_0930_pct") or 0


def core(r):
    g = r.get
    return {
        "early": g("trig_min_from_open") is not None
        and g("trig_min_from_open") <= 7.0,  # trigger by 09:22
        "market_ok": g("univ_med_ret_to_trig") is not None
        and g("univ_med_ret_to_trig") > -0.3,  # universe median open->trigger
        "clean_vol": g("vol_ratio") is not None
        and g("vol_ratio") < 1.55,  # gate crossed, not blown through
    }


def grade2(r):
    ch = core(r)
    late = (r.get("trig_min_from_open") or 0) > 9
    if late or not ch["market_ok"]:
        return "C"
    if ch["early"] and ch["clean_vol"]:
        return "A"
    return "B"


def rep(rows, label, money):
    print(f"\n=== {label} n={len(rows)} ===")
    for side in ("L", "S", "ALL"):
        rs = [r for r in rows if side == "ALL" or r["side"] == side]
        for gr in ("A", "B", "C"):
            sub = [r for r in rs if grade2(r) == gr]
            if not sub:
                print(f"  {side} {gr}: n=0")
                continue
            days = len({r["trade_date"] for r in sub})
            line = f"  {side} {gr}: n={len(sub):2d} days={days:2d} stockWR={sum(1 for r in sub if stock(r) > 0) / len(sub):4.0%} med={st.median([stock(r) for r in sub]):+.2f}% mean={st.mean([stock(r) for r in sub]):+.2f}%"
            if money:
                line += f"  netWR={sum(1 for r in sub if net(r) > 0) / len(sub):4.0%} net=Rs{sum(net(r) for r in sub):+8.0f} avg=Rs{sum(net(r) for r in sub) / len(sub):+6.0f}"
            print(line)


rep(real, "REAL (robust 3-check rating)", True)
rep(other, "OTHER holdout (stock basis)", False)
priced = [r for r in other if r.get("pnl") is not None]
for gr in ("A", "B", "C"):
    sub = [r for r in priced if grade2(r) == gr]
    if sub:
        print(
            f"  other modelled {gr}: n={len(sub)} net=Rs{sum(net(r) for r in sub):+.0f} WR={sum(1 for r in sub if net(r) > 0) / len(sub):.0%}"
        )

# time-split check on real: first half vs second half of the real window
real_sorted = sorted(real, key=lambda r: (r["trade_date"], r["trigger_minute"]))
h1, h2 = real_sorted[:24], real_sorted[24:]
rep(h1, "REAL first 24 fills (08-06..09-01)", True)
rep(h2, "REAL last 24 fills (09-01..09-11)", True)

# 60-second follow-through as a post-entry confirmation (not a rating input)
print("\n=== post-entry 60s follow-through (real) ===")
for side in ("L", "S"):
    rs = [r for r in real if r["side"] == side and r.get("ret_60s_pct") is not None]
    for lab, cond in (
        ("ret60>0.1", lambda r: r["ret_60s_pct"] > 0.1),
        ("0<ret60<=0.1", lambda r: 0 < r["ret_60s_pct"] <= 0.1),
        ("ret60<=0", lambda r: r["ret_60s_pct"] <= 0),
    ):
        sub = [r for r in rs if cond(r)]
        if sub:
            print(
                f"  {side} {lab:14s} n={len(sub):2d} netWR={sum(1 for r in sub if net(r) > 0) / len(sub):4.0%} net=Rs{sum(net(r) for r in sub):+8.0f} med stock->0930={st.median([stock(r) for r in sub]):+.2f}%"
            )

# day clustering: per-day sum of real net, and the universe median on that day at 09:20
print("\n=== per-day (real) ===")
days = {}
for r in real:
    days.setdefault(r["trade_date"], []).append(r)
for d, rs in sorted(days.items()):
    print(
        f"  {d} n={len(rs)} net=Rs{sum(net(r) for r in rs):+8.0f} wins={sum(1 for r in rs if net(r) > 0)} univ_med@trig={st.median([r.get('univ_med_ret_to_trig') or 0 for r in rs]):+.2f}%  syms={[r['symbol'] + r['side'] for r in rs]}"
    )
