"""
R44 Delayed-Entry News-Event Pattern Simulator.

R43 proved that CHASING a news-driven spike loses (mean reversion dominates
once charges are included). R44 tests the opposite discipline: wait for the
spike to digest, then enter on a defined-risk reversal/continuation pattern.
All five strategies here are LONG-only.

Reuses the Zerodha MIS/CNC charge model, penny floor, and circuit-lock
detector from ``backtest/news_event_study/simulate.py`` verbatim (imported,
not re-derived) so R43 and R44 price the same trade the same way.

Strategies:
    S1 bull_flag_pullback  -- pole/pullback/reversal continuation (+ a mirrored
                               "recovery" bucket for negative-polarity events).
    S2 no_supply_reversal  -- lowest-volume red candle -> volume-surge green.
    S3 vwap_reclaim        -- spike -> close below VWAP >=2 bars -> reclaim.
    S4 orb                 -- 09:15-09:45 opening-range breakout (at_open only).
    S5 preclose_strength   -- 15:10 momentum/proximity/volume gate, T+1 hold.

Usage:
    uv run python backtest/news_event_study/simulate_entries.py --strategy all
    uv run python backtest/news_event_study/simulate_entries.py --strategy s1
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

# Make the sibling simulate.py importable regardless of CWD (script's own
# directory is normally sys.path[0] already when run directly, but be
# explicit so `python -m` invocation also works).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from simulate import (  # noqa: E402
    ADVERSE_SLIPPAGE,
    ENTRY_CUTOFF,
    MARKET_CLOSE_STAMP,
    MARKET_OPEN,
    PENNY_FLOOR,
    POSITION_SIZE,
    build_daily_context,
    compute_charges,
    compute_locked_flag,
    connect_announcements_readonly,
    connect_prices_readonly,
    detection_timestamp,
    get_1m_availability,
    load_1m_bars_for_symbols,
    load_events,
)

# --------------------------------------------------------------------------
# Constants / grids
# --------------------------------------------------------------------------

DEFAULT_ANNOUNCEMENTS = "outputs/news_event_study/announcements.duckdb"
DEFAULT_PRICES = "outputs/news_event_study/prices.duckdb"
DEFAULT_OUT = "outputs/news_event_study/results_r44.duckdb"

DETECTION_LATENCY_S = 90  # fixed; see detect note below

EXIT_VARIANTS_R44 = ["sameday_1515", "t1_1515"]

RR_MIN_GRID = [1.5, 2.0]  # S1
S2_UP_CONTEXT_GRID = [0.0, 1.0]  # percent
S5_REACT_GRID = [2.0, 3.0]  # percent
S5_PROX_GRID = [1.0, 2.0]  # percent

POLE_GATE_PCT = 0.02  # +/- 2% pole/spike confirmation
GOLDEN_RATIO_RETRACE = 0.382
PULLBACK_MIN_BARS = 2
PULLBACK_MAX_BARS = 8

S2_TRAILING_WINDOW = 6
S2_VOL_MULT_MIN = 2.0
S2_TRAILING_MULT_MIN = 1.25

S3_MIN_BELOW_VWAP_BARS = 2
S3_TRAILING_WINDOW = 6
S3_VOL_MULT_MIN = 1.5

ORB_WARMUP_END = "09:45:00"
S4_OR_VOL_MULT_MIN = 1.5
S4_OR_RANGE_MAX_PCT = 0.04

PRECLOSE_SIGNAL_TS = "15:10:00"
S5_VOL_MULT_MIN = 2.0

STRATEGY_FULL_NAMES = {
    "s1": "bull_flag_pullback",
    "s2": "no_supply_reversal",
    "s3": "vwap_reclaim",
    "s4": "orb",
    "s5": "preclose_strength",
}

# --------------------------------------------------------------------------
# 5-minute bar construction (09:15-anchored) + VWAP
# --------------------------------------------------------------------------


def build_5m_bars(day_1m: pd.DataFrame) -> pd.DataFrame:
    """Resample 1m bars for a single symbol-day into 09:15-anchored 5m bars.

    Columns returned: bin, bar_open_ts (ts of the bar's first 1m row -- this
    is the price used for "next bar open" entries), bar_close_ts
    (bar_open_ts + 5min -- the time at which the bar's OHLC is fully known,
    used for pattern evaluation "on 5m closes"), open, high, low, close,
    volume, cum_vol (cumulative day volume through this bar's close, for
    volume-surge/day-cumvol gates), vwap (cumulative typical-price*volume /
    cumulative volume, sampled at the bar's last 1m row).
    """
    cols = [
        "bin",
        "bar_open_ts",
        "bar_close_ts",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "cum_vol",
        "vwap",
    ]
    if day_1m.empty:
        return pd.DataFrame(columns=cols)

    d = day_1m.sort_values("ts").reset_index(drop=True).copy()
    day_date = d["ts"].iloc[0].date()
    anchor = pd.Timestamp.combine(day_date, pd.Timestamp(MARKET_OPEN).time())
    minute_offset = ((d["ts"] - anchor).dt.total_seconds() // 60).astype(int)
    d["_bin"] = (minute_offset // 5).clip(lower=0)

    tp = (d["high"] + d["low"] + d["close"]) / 3.0
    d["_tpv"] = tp * d["volume"]
    d["_cum_tpv"] = d["_tpv"].cumsum()
    d["_cum_vol"] = d["volume"].cumsum()
    d["_vwap"] = d["_cum_tpv"] / d["_cum_vol"].replace(0, np.nan)

    grp = d.groupby("_bin", sort=True)
    bars = grp.agg(
        bar_open_ts=("ts", "first"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        cum_vol=("_cum_vol", "last"),
        vwap=("_vwap", "last"),
    ).reset_index()
    bars = bars.rename(columns={"_bin": "bin"}).sort_values("bin").reset_index(drop=True)
    bars["bar_close_ts"] = bars["bar_open_ts"] + pd.Timedelta(minutes=5)
    return bars[cols]


# --------------------------------------------------------------------------
# Generic entry / exit resolution
# --------------------------------------------------------------------------


def resolve_entry(
    bars: pd.DataFrame, signal_idx: int, mode: str, trigger_level: float | None = None
):
    """Resolve an entry from a signal bar index.

    mode='next_open': enter at the next bar's open (the standard convention
        used by S2/S3/S4/S5).
    mode='confirm_high': (S1 only) the next bar must actually trade above
        `trigger_level` (the reversal candle's high); entry price is
        max(next bar open, trigger_level).
    Adverse slippage (long-only) is applied on top; qty sized off
    POSITION_SIZE, matching R43.
    """
    n = len(bars)
    next_idx = signal_idx + 1
    if next_idx >= n:
        return None, "no_next_bar"
    nxt = bars.iloc[next_idx]
    if mode == "next_open":
        raw = float(nxt.open)
    elif mode == "confirm_high":
        if nxt.high <= trigger_level:
            return None, "no_breakout_confirm"
        raw = max(float(nxt.open), float(trigger_level))
    else:
        raise ValueError(f"unknown entry mode {mode}")

    entry_px = raw * (1 + ADVERSE_SLIPPAGE)
    qty = int(POSITION_SIZE // entry_px) if entry_px > 0 else 0
    if qty <= 0:
        return None, "qty_zero"
    return {
        "entry_idx": next_idx,
        "entry_ts": nxt.bar_open_ts,
        "entry_px": entry_px,
        "qty": qty,
    }, None


def resolve_exit(
    bars: pd.DataFrame,
    t1_bars: pd.DataFrame,
    entry_idx: int,
    stop_px: float | None,
    target_px: float | None,
    exit_variant: str,
    eff_date,
):
    """Walk forward from entry checking 5m bar high/low for stop/target
    crossings (stop priority if both trigger in the same bar); if neither
    hits by the exit_variant's terminal time, exit at that bar's close.
    Exit slippage (0.25%, adverse) applied uniformly. Returns
    (result_dict, None) or (None, reason).
    """
    n = len(bars)
    close_1515 = pd.Timestamp.combine(eff_date, pd.Timestamp(MARKET_CLOSE_STAMP).time())

    if exit_variant == "sameday_1515":
        idxs = [i for i in range(entry_idx, n) if bars.bar_close_ts.iloc[i] <= close_1515]
        if not idxs:
            return None, "after_close"
        seq_bars = bars.iloc[idxs].reset_index(drop=True)
        is_t1_seq = [False] * len(idxs)
    elif exit_variant == "t1_1515":
        if t1_bars is None or t1_bars.empty:
            return None, "no_t1_data"
        t1_date = t1_bars.bar_close_ts.iloc[0].date()
        t1_close_1515 = pd.Timestamp.combine(t1_date, pd.Timestamp(MARKET_CLOSE_STAMP).time())
        same_day_idxs = list(range(entry_idx, n))
        t1_idxs = [i for i in range(len(t1_bars)) if t1_bars.bar_close_ts.iloc[i] <= t1_close_1515]
        if not t1_idxs:
            t1_idxs = list(
                range(len(t1_bars))
            )  # T+1 data present but doesn't reach 15:15; use last available
        frames = []
        if same_day_idxs:
            frames.append(bars.iloc[same_day_idxs])
        frames.append(t1_bars.iloc[t1_idxs])
        seq_bars = pd.concat(frames, ignore_index=True)
        is_t1_seq = [False] * len(same_day_idxs) + [True] * len(t1_idxs)
    else:
        raise ValueError(f"unknown exit_variant {exit_variant}")

    if seq_bars.empty:
        return None, "no_bars_to_exit"

    for j in range(len(seq_bars)):
        row = seq_bars.iloc[j]
        stop_hit = stop_px is not None and row.low <= stop_px
        target_hit = target_px is not None and row.high >= target_px
        if stop_hit:
            return _finalize_exit(stop_px, "stop", row.bar_close_ts, is_t1_seq[j]), None
        if target_hit:
            return _finalize_exit(target_px, "target", row.bar_close_ts, is_t1_seq[j]), None

    last = seq_bars.iloc[-1]
    return _finalize_exit(float(last.close), "eod", last.bar_close_ts, is_t1_seq[-1]), None


def _finalize_exit(px_raw: float, reason: str, ts, overnight: bool) -> dict:
    exit_px = px_raw * (1 - ADVERSE_SLIPPAGE)
    return {"exit_px": exit_px, "exit_reason": reason, "exit_ts": ts, "overnight": overnight}


def build_trade_row(
    strategy_params: dict,
    exit_variant: str,
    entry: dict,
    stop_px,
    target_px,
    rr_at_entry,
    exit_res: dict,
) -> dict:
    qty = entry["qty"]
    entry_px = entry["entry_px"]
    exit_px = exit_res["exit_px"]
    gross_pnl = (exit_px - entry_px) * qty
    charges = compute_charges(
        direction="long",
        overnight=exit_res["overnight"],
        entry_px=entry_px,
        exit_px=exit_px,
        qty=qty,
    )
    total_charges = charges["total"]
    net_pnl = gross_pnl - total_charges
    position_value = entry_px * qty
    net_ret_pct = (net_pnl / position_value * 100.0) if position_value > 0 else None
    return {
        **strategy_params,
        "exit_variant": exit_variant,
        "entry_ts": entry["entry_ts"],
        "entry_px": entry_px,
        "stop_px": stop_px,
        "target_px": target_px,
        "rr_at_entry": rr_at_entry,
        "exit_ts": exit_res["exit_ts"],
        "exit_px": exit_px,
        "exit_reason": exit_res["exit_reason"],
        "qty": qty,
        "gross_pnl": gross_pnl,
        "charges": total_charges,
        "net_pnl": net_pnl,
        "net_ret_pct": net_ret_pct,
    }


# --------------------------------------------------------------------------
# S1 bull_flag_pullback (+ mirrored down_recovery bucket)
# --------------------------------------------------------------------------


def detect_s1(
    bars: pd.DataFrame, prev_close: float, day_open: float, detection_ts, polarity: str
) -> dict | None:
    """Pole -> pullback (>=38.2% retrace, 2-8 bars, declining volume) ->
    green reversal candle closing above the prior candle's high, with low
    within 1.5% of the pullback low.

    'up' bucket (positive/results/tape_decide): requires a bar after
    detection with ret >= +2%; pole top H = running high; pullback measured
    against (H - max(prev_close, day_open)).

    'down_recovery' bucket (negative): requires a bar after detection with
    ret <= -2%. The day's post-detection low L anchors a fresh pole for the
    BOUNCE off that low (running high since L is the pole top, playing the
    role of H); pullback measured against (bounce_top - L). This is the
    literal reading of the spec's "logic identical from the pullback low" --
    both buckets end in the same green-reversal-breaks-above-pullback-high
    LONG entry; only the anchor differs (fixed floor vs. discovered low).
    """
    n = len(bars)
    if n == 0 or prev_close is None or prev_close <= 0:
        return None
    if polarity in ("positive", "results", "tape_decide"):
        bucket = "up"
    elif polarity == "negative":
        bucket = "down_recovery"
    else:
        return None

    start_idx = None
    for i in range(n):
        if bars.bar_close_ts.iloc[i] > detection_ts:
            start_idx = i
            break
    if start_idx is None:
        return None

    cutoff_ts = pd.Timestamp.combine(
        bars.bar_close_ts.iloc[0].date(), pd.Timestamp(ENTRY_CUTOFF).time()
    )

    pole_floor = max(prev_close, day_open) if bucket == "up" else None
    pole_top = None
    pole_top_idx = None
    pole_max_vol = None
    gate_reached = False
    pullback_idxs: list[int] = []

    for i in range(start_idx, n):
        row = bars.iloc[i]
        ret_i = (row.close / prev_close) - 1.0

        if bucket == "up":
            if pole_top is None or row.high > pole_top:
                pole_top = row.high
                pole_top_idx = i
                pole_max_vol = row.volume
                pullback_idxs = []
            else:
                pole_max_vol = max(pole_max_vol, row.volume)
            if ret_i >= POLE_GATE_PCT:
                gate_reached = True
        else:  # down_recovery
            if ret_i <= -POLE_GATE_PCT:
                gate_reached = True
            if pole_floor is None or row.low < pole_floor:
                pole_floor = row.low
                pole_top = row.high
                pole_top_idx = i
                pole_max_vol = row.volume
                pullback_idxs = []
                continue
            if row.high > pole_top:
                pole_top = row.high
                pole_top_idx = i
                pole_max_vol = row.volume
                pullback_idxs = []
            else:
                pole_max_vol = max(pole_max_vol, row.volume)

        if not gate_reached or i <= pole_top_idx:
            continue

        pullback_idxs.append(i)
        denom = pole_top - pole_floor
        if denom is None or denom <= 0:
            continue
        pullback_low_so_far = min(bars.low.iloc[j] for j in pullback_idxs)
        retrace_frac = (pole_top - pullback_low_so_far) / denom
        n_pb = len(pullback_idxs)
        if n_pb > PULLBACK_MAX_BARS or n_pb < PULLBACK_MIN_BARS:
            continue
        if retrace_frac < GOLDEN_RATIO_RETRACE:
            continue
        if row.volume >= pole_max_vol:
            continue  # not declining volume vs. the pole bar
        if row.bar_close_ts > cutoff_ts:
            continue
        is_green = row.close > row.open
        if not is_green:
            continue
        prior = bars.iloc[i - 1]
        if row.close <= prior.high:
            continue
        if (
            pullback_low_so_far <= 0
            or abs(row.low - pullback_low_so_far) / pullback_low_so_far > 0.015
        ):
            continue

        stop_px = pullback_low_so_far * (1 - 0.001)
        return {
            "signal_idx": i,
            "stop_px": stop_px,
            "target_px": pole_top,
            "trigger_level": row.high,
            "bucket": bucket,
        }

    return None


def run_s1(
    day5m, t1_5m, day_1m, day_open, prev_close, avg20_vol, detection_ts, eff_date, polarity, **_
):
    sig = detect_s1(day5m, prev_close, day_open, detection_ts, polarity)
    if sig is None:
        return [], "no_pattern"
    entry, entry_reason = resolve_entry(
        day5m, sig["signal_idx"], "confirm_high", sig["trigger_level"]
    )
    if entry is None:
        return [], entry_reason
    stop_px, target_px = sig["stop_px"], sig["target_px"]
    if entry["entry_px"] <= stop_px:
        return [], "invalid_stop"

    trades = []
    any_rr_ok = False
    last_reason = "rr_below_min"
    for rr_min in RR_MIN_GRID:
        rr_at_entry = (target_px - entry["entry_px"]) / (entry["entry_px"] - stop_px)
        if rr_at_entry < rr_min:
            continue
        any_rr_ok = True
        for exit_variant in EXIT_VARIANTS_R44:
            exit_res, exit_reason = resolve_exit(
                day5m, t1_5m, entry["entry_idx"], stop_px, target_px, exit_variant, eff_date
            )
            if exit_res is None:
                last_reason = exit_reason
                continue
            trades.append(
                build_trade_row(
                    {
                        "rr_min": rr_min,
                        "up_context_pct": None,
                        "react_pct": None,
                        "prox_pct": None,
                        "bucket": sig["bucket"],
                    },
                    exit_variant,
                    entry,
                    stop_px,
                    target_px,
                    rr_at_entry,
                    exit_res,
                )
            )
    if not trades:
        return [], (last_reason if any_rr_ok else "rr_below_min")
    return trades, None


# --------------------------------------------------------------------------
# S2 no_supply_reversal
# --------------------------------------------------------------------------


def detect_s2(
    bars: pd.DataFrame, detection_ts, prev_close: float, up_context_min_frac: float
) -> dict | None:
    """A RED 5m candle whose volume is the lowest of its trailing 6 (itself
    included), with ret-vs-prev_close at that bar >= up_context_min_frac,
    followed by a GREEN candle with volume >= 2x the red candle's volume AND
    >= 1.25x the same trailing-6 average. Returns signal_idx = the GREEN
    (confirmation) bar's index; entry is the bar after that.
    """
    n = len(bars)
    if n < S2_TRAILING_WINDOW + 2 or prev_close is None or prev_close <= 0:
        return None
    cutoff_ts = pd.Timestamp.combine(
        bars.bar_close_ts.iloc[0].date(), pd.Timestamp(ENTRY_CUTOFF).time()
    )

    for i in range(S2_TRAILING_WINDOW - 1, n - 1):
        row = bars.iloc[i]
        if row.bar_close_ts <= detection_ts:
            continue
        if row.bar_close_ts > cutoff_ts:
            break
        if row.close >= row.open:
            continue  # not red
        window = bars.iloc[i - S2_TRAILING_WINDOW + 1 : i + 1]
        if row.volume > window.volume.min():
            continue  # not the lowest of the trailing 6
        ret_i = (row.close / prev_close) - 1.0
        if ret_i < up_context_min_frac:
            continue
        trailing_avg = window.volume.mean()
        green = bars.iloc[i + 1]
        if green.close <= green.open:
            continue
        if green.volume < S2_VOL_MULT_MIN * row.volume:
            continue
        if green.volume < S2_TRAILING_MULT_MIN * trailing_avg:
            continue
        stop_px = row.low * (1 - 0.001)
        return {"signal_idx": i + 1, "stop_px": stop_px}
    return None


def run_s2(
    day5m, t1_5m, day_1m, day_open, prev_close, avg20_vol, detection_ts, eff_date, polarity, **_
):
    trades = []
    last_reason = "no_pattern"
    for up_ctx_pct in S2_UP_CONTEXT_GRID:
        sig = detect_s2(day5m, detection_ts, prev_close, up_ctx_pct / 100.0)
        if sig is None:
            last_reason = "no_pattern"
            continue
        entry, entry_reason = resolve_entry(day5m, sig["signal_idx"], "next_open")
        if entry is None:
            last_reason = entry_reason
            continue
        stop_px = sig["stop_px"]
        if entry["entry_px"] <= stop_px:
            last_reason = "invalid_stop"
            continue
        target_px = entry["entry_px"] + 2.0 * (entry["entry_px"] - stop_px)
        for exit_variant in EXIT_VARIANTS_R44:
            exit_res, exit_reason = resolve_exit(
                day5m, t1_5m, entry["entry_idx"], stop_px, target_px, exit_variant, eff_date
            )
            if exit_res is None:
                last_reason = exit_reason
                continue
            trades.append(
                build_trade_row(
                    {
                        "rr_min": None,
                        "up_context_pct": up_ctx_pct,
                        "react_pct": None,
                        "prox_pct": None,
                        "bucket": None,
                    },
                    exit_variant,
                    entry,
                    stop_px,
                    target_px,
                    2.0,
                    exit_res,
                )
            )
    if not trades:
        return [], last_reason
    return trades, None


# --------------------------------------------------------------------------
# S3 vwap_reclaim
# --------------------------------------------------------------------------


def detect_s3(bars: pd.DataFrame, detection_ts, prev_close: float) -> dict | None:
    n = len(bars)
    if prev_close is None or prev_close <= 0:
        return None
    cutoff_ts = pd.Timestamp.combine(
        bars.bar_close_ts.iloc[0].date(), pd.Timestamp(ENTRY_CUTOFF).time()
    )

    spike_idx = None
    for i in range(n):
        if bars.bar_close_ts.iloc[i] <= detection_ts:
            continue
        ret_i = (bars.close.iloc[i] / prev_close) - 1.0
        if ret_i >= POLE_GATE_PCT:
            spike_idx = i
            break
    if spike_idx is None:
        return None

    below_streak: list[int] = []
    for i in range(spike_idx + 1, n - 1):
        row = bars.iloc[i]
        if pd.isna(row.vwap):
            below_streak = []
            continue
        if row.close < row.vwap:
            below_streak.append(i)
            continue
        if len(below_streak) >= S3_MIN_BELOW_VWAP_BARS:
            if row.bar_close_ts > cutoff_ts:
                return None
            lo = max(0, i - S3_TRAILING_WINDOW + 1)
            trailing_avg = bars.volume.iloc[lo : i + 1].mean()
            if row.volume >= S3_VOL_MULT_MIN * trailing_avg:
                stop_px = bars.low.iloc[below_streak].min() * (1 - 0.001)
                return {"signal_idx": i, "stop_px": stop_px}
        below_streak = []
    return None


def run_s3(
    day5m, t1_5m, day_1m, day_open, prev_close, avg20_vol, detection_ts, eff_date, polarity, **_
):
    sig = detect_s3(day5m, detection_ts, prev_close)
    if sig is None:
        return [], "no_pattern"
    entry, entry_reason = resolve_entry(day5m, sig["signal_idx"], "next_open")
    if entry is None:
        return [], entry_reason
    stop_px = sig["stop_px"]
    if entry["entry_px"] <= stop_px:
        return [], "invalid_stop"
    target_px = entry["entry_px"] + 2.0 * (entry["entry_px"] - stop_px)

    trades = []
    last_reason = "no_exit"
    for exit_variant in EXIT_VARIANTS_R44:
        exit_res, exit_reason = resolve_exit(
            day5m, t1_5m, entry["entry_idx"], stop_px, target_px, exit_variant, eff_date
        )
        if exit_res is None:
            last_reason = exit_reason
            continue
        trades.append(
            build_trade_row(
                {
                    "rr_min": None,
                    "up_context_pct": None,
                    "react_pct": None,
                    "prox_pct": None,
                    "bucket": None,
                },
                exit_variant,
                entry,
                stop_px,
                target_px,
                2.0,
                exit_res,
            )
        )
    if not trades:
        return [], last_reason
    return trades, None


# --------------------------------------------------------------------------
# S4 orb
# --------------------------------------------------------------------------


def detect_s4(day_1m: pd.DataFrame, bars: pd.DataFrame, prev_close: float) -> dict | None:
    if day_1m.empty or bars.empty or prev_close is None or prev_close <= 0:
        return None
    day_date = day_1m.ts.iloc[0].date()
    or_end_ts = pd.Timestamp.combine(day_date, pd.Timestamp(ORB_WARMUP_END).time())
    or_bars = day_1m[day_1m.ts < or_end_ts]
    if len(or_bars) < 25:  # require substantially all 30 OR minutes present
        return None
    or_high = or_bars.high.max()
    or_low = or_bars.low.min()
    or_range_pct = (or_high - or_low) / prev_close
    if or_range_pct > S4_OR_RANGE_MAX_PCT:
        return None
    or_avg_vol_per_5m = or_bars.volume.sum() / 6.0
    cutoff_ts = pd.Timestamp.combine(day_date, pd.Timestamp(ENTRY_CUTOFF).time())

    n = len(bars)
    for i in range(n - 1):
        row = bars.iloc[i]
        if row.bar_close_ts <= or_end_ts:
            continue
        if row.bar_close_ts > cutoff_ts:
            break
        if row.close <= or_high:
            continue
        if row.volume < S4_OR_VOL_MULT_MIN * or_avg_vol_per_5m:
            continue
        return {"signal_idx": i, "stop_px": or_low, "or_high": or_high, "or_low": or_low}
    return None


def run_s4(
    day5m, t1_5m, day_1m, day_open, prev_close, avg20_vol, detection_ts, eff_date, polarity, **_
):
    sig = detect_s4(day_1m, day5m, prev_close)
    if sig is None:
        return [], "no_pattern_or_or_range"
    entry, entry_reason = resolve_entry(day5m, sig["signal_idx"], "next_open")
    if entry is None:
        return [], entry_reason
    stop_px = sig["stop_px"]
    if entry["entry_px"] <= stop_px:
        return [], "invalid_stop"
    target_px = entry["entry_px"] + 2.0 * (entry["entry_px"] - stop_px)

    trades = []
    last_reason = "no_exit"
    for exit_variant in EXIT_VARIANTS_R44:
        exit_res, exit_reason = resolve_exit(
            day5m, t1_5m, entry["entry_idx"], stop_px, target_px, exit_variant, eff_date
        )
        if exit_res is None:
            last_reason = exit_reason
            continue
        trades.append(
            build_trade_row(
                {
                    "rr_min": None,
                    "up_context_pct": None,
                    "react_pct": None,
                    "prox_pct": None,
                    "bucket": None,
                },
                exit_variant,
                entry,
                stop_px,
                target_px,
                2.0,
                exit_res,
            )
        )
    if not trades:
        return [], last_reason
    return trades, None


# --------------------------------------------------------------------------
# S5 preclose_strength
# --------------------------------------------------------------------------


def detect_s5(
    bars: pd.DataFrame,
    prev_close: float,
    avg20_vol: float,
    react_min_frac: float,
    prox_max_frac: float,
) -> dict | None:
    if bars.empty or prev_close is None or prev_close <= 0:
        return None
    if avg20_vol is None or avg20_vol <= 0:
        return None
    day_date = bars.bar_close_ts.iloc[0].date()
    sig_ts = pd.Timestamp.combine(day_date, pd.Timestamp(PRECLOSE_SIGNAL_TS).time())
    match_idx = bars.index[bars.bar_close_ts == sig_ts]
    if len(match_idx) == 0:
        return None
    idx = int(match_idx[0])
    row = bars.iloc[idx]

    ret_i = (row.close / prev_close) - 1.0
    if ret_i < react_min_frac:
        return None
    day_high_so_far = bars.high.iloc[: idx + 1].max()
    if day_high_so_far <= 0:
        return None
    prox = (day_high_so_far - row.close) / day_high_so_far
    if prox > prox_max_frac:
        return None
    if row.cum_vol < S5_VOL_MULT_MIN * avg20_vol:
        return None
    return {"signal_idx": idx}


def run_s5(
    day5m, t1_5m, day_1m, day_open, prev_close, avg20_vol, detection_ts, eff_date, polarity, **_
):
    trades = []
    last_reason = "no_pattern"
    for react_pct in S5_REACT_GRID:
        for prox_pct in S5_PROX_GRID:
            sig = detect_s5(day5m, prev_close, avg20_vol, react_pct / 100.0, prox_pct / 100.0)
            if sig is None:
                last_reason = "no_pattern"
                continue
            entry, entry_reason = resolve_entry(day5m, sig["signal_idx"], "next_open")
            if entry is None:
                last_reason = entry_reason
                continue
            exit_res, exit_reason = resolve_exit(
                day5m, t1_5m, entry["entry_idx"], None, None, "t1_1515", eff_date
            )
            if exit_res is None:
                last_reason = exit_reason
                continue
            trades.append(
                build_trade_row(
                    {
                        "rr_min": None,
                        "up_context_pct": None,
                        "react_pct": react_pct,
                        "prox_pct": prox_pct,
                        "bucket": None,
                    },
                    "t1_1515",
                    entry,
                    None,
                    None,
                    None,
                    exit_res,
                )
            )
    if not trades:
        return [], last_reason
    return trades, None


STRATEGY_RUNNERS = {"s1": run_s1, "s2": run_s2, "s3": run_s3, "s4": run_s4, "s5": run_s5}


def eligible_strategies(polarity: str, entry_mode: str, requested: list[str]) -> list[str]:
    elig = []
    if "s1" in requested and polarity in ("positive", "results", "tape_decide", "negative"):
        elig.append("s1")
    if "s2" in requested:
        elig.append("s2")
    if "s3" in requested and polarity in ("positive", "results", "tape_decide"):
        elig.append("s3")
    if (
        "s4" in requested
        and entry_mode == "at_open"
        and polarity in ("positive", "results", "tape_decide")
    ):
        elig.append("s4")
    if "s5" in requested:
        elig.append("s5")
    return elig


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run(announcements_path: str, prices_path: str, out_path: str, strategies: list[str]) -> dict:
    t0 = time.time()
    ann_con = connect_announcements_readonly(announcements_path)
    prices_con = connect_prices_readonly(prices_path)

    print("[run] loading events...")
    events = load_events(ann_con)
    print(f"[run] {len(events)} events loaded")

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

    # dict(groupby) raises TypeError (DataFrameGroupBy iteration is incompatible
    # with the dict() constructor call form) — same rationale as simulate.py.
    bars_by_symbol = {sym: df for sym, df in all_1m.groupby("symbol")} if not all_1m.empty else {}  # noqa: C416

    all_trades: list[dict] = []
    all_no_trades: list[dict] = []
    n_events = len(events)

    for row_i, ev in enumerate(events.itertuples(index=False)):
        if row_i % 2000 == 0 and row_i > 0:
            print(f"[run] {row_i}/{n_events} events processed ({time.time() - t0:.1f}s elapsed)")

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

        eligible = eligible_strategies(polarity, entry_mode, strategies)
        if not eligible:
            continue

        def emit_block(reason, eligible=eligible, seq_id=seq_id):
            for s in eligible:
                all_no_trades.append(
                    {"strategy": STRATEGY_FULL_NAMES[s], "seq_id": seq_id, "reason": reason}
                )

        if prev_close is None or pd.isna(prev_close) or n_prior_days is None or n_prior_days < 10:
            emit_block("insufficient_daily")
            continue
        if prev_close < PENNY_FLOOR:
            emit_block("penny")
            continue
        if (symbol, eff_date) not in avail_set:
            emit_block("no_1m")
            continue

        symbol_bars = bars_by_symbol.get(symbol)
        if symbol_bars is None:
            emit_block("no_1m")
            continue

        day_1m = symbol_bars[symbol_bars["bar_date"] == eff_date]
        if day_1m.empty:
            emit_block("no_1m")
            continue

        t1_1m = pd.DataFrame()
        for offset in range(1, 6):
            cand_date = eff_date + timedelta(days=offset)
            cand = symbol_bars[symbol_bars["bar_date"] == cand_date]
            if not cand.empty:
                t1_1m = cand
                break

        day5m = build_5m_bars(day_1m)
        if day5m.empty:
            emit_block("no_1m")
            continue
        day_open = float(day5m["open"].iloc[0])
        t1_5m = build_5m_bars(t1_1m) if not t1_1m.empty else pd.DataFrame()

        detection_ts = detection_timestamp(entry_mode, eff_date, announced_at, DETECTION_LATENCY_S)
        pre_detect = day_1m[day_1m["ts"] <= detection_ts]
        if not pre_detect.empty and prev_close:
            detection_ret = (pre_detect["close"].iloc[-1] / prev_close) - 1.0
        else:
            detection_ret = 0.0
        locked = compute_locked_flag(day_1m, 0, detection_ret)
        if locked:
            emit_block("locked")
            continue

        common = {
            "seq_id": seq_id,
            "symbol": symbol,
            "category": category,
            "polarity": polarity,
            "entry_mode": entry_mode,
        }

        for strat in eligible:
            strat_trades, reason = STRATEGY_RUNNERS[strat](
                day5m=day5m,
                t1_5m=t1_5m,
                day_1m=day_1m,
                day_open=day_open,
                prev_close=prev_close,
                avg20_vol=avg20_vol,
                detection_ts=detection_ts,
                eff_date=eff_date,
                polarity=polarity,
            )
            strat_full = STRATEGY_FULL_NAMES[strat]
            if strat_trades:
                for tr in strat_trades:
                    all_trades.append({**common, "strategy": strat_full, **tr})
            else:
                all_no_trades.append(
                    {"strategy": strat_full, "seq_id": seq_id, "reason": reason or "no_pattern"}
                )

    elapsed = time.time() - t0
    print(f"[run] event loop complete in {elapsed:.1f}s")

    trades_df = pd.DataFrame(all_trades)
    no_trades_df = pd.DataFrame(all_no_trades)

    print(f"[run] writing results to {out_path} (CREATE OR REPLACE)...")
    out_con = duckdb.connect(out_path)
    out_con.execute("DROP TABLE IF EXISTS trades_r44")
    out_con.execute("DROP TABLE IF EXISTS no_trade_r44")
    out_con.register("trades_df", trades_df)
    out_con.execute("CREATE TABLE trades_r44 AS SELECT * FROM trades_df")
    out_con.register("no_trades_df", no_trades_df)
    out_con.execute("CREATE TABLE no_trade_r44 AS SELECT * FROM no_trades_df")
    out_con.close()

    ann_con.close()
    prices_con.close()

    print_summary(trades_df)

    return {
        "n_events": n_events,
        "n_trades": len(trades_df),
        "n_no_trades": len(no_trades_df),
        "trades_df": trades_df,
        "no_trades_df": no_trades_df,
        "elapsed_s": elapsed,
    }


def print_summary(trades_df: pd.DataFrame) -> None:
    if trades_df.empty:
        print("[summary] no trades produced by any strategy")
        return

    param_cols_map = {
        "bull_flag_pullback": ["bucket", "rr_min", "exit_variant"],
        "no_supply_reversal": ["up_context_pct", "exit_variant"],
        "vwap_reclaim": ["exit_variant"],
        "orb": ["exit_variant"],
        "preclose_strength": ["react_pct", "prox_pct"],
    }
    pd.set_option("display.max_rows", 300)
    pd.set_option("display.width", 220)

    for strat in sorted(trades_df["strategy"].unique()):
        group = trades_df[trades_df["strategy"] == strat]
        print(f"\n=== {strat} : per polarity x param combo ===")
        cols = ["polarity"] + param_cols_map.get(strat, [])
        agg = (
            group.groupby(cols, dropna=False)
            .agg(
                n=("net_pnl", "count"),
                hit_rate=("net_pnl", lambda s: (s > 0).mean()),
                avg_net_ret_pct=("net_ret_pct", "mean"),
                median_net_ret_pct=("net_ret_pct", "median"),
                total_net_pnl=("net_pnl", "sum"),
                avg_rr_at_entry=("rr_at_entry", "mean"),
            )
            .reset_index()
        )
        print(agg.sort_values("total_net_pnl", ascending=False).to_string(index=False))

        n = len(group)
        hit_rate = (group["net_pnl"] > 0).mean()
        avg_ret = group["net_ret_pct"].mean()
        med_ret = group["net_ret_pct"].median()
        total_pnl = group["net_pnl"].sum()
        avg_rr = group["rr_at_entry"].mean()
        print(
            f"[{strat} POOLED] n={n} hit_rate={hit_rate:.3f} avg_net_ret_pct={avg_ret:.3f} "
            f"median_net_ret_pct={med_ret:.3f} total_net_pnl={total_pnl:.1f} avg_rr_at_entry={avg_rr}"
        )
        if n < 30:
            print(f"[{strat}] INSUFFICIENT -- only {n} trades total (<30 per project protocol)")

    print("\n=== Top 20 param combos across all strategies by total_net_pnl ===")
    all_cols = [
        "strategy",
        "polarity",
        "bucket",
        "rr_min",
        "up_context_pct",
        "react_pct",
        "prox_pct",
        "exit_variant",
    ]
    present_cols = [c for c in all_cols if c in trades_df.columns]
    top = (
        trades_df.groupby(present_cols, dropna=False)
        .agg(
            n=("net_pnl", "count"),
            hit_rate=("net_pnl", lambda s: (s > 0).mean()),
            total_net_pnl=("net_pnl", "sum"),
        )
        .reset_index()
        .sort_values("total_net_pnl", ascending=False)
        .head(20)
    )
    print(top.to_string(index=False))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="R44 delayed-entry news-event pattern simulator")
    parser.add_argument("--announcements", default=DEFAULT_ANNOUNCEMENTS)
    parser.add_argument("--prices", default=DEFAULT_PRICES)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--strategy", default="all", choices=["all", "s1", "s2", "s3", "s4", "s5"])
    args = parser.parse_args()

    strategies = ["s1", "s2", "s3", "s4", "s5"] if args.strategy == "all" else [args.strategy]

    run(
        announcements_path=args.announcements,
        prices_path=args.prices,
        out_path=args.out,
        strategies=strategies,
    )


if __name__ == "__main__":
    main()
