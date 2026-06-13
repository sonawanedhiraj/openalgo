"""MVP backtester service.

This module owns three concerns:

* DB helpers over ``backtest_runs`` / ``backtest_trades`` — every helper is
  fail-safe (logs on error, returns a sentinel rather than raising).
* Summary metrics (``finalize_run``) — total trades, winners/losers, gross
  P&L, win rate, peak-to-trough drawdown on the cumulative P&L curve.
* The replay loop and simulated execution (``run_backtest``) — added in a
  later commit.

The simulator runs in PARALLEL to the live engine and writes to
``backtest_*`` tables only. It must never touch ``trade_journal``,
``daily_intent``, or any live state.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict, dataclass
from typing import Any

import pytz

from database.backtest_db import (
    BacktestRun,
    BacktestTrade,
    _now_iso,
    _run_to_dict,
    _trade_to_dict,
)
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")


@dataclass
class SlippageModel:
    """Configurable model for realistic fill prices.

    Defaults are sensible for NSE intraday equity. Operator can override
    per-run by constructing a SlippageModel and passing it to ``run_backtest``.

    All offsets are applied around the *mid* price the simulator passes in
    (e.g. the next-bar open for an entry, the SL level for an SL exit, or
    the EOD bar's close for a force-exit). For entries, LONG fills above
    mid (you pay ask + slippage) and SHORT fills below mid (you sell bid -
    slippage). Exits invert: LONG exit fills below mid, SHORT exit above.
    The final price is rounded to the nearest tick.
    """

    tick_size: float = 0.05  # NSE equity standard
    slippage_bps: float = 2.0  # adverse slippage in basis points (0.02% default)
    half_spread_bps: float = 1.5  # half of typical bid-ask spread in bps

    def round_to_tick(self, price: float) -> float:
        """Round ``price`` to the nearest multiple of ``tick_size``."""
        if self.tick_size <= 0:
            return float(price)
        ticks = round(float(price) / self.tick_size)
        return round(ticks * self.tick_size, 4)

    def _offset_price(self, price: float) -> float:
        """Combined half-spread + slippage as an absolute price delta."""
        return float(price) * (self.half_spread_bps + self.slippage_bps) / 10_000.0

    def entry_fill(self, mid_price: float, direction: str) -> float:
        """Realistic entry fill price.

        LONG entry fills above mid (you pay the ask + slippage).
        SHORT entry fills below mid (you sell at bid - slippage).
        """
        offset = self._offset_price(mid_price)
        if direction == "LONG":
            return self.round_to_tick(float(mid_price) + offset)
        return self.round_to_tick(float(mid_price) - offset)

    def exit_fill(self, mid_price: float, direction: str) -> float:
        """Realistic exit fill price.

        LONG exit fills below mid (you sell at bid - slippage).
        SHORT exit fills above mid (you cover at ask + slippage).
        """
        offset = self._offset_price(mid_price)
        if direction == "LONG":
            return self.round_to_tick(float(mid_price) - offset)
        return self.round_to_tick(float(mid_price) + offset)


def _session():
    """Resolve the live session from the DB module on each call.

    Tests monkeypatch the module-level ``db_session``; binding at import time
    would freeze the original session and skip the patch.
    """
    from database import backtest_db as bdb

    return bdb.db_session


def init_backtest_db() -> None:
    """Idempotent table creation. Thin wrapper around the DB module's init."""
    from database import backtest_db as bdb

    bdb.init_db()


# ---------------------------------------------------------------------------
# Write path — fail-safe helpers.
# ---------------------------------------------------------------------------


def create_run(
    strategy_name: str,
    rule_names: list[str],
    symbols: list[str],
    from_date: str,
    to_date: str,
    interval: str,
    config: dict[str, Any],
) -> int:
    """Insert a fresh ``backtest_runs`` row with ``status='running'``.

    Returns the new row id, or ``0`` on DB failure.
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
        )
        sess.add(row)
        sess.commit()
        return int(row.id)
    except Exception as e:
        logger.warning("backtest_service.create_run failed: %s", e)
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


def update_run_status(
    run_id: int,
    status: str,
    error_message: str | None = None,
) -> None:
    """Set ``status`` (and optional ``error_message``) on a run row. Silent no-op on failure."""
    if not run_id or run_id <= 0:
        return
    sess = _session()
    try:
        row = sess.query(BacktestRun).filter_by(id=run_id).first()
        if row is None:
            logger.warning("backtest_service.update_run_status: run_id=%s not found", run_id)
            return
        row.status = status
        if error_message is not None:
            row.error_message = error_message
        if status in ("completed", "error"):
            row.completed_at = _now_iso()
        sess.commit()
    except Exception as e:
        logger.warning("backtest_service.update_run_status failed (id=%s): %s", run_id, e)
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def record_trade(
    run_id: int,
    symbol: str,
    direction: str,
    entry_at: str,
    entry_price: float,
    entry_reason: str,
    quantity: int,
    atr_at_entry: float | None,
    sl_price: float | None,
    target_price: float | None = None,
) -> int:
    """Insert a ``backtest_trades`` row representing an open position.

    ``target_price`` is optional — trailing-stop strategies leave it None.
    Returns the new row id, or ``0`` on DB failure.
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
        )
        sess.add(row)
        sess.commit()
        return int(row.id)
    except Exception as e:
        logger.warning("backtest_service.record_trade failed: %s", e)
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


