"""Stage 2 — Run in-house-scanner signals through the SimplifiedStockEngine.

Reads the Stage-1 ``signals*.parquet`` (first-fire per symbol-day-direction from
the REAL Chartink-mirror rules) and replays each trading day through the actual
``SimplifiedStockEngine`` (``services/simplified_stock_engine_core.py``,
``mode=disabled``) — the same execution core the live engine and
``backtest/run_backtest.py`` use.

Faithful to live: a scanner hit only ARMS a symbol; the engine then looks for its
own 5m breakout off the last opposite-colour reference candle with ATR/volume
confirmation. So each symbol is armed at its fire time (history seeded up to that
bar), and only candles AFTER the fire time can trigger an entry. One engine
instance per day shares the global risk state (``max_trades_per_day``, cooldown,
same-day stop-out block, global profit lock) exactly as the live singleton does.

Config comes from the live ``SIMPLIFIED_ENGINE_*`` env (``config_from_env``),
forced to ``mode=disabled`` so nothing touches sandbox.db or the broker.

Run:
    uv run python -m backtest.inhouse_scanner.replay_engine --signals signals.parquet
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

import duckdb
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import dataclasses  # noqa: E402

from services.simplified_stock_engine_core import (  # noqa: E402
    DIRECTION_BUY,
    Candle,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
    compute_zerodha_intraday_charges,
)

DB_PATH = REPO_ROOT / "outputs" / "tod_volume_gate" / "prices.duckdb"
OUT_DIR = REPO_ROOT / "backtest" / "inhouse_scanner"


def load_config() -> SimplifiedEngineConfig:
    try:
        from services.simplified_stock_engine_service import config_from_env

        cfg = config_from_env()
        cfg = dataclasses.replace(cfg, mode="disabled")
        print(
            f"  config(from env): capital={cfg.account_capital:,.0f} lev={cfg.account_leverage}x "
            f"max_risk={cfg.max_risk_per_trade} atr_sl={cfg.atr_sl_mult} "
            f"max_trades={cfg.max_trades_per_day} cooldown={cfg.cooldown_candles} "
            f"vol_mult={cfg.volume_multiplier}",
            flush=True,
        )
        return cfg
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] config_from_env failed ({e!r}); using dataclass defaults", flush=True)
        return SimplifiedEngineConfig(mode="disabled")


def fetch_5m(
    con: duckdb.DuckDBPyConnection, sym: str, day: dt.date, warmup_days: int
) -> list[Candle]:
    """Continuous 5m candles for ``sym`` from (day - warmup_days) .. day inclusive."""
    start = day - dt.timedelta(days=warmup_days)
    m1 = con.execute(
        "SELECT ts, open, high, low, close, volume FROM prices_1m "
        "WHERE symbol = ? AND CAST(ts AS DATE) >= ? AND CAST(ts AS DATE) <= ? ORDER BY ts",
        [sym, start.isoformat(), day.isoformat()],
    ).df()
    if m1.empty:
        return []
    m1 = m1[(m1["ts"].dt.time >= dt.time(9, 15)) & (m1["ts"].dt.time <= dt.time(15, 29))]
    if m1.empty:
        return []
    b5start = m1["ts"].dt.floor("5min")
    g = (
        m1.groupby(b5start)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
    )
    out = []
    for _, r in g.iterrows():
        out.append(
            Candle(
                ts=pd.Timestamp(r["ts"]).to_pydatetime(),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=int(r["volume"] or 0),
                elapsed_pct=1.0,
            )
        )
    return out


def run_day(con, day: dt.date, sigs: pd.DataFrame, config: SimplifiedEngineConfig) -> list[dict]:
    """Replay a single day; return completed-trade dicts."""
    sim = {"t": dt.datetime.combine(day, dt.time(9, 15))}
    engine = SimplifiedStockEngine(config=config, now_provider=lambda: sim["t"])

    # Per fired symbol: today candles, warmup candles, fire time, direction
    arm_events: list[tuple[dt.datetime, str, str]] = []  # (fire_ts, symbol, direction)
    candle_events: list[tuple[dt.datetime, str, Candle]] = []
    per_symbol_hist: dict[str, list[Candle]] = {}

    for _, s in sigs.iterrows():
        sym = s["symbol"]
        direction = s["direction"]
        fire_ts = pd.Timestamp(s["fire_ts"]).to_pydatetime()
        allc = fetch_5m(con, sym, day, warmup_days=7)
        if not allc:
            continue
        today = [c for c in allc if c.ts.date() == day]
        # history to seed reference/ATR at arm time = everything with close <= fire_ts
        # (5m close = bar start + 5min)
        hist = [c for c in allc if (c.ts + dt.timedelta(minutes=5)) <= fire_ts]
        per_symbol_hist[sym] = hist
        arm_events.append((fire_ts, sym, direction))
        for c in today:
            if (c.ts + dt.timedelta(minutes=5)) > fire_ts:
                candle_events.append((c.ts + dt.timedelta(minutes=5), sym, c))

    if not arm_events:
        return []

    # Merge arm + candle events on a single timeline. Arm before candle at equal ts.
    events: list[tuple[dt.datetime, int, str, Candle | None]] = []
    for fire_ts, sym, _direction in arm_events:
        events.append((fire_ts, 0, sym, None))  # 0 = arm
    for close_ts, sym, c in candle_events:
        events.append((close_ts, 1, sym, c))  # 1 = candle
    events.sort(key=lambda e: (e[0], e[1]))

    armed: set[str] = set()
    directions = {sym: d for (_, sym, d) in arm_events}

    for ts, kind, sym, candle in events:
        sim["t"] = ts
        if kind == 0:
            direction = directions[sym]
            if direction == DIRECTION_BUY:
                engine.activate_buy_symbol(sym)
            else:
                engine.activate_sell_symbol(sym)
            hist = per_symbol_hist.get(sym, [])
            if hist:
                engine.load_historical_candles(sym, hist)
            armed.add(sym)
            continue

        if sym not in armed:
            continue
        # Advance clock to ~candle end so elapsed_pct entry gate opens
        sim["t"] = candle.ts + dt.timedelta(seconds=config.candle_seconds - 1)
        entry = engine.on_new_candle(sym, candle)
        if entry:
            engine.confirm_entry(sym, entry.reference_price)
        for ex in engine.on_price_update(sym, candle.close):
            engine.confirm_exit(ex.symbol, ex.reference_price, ex.reason)
        # intra-candle SL check (mirrors BacktestRunner)
        if sym in engine.positions:
            pos = engine.positions[sym]
            if pos.qty > 0 and candle.low <= pos.stop_loss:
                for ex in engine.on_price_update(sym, pos.stop_loss):
                    engine.confirm_exit(ex.symbol, pos.stop_loss, "stop_loss_intracandle")
            elif pos.qty < 0 and candle.high >= pos.stop_loss:
                for ex in engine.on_price_update(sym, pos.stop_loss):
                    engine.confirm_exit(ex.symbol, pos.stop_loss, "stop_loss_intracandle")

    # EOD flatten
    sim["t"] = dt.datetime.combine(day, config.eod_exit_time)
    for ex in engine.check_eod_exits():
        engine.confirm_exit(ex.symbol, ex.reference_price, "eod")

    trades = []
    for t in engine.completed_trades:
        charges = compute_zerodha_intraday_charges(t.buy_value, t.sell_value)
        trades.append(
            {
                "day": day.isoformat(),
                "symbol": t.symbol,
                "direction": "LONG" if t.is_long else "SHORT",
                "qty": t.abs_qty,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "entry_time": t.entry_time.strftime("%H:%M:%S"),
                "exit_time": t.exit_time.strftime("%H:%M:%S"),
                "exit_reason": t.exit_reason,
                "gross_pnl": round(t.gross_pnl, 2),
                "charges": round(charges.total, 2),
                "net_pnl": round(t.gross_pnl - charges.total, 2),
            }
        )
    return trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default="signals.parquet")
    ap.add_argument("--suffix", default="")
    args = ap.parse_args()

    sig_path = (
        OUT_DIR / args.signals if not Path(args.signals).is_absolute() else Path(args.signals)
    )
    signals = pd.read_parquet(sig_path)
    print(f"signals: {len(signals)} rows from {sig_path}", flush=True)
    if signals.empty:
        print("no signals; nothing to run")
        return 0

    config = load_config()
    con = duckdb.connect(str(DB_PATH), read_only=True)

    by_day = defaultdict(list)
    for _, r in signals.iterrows():
        by_day[r["day"]].append(r)

    all_trades: list[dict] = []
    days = sorted(by_day.keys())
    for i, day_str in enumerate(days, 1):
        day = dt.date.fromisoformat(day_str)
        day_sigs = pd.DataFrame(by_day[day_str])
        all_trades.extend(run_day(con, day, day_sigs, config))
        if i % 20 == 0 or i == len(days):
            print(f"  {i}/{len(days)} days  trades={len(all_trades)}", flush=True)
    con.close()

    tdf = pd.DataFrame(all_trades)
    out = OUT_DIR / f"trades{args.suffix}.parquet"
    tdf.to_parquet(out, index=False)

    summarize(tdf, signals, config, args.suffix)
    return 0


def summarize(
    tdf: pd.DataFrame, signals: pd.DataFrame, config: SimplifiedEngineConfig, suffix: str
) -> None:
    print("\n" + "=" * 64)
    print("  IN-HOUSE SCANNER → SIMPLIFIED ENGINE  — BACKTEST SUMMARY")
    print("=" * 64)
    print(
        f"  signals (armed):     {len(signals)}  "
        f"(BUY={int((signals['direction'] == 'BUY').sum())}, "
        f"SELL={int((signals['direction'] == 'SELL').sum())})"
    )
    if tdf.empty:
        print("  trades:              0 (no engine breakout confirmed any armed signal)")
        print("=" * 64)
        return
    n = len(tdf)
    gross = tdf["gross_pnl"].sum()
    charges = tdf["charges"].sum()
    net = tdf["net_pnl"].sum()
    winners = int((tdf["net_pnl"] >= 0).sum())
    print(
        f"  trades:              {n}  (LONG={int((tdf['direction'] == 'LONG').sum())}, "
        f"SHORT={int((tdf['direction'] == 'SHORT').sum())})"
    )
    print(f"  win rate (net):      {winners / n * 100:.1f}%  ({winners}/{n})")
    print(f"  gross P&L:           ₹{gross:,.0f}")
    print(f"  charges:             ₹{charges:,.0f}")
    print(f"  net P&L:             ₹{net:,.0f}")
    print(f"  net / gross:         {net / gross * 100:.0f}%" if gross else "  net/gross: n/a")
    print(f"  avg net / trade:     ₹{net / n:,.0f}")
    print(
        f"  ROI on capital:      {net / config.account_capital * 100:.1f}%  "
        f"(capital ₹{config.account_capital:,.0f})"
    )

    tdf["ym"] = tdf["day"].str.slice(0, 7)
    monthly = tdf.groupby("ym")["net_pnl"].agg(["sum", "count"])
    green_months = int((monthly["sum"] > 0).sum())
    print(
        f"\n  months: {len(monthly)}  green: {green_months}  "
        f"({green_months / len(monthly) * 100:.0f}%)"
    )
    print("  month     net_pnl     trades")
    for ym, row in monthly.iterrows():
        print(f"  {ym}   ₹{row['sum']:>10,.0f}   {int(row['count']):>4}")

    print("\n  by exit_reason:")
    for reason, row in tdf.groupby("exit_reason")["net_pnl"].agg(["sum", "count"]).iterrows():
        print(f"    {reason:<24} ₹{row['sum']:>10,.0f}  ({int(row['count'])})")
    print("=" * 64)

    rep = {
        "signals": int(len(signals)),
        "signals_buy": int((signals["direction"] == "BUY").sum()),
        "signals_sell": int((signals["direction"] == "SELL").sum()),
        "trades": n,
        "win_rate_pct": round(winners / n * 100, 1),
        "gross_pnl": round(gross, 2),
        "charges": round(charges, 2),
        "net_pnl": round(net, 2),
        "roi_pct": round(net / config.account_capital * 100, 2),
        "green_months": green_months,
        "total_months": len(monthly),
        "monthly": {ym: round(v, 2) for ym, v in monthly["sum"].items()},
        "capital": config.account_capital,
    }
    (OUT_DIR / f"summary{suffix}.json").write_text(json.dumps(rep, indent=2))
    print(f"  report -> {OUT_DIR / f'summary{suffix}.json'}")


if __name__ == "__main__":
    raise SystemExit(main())
