"""R81: earliest Kite daily bar per instrument class (read-only). -> data/data_depth.json"""
import json, os, sqlite3, sys, time
sys.path.insert(0, os.getcwd())
from database.auth_db import get_auth_token_broker, get_first_available_api_key
from utils.httpx_client import get_httpx_client
auth, _ = get_auth_token_broker(get_first_available_api_key())
cli = get_httpx_client(); H = {"X-Kite-Version": "3", "Authorization": f"token {auth}"}
sq = sqlite3.connect("C:/Users/Dheeraj/AppData/Local/Temp/claude/8a49aefb53802e1c/43628c04-66b4-49ef-b5b9-1418fb1cfefe/scratchpad/oa81.db")
def tok(ex, sym):
    r = sq.execute("select token from symtoken where exchange=? and symbol=?", (ex, sym)).fetchone()
    return int(str(r[0]).split("::::")[0]) if r else None
items = [("NSE_INDEX", s) for s in ("NIFTY", "BANKNIFTY", "NIFTYIT", "NIFTYPHARMA", "NIFTYMETAL", "NIFTYFMCG", "NIFTYAUTO", "NIFTYPSUBANK", "NIFTYPVTBANK", "FINNIFTY", "NIFTYENERGY", "NIFTYREALTY", "NIFTYMEDIA")]
items += [("NSE", s) for s in ("BANKBEES", "ITBEES", "PSUBNKBEES", "PHARMABEES", "FMCGIETF", "AUTOBEES", "NIFTYBEES", "RELIANCE", "INFY", "TATASTEEL")]
out = {}
for ex, s in items:
    t = tok(ex, s)
    if not t: out[s] = None; continue
    first = None
    for a, b in (("2000-01-01", "2004-12-31"), ("2005-01-01", "2009-12-31"), ("2010-01-01", "2014-12-31"), ("2015-01-01", "2019-12-31"), ("2020-01-01", "2024-12-31"), ("2025-01-01", "2026-11-13")):
        time.sleep(0.36)
        c = cli.get(f"https://api.kite.trade/instruments/historical/{t}/day", params={"from": a, "to": b}, headers=H).json().get("data") or {}; c = c.get("candles", [])
        if c: first = c[0][0][:10]; break
    out[s] = first; print(ex, s, first, flush=True)
# NIFTY continuous futures (daily continuous=1)
nf = sq.execute("select token from symtoken where exchange='NFO' and brsymbol='NIFTY26DECFUT'").fetchone()
t = int(str(nf[0]).split("::::")[0])
for a, b in (("2005-01-01", "2009-12-31"), ("2010-01-01", "2014-12-31")):
    c = cli.get(f"https://api.kite.trade/instruments/historical/{t}/day", params={"from": a, "to": b, "continuous": 1}, headers=H).json().get("data") or {}; c = c.get("candles", [])
    if c: out["NIFTY_FUT_continuous"] = c[0][0][:10]; break
print("NIFTY_FUT_continuous", out.get("NIFTY_FUT_continuous"))
json.dump(out, open("backtest/r81_survey/data/data_depth.json", "w"), indent=1)
