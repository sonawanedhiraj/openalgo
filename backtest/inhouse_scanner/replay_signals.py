"""Stage 1 — In-house scanner signal replay (drives the REAL rule functions).

Unlike ``backtest/tod_volume_gate/replay.py`` (which re-implements the 12 BUY
gates in standalone vectorised code), this harness feeds point-in-time causal
frames into the **actual** Chartink-mirror rule functions
(``services/scan_rules/fno_intraday_{buy,sell}_chartink.py``) so the signals are
byte-faithful to what the live in-house scanner would fire.

Data source: the app-independent ``outputs/tod_volume_gate/prices.duckdb``
(1m + daily, scanner universe) — OpenAlgo holds ``db/historify.duckdb`` open
read-write, so an external process cannot read it. prices.duckdb was built via
the broker historical API (``backtest/tod_volume_gate/fetch_prices.py``).

Faithfulness to the live scanner
--------------------------------
* 5m / 15m frames are **continuous across days** (production keeps a rolling
  100-bar 5m frame and a rolling 15m frame), so Supertrend/RSI are warm from
  day one — matching ``ScannerService._append_bar``'s rolling window.
* ``bars_daily`` / ``bars_weekly`` are point-in-time: only bars settled on or
  before D-1, tail 205 / 22, carrying a ``timestamp`` column so
  ``derive_today_and_yest`` takes production Path B (today's running snapshot
  from today's 5m bars, yest = latest settled daily).
* The rule reads ``datetime.now(IST)`` internally (shallow-daily warn, the
  running-snapshot ``now_ist``, the D-bar staleness guard). We freeze it to the
  simulated 5m-bar-close time via a ``datetime`` subclass monkeypatched onto the
  three rule modules — so the replay is causal, not wall-clock.
* ``reference_certified=True`` is passed in the indicators dict (clean PIT
  daily), skipping the broker-prev-close cross-check that has no registry in a
  backtest. The rule's gap %, price band, SMA windows, ATR/RSI/Supertrend params
  all resolve from the same env the live scanner reads
  (``CHARTINK_RULE_BUY_GAP_PCT=1.5`` etc.).

Tractability
------------
Gates 9 & 10 (``open > yest_close`` and ``open > pivot`` for BUY; flipped for
SELL) are **day-constant** — ``today_d.open`` in Path B is the day's first 5m
open, fixed all session. If they fail, the real rule ``_fail``s at that gate for
every bar, so the day can never fire. We pre-filter on those cheap gates and
only drive the real rule bar-by-bar on surviving symbol-days. This skips ONLY
days the real rule would reject unconditionally, so it changes nothing about
which signals fire — it just avoids ~90% of the work.

Output: a parquet of first-fire-per-(symbol, day, direction) rows.

Run from the repo root:
    uv run python -m backtest.inhouse_scanner.replay_signals \
        --start 2025-06-20 --end 2026-07-06
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time as _time
from datetime import datetime as _dt
from datetime import timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytz

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Quiet the rule modules' WARNING firehose (divergence / shallow-daily / etc.);
# they are observability for the live path, noise in a backtest.
logging.disable(logging.WARNING)

import services.scan_rules._today_running as todaymod  # noqa: E402
import services.scan_rules.fno_intraday_buy_chartink as buymod  # noqa: E402
import services.scan_rules.fno_intraday_sell_chartink as sellmod  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")

DB_PATH = REPO_ROOT / "outputs" / "tod_volume_gate" / "prices.duckdb"
OUT_DIR = REPO_ROOT / "backtest" / "inhouse_scanner"

# Live-faithful gap % (env, same as the scanner reads; default matches the rule).
BUY_GAP_PCT = float(os.environ.get("CHARTINK_RULE_BUY_GAP_PCT", "3.0"))
SELL_GAP_PCT = float(os.environ.get("CHARTINK_RULE_SELL_GAP_PCT", "3.0"))
PRICE_MIN, PRICE_MAX = 100.0, 5000.0


# ── Frozen clock (causal replay) ─────────────────────────────────────────────


class FrozenNow(_dt):
    """``datetime`` subclass whose ``now()`` returns a pinned instant.

    Monkeypatched onto the rule modules so their internal ``datetime.now(IST)``
    calls resolve to the simulated 5m-bar-close time instead of wall-clock.
    """

    frozen: _dt | None = None  # aware IST datetime

    @classmethod
    def now(cls, tz=None):
        f = cls.frozen
        if f is None:
            return _dt.now(tz)
        return f.astimezone(tz) if tz is not None else f.replace(tzinfo=None)


buymod.datetime = FrozenNow
sellmod.datetime = FrozenNow
todaymod.datetime = FrozenNow


def _set_now(bar_close_naive_ist: pd.Timestamp) -> None:
    FrozenNow.frozen = IST.localize(bar_close_naive_ist.to_pydatetime())


# ── Weekly frame (ISO Monday weeks), PIT ─────────────────────────────────────


def weekly_frame(daily: pd.DataFrame, upto_idx: int) -> pd.DataFrame:
    """Weekly OHLCV of daily bars [0..upto_idx], last 22 rows, with a
    ``timestamp`` epoch column so the rule's ``_ts_col`` recognises it."""
    d = daily.iloc[: upto_idx + 1]
    wk_monday = d["bar_date"] - pd.to_timedelta(pd.DatetimeIndex(d["bar_date"]).weekday, unit="D")
    g = d.groupby(wk_monday)
    w = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
        }
    ).tail(22)
    w = w.reset_index(names="bar_date")
    w["timestamp"] = _ist_epoch_seconds(w["bar_date"])
    # Drop the datetime64 ``bar_date`` — a non-numeric column forces the rule's
    # ``bars.iloc[-1]`` row to object dtype, exposing the np.int64 isinstance
    # trap in the rule's _daily_bar_date (epoch read as ns → 1970 → dbar_stale).
    # Production historify frames are all-numeric (timestamp + OHLCV + oi).
    return w[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def _ist_epoch_seconds(bar_dates: pd.Series) -> pd.Series:
    """Epoch seconds for IST-midnight of each date, resolution-independent.

    duckdb DATE → pandas can be datetime64[us] (not [ns]), so a bare
    ``.astype('int64')//10**9`` is off by 1000×. ``Timestamp.timestamp()`` is
    unit-safe (returns POSIX seconds regardless of underlying resolution)."""
    ts = pd.to_datetime(bar_dates).dt.tz_localize("Asia/Kolkata")
    return ts.apply(lambda x: int(x.timestamp())).astype("int64")


def _daily_pit_with_ts(daily: pd.DataFrame, yest_idx: int) -> pd.DataFrame:
    """Settled daily bars ≤ D-1, tail 205, with a ``timestamp`` epoch column."""
    d = daily.iloc[: yest_idx + 1].tail(205).copy()
    d["timestamp"] = _ist_epoch_seconds(d["bar_date"])
    return d[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


# ── Per-symbol replay ────────────────────────────────────────────────────────


def process_symbol(
    con: duckdb.DuckDBPyConnection,
    sym: str,
    eval_start: pd.Timestamp,
    eval_end: pd.Timestamp,
) -> list[dict]:
    daily = con.execute(
        "SELECT bar_date, open, high, low, close, volume FROM prices_daily "
        "WHERE symbol = ? ORDER BY bar_date",
        [sym],
    ).df()
    if len(daily) < 205:
        return []
    daily["bar_date"] = pd.to_datetime(daily["bar_date"])
    d_dates = daily["bar_date"].values
    d_close = daily["close"].values
    d_high = daily["high"].values
    d_low = daily["low"].values

    m1 = con.execute(
        "SELECT ts, open, high, low, close, volume FROM prices_1m WHERE symbol = ? ORDER BY ts",
        [sym],
    ).df()
    if m1.empty:
        return []
    m1 = m1[
        (m1["ts"].dt.time >= pd.Timestamp("09:15").time())
        & (m1["ts"].dt.time <= pd.Timestamp("15:29").time())
    ]
    if m1.empty:
        return []

    # ---- continuous 5m frame (ts = bar start, naive IST) ----
    b5start = m1["ts"].dt.floor("5min")
    df5 = (
        m1.groupby(b5start)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
        .rename(columns={"ts": "ts"})
    )
    df5["close_ts"] = df5["ts"] + pd.Timedelta(minutes=5)
    df5["day"] = df5["ts"].dt.normalize()

    # ---- continuous 15m frame (closed bars) ----
    b15start = m1["ts"].dt.floor("15min")
    df15 = (
        m1.groupby(b15start)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
        .rename(columns={"ts": "ts"})
    )
    df15["close_ts"] = df15["ts"] + pd.Timedelta(minutes=15)
    # For each 5m bar, index of the latest CLOSED 15m bar (close_ts <= 5m close_ts)
    idx15 = np.searchsorted(df15["close_ts"].values, df5["close_ts"].values, side="right") - 1

    close5_ts = df5["close_ts"].values

    rows: list[dict] = []

    for day, day_idx in df5.groupby("day").indices.items():
        day_ts = pd.Timestamp(day)
        if day_ts < eval_start or day_ts > eval_end:
            continue
        # yesterday settled daily bar
        yi = int(np.searchsorted(d_dates, day_ts.to_datetime64()) - 1)
        if yi < 0 or d_dates[yi] >= day_ts.to_datetime64():
            continue
        if yi < 199:  # need 200 settled rows for SMA200 (BUY); harmless for SELL
            pass  # SELL only needs a few; BUY rule itself rejects on warm-up
        yc = float(d_close[yi])
        yh = float(d_high[yi])
        yl = float(d_low[yi])
        pivot = (yh + yl + yc) / 3.0

        idxs = np.asarray(day_idx)
        idxs.sort()
        day_open = float(df5["open"].values[idxs[0]])

        buy_possible = day_open > yc and day_open > pivot
        sell_possible = day_open < yc and day_open < pivot
        if not (buy_possible or sell_possible):
            continue  # gates 9/10 day-constant reject — real rule can't fire

        bars_daily = _daily_pit_with_ts(daily, yi)
        bars_weekly = weekly_frame(daily, yi)

        direction = "BUY" if buy_possible else "SELL"
        rule_fn = buymod.rule if buy_possible else sellmod.rule
        # Gate-1 necessary condition (identical to the real rule's gate 1, since
        # Path-B today_d.close == the current 5m close): only the expensive real
        # rule at bars already clearing the gap. A strict necessary condition —
        # never drops a real fire, just avoids ~all the Supertrend/RSI work.
        if buy_possible:
            gap_thr = yc * (1.0 + BUY_GAP_PCT / 100.0)
        else:
            gap_thr = yc * (1.0 - SELL_GAP_PCT / 100.0)

        fired = False
        for i in idxs:
            if i < 8:  # 5m Supertrend warm-up (rule rejects anyway; skip cheaply)
                continue
            j = int(idx15[i])
            if j < 14:  # 15m RSI(14) warm-up
                continue
            close_i = float(df5["close"].values[i])
            if not (PRICE_MIN < close_i < PRICE_MAX):
                continue
            if buy_possible and close_i <= gap_thr:
                continue  # gate 1 (gap up) cannot pass
            if sell_possible and close_i >= gap_thr:
                continue  # gate 1 (gap down) cannot pass
            bars_5m = df5.iloc[max(0, i - 99) : i + 1][
                ["ts", "open", "high", "low", "close", "volume"]
            ]
            bars_15m = df15.iloc[max(0, j - 49) : j + 1][
                ["ts", "open", "high", "low", "close", "volume"]
            ]
            indicators = {
                "symbol": sym,
                "exchange": "NSE",
                "bars_5m": bars_5m,
                "bars_15m": bars_15m,
                "bars_daily": bars_daily,
                "bars_weekly": bars_weekly,
                "parameters": {},  # → env resolution (live-faithful)
                "reference_certified": True,
            }
            _set_now(pd.Timestamp(close5_ts[i]))
            try:
                hit = rule_fn(bars_5m, indicators)
            except Exception:
                hit = False
            if hit:
                ct = pd.Timestamp(close5_ts[i])
                fire_price = float(df5["close"].values[i])
                rows.append(
                    {
                        "symbol": sym,
                        "day": day_ts.date().isoformat(),
                        "direction": direction,
                        "fire_ts": ct.isoformat(),
                        "fire_min": (ct - day_ts).total_seconds() / 60.0 - (9 * 60 + 20),
                        "fire_price": fire_price,
                        "yest_close": yc,
                        "ret_at_fire": fire_price / yc - 1.0,
                    }
                )
                fired = True
                break
        _ = fired
    return rows


def universe(con: duckdb.DuckDBPyConnection, arg_symbols: str, limit: int) -> list[str]:
    if arg_symbols:
        return [s.strip().upper() for s in arg_symbols.split(",") if s.strip()]
    syms = [
        r[0]
        for r in con.execute("SELECT DISTINCT symbol FROM prices_1m ORDER BY symbol").fetchall()
    ]
    return syms[:limit] if limit else syms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-06-20")
    ap.add_argument("--end", default="2026-07-06")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--limit-symbols", type=int, default=0)
    ap.add_argument("--suffix", default="")
    args = ap.parse_args()

    eval_start = pd.Timestamp(args.start)
    eval_end = pd.Timestamp(args.end)

    con = duckdb.connect(str(DB_PATH), read_only=True)
    syms = universe(con, args.symbols, args.limit_symbols)
    print(f"symbols: {len(syms)}  window: {args.start} .. {args.end}", flush=True)

    all_rows: list[dict] = []
    t0 = _time.monotonic()
    for i, sym in enumerate(syms, 1):
        try:
            all_rows.extend(process_symbol(con, sym, eval_start, eval_end))
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {sym}: {e!r}", flush=True)
        if i % 10 == 0 or i == len(syms):
            el = _time.monotonic() - t0
            print(
                f"  {i}/{len(syms)}  {el:.0f}s  signals={len(all_rows)}  ({el / i:.1f}s/sym)",
                flush=True,
            )
    con.close()

    df = pd.DataFrame(all_rows)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"signals{args.suffix}.parquet"
    df.to_parquet(out, index=False)
    print(f"\nsignals: {len(df)} -> {out}", flush=True)
    if len(df):
        print("by direction:")
        print(df["direction"].value_counts().to_string())
        print(f"median fire minute-into-session: {df['fire_min'].median():.0f}")
        print(f"distinct symbol-days: {len(df.groupby(['symbol', 'day']))}")
        print("sample:")
        print(df.head(12).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
