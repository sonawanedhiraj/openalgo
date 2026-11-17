"""R85: quarterly financial-results broadcast timestamps for all NSE companies, 2018-12 -> 2026-11 (dates only,
no XBRL), via the NSE bulk filings list used by R50's harvester. -> data/results_dates.parquet"""

import datetime as dt
import sys
import time

import pandas as pd
import requests

sys.path.insert(0, "backtest/news_event_study")
import harvest_results_eps as H  # noqa: E402

s = requests.Session()
H.prime_session(s)
rows, a, end = [], dt.date(2018, 12, 1), dt.date(2026, 11, 13)
while a <= end:
    b = min(a + dt.timedelta(days=14), end)
    r = H.fetch_bulk_window(s, a, b)
    if r is None:
        print("FAIL", a, b, flush=True)
    else:
        for x in r:
            rows.append(
                {
                    k: x.get(k)
                    for k in (
                        "symbol",
                        "broadCastDate",
                        "filingDate",
                        "exchdisstime",
                        "relatingTo",
                        "period",
                        "consolidated",
                        "audited",
                    )
                }
            )
    a = b + dt.timedelta(days=1)
    time.sleep(0.8)
df = pd.DataFrame(rows).drop_duplicates()
df["ts"] = pd.to_datetime(df.broadCastDate, format="%d-%b-%Y %H:%M:%S", errors="coerce")
df.to_parquet("backtest/r85_options/data/results_dates.parquet", index=False)
print("rows", len(df), "symbols", df.symbol.nunique(), df.ts.min(), df.ts.max(), flush=True)
