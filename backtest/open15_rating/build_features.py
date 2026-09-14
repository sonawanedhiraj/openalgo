"""Build per-trade tick features + news matches for every open15 row with a trigger (all fill classes)."""

import collections
import csv
import glob
import json
import os
import sqlite3
import statistics as st
from datetime import datetime, timedelta

S = os.path.dirname(os.path.abspath(__file__))
c = sqlite3.connect("file:db/openalgo.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = c.execute("""select * from open15_trades where trigger_price is not null and trigger_minute is not null
                  order by trade_date, trigger_minute, trigger_second""").fetchall()
names = {
    r["symbol"]: r["name"]
    for r in c.execute("select symbol,name from symtoken where exchange='NSE'")
}
print(len(rows), "rows with a trigger")


def tick_file(d):
    fs = glob.glob(f"tick_logs/open15/ticks-{d.replace('-', '')}-*.jsonl")
    if not fs:
        return None
    return max(fs, key=os.path.getsize)


_cache = {}


def load_day(d):
    if d in _cache:
        return _cache[d]
    f = tick_file(d)
    if not f:
        _cache[d] = None
        return None
    per = collections.defaultdict(list)
    with open(f) as fh:
        for line in fh:
            try:
                t = json.loads(line)
            except Exception:  # nosec B112 — a malformed tick line is skipped, not fatal
                continue
            ts = t["ts"][11:19]
            per[t["symbol"]].append((ts, float(t["ltp"]), float(t.get("volume") or 0)))
    for k in per:
        per[k].sort()
    _cache[d] = per
    return per


def px_at(ticks, hhmmss):
    last = None
    for ts, p, v in ticks:
        if ts <= hhmmss:
            last = (ts, p, v)
        else:
            break
    return last


def minute_bars(ticks):
    bars = collections.OrderedDict()
    for ts, p, v in ticks:
        m = ts[:5]
        b = bars.setdefault(m, {"o": p, "h": p, "l": p, "c": p, "v0": v, "v1": v, "n": 0})
        b["h"] = max(b["h"], p)
        b["l"] = min(b["l"], p)
        b["c"] = p
        b["v1"] = v
        b["n"] += 1
    return bars


def universe_breadth(per, t0, t1):
    ups = 0
    tot = 0
    rets = []
    for _s, ticks in per.items():
        a = px_at(ticks, t0)
        b = px_at(ticks, t1)
        if not a or not b or a[1] <= 0:
            continue
        r = b[1] / a[1] - 1
        rets.append(r)
        tot += 1
        ups += r > 0
    return (st.median(rets) if rets else None, ups / tot if tot else None, tot)


SPECIAL = {
    "IDEA": ["vodafone idea", " vi "],
    "PFC": ["power finance", " pfc"],
    "HAL": ["hindustan aeronautics", " hal "],
    "MCX": ["mcx", "multi commodity"],
    "LTF": ["l&t finance", "lt finance"],
    "DLF": ["dlf"],
    "UPL": [" upl"],
    "VBL": ["varun beverages"],
    "TVSMOTOR": ["tvs motor"],
    "CGPOWER": ["cg power"],
    "LICHSGFIN": ["lic housing"],
    "SBICARD": ["sbi card"],
    "BAJAJ-AUTO": ["bajaj auto"],
    "HDFCBANK": ["hdfc bank"],
    "TECHM": ["tech mahindra"],
    "NATIONALUM": ["nalco", "national aluminium"],
    "CANBK": ["canara bank"],
    "UNIONBANK": ["union bank"],
    "HINDZINC": ["hindustan zinc"],
    "INFY": ["infosys"],
    "TCS": ["tata consultancy", " tcs"],
    "MAXHEALTH": ["max healthcare"],
    "GVT&D": ["gvt&d", "ge vernova"],
    "MOTHERSON": ["motherson"],
    "ADANIPORTS": ["adani ports"],
    "ONGC": ["ongc"],
    "ANGELONE": ["angel one"],
    "PREMIERENE": ["premier energies"],
    "BANDHANBNK": ["bandhan bank"],
    "HEROMOTOCO": ["hero motocorp"],
    "DIVISLAB": ["divi's", "divis lab"],
    "POLYCAB": ["polycab"],
    "SUNPHARMA": ["sun pharma"],
    "LODHA": ["lodha", "macrotech"],
    "CUMMINSIND": ["cummins"],
    "ASHOKLEY": ["ashok leyland"],
    "MUTHOOTFIN": ["muthoot"],
    "DIXON": ["dixon"],
    "BIOCON": ["biocon"],
    "MANKIND": ["mankind"],
    "HINDALCO": ["hindalco"],
    "VEDL": ["vedanta"],
    "BRITANNIA": ["britannia"],
    "WIPRO": ["wipro"],
}
news_rows = [
    (r[0], json.loads(r[1]))
    for r in c.execute(
        "select captured_at,payload_json from market_intel where kind='news' and captured_at>='2026-08-03'"
    )
]


def news_for(sym, d):
    nm = (names.get(sym) or "").title()
    words = [
        w
        for w in nm.split()
        if len(w) > 3
        and w.lower()
        not in ("limited", "india", "industries", "corp", "ltd", "and", "fin", "the", "ind")
    ]
    keys = {sym.lower()}
    if words:
        keys.add(words[0].lower())
    if len(words) >= 2:
        keys.add((words[0] + " " + words[1]).lower())
    if sym in SPECIAL:
        keys = {k.lower() for k in SPECIAL[sym]}
    lo = f"{d}T09:35"
    hi_prev = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y-%m-%dT15:00")
    out = []
    for cap, p in news_rows:
        if not (hi_prev <= cap <= lo):
            continue
        text = f" {p.get('title', '')} {p.get('summary', '')} ".lower()
        if any(k in text for k in keys):
            out.append((cap[:16], p.get("source"), p.get("title")))
    return out


feat = []
for r in rows:
    d = r["trade_date"]
    sym = r["symbol"]
    side = r["side"]
    sgn = 1 if side == "L" else -1
    per = load_day(d)
    f = {
        k: r[k]
        for k in (
            "id",
            "trade_date",
            "symbol",
            "side",
            "fill",
            "status",
            "reason",
            "watch_source",
            "gap_pct",
            "level",
            "baseline_vol",
            "cum_vol_at_trigger",
            "trigger_minute",
            "trigger_second",
            "trigger_price",
            "entry_minute_close",
            "pnl",
            "charges_inr",
            "opt_entry_premium",
            "opt_exit_premium",
            "entry_fill_price",
            "exit_fill_price",
            "quantity",
            "sizing_basis",
            "opt_entry_oi",
            "opt_lot_size",
            "opt_entry_bid",
            "opt_entry_ask",
            "exit_price",
            "cf_pnl",
        )
    }
    f["net"] = (r["pnl"] or 0) - (r["charges_inr"] or 0) if r["pnl"] is not None else None
    f["vol_ratio"] = (r["cum_vol_at_trigger"] or 0) / (r["baseline_vol"] or 1)
    f["trig_sec_of_day"] = (
        int(r["trigger_minute"][:2]) * 3600
        + int(r["trigger_minute"][3:]) * 60
        + (r["trigger_second"] or 0)
    )
    f["trig_min_from_open"] = (f["trig_sec_of_day"] - 9 * 3600 - 15 * 60) / 60
    f["beyond_level_pct"] = (
        sgn * (r["trigger_price"] / r["level"] - 1) * 100 if r["level"] else None
    )
    if r["opt_entry_ask"] and r["opt_entry_bid"]:
        f["opt_spread_pct"] = (
            (r["opt_entry_ask"] - r["opt_entry_bid"])
            / ((r["opt_entry_ask"] + r["opt_entry_bid"]) / 2)
            * 100
        )
    if r["opt_entry_oi"] and r["opt_lot_size"]:
        f["opt_oi_lots"] = r["opt_entry_oi"] / r["opt_lot_size"]
    f["tick_ok"] = False
    if per and sym in per:
        ticks = per[sym]
        bars = minute_bars(ticks)
        tt = f"{r['trigger_minute']}:{(r['trigger_second'] or 0):02d}"
        b15 = bars.get("09:15")
        if b15:
            rng = b15["h"] - b15["l"]
            f["c15_pos"] = (b15["c"] - b15["l"]) / rng if rng > 0 else 0.5
            f["c15_range_pct"] = rng / b15["o"] * 100
            f["c15_ret_pct"] = (b15["c"] / b15["o"] - 1) * 100
            f["c15_dir_with_side"] = sgn * f["c15_ret_pct"]
            f["open_px"] = b15["o"]
            if r["gap_pct"] is not None:
                pc = b15["o"] / (1 + r["gap_pct"] / 100)
                f["prev_close_est"] = pc
                f["trig_vs_prev_pct"] = sgn * (r["trigger_price"] / pc - 1) * 100
                f["level_vs_prev_pct"] = sgn * (r["level"] / pc - 1) * 100
        a = px_at(ticks, tt)
        if a:
            f["tick_ok"] = True
            tdt = datetime.strptime(tt, "%H:%M:%S")
            for w in (30, 60):
                tw = (tdt - timedelta(seconds=w)).strftime("%H:%M:%S")
                pw = px_at(ticks, tw)
                f[f"mom{w}_pct"] = sgn * (a[1] / pw[1] - 1) * 100 if pw else None
            p16 = px_at(ticks, "09:16:00")
            f["mom_from_c15_pct"] = sgn * (a[1] / p16[1] - 1) * 100 if p16 else None
            first_cross = None
            for ts, p, _v in ticks:
                if ts > tt:
                    break
                if ts >= "09:16:00" and sgn * (p - r["level"]) > 0:
                    first_cross = ts
                    break
            f["first_cross"] = first_cross
            if first_cross:
                f["secs_since_first_cross"] = (
                    tdt - datetime.strptime(first_cross, "%H:%M:%S")
                ).total_seconds()
            # fraction of ticks since 09:16 that were beyond the level (persistence)
            since = [(ts, p) for ts, p, v in ticks if "09:16:00" <= ts <= tt]
            if since:
                f["frac_beyond_level_pre"] = sum(
                    1 for ts, p in since if sgn * (p - r["level"]) > 0
                ) / len(since)
            tm = r["trigger_minute"]
            prior = [bars[m]["n"] for m in bars if "09:16" <= m < tm]
            if prior and tm in bars:
                f["tick_intensity"] = bars[tm]["n"] / st.mean(prior)
            if tm in bars:
                prevm = (datetime.strptime(tm, "%H:%M") - timedelta(minutes=1)).strftime("%H:%M")
                if prevm in bars and r["baseline_vol"]:
                    f["prev_min_vol_ratio"] = (bars[prevm]["v1"] - bars[prevm]["v0"]) / r[
                        "baseline_vol"
                    ]
            post = [(ts, p) for ts, p, v in ticks if tt < ts <= "09:30:05"]
            if post:
                ps = [p for _, p in post]
                f["mfe_pct"] = max(sgn * (p / a[1] - 1) for p in ps) * 100
                f["mae_pct"] = min(sgn * (p / a[1] - 1) for p in ps) * 100
                f["stock_ret_0930_pct"] = sgn * (post[-1][1] / a[1] - 1) * 100
                f["exit_px_0930"] = post[-1][1]
                for w in (60, 180):
                    te = (tdt + timedelta(seconds=w)).strftime("%H:%M:%S")
                    pe = px_at(ticks, te)
                    f[f"ret_{w}s_pct"] = sgn * (pe[1] / a[1] - 1) * 100 if pe else None
                f["retest_level"] = any(sgn * (p - r["level"]) <= 0 for p in ps)
                f["mfe_ts"] = max(post, key=lambda x: sgn * x[1])[0]
            med, upf, tot = universe_breadth(per, "09:15:05", tt)
            f["univ_med_ret_to_trig"] = med * 100 if med is not None else None
            f["univ_up_frac"] = upf
            med2, _, _ = universe_breadth(per, tt, "09:30:00")
            f["univ_med_ret_hold"] = med2 * 100 if med2 is not None else None
            if f.get("mom_from_c15_pct") is not None and f["univ_med_ret_to_trig"] is not None:
                f["rel_mom_pct"] = f["mom_from_c15_pct"] - sgn * f["univ_med_ret_to_trig"]
    f["news"] = news_for(sym, d)
    f["n_news"] = len(f["news"])
    feat.append(f)

keys = sorted({k for f in feat for k in f if k != "news"})
with open(os.path.join(S, "features.csv"), "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=keys + ["news"])
    w.writeheader()
    for f in feat:
        g = dict(f)
        g["news"] = " || ".join(f"{a} {b}: {t}" for a, b, t in f["news"])
        w.writerow(g)
json.dump(feat, open(os.path.join(S, "features.json"), "w"), default=str, indent=0)
print("written", len(feat), "tick_ok", sum(1 for f in feat if f["tick_ok"]))