def close_trade(
    trade_id: int,
    exit_at: str,
    exit_price: float,
    exit_reason: str,
    pnl: float,
    pnl_pct: float,
    hold_duration_seconds: int,
) -> None:
    """Finalise a trade row with exit details + outcome. Silent no-op on failure."""
    if not trade_id or trade_id <= 0:
        return
    sess = _session()
    try:
        row = sess.query(BacktestTrade).filter_by(id=trade_id).first()
        if row is None:
            logger.warning("backtest_service.close_trade: trade_id=%s not found", trade_id)
            return
        row.exit_at = exit_at
        row.exit_price = float(exit_price)
        row.exit_reason = exit_reason
        row.pnl = float(pnl)
        row.pnl_pct = float(pnl_pct)
        row.hold_duration_seconds = int(hold_duration_seconds)
        sess.commit()
    except Exception as e:
        logger.warning("backtest_service.close_trade failed (id=%s): %s", trade_id, e)
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def finalize_run(run_id: int) -> dict[str, Any]:
    """Compute summary metrics from ``backtest_trades`` and stamp the run row.

    Returns ``{total_trades, winners, losers, gross_pnl, win_rate, max_drawdown}``.
    Trades that never closed (``pnl IS NULL``) are excluded from the metrics
    but still counted in ``total_trades``. Status is bumped to ``completed``.
    Returns the empty-shape dict on DB failure (and does not raise).
    """
    empty = {
        "total_trades": 0,
        "winners": 0,
        "losers": 0,
        "gross_pnl": 0.0,
        "win_rate": 0.0,
        "max_drawdown": 0.0,
    }
    if not run_id or run_id <= 0:
        return empty

    sess = _session()
    try:
        trades = (
            sess.query(BacktestTrade)
            .filter_by(run_id=int(run_id))
            .order_by(BacktestTrade.id.asc())
            .all()
        )

        total = len(trades)
        winners = sum(1 for t in trades if (t.pnl or 0.0) > 0)
        losers = sum(1 for t in trades if (t.pnl or 0.0) < 0)
        gross = sum(float(t.pnl or 0.0) for t in trades)

        # Win rate over CLOSED trades; an open trade with NULL pnl shouldn't
        # be counted as a loser. Falls back to 0.0 when nothing has closed.
        closed = winners + losers
        win_rate = (winners / closed) if closed > 0 else 0.0

        # Max peak-to-trough drawdown on the cumulative P&L curve.
        # Drawdown is the magnitude of the worst peak-trough fall; reported
        # as a positive number (0.0 when the curve never retreats).
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            running += float(t.pnl or 0.0)
            if running > peak:
                peak = running
            dd = peak - running
            if dd > max_dd:
                max_dd = dd

        row = sess.query(BacktestRun).filter_by(id=int(run_id)).first()
        if row is not None:
            row.total_trades = total
            row.winners = winners
            row.losers = losers
            row.gross_pnl = round(gross, 4)
            row.win_rate = round(win_rate, 6)
            row.max_drawdown = round(max_dd, 4)
            row.status = "completed"
            row.completed_at = _now_iso()
            sess.commit()

        return {
            "total_trades": total,
            "winners": winners,
            "losers": losers,
            "gross_pnl": round(gross, 4),
            "win_rate": round(win_rate, 6),
            "max_drawdown": round(max_dd, 4),
        }
    except Exception as e:
        logger.warning("backtest_service.finalize_run failed (id=%s): %s", run_id, e)
        try:
            sess.rollback()
        except Exception:
            pass
        return empty
    finally:
        try:
            sess.remove()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Read path — fail-safe to empty containers.
