"""R85 option-structure backtest engine (daily closes only). See PREREGISTRATION.md for every rule.

Prices: a contract is TRADEABLE on a day only if it traded (contracts >= 1); entries require that plus the
liquidity floor. Marks/exits use the close when traded, else the exchange settlement price (theoretical) with a
doubled spread penalty. Spot = front-month stock-futures close from the same bhavcopy (UNADJUSTED, matches
strikes); volatility = 60d realised vol of Kite split-adjusted daily returns.
"""

import glob
import math
from functools import lru_cache
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

D = Path(__file__).resolve().parent / "data"
ROOT = D.parents[2]
GLOB = str(D / "fo_20*.parquet").replace("\\", "/")
EXIT_BEFORE_EXPIRY = (
    5  # exit at the close of E-5 (before NSE's E-4.. physical-delivery margin ramp)
)
DELIVERY_RAMP = {
    4: 0.10,
    3: 0.25,
    2: 0.45,
    1: 0.70,
}  # E-k -> % of contract value on ITM shorts (O4 only)
SPREAD = {
    "zero": (0.0, 0.0),
    "central": (0.015, 0.05),
    "pess": (0.03, 0.10),
}  # half-spread per leg: (pct, floor Rs)


# ------------------------------------------------------------------ costs (Kite /charges/orders, 2026-11-16)
def opt_charges(premium, qty, sell):
    v = premium * qty
    brk = 20.0 if v > 0 else 0.0
    exch, sebi = 0.0003553 * v, 0.000001 * v
    gst = 0.18 * (brk + exch + sebi)
    return brk + exch + sebi + gst + (0.0015 * v if sell else 0.00003 * v)


def half_spread(p, scen):
    pct, floor = SPREAD[scen]
    return 0.0 if pct == 0 else max(pct * p, floor)


# ------------------------------------------------------------------ margin (fit to Kite /margins/basket, 2026-11-16)
def margin_naked(S, K, lot, opt):
    otm = max((K / S - 1) if opt == "CE" else (1 - K / S), 0.0)
    itm = max((S / K - 1) if opt == "CE" else (K / S - 1), 0.0)
    a, b = (0.18, 0.55) if opt == "CE" else (0.17, 0.70)
    return S * lot * max(a - b * otm + itm, 0.08)


def margin_strangle(S, Kp, Kc, lot):
    mp, mc = margin_naked(S, Kp, lot, "PE"), margin_naked(S, Kc, lot, "CE")
    return max(mp, mc) + 0.25 * min(mp, mc)


def margin_spread(width, lot):
    return 1.4 * width * lot


# ------------------------------------------------------------------ data
_con = None


def con():
    global _con
    if _con is None:
        _con = duckdb.connect()
        _con.execute(
            "create view fo as select date, kind, symbol, expiry, cast(strike as double) strike, opt, "
            'cast("open" as double) "open", cast(high as double) high, cast(low as double) low, cast("close" as double) "close", '
            "cast(settle as double) settle, cast(contracts as double) contracts, cast(value_lakh as double) value_lakh, "
            "cast(oi as double) oi, cast(lot as double) lot, cast(underlying as double) underlying, fmt "
            f"from read_parquet('{GLOB}', union_by_name=true)"
        )
    return _con


@lru_cache(maxsize=1)
def calendar():
    d = con().execute("select distinct date from fo order by date").df().date
    return pd.DatetimeIndex(pd.to_datetime(d))


@lru_cache(maxsize=1)
def futures():
    """symbol, date, expiry, close, contracts for stock futures (all expiries)."""
    f = (
        con()
        .execute(
            "select symbol, date, expiry, close, contracts, value_lakh, lot from fo where kind='FUT'"
        )
        .df()
    )
    f["date"], f["expiry"] = pd.to_datetime(f.date), pd.to_datetime(f.expiry)
    return f


@lru_cache(maxsize=1)
def spot_and_lot():
    """(symbol,date) -> front-month futures close (unadjusted) and lot size (UDiFF lot, else traded-value estimate)."""
    f = futures().sort_values("expiry")
    g = f.groupby(["symbol", "date"]).first().reset_index()
    est = g.value_lakh * 1e5 / (g.contracts.replace(0, np.nan) * g.close)
    g["lot_est"] = (est / 25).round() * 25
    g["lot"] = pd.to_numeric(g.lot, errors="coerce").fillna(g.lot_est)
    g["lot"] = g.groupby("symbol").lot.transform(lambda s: s.ffill().bfill())
    return g.set_index(["symbol", "date"])[["close", "lot"]].rename(columns={"close": "spot"})


@lru_cache(maxsize=1)
def expiries():
    """symbol -> sorted monthly expiries seen in the data."""
    f = futures()
    return f.groupby("symbol").expiry.apply(lambda s: sorted(set(s))).to_dict()


@lru_cache(maxsize=1)
def realised_vol():
    """Kite split-adjusted daily closes -> 60d annualised vol, (date x symbol). Corporate-action days zeroed."""
    import sys

    sys.path.insert(0, str(ROOT / "backtest/r83_fno"))
    import core as K

    r = K.daily_returns()
    return (r.rolling(60, min_periods=50).std() * math.sqrt(252)).shift(0)


def chain(symbol, expiry, d0, d1):
    q = (
        "select date, strike, opt, close, settle, contracts, oi from fo where kind='OPT' and symbol=? and expiry=? "
        "and date between ? and ?"
    )
    c = (
        con()
        .execute(
            q,
            [symbol, pd.Timestamp(expiry).date(), pd.Timestamp(d0).date(), pd.Timestamp(d1).date()],
        )
        .df()
    )
    c["date"] = pd.to_datetime(c.date)
    return c


def trading_days_between(a, b):
    cal = calendar()
    return int(((cal > a) & (cal <= b)).sum())


def day_offset(expiry, k):
    """The trading day k sessions before `expiry` (k=0 -> expiry day itself, if a session)."""
    cal = calendar()
    idx = cal.searchsorted(pd.Timestamp(expiry), side="right") - 1
    return cal[max(idx - k, 0)]
