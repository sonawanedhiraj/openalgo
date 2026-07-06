"""
R43 News-Event-Momentum event-study + trading simulator.

Reads announcements.duckdb (events_filtered) + prices.duckdb (prices_1m,
prices_daily), builds a pure event-study table (event_returns) and a
grid-swept intraday-momentum trading simulation (trades), writing both to
results.duckdb.

Usage:
    uv run python backtest/news_event_study/simulate.py --summary
    uv run python backtest/news_event_study/simulate.py --full-grid --summary
    uv run python backtest/news_event_study/simulate.py \
        --latency 90 --react 2 --volsurge 2.5 --exit sameday_1515 --summary

Performance: walks each event's 1m bars ONCE per latency value, builds
numpy arrays (ret, vol_surge, cumvol, highs, lows, opens, closes, ts), and
evaluates all react x volsurge x exit cells from those arrays without
re-walking bars per cell.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Constants / grid
# --------------------------------------------------------------------------

DEFAULT_ANNOUNCEMENTS = "outputs/news_event_study/announcements.duckdb"
DEFAULT_PRICES = "outputs/news_event_study/prices.duckdb"
DEFAULT_OUT = "outputs/news_event_study/results.duckdb"

LATENCY_GRID = [30, 90, 300]  # seconds
REACT_PCT_GRID = [1.0, 2.0, 3.0]  # percent
VOLSURGE_GRID = [1.5, 2.5, 4.0]
EXIT_VARIANTS = ["sameday_1515", "t1_1515", "trail3", "stop3_t1"]

MARKET_OPEN = "09:15:00"
MARKET_CLOSE_STAMP = "15:15:00"  # exit reference time
ENTRY_CUTOFF = "14:30:00"  # no entries after this time
F_FLOOR = 0.005  # floor f(t) at 0.5% to avoid blow-up division
PENNY_FLOOR = 50.0  # prev_close must be >= this
POSITION_SIZE = 50000.0
ADVERSE_SLIPPAGE = 0.0025  # 0.25%

# Charges
MIS_BROKERAGE_CAP = 20.0
MIS_BROKERAGE_PCT = 0.0003  # 0.03%
MIS_STT_SELL_PCT = 0.00025  # 0.025% on sell leg only
MIS_EXCH_TXN_PCT = 0.0000297  # 0.00297% per leg
MIS_SEBI_PER_CRORE = 10.0  # per leg, per crore
MIS_STAMP_BUY_PCT = 0.00003  # 0.003% on buy leg
GST_PCT = 0.18

CNC_STT_PCT = 0.001  # 0.1% on BOTH legs
CNC_EXCH_TXN_PCT = 0.0000297  # per leg
CNC_SEBI_PER_CRORE = 10.0
CNC_STAMP_BUY_PCT = 0.00015  # 0.015% on buy
CNC_DP_CHARGE = 15.34  # on the sell leg, flat


# --------------------------------------------------------------------------
# DB connection helpers
# --------------------------------------------------------------------------


def connect_prices_readonly(
    path: str, retries: int = 3, retry_sleep: float = 2.0
) -> duckdb.DuckDBPyConnection:
    """Connect to prices.duckdb READ-ONLY. A background writer may hold the
    file; retry a few times, then fall back to copying the file (+ any .wal
    sidecar) to a temp path and reading that instead."""
    last_err = None
    for attempt in range(retries):
        try:
            return duckdb.connect(path, read_only=True)
        except duckdb.Error as e:
            last_err = e
            print(f"[connect_prices_readonly] attempt {attempt + 1}/{retries} failed: {e}")
            if attempt < retries - 1:
                time.sleep(retry_sleep)

    print("[connect_prices_readonly] falling back to temp copy of the db file")
    tmpdir = tempfile.mkdtemp(prefix="prices_ro_")
    tmp_path = str(Path(tmpdir) / "prices_copy.duckdb")
    wal_src = path + ".wal"
    copy_err = None
    for attempt in range(retries):
        try:
            shutil.copy2(path, tmp_path)
            if Path(wal_src).exists():
                try:
                    shutil.copy2(wal_src, tmp_path + ".wal")
                except OSError:
                    # WAL copy failing is non-fatal; main file may still have
                    # a checkpointed snapshot usable read-only.
                    pass
            copy_err = None
            break
        except OSError as e:
            copy_err = e
            print(f"[connect_prices_readonly] copy attempt {attempt + 1}/{retries} failed: {e}")
            if attempt < retries - 1:
                time.sleep(retry_sleep)

    if copy_err is not None:
        raise RuntimeError(
            f"Could not open {path} read-only nor copy it to a temp path "
            f"(writer holds an exclusive OS lock). Last copy error: {copy_err}. "
            f"Last connect error: {last_err}"
        ) from copy_err

    return duckdb.connect(tmp_path, read_only=True)


def connect_announcements_readonly(path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(path, read_only=True)


# --------------------------------------------------------------------------
# Preprocessing: intraday volume curve f(t)
# --------------------------------------------------------------------------


def compute_volume_curve(prices_con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """For each minute-of-day 09:15..15:29, compute the average fraction of
    the day's total volume traded by that minute (cumulative), across all
    symbol-days with >= 300 bars. Returns a DataFrame indexed by
    minute-of-day (HH:MM string) with column 'f'.
    """
    query = """
        WITH day_bars AS (
            SELECT
                symbol,
                CAST(ts AS DATE) AS bar_date,
                strftime(ts, '%H:%M') AS minute_of_day,
                ts,
                volume
            FROM prices_1m
        ),
        day_totals AS (
            SELECT symbol, bar_date, SUM(volume) AS day_total_vol, COUNT(*) AS n_bars
            FROM day_bars
            GROUP BY symbol, bar_date
            HAVING COUNT(*) >= 300
        ),
        cum AS (
            SELECT
                db.symbol,
                db.bar_date,
                db.minute_of_day,
                SUM(db.volume) OVER (
                    PARTITION BY db.symbol, db.bar_date
                    ORDER BY db.ts
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS cum_vol,
                dt.day_total_vol
            FROM day_bars db
            JOIN day_totals dt
              ON db.symbol = dt.symbol AND db.bar_date = dt.bar_date
        )
        SELECT
            minute_of_day,
            AVG(CASE WHEN day_total_vol > 0 THEN cum_vol * 1.0 / day_total_vol ELSE NULL END) AS f
        FROM cum
        GROUP BY minute_of_day
        ORDER BY minute_of_day
    """
    df = prices_con.execute(query).df()
    if df.empty:
        # No 1m data available yet; return an empty-but-well-formed frame.
        return pd.DataFrame(columns=["minute_of_day", "f"]).set_index("minute_of_day")
    df["f"] = df["f"].clip(lower=F_FLOOR)
    return df.set_index("minute_of_day")


def f_at(vol_curve: pd.DataFrame, ts: pd.Timestamp) -> float:
    """Look up f(t) for a given timestamp's minute-of-day, floored at
    F_FLOOR. Falls back to F_FLOOR if the minute isn't in the curve
    (e.g. sparse partial data)."""
    key = ts.strftime("%H:%M")
    if key in vol_curve.index:
        val = vol_curve.loc[key, "f"]
        if pd.isna(val):
            return F_FLOOR
        return max(float(val), F_FLOOR)
    return F_FLOOR


# --------------------------------------------------------------------------
# Event loading + per-event daily context (prev_close, avg20_vol)
# --------------------------------------------------------------------------


@dataclass
class EventContext:
    seq_id: str
    symbol: str
    category: str
    polarity: str
    entry_mode: str
    eff_date: date_cls
    announced_at: pd.Timestamp
    prev_close: float
    avg20_vol: float


def load_events(ann_con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = ann_con.execute(
        """
        SELECT
            seq_id,
            symbol,
            category,
            announced_at,
            summary_text,
            polarity,
            CAST(eff_date AS DATE) AS eff_date,
            entry_mode
        FROM events_filtered
        """
    ).df()
    return df


def build_daily_context(
    prices_con: duckdb.DuckDBPyConnection, events: pd.DataFrame
) -> pd.DataFrame:
    """For each (symbol, eff_date) needed, compute prev_close (last close
    strictly before eff_date) and avg20_vol (mean volume of the 20 trading
    days strictly before eff_date, requiring >= 10 days else NaN)."""
    pairs = events[["symbol", "eff_date"]].drop_duplicates()
    if pairs.empty:
        return pd.DataFrame(
            columns=["symbol", "eff_date", "prev_close", "avg20_vol", "n_prior_days"]
        )

    prices_con.register("pairs_df", pairs)
    query = """
        WITH prior AS (
            SELECT
                p.symbol,
                pr.eff_date,
                p.bar_date,
                p.close,
                p.volume,
                ROW_NUMBER() OVER (
                    PARTITION BY p.symbol, pr.eff_date
                    ORDER BY p.bar_date DESC
                ) AS rn_desc
            FROM prices_daily p
            JOIN pairs_df pr
              ON p.symbol = pr.symbol AND p.bar_date < pr.eff_date
        )
        SELECT
            symbol,
            eff_date,
            MAX(CASE WHEN rn_desc = 1 THEN close END) AS prev_close,
            AVG(CASE WHEN rn_desc <= 20 THEN volume END) AS avg20_vol,
            SUM(CASE WHEN rn_desc <= 20 THEN 1 ELSE 0 END) AS n_prior_days
        FROM prior
        GROUP BY symbol, eff_date
    """
    out = prices_con.execute(query).df()
    prices_con.unregister("pairs_df")
    return out


def get_1m_availability(
    prices_con: duckdb.DuckDBPyConnection, events: pd.DataFrame
) -> pd.DataFrame:
    """Return the set of (symbol, bar_date) that have any 1m data, restricted
    to symbols/dates present in `events` (eff_date and eff_date+1) to keep
    the query bounded."""
    pairs = events[["symbol", "eff_date"]].drop_duplicates()
    if pairs.empty:
        return pd.DataFrame(columns=["symbol", "bar_date"])
    prices_con.register("pairs_df2", pairs)
    query = """
        SELECT DISTINCT symbol, CAST(ts AS DATE) AS bar_date
        FROM prices_1m
        WHERE symbol IN (SELECT DISTINCT symbol FROM pairs_df2)
    """
    out = prices_con.execute(query).df()
    prices_con.unregister("pairs_df2")
    return out


# --------------------------------------------------------------------------
# Per-event 1m bar loading (batched)
# --------------------------------------------------------------------------


def load_1m_bars_for_symbols(
    prices_con: duckdb.DuckDBPyConnection, symbols: list[str]
) -> pd.DataFrame:
    """Load ALL 1m bars for the given symbols in one query (batched, not
    per-event). Caller slices per symbol/day in-memory."""
    if not symbols:
        return pd.DataFrame(columns=["symbol", "ts", "open", "high", "low", "close", "volume"])
    prices_con.register("symlist", pd.DataFrame({"symbol": symbols}))
    df = prices_con.execute(
        """
        SELECT p.symbol, p.ts, p.open, p.high, p.low, p.close, p.volume
        FROM prices_1m p
        JOIN symlist s ON p.symbol = s.symbol
        ORDER BY p.symbol, p.ts
        """
    ).df()
    prices_con.unregister("symlist")
    df["ts"] = pd.to_datetime(df["ts"])
    return df


# --------------------------------------------------------------------------
# Charges
# --------------------------------------------------------------------------


def mis_charges(buy_value: float, sell_value: float) -> dict:
    """Same-day (MIS intraday) charges for one round trip."""
    brokerage_buy = min(MIS_BROKERAGE_PCT * buy_value, MIS_BROKERAGE_CAP)
    brokerage_sell = min(MIS_BROKERAGE_PCT * sell_value, MIS_BROKERAGE_CAP)
    stt = MIS_STT_SELL_PCT * sell_value
    exch_txn_buy = MIS_EXCH_TXN_PCT * buy_value
    exch_txn_sell = MIS_EXCH_TXN_PCT * sell_value
    sebi_buy = MIS_SEBI_PER_CRORE * buy_value / 1e7
    sebi_sell = MIS_SEBI_PER_CRORE * sell_value / 1e7
    stamp = MIS_STAMP_BUY_PCT * buy_value
    gst = GST_PCT * (
        brokerage_buy + brokerage_sell + exch_txn_buy + exch_txn_sell + sebi_buy + sebi_sell
    )
    total = (
        brokerage_buy
        + brokerage_sell
        + stt
        + exch_txn_buy
        + exch_txn_sell
        + sebi_buy
        + sebi_sell
        + stamp
        + gst
    )
    return {
        "brokerage_buy": brokerage_buy,
        "brokerage_sell": brokerage_sell,
        "stt": stt,
        "exch_txn_buy": exch_txn_buy,
        "exch_txn_sell": exch_txn_sell,
        "sebi_buy": sebi_buy,
        "sebi_sell": sebi_sell,
        "stamp": stamp,
        "gst": gst,
        "total": total,
    }


def cnc_charges(buy_value: float, sell_value: float) -> dict:
    """Overnight long (CNC delivery) charges for one round trip."""
    brokerage = 0.0
    stt = CNC_STT_PCT * buy_value + CNC_STT_PCT * sell_value
    exch_txn_buy = CNC_EXCH_TXN_PCT * buy_value
    exch_txn_sell = CNC_EXCH_TXN_PCT * sell_value
    sebi_buy = CNC_SEBI_PER_CRORE * buy_value / 1e7
    sebi_sell = CNC_SEBI_PER_CRORE * sell_value / 1e7
    stamp = CNC_STAMP_BUY_PCT * buy_value
    dp_charge = CNC_DP_CHARGE
    gst = GST_PCT * (exch_txn_buy + exch_txn_sell + sebi_buy + sebi_sell)
    total = (
        brokerage
        + stt
        + exch_txn_buy
        + exch_txn_sell
        + sebi_buy
        + sebi_sell
        + stamp
        + dp_charge
        + gst
    )
    return {
        "brokerage_buy": 0.0,
        "brokerage_sell": 0.0,
        "stt": stt,
        "exch_txn_buy": exch_txn_buy,
        "exch_txn_sell": exch_txn_sell,
        "sebi_buy": sebi_buy,
        "sebi_sell": sebi_sell,
        "stamp": stamp,
        "dp_charge": dp_charge,
        "gst": gst,
        "total": total,
    }


def compute_charges(
    direction: str, overnight: bool, entry_px: float, exit_px: float, qty: int
) -> dict:
    """Route to MIS or CNC charge model based on direction/holding period.

    direction: 'long' or 'short'. Shorts are always intraday (MIS) since
    Indian cash equities can't be held short overnight.
    """
    if direction == "short" or not overnight:
        buy_value = (exit_px if direction == "short" else entry_px) * qty
        sell_value = (entry_px if direction == "short" else exit_px) * qty
        return mis_charges(buy_value=buy_value, sell_value=sell_value)
    else:
        buy_value = entry_px * qty
        sell_value = exit_px * qty
        return cnc_charges(buy_value=buy_value, sell_value=sell_value)


# --------------------------------------------------------------------------
# Locked/circuit detection
# --------------------------------------------------------------------------


def compute_locked_flag(day_bars: pd.DataFrame, detection_idx: int, detection_ret: float) -> bool:
    """locked = (>=80% of eff_date bars have high==low) OR
    (|ret at detection| > 9.5% and day range < 1%)."""
    if day_bars.empty:
        return False
    hl_equal_frac = (day_bars["high"] == day_bars["low"]).mean()
    cond1 = hl_equal_frac >= 0.80

    day_high = day_bars["high"].max()
    day_low = day_bars["low"].min()
    day_open = day_bars["open"].iloc[0]
    day_range_pct = (day_high - day_low) / day_open if day_open else 0.0
    cond2 = (abs(detection_ret) > 0.095) and (day_range_pct < 0.01)

    return bool(cond1 or cond2)


# --------------------------------------------------------------------------
# Core per-event computation
# --------------------------------------------------------------------------


@dataclass
class EventSeries:
    """Precomputed arrays for one event at one latency value, spanning from
    detection_ts through T+1 15:15 (or end of available data)."""

    seq_id: str
    symbol: str
    category: str
    polarity: str
    entry_mode: str
    eff_date: date_cls
    prev_close: float
    avg20_vol: float
    latency: int
    detection_ts: pd.Timestamp
    ts: np.ndarray  # bar close timestamps, from first bar >= detection_ts
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    ret: np.ndarray  # close/prev_close - 1
    vol_surge: np.ndarray
    cumvol: np.ndarray
    is_t1: np.ndarray  # bool, bar belongs to eff_date+1 trading data present
    detection_ret: float | None
    ret_at_detection_close: float | None
    locked: bool
    reason_skip: str | None  # if set, event is entirely skipped for this latency


def detection_timestamp(
    entry_mode: str, eff_date: date_cls, announced_at: pd.Timestamp, latency_s: int
) -> pd.Timestamp:
    if entry_mode == "intraday":
        base = announced_at
    else:  # at_open
        base = pd.Timestamp(eff_date) + pd.Timedelta(hours=9, minutes=15)
    return base + pd.Timedelta(seconds=latency_s)


def build_event_series(
    symbol_bars: pd.DataFrame,
    seq_id: str,
    symbol: str,
    category: str,
    polarity: str,
    entry_mode: str,
    eff_date: date_cls,
    announced_at: pd.Timestamp,
    prev_close: float,
    avg20_vol: float,
    latency: int,
    vol_curve: pd.DataFrame,
) -> EventSeries:
    """symbol_bars: pre-filtered 1m bars for this symbol only (all dates),
    sorted by ts, with a 'bar_date' column precomputed."""

    detection_ts = detection_timestamp(entry_mode, eff_date, announced_at, latency)

    day_bars = symbol_bars[symbol_bars["bar_date"] == eff_date]
    if day_bars.empty:
        return EventSeries(
            seq_id=seq_id,
            symbol=symbol,
            category=category,
            polarity=polarity,
            entry_mode=entry_mode,
            eff_date=eff_date,
            prev_close=prev_close,
            avg20_vol=avg20_vol,
            latency=latency,
            detection_ts=detection_ts,
            ts=np.array([]),
            opens=np.array([]),
            highs=np.array([]),
            lows=np.array([]),
            closes=np.array([]),
            ret=np.array([]),
            vol_surge=np.array([]),
            cumvol=np.array([]),
            is_t1=np.array([]),
            detection_ret=None,
            ret_at_detection_close=None,
            locked=False,
            reason_skip="no_1m",
        )

    t1_date = eff_date + timedelta(days=1)
    # search forward up to 5 calendar days for the next trading day's bars
    t1_bars = pd.DataFrame()
    for offset in range(1, 6):
        cand_date = eff_date + timedelta(days=offset)
        cand = symbol_bars[symbol_bars["bar_date"] == cand_date]
        if not cand.empty:
            t1_bars = cand
            t1_date = cand_date
            break

    combined = (
        pd.concat([day_bars, t1_bars], ignore_index=True) if not t1_bars.empty else day_bars.copy()
    )
    combined = combined.sort_values("ts").reset_index(drop=True)

    # cumulative volume within eff_date only (for vol_surge calc); T+1 bars
    # get their own cumvol reset (not used for entry gating post T+1, but
    # keep consistent).
    day_mask = combined["bar_date"] == eff_date
    cumvol = np.zeros(len(combined))
    cumvol[day_mask.values] = combined.loc[day_mask, "volume"].cumsum().values
    if not t1_bars.empty:
        t1_mask = combined["bar_date"] == t1_date
        cumvol[t1_mask.values] = combined.loc[t1_mask, "volume"].cumsum().values

    ret = (
        (combined["close"].values / prev_close) - 1.0
        if prev_close and prev_close > 0
        else np.full(len(combined), np.nan)
    )

    vol_surge = np.full(len(combined), np.nan)
    if avg20_vol and avg20_vol > 0:
        for i, row in combined.iterrows():
            if row["bar_date"] == eff_date:
                f_t = f_at(vol_curve, row["ts"])
                denom = avg20_vol * f_t
                vol_surge[i] = cumvol[i] / denom if denom > 0 else np.nan
            # T+1 bars: vol_surge not meaningful for entry gating (entries
            # only happen same-day up to 14:30), leave as NaN.

    locked = compute_locked_flag(day_bars, 0, 0.0)

    # detection_ret: last 1m close at/just before detection_ts, over prev_close
    pre_detect = day_bars[day_bars["ts"] <= detection_ts]
    if not pre_detect.empty and prev_close and prev_close > 0:
        last_close_before = pre_detect["close"].iloc[-1]
        detection_ret = (last_close_before / prev_close) - 1.0
        # recompute locked using actual detection ret
        locked = compute_locked_flag(day_bars, 0, detection_ret)
    else:
        detection_ret = None

    ret_at_detection_close = detection_ret

    # restrict arrays to bars strictly AFTER detection_ts (first bar open
    # after detection is the earliest possible entry trigger bar for
    # confirmation-walk purposes; we keep from first bar with ts > detection_ts)
    post_mask = combined["ts"].values > np.datetime64(detection_ts)
    idx = np.where(post_mask)[0]
    if len(idx) == 0:
        return EventSeries(
            seq_id=seq_id,
            symbol=symbol,
            category=category,
            polarity=polarity,
            entry_mode=entry_mode,
            eff_date=eff_date,
            prev_close=prev_close,
            avg20_vol=avg20_vol,
            latency=latency,
            detection_ts=detection_ts,
            ts=np.array([]),
            opens=np.array([]),
            highs=np.array([]),
            lows=np.array([]),
            closes=np.array([]),
            ret=np.array([]),
            vol_surge=np.array([]),
            cumvol=np.array([]),
            is_t1=np.array([]),
            detection_ret=detection_ret,
            ret_at_detection_close=ret_at_detection_close,
            locked=locked,
            reason_skip="no_1m",
        )

    start = idx[0]
    sl = combined.iloc[start:]
    is_t1 = (
        (sl["bar_date"].values == t1_date) if not t1_bars.empty else np.zeros(len(sl), dtype=bool)
    )

    return EventSeries(
        seq_id=seq_id,
        symbol=symbol,
        category=category,
        polarity=polarity,
        entry_mode=entry_mode,
        eff_date=eff_date,
        prev_close=prev_close,
        avg20_vol=avg20_vol,
        latency=latency,
        detection_ts=detection_ts,
        ts=sl["ts"].values,
        opens=sl["open"].values.astype(float),
        highs=sl["high"].values.astype(float),
        lows=sl["low"].values.astype(float),
        closes=sl["close"].values.astype(float),
        ret=ret[start:],
        vol_surge=vol_surge[start:],
        cumvol=cumvol[start:],
        is_t1=is_t1,
        detection_ret=detection_ret,
        ret_at_detection_close=ret_at_detection_close,
        locked=locked,
        reason_skip=None,
    )


# --------------------------------------------------------------------------
# Event-study pass (pure, no trading) — one row per event (latency default)
# --------------------------------------------------------------------------


def compute_event_returns_row(es: EventSeries, latency_default: int) -> dict:
    seq_id, symbol = es.seq_id, es.symbol
    base_row = {
        "seq_id": seq_id,
        "symbol": symbol,
        "category": es.category,
        "polarity": es.polarity,
        "entry_mode": es.entry_mode,
        "eff_date": es.eff_date,
        "latency": latency_default,
        "detection_ts": es.detection_ts,
        "prev_close": es.prev_close,
        "avg20_vol": es.avg20_vol,
        "ret_at_detection": es.detection_ret,
        "vol_surge_at_detection": None,
        "ret_fwd_30m": None,
        "ret_fwd_60m": None,
        "ret_sameday_1515": None,
        "ret_t1_1515": None,
        "max_ret_60m": None,
        "min_ret_60m": None,
        "locked": es.locked,
        "skip_reason": es.reason_skip,
    }
    if es.reason_skip is not None or len(es.ts) == 0:
        return base_row

    # vol_surge_at_detection: at the bar at/just before detection (use first
    # available post-detection element's preceding cumvol proxy: recompute
    # directly here using the same helper logic as ret_at_detection).
    # We already stored es.detection_ret; compute vol_surge similarly using
    # cumvol up to detection minute.
    # Reconstruct using day bars info embedded in es via first post-detection
    # index context: approximate using es.cumvol[0] is AFTER detection, so we
    # instead recompute directly from raw arrays is not available here-- use
    # the same approach as build_event_series's detection_ret calc, done via
    # a stored field would be cleaner; approximate with the bar immediately
    # preceding first entry in es.ts by looking at es.cumvol[0] minus the
    # volume of the first bar itself (since es.cumvol[0] includes it).
    if es.avg20_vol and es.avg20_vol > 0 and len(es.cumvol) > 0:
        f_t = f_at(_GLOBAL_VOL_CURVE[0], es.detection_ts)
        denom = es.avg20_vol * f_t
        # cumvol just before first post-detection bar approximates cumvol at
        # detection (bars are 1-minute; the gap is <= 1 bar of volume).
        approx_cumvol_at_detection = max(es.cumvol[0] - 0.0, 0.0)
        base_row["vol_surge_at_detection"] = (
            approx_cumvol_at_detection / denom if denom > 0 else None
        )

    ts_pd = pd.to_datetime(es.ts)

    def close_at_or_after(minutes: int) -> float | None:
        target = es.detection_ts + pd.Timedelta(minutes=minutes)
        mask = ts_pd >= target
        idxs = np.where(mask)[0]
        if len(idxs) == 0:
            return None
        return float(es.closes[idxs[0]])

    c30 = close_at_or_after(30)
    c60 = close_at_or_after(60)
    if c30 is not None:
        base_row["ret_fwd_30m"] = (c30 / es.prev_close) - 1.0
    if c60 is not None:
        base_row["ret_fwd_60m"] = (c60 / es.prev_close) - 1.0

    # same-day 15:15 close (or last bar of eff_date)
    same_day_mask = ~es.is_t1
    if same_day_mask.any():
        sd_idx = np.where(same_day_mask)[0]
        sd_ts = ts_pd[sd_idx]
        target_1515 = pd.Timestamp.combine(es.eff_date, pd.Timestamp(MARKET_CLOSE_STAMP).time())
        le_mask = sd_ts <= target_1515
        if le_mask.any():
            chosen = sd_idx[np.where(le_mask)[0][-1]]
        else:
            chosen = sd_idx[-1]
        base_row["ret_sameday_1515"] = (float(es.closes[chosen]) / es.prev_close) - 1.0

    # T+1 15:15 close
    if es.is_t1.any():
        t1_idx = np.where(es.is_t1)[0]
        t1_ts = ts_pd[t1_idx]
        t1_date = t1_ts[0].date()
        target_t1_1515 = pd.Timestamp.combine(t1_date, pd.Timestamp(MARKET_CLOSE_STAMP).time())
        le_mask = t1_ts <= target_t1_1515
        if le_mask.any():
            chosen = t1_idx[np.where(le_mask)[0][-1]]
        else:
            chosen = t1_idx[-1]
        base_row["ret_t1_1515"] = (float(es.closes[chosen]) / es.prev_close) - 1.0

    # max/min ret over the 60m window (from highs/lows), same-day only bars
    window_mask = (ts_pd <= (es.detection_ts + pd.Timedelta(minutes=60))) & (~es.is_t1)
    if window_mask.any():
        w_highs = es.highs[window_mask]
        w_lows = es.lows[window_mask]
        base_row["max_ret_60m"] = float(w_highs.max() / es.prev_close - 1.0)
        base_row["min_ret_60m"] = float(w_lows.min() / es.prev_close - 1.0)

    return base_row


# --------------------------------------------------------------------------
# Trading simulation: evaluate all grid cells from precomputed arrays
# --------------------------------------------------------------------------


def direction_for_polarity(polarity: str, ret: float) -> str | None:
    if polarity == "positive":
        return "long"
    if polarity == "negative":
        return "short"
    if polarity in ("results", "tape_decide"):
        return "long" if ret > 0 else ("short" if ret < 0 else None)
    return None


def simulate_event_cells(
    es: EventSeries,
    react_grid: list[float],
    volsurge_grid: list[float],
    exit_variants: list[str],
) -> tuple[list[dict], list[dict]]:
    """Evaluate all (react_pct, vol_surge_min, exit_variant) cells for one
    event's precomputed EventSeries. Returns (trade_rows, no_trade_rows)."""

    trades: list[dict] = []
    no_trades: list[dict] = []

    common = {
        "seq_id": es.seq_id,
        "symbol": es.symbol,
        "category": es.category,
        "polarity": es.polarity,
        "entry_mode": es.entry_mode,
        "latency": es.latency,
    }

    if es.reason_skip is not None:
        for react_pct in react_grid:
            for vs_min in volsurge_grid:
                for exit_variant in exit_variants:
                    no_trades.append(
                        {
                            **common,
                            "react_pct": react_pct,
                            "vol_surge_min": vs_min,
                            "exit_variant": exit_variant,
                            "reason": es.reason_skip,
                        }
                    )
        return trades, no_trades

    if es.prev_close is None or es.prev_close < PENNY_FLOOR:
        for react_pct in react_grid:
            for vs_min in volsurge_grid:
                for exit_variant in exit_variants:
                    no_trades.append(
                        {
                            **common,
                            "react_pct": react_pct,
                            "vol_surge_min": vs_min,
                            "exit_variant": exit_variant,
                            "reason": "penny",
                        }
                    )
        return trades, no_trades

    if es.locked:
        for react_pct in react_grid:
            for vs_min in volsurge_grid:
                for exit_variant in exit_variants:
                    no_trades.append(
                        {
                            **common,
                            "react_pct": react_pct,
                            "vol_surge_min": vs_min,
                            "exit_variant": exit_variant,
                            "reason": "locked",
                        }
                    )
        return trades, no_trades

    n = len(es.ts)
    same_day_mask = ~es.is_t1
    ts_pd = pd.to_datetime(es.ts)
    cutoff_ts = pd.Timestamp.combine(es.eff_date, pd.Timestamp(ENTRY_CUTOFF).time())

    # eligible confirmation bars: same-day only, at/before cutoff, index < n-1
    # (need a next bar to enter on)
    eligible = np.where(same_day_mask & (ts_pd <= cutoff_ts) & (np.arange(n) < n - 1))[0]

    for react_pct in react_grid:
        react_frac = react_pct / 100.0
        for vs_min in volsurge_grid:
            # find first eligible bar meeting direction + react + volsurge gate
            confirm_i = None
            confirm_dir = None
            for i in eligible:
                r = es.ret[i]
                if np.isnan(r):
                    continue
                d = direction_for_polarity(es.polarity, r)
                if d is None:
                    continue
                if d == "long" and r < react_frac:
                    continue
                if d == "short" and r > -react_frac:
                    continue
                vs = es.vol_surge[i]
                if vs is None or np.isnan(vs) or vs < vs_min:
                    continue
                confirm_i = i
                confirm_dir = d
                break

            if confirm_i is None:
                for exit_variant in exit_variants:
                    no_trades.append(
                        {
                            **common,
                            "react_pct": react_pct,
                            "vol_surge_min": vs_min,
                            "exit_variant": exit_variant,
                            "reason": "no_gate_pass",
                        }
                    )
                continue

            entry_idx = confirm_i + 1
            if entry_idx >= n or es.is_t1[entry_idx]:
                # no next same-day bar to enter on
                for exit_variant in exit_variants:
                    no_trades.append(
                        {
                            **common,
                            "react_pct": react_pct,
                            "vol_surge_min": vs_min,
                            "exit_variant": exit_variant,
                            "reason": "after_cutoff",
                        }
                    )
                continue

            entry_ts = ts_pd[entry_idx]
            raw_entry_px = float(es.opens[entry_idx])
            if confirm_dir == "long":
                entry_px = raw_entry_px * (1 + ADVERSE_SLIPPAGE)
            else:
                entry_px = raw_entry_px * (1 - ADVERSE_SLIPPAGE)

            qty = int(50000 // entry_px) if entry_px > 0 else 0
            if qty <= 0:
                for exit_variant in exit_variants:
                    no_trades.append(
                        {
                            **common,
                            "react_pct": react_pct,
                            "vol_surge_min": vs_min,
                            "exit_variant": exit_variant,
                            "reason": "no_gate_pass",
                        }
                    )
                continue

            vol_surge_at_entry = float(es.vol_surge[confirm_i])

            for exit_variant in exit_variants:
                trade_row = _simulate_exit(
                    es=es,
                    common=common,
                    react_pct=react_pct,
                    vs_min=vs_min,
                    exit_variant=exit_variant,
                    confirm_dir=confirm_dir,
                    entry_idx=entry_idx,
                    entry_ts=entry_ts,
                    entry_px=entry_px,
                    qty=qty,
                    vol_surge_at_entry=vol_surge_at_entry,
                )
                if trade_row is None:
                    no_trades.append(
                        {
                            **common,
                            "react_pct": react_pct,
                            "vol_surge_min": vs_min,
                            "exit_variant": exit_variant,
                            "reason": "no_1m",
                        }
                    )
                else:
                    trades.append(trade_row)

    return trades, no_trades


def _simulate_exit(
    es: EventSeries,
    common: dict,
    react_pct: float,
    vs_min: float,
    exit_variant: str,
    confirm_dir: str,
    entry_idx: int,
    entry_ts,
    entry_px: float,
    qty: int,
    vol_surge_at_entry: float,
) -> dict | None:
    n = len(es.ts)
    ts_pd = pd.to_datetime(es.ts)
    short_degraded = False
    effective_variant = exit_variant

    if confirm_dir == "short" and exit_variant in ("t1_1515", "stop3_t1"):
        short_degraded = True
        effective_variant = "sameday_1515" if exit_variant == "t1_1515" else "stop3_sameday"

    exit_idx = None

    if effective_variant == "sameday_1515":
        same_day_mask = ~es.is_t1
        target_1515 = pd.Timestamp.combine(es.eff_date, pd.Timestamp(MARKET_CLOSE_STAMP).time())
        candidates = np.where(same_day_mask & (np.arange(n) >= entry_idx))[0]
        if len(candidates) == 0:
            return None
        le_mask = ts_pd[candidates] <= target_1515
        if le_mask.any():
            exit_idx = candidates[np.where(le_mask)[0][-1]]
        else:
            exit_idx = candidates[-1]
        exit_px_raw = float(es.closes[exit_idx])

    elif effective_variant == "t1_1515":
        if not es.is_t1.any():
            return None
        t1_idx = np.where(es.is_t1)[0]
        t1_date = ts_pd[t1_idx[0]].date()
        target_t1_1515 = pd.Timestamp.combine(t1_date, pd.Timestamp(MARKET_CLOSE_STAMP).time())
        le_mask = ts_pd[t1_idx] <= target_t1_1515
        if le_mask.any():
            exit_idx = t1_idx[np.where(le_mask)[0][-1]]
        else:
            exit_idx = t1_idx[-1]
        exit_px_raw = float(es.closes[exit_idx])

    elif effective_variant == "trail3":
        # trailing 3% from best favorable 1m close since entry, checked on
        # closes, exit next bar open, else sameday 15:15
        same_day_mask = ~es.is_t1
        candidates = np.where(same_day_mask & (np.arange(n) >= entry_idx))[0]
        if len(candidates) == 0:
            return None
        best_favorable = entry_px
        exit_trigger_idx = None
        for i in candidates:
            c = es.closes[i]
            if confirm_dir == "long":
                best_favorable = max(best_favorable, c)
                if c <= best_favorable * (1 - 0.03):
                    exit_trigger_idx = i
                    break
            else:
                best_favorable = min(best_favorable, c)
                if c >= best_favorable * (1 + 0.03):
                    exit_trigger_idx = i
                    break
        if exit_trigger_idx is not None:
            next_i = exit_trigger_idx + 1
            same_day_next = np.where(same_day_mask & (np.arange(n) == next_i))[0]
            if len(same_day_next) > 0:
                exit_idx = same_day_next[0]
                exit_px_raw = float(es.opens[exit_idx])
            else:
                # no next same-day bar to exit on open; fall back to sameday 15:15
                target_1515 = pd.Timestamp.combine(
                    es.eff_date, pd.Timestamp(MARKET_CLOSE_STAMP).time()
                )
                le_mask = ts_pd[candidates] <= target_1515
                exit_idx = candidates[np.where(le_mask)[0][-1]] if le_mask.any() else candidates[-1]
                exit_px_raw = float(es.closes[exit_idx])
        else:
            target_1515 = pd.Timestamp.combine(es.eff_date, pd.Timestamp(MARKET_CLOSE_STAMP).time())
            le_mask = ts_pd[candidates] <= target_1515
            exit_idx = candidates[np.where(le_mask)[0][-1]] if le_mask.any() else candidates[-1]
            exit_px_raw = float(es.closes[exit_idx])

    elif effective_variant == "stop3_t1":
        # hard stop at -3% from entry (1m close basis, exit next open), else
        # T+1 15:15
        same_day_mask = ~es.is_t1
        candidates = np.where(same_day_mask & (np.arange(n) >= entry_idx))[0]
        stop_trigger_idx = None
        for i in candidates:
            c = es.closes[i]
            adverse_ret = (c / entry_px - 1.0) if confirm_dir == "long" else (entry_px / c - 1.0)
            if adverse_ret <= -0.03:
                stop_trigger_idx = i
                break
        if stop_trigger_idx is not None:
            next_i = stop_trigger_idx + 1
            if next_i < n:
                exit_idx = next_i
                exit_px_raw = float(es.opens[exit_idx])
            else:
                return None
        else:
            if not es.is_t1.any():
                return None
            t1_idx = np.where(es.is_t1)[0]
            t1_date = ts_pd[t1_idx[0]].date()
            target_t1_1515 = pd.Timestamp.combine(t1_date, pd.Timestamp(MARKET_CLOSE_STAMP).time())
            le_mask = ts_pd[t1_idx] <= target_t1_1515
            exit_idx = t1_idx[np.where(le_mask)[0][-1]] if le_mask.any() else t1_idx[-1]
            exit_px_raw = float(es.closes[exit_idx])

    elif effective_variant == "stop3_sameday":
        # degraded stop3_t1 for shorts: stop at -3% adverse, else sameday 15:15
        same_day_mask = ~es.is_t1
        candidates = np.where(same_day_mask & (np.arange(n) >= entry_idx))[0]
        if len(candidates) == 0:
            return None
        stop_trigger_idx = None
        for i in candidates:
            c = es.closes[i]
            adverse_ret = entry_px / c - 1.0  # short only reaches here
            if adverse_ret <= -0.03:
                stop_trigger_idx = i
                break
        if stop_trigger_idx is not None:
            next_i = stop_trigger_idx + 1
            same_day_next = np.where(same_day_mask & (np.arange(n) == next_i))[0]
            if len(same_day_next) > 0:
                exit_idx = same_day_next[0]
                exit_px_raw = float(es.opens[exit_idx])
            else:
                target_1515 = pd.Timestamp.combine(
                    es.eff_date, pd.Timestamp(MARKET_CLOSE_STAMP).time()
                )
                le_mask = ts_pd[candidates] <= target_1515
                exit_idx = candidates[np.where(le_mask)[0][-1]] if le_mask.any() else candidates[-1]
                exit_px_raw = float(es.closes[exit_idx])
        else:
            target_1515 = pd.Timestamp.combine(es.eff_date, pd.Timestamp(MARKET_CLOSE_STAMP).time())
            le_mask = ts_pd[candidates] <= target_1515
            exit_idx = candidates[np.where(le_mask)[0][-1]] if le_mask.any() else candidates[-1]
            exit_px_raw = float(es.closes[exit_idx])
    else:
        raise ValueError(f"unknown exit_variant {exit_variant}")

    exit_ts = ts_pd[exit_idx]
    if confirm_dir == "long":
        exit_px = exit_px_raw * (1 - ADVERSE_SLIPPAGE)
    else:
        exit_px = exit_px_raw * (1 + ADVERSE_SLIPPAGE)

    overnight = es.is_t1[exit_idx] if exit_idx is not None else False

    if confirm_dir == "long":
        gross_pnl = (exit_px - entry_px) * qty
    else:
        gross_pnl = (entry_px - exit_px) * qty

    charges = compute_charges(
        direction=confirm_dir,
        overnight=overnight,
        entry_px=entry_px,
        exit_px=exit_px,
        qty=qty,
    )
    total_charges = charges["total"]
    net_pnl = gross_pnl - total_charges
    position_value = entry_px * qty
    net_ret_pct = (net_pnl / position_value * 100.0) if position_value > 0 else None

    return {
        **common,
        "react_pct": react_pct,
        "vol_surge_min": vs_min,
        "exit_variant": exit_variant,
        "direction": confirm_dir,
        "entry_ts": entry_ts,
        "entry_px": entry_px,
        "exit_ts": exit_ts,
        "exit_px": exit_px,
        "qty": qty,
        "gross_pnl": gross_pnl,
        "charges": total_charges,
        "net_pnl": net_pnl,
        "net_ret_pct": net_ret_pct,
        "prev_close": es.prev_close,
        "vol_surge_at_entry": vol_surge_at_entry,
        "short_degraded": short_degraded,
        "_charges_detail": charges,
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

_GLOBAL_VOL_CURVE: list[pd.DataFrame] = [None]  # module-level stash for f_at() convenience


def run(
    announcements_path: str,
    prices_path: str,
    out_path: str,
    latency_grid: list[int],
    react_grid: list[float],
    volsurge_grid: list[float],
    exit_variants: list[str],
    print_summary: bool,
) -> dict:
    t0 = time.time()
    ann_con = connect_announcements_readonly(announcements_path)
    prices_con = connect_prices_readonly(prices_path)

    print("[run] loading events...")
    events = load_events(ann_con)
    print(f"[run] {len(events)} events loaded from events_filtered")

    print("[run] computing intraday volume curve f(t)...")
    vol_curve = compute_volume_curve(prices_con)
    _GLOBAL_VOL_CURVE[0] = vol_curve
    print(f"[run] volume curve has {len(vol_curve)} minute-of-day buckets")

    print("[run] computing daily context (prev_close, avg20_vol)...")
    daily_ctx = build_daily_context(prices_con, events)
    events = events.merge(daily_ctx, on=["symbol", "eff_date"], how="left")

    print("[run] checking 1m data availability...")
    avail = get_1m_availability(prices_con, events)
    avail_bar_dates = [d.date() if hasattr(d, "date") else d for d in avail["bar_date"]]
    avail_set = set(zip(avail["symbol"], avail_bar_dates, strict=True))

    symbols_needed = sorted(events["symbol"].unique().tolist())
    print(f"[run] loading 1m bars for {len(symbols_needed)} symbols (batched)...")
    all_1m = load_1m_bars_for_symbols(prices_con, symbols_needed)
    if not all_1m.empty:
        all_1m["bar_date"] = all_1m["ts"].dt.date
    print(f"[run] loaded {len(all_1m)} 1m bar rows total")

    # group per symbol for fast slicing
    bars_by_symbol = (
        {sym: df for sym, df in all_1m.groupby("symbol")}  # noqa: C416 -- dict() breaks on GroupBy objects
        if not all_1m.empty
        else {}
    )

    event_returns_rows = []
    all_trade_rows = []
    all_no_trade_rows = []

    n_events = len(events)
    n_with_price_data = 0
    n_insufficient_daily = 0
    n_no_1m = 0

    for row_i, ev in enumerate(events.itertuples(index=False)):
        if row_i % 2000 == 0 and row_i > 0:
            elapsed = time.time() - t0
            print(f"[run] {row_i}/{n_events} events processed ({elapsed:.1f}s elapsed)")

        seq_id = ev.seq_id
        symbol = ev.symbol
        category = ev.category
        polarity = ev.polarity
        entry_mode = ev.entry_mode
        eff_date = ev.eff_date.date() if hasattr(ev.eff_date, "date") else ev.eff_date
        announced_at = pd.Timestamp(ev.announced_at)
        prev_close = getattr(ev, "prev_close", None)
        avg20_vol = getattr(ev, "avg20_vol", None)
        n_prior_days = getattr(ev, "n_prior_days", 0)

        common_skip = {
            "seq_id": seq_id,
            "symbol": symbol,
            "category": category,
            "polarity": polarity,
            "entry_mode": entry_mode,
        }

        if prev_close is None or pd.isna(prev_close) or n_prior_days is None or n_prior_days < 10:
            n_insufficient_daily += 1
            for latency in latency_grid:
                for react_pct in react_grid:
                    for vs_min in volsurge_grid:
                        for exit_variant in exit_variants:
                            all_no_trade_rows.append(
                                {
                                    **common_skip,
                                    "latency": latency,
                                    "react_pct": react_pct,
                                    "vol_surge_min": vs_min,
                                    "exit_variant": exit_variant,
                                    "reason": "insufficient_daily",
                                }
                            )
            event_returns_rows.append(
                {
                    "seq_id": seq_id,
                    "symbol": symbol,
                    "category": category,
                    "polarity": polarity,
                    "entry_mode": entry_mode,
                    "eff_date": eff_date,
                    "latency": latency_grid[0],
                    "detection_ts": None,
                    "prev_close": prev_close,
                    "avg20_vol": avg20_vol,
                    "ret_at_detection": None,
                    "vol_surge_at_detection": None,
                    "ret_fwd_30m": None,
                    "ret_fwd_60m": None,
                    "ret_sameday_1515": None,
                    "ret_t1_1515": None,
                    "max_ret_60m": None,
                    "min_ret_60m": None,
                    "locked": False,
                    "skip_reason": "insufficient_daily",
                }
            )
            continue

        if (symbol, eff_date) not in avail_set:
            n_no_1m += 1
            for latency in latency_grid:
                for react_pct in react_grid:
                    for vs_min in volsurge_grid:
                        for exit_variant in exit_variants:
                            all_no_trade_rows.append(
                                {
                                    **common_skip,
                                    "latency": latency,
                                    "react_pct": react_pct,
                                    "vol_surge_min": vs_min,
                                    "exit_variant": exit_variant,
                                    "reason": "no_1m",
                                }
                            )
            event_returns_rows.append(
                {
                    "seq_id": seq_id,
                    "symbol": symbol,
                    "category": category,
                    "polarity": polarity,
                    "entry_mode": entry_mode,
                    "eff_date": eff_date,
                    "latency": latency_grid[0],
                    "detection_ts": None,
                    "prev_close": prev_close,
                    "avg20_vol": avg20_vol,
                    "ret_at_detection": None,
                    "vol_surge_at_detection": None,
                    "ret_fwd_30m": None,
                    "ret_fwd_60m": None,
                    "ret_sameday_1515": None,
                    "ret_t1_1515": None,
                    "max_ret_60m": None,
                    "min_ret_60m": None,
                    "locked": False,
                    "skip_reason": "no_1m",
                }
            )
            continue

        n_with_price_data += 1
        symbol_bars = bars_by_symbol.get(symbol)
        if symbol_bars is None:
            continue

        for latency_i, latency in enumerate(latency_grid):
            es = build_event_series(
                symbol_bars=symbol_bars,
                seq_id=seq_id,
                symbol=symbol,
                category=category,
                polarity=polarity,
                entry_mode=entry_mode,
                eff_date=eff_date,
                announced_at=announced_at,
                prev_close=prev_close,
                avg20_vol=avg20_vol,
                latency=latency,
                vol_curve=vol_curve,
            )

            if latency_i == 0:
                event_returns_rows.append(compute_event_returns_row(es, latency))

            trades, no_trades = simulate_event_cells(es, react_grid, volsurge_grid, exit_variants)
            all_trade_rows.extend(trades)
            all_no_trade_rows.extend(no_trades)

    elapsed = time.time() - t0
    print(f"[run] event loop complete in {elapsed:.1f}s")
    print(f"[run] events with 1m price data available: {n_with_price_data}/{n_events}")
    print(f"[run] events skipped (insufficient_daily): {n_insufficient_daily}")
    print(f"[run] events skipped (no_1m): {n_no_1m}")

    event_returns_df = pd.DataFrame(event_returns_rows)
    trades_df = pd.DataFrame(all_trade_rows)
    if "_charges_detail" in trades_df.columns:
        trades_df = trades_df.drop(columns=["_charges_detail"])
    no_trades_df = pd.DataFrame(all_no_trade_rows)

    print(f"[run] writing results to {out_path} (CREATE OR REPLACE)...")
    out_con = duckdb.connect(out_path)
    out_con.execute("DROP TABLE IF EXISTS trades")
    out_con.execute("DROP TABLE IF EXISTS event_returns")
    out_con.execute("DROP TABLE IF EXISTS no_trade_events")
    out_con.register("trades_df", trades_df)
    out_con.execute("CREATE TABLE trades AS SELECT * FROM trades_df")
    out_con.register("event_returns_df", event_returns_df)
    out_con.execute("CREATE TABLE event_returns AS SELECT * FROM event_returns_df")
    out_con.register("no_trades_df", no_trades_df)
    out_con.execute("CREATE TABLE no_trade_events AS SELECT * FROM no_trades_df")
    out_con.close()

    ann_con.close()
    prices_con.close()

    if print_summary:
        print_summary_report(trades_df)

    return {
        "n_events": n_events,
        "n_with_price_data": n_with_price_data,
        "n_trades": len(trades_df),
        "n_event_returns": len(event_returns_df),
        "trades_df": trades_df,
        "event_returns_df": event_returns_df,
        "no_trades_df": no_trades_df,
        "vol_curve": vol_curve,
    }


def print_summary_report(trades_df: pd.DataFrame) -> None:
    if trades_df.empty:
        print("[summary] no trades to summarize")
        return
    group_cols = [
        "polarity",
        "category",
        "latency",
        "react_pct",
        "vol_surge_min",
        "exit_variant",
    ]
    agg = (
        trades_df.groupby(group_cols)
        .agg(
            n_trades=("net_pnl", "count"),
            hit_rate=("net_pnl", lambda s: (s > 0).mean()),
            avg_net_ret_pct=("net_ret_pct", "mean"),
            median_net_ret_pct=("net_ret_pct", "median"),
            sum_net_pnl=("net_pnl", "sum"),
        )
        .reset_index()
    )
    pd.set_option("display.max_rows", 200)
    pd.set_option("display.width", 200)
    print(agg.sort_values("sum_net_pnl", ascending=False).to_string(index=False))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="R43 news-event-momentum simulator")
    parser.add_argument("--announcements", default=DEFAULT_ANNOUNCEMENTS)
    parser.add_argument("--prices", default=DEFAULT_PRICES)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--latency", type=int, default=90)
    parser.add_argument("--react", type=float, default=2.0)
    parser.add_argument("--volsurge", type=float, default=2.5)
    parser.add_argument("--exit", default="t1_1515", choices=EXIT_VARIANTS)
    parser.add_argument("--full-grid", action="store_true", default=False)
    parser.add_argument("--summary", action="store_true", default=False)
    args = parser.parse_args()

    if args.full_grid:
        latency_grid = LATENCY_GRID
        react_grid = REACT_PCT_GRID
        volsurge_grid = VOLSURGE_GRID
        exit_variants = EXIT_VARIANTS
    else:
        latency_grid = [args.latency]
        react_grid = [args.react]
        volsurge_grid = [args.volsurge]
        exit_variants = [args.exit]

    run(
        announcements_path=args.announcements,
        prices_path=args.prices,
        out_path=args.out,
        latency_grid=latency_grid,
        react_grid=react_grid,
        volsurge_grid=volsurge_grid,
        exit_variants=exit_variants,
        print_summary=args.summary,
    )


if __name__ == "__main__":
    main()
