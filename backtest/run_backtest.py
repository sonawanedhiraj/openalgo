#!/usr/bin/env python3
"""
OpenAlgo Simplified Engine Backtester
=====================================
Replays historical 5-min candles (or tick data when available) through the
SimplifiedStockEngine to simulate what trades would have been triggered on a
given date.

**Config sourcing (no hardcoded values):**
The backtester reads engine parameters from one of three sources, in priority
order:

1. ``--from-engine``  – fetch live config from the running engine's
   ``/chartink/simplified-engine/api/status`` API endpoint.  This is the
   recommended default so backtests match what the live engine actually uses.
2. ``--from-env``     – read the same ``SIMPLIFIED_ENGINE_*`` env vars the
   production service reads (via ``config_from_env()``).
3. CLI overrides      – explicit ``--capital``, ``--atr-sl-mult``, etc.
   override whichever base config was loaded.

**Tick data replay:**
When ``--tick-data <dir>`` is provided and JSONL tick-log files exist for the
target date, the backtester replays individual ticks instead of aggregated
5-min candles.  This gives much more accurate stop-loss and trailing-stop
behaviour.  Tick log files are written by the live engine when
``SIMPLIFIED_ENGINE_TICK_LOG_ENABLED=true`` — see ``simplified_stock_engine_ticklog.py``.

Usage:
    cd C:\\workspace\\ai-trade-agent\\openalgo

    # Recommended: mirror the live engine config exactly
    uv run python backtest/run_backtest.py --date 2026-05-21 --from-engine

    # Use env vars (same as the service reads)
    uv run python backtest/run_backtest.py --date 2026-05-21 --from-env

    # With tick data for higher-fidelity replay
    uv run python backtest/run_backtest.py --date 2026-05-21 --from-engine --tick-data tick_logs

    # Override specific params on top of engine config
    uv run python backtest/run_backtest.py --date 2026-05-21 --from-engine --max-risk 600

    # Multiple days:
    uv run python backtest/run_backtest.py --date 2026-05-19 --date 2026-05-20 --from-engine
"""

import argparse
import datetime as dt
import gzip
import json
import os
import sys
import urllib.request

# Windows console defaults to cp1252 which can't encode ₹ (₹). Force UTF-8
# so the script renders consistently on PowerShell, cmd, and Linux terminals.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.simplified_stock_engine_core import (
    DIRECTION_BUY,
    Candle,
    CompletedTrade,
    FiveMinuteCandleBuilder,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
    compute_zerodha_intraday_charges,
)

# ── Default stocks: FnO Top Gainers from May 20, 2026 ──────────────────────
DEFAULT_STOCKS = [
    "POWERINDIA",
    "ABB",
    "CGPOWER",
    "SIEMENS",
    "MANKIND",
    "HINDALCO",
    "HINDPETRO",
    "SAMMAANCAP",
]

# ── Market timing ───────────────────────────────────────────────────────────
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)


# ── Config sourcing ─────────────────────────────────────────────────────────


def config_from_engine_api(
    base_url: str = "http://127.0.0.1:5000",
) -> SimplifiedEngineConfig:
    """Fetch the live engine's running config from the status API.

    This ensures backtests use the exact same parameters as the live engine
    (atr_sl_mult, max_trades_per_day, cooldown_candles, etc.) rather than
    hardcoded defaults that can silently diverge.
    """
    url = f"{base_url}/chartink/simplified-engine/api/status"
    print(f"  Fetching live engine config from {url} ...")
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [ERROR] Could not reach engine API: {e}")
        print("  [HINT] Is OpenAlgo running? Falling back to env-based config.")
        return config_from_env_safe()

    if body.get("status") != "success":
        print(f"  [WARN] Engine API returned unexpected status: {body}")
        return config_from_env_safe()

    data = body.get("data", {})

    # Map API response fields to SimplifiedEngineConfig fields.
    # The status endpoint exposes a subset; fill the rest from env defaults.
    cfg = config_from_env_safe()

    # Override with values the API exposes
    if "atr_sl_mult" in data:
        cfg = _replace(cfg, atr_sl_mult=float(data["atr_sl_mult"]))
    if "max_trades_per_day" in data:
        cfg = _replace(cfg, max_trades_per_day=int(data["max_trades_per_day"]))
    if "cooldown_candles" in data:
        cfg = _replace(cfg, cooldown_candles=int(data["cooldown_candles"]))
    if "engine_mode" in data:
        # We always force disabled for backtest — just log what the live mode is
        print(f"  Live engine mode: {data['engine_mode']}")

    # Always override mode to disabled for backtesting safety
    cfg = _replace(cfg, mode="disabled")

    print(
        f"  Config loaded: atr_sl_mult={cfg.atr_sl_mult}, "
        f"max_trades={cfg.max_trades_per_day}, "
        f"cooldown={cfg.cooldown_candles}, "
        f"capital={cfg.account_capital}, "
        f"leverage={cfg.account_leverage}x"
    )
    return cfg


