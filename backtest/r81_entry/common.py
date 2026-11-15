"""R81 shared helpers: data access (R81 fetch + R80 fetch), PIT universe, name mapping, charges."""
import datetime as dt
import json
from functools import lru_cache
from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).resolve().parent
D = HERE / "data"
R80_DB = HERE.parents[0] / "r80_scanner" / "data" / "bars_r80.duckdb"
R81_DB = D / "bars_r81.duckdb"
R80_LO = dt.date(2023, 12, 18)
TRAIN = (dt.date(2019, 1, 1), dt.date(2022, 12, 31))
VALID = (dt.date(2023, 1, 1), dt.date(2023, 12, 31))
HOLD = (dt.date(2024, 1, 1), dt.date(2025, 6, 19))
SLIP = 0.00024  # 2.4 bps per side

# PIT name -> name used in today's master / sector map (Kite token continuity)
REN = {"MCDOWELL-N": "UNITDSPR", "L&TFH": "LTF", "GMRINFRA": "GMRAIRPORT", "TATAMOTORS": "TMPV", "LTIM": "LTM",
       "LTI": "LTM", "ZOMATO": "ETERNAL", "CADILAHC": "ZYDUSLIFE", "SRTRANSFIN": "SHRIRAMFIN", "AMARAJABAT": "ARE&M",
       "NIITTECH": "COFORGE", "PVR": "PVRINOX", "IBULHSGFIN": "SAMMAANCAP", "ADANITRANS": "ADANIENSOL",
       "INFRATEL": "INDUSTOWER", "TATAGLOBAL": "TATACONSUM", "MOTHERSUMI": "MOTHERSON", "CENTURYTEX": "ABREL",
       "SHRIRAMCIT": "SHRIRAMFIN", "KPIT": "KPITTECH", "UJJIVAN": "UJJIVANSFB"}


def period_of(d):
    if TRAIN[0] <= d <= TRAIN[1]:
        return "train"
    if VALID[0] <= d <= VALID[1]:
        return "validate"
    if HOLD[0] <= d <= HOLD[1]:
        return "holdout"
    return None


@lru_cache(maxsize=1)
def universe_pit():
    return {k: set(v["stocks"]) for k, v in json.load(open(D / "universe_pit.json")).items()}


@lru_cache(maxsize=1)
def sector_of():
    m = json.load(open(HERE.parents[1] / "strategies" / "intraday_pullback_top2" / "sector_map.json"))["mapping"]
    return m


def sector_for(sym):
    m = sector_of()
    return m.get(sym) or m.get(REN.get(sym, ""))


_cons = {}


def _con(p):
    if p not in _cons:
        _cons[p] = duckdb.connect(str(p), read_only=True)
    return _cons[p]


@lru_cache(maxsize=1)
def r80_symbols():
    return {r[0] for r in _con(R80_DB).execute("select distinct symbol from prices_1m").fetchall()}


def load_1m(sym, a, b):
    """1m bars for sym, dates a..b inclusive, 09:15-15:29, columns ts,open,high,low,close,volume."""
    parts = []
    lo_r81 = a
    hi_r81 = b if sym not in r80_symbols() else min(b, R80_LO - dt.timedelta(days=1))
    if lo_r81 <= hi_r81:
        parts.append(_con(R81_DB).execute(
            "select ts,open,high,low,close,volume from m1 where symbol=? and ts>=? and ts<? order by ts",
            [sym, dt.datetime.combine(lo_r81, dt.time()), dt.datetime.combine(hi_r81 + dt.timedelta(days=1), dt.time())]).df())
    if sym in r80_symbols() and b >= R80_LO:
        parts.append(_con(R80_DB).execute(
            "select ts,open,high,low,close,volume from prices_1m where symbol=? and ts>=? and ts<? order by ts",
            [sym, dt.datetime.combine(max(a, R80_LO), dt.time()), dt.datetime.combine(b + dt.timedelta(days=1), dt.time())]).df())
    if not parts:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    m = pd.concat(parts, ignore_index=True).drop_duplicates("ts").sort_values("ts")
    t = m.ts.dt.time
    return m[(t >= dt.time(9, 15)) & (t <= dt.time(15, 29))].reset_index(drop=True)


def load_daily(sym):
    return _con(R81_DB).execute("select bar_date,open,high,low,close,volume from d1 where symbol=? order by bar_date",
                                [sym]).df()


def load_index_1m(name, a, b):
    return load_1m("IDX:" + name, a, b)


def charges_orders(orders):
    """Zerodha MIS, per-order Rs20 cap, exchange 0.00307% (#783). orders: [(is_buy, value)]."""
    bv = sum(v for b, v in orders if b)
    sv = sum(v for b, v in orders if not b)
    t = bv + sv
    brk = sum(round(min(20.0, 0.0003 * v), 2) for _, v in orders if v > 0)
    c = dict(brk=brk, stt=0.00025 * sv, exch=0.0000307 * t, sebi=0.000001 * t, stamp=0.00003 * bv)
    c["gst"] = 0.18 * (brk + c["exch"] + c["sebi"])
    c["total"] = sum(c.values())
    c["turnover"] = t
    return c
