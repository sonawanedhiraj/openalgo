"""R48 backtest — time-of-day-adjusted volume gate for the chartink-mirror BUY rule.

Replays every scanner-universe symbol-day at 5m bar closes, replicating the 12
gates of services/scan_rules/fno_intraday_buy_chartink.py (live params: gap 1.5%
per CHARTINK_RULE_BUY_GAP_PCT, price 100-5000, vol SMA 50/200, weekly ATR(21)>5%,
15m RSI(14)>50, 5m Supertrend(7,3) x2). Two volume-gate arms:

  baseline : cumvol_t >  SMA50(fullday vol) AND cumvol_t >  SMA200(fullday vol)
  variant K: cumvol_t >= K*f(t)*SMA50       AND cumvol_t >= K*f(t)*SMA200

where f(t) = average cumulative intraday volume fraction by minute-of-day
(computed from the same 1m dataset, floor 0.005 — precedent:
backtest/news_event_study/simulate.py).

Fidelity notes (symmetric across arms, so latency deltas are unaffected):
- RSI(14) computed full-series causal (production recomputes on a rolling
  50-bar window; small absolute drift, same for both arms).
- Supertrend computed full-series causal (production window ~larger of rolling
  frame; band stickiness memory differs marginally).
- bars_15m = CLOSED 15m bars only (matches scanner Closed15mWindow).
- Daily SMA/pivot/gap reference = broker daily bars (production reads
  historify-D which is broker-sourced; assumes full 200-bar depth).

Run from the MAIN repo root (uv run python <this file>) so services.indicators
(pandas_ta_classic) imports from the project venv. Reads ONLY the
outputs/tod_volume_gate/prices.duckdb; writes results parquet + summary JSON next to it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time as _time
from datetime import time as dtime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.indicators import atr, rsi, supertrend  # noqa: E402

_ap = argparse.ArgumentParser()
_ap.add_argument("--start", default="2025-07-01")
_ap.add_argument("--end", default="2026-07-06")
_ap.add_argument("--suffix", default="")
_ap.add_argument("--symbols", default="")
_args = _ap.parse_args()

HERE = REPO_ROOT / "outputs" / "tod_volume_gate"
DB_PATH = HERE / "prices.duckdb"
OUT_PARQUET = HERE / f"results{_args.suffix}.parquet"
OUT_SUMMARY = HERE / f"summary{_args.suffix}.json"

EVAL_START = pd.Timestamp(_args.start)
EVAL_END = pd.Timestamp(_args.end)

# Live-verified parameters (scan_definitions.parameters_json is NULL -> env/defaults)
GAP_PCT = 1.5  # CHARTINK_RULE_BUY_GAP_PCT in live .env
PRICE_MIN, PRICE_MAX = 100.0, 5000.0
ATR_THRESH = 0.05
RSI_THRESH = 50.0
VOL_SMA_S, VOL_SMA_L = 50, 200
F_FLOOR = 0.005
K_VALUES = [0.75, 1.0, 1.25, 1.5, 2.0, 3.0]


def compute_volume_curve(con) -> pd.Series:
    """f(t): average cumulative fraction of the day's volume by minute-of-day,
    across all symbol-days with >= 300 one-minute bars. Indexed 'HH:MM'."""
    q = """
        WITH day_bars AS (
            SELECT symbol, CAST(ts AS DATE) AS d, strftime(ts, '%H:%M') AS mod, ts, volume
            FROM prices_1m
            WHERE strftime(ts, '%H:%M') BETWEEN '09:15' AND '15:29'
        ),
        day_totals AS (
            SELECT symbol, d, SUM(volume) AS tot, COUNT(*) AS n
            FROM day_bars GROUP BY symbol, d HAVING COUNT(*) >= 300
        ),
        cum AS (
            SELECT db.mod,
                   SUM(db.volume) OVER (PARTITION BY db.symbol, db.d ORDER BY db.ts) AS cv,
                   dt.tot
            FROM day_bars db JOIN day_totals dt ON db.symbol = dt.symbol AND db.d = dt.d
        )
        SELECT mod, AVG(CASE WHEN tot > 0 THEN cv * 1.0 / tot END) AS f
        FROM cum GROUP BY mod ORDER BY mod
    """
    df = con.execute(q).df()
    s = df.set_index("mod")["f"].clip(lower=F_FLOOR)
    return s


def weekly_frame(daily: pd.DataFrame, upto_idx: int) -> pd.DataFrame:
    """Weekly OHLCV (ISO Monday weeks) of daily bars [0..upto_idx], last 22 rows.
    The last row is the (possibly partial) week containing daily[upto_idx] —
    matches historify W aggregation + provider weekly_lookback_bars=22."""
    d = daily.iloc[: upto_idx + 1]
    wk = d["bar_date"] - pd.to_timedelta(pd.DatetimeIndex(d["bar_date"]).weekday, unit="D")
    g = d.groupby(wk)
    w = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
        }
    )
    return w.tail(22)


def process_symbol(con, sym: str, vol_curve: pd.Series) -> list[dict]:
    daily = con.execute(
        "SELECT bar_date, open, high, low, close, volume FROM prices_daily "
        "WHERE symbol = ? ORDER BY bar_date",
        [sym],
    ).df()
    if len(daily) < VOL_SMA_L + 5:
        return []
    daily["bar_date"] = pd.to_datetime(daily["bar_date"])
    sma50 = daily["volume"].rolling(VOL_SMA_S).mean().values
    sma200 = daily["volume"].rolling(VOL_SMA_L).mean().values
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
    m1 = m1[(m1["ts"].dt.time >= dtime(9, 15)) & (m1["ts"].dt.time <= dtime(15, 29))]
    if m1.empty:
        return []

    # ---- 5m bars (continuous across days, matches rolling aggregator frame) ----
    b5start = m1["ts"].dt.floor("5min")
    b5 = (
        m1.groupby(b5start)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
        .rename(columns={"ts": "start"})
    )
    b5["close_ts"] = b5["start"] + pd.Timedelta(minutes=5)
    b5["day"] = b5["start"].dt.normalize()
    st_line = supertrend(b5, period=7, multiplier=3.0)["line"].values
    st_prev = np.empty_like(st_line)
    st_prev[0] = np.nan
    st_prev[1:] = st_line[:-1]

    # last 1m minute included in each 5m bucket -> f(t) key
    last_min = m1.groupby(b5start)["ts"].max().dt.strftime("%H:%M").reindex(b5["start"]).values
    f_t = np.array([vol_curve.get(k, F_FLOOR) for k in last_min])

    # ---- 15m closed bars + causal RSI ----
    b15start = m1["ts"].dt.floor("15min")
    b15 = (
        m1.groupby(b15start)
        .agg(close=("close", "last"))
        .reset_index()
        .rename(columns={"ts": "start"})
    )
    b15["close_ts"] = b15["start"] + pd.Timedelta(minutes=15)
    rsi15 = rsi(b15["close"], period=14).values
    # map each 5m close -> latest CLOSED 15m bar
    idx15 = np.searchsorted(b15["close_ts"].values, b5["close_ts"].values, side="right") - 1
    rsi_at5 = np.where(idx15 >= 0, rsi15[np.clip(idx15, 0, None)], np.nan)
    warm15_ok = idx15 >= 14  # rule needs >=15 closed 15m bars

    day_open_map = m1.groupby(m1["ts"].dt.normalize())["open"].first()

    rows: list[dict] = []
    for day, day_b5_idx in b5.groupby("day").indices.items():
        day = pd.Timestamp(day)
        if day < EVAL_START or day > EVAL_END:
            continue
        yi = int(np.searchsorted(d_dates, day.to_datetime64()) - 1)
        if yi < 0 or d_dates[yi] >= day.to_datetime64():
            continue
        s50, s200 = sma50[yi], sma200[yi]
        if np.isnan(s50) or np.isnan(s200):
            continue  # daily warm-up rejection (production: daily_warmup fail)
        yc, yh, yl = d_close[yi], d_high[yi], d_low[yi]
        pivot = (yh + yl + yc) / 3.0
        day_open = float(day_open_map.loc[day])

        # day-constant gates 9/10
        if not (day_open > yc and day_open > pivot):
            continue  # no arm can ever fire this day; skip cheaply

        # weekly ATR (day-constant)
        wf = weekly_frame(daily, yi)
        if len(wf) < 22:
            continue  # weekly warm-up rejection
        watr = atr(wf, period=21).iloc[-1]
        if pd.isna(watr):
            continue

        idx = np.asarray(day_b5_idx)
        # rule warm-ups: >=8 5m bars in frame (full-series index), >=15 closed 15m
        pos_ok = idx >= 8
        close_t = b5["close"].values[idx]
        cumvol = np.cumsum(b5["volume"].values[idx]).astype(float)
        st_now_t = st_line[idx]
        st_prev_t = st_prev[idx]
        rsi_t = rsi_at5[idx]
        ft = f_t[idx]
        ts_t = b5["close_ts"].values[idx]

        gap_mult = 1.0 + GAP_PCT / 100.0
        with np.errstate(invalid="ignore"):
            other = (
                pos_ok
                & warm15_ok[idx]
                & (close_t > PRICE_MIN)
                & (close_t < PRICE_MAX)
                & (close_t > yc * gap_mult)
                & (watr > close_t * ATR_THRESH)
                & (rsi_t > RSI_THRESH)
                & (st_now_t < close_t)
                & (st_prev_t >= yc)
            )
            base_vol = (cumvol > s50) & (cumvol > s200)
        base = other & base_vol

        def first_true(mask) -> int | None:
            nz = np.nonzero(mask)[0]
            return int(nz[0]) if len(nz) else None

        fb = first_true(base)
        fo = first_true(other)
        variant_firsts = {}
        for k in K_VALUES:
            with np.errstate(invalid="ignore"):
                vvol = (cumvol >= k * ft * s50) & (cumvol >= k * ft * s200)
            variant_firsts[k] = first_true(other & vvol)

        if fb is None and fo is None and all(v is None for v in variant_firsts.values()):
            continue  # nothing fired on any arm; don't record

        day_total_vol = float(cumvol[-1])
        day_close = float(close_t[-1])
        # T+1 close from daily series (next settled bar after `day`)
        ni = int(np.searchsorted(d_dates, day.to_datetime64(), side="right"))
        # d_dates[ni] could equal day (daily bar for eval day exists) -> T+1 is ni+1
        while ni < len(d_dates) and d_dates[ni] <= day.to_datetime64():
            ni += 1
        t1_close = float(d_close[ni]) if ni < len(d_dates) else np.nan

        def arm_fields(  # bind loop vars as defaults (B023) — called within this iteration only
            prefix: str,
            fi: int | None,
            *,
            ts_t=ts_t,
            close_t=close_t,
            day=day,
            yc=yc,
            day_close=day_close,
            t1_close=t1_close,
        ) -> dict:
            if fi is None:
                return {
                    f"{prefix}_ts": None,
                    f"{prefix}_min": np.nan,
                    f"{prefix}_price": np.nan,
                    f"{prefix}_ret_at_fire": np.nan,
                    f"{prefix}_ret_to_close": np.nan,
                    f"{prefix}_ret_to_t1": np.nan,
                }
            t = pd.Timestamp(ts_t[fi])
            p = float(close_t[fi])
            return {
                f"{prefix}_ts": t.isoformat(),
                f"{prefix}_min": (t - day).total_seconds() / 60.0 - (9 * 60 + 15),
                f"{prefix}_price": p,
                f"{prefix}_ret_at_fire": p / yc - 1.0,
                f"{prefix}_ret_to_close": day_close / p - 1.0,
                f"{prefix}_ret_to_t1": (t1_close / p - 1.0) if not np.isnan(t1_close) else np.nan,
            }

        row = {
            "symbol": sym,
            "day": day.date().isoformat(),
            "yest_close": float(yc),
            "sma50_vol": float(s50),
            "sma200_vol": float(s200),
            "day_total_vol": day_total_vol,
            "fullday_ratio50": day_total_vol / s50 if s50 > 0 else np.nan,
            "fullday_ratio200": day_total_vol / s200 if s200 > 0 else np.nan,
            "day_close": day_close,
            "t1_close": t1_close,
            "day_ret": day_close / yc - 1.0,
        }
        row.update(arm_fields("base", fb))
        row.update(arm_fields("other", fo))
        for k in K_VALUES:
            row.update(arm_fields(f"k{str(k).replace('.', '_')}", variant_firsts[k]))
        rows.append(row)
    return rows


def main() -> int:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    if _args.symbols:
        syms = [s.strip().upper() for s in _args.symbols.split(",") if s.strip()]
    else:
        syms = [
            r[0]
            for r in con.execute("SELECT DISTINCT symbol FROM prices_1m ORDER BY symbol").fetchall()
        ]
    print(f"symbols with 1m data: {len(syms)}")
    print("computing volume curve f(t)...")
    vol_curve = compute_volume_curve(con)
    print(
        f"  {len(vol_curve)} minute buckets; f(09:19)={vol_curve.get('09:19', np.nan):.4f} "
        f"f(10:19)={vol_curve.get('10:19', np.nan):.4f} f(12:29)={vol_curve.get('12:29', np.nan):.4f}"
    )
    vol_curve.to_frame("f").to_csv(HERE / "volume_curve.csv")

    all_rows: list[dict] = []
    t0 = _time.monotonic()
    for i, sym in enumerate(syms, 1):
        try:
            all_rows.extend(process_symbol(con, sym, vol_curve))
        except Exception as e:  # noqa: BLE001 — record and continue
            print(f"  ERROR {sym}: {e!r}")
        if i % 20 == 0:
            print(
                f"  ... {i}/{len(syms)} ({_time.monotonic() - t0:.0f}s, rows={len(all_rows)})",
                flush=True,
            )
    con.close()

    df = pd.DataFrame(all_rows)
    df.to_parquet(OUT_PARQUET, index=False)
    print(f"rows: {len(df)} -> {OUT_PARQUET}")

    # ---- summary ----
    summary: dict = {"n_symbol_days_recorded": len(df), "k_values": K_VALUES}
    base_days = df[df["base_ts"].notna()]
    summary["baseline_fires"] = len(base_days)
    summary["baseline_fire_min_median"] = (
        float(base_days["base_min"].median()) if len(base_days) else None
    )
    for k in K_VALUES:
        p = f"k{str(k).replace('.', '_')}"
        vd = df[df[f"{p}_ts"].notna()]
        both = df[df["base_ts"].notna() & df[f"{p}_ts"].notna()]
        von = df[df["base_ts"].isna() & df[f"{p}_ts"].notna()]
        bon = df[df["base_ts"].notna() & df[f"{p}_ts"].isna()]
        summary[p] = {
            "fires": len(vd),
            "both": len(both),
            "variant_only": len(von),
            "baseline_only": len(bon),
            "latency_gain_min_median": float((both["base_min"] - both[f"{p}_min"]).median())
            if len(both)
            else None,
            "latency_gain_min_mean": float((both["base_min"] - both[f"{p}_min"]).mean())
            if len(both)
            else None,
            "vonly_ret_to_close_mean": float(von[f"{p}_ret_to_close"].mean()) if len(von) else None,
            "vonly_ret_to_t1_mean": float(von[f"{p}_ret_to_t1"].mean()) if len(von) else None,
            "vonly_fullday_ratio50_lt1_pct": float((von["fullday_ratio50"] < 1).mean())
            if len(von)
            else None,
            "both_ret_to_close_at_variant_fire_mean": float(both[f"{p}_ret_to_close"].mean())
            if len(both)
            else None,
            "both_ret_to_close_at_base_fire_mean": float(both["base_ret_to_close"].mean())
            if len(both)
            else None,
        }
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