# ---------------------------------------------------------------------------


def get_run(run_id: int) -> dict[str, Any]:
    """Return the run row as a dict, or ``{}`` if not found / on DB failure."""
    if not run_id or run_id <= 0:
        return {}
    sess = _session()
    try:
        row = sess.query(BacktestRun).filter_by(id=int(run_id)).first()
        return _run_to_dict(row) if row else {}
    except Exception as e:
        logger.warning("backtest_service.get_run failed (id=%s): %s", run_id, e)
        return {}
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def get_run_trades(run_id: int) -> list[dict[str, Any]]:
    """Return all trades for a run, ordered by id. ``[]`` on failure."""
    if not run_id or run_id <= 0:
        return []
    sess = _session()
    try:
        rows = (
            sess.query(BacktestTrade)
            .filter_by(run_id=int(run_id))
            .order_by(BacktestTrade.id.asc())
            .all()
        )
        return [_trade_to_dict(r) for r in rows]
    except Exception as e:
        logger.warning("backtest_service.get_run_trades failed (id=%s): %s", run_id, e)
        return []
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def get_recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Return recent runs ordered by ``started_at DESC``. ``[]`` on failure."""
    sess = _session()
    try:
        rows = (
            sess.query(BacktestRun)
            .order_by(BacktestRun.started_at.desc())
            .limit(max(int(limit), 1))
            .all()
        )
        return [_run_to_dict(r) for r in rows]
    except Exception as e:
        logger.warning("backtest_service.get_recent_runs failed: %s", e)
        return []
    finally:
        try:
            sess.remove()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Replay loop + simulated execution
# ---------------------------------------------------------------------------
#
# The simulator walks per-symbol bars in chronological order. For each closed
# 5-min bar it:
#
#   1. Updates a rolling pandas DataFrame of the last N bars (history window).
#   2. If a position is open:
#       a. Checks if the bar's high/low crosses SL or target — exits at the
#          touched level, no slippage. Direction-aware (LONG: low<=SL or
#          high>=target; SHORT: high>=SL or low<=target).
#       b. If still open, checks EOD time — force-exits at bar close.
#   3. If no position is open AND no same-day stop-out block is active:
#       a. Builds indicators from the bar history.
#       b. Evaluates every enabled rule via the scanner registry.
#       c. On first match: arms an entry for the NEXT bar's open. The next
#          bar's open is the first realistic price an order placed at the
#          closing tick can fill at — entering on the closing bar would peek
#          into the bar's own data.
#
# A stop_loss exit installs a same-day cooldown for that symbol: any rule
# match on the same calendar day after a stop-out is ignored. Mirrors the
# live engine's RISK_SAME_DAY_STOPOUT_BLOCK behaviour without dragging in
# the full SimplifiedStockEngine state machine.

# A modest history window is enough: the active rules (fno_intraday_buy_chartink
# / sell_chartink) need at most 21 bars for the 20-bar volume average. ATR(14)
# needs 14. 60 bars gives a comfortable margin without blowing memory on
# long replays.
_HISTORY_WINDOW = 60


def _exchange_for_symbol(symbol: str, default: str = "NSE") -> str:
    """Look up the canonical exchange for ``symbol`` from the symbol table.

    Falls back to ``default`` when the lookup fails — the symbol either
    isn't in the master contract yet or the lookup raised. Backtests
    typically run against NSE equity, so the default is intentional.
    """
    try:
        from database.token_db import get_symbol_info  # noqa: PLC0415

        info = get_symbol_info(symbol)
        if info is not None:
            exch = getattr(info, "exchange", None) or (
                info.get("exchange") if isinstance(info, dict) else None
            )
            if exch:
                return str(exch)
    except Exception:
        pass
    return default


def _parse_bar_ts(raw: Any) -> _dt.datetime | None:
    """Coerce a Historify timestamp (epoch int or datetime) to a naive datetime."""
    if raw is None:
        return None
    if isinstance(raw, _dt.datetime):
        return raw.replace(tzinfo=None)
    try:
        # Historify stores epoch seconds; tolerate epoch ms too.
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 10_000_000_000:
        value = value / 1000.0
    return _dt.datetime.fromtimestamp(value)


def _aggregate_to_interval(
    bars_1m: list[dict[str, Any]],
    symbol: str,
    interval: str,
) -> list[dict[str, Any]]:
    """Re-aggregate 1-minute bars into ``interval`` bars via ``BarBuilder``.

    Each 1m bar is fed to the builder as a synthetic tick at the bar's close
    (price=close, volume=running cumulative volume). The builder's natural
    bucket-rollover semantics emit a closed bar when the next bucket starts.
    A final ``close_current_bar`` call flushes the trailing bucket.
    """
    from services.bar_aggregator import BarBuilder  # noqa: PLC0415

    closed: list[dict[str, Any]] = []
    builder = BarBuilder(
        symbol,
        interval,
        on_bar=lambda bar: (closed.append(bar) if bar.get("elapsed_pct", 0.0) >= 1.0 else None),
    )

    cum_vol = 0
    for raw in bars_1m:
        ts = _parse_bar_ts(raw.get("timestamp"))
        if ts is None:
            continue
        cum_vol += int(raw.get("volume") or 0)
        builder.on_tick(
            {
                "ts": ts,
                "price": float(raw.get("close") or 0.0),
                "cumulative_volume": cum_vol,
            }
        )

    final_bar = builder.close_current_bar(forced=True)
    if final_bar is not None and final_bar.get("elapsed_pct", 0.0) >= 1.0:
        closed.append(final_bar)
    return closed


def _aggregate_to_interval_ohlc(
    bars_1m: list[dict[str, Any]],
    interval: str,
) -> list[dict[str, Any]]:
    """Aggregate 1m bars to ``interval`` preserving true OHLC + true volume.

    The BarBuilder is tick-driven and only sees a single close-tick per 1m
    bar, so it doesn't observe the 1m bar's actual high/low. The simulator
    needs true intrabar high/low to evaluate SL/target hits. This helper
    bucket-aggregates 1m bars into ``interval`` buckets and aggregates
    open=first, high=max, low=min, close=last, volume=sum.
    """
    from services.bar_aggregator import bucket_for_interval, interval_to_seconds  # noqa: PLC0415

    interval_seconds = interval_to_seconds(interval)

    buckets: dict[_dt.datetime, dict[str, Any]] = {}
    order: list[_dt.datetime] = []

    for raw in bars_1m:
        ts = _parse_bar_ts(raw.get("timestamp"))
        if ts is None:
            continue
        bkey = bucket_for_interval(ts, interval_seconds)
        cell = buckets.get(bkey)
        o = float(raw.get("open") or 0.0)
        h = float(raw.get("high") or 0.0)
        l = float(raw.get("low") or 0.0)
        c = float(raw.get("close") or 0.0)
        v = int(raw.get("volume") or 0)
        if cell is None:
            buckets[bkey] = {
                "ts": bkey,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
            }
            order.append(bkey)
        else:
            cell["high"] = max(cell["high"], h)
            cell["low"] = min(cell["low"], l)
            cell["close"] = c
            cell["volume"] += v

    return [buckets[k] for k in order]


_VALID_DATA_SOURCES = ("api", "db", "auto")


def _fetch_bars(
    symbol: str,
    exchange: str,
    from_date: str,
    to_date: str,
    source: str = "api",
    cache: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Fetch 1m bars via ``services.history_service.get_history``.

    ``source`` controls the data origin:

    * ``"api"`` — broker history API (live, requires an authenticated
      broker session and is rate-limited at ~3 req/sec).
    * ``"db"`` — DuckDB / Historify cache (fast, may be stale).
    * ``"auto"`` — try DB first; fall back to the API on empty / failure.

    ``cache`` is an optional dict used to memoise calls within a single
    ``run_backtest`` invocation, keyed by
    ``(symbol, exchange, "1m", from_date, to_date)``. Repeated calls for
    the same window (e.g. a duplicated symbol) hit the cache instead of
    spamming the broker. Cleared by the caller at the end of the run.

    Returns the raw record list, or ``[]`` when no data is available.
    Raises only if get_history itself raises — caller catches and converts
    to a per-symbol skip.
    """
    from services.history_service import get_history  # noqa: PLC0415

    cache_key = (symbol, exchange, "1m", from_date, to_date)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    def _fetch(src: str) -> list[dict[str, Any]]:
        success, payload, _status = get_history(
            symbol=symbol,
            exchange=exchange,
            interval="1m",
            start_date=from_date,
            end_date=to_date,
            source=src,
        )
        if not success:
            return []
        return list(payload.get("data") or [])

    if source == "auto":
        rows = _fetch("db")
        if not rows:
            rows = _fetch("api")
    else:
        rows = _fetch(source)

    if cache is not None:
        cache[cache_key] = rows
    return rows


