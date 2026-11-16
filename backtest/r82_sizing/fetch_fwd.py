"""R82 forward window data (2025-06-20..2026-11-13, never used under the new sizing): NSE month-end F&O lists for
trading months 2025-06..2026-11, token resolution from a byte-copy of openalgo.db, Kite 1m (2025-04-01..2026-11-13,
warm-up included) + daily (2018-01..2026-11-13) into data/bars_fwd.duckdb. Resumable. Read-only on live services.
  uv run python backtest/r82_sizing/fetch_fwd.py <byte-copy of openalgo.db>"""

import calendar
import datetime as dt
import io
import json
import os
import sqlite3
import sys
import time
import zipfile

import duckdb
import httpx
import pandas as pd

sys.path.insert(0, os.getcwd())
D = "backtest/r82_sizing/data"
R81 = "backtest/r81_entry/data"
os.makedirs(f"{D}/nse_fo", exist_ok=True)
H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def urls(d):
    return [
        f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip",
        f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d.year}/{MON[d.month - 1]}/fo{d:%d}{MON[d.month - 1]}{d.year}bhav.csv.zip",
    ]


def stocks(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    df = pd.read_csv(z.open(z.namelist()[0]))
    if "INSTRUMENT" in df:
        return sorted(df[df.INSTRUMENT.str.strip() == "FUTSTK"].SYMBOL.str.strip().unique())
    return sorted(df[df.FinInstrmTp.str.strip() == "STF"].TckrSymb.str.strip().unique())


LO1, HI = dt.date(2025, 4, 1), dt.date(2026, 11, 13)
up = f"{D}/universe_fwd.json"
if not os.path.exists(up):
    U = {"2025-06": json.load(open(f"{R81}/universe_pit.json"))["2025-06"]}
    y, m = 2025, 6
    with httpx.Client(headers=H, timeout=30, follow_redirects=True) as c:
        while (y, m) <= (2026, 10):
            last = calendar.monthrange(y, m)[1]
            got = None
            for k in range(8):
                d = dt.date(y, m, last) - dt.timedelta(days=k)
                if d.weekday() >= 5:
                    continue
                p = f"{D}/nse_fo/{d:%Y%m%d}.zip"
                if os.path.exists(p):
                    got = (d, open(p, "rb").read())
                    break
                for u in urls(d):
                    time.sleep(0.6)
                    try:
                        r = c.get(u)
                    except Exception:  # nosec B112 - try the next archive URL
                        continue
                    if r.status_code == 200 and r.content[:2] == b"PK":
                        open(p, "wb").write(r.content)
                        got = (d, r.content)
                        break
                if got:
                    break
            nm = (y + (m == 12), 1 if m == 12 else m + 1)
            if got:
                U[f"{nm[0]}-{nm[1]:02d}"] = {
                    "source_date": got[0].isoformat(),
                    "stocks": stocks(got[1]),
                }
                print(
                    f"{y}-{m:02d} -> {got[0]} {len(U[f'{nm[0]}-{nm[1]:02d}']['stocks'])}",
                    flush=True,
                )
            else:
                print(f"{y}-{m:02d} NOT FOUND", flush=True)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    json.dump(U, open(up, "w"), indent=0)
U = json.load(open(up))
sq = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
old = json.load(open(f"{R81}/tokens_pit.json"))["resolved"]
REN = {"TATAMOTORS": "TMPV", "LTIM": "LTM", "ZOMATO": "ETERNAL"}
tok, unres = {}, []
for s in sorted({x for v in U.values() for x in v["stocks"]}):
    if s in old:
        tok[s] = old[s]["token"]
        continue
    r = sq.execute(
        "select token from symtoken where exchange='NSE' and symbol=?", (REN.get(s, s),)
    ).fetchone()
    if r:
        tok[s] = int(str(r[0]).split("::::")[0])
    else:
        unres.append(s)
json.dump({"resolved": tok, "unresolved": unres}, open(f"{D}/tokens_fwd.json", "w"), indent=0)
print("symbols", len(tok), "unresolved", unres, flush=True)
SM = json.load(open("strategies/intraday_pullback_top2/sector_map.json"))
jobs = list(tok.items())
for name in sorted(set(SM["backfilled_sectors"])) + ["NIFTY"]:
    r = sq.execute(
        "select token from symtoken where exchange='NSE_INDEX' and symbol=?", (name,)
    ).fetchone()
    if r:
        jobs.append(("IDX:" + name, int(str(r[0]).split("::::")[0])))
from database.auth_db import get_auth_token_broker, get_first_available_api_key
from utils.httpx_client import get_httpx_client

auth, _ = get_auth_token_broker(get_first_available_api_key())
cli = get_httpx_client()
HK = {"X-Kite-Version": "3", "Authorization": f"token {auth}"}
last = [0.0]


def get(t, iv, a, b):
    for k in range(6):
        w = 0.36 - (time.time() - last[0])
        if w > 0:
            time.sleep(w)
        last[0] = time.time()
        r = cli.get(
            f"https://api.kite.trade/instruments/historical/{t}/{iv}",
            params={"from": f"{a} 09:00:00", "to": f"{b} 15:35:00"},
            headers=HK,
        )
        if r.status_code == 200:
            return r.json()["data"]["candles"]
        time.sleep(1.5 * (k + 1))
    print("FAIL", t, iv, a, b, r.status_code, r.text[:120], flush=True)
    return []


con = duckdb.connect(f"{D}/bars_fwd.duckdb")
con.execute(
    "create table if not exists m1(symbol varchar, ts timestamp, open double, high double, low double, close double, volume bigint)"
)
con.execute(
    "create table if not exists d1(symbol varchar, bar_date date, open double, high double, low double, close double, volume bigint)"
)
con.execute("create table if not exists done(symbol varchar, kind varchar)")
done = {x[0] for x in con.execute("select symbol||'|'||kind from done").fetchall()}
t0 = time.time()
for i, (s, t) in enumerate(jobs, 1):
    if f"{s}|d" not in done:
        c = get(t, "day", dt.date(2018, 1, 1), dt.date(2022, 12, 31)) + get(
            t, "day", dt.date(2023, 1, 1), HI
        )
        if c:
            df = pd.DataFrame(
                [x[:6] for x in c], columns=["ts", "open", "high", "low", "close", "volume"]
            )
            df["bar_date"] = pd.to_datetime(df.ts.str[:10]).dt.date
            df["symbol"] = s
            df = df.drop_duplicates("bar_date")
            con.execute("insert into d1 select symbol,bar_date,open,high,low,close,volume from df")
        con.execute("insert into done values (?, 'd')", [s])
    if f"{s}|m" not in done:
        rows, a = [], LO1
        while a <= HI:
            e = min(a + dt.timedelta(days=59), HI)
            rows += get(t, "minute", a, e)
            a = e + dt.timedelta(days=1)
        if rows:
            df = pd.DataFrame(
                [x[:6] for x in rows], columns=["ts", "open", "high", "low", "close", "volume"]
            )
            df["ts"] = pd.to_datetime(df.ts.str[:19])
            df["symbol"] = s
            df = df.drop_duplicates("ts")
            con.execute("insert into m1 select symbol,ts,open,high,low,close,volume from df")
        con.execute("insert into done values (?, 'm')", [s])
    print(f"{i}/{len(jobs)} {s} {time.time() - t0:.0f}s", flush=True)
print("DONE", flush=True)
