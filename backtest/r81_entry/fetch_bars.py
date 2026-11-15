"""R81 data: Kite 1m (membership months + 35-day warm-up) and daily for the PIT F&O union, plus NIFTY and the
sector indices. Reuses R80's bars for 2023-12-18..2025-06-20. Resumable. Read-only on live services.
  PYTHONPATH=. .venv/Scripts/python.exe backtest/r81_entry/fetch_bars.py <byte-copy of openalgo.db>"""
import datetime as dt, json, os, sqlite3, sys, time
import duckdb, pandas as pd
sys.path.insert(0, os.getcwd())
from database.auth_db import get_auth_token_broker, get_first_available_api_key
from utils.httpx_client import get_httpx_client
D = "backtest/r81_entry/data"
U = json.load(open(f"{D}/universe_pit.json")); T = json.load(open(f"{D}/tokens_pit.json"))["resolved"]
SM = json.load(open("strategies/intraday_pullback_top2/sector_map.json"))
sectors = sorted(set(SM["backfilled_sectors"]))
sq = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
LO, HI = dt.date(2018, 12, 1), dt.date(2025, 6, 20)
R80_LO = dt.date(2023, 12, 18)
r80 = set(r[0] for r in duckdb.connect("backtest/r80_scanner/data/bars_r80.duckdb", read_only=True)
          .execute("select distinct symbol from prices_1m").fetchall())
def ranges(sym):
    ms = sorted(k for k, v in U.items() if sym in v["stocks"])
    out = []
    for k in ms:
        y, m = map(int, k.split("-"))
        a = dt.date(y, m, 1) - dt.timedelta(days=35)
        b = (dt.date(y + (m == 12), 1 if m == 12 else m + 1, 1) - dt.timedelta(days=1))
        a, b = max(a, LO), min(b, HI)
        if sym in r80: b = min(b, R80_LO - dt.timedelta(days=1))
        if a <= b: out.append([a, b])
    out.sort(); merged = []
    for a, b in out:
        if merged and a <= merged[-1][1] + dt.timedelta(days=1): merged[-1][1] = max(merged[-1][1], b)
        else: merged.append([a, b])
    return merged
jobs = [(s, v["token"], ranges(s)) for s, v in T.items()]
for name in sectors + ["NIFTY"]:
    r = sq.execute("select token from symtoken where exchange='NSE_INDEX' and symbol=?", (name,)).fetchone()
    if r: jobs.append(("IDX:" + name, int(str(r[0]).split("::::")[0]), [[LO, HI]]))
    else: print("no index token", name, flush=True)
auth, _ = get_auth_token_broker(get_first_available_api_key())
cli = get_httpx_client(); H = {"X-Kite-Version": "3", "Authorization": f"token {auth}"}
last = [0.0]
def get(tok, iv, a, b):
    for k in range(6):
        w = 0.36 - (time.time() - last[0])
        if w > 0: time.sleep(w)
        last[0] = time.time()
        r = cli.get(f"https://api.kite.trade/instruments/historical/{tok}/{iv}",
                    params={"from": f"{a} 09:00:00", "to": f"{b} 15:35:00"}, headers=H)
        if r.status_code == 200: return r.json()["data"]["candles"]
        time.sleep(1.5 * (k + 1))
    print("FAIL", tok, iv, a, b, r.status_code, r.text[:120], flush=True); return []
con = duckdb.connect(f"{D}/bars_r81.duckdb")
con.execute("create table if not exists m1(symbol varchar, ts timestamp, open double, high double, low double, close double, volume bigint)")
con.execute("create table if not exists d1(symbol varchar, bar_date date, open double, high double, low double, close double, volume bigint)")
con.execute("create table if not exists done(symbol varchar, kind varchar)")
done = set(con.execute("select symbol||'|'||kind from done").fetchall()); done = {x[0] for x in done}
t0 = time.time()
for i, (s, tok, rgs) in enumerate(jobs, 1):
    if f"{s}|d" not in done:
        c = get(tok, "day", dt.date(2018, 1, 1), dt.date(2022, 12, 31)) + get(tok, "day", dt.date(2023, 1, 1), dt.date(2025, 6, 30))
        if c:
            df = pd.DataFrame([x[:6] for x in c], columns=["ts", "open", "high", "low", "close", "volume"])
            df["bar_date"] = pd.to_datetime(df.ts.str[:10]).dt.date; df["symbol"] = s
            df = df.drop_duplicates("bar_date")
            con.execute("insert into d1 select symbol,bar_date,open,high,low,close,volume from df")
        con.execute("insert into done values (?, 'd')", [s])
    if f"{s}|m" not in done:
        rows = []
        for a, b in rgs:
            while a <= b:
                e = min(a + dt.timedelta(days=59), b); rows += get(tok, "minute", a, e); a = e + dt.timedelta(days=1)
        if rows:
            df = pd.DataFrame([x[:6] for x in rows], columns=["ts", "open", "high", "low", "close", "volume"])
            df["ts"] = pd.to_datetime(df.ts.str[:19]); df["symbol"] = s; df = df.drop_duplicates("ts")
            con.execute("insert into m1 select symbol,ts,open,high,low,close,volume from df")
        con.execute("insert into done values (?, 'm')", [s])
    print(f"{i}/{len(jobs)} {s} {time.time()-t0:.0f}s", flush=True)
print("DONE", flush=True)