def _build_indicators_dict(bars_df):
    """Mirror ``ScannerService._build_indicators`` — same NaN-safety envelope."""
    from services import indicators as _ind  # noqa: PLC0415

    try:
        ema_20 = _ind.ema(bars_df["close"], period=20)
    except Exception:
        ema_20 = None
    try:
        atr_14 = _ind.atr(bars_df, period=14) if len(bars_df) >= 2 else None
    except Exception:
        atr_14 = None
    try:
        rsi_14 = _ind.rsi(bars_df["close"], period=14)
    except Exception:
        rsi_14 = None
    try:
        vol_avg_20 = _ind.volume_average(bars_df["volume"], period=20)
    except Exception:
        vol_avg_20 = None
    return {
        "ema_20": ema_20,
        "atr_14": atr_14,
        "rsi_14": rsi_14,
        "volume_avg_20": vol_avg_20,
    }


def _enabled_rules(rule_names: list[str] | None) -> list[tuple[str, Any, str]]:
    """Resolve rule names → (name, callable, screener_type) triples.

    When ``rule_names`` is None / empty, returns every registered rule
    (mirroring ScannerService.all_rules). Unknown names are silently
    skipped — the rule registry may not have a callable for every DB
    definition (e.g. legacy Chartink-only rows).
    """
    # Import the rule package so @scan_rule decorators self-register.
    import services.scan_rules  # noqa: F401, PLC0415
    from services import scanner_service  # noqa: PLC0415

    all_rules = scanner_service.all_rules()
    if not rule_names:
        return [
            (name, meta["fn"], meta.get("screener_type", "buy")) for name, meta in all_rules.items()
        ]

    out: list[tuple[str, Any, str]] = []
    for name in rule_names:
        meta = all_rules.get(name)
        if meta is None:
            logger.warning("backtest: skipping unknown rule %r", name)
            continue
        out.append((name, meta["fn"], meta.get("screener_type", "buy")))
    return out


