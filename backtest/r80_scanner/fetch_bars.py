"""R80 step 2 data: Kite 1m + daily for the 2024-03-01 F&O stock list (approximate PIT universe).

Read-only on live services: Kite historical GETs with the stored session token, plus a BYTE COPY of
openalgo.db for instrument tokens. Writes data/bars_r80.duckdb only. Resumable (skips done symbols).

  PYTHONPATH=. .venv/Scripts/python.exe backtest/r80_scanner/fetch_bars.py <copy_of_openalgo.db>
"""
import datetime as dt
import json
import os
import sqlite3
import sys
import time

import duckdb
import pandas as pd

sys.path.insert(0, os.getcwd())
from database.auth_db import get_auth_token_broker, get_first_available_api_key  # noqa: E402
from utils.httpx_client import get_httpx_client  # noqa: E402

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RENAME = {"GMRINFRA": "GMRAIRPORT", "L&TFH": "LTF", "MCDOWELL-N": "UNITDSPR", "TATAMOTORS": "TMPV",
          "LTIM": "LTM"}
KNOWN_TOKEN = {"ICICIPRULI": 4774913, "GUJGASLTD": 2713345}  # absent from today's master; price-verified
EXCLUDE = {"IDFC": "merged into IDFCFIRSTB Oct-2024, no token", "PEL": "merged into PIRAMALFIN, new token"}
M1_FROM, M1_TO = dt.date(2023, 12, 18), dt.date(2025, 6, 20)
D_FROM, D_TO = dt.date(2022, 6, 1), dt.date(2025, 6, 30)

sq = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
U = json.load(open(f"{D}/universe.json"))["fo2024"]
tokens, notes = {}, {}
for s in U:
    if s in EXCLUDE:
        notes[s] = EXCLUDE[s]
        continue
    if s in KNOWN_TOKEN:
        tokens[s] = KNOWN_TOKEN[s]
        notes[s] = "known token (not in current master)"
        continue
    q = RENAME.get(s, s)
    r = sq.execute("select token from symtoken where exchange='NSE' and symbol=?", (q,)).fetchone()
    if r:
        tokens[s] = int(str(r[0]).split("::::")[0])
        if q != s:
            notes[s] = f"renamed -> {q}"
    else:
        notes[s] = "no token"
for idx in ("NIFTY 50", "NIFTY BANK"):
    pass
tokens["__NIFTY"] = 256265
json.dump(dict(tokens=tokens, notes=notes), open(f"{D}/tokens.json", "w"), indent=1)
print(f"tokens {len(tokens)} notes {notes}", flush=True)

auth, _ = get_auth_token_broker(get_first_available_api_key())
cli = get_httpx_client()
H = {"X-Kite-Version": "3", "Authorization": f"token {auth}"}
last = [0.0]


def get(tok, interval, a, b):
    for attempt in range(6):
        w = 0.36 - (time.time() - last[0])
        if w > 0:
            time.sleep(w)
        last[0] = time.time()
        r = cli.get(f"https://api.kite.trade/instruments/historical/{tok}/{interval}",
                    params={"from": f"{a} 09:00:00", "to": f"{b} 15:35:00"}, headers=H)
        if r.status_code == 200:
            return r.json()["data"]["candles"]
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{tok} {interval} {a}..{b}: {r.status_code} {r.text[:200]}")


con = duckdb.connect(f"{D}/bars_r80.duckdb")
con.execute("create table if not exists prices_daily(symbol varchar, bar_date date, open double, high double,"
            " low double, close double, volume bigint)")
con.execute("create table if not exists prices_1m(symbol varchar, ts timestamp, open double, high double,"
            " low double, close double, volume bigint)")
done_d = {r[0] for r in con.execute("select distinct symbol from prices_daily").fetchall()}
done_m = {r[0] for r in con.execute("select distinct symbol from prices_1m").fetchall()}
t0 = time.time()
for i, (s, tok) in enumerate(tokens.items(), 1):
    if s not in done_d:
        c = get(tok, "day", D_FROM, D_TO)
        if c:
            df = pd.DataFrame(c, columns=["ts", "open", "high", "low", "close", "volume"] + (["oi"] if len(c[0]) > 6 else []))
            df["bar_date"] = pd.to_datetime(df.ts.str[:10]).dt.date
            df["symbol"] = s
            con.execute("insert into prices_daily select symbol,bar_date,open,high,low,close,volume from df")
    if s not in done_m:
        rows = []
        a = M1_FROM
        while a <= M1_TO:
            b = min(a + dt.timedelta(days=59), M1_TO)
            rows += get(tok, "minute", a, b)
            a = b + dt.timedelta(days=1)
        if rows:
            df = pd.DataFrame([r[:6] for r in rows], columns=["ts", "open", "high", "low", "close", "volume"])
            df["ts"] = pd.to_datetime(df.ts.str[:19])
            df["symbol"] = s
            df = df.drop_duplicates("ts")
            con.execute("insert into prices_1m select symbol,ts,open,high,low,close,volume from df")
    print(f"{i}/{len(tokens)} {s} {time.time() - t0:.0f}s", flush=True)
con.close()
print("DONE", flush=True)
