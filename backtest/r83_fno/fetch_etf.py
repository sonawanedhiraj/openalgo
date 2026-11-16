"""R83: Kite daily bars for the sector-rotation ETF universe + NIFTYBEES (read-only). -> data/etf_daily.parquet
uv run python backtest/r83_fno/fetch_etf.py <byte-copy of openalgo.db>"""

import datetime as dt
import os
import sqlite3
import sys
import time

import pandas as pd

sys.path.insert(0, os.getcwd())
from database.auth_db import get_auth_token_broker, get_first_available_api_key  # noqa: E402
from utils.httpx_client import get_httpx_client  # noqa: E402

ETFS = [
    "BANKBEES",
    "ITBEES",
    "PSUBNKBEES",
    "PHARMABEES",
    "METALIETF",
    "PVTBANIETF",
    "FMCGIETF",
    "AUTOBEES",
    "HEALTHIETF",
    "NIFTYBEES",
]
sq = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
auth, _ = get_auth_token_broker(get_first_available_api_key())
cli = get_httpx_client()
H = {"X-Kite-Version": "3", "Authorization": f"token {auth}"}
rows = []
for e in ETFS:
    r = sq.execute("select token from symtoken where exchange='NSE' and symbol=?", (e,)).fetchone()
    if not r:
        print("no token", e)
        continue
    tok = int(str(r[0]).split("::::")[0])
    for a, b in (
        (dt.date(2015, 1, 1), dt.date(2019, 12, 31)),
        (dt.date(2020, 1, 1), dt.date(2024, 12, 31)),
        (dt.date(2025, 1, 1), dt.date(2026, 11, 13)),
    ):
        time.sleep(0.4)
        resp = cli.get(
            f"https://api.kite.trade/instruments/historical/{tok}/day",
            params={"from": f"{a} 09:00:00", "to": f"{b} 15:35:00"},
            headers=H,
        )
        for c in resp.json().get("data", {}).get("candles", []):
            rows.append((e, c[0][:10], c[1], c[2], c[3], c[4], c[5]))
    print(e, sum(1 for x in rows if x[0] == e))
df = pd.DataFrame(
    rows, columns=["symbol", "date", "open", "high", "low", "close", "volume"]
).drop_duplicates(["symbol", "date"])
df.to_parquet("backtest/r83_fno/data/etf_daily.parquet", index=False)
print(df.groupby("symbol").date.agg(["min", "max", "count"]))