def _parse_eod_time(eod_time_ist: str) -> _dt.time:
    h, m = eod_time_ist.split(":")
    return _dt.time(int(h), int(m))


def _is_scan_boundary(bar_ts: _dt.datetime, scan_cadence_minutes: int) -> bool:
    """Return True iff ``bar_ts`` aligns with the scan cadence.

    Scan boundaries are clock-time aligned. With cadence=15 the boundaries
    are minutes 0/15/30/45 of every hour. With cadence equal to the bar
    interval, every bar is a boundary (effectively disabling cadence).
    Bars are timestamped at the OPEN of the interval, which matches the
    moment the screener would fire on the previous bar's close.
    """
    if scan_cadence_minutes <= 0:
        return True
    return (bar_ts.hour * 60 + bar_ts.minute) % scan_cadence_minutes == 0


def _confirms_direction(bar_open: float, bar_close: float, direction: str) -> bool:
    """Default confirmation: the bar moved in the armed direction (or was flat).

    LONG confirms when close >= open (no fall on the candle); SHORT confirms
    when close <= open. The permissive ``>=`` / ``<=`` lets a flat candle
    confirm — useful for the existing tests' flat warm-up bars when they
    pass ``entry_confirmation_bars=0`` to skip confirmation entirely. The
    strict ``>``/``<`` semantics fall out naturally once a real synthetic
    UP/DOWN bar is provided.
    """
    if direction == "LONG":
        return bar_close >= bar_open
    return bar_close <= bar_open


