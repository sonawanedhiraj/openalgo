import collections
import datetime as dt
import math
import sqlite3

c = sqlite3.connect("file:db/openalgo.db?mode=ro", uri=True)
rows = c.execute("""select id,placed_at,symbol,direction,quantity,entry_price,exit_price,exit_reason,pnl,charges_inr,
 nifty_pct_at_entry,india_vix_at_entry,ltp_at_signal,hold_duration_seconds,signal_source
 from trade_journal where exit_price is not null and entry_price is not null
 and exit_reason not like 'phantom%' order by placed_at""").fetchall()
T = []
seen = collections.defaultdict(list)
for r in rows:
    (i, pa, sym, d, q, e, x, er, p, ch, nf, vix, ltp, hold, src) = r
    t = dt.datetime.fromisoformat(pa)
    day = t.date().isoformat()
    net = (p or 0) - (ch or 0)
    prev = seen[day]
    rec = {
        "day": day,
        "hm": t.hour * 60 + t.minute,
        "sym": sym,
        "dir": d,
        "net": net,
        "gross": p or 0,
        "ch": ch or 0,
        "nf": nf,
        "vix": vix,
        "chase": None if not ltp else ((e - ltp) / ltp * 100 * (1 if d == "LONG" else -1)),
        "n": len(prev) + 1,
        "prior_loss": any(z["net"] < 0 for z in prev),
        "same_sym": any(z["sym"] == sym for z in prev),
        "nsame": sum(z["dir"] == d for z in prev),
        "notional": e * abs(q),
        "er": er,
        "hold": hold,
    }
    prev.append(rec)
    T.append(rec)


def win(d):
    return "1" if d <= "2026-08-08" else ("2" if d <= "2026-09-18" else "3")


for r in T:
    r["w"] = win(r["day"])


def stats(L):
    n = len(L)
    if not n:
        return "   -   "
    w = sum(r["net"] > 0 for r in L)
    return f"{n:4d} {100 * w / n:5.1f}% {sum(r['net'] for r in L):+8.0f}"


print("ALL", stats(T))
print("gross", round(sum(r["gross"] for r in T)), "charges", round(sum(r["ch"] for r in T)))
for w in "123":
    print(" win", w, stats([r for r in T if r["w"] == w]))


def cn(r):
    if r["nf"] is None:
        return None
    s = 1 if r["dir"] == "LONG" else -1
    v = r["nf"] * s
    return "against>0.25" if v < -0.25 else ("with>0.25" if v > 0.25 else "flat")


F = {
    "LONG": lambda r: r["dir"] == "LONG",
    "SHORT": lambda r: r["dir"] == "SHORT",
    "before 11:00": lambda r: r["hm"] < 660,
    "11-13": lambda r: 660 <= r["hm"] < 780,
    "after 13:00": lambda r: r["hm"] >= 780,
    "1st trade of day": lambda r: r["n"] == 1,
    "trade #>=4": lambda r: r["n"] >= 4,
    "after a loss today": lambda r: r["prior_loss"],
    "no loss yet today": lambda r: not r["prior_loss"],
    "same symbol re-entry": lambda r: r["same_sym"],
    "3rd+ same direction": lambda r: r["nsame"] >= 2,
    "chase>0.3% past signal": lambda r: r["chase"] is not None and r["chase"] > 0.3,
    "chase<=0.1%": lambda r: r["chase"] is not None and r["chase"] <= 0.1,
    "VIX>=12": lambda r: r["vix"] is not None and r["vix"] >= 12,
    "VIX<12": lambda r: r["vix"] is not None and r["vix"] < 12,
    "counter-NIFTY >0.25%": lambda r: cn(r) == "against>0.25",
    "with NIFTY >0.25%": lambda r: cn(r) == "with>0.25",
    "flat NIFTY": lambda r: cn(r) == "flat",
    "counter-NIFTY LONG": lambda r: cn(r) == "against>0.25" and r["dir"] == "LONG",
    "counter-NIFTY SHORT": lambda r: cn(r) == "against>0.25" and r["dir"] == "SHORT",
}
print(
    f"\n{'cohort':26s} | {'W1 <=08-08':20s} | {'W2 08-09..09-18':20s} | {'W3 09-19..10-27 (fresh)':20s}"
)
for k, f in F.items():
    print(
        f"{k:26s} | "
        + " | ".join(f"{stats([r for r in T if r['w'] == w and f(r)]):20s}" for w in "123")
    )
# monthly W3 counter-NIFTY
print()
for m in ["2026-08", "2026-09", "2026-10"]:
    L = [r for r in T if r["day"].startswith(m) and cn(r)]
    print(m, "all", stats(L), "counter", stats([r for r in L if cn(r) == "against>0.25"]))
# stop analysis: avg win vs avg loss
W = [r["net"] for r in T if r["net"] > 0]
Lo = [r["net"] for r in T if r["net"] <= 0]
print("avg win", round(sum(W) / len(W)), "avg loss", round(sum(Lo) / len(Lo)), "n", len(W), len(Lo))
print("exit reasons", collections.Counter(r["er"] for r in T))
# placebo for counter cell in W3
import random

L3 = [r for r in T if r["w"] == "3" and cn(r)]
k = sum(cn(r) == "against>0.25" for r in L3)
obs = sum(r["net"] for r in L3 if cn(r) == "against>0.25") / max(k, 1)
random.seed(1)
cnt = 0
for _ in range(5000):
    s = random.sample(L3, k)  # nosec B311 - placebo resampling, not crypto
    if sum(r["net"] for r in s) / k >= obs:
        cnt += 1
print("W3 counter placebo p", cnt / 5000, "k", k, "avg", round(obs))
print()
for w in "123":
    L = [r for r in T if r["w"] == w]
    for lab, key in (("gross", "gross"), ("net", "net")):
        W = [r[key] for r in L if r[key] > 0]
        Lo = [r[key] for r in L if r[key] <= 0]
        aw = sum(W) / len(W)
        al = -sum(Lo) / len(Lo)
        print(
            w,
            lab,
            "WR",
            round(100 * len(W) / len(L), 1),
            "avgW",
            round(aw),
            "avgL",
            round(al),
            "payoff",
            round(aw / al, 2),
            "breakevenWR",
            round(100 * al / (aw + al), 1),
        )
    ch = sum(r["ch"] for r in L) / len(L)
    print("   charges/trade", round(ch))
