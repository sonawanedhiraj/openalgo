"""R85 Phase 1: NSE daily F&O bhavcopies 2019-01-01 -> 2026-11-13 (legacy fo<DD><MON><YYYY>bhav.csv.zip before
2024-07-08, UDiFF BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip after; each falls back to the other).
Keeps stock options + stock futures only, normalised to one schema -> data/fo_<year>.parquet. Resumable
(raw zips kept in data/raw/, gitignored). Public NSE archive, browser UA, 0.5s between requests.
  uv run python backtest/r85_options/fetch_bhav.py [start YYYY-MM-DD] [end YYYY-MM-DD]"""

import datetime as dt
import io
import os
import sys
import time
import zipfile

import httpx
import pandas as pd

D = "backtest/r85_options/data"
H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
UDIFF_FROM = dt.date(2024, 7, 8)


def urls(d):
    leg = f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d.year}/{MON[d.month - 1]}/fo{d:%d}{MON[d.month - 1]}{d.year}bhav.csv.zip"
    udf = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    return [leg, udf] if d < UDIFF_FROM else [udf, leg]


def parse(raw, d):
    z = zipfile.ZipFile(io.BytesIO(raw))
    df = pd.read_csv(z.open(z.namelist()[0]), low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    if "INSTRUMENT" in df:
        df = df[df.INSTRUMENT.str.strip().isin(["OPTSTK", "FUTSTK"])]
        out = pd.DataFrame(
            {
                "date": d,
                "kind": df.INSTRUMENT.str.strip().map({"OPTSTK": "OPT", "FUTSTK": "FUT"}),
                "symbol": df.SYMBOL.str.strip(),
                "expiry": pd.to_datetime(df.EXPIRY_DT.str.strip(), format="%d-%b-%Y").dt.date,
                "strike": df.STRIKE_PR.astype(float),
                "opt": df.OPTION_TYP.str.strip().replace({"XX": None}),
                "open": df.OPEN,
                "high": df.HIGH,
                "low": df.LOW,
                "close": df.CLOSE,
                "settle": df.SETTLE_PR,
                "contracts": df.CONTRACTS,
                "value_lakh": df.VAL_INLAKH,
                "oi": df.OPEN_INT,
                "lot": pd.NA,
                "underlying": pd.NA,
                "fmt": "legacy",
            }
        )
    else:
        df = df[df.FinInstrmTp.str.strip().isin(["STO", "STF"])]
        lot = pd.to_numeric(df.get("NewBrdLotQty"), errors="coerce")
        vol = pd.to_numeric(df.TtlTradgVol, errors="coerce")
        out = pd.DataFrame(
            {
                "date": d,
                "kind": df.FinInstrmTp.str.strip().map({"STO": "OPT", "STF": "FUT"}),
                "symbol": df.TckrSymb.str.strip(),
                "expiry": pd.to_datetime(df.XpryDt.str.strip()).dt.date,
                "strike": pd.to_numeric(df.StrkPric, errors="coerce").fillna(0.0),
                "opt": df.OptnTp.where(df.OptnTp.notna(), None),
                "open": df.OpnPric,
                "high": df.HghPric,
                "low": df.LwPric,
                "close": df.ClsPric,
                "settle": df.SttlmPric,
                "contracts": vol,
                "value_lakh": pd.to_numeric(df.TtlTrfVal, errors="coerce") / 1e5,
                "oi": df.OpnIntrst,
                "lot": lot,
                "underlying": pd.to_numeric(df.UndrlygPric, errors="coerce"),
                "fmt": "udiff",
            }
        )
    return out


def main(start, end):
    os.makedirs(f"{D}/raw", exist_ok=True)
    days = [d.date() for d in pd.bdate_range(start, end)]
    frames, cur_year, log = [], None, []
    with httpx.Client(headers=H, timeout=45, follow_redirects=True) as c:
        for d in days:
            if cur_year is not None and d.year != cur_year and frames:
                pd.concat(frames).to_parquet(f"{D}/fo_{cur_year}.parquet", index=False)
                frames = []
            cur_year = d.year
            p = f"{D}/raw/{d:%Y%m%d}.zip"
            raw = open(p, "rb").read() if os.path.exists(p) else None
            src = "cache" if raw else None
            if raw is None and not os.path.exists(p + ".none"):
                for u in urls(d):
                    for k in range(3):
                        time.sleep(0.5)
                        try:
                            r = c.get(u)
                        except Exception:  # nosec B112 - retry / next URL
                            time.sleep(2 * (k + 1))
                            continue
                        if r.status_code == 200 and r.content[:2] == b"PK":
                            raw, src = r.content, u.rsplit("/", 1)[-1][:8]
                            open(p, "wb").write(raw)
                        break
                    if raw:
                        break
                if raw is None:
                    open(p + ".none", "w").close()  # holiday / not published
            if raw:
                try:
                    f = parse(raw, d)
                    frames.append(f)
                    log.append((d, src, len(f)))
                except Exception as e:  # noqa: BLE001
                    log.append((d, f"parse_error {e}", 0))
            if len(log) % 50 == 0 and log:
                print(log[-1], flush=True)
        if frames:
            pd.concat(frames).to_parquet(f"{D}/fo_{cur_year}.parquet", index=False)
    pd.DataFrame(log, columns=["date", "src", "rows"]).to_csv(
        f"{D}/fetch_log_{start}_{end}.csv", index=False
    )
    print("DONE", len(log), flush=True)


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "2019-01-01"
    b = sys.argv[2] if len(sys.argv) > 2 else "2026-11-13"
    main(a, b)
