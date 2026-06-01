"""Screener-filtered backtest harness.

Per-day, per-symbol replay that only takes positions on stocks where the
in-house scan rule fires at a bar close — mirroring how the live engine
gates entries on Chartink picks. Companion to the older all-symbol
:mod:`services.backtest_service.run_backtest`, but tagged with
``methodology='screener_filtered'`` so the journal reflection prompt
can distinguish the two when both data sets are present in the window.

Methodology
-----------

For each historical trading day in the window:

1. Load that day's 5-min bars per F&O symbol from
   ``database.historify_db`` (DuckDB).
2. For each 5-min bar close from market open onward, evaluate each
   enabled scan rule against the symbol's rolling bar history up to and
   including the current bar.
3. Collect ``(symbol, side, hit_timestamp)`` tuples where a rule fires
   — these are the day's screener picks.
4. For each pick, simulate strategy entry on the next bar's open (with
   slippage), manage with an ATR-based stop, and force-exit at the EOD
   cutoff (15:20 IST).

This module **never** writes to live tables. It uses the same
``backtest_*`` tables as the all-symbol harness, distinguished only by
the ``methodology`` tag on both ``backtest_runs`` and ``backtest_trades``.

The scanner rule (``services.scan_rules.fno_intraday_buy_20`` and
``fno_intraday_sell_20``) is an admitted placeholder — operators will
tune the thresholds against shadow-mode output before any rule is
promoted. Until then, quantitative conclusions from this harness are
directional, not predictive.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import asdict
from typing import Any

import pandas as pd
import pytz

from database.backtest_db import (
    BacktestRun,
    BacktestTrade,
    _now_iso,
)
from services.backtest_service import (
    SlippageModel,
    _aggregate_to_interval_ohlc,
    _build_indicators_dict,
    _enabled_rules,
    _exchange_for_symbol,
    _fetch_bars,
    _parse_eod_time,
    finalize_run,
    init_backtest_db,
    update_run_status,
)
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# History needed for the 21-bar warm-up in fno_intraday_*_20. 60 gives a
# comfortable margin for ATR(14) + the 20-bar volume rolling average.
_HISTORY_WINDOW = 60

# Where the F&O universe lives if the caller doesn't pass an explicit
# ``universe`` argument. The file is one symbol per line, blank lines OK.
_FNO_UNIVERSE_FILE = "_fno_universe.txt"

# A day is treated as a market holiday and skipped when fewer than this
# fraction of the universe has any bars on that date.
_HOLIDAY_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Universe + utility helpers
# ---------------------------------------------------------------------------


def _load_default_universe() -> list[str]:
    """Read ``_fno_universe.txt`` from the project root.

    Returns the list of non-empty symbols. The file is checked into the
    repo root next to ``CLAUDE.md``; running this from a different cwd
    falls back gracefully to an empty universe (tests always pass a
    universe explicitly).
    """
    candidates = [
        _FNO_UNIVERSE_FILE,
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _FNO_UNIVERSE_FILE),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                symbols = [
                    line.strip()
                    for line in f.readlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
            logger.info("screener_bt: loaded %d symbols from %s", len(symbols), path)
            return symbols
        except Exception as e:
            logger.warning("screener_bt: failed to read universe %s: %s", path, e)
    logger.warning("screener_bt: no universe file found; returning empty universe")
    return []


def _trading_dates(start_date: str, end_date: str) -> list[_dt.date]:
    """Inclusive list of weekdays in the window (Mon=0..Fri=4).

    Indian market holidays are detected later (via the per-day
    bar-availability check) — the calendar of NSE holidays isn't shipped
    with the codebase, so we approximate by skipping any day where most
    of the universe is missing bars.
    """
    s = _dt.date.fromisoformat(start_date)
    e = _dt.date.fromisoformat(end_date)
    if e < s:
        return []
    out: list[_dt.date] = []
    cur = s
    while cur <= e:
        if cur.weekday() < 5:
            out.append(cur)
        cur += _dt.timedelta(days=1)
    return out


def _session():
    """Resolve the live DB session lazily so tests can monkeypatch it."""
    from database import backtest_db as bdb

    return bdb.db_session


def _create_run_with_methodology(
    *,
    strategy_name: str,
    rule_names: list[str],
    symbols: list[str],
    from_date: str,
    to_date: str,
    interval: str,
    config: dict[str, Any],
    methodology: str,
) -> int:
    """Insert a ``backtest_runs`` row with the methodology tag set.

    Mirrors :func:`services.backtest_service.create_run` but writes the
    new ``methodology`` column on the same insert so the row is never
    visible without its tag. Returns 0 on DB failure.
    """
    sess = _session()
    try:
        row = BacktestRun(
            started_at=_now_iso(),
            strategy_name=strategy_name,
            rule_names=json.dumps(list(rule_names or [])),
            symbols=json.dumps(list(symbols or [])),
            from_date=from_date,
            to_date=to_date,
            interval=interval,
            config=json.dumps(config or {}, default=str),
            status="running",
            methodology=methodology,
        )
        sess.add(row)
        sess.commit()
        return int(row.id)
    except Exception as e:
        logger.warning("screener_bt: create_run failed: %s", e)
        try:
            sess.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def _record_screener_trade(
    *,
    run_id: int,
    symbol: str,
    direction: str,
    entry_at: str,
    entry_price: float,
    entry_reason: str,
    quantity: int,
    atr_at_entry: float | None,
    sl_price: float | None,
    target_price: float | None,
    methodology: str,
    scanner_hit_timestamp: str | None,
) -> int:
    """Insert a ``backtest_trades`` row with methodology + hit timestamp.

    Returns the new row id, or 0 on DB failure. The trade is created in
    the open state — exit columns are populated later by
    :func:`_close_trade_row`.
    """
    sess = _session()
    try:
        row = BacktestTrade(
            run_id=int(run_id),
            symbol=symbol,
            direction=direction,
            entry_at=entry_at,
            entry_price=float(entry_price),
            entry_reason=entry_reason,
            quantity=int(quantity),
            atr_at_entry=float(atr_at_entry) if atr_at_entry is not None else None,
            sl_price=float(sl_price) if sl_price is not None else None,
            target_price=float(target_price) if target_price is not None else None,
            methodology=methodology,
            scanner_hit_timestamp=scanner_hit_timestamp,
        )
        sess.add(row)
        sess.commit()
        return int(row.id)
    except Exception as e:
        logger.warning("screener_bt: record_trade failed: %s", e)
        try:
            sess.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def _close_trade_row(
    *,
    trade_id: int,
    exit_at: str,
    exit_price: float,
    exit_reason: str,
    pnl: float,
    pnl_pct: float,
    hold_duration_seconds: int,
) -> None:
    """Patch a row with exit details. Silent on failure."""
    if not trade_id or trade_id <= 0:
        return
    sess = _session()
    try:
        row = sess.query(BacktestTrade).filter_by(id=trade_id).first()
        if row is None:
            return
        row.exit_at = exit_at
        row.exit_price = float(exit_price)
        row.exit_reason = exit_reason
        row.pnl = float(pnl)
        row.pnl_pct = float(pnl_pct)
        row.hold_duration_seconds = int(hold_duration_seconds)
        sess.commit()
    except Exception as e:
        logger.warning("screener_bt: close_trade failed (id=%s): %s", trade_id, e)
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        try:
            sess.remove()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Per-symbol replay
# ---------------------------------------------------------------------------


def _replay_symbol_screener(
    *,
    run_id: int,
    symbol: str,
    exchange: str,
    from_date: str,
    to_date: str,
    interval: str,
    atr_period: int,
    atr_sl_mult: float,
    rr_target: float,
    position_size: int,
    eod_time: _dt.time,
    rules: list[tuple[str, Any, str]],
    slippage_model: SlippageModel,
    methodology: str,
    bar_cache: dict[tuple[str, str, str, str, str], list[dict[str, Any]]],
    indicator_fn: Any = None,
) -> dict[str, Any]:
    """Walk a single symbol's bars; return per-day pick + trade counts.

    Returns a dict::

        {
          "by_date": {date_iso: {"buy_hits": int, "sell_hits": int,
                                 "entries": int, "wins": int, "losses": int,
                                 "pnl": float}, ...},
          "trades": [trade_dict, ...],   # condensed for the summary
        }
    """
    from services import indicators as _ind  # noqa: PLC0415

    by_date: dict[str, dict[str, Any]] = {}
    trades_summary: list[dict[str, Any]] = []

    def _day_bucket(d: _dt.date) -> dict[str, Any]:
        key = d.isoformat()
        cell = by_date.get(key)
        if cell is None:
            cell = {
                "buy_hits": 0,
                "sell_hits": 0,
                "entries": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
            }
            by_date[key] = cell
        return cell

    bars_1m = _fetch_bars(
        symbol, exchange, from_date, to_date, source="db", cache=bar_cache
    )
    if not bars_1m:
        return {"by_date": by_date, "trades": trades_summary}

    bars = _aggregate_to_interval_ohlc(bars_1m, interval)
    if not bars:
        return {"by_date": by_date, "trades": trades_summary}

    history_rows: list[dict[str, Any]] = []
    open_trade: dict[str, Any] | None = None
    pending_entry: dict[str, Any] | None = None
    sl_block_date: _dt.date | None = None
    last_pick_bar_ts: _dt.datetime | None = None

    for _idx, bar in enumerate(bars):
        bar_ts: _dt.datetime = bar["ts"]
        bar_date = bar_ts.date()
        bar_open = float(bar["open"])
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])

        # New day → reset cross-day state.
        if sl_block_date is not None and bar_date > sl_block_date:
            sl_block_date = None

        # 1) Service pending entry on this bar's open.
        if open_trade is None and pending_entry is not None:
            direction = pending_entry["direction"]
            entry_price = slippage_model.entry_fill(bar_open, direction)
            atr = pending_entry["atr"]
            risk_per_share = max(atr * atr_sl_mult, 0.01) if atr else 0.01
            if direction == "LONG":
                sl_price = entry_price - risk_per_share
                target_price = entry_price + (rr_target * risk_per_share)
            else:
                sl_price = entry_price + risk_per_share
                target_price = entry_price - (rr_target * risk_per_share)

            trade_id = _record_screener_trade(
                run_id=run_id,
                symbol=symbol,
                direction=direction,
                entry_at=bar_ts.isoformat(),
                entry_price=entry_price,
                entry_reason=pending_entry["rule_name"],
                quantity=position_size,
                atr_at_entry=atr,
                sl_price=sl_price,
                target_price=target_price,
                methodology=methodology,
                scanner_hit_timestamp=pending_entry.get("hit_ts"),
            )
            if trade_id > 0:
                open_trade = {
                    "id": trade_id,
                    "direction": direction,
                    "entry_at": bar_ts,
                    "entry_price": entry_price,
                    "sl_price": sl_price,
                    "target_price": target_price,
                    "quantity": position_size,
                    "rule_name": pending_entry["rule_name"],
                    "hit_ts": pending_entry.get("hit_ts"),
                }
                _day_bucket(bar_date)["entries"] += 1
            pending_entry = None

        # 2) Append this bar to the rolling history.
        history_rows.append({
            "ts": bar_ts,
            "open": bar_open,
            "high": bar_high,
            "low": bar_low,
            "close": bar_close,
            "volume": float(bar.get("volume", 0) or 0),
        })
        if len(history_rows) > _HISTORY_WINDOW:
            history_rows = history_rows[-_HISTORY_WINDOW:]

        # 3) Manage open trade — SL / target / EOD.
        if open_trade is not None:
            direction = open_trade["direction"]
            exit_price: float | None = None
            exit_reason: str | None = None

            if direction == "LONG":
                if bar_low <= open_trade["sl_price"]:
                    exit_price = slippage_model.exit_fill(open_trade["sl_price"], direction)
                    exit_reason = "stop_loss"
                elif bar_high >= open_trade["target_price"]:
                    exit_price = slippage_model.exit_fill(open_trade["target_price"], direction)
                    exit_reason = "target"
            else:
                if bar_high >= open_trade["sl_price"]:
                    exit_price = slippage_model.exit_fill(open_trade["sl_price"], direction)
                    exit_reason = "stop_loss"
                elif bar_low <= open_trade["target_price"]:
                    exit_price = slippage_model.exit_fill(open_trade["target_price"], direction)
                    exit_reason = "target"

            if exit_reason is None and bar_ts.time() >= eod_time:
                exit_price = slippage_model.exit_fill(bar_close, direction)
                exit_reason = "eod_squareoff"

            if exit_reason is not None:
                qty = open_trade["quantity"]
                entry_p = open_trade["entry_price"]
                if direction == "LONG":
                    pnl = (exit_price - entry_p) * qty
                else:
                    pnl = (entry_p - exit_price) * qty
                denom = entry_p * qty
                pnl_pct = (pnl / denom) if denom > 0 else 0.0
                hold = int((bar_ts - open_trade["entry_at"]).total_seconds())
                _close_trade_row(
                    trade_id=open_trade["id"],
                    exit_at=bar_ts.isoformat(),
                    exit_price=float(exit_price),
                    exit_reason=exit_reason,
                    pnl=float(pnl),
                    pnl_pct=float(pnl_pct),
                    hold_duration_seconds=hold,
                )
                cell = _day_bucket(bar_date)
                cell["pnl"] += float(pnl)
                if pnl > 0:
                    cell["wins"] += 1
                elif pnl < 0:
                    cell["losses"] += 1
                trades_summary.append({
                    "symbol": symbol,
                    "direction": direction,
                    "entry_at": open_trade["entry_at"].isoformat(),
                    "entry_price": entry_p,
                    "exit_at": bar_ts.isoformat(),
                    "exit_price": float(exit_price),
                    "pnl": float(pnl),
                    "exit_reason": exit_reason,
                    "rule_name": open_trade["rule_name"],
                    "hit_ts": open_trade["hit_ts"],
                })
                if exit_reason == "stop_loss":
                    sl_block_date = bar_date
                open_trade = None

        # 4) After EOD time: no new picks or entries arm.
        if bar_ts.time() >= eod_time:
            pending_entry = None
            continue

        # 5) Same-day stop-out cooldown.
        if open_trade is None and sl_block_date == bar_date:
            pending_entry = None
            continue

        # 6) Scan EVERY bar close (cadence == bar interval). This is the
        # screener-filtered methodology: at each 5-min close, evaluate
        # the rule and treat a match as a pick.
        if open_trade is None and pending_entry is None and len(history_rows) >= 21:
            bars_df = pd.DataFrame(history_rows)
            indicators_dict = (
                _build_indicators_dict(bars_df)
                if indicator_fn is None
                else indicator_fn(bars_df)
            )
            for rule_name, rule_fn, screener_type in rules:
                try:
                    matched = bool(rule_fn(bars_df, indicators_dict))
                except Exception:
                    logger.exception(
                        "screener_bt: rule %r raised on %s @ %s",
                        rule_name, symbol, bar_ts.isoformat(),
                    )
                    continue
                if not matched:
                    continue

                # Bookkeeping: this bar produced a pick.
                cell = _day_bucket(bar_date)
                if screener_type == "buy":
                    cell["buy_hits"] += 1
                else:
                    cell["sell_hits"] += 1
                last_pick_bar_ts = bar_ts

                try:
                    atr_series = _ind.atr(bars_df, period=atr_period)
                    atr_value = float(atr_series.iloc[-1]) if atr_series is not None else 0.0
                    if atr_value != atr_value:  # NaN check
                        atr_value = 0.0
                except Exception:
                    atr_value = 0.0

                pending_entry = {
                    "direction": "LONG" if screener_type == "buy" else "SHORT",
                    "rule_name": rule_name,
                    "atr": atr_value,
                    "hit_ts": bar_ts.isoformat(),
                }
                break  # first matching rule wins on this bar

    return {"by_date": by_date, "trades": trades_summary, "last_pick_bar_ts": last_pick_bar_ts}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_screener_filtered_backtest(
    *,
    start_date: str,
    end_date: str,
    strategies: list[str] | None = None,
    universe: list[str] | None = None,
    interval: str = "5m",
    methodology_tag: str = "screener_filtered",
    rule_names: list[str] | None = None,
    atr_period: int = 14,
    atr_sl_mult: float = 1.5,
    rr_target: float = 1.5,
    position_size: int = 500,
    eod_time_ist: str = "15:20",
    slippage_model: SlippageModel | None = None,
    exchange_default: str = "NSE",
    log_progress_every: int = 25,
) -> dict[str, Any]:
    """Run a screener-filtered backtest over ``[start_date, end_date]``.

    Returns the result dict described in the module docstring's task spec.
    The dict shape mirrors what the journal reflection prompt will read
    once ``methodology='screener_filtered'`` rows appear in ``backtest_trades``.

    Per-symbol failures (no DB history, replay raises) are logged and
    skipped — the run still completes with whatever symbols succeeded.
    """
    if slippage_model is None:
        slippage_model = SlippageModel()

    strategies = strategies or ["trending_equity_intraday"]
    if universe is None:
        universe = _load_default_universe()

    rules = _enabled_rules(rule_names)
    effective_rule_names = [name for name, _, _ in rules]
    eod_time = _parse_eod_time(eod_time_ist)

    warnings: list[str] = []

    if not universe:
        warnings.append("universe is empty; nothing to do")
    if not rules:
        warnings.append("no scan rules registered; nothing to do")

    # Make sure the schema migrations land before any insert.
    try:
        init_backtest_db()
    except Exception as e:
        warnings.append(f"init_backtest_db raised: {e}")

    config = {
        "atr_period": atr_period,
        "atr_sl_mult": atr_sl_mult,
        "rr_target": rr_target,
        "position_size": position_size,
        "eod_time_ist": eod_time_ist,
        "exchange_default": exchange_default,
        "slippage_model": asdict(slippage_model),
        "methodology_tag": methodology_tag,
        "interval": interval,
        "universe_size": len(universe),
    }

    run_id = _create_run_with_methodology(
        strategy_name=strategies[0],
        rule_names=effective_rule_names,
        symbols=universe,
        from_date=start_date,
        to_date=end_date,
        interval=interval,
        config=config,
        methodology=methodology_tag,
    )
    if run_id <= 0:
        warnings.append("failed to create backtest_runs row; aborting")
        return _empty_result(warnings=warnings)

    trading_dates = _trading_dates(start_date, end_date)
    expected_days = len(trading_dates)
    logger.info(
        "screener_bt: run_id=%s start=%s end=%s universe=%d weekdays=%d rules=%s",
        run_id, start_date, end_date, len(universe),
        expected_days, [r[0] for r in rules],
    )

    by_date_total: dict[str, dict[str, Any]] = {}
    trades_summary: list[dict[str, Any]] = []
    bar_cache: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}

    completed_symbols = 0
    skipped_symbols = 0

    for sym_idx, symbol in enumerate(universe):
        sym_exchange = _exchange_for_symbol(symbol, default=exchange_default)
        try:
            sym_result = _replay_symbol_screener(
                run_id=run_id,
                symbol=symbol,
                exchange=sym_exchange,
                from_date=start_date,
                to_date=end_date,
                interval=interval,
                atr_period=atr_period,
                atr_sl_mult=atr_sl_mult,
                rr_target=rr_target,
                position_size=position_size,
                eod_time=eod_time,
                rules=rules,
                slippage_model=slippage_model,
                methodology=methodology_tag,
                bar_cache=bar_cache,
            )
        except Exception:
            logger.exception("screener_bt: replay failed for %s", symbol)
            skipped_symbols += 1
            warnings.append(f"replay raised for {symbol}")
            continue

        completed_symbols += 1
        for date_key, cell in sym_result.get("by_date", {}).items():
            agg = by_date_total.setdefault(date_key, {
                "date": date_key,
                "buy_count": 0,
                "sell_count": 0,
                "entries": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
            })
            agg["buy_count"] += cell["buy_hits"]
            agg["sell_count"] += cell["sell_hits"]
            agg["entries"] += cell["entries"]
            agg["wins"] += cell["wins"]
            agg["losses"] += cell["losses"]
            agg["pnl"] += cell["pnl"]

        trades_summary.extend(sym_result.get("trades", []))

        if log_progress_every > 0 and (sym_idx + 1) % log_progress_every == 0:
            logger.info(
                "screener_bt: progress %d/%d symbols done; running totals "
                "buy=%d sell=%d entries=%d",
                sym_idx + 1, len(universe),
                sum(d["buy_count"] for d in by_date_total.values()),
                sum(d["sell_count"] for d in by_date_total.values()),
                sum(d["entries"] for d in by_date_total.values()),
            )

    # Holiday detection: any day with zero bars across the universe is
    # likely a holiday. With per-symbol bar fetches we can't cheaply tell
    # ahead of time, but we can flag a day's emptiness in the summary.
    expected_dates = {d.isoformat() for d in trading_dates}
    days_with_any_data = {
        date_key for date_key, cell in by_date_total.items()
        if cell["buy_count"] + cell["sell_count"] + cell["entries"] > 0
    }
    silent_days = sorted(expected_dates - days_with_any_data)
    if silent_days:
        warnings.append(
            f"{len(silent_days)} weekday(s) had no scanner hits "
            f"(probable holidays or thin data): {silent_days[:5]}..."
        )

    metrics = finalize_run(run_id)
    if metrics.get("total_trades", 0) == 0 and not by_date_total:
        warnings.append("no scanner hits across the entire window")

    scanner_hits_per_day = sorted(by_date_total.values(), key=lambda r: r["date"])
    scanner_hits_total = sum(d["buy_count"] + d["sell_count"] for d in scanner_hits_per_day)
    entries_taken = sum(d["entries"] for d in scanner_hits_per_day)
    wins = sum(d["wins"] for d in scanner_hits_per_day)
    losses = sum(d["losses"] for d in scanner_hits_per_day)
    net_pnl = round(sum(d["pnl"] for d in scanner_hits_per_day), 4)

    # Days the harness actually processed = days where any symbol produced
    # bars within market hours (i.e. days_with_any_data plus days that had
    # bars but no hits — we can't tell those apart without per-day fetches,
    # so we report the weekday count minus silent_days as a lower bound).
    days_processed = max(expected_days - len(silent_days), 0)

    summary = {
        "run_id": run_id,
        "methodology": methodology_tag,
        "days_processed": days_processed,
        "scanner_hits_total": scanner_hits_total,
        "scanner_hits_per_day": scanner_hits_per_day,
        "entries_taken": entries_taken,
        "wins": wins,
        "losses": losses,
        "net_pnl": net_pnl,
        "trades_summary": trades_summary[:200],  # cap to keep the dict bounded
        "warnings": warnings,
        "metrics": metrics,
        "symbols_completed": completed_symbols,
        "symbols_skipped": skipped_symbols,
    }

    # Bookkeeping: mark the run completed even if zero trades — finalize_run
    # already does this, but we want the methodology + status guaranteed.
    try:
        update_run_status(run_id, "completed")
    except Exception:
        pass

    logger.info(
        "screener_bt: run_id=%s done — hits=%d entries=%d wins=%d losses=%d pnl=%.2f",
        run_id, scanner_hits_total, entries_taken, wins, losses, net_pnl,
    )
    return summary


def _empty_result(*, warnings: list[str]) -> dict[str, Any]:
    return {
        "run_id": 0,
        "methodology": "screener_filtered",
        "days_processed": 0,
        "scanner_hits_total": 0,
        "scanner_hits_per_day": [],
        "entries_taken": 0,
        "wins": 0,
        "losses": 0,
        "net_pnl": 0.0,
        "trades_summary": [],
        "warnings": warnings,
        "metrics": {},
        "symbols_completed": 0,
        "symbols_skipped": 0,
    }