def _replay_symbol(
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
    scan_cadence_minutes: int,
    entry_confirmation_bars: int,
    data_source: str,
    bar_cache: dict[tuple[str, str, str, str, str], list[dict[str, Any]]],
) -> int:
    """Walk a single symbol's bars end-to-end. Returns trade count."""
    import pandas as pd  # noqa: PLC0415

    from services import indicators as _ind  # noqa: PLC0415

    bars_1m = _fetch_bars(symbol, exchange, from_date, to_date, source=data_source, cache=bar_cache)
    if not bars_1m:
        logger.info("backtest: no 1m history for %s/%s, skipping", symbol, exchange)
        return 0

    bars = _aggregate_to_interval_ohlc(bars_1m, interval)
    if not bars:
        logger.info("backtest: no aggregated bars for %s/%s, skipping", symbol, exchange)
        return 0

    history_rows: list[dict[str, Any]] = []
    open_trade: dict[str, Any] | None = None  # in-memory mirror of the open trade row
    pending_entry: dict[str, Any] | None = None  # signal armed at previous bar's close
    sl_block_date: _dt.date | None = None  # same-day stop-out block date
    # Watchlist arming state. None when no symbol-level watch is active. When
    # populated by a scan-boundary match, ``armed_at_idx`` anchors the bar
    # whose close fired the screener; subsequent bars (idx > armed_at_idx)
    # are confirmation candidates until ``entry_confirmation_bars`` elapses
    # or the next scan boundary re-evaluates.
    armed: dict[str, Any] | None = None
    trades_recorded = 0

    for idx, bar in enumerate(bars):
        bar_ts = bar["ts"]
        bar_date = bar_ts.date()
        bar_open = float(bar["open"])
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])

        # New day → clear yesterday's same-day block.
        if sl_block_date is not None and bar_date > sl_block_date:
            sl_block_date = None

        # ----- 1. Service any pending entry from the previous bar's close.
        if open_trade is None and pending_entry is not None:
            direction = pending_entry["direction"]
            # Apply slippage / half-spread / tick rounding on top of the
            # next-bar open. LONG fills above the open, SHORT fills below.
            entry_price = slippage_model.entry_fill(bar_open, direction)
            atr = pending_entry["atr"]
            risk_per_share = max(atr * atr_sl_mult, 0.01) if atr else 0.01
            if direction == "LONG":
                sl_price = entry_price - risk_per_share
                target_price = entry_price + (rr_target * risk_per_share)
            else:
                sl_price = entry_price + risk_per_share
                target_price = entry_price - (rr_target * risk_per_share)

            trade_id = record_trade(
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
                }
                trades_recorded += 1
            pending_entry = None

        # ----- 2. Append this bar to the rolling history window.
        history_rows.append(
            {
                "ts": bar_ts,
                "open": bar_open,
                "high": bar_high,
                "low": bar_low,
                "close": bar_close,
                "volume": float(bar.get("volume", 0) or 0),
            }
        )
        if len(history_rows) > _HISTORY_WINDOW:
            history_rows = history_rows[-_HISTORY_WINDOW:]

        # ----- 3. Manage an open trade: check SL/target intra-bar, then EOD.
        if open_trade is not None:
            direction = open_trade["direction"]
            exit_price: float | None = None
            exit_reason: str | None = None

            if direction == "LONG":
                # Order matters when both levels are touched in a single bar:
                # without tick-level data we cannot prove which hit first.
                # Conservative choice: assume SL fills first (worst case for
                # the strategy). Matches how a real broker prices an
                # ambiguous intrabar fill.
                if bar_low <= open_trade["sl_price"]:
                    exit_price = slippage_model.exit_fill(open_trade["sl_price"], direction)
                    exit_reason = "stop_loss"
                elif bar_high >= open_trade["target_price"]:
                    exit_price = slippage_model.exit_fill(open_trade["target_price"], direction)
                    exit_reason = "target"
            else:  # SHORT
                if bar_high >= open_trade["sl_price"]:
                    exit_price = slippage_model.exit_fill(open_trade["sl_price"], direction)
                    exit_reason = "stop_loss"
                elif bar_low <= open_trade["target_price"]:
                    exit_price = slippage_model.exit_fill(open_trade["target_price"], direction)
                    exit_reason = "target"

            # EOD squareoff: force-close at bar close (we are by definition
            # past the cutoff time at this bar's close).
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
                close_trade(
                    trade_id=open_trade["id"],
                    exit_at=bar_ts.isoformat(),
                    exit_price=float(exit_price),
                    exit_reason=exit_reason,
                    pnl=float(pnl),
                    pnl_pct=float(pnl_pct),
                    hold_duration_seconds=hold,
                )
                if exit_reason == "stop_loss":
                    sl_block_date = bar_date
                open_trade = None

        # ----- 4. After EOD time, no new entries arm.
        if bar_ts.time() >= eod_time:
            pending_entry = None
            armed = None
            continue

        # ----- 5. Same-day stop-out cooldown blocks new entries.
        if open_trade is None and sl_block_date == bar_date:
            pending_entry = None
            armed = None
            continue

        # ----- 6. Confirmation check — if a prior scan armed this symbol,
        # bars after the arming bar are candidates for confirmation.
        # Default rule: the candle moved in the armed direction (LONG: close
        # >= open, SHORT: close <= open). Once the entry_confirmation_bars
        # window expires without a confirming candle, the watch is dropped.
        if armed is not None and open_trade is None and pending_entry is None:
            elapsed = idx - armed["armed_at_idx"]
            if elapsed >= 1:
                if _confirms_direction(bar_open, bar_close, armed["direction"]):
                    pending_entry = {
                        "direction": armed["direction"],
                        "rule_name": armed["rule_name"],
                        "atr": armed["atr"],
                    }
                    armed = None
                elif elapsed >= entry_confirmation_bars:
                    armed = None  # confirmation window expired

        # ----- 7. Scan boundary — re-evaluate every rule against the most
        # recent closed bar (including this one). Mirrors Chartink's 15-min
        # cadence; without this guard the screener would fire on every bar.
        # An open trade or queued entry suppresses re-arming so a symbol
        # already mid-trade does not get a second watch on the same bar.
        if (
            _is_scan_boundary(bar_ts, scan_cadence_minutes)
            and open_trade is None
            and pending_entry is None
            and len(history_rows) >= 21
        ):
            bars_df = pd.DataFrame(history_rows)
            indicators_dict = _build_indicators_dict(bars_df)
            matched_arm: dict[str, Any] | None = None
            for rule_name, rule_fn, screener_type in rules:
                try:
                    matched = bool(rule_fn(bars_df, indicators_dict))
                except Exception:
                    logger.exception(
                        "backtest: rule %r raised on %s @ %s",
                        rule_name,
                        symbol,
                        bar_ts.isoformat(),
                    )
                    continue
                if not matched:
                    continue
                # Compute ATR for sizing the stop. Use the value at this bar.
                try:
                    atr_series = _ind.atr(bars_df, period=atr_period)
                    atr_value = float(atr_series.iloc[-1]) if atr_series is not None else 0.0
                    if atr_value != atr_value:  # NaN check (NaN != NaN)
                        atr_value = 0.0
                except Exception:
                    atr_value = 0.0
                matched_arm = {
                    "direction": "LONG" if screener_type == "buy" else "SHORT",
                    "rule_name": rule_name,
                    "atr": atr_value,
                    "armed_at_idx": idx,
                }
                break  # first match wins

            if matched_arm is None:
                # Currently-armed symbol no longer matches at this scan — drop.
                armed = None
            elif entry_confirmation_bars == 0:
                # No confirmation required; queue entry for the next bar.
                # Mirrors the MVP's behaviour when the operator wants the
                # screener flag itself to be the entry signal.
                pending_entry = {
                    "direction": matched_arm["direction"],
                    "rule_name": matched_arm["rule_name"],
                    "atr": matched_arm["atr"],
                }
                armed = None
            else:
                armed = matched_arm

    return trades_recorded


