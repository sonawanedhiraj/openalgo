"""R81 Phase 2b: CNC legs priced SEPARATELY (a same-day buy+sell pair is taxed as intraday). -> data/cost_floors_cnc.json"""
import json, os, sys
sys.path.insert(0, os.getcwd())
from database.auth_db import get_auth_token_broker, get_first_available_api_key
from utils.httpx_client import get_httpx_client
auth, _ = get_auth_token_broker(get_first_available_api_key())
cli = get_httpx_client(); H = {"X-Kite-Version": "3", "Authorization": f"token {auth}"}
L = json.load(open("backtest/r81_survey/data/cost_floors.json"))["ltp"]
cases = [("equity_CNC_50k", "RELIANCE", 50000), ("equity_CNC_1L", "RELIANCE", 100000), ("equity_CNC_5L", "RELIANCE", 500000),
         ("ETF_CNC_50k_BANKBEES", "BANKBEES", 50000), ("ETF_CNC_1L_BANKBEES", "BANKBEES", 100000), ("ETF_CNC_50k_ITBEES", "ITBEES", 50000)]
out = {}
for name, sym, notional in cases:
    px = L[f"NSE:{sym}"]; q = round(notional / px)
    d = dict(notional=round(q * px), legs={})
    for side in ("BUY", "SELL"):
        leg = dict(order_id=f"{name}|{side}", exchange="NSE", tradingsymbol=sym, transaction_type=side, variety="regular",
                   product="CNC", order_type="MARKET", quantity=q, average_price=px)
        a = cli.post("https://api.kite.trade/charges/orders", headers=H, json=[leg]).json()["data"][0]["charges"]
        d["legs"][side] = dict(brokerage=a["brokerage"], stt=a["transaction_tax"], exch=a["exchange_turnover_charge"],
                               sebi=a["sebi_turnover_charge"], stamp=a["stamp_duty"], gst=a["gst"]["total"], total=a["total"])
    t = d["legs"]["BUY"]["total"] + d["legs"]["SELL"]["total"]
    d["round_trip_inr_ex_dp"] = round(t, 2); d["bps_ex_dp"] = round(1e4 * t / d["notional"], 2)
    d["round_trip_inr_with_dp"] = round(t + 15.34, 2); d["bps_with_dp"] = round(1e4 * (t + 15.34) / d["notional"], 2)
    out[name] = d
    print(f"{name:22s} {d['notional']:>8,}  ex-DP Rs{t:7.2f} {d['bps_ex_dp']:6.2f} bps | +DP {d['bps_with_dp']:6.2f} bps  ",
          {k: round(d['legs']['BUY'][k] + d['legs']['SELL'][k], 2) for k in ("brokerage", "stt", "exch", "stamp", "gst")})
json.dump(out, open("backtest/r81_survey/data/cost_floors_cnc.json", "w"), indent=1)
