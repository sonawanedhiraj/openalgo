"""R81: point-in-time F&O stock universe from NSE's public month-end F&O bhavcopies (approved download).
For trading month M the universe = FUTSTK underlyings in the bhavcopy of the last trading day of month M-1.
-> data/nse_fo/*.zip, data/universe_pit.json"""
import calendar, datetime as dt, io, json, os, time, zipfile
import httpx, pandas as pd
D = "backtest/r81_entry/data"
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
     "Accept": "*/*", "Referer": "https://www.nseindia.com/"}
MON = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
def urls(d):
    return [f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d.year}/{MON[d.month-1]}/fo{d:%d}{MON[d.month-1]}{d.year}bhav.csv.zip",
            f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"]
def stocks(raw):
    z = zipfile.ZipFile(io.BytesIO(raw)); df = pd.read_csv(z.open(z.namelist()[0]))
    if "INSTRUMENT" in df: return sorted(df[df.INSTRUMENT.str.strip() == "FUTSTK"].SYMBOL.str.strip().unique())
    return sorted(df[df.FinInstrmTp.str.strip() == "STF"].TckrSymb.str.strip().unique())
out = {}
y, m = 2018, 12
with httpx.Client(headers=H, timeout=30, follow_redirects=True) as c:
    while (y, m) <= (2025, 5):
        last = calendar.monthrange(y, m)[1]; got = None
        for k in range(0, 8):
            d = dt.date(y, m, last) - dt.timedelta(days=k)
            if d.weekday() >= 5: continue
            p = f"{D}/nse_fo/{d:%Y%m%d}.zip"
            if os.path.exists(p): got = (d, open(p, "rb").read()); break
            for u in urls(d):
                time.sleep(0.6)
                try: r = c.get(u)
                except Exception: continue
                if r.status_code == 200 and r.content[:2] == b"PK":
                    open(p, "wb").write(r.content); got = (d, r.content); break
            if got: break
        if got:
            nm = (y + (m == 12), 1 if m == 12 else m + 1)
            out[f"{nm[0]}-{nm[1]:02d}"] = dict(source_date=got[0].isoformat(), stocks=stocks(got[1]))
            print(f"{y}-{m:02d} -> {got[0]} {len(out[f'{nm[0]}-{nm[1]:02d}']['stocks'])}", flush=True)
        else:
            print(f"{y}-{m:02d} NOT FOUND", flush=True)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
json.dump(out, open(f"{D}/universe_pit.json", "w"), indent=0)
allu = sorted(set(s for v in out.values() for s in v["stocks"]))
print("months", len(out), "union", len(allu))