def run_backtest(
    *,
    strategy_name: str = "trending_equity_intraday",
    rule_names: list[str] | None = None,
    symbols: list[str],
    from_date: str,
    to_date: str,
    interval: str = "5m",
    atr_period: int = 14,
    atr_sl_mult: float = 1.5,
    rr_target: float = 1.5,
    position_size: int = 500,
    eod_time_ist: str = "15:15",
    exchange: str | None = None,
    slippage_model: SlippageModel | None = None,
    scan_cadence_minutes: int = 15,
    entry_confirmation_bars: int = 1,
    data_source: str = "api",
) -> int:
    """Synchronous backtest. Returns the new run_id.

    For each symbol: fetches 1m bars (from the broker history API by
    default, see ``data_source``), re-aggregates to ``interval``, and
    walks the bars through a simulated entry/exit state machine that
    mirrors the live engine's SL / target / EOD / same-day stop-out
    semantics. Records every trade to ``backtest_trades`` and finalises
    summary metrics on completion.

    ``data_source`` (default ``"api"``):

    * ``"api"`` — fetch from the broker's history API. Requires an active
      broker session. The history call is rate-limited at ~3 req/sec, so
      runs covering many symbols × months can be slow. For those workloads
      pre-populate Historify and switch to ``"db"`` instead.
    * ``"db"`` — read from the Historify / DuckDB cache. Faster but may
      lag the broker on the current day's bars.
    * ``"auto"`` — try the DB first; fall back to the API on empty/failure.

    Per-symbol failures (no history, get_history raises) are logged and
    skipped; the run still completes with whatever symbols succeeded. If
    every symbol fails the run is still marked ``completed`` (with zero
    trades). The run is only marked ``error`` when the orchestrator
    itself crashes.
    """
    if data_source not in _VALID_DATA_SOURCES:
        raise ValueError(f"data_source must be one of {_VALID_DATA_SOURCES}, got {data_source!r}")
    rules = _enabled_rules(rule_names)
    effective_rule_names = [name for name, _, _ in rules]
    eod_time = _parse_eod_time(eod_time_ist)
    if slippage_model is None:
        slippage_model = SlippageModel()

    config = {
        "atr_period": atr_period,
        "atr_sl_mult": atr_sl_mult,
        "rr_target": rr_target,
        "position_size": position_size,
        "eod_time_ist": eod_time_ist,
        "exchange_default": exchange or "NSE",
        "slippage_model": asdict(slippage_model),
        "scan_cadence_minutes": scan_cadence_minutes,
        "entry_confirmation_bars": entry_confirmation_bars,
        "data_source": data_source,
    }

    # In-process bar cache, shared across the symbols loop below. Cleared
    # implicitly when this invocation returns. Keeps the broker history
    # API from being hit twice for the same (symbol, exchange, window).
    bar_cache: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}

    run_id = create_run(
        strategy_name=strategy_name,
        rule_names=effective_rule_names,
        symbols=symbols,
        from_date=from_date,
        to_date=to_date,
        interval=interval,
        config=config,
    )
    if run_id <= 0:
        logger.error("backtest: failed to create run row")
        return 0

    try:
        for symbol in symbols:
            sym_exchange = exchange or _exchange_for_symbol(symbol)
            try:
                _replay_symbol(
                    run_id=run_id,
                    symbol=symbol,
                    exchange=sym_exchange,
                    from_date=from_date,
                    to_date=to_date,
                    interval=interval,
                    atr_period=atr_period,
                    atr_sl_mult=atr_sl_mult,
                    rr_target=rr_target,
                    position_size=position_size,
                    eod_time=eod_time,
                    rules=rules,
                    slippage_model=slippage_model,
                    scan_cadence_minutes=scan_cadence_minutes,
                    entry_confirmation_bars=entry_confirmation_bars,
                    data_source=data_source,
                    bar_cache=bar_cache,
                )
            except Exception:
                # Per-symbol failure — log and continue with remaining symbols
                # rather than poisoning the whole run.
                logger.exception("backtest: replay failed for symbol %s", symbol)
                continue
    except Exception as e:
        logger.exception("backtest: orchestrator crashed for run %s", run_id)
        update_run_status(run_id, "error", error_message=str(e))
        return run_id

    finalize_run(run_id)
    return run_id