def config_from_env_safe() -> SimplifiedEngineConfig:
    """Read config from the same env vars the live service uses.

    Imports the service's config_from_env() to guarantee parity, then
    forces mode=disabled for backtest safety.
    """
    try:
        from services.simplified_stock_engine_service import config_from_env

        cfg = config_from_env()
        # Force disabled mode — backtests must never touch sandbox.db or broker
        cfg = _replace(cfg, mode="disabled")
        print(
            f"  Config from env: atr_sl_mult={cfg.atr_sl_mult}, "
            f"max_trades={cfg.max_trades_per_day}, "
            f"cooldown={cfg.cooldown_candles}, "
            f"capital={cfg.account_capital}, "
            f"leverage={cfg.account_leverage}x"
        )
        return cfg
    except Exception as e:
        print(f"  [WARN] Could not load config_from_env: {e}")
        print("  [WARN] Using dataclass defaults (mode=disabled)")
        return SimplifiedEngineConfig(mode="disabled")


def _replace(cfg: SimplifiedEngineConfig, **overrides) -> SimplifiedEngineConfig:
    """Return a new config with specific fields overridden.

    dataclasses.replace() would work but we support manual overrides too.
    """
    import dataclasses

    return dataclasses.replace(cfg, **overrides)


# ── Tick data loading ───────────────────────────────────────────────────────


