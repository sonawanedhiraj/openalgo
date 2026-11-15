"""R81 Phase 2: exact Zerodha charges per vehicle from Kite /charges/orders (a calculator; places no order).
Read-only. -> data/cost_floors.json"""
import json, os, sys
sys.path.insert(0, os.getcwd())
from database.auth_db import get_auth_token_broker, get_first_available_api_key
from utils.httpx_client import get_httpx_client
auth, _ = get_auth_token_broker(get_first_available_api_key())
cli = get_httpx_client(); H = {"X-Kite-Version": "3", "Authorization": f"token {auth}"}
ins = ["NSE:RELIANCE", "NSE:TATASTEEL", "NSE:BANKBEES", "NSE:ITBEES", "NFO:NIFTY26DECFUT", "NFO:RELIANCE26DECFUT", "NFO:TATASTEEL26DECFUT"]
ltp = {k: v["last_price"] for k, v in cli.get("https://api.kite.trade/quote/ltp", params={"i": ins}, headers=H).json()["data"].items()}
print(ltp)
cases = [  # name, exchange, symbol, product, qty
    ("equity_MIS_86k", "NSE", "RELIANCE", "MIS", round(86000 / ltp["NSE:RELIANCE"])),
    ("equity_MIS_5L", "NSE", "RELIANCE", "MIS", round(500000 / ltp["NSE:RELIANCE"])),
    ("equity_CNC_50k", "NSE", "RELIANCE", "CNC", round(50000 / ltp["NSE:RELIANCE"])),
    ("equity_CNC_1L", "NSE", "RELIANCE", "CNC", round(100000 / ltp["NSE:RELIANCE"])),
    ("equity_CNC_5L", "NSE", "RELIANCE", "CNC", round(500000 / ltp["NSE:RELIANCE"])),
    ("ETF_CNC_50k_BANKBEES", "NSE", "BANKBEES", "CNC", round(50000 / ltp["NSE:BANKBEES"])),
    ("ETF_CNC_50k_ITBEES", "NSE", "ITBEES", "CNC", round(50000 / ltp["NSE:ITBEES"])),
    ("NIFTY_fut_1lot", "NFO", "NIFTY26DECFUT", "NRML", 65),
    ("RELIANCE_fut_1lot", "NFO", "RELIANCE26DECFUT", "NRML", 500),
    ("TATASTEEL_fut_1lot", "NFO", "TATASTEEL26DECFUT", "NRML", 2750),
]
legs = []
for name, ex, sym, prod, q in cases:
    px = ltp[f"{ex}:{sym}"]
    for side in ("BUY", "SELL"):
        legs.append(dict(order_id=f"{name}|{side}", exchange=ex, tradingsymbol=sym, transaction_type=side,
                         variety="regular", product=prod, order_type="MARKET", quantity=int(q), average_price=float(px)))
r = cli.post("https://api.kite.trade/charges/orders", headers=H, json=legs).json()
out = {}
for leg, a in zip(legs, r["data"], strict=True):
    name, side = leg["order_id"].split("|")
    ch = a["charges"]
    d = out.setdefault(name, dict(notional=round(leg["quantity"] * leg["average_price"], 0), qty=leg["quantity"],
                                  price=leg["average_price"], legs={}))
    d["legs"][side] = dict(brokerage=ch["brokerage"], stt=ch["transaction_tax"], exch=ch["exchange_turnover_charge"],
                           sebi=ch["sebi_turnover_charge"], stamp=ch["stamp_duty"], gst=ch["gst"]["total"], total=ch["total"])
for name, d in out.items():
    t = d["legs"]["BUY"]["total"] + d["legs"]["SELL"]["total"]
    d["round_trip_inr"] = round(t, 2)
    d["round_trip_bps_of_notional"] = round(1e4 * t / d["notional"], 2)
    print(f"{name:24s} notional {d['notional']:>10,.0f}  RT Rs{t:8.2f}  {d['round_trip_bps_of_notional']:6.2f} bps   "
          + " ".join(f"{k}={d['legs']['BUY'][k] + d['legs']['SELL'][k]:.2f}" for k in ("brokerage", "stt", "exch", "sebi", "stamp", "gst")))
json.dump(dict(ltp=ltp, cases=out), open("backtest/r81_survey/data/cost_floors.json", "w"), indent=1)