def load_tick_data(
    tick_dir: str,
    date_str: str,
    symbols: list[str] | None = None,
) -> dict[str, list[dict]]:
    """Load tick-log JSONL files for a given date.

    Tick logs are written by TickLogWriter with filename pattern:
        <tick_dir>/ticks-YYYYMMDD-<pid>.jsonl  (or .jsonl.gz)
    Each line: {"ts": "...", "symbol": "INFY", "ltp": 1234.5, "volume": 100}

    Multiple PID files may exist for the same date (e.g. after app restart).
    All matching files are loaded and merged.

    Also supports the older format:
        <tick_dir>/ticks_YYYY-MM-DD.jsonl  (or .jsonl.gz)
    with field "price" instead of "ltp".

    Returns {symbol: [{"price": float, "volume": int, "ts": datetime}, ...]}
    sorted by timestamp.
    """
    result: dict[str, list[dict]] = {}

    # The TickLogWriter uses: ticks-YYYYMMDD-<pid>.jsonl[.gz]
    # We also support the older format: ticks_YYYY-MM-DD.jsonl[.gz]
    date_compact = date_str.replace("-", "")  # "2026-05-21" → "20260521"

    tick_files: list[str] = []

    if os.path.isdir(tick_dir):
        for name in os.listdir(tick_dir):
            # Match writer format: ticks-YYYYMMDD-*.jsonl[.gz]
            if name.startswith(f"ticks-{date_compact}-") and (
                name.endswith(".jsonl") or name.endswith(".jsonl.gz")
            ):
                tick_files.append(os.path.join(tick_dir, name))
            # Match legacy format: ticks_YYYY-MM-DD.jsonl[.gz]
            elif name.startswith(f"ticks_{date_str}") and (
                name.endswith(".jsonl") or name.endswith(".jsonl.gz")
            ):
                tick_files.append(os.path.join(tick_dir, name))

    if not tick_files:
        return result

    print(
        f"  Loading tick data from {len(tick_files)} file(s): {', '.join(os.path.basename(f) for f in tick_files)}"
    )
    line_count = 0

    for tick_file in tick_files:
        opener = gzip.open if tick_file.endswith(".gz") else open
        with opener(tick_file, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    sym = row.get("symbol", "").upper()
                    if symbols and sym not in symbols:
                        continue

                    ts_raw = row.get("ts") or row.get("timestamp")
                    if isinstance(ts_raw, str):
                        ts = dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        ts = ts.replace(tzinfo=None)
                    elif isinstance(ts_raw, (int, float)):
                        ts = dt.datetime.fromtimestamp(
                            ts_raw / 1000 if ts_raw > 10_000_000_000 else ts_raw
                        )
                    else:
                        continue

                    if sym not in result:
                        result[sym] = []
                    # TickLogWriter uses "ltp", legacy format uses "price"
                    price = row.get("ltp") or row.get("price") or 0
                    result[sym].append(
                        {
                            "price": float(price),
                            "volume": int(row.get("volume", 0) or 0),
                            "ts": ts,
                        }
                    )
                    line_count += 1
                except (json.JSONDecodeError, ValueError, KeyError):
                    continue

    # Sort each symbol's ticks by timestamp
    for sym in result:
        result[sym].sort(key=lambda t: t["ts"])

    total_symbols = len(result)
    print(f"  Loaded {line_count} ticks across {total_symbols} symbols")
    return result


def symbols_from_engine(base_url: str = "http://127.0.0.1:5000") -> list[str]:
    """Fetch the stock list currently armed in the live engine.

    Returns the union of buy_symbols and sell_symbols from the engine's
    status endpoint. This lets ``--replay-symbols`` replay exactly the
    stocks that were fed to the engine today.
    """
    url = f"{base_url}/chartink/simplified-engine/api/status"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [WARN] Could not reach engine for symbol list: {e}")
        return []

    data = body.get("data", {})
    syms = list(data.get("active_symbols", {}).keys())
    if not syms:
        # Fallback: union of buy + sell lists
        syms = list(set(data.get("buy_symbols", []) + data.get("sell_symbols", [])))
    return syms


def symbols_from_results(results_path: str) -> list[str]:
    """Extract the unique stock list from a previous backtest results JSON.

    Useful for replaying a past day after the engine has reset and no
    longer has those symbols armed.
    """
    try:
        with open(results_path) as f:
            data = json.load(f)
        # Handle both single-report and multi-report format
        trades = data.get("trades", [])
        if not trades and "reports" in data:
            trades = []
            for r in data["reports"]:
                trades.extend(r.get("trades", []))
        syms = list(dict.fromkeys(t["symbol"] for t in trades))  # preserve order, dedupe
        return syms
    except Exception as e:
        print(f"  [WARN] Could not load symbols from {results_path}: {e}")
        return []


def resolve_broker_auth() -> tuple[str | None, str | None, str | None]:
    """Resolve the active broker session's auth credentials from the DB.

    The history service needs either an OpenAlgo api_key (hashed in the DB,
    not recoverable) or a direct broker auth_token + broker name. We use the
    latter: pull the stored, decrypted Zerodha token for the single active
    (non-revoked) session — the same path internal callers use.

    Returns (auth_token, feed_token, broker), or (None, None, None) if no
    active session is found.
    """
    try:
        from database.auth_db import Auth, get_auth_token, get_feed_token

        session = Auth.query.filter_by(is_revoked=False).first()
        if session is None:
            return None, None, None

        auth_token = get_auth_token(session.name)
        feed_token = get_feed_token(session.name)
        return auth_token, feed_token, session.broker
    except Exception as e:
        print(f"  [WARN] Could not resolve broker auth: {e}")
        return None, None, None


def fetch_history_candles(
    symbol: str,
    exchange: str,
    date_str: str,
    history_days: int = 3,
    auth_token: str | None = None,
    feed_token: str | None = None,
    broker: str | None = None,
) -> list[Candle]:
    """Fetch 5-min candles from broker API via the history service.

    Returns a list of Candle objects sorted by timestamp.
    Fetches `history_days` days of history ending on `date_str` to
    ensure the ATR has enough data to warm up.
    """
    from services.history_service import get_history

    target = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    start = target - dt.timedelta(days=history_days)

    success, response, status_code = get_history(
        symbol=symbol,
        exchange=exchange,
        interval="5m",
        start_date=start.strftime("%Y-%m-%d"),
        end_date=target.strftime("%Y-%m-%d"),
        auth_token=auth_token,
        feed_token=feed_token,
        broker=broker,
    )

    if not success:
        print(f"  [WARN] Failed to fetch history for {symbol}: {response}")
        return []

    raw_data = response.get("data", [])
    if not raw_data:
        print(f"  [WARN] No candle data returned for {symbol}")
        return []

    candles = []
    for row in raw_data:
        c = _row_to_candle(row)
        if c is not None:
            candles.append(c)

    candles.sort(key=lambda c: c.ts)
    print(f"  {symbol}: {len(candles)} candles fetched ({start} to {target})")
    return candles


def _row_to_candle(row: dict) -> Candle | None:
    """Parse a history API row into a Candle. Mirrors the service's logic."""
    try:
        raw_ts = (
            row.get("timestamp")
            or row.get("datetime")
            or row.get("date")
            or row.get("time")
            or row.get("t")
        )
        if isinstance(raw_ts, (int, float)):
            ts = dt.datetime.fromtimestamp(raw_ts / 1000 if raw_ts > 10_000_000_000 else raw_ts)
        elif isinstance(raw_ts, str):
            from dateutil import parser as date_parser

            ts = date_parser.parse(raw_ts)
        elif isinstance(raw_ts, dt.datetime):
            ts = raw_ts
        else:
            return None

        return Candle(
            ts=FiveMinuteCandleBuilder.bucket(ts.replace(tzinfo=None)),
            open=float(row.get("open", row.get("o", 0))),
            high=float(row.get("high", row.get("h", row.get("open", 0)))),
            low=float(row.get("low", row.get("l", row.get("open", 0)))),
            close=float(row.get("close", row.get("c", 0))),
            volume=int(row.get("volume", row.get("v", 0)) or 0),
            elapsed_pct=1.0,
        )
    except Exception:
        return None


class BacktestRunner:
    """Replays historical candles (or tick data) through the SimplifiedStockEngine."""

    def __init__(
        self,
        symbols: list[str],
        direction: str = DIRECTION_BUY,
        config: SimplifiedEngineConfig | None = None,
    ):
        self.symbols = symbols
        self.direction = direction
        # Use disabled mode — we don't want to touch sandbox.db or broker
        self.config = config or SimplifiedEngineConfig(mode="disabled")
        self._sim_time = dt.datetime.now()
        self.engine = SimplifiedStockEngine(
            config=self.config,
            now_provider=lambda: self._sim_time,
        )
        self.trade_log: list[dict] = []
        self._replay_mode: str = "candle"  # "candle" or "tick"

    def run(
        self,
        date_str: str,
        all_candles: dict[str, list[Candle]],
        tick_data: dict[str, list[dict]] | None = None,
    ) -> list[CompletedTrade]:
        """Run the backtest for a single date.

        Args:
            date_str: The target date (YYYY-MM-DD)
            all_candles: {symbol: [Candle, ...]} with multi-day history
            tick_data: Optional {symbol: [{"price", "volume", "ts"}, ...]}
                       When provided, replays ticks through FiveMinuteCandleBuilder
                       for higher-fidelity stop-loss and trailing behaviour.

        Returns:
            List of CompletedTrade objects from the engine.
        """
        target_date = dt.datetime.strptime(date_str, "%Y-%m-%d").date()

        # ── 1. Load history (prior days) to warm up ATR ─────────────────
        for symbol in self.symbols:
            candles = all_candles.get(symbol, [])
            history = [c for c in candles if c.ts.date() < target_date]
            if history:
                self.engine.load_historical_candles(symbol, history)
                print(f"  {symbol}: Loaded {len(history)} historical candles for ATR warmup")

            # Activate direction
            if self.direction == DIRECTION_BUY:
                self.engine.activate_buy_symbol(symbol)
            else:
                self.engine.activate_sell_symbol(symbol)

        # ── 2. Choose replay mode ──────────────────────────────────────
        # If tick data is available for the target date, use tick replay
        # for higher fidelity. Otherwise fall back to candle replay.
        if tick_data and any(tick_data.values()):
            self._replay_mode = "tick"
            print(f"\n  Replay mode: TICK ({sum(len(v) for v in tick_data.values())} ticks)")
            return self._run_tick_replay(date_str, target_date, all_candles, tick_data)
        else:
            self._replay_mode = "candle"
            print("\n  Replay mode: CANDLE (5-min bars)")
            return self._run_candle_replay(date_str, target_date, all_candles)

    def _run_candle_replay(
        self,
        date_str: str,
        target_date: dt.date,
        all_candles: dict[str, list[Candle]],
    ) -> list[CompletedTrade]:
        """Original candle-based replay logic."""
        day_candles: list[tuple[str, Candle]] = []
        for symbol in self.symbols:
            candles = all_candles.get(symbol, [])
            for c in candles:
                if c.ts.date() == target_date:
                    day_candles.append((symbol, c))

        # Sort all candles across symbols by timestamp
        day_candles.sort(key=lambda x: x[1].ts)

        entries_triggered = 0
        exits_triggered = 0

        for symbol, candle in day_candles:
            # Advance simulation clock to this candle's time
            # Set to candle_end (bucket + 5 min - 1 sec) to simulate 70%+ elapsed
            self._sim_time = candle.ts + dt.timedelta(seconds=self.config.candle_seconds - 1)

            # Feed candle to engine
            entry_signal = self.engine.on_new_candle(symbol, candle)

            if entry_signal:
                entries_triggered += 1
                # Auto-confirm entry at the signal's reference price
                position = self.engine.confirm_entry(symbol, entry_signal.reference_price)
                if position:
                    self._log_event(
                        "ENTRY",
                        symbol,
                        candle,
                        {
                            "price": position.entry_price,
                            "qty": position.qty,
                            "stop_loss": position.stop_loss,
                            "risk_per_share": position.risk_per_share,
                        },
                    )

            # Check for exits (stop-loss, trailing, EOD). on_price_update may
            # return exits for OTHER symbols too (global profit-lock, EOD
            # batch), so confirm each signal against its OWN symbol.
            exit_signals = self.engine.on_price_update(symbol, candle.close)
            for exit_sig in exit_signals:
                exits_triggered += 1
                trade = self.engine.confirm_exit(
                    exit_sig.symbol, exit_sig.reference_price, exit_sig.reason
                )
                if trade:
                    self._log_event(
                        "EXIT",
                        exit_sig.symbol,
                        candle,
                        {
                            "entry_price": trade.entry_price,
                            "exit_price": trade.exit_price,
                            "qty": trade.qty,
                            "gross_pnl": trade.gross_pnl,
                            "reason": trade.exit_reason,
                        },
                    )

            # Also check with high/low to see if SL was hit intra-candle
            if symbol in self.engine.positions:
                pos = self.engine.positions[symbol]
                # Check if SL was breached by candle low (for longs)
                if pos.qty > 0 and candle.low <= pos.stop_loss:
                    exit_sigs = self.engine.on_price_update(symbol, pos.stop_loss)
                    for exit_sig in exit_sigs:
                        exits_triggered += 1
                        trade = self.engine.confirm_exit(
                            symbol, pos.stop_loss, "stop_loss_intracandle"
                        )
                        if trade:
                            self._log_event(
                                "EXIT",
                                symbol,
                                candle,
                                {
                                    "entry_price": trade.entry_price,
                                    "exit_price": trade.exit_price,
                                    "qty": trade.qty,
                                    "gross_pnl": trade.gross_pnl,
                                    "reason": trade.exit_reason,
                                },
                            )
                elif pos.qty < 0 and candle.high >= pos.stop_loss:
                    exit_sigs = self.engine.on_price_update(symbol, pos.stop_loss)
                    for exit_sig in exit_sigs:
                        exits_triggered += 1
                        trade = self.engine.confirm_exit(
                            symbol, pos.stop_loss, "stop_loss_intracandle"
                        )
                        if trade:
                            self._log_event(
                                "EXIT",
                                symbol,
                                candle,
                                {
                                    "entry_price": trade.entry_price,
                                    "exit_price": trade.exit_price,
                                    "qty": trade.qty,
                                    "gross_pnl": trade.gross_pnl,
                                    "reason": trade.exit_reason,
                                },
                            )

        # ── 3. Force EOD exit for any remaining positions ───────────────
        self._force_eod_exits(target_date)

        print(f"\n  Signals: {entries_triggered} entries, {exits_triggered} exits")
        return list(self.engine.completed_trades)

    def _run_tick_replay(
        self,
        date_str: str,
        target_date: dt.date,
        all_candles: dict[str, list[Candle]],
        tick_data: dict[str, list[dict]],
    ) -> list[CompletedTrade]:
        """Tick-by-tick replay for higher-fidelity backtesting.

        Each tick is fed through a FiveMinuteCandleBuilder, which emits the
        current (continuously-updating) 5-min candle on every tick — exactly
        as the live service does. Each emitted candle is passed to
        on_new_candle(), and each tick is also passed to on_price_update() so
        stop-loss and trailing logic reacts to actual price movement, not just
        candle OHLC extremes.
        """
        entries_triggered = 0
        exits_triggered = 0

        # Build per-symbol candle builders that route completed candles
        # back to the engine
        completed_candles: dict[str, list[Candle]] = {s: [] for s in self.symbols}

        def make_handler(sym: str):
            def handler(symbol: str, candle: Candle):
                completed_candles[symbol].append(candle)

            return handler

        builders: dict[str, FiveMinuteCandleBuilder] = {}
        for sym in self.symbols:
            builders[sym] = FiveMinuteCandleBuilder(
                make_handler(sym),
                candle_seconds=self.config.candle_seconds,
            )

        # Merge all ticks across symbols into a single timeline
        all_ticks: list[tuple[str, dict]] = []
        for sym in self.symbols:
            for tick in tick_data.get(sym, []):
                if tick["ts"].date() == target_date:
                    all_ticks.append((sym, tick))

        all_ticks.sort(key=lambda x: x[1]["ts"])
        print(f"  Replaying {len(all_ticks)} ticks across {len(self.symbols)} symbols ...")

        for sym, tick in all_ticks:
            ts = tick["ts"]
            price = tick["price"]
            volume = tick["volume"]

            # Advance simulation clock
            self._sim_time = ts

            # Feed tick to candle builder — emits the current 5-min candle
            builders[sym].on_tick(sym, price, volume, ts)

            # Process any newly completed candles
            while completed_candles[sym]:
                candle = completed_candles[sym].pop(0)
                entry_signal = self.engine.on_new_candle(sym, candle)
                if entry_signal:
                    entries_triggered += 1
                    position = self.engine.confirm_entry(sym, entry_signal.reference_price)
                    if position:
                        self._log_event(
                            "ENTRY",
                            sym,
                            candle,
                            {
                                "price": position.entry_price,
                                "qty": position.qty,
                                "stop_loss": position.stop_loss,
                                "risk_per_share": position.risk_per_share,
                            },
                        )

            # Feed every tick to on_price_update for SL/trailing checks.
            # on_price_update may return exits for OTHER symbols too (global
            # profit-lock and the EOD batch close all open positions at once),
            # so confirm each signal against its OWN symbol, not the tick's.
            exit_signals = self.engine.on_price_update(sym, price)
            for exit_sig in exit_signals:
                exits_triggered += 1
                trade = self.engine.confirm_exit(
                    exit_sig.symbol, exit_sig.reference_price, exit_sig.reason
                )
                if trade:
                    self._log_event(
                        "EXIT_TICK",
                        exit_sig.symbol,
                        None,
                        {
                            "entry_price": trade.entry_price,
                            "exit_price": trade.exit_price,
                            "qty": trade.qty,
                            "gross_pnl": trade.gross_pnl,
                            "reason": trade.exit_reason,
                            "tick_price": price,
                            "tick_time": ts.strftime("%H:%M:%S.%f")[:12],
                        },
                    )

        # Force EOD exit
        self._force_eod_exits(target_date)

        print(f"\n  Signals: {entries_triggered} entries, {exits_triggered} exits")
        return list(self.engine.completed_trades)

    def _force_eod_exits(self, target_date: dt.date):
        """Force EOD exit for any remaining positions.

        check_eod_exits() returns exit signals for ALL open positions in a
        single call and then marks EOD done (subsequent calls return []), so
        it must be called once — not per-symbol — and each signal confirmed
        against its own symbol.
        """
        self._sim_time = dt.datetime.combine(target_date, self.config.eod_exit_time)
        for exit_sig in self.engine.check_eod_exits():
            trade = self.engine.confirm_exit(exit_sig.symbol, exit_sig.reference_price, "eod")
            if trade:
                self._log_event(
                    "EXIT_EOD",
                    exit_sig.symbol,
                    None,
                    {
                        "entry_price": trade.entry_price,
                        "exit_price": trade.exit_price,
                        "qty": trade.qty,
                        "gross_pnl": trade.gross_pnl,
                        "reason": trade.exit_reason,
                    },
                )

    def _log_event(self, event_type: str, symbol: str, candle: Candle | None, details: dict):
        entry = {
            "event": event_type,
            "symbol": symbol,
            "time": self._sim_time.strftime("%H:%M:%S"),
            "candle_time": candle.ts.strftime("%H:%M") if candle else "—",
            **details,
        }
        self.trade_log.append(entry)


def print_results(
    date_str: str,
    trades: list[CompletedTrade],
    trade_log: list[dict],
    config: SimplifiedEngineConfig,
):
    """Pretty-print backtest results."""
    print("\n" + "=" * 70)
    print(f"  BACKTEST RESULTS — {date_str}")
    print(f"  Capital: ₹{config.account_capital:,.0f} | Leverage: {config.account_leverage}x")
    print(f"  Max Risk/Trade: ₹{config.max_risk_per_trade:,.0f}")
    print("=" * 70)

    if not trades:
        print("\n  No trades triggered. Possible reasons:")
        print("  - No 5-min breakout with volume confirmation occurred")
        print("  - ATR entry filter rejected all signals")
        print("  - Market was closed or no data available")
        print("=" * 70)
        return

    # ── Per-trade breakdown ─────────────────────────────────────────────
    print(
        f"\n  {'#':>2}  {'Symbol':<14} {'Dir':>4} {'Qty':>5} {'Entry':>9} {'Exit':>9} "
        f"{'Gross P&L':>10} {'Charges':>8} {'Net P&L':>10} {'Reason':<12}"
    )
    print("  " + "-" * 95)

    total_gross = 0.0
    total_charges = 0.0
    total_net = 0.0
    winners = 0
    losers = 0

    for i, t in enumerate(trades, 1):
        charges = compute_zerodha_intraday_charges(t.buy_value, t.sell_value)
        net_pnl = t.gross_pnl - charges.total
        total_gross += t.gross_pnl
        total_charges += charges.total
        total_net += net_pnl

        if net_pnl >= 0:
            winners += 1
        else:
            losers += 1

        direction = "LONG" if t.is_long else "SHORT"
        reason = (t.exit_reason or "unknown")[:12]
        pnl_sign = "+" if t.gross_pnl >= 0 else ""

        print(
            f"  {i:>2}  {t.symbol:<14} {direction:>4} {t.abs_qty:>5} "
            f"₹{t.entry_price:>8,.2f} ₹{t.exit_price:>8,.2f} "
            f"{pnl_sign}₹{t.gross_pnl:>8,.2f} ₹{charges.total:>7,.2f} "
            f"{'+' if net_pnl >= 0 else ''}₹{net_pnl:>8,.2f} {reason}"
        )

    # ── Summary ─────────────────────────────────────────────────────────
    print("  " + "-" * 95)
    win_rate = (winners / len(trades) * 100) if trades else 0
    print(
        f"\n  Total Trades: {len(trades)}  |  Winners: {winners}  |  Losers: {losers}  |  Win Rate: {win_rate:.0f}%"
    )
    print(f"  Gross P&L:  {'+' if total_gross >= 0 else ''}₹{total_gross:,.2f}")
    print(f"  Charges:    ₹{total_charges:,.2f}")
    print(f"  Net P&L:    {'+' if total_net >= 0 else ''}₹{total_net:,.2f}")
    print(
        f"  ROI:        {'+' if total_net >= 0 else ''}{(total_net / config.account_capital * 100):.2f}%"
    )
    print("=" * 70)

    # ── Detailed Trade Log ──────────────────────────────────────────────
    if trade_log:
        print(f"\n  TRADE LOG ({len(trade_log)} events):")
        print(f"  {'Time':>8} {'Event':<6} {'Symbol':<14} {'Details'}")
        print("  " + "-" * 60)
        for entry in trade_log:
            event = entry.pop("event")
            symbol = entry.pop("symbol")
            time = entry.pop("time")
            candle_time = entry.pop("candle_time", "")
            details = ", ".join(f"{k}={v}" for k, v in entry.items())
            print(f"  {time:>8} {event:<6} {symbol:<14} {details}")
        print()


def generate_json_report(
    date_str: str,
    trades: list[CompletedTrade],
    config: SimplifiedEngineConfig,
) -> dict:
    """Generate a structured JSON report."""
    trade_data = []
    total_gross = 0.0
    total_charges_val = 0.0

    for t in trades:
        charges = compute_zerodha_intraday_charges(t.buy_value, t.sell_value)
        net_pnl = t.gross_pnl - charges.total
        total_gross += t.gross_pnl
        total_charges_val += charges.total

        trade_data.append(
            {
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
                "net_pnl": round(net_pnl, 2),
                "turnover": round(t.turnover, 2),
            }
        )

    total_net = total_gross - total_charges_val
    winners = sum(1 for t in trade_data if t["net_pnl"] >= 0)

    return {
        "date": date_str,
        "config": {
            "capital": config.account_capital,
            "leverage": config.account_leverage,
            "max_risk_per_trade": config.max_risk_per_trade,
            "atr_period": config.atr_period,
            "atr_sl_mult": config.atr_sl_mult,
            "atr_entry_min_mult": config.atr_entry_min_mult,
            "volume_multiplier": config.volume_multiplier,
            "max_trades_per_day": config.max_trades_per_day,
            "cooldown_candles": config.cooldown_candles,
            "trail_atr_mult": config.trail_atr_mult,
            "rr_trail_start_r": config.rr_trail_start_r,
            "sl_confirm_seconds": config.sl_confirm_seconds,
            "enable_global_profit_lock": config.enable_global_profit_lock,
        },
        "summary": {
            "total_trades": len(trades),
            "winners": winners,
            "losers": len(trades) - winners,
            "win_rate": round(winners / len(trades) * 100, 1) if trades else 0,
            "gross_pnl": round(total_gross, 2),
            "total_charges": round(total_charges_val, 2),
            "net_pnl": round(total_net, 2),
            "roi_pct": round(total_net / config.account_capital * 100, 2),
        },
        "trades": trade_data,
    }


def main():
    parser = argparse.ArgumentParser(
        description="OpenAlgo Simplified Engine Backtester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Config sourcing (pick one):
  --from-engine   Fetch live config from the running engine API (recommended)
  --from-env      Read SIMPLIFIED_ENGINE_* env vars (same as production)
  (neither)       Use dataclass defaults — NOT recommended, may diverge

Any CLI override (--capital, --atr-sl-mult, etc.) is applied ON TOP of
whichever base config was loaded.

Exact day replay (reproduce today's session):
  --from-engine --replay-symbols --tick-data tick_logs --date 2026-05-22

  This fetches the live config AND stock list from the running engine, then
  replays tick-by-tick through the same engine logic. The closest you can
  get to a perfect reproduction of the live trading day.

Symbol sources (pick one):
  --replay-symbols   Fetch stock list from the running engine (today's replay)
  --from-results F   Load stock list from a previous results JSON file
  --symbols S1,S2    Explicit comma-separated list
  (none)             Use default FnO top gainers list

Tick data replay:
  --tick-data DIR   Replay tick-log JSONL files from DIR instead of 5-min
                    candles. Produces higher-fidelity SL and trailing results.
                    Enable tick logging in .env: SIMPLIFIED_ENGINE_TICK_LOG_ENABLED=true
""",
    )
    parser.add_argument(
        "--date",
        "-d",
        action="append",
        default=None,
        help="Date(s) to backtest (YYYY-MM-DD). Can specify multiple. Default: yesterday.",
    )
    parser.add_argument(
        "--symbols",
        "-s",
        default=None,
        help=f"Comma-separated symbols. Default: {','.join(DEFAULT_STOCKS)}",
    )
    parser.add_argument(
        "--replay-symbols",
        action="store_true",
        default=False,
        help="Fetch the stock list from the running engine (exact day replay).",
    )
    parser.add_argument(
        "--from-results",
        default=None,
        metavar="FILE",
        help="Load stock list from a previous backtest results JSON file.",
    )
    parser.add_argument(
        "--direction",
        default="BUY",
        choices=["BUY", "SELL"],
        help="Trading direction. Default: BUY",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=5,
        help="Days of history for ATR warmup. Default: 5",
    )
    parser.add_argument(
        "--json-output",
        default=None,
        help="Save results as JSON to this file path.",
    )

    # ── Config sourcing ──────────────────────────────────────────────────
    config_group = parser.add_mutually_exclusive_group()
    config_group.add_argument(
        "--from-engine",
        action="store_true",
        default=False,
        help="Fetch config from live engine API (recommended).",
    )
    config_group.add_argument(
        "--from-env",
        action="store_true",
        default=False,
        help="Read config from SIMPLIFIED_ENGINE_* env vars.",
    )
    parser.add_argument(
        "--engine-url",
        default="http://127.0.0.1:5000",
        help="Base URL for engine API (used with --from-engine). Default: http://127.0.0.1:5000",
    )

    # ── Tick data ────────────────────────────────────────────────────────
    parser.add_argument(
        "--tick-data",
        default=None,
        metavar="DIR",
        help="Directory containing tick-log JSONL files (ticks_YYYY-MM-DD.jsonl).",
    )

    # ── Optional config overrides (applied on top of base config) ────────
    parser.add_argument("--capital", type=float, default=None, help="Override account capital.")
    parser.add_argument("--leverage", type=float, default=None, help="Override leverage.")
    parser.add_argument("--max-risk", type=float, default=None, help="Override max risk per trade.")
    parser.add_argument(
        "--atr-sl-mult", type=float, default=None, help="Override ATR stop-loss multiplier."
    )
    parser.add_argument("--max-trades", type=int, default=None, help="Override max trades per day.")
    parser.add_argument("--cooldown", type=int, default=None, help="Override cooldown candles.")
    parser.add_argument(
        "--volume-mult", type=float, default=None, help="Override volume multiplier."
    )

    args = parser.parse_args()

    dates = args.date or [(dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d")]

    # Resolve symbols: --replay-symbols > --from-results > --symbols > defaults
    if args.replay_symbols:
        symbols = symbols_from_engine(args.engine_url)
        if not symbols:
            print(
                "  [ERROR] --replay-symbols: no active symbols in engine. "
                "Use --symbols or --from-results instead."
            )
            sys.exit(1)
        print(f"  Symbols from engine: {', '.join(symbols)}")
    elif args.from_results:
        symbols = symbols_from_results(args.from_results)
        if not symbols:
            print(f"  [ERROR] --from-results: no symbols found in {args.from_results}")
            sys.exit(1)
        print(f"  Symbols from results file: {', '.join(symbols)}")
    elif args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = DEFAULT_STOCKS[:]

    # ── Load base config ─────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  OpenAlgo Simplified Engine Backtester")
    print("=" * 70)

    if args.from_engine:
        print("\n  Config source: LIVE ENGINE API")
        config = config_from_engine_api(args.engine_url)
    elif args.from_env:
        print("\n  Config source: ENVIRONMENT VARIABLES")
        config = config_from_env_safe()
    else:
        print("\n  Config source: DATACLASS DEFAULTS (consider using --from-engine)")
        config = SimplifiedEngineConfig(mode="disabled")

    # ── Apply CLI overrides on top of base config ────────────────────────
    import dataclasses

    overrides = {}
    if args.capital is not None:
        overrides["account_capital"] = args.capital
    if args.leverage is not None:
        overrides["account_leverage"] = args.leverage
    if args.max_risk is not None:
        overrides["max_risk_per_trade"] = args.max_risk
    if args.atr_sl_mult is not None:
        overrides["atr_sl_mult"] = args.atr_sl_mult
    if args.max_trades is not None:
        overrides["max_trades_per_day"] = args.max_trades
    if args.cooldown is not None:
        overrides["cooldown_candles"] = args.cooldown
    if args.volume_mult is not None:
        overrides["volume_multiplier"] = args.volume_mult

    if overrides:
        config = dataclasses.replace(config, **overrides)
        print(f"  CLI overrides applied: {overrides}")

    # Always ensure disabled mode
    if config.mode != "disabled":
        config = dataclasses.replace(config, mode="disabled")

    print(f"\n  Dates:        {', '.join(dates)}")
    print(f"  Symbols:      {', '.join(symbols)}")
    print(f"  Direction:    {args.direction}")
    print(f"  Capital:      ₹{config.account_capital:,.0f} @ {config.account_leverage}x leverage")
    print(f"  ATR SL mult:  {config.atr_sl_mult}")
    print(f"  Max trades:   {config.max_trades_per_day}")
    print(f"  Cooldown:     {config.cooldown_candles} candles")
    print(f"  Tick data:    {args.tick_data or 'Not provided (candle mode)'}")
    print("=" * 70)

    # Resolve broker auth once — the history service needs a broker token to
    # hit the Zerodha API (DuckDB fallback has no intraday data for these).
    auth_token, feed_token, broker = resolve_broker_auth()
    if auth_token and broker:
        print(f"  Broker session: {broker} (token resolved from DB)")
    else:
        print("  [WARN] No active broker session found — history fetch will fail.")

    all_reports = []

    for date_str in dates:
        print(f"\n{'─' * 70}")
        print(f"  Fetching data for {date_str}...")
        print(f"{'─' * 70}")

        # Fetch candles for all symbols (always needed for ATR warmup)
        all_candles: dict[str, list[Candle]] = {}
        for symbol in symbols:
            candles = fetch_history_candles(
                symbol,
                "NSE",
                date_str,
                args.history_days,
                auth_token=auth_token,
                feed_token=feed_token,
                broker=broker,
            )
            if candles:
                all_candles[symbol] = candles

        if not all_candles:
            print(f"\n  [ERROR] No data available for any symbol on {date_str}")
            continue

        # Load tick data if requested
        tick_data = None
        if args.tick_data:
            tick_data = load_tick_data(args.tick_data, date_str, symbols)
            if not tick_data:
                print(f"  [INFO] No tick data found for {date_str} — falling back to candle mode")

        # Run backtest
        runner = BacktestRunner(symbols=symbols, direction=args.direction, config=config)
        trades = runner.run(date_str, all_candles, tick_data=tick_data)

        # Print results
        print_results(date_str, trades, runner.trade_log, config)

        # Generate JSON report
        report = generate_json_report(date_str, trades, config)
        report["replay_mode"] = runner._replay_mode
        report["symbols_used"] = symbols
        all_reports.append(report)

    # Save JSON if requested — auto-save to backtest/ dir if not specified
    json_output = args.json_output
    if not json_output and all_reports:
        # Auto-save to backtest/results_YYYY-MM-DD.json
        backtest_dir = os.path.dirname(os.path.abspath(__file__))
        first_date = dates[0]
        json_output = os.path.join(backtest_dir, f"results_{first_date}.json")

    if json_output and all_reports:
        output = all_reports[0] if len(all_reports) == 1 else {"reports": all_reports}
        with open(json_output, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\n  JSON report saved to: {json_output}")

    return all_reports


if __name__ == "__main__":
    main()
