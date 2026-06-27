import datetime as dt
import json
import os
import threading
import time
from typing import Any

from dateutil import parser as date_parser

from services.simplified_stock_engine_core import (
    DIRECTION_BUY,
    DIRECTION_SELL,
    MODE_DISABLED,
    MODE_LIVE,
    MODE_SANDBOX,
    VALID_MODES,
    Candle,
    CompletedTrade,
    EntrySignal,
    ExitSignal,
    FiveMinuteCandleBuilder,
    Position,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
    TradeCharges,
    compute_zerodha_intraday_charges,
)
from services.simplified_stock_engine_ticklog import TickLogWriter
from utils.logging import get_logger

logger = get_logger(__name__)


def _parse_time_env(name: str, default: dt.time) -> dt.time:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return dt.datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError:
        logger.warning("Invalid %s=%r, using %s", name, value, default.strftime("%H:%M"))
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _resolve_mode_from_env() -> str:
    """Resolve the engine routing mode from SIMPLIFIED_ENGINE_MODE.

    Recognized values: disabled | sandbox | live. Anything else (or unset)
    falls back to sandbox so an entry-typo never silently sends live orders.

    Note: an earlier release honored SIMPLIFIED_ENGINE_DRY_RUN as a backward-
    compat shim. That fallback has been removed; operators upgrading must
    set SIMPLIFIED_ENGINE_MODE explicitly.
    """
    raw_mode = os.getenv("SIMPLIFIED_ENGINE_MODE")
    if raw_mode is None:
        return MODE_SANDBOX
    normalized = raw_mode.strip().lower()
    if normalized in VALID_MODES:
        return normalized
    logger.warning(
        "Invalid SIMPLIFIED_ENGINE_MODE=%r; expected one of %s. Falling back to sandbox.",
        raw_mode,
        VALID_MODES,
    )
    return MODE_SANDBOX


def config_from_env() -> SimplifiedEngineConfig:
    return SimplifiedEngineConfig(
        account_capital=_env_float("SIMPLIFIED_ENGINE_CAPITAL", 20000.0),
        account_leverage=_env_float("SIMPLIFIED_ENGINE_LEVERAGE", 5.0),
        max_risk_per_trade=_env_float("SIMPLIFIED_ENGINE_MAX_RISK_PER_TRADE", 500.0),
        min_risk_per_share=_env_float("SIMPLIFIED_ENGINE_MIN_RISK_PER_SHARE", 1.0),
        max_trades_per_day=_env_int("SIMPLIFIED_ENGINE_MAX_TRADES_PER_DAY", 4),
        exchange=os.getenv("SIMPLIFIED_ENGINE_EXCHANGE", "NSE").upper(),
        product=os.getenv("SIMPLIFIED_ENGINE_PRODUCT", "MIS").upper(),
        no_new_openings_time=_parse_time_env(
            "SIMPLIFIED_ENGINE_NO_NEW_ENTRIES_AFTER", dt.time(15, 10)
        ),
        eod_exit_time=_parse_time_env("SIMPLIFIED_ENGINE_EOD_EXIT_TIME", dt.time(15, 20)),
        atr_period=_env_int("SIMPLIFIED_ENGINE_ATR_PERIOD", 14),
        atr_sl_mult=_env_float("SIMPLIFIED_ENGINE_ATR_SL_MULT", 1.5),
        atr_entry_min_mult=_env_float("SIMPLIFIED_ENGINE_ATR_ENTRY_MIN_MULT", 0.5),
        volume_multiplier=_env_float("SIMPLIFIED_ENGINE_VOLUME_MULTIPLIER", 2.5),
        trail_atr_mult=_env_float("SIMPLIFIED_ENGINE_TRAIL_ATR_MULT", 0.5),
        sl_confirm_seconds=_env_float("SIMPLIFIED_ENGINE_SL_CONFIRM_SECONDS", 3.0),
        cooldown_candles=_env_int("SIMPLIFIED_ENGINE_COOLDOWN_CANDLES", 3),
        same_day_stopout_block=_env_bool("RISK_SAME_DAY_STOPOUT_BLOCK", True),
        enable_global_profit_lock=_env_bool("SIMPLIFIED_ENGINE_GLOBAL_PROFIT_LOCK", True),
        mode=_resolve_mode_from_env(),
        funds_floor=_resolve_funds_floor_from_env(),
    )


def _resolve_funds_floor_from_env() -> float | None:
    """Returns None when SIMPLIFIED_ENGINE_FUNDS_FLOOR is unset, signalling
    SimplifiedEngineConfig to fall back to account_capital."""
    raw = os.getenv("SIMPLIFIED_ENGINE_FUNDS_FLOOR")
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Invalid SIMPLIFIED_ENGINE_FUNDS_FLOOR=%r; falling back to account_capital",
            raw,
        )
        return None


def parse_chartink_symbols(payload: dict[str, Any]) -> list[str]:
    symbols: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            symbols.extend([s for s in value.split(",") if s.strip()])
        elif isinstance(value, list):
            symbols.extend([str(s) for s in value if str(s).strip()])

    add(payload.get("stocks"))
    add(payload.get("symbol"))
    add(payload.get("nsecode"))

    seen = set()
    out = []
    for raw in symbols:
        normalized = normalize_chartink_symbol(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def normalize_chartink_symbol(symbol: str) -> str:
    cleaned = str(symbol or "").strip().upper()
    if not cleaned:
        return ""
    for prefix in ("NSE:", "BSE:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    for suffix in (".NS", ".BO", "-EQ"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    cleaned = cleaned.replace(" ", "")

    mapping = _symbol_overrides()
    return mapping.get(cleaned, cleaned)


def _symbol_overrides() -> dict[str, str]:
    raw = os.getenv("SIMPLIFIED_ENGINE_SYMBOL_MAP", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("SIMPLIFIED_ENGINE_SYMBOL_MAP is not valid JSON")
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k).upper(): str(v).upper() for k, v in parsed.items()}


class SimplifiedStockEngineService:
    def __init__(
        self,
        config: SimplifiedEngineConfig | None = None,
        engine: SimplifiedStockEngine | None = None,
    ):
        self.config = config or config_from_env()
        self.engine = engine or SimplifiedStockEngine(self.config)
        self.builder = FiveMinuteCandleBuilder(
            self._handle_candle,
            candle_seconds=self.config.candle_seconds,
        )
        # Order routing mode: disabled | sandbox | live. See MODE_* in core.
        self.mode = self.config.mode
        # Unified daily-intent resolver (services.mode_service). Injectable for
        # tests; default consults resolve_strategy_mode('simplified_engine').
        # See docs/design/strategy_daily_intent.md — the gate lives here (order
        # dispatch) rather than in place_order_service because the engine's own
        # _dispatch_order bypasses that path in sandbox mode.
        self._intent_resolver: Any | None = None
        self.history_source = os.getenv("SIMPLIFIED_ENGINE_HISTORY_SOURCE", "api")
        self.history_lookback_days = _env_int("SIMPLIFIED_ENGINE_HISTORY_LOOKBACK_DAYS", 3)
        self.order_poll_attempts = _env_int("SIMPLIFIED_ENGINE_ORDER_POLL_ATTEMPTS", 5)
        self.order_poll_interval = _env_float("SIMPLIFIED_ENGINE_ORDER_POLL_INTERVAL", 1.0)
        # Funds-cache TTL in seconds. The live-mode entry gate caches the
        # broker's availablecash reading so a burst of entries within this
        # window doesn't hammer the broker's funds endpoint.
        self.funds_cache_ttl_seconds = _env_float("SIMPLIFIED_ENGINE_FUNDS_CACHE_SECONDS", 30.0)
        # api_key -> (timestamp, available_cash). Populated on successful
        # funds-fetch only; failures leave the cache untouched so the next
        # entry triggers a re-fetch (fail-open-with-retry semantics).
        self._funds_cache: dict[str, tuple[float, float]] = {}
        # Tick log writer (step 5). Off by default; opt in with
        # SIMPLIFIED_ENGINE_TICK_LOG=true. Cheap no-op when disabled.
        self._tick_log = TickLogWriter(
            enabled=_env_bool("SIMPLIFIED_ENGINE_TICK_LOG", False),
            directory=os.getenv("SIMPLIFIED_ENGINE_TICK_LOG_DIR", "tick_logs"),
            max_queue=_env_int("SIMPLIFIED_ENGINE_TICK_LOG_QUEUE", 10000),
            batch_size=_env_int("SIMPLIFIED_ENGINE_TICK_LOG_BATCH", 200),
            flush_seconds=_env_float("SIMPLIFIED_ENGINE_TICK_LOG_FLUSH_SECONDS", 1.0),
            compress=_env_bool("SIMPLIFIED_ENGINE_TICK_LOG_COMPRESS", False),
            retention_days=_env_int("SIMPLIFIED_ENGINE_TICK_LOG_RETENTION_DAYS", 14),
        )
        self._lock = threading.RLock()
        self._user_api_keys: dict[str, str] = {}
        self._strategy_by_symbol: dict[str, str] = {}
        self._api_key_by_symbol: dict[str, str] = {}
        self._user_callbacks_registered: set[str] = set()
        self._subscribed_symbols: set[tuple[str, str, str]] = set()
        self._sl_timers: dict[str, threading.Timer] = {}
        # Throttle state for the "no api_key resolvable at all" log, keyed by
        # symbol → last monotonic log time. Stops a keyless window (e.g. before
        # the broker session exists) from flooding errors.jsonl the way the
        # unmapped-symbol exit did on 2026-06-19.
        self._keyless_logged_at: dict[str, float] = {}
        # Tracks the date on which the live-mode broker-position-aware EOD
        # flatten has already run. Reset implicitly when the date rolls.
        self._eod_flatten_done_date: dt.date | None = None
        # Tracks the date on which the EOD trading summary has been logged.
        # Independent of _eod_flatten_done_date so the summary can run in
        # sandbox/disabled modes (where the flatten is a no-op).
        self._eod_summary_done_date: dt.date | None = None
        # Per-direction kill switches. When False, webhook arms for that direction
        # are rejected. Existing positions are NOT closed by toggling these.
        self._direction_enabled: dict[str, bool] = {
            DIRECTION_BUY: _env_bool("SIMPLIFIED_ENGINE_BUY_ENABLED", True),
            DIRECTION_SELL: _env_bool("SIMPLIFIED_ENGINE_SELL_ENABLED", True),
        }

    def process_chartink_webhook(
        self,
        *,
        user_id: str,
        strategy_name: str,
        payload: dict[str, Any],
        direction_override: str | None = None,
    ) -> dict[str, Any]:
        symbols = parse_chartink_symbols(payload)
        if not symbols:
            return {"status": "empty", "message": "No symbols found", "processed": []}

        direction = (direction_override or self._infer_direction(payload)).upper()
        if direction not in (DIRECTION_BUY, DIRECTION_SELL):
            return {
                "status": "error",
                "message": f"Unsupported direction: {direction}",
                "processed": [],
            }

        with self._lock:
            enabled = self._direction_enabled.get(direction, False)
        if not enabled:
            return {
                "status": "ignored",
                "message": f"{direction} strategy is currently disabled",
                "direction": direction,
                "processed": [],
                "ignored": symbols,
            }

        from database.auth_db import get_api_key_for_tradingview

        api_key = get_api_key_for_tradingview(user_id)
        if not api_key:
            return {"status": "error", "message": "No OpenAlgo API key found", "processed": []}

        self._user_api_keys[user_id] = api_key
        self._ensure_websocket_callback(user_id)

        processed = []
        rejected = []
        for symbol in symbols:
            resolved, reason = self._resolve_symbol(symbol)
            if not resolved:
                rejected.append({"symbol": symbol, "reason": reason})
                continue

            with self._lock:
                if direction == DIRECTION_SELL:
                    self.engine.activate_sell_symbol(resolved)
                else:
                    self.engine.activate_buy_symbol(resolved)
                self._strategy_by_symbol[resolved] = strategy_name
                self._api_key_by_symbol[resolved] = api_key

            history_result = self._seed_history(resolved, api_key)
            subscription_result = self._subscribe_quote(user_id, api_key, resolved)
            processed.append(
                {
                    "symbol": resolved,
                    "direction": direction,
                    "history": history_result,
                    "subscription": subscription_result,
                }
            )

        status = "success" if processed else "error"
        return {
            "status": status,
            "direction": direction,
            "mode": self._mode_label(),
            "engine_mode": self.mode,
            "processed": processed,
            "rejected": rejected,
        }

    @staticmethod
    def _infer_direction(payload: dict[str, Any]) -> str:
        scan_name = str(payload.get("scan_name", "")).lower()
        if "sell" in scan_name or "short" in scan_name or "cover" in scan_name:
            return DIRECTION_SELL
        return DIRECTION_BUY

    def get_direction_enabled(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._direction_enabled)

    def set_direction_enabled(self, direction: str, enabled: bool) -> dict[str, bool]:
        normalized = direction.upper()
        if normalized not in (DIRECTION_BUY, DIRECTION_SELL):
            raise ValueError(f"Unsupported direction: {direction}")
        with self._lock:
            self._direction_enabled[normalized] = bool(enabled)
            return dict(self._direction_enabled)

    def on_quote(self, symbol: str, quote: dict[str, Any]) -> None:
        normalized = normalize_chartink_symbol(symbol)
        price = self._extract_price(quote)
        if price is None:
            return

        volume = self._extract_volume(quote, normalized)
        ts = self._extract_timestamp(quote)

        # Off the hot path: enqueue is non-blocking and a no-op when disabled.
        self._tick_log.enqueue(normalized, price, volume, ts)

        with self._lock:
            try:
                self.builder.on_tick(normalized, price, volume, ts)
            except Exception:
                logger.exception("[SIMPLIFIED-ENGINE] Candle builder failed for %s", normalized)

            exit_signals = self.engine.on_price_update(normalized, price)

        for signal in exit_signals:
            self._schedule_exit(signal)

        self._maybe_flatten_eod()
        self._maybe_log_eod_summary()

    def status(self) -> dict[str, Any]:
        with self._lock:
            buy_symbols = [s for s, d in self.engine.symbol_direction.items() if d == DIRECTION_BUY]
            sell_symbols = [
                s for s, d in self.engine.symbol_direction.items() if d == DIRECTION_SELL
            ]
            # Most recent funds reading across all api_keys we've seen. Surfaced
            # so operators can tell why the engine has stopped arming entries.
            funds_summary: dict[str, Any] | None = None
            if self._funds_cache:
                last_ts, last_value = max(self._funds_cache.values(), key=lambda t: t[0])
                funds_summary = {
                    "available_cash": last_value,
                    "floor": self.config.effective_funds_floor,
                    "checked_at": dt.datetime.fromtimestamp(last_ts).isoformat(),
                }
            return {
                "mode": self._mode_label(),
                "engine_mode": self.mode,
                "eod_flatten_done": self._eod_flatten_done_date.isoformat()
                if self._eod_flatten_done_date
                else None,
                "eod_summary_done": self._eod_summary_done_date.isoformat()
                if self._eod_summary_done_date
                else None,
                "completed_trades_today": len(self.engine.completed_trades),
                "tick_log": self._tick_log.stats(),
                "funds": funds_summary,
                "direction_enabled": dict(self._direction_enabled),
                "positions": {
                    symbol: {
                        "qty": pos.qty,
                        "side": "LONG" if pos.qty > 0 else "SHORT",
                        "entry_price": pos.entry_price,
                        "stop_loss": pos.stop_loss,
                        "risk_per_share": pos.risk_per_share,
                    }
                    for symbol, pos in self.engine.positions.items()
                },
                "pending_entries": list(self.engine.pending_entries.keys()),
                "pending_exits": list(self.engine.pending_exits.keys()),
                "active_symbols": dict(self.engine.symbol_direction),
                "buy_symbols": buy_symbols,
                "sell_symbols": sell_symbols,
                "trades_today": self.engine.trades_today,
                "max_trades_per_day": self.config.max_trades_per_day,
                "cooldown_candles": self.config.cooldown_candles,
                "atr_sl_mult": self.config.atr_sl_mult,
                "symbols_in_cooldown": list(self.engine._sl_cooldown.keys()),
                "subscribed_symbols": [
                    {"user_id": user_id, "exchange": exchange, "symbol": symbol}
                    for user_id, exchange, symbol in self._subscribed_symbols
                ],
            }

    def _handle_candle(self, symbol: str, candle: Candle) -> None:
        with self._lock:
            signal = self.engine.on_new_candle(symbol, candle)
        if signal:
            self._schedule_entry(signal)

    # Throttle interval for the "no api_key resolvable at all" error log.
    _KEYLESS_LOG_INTERVAL_SEC = 300.0

    def _resolve_order_api_key(self, symbol: str) -> tuple[str | None, str]:
        """Resolve ``(api_key, strategy_name)`` for an engine order on ``symbol``.

        Prefers the per-symbol maps the scan webhook populates. When a symbol
        has no mapping — which happens for a position rehydrated from the
        journal after a restart that no later scan re-armed — fall back to a
        default key so the order (an exit especially) is never blocked:
        most-recently-mapped symbol key → any known user key →
        ``database.auth_db.get_first_available_api_key()``. This deployment is
        single-user (one broker session per instance), so the fallback key is
        always the correct account.

        Returns ``(None, strategy_name)`` only when no key exists anywhere
        (e.g. no API key configured / broker session never established).
        """
        strategy_name = self._strategy_by_symbol.get(symbol, "simplified_stock_engine")
        api_key = self._api_key_by_symbol.get(symbol)
        if api_key:
            return api_key, strategy_name
        # Fallback — maps are read without the lock (RLock; callers run off the
        # lock-held path and existing reads here are already lock-free) so an
        # exit is never blocked by a missing per-symbol mapping.
        if self._api_key_by_symbol:
            return next(reversed(self._api_key_by_symbol.values())), strategy_name
        if self._user_api_keys:
            return next(reversed(self._user_api_keys.values())), strategy_name
        try:
            from database.auth_db import get_first_available_api_key

            return get_first_available_api_key(), strategy_name
        except Exception:
            logger.exception(
                "[SIMPLIFIED-ENGINE] get_first_available_api_key raised for %s", symbol
            )
            return None, strategy_name

    def _log_keyless_throttled(self, symbol: str, kind: str) -> None:
        """Log the unresolvable-key case at most once per symbol per
        ``_KEYLESS_LOG_INTERVAL_SEC``, so it can never become a per-tick storm."""
        # The first-sight case must log unconditionally — using a default of
        # ``0.0`` in ``dict.get`` reduces the guard to ``now >= INTERVAL`` for
        # an unseen symbol, which fails silently on freshly-booted hosts where
        # ``time.monotonic()`` is small (CI VMs, containers). Distinguish
        # "never logged" from "logged ≥ INTERVAL ago" explicitly.
        now = time.monotonic()
        last = self._keyless_logged_at.get(symbol)
        if last is None or now - last >= self._KEYLESS_LOG_INTERVAL_SEC:
            self._keyless_logged_at[symbol] = now
            logger.error(
                "[SIMPLIFIED-ENGINE] No api_key resolvable for %s %s — order skipped "
                "(throttled; check broker session / API key)",
                symbol,
                kind,
            )

    def _schedule_entry(self, signal: EntrySignal) -> None:
        api_key, strategy_name = self._resolve_order_api_key(signal.symbol)
        if not api_key:
            self._log_keyless_throttled(signal.symbol, "entry")
            self.engine.clear_pending_entry(signal.symbol)
            return

        thread = threading.Thread(
            target=self._place_entry_order,
            args=(signal, api_key, strategy_name),
            name=f"SimplifiedEntry-{signal.symbol}",
            daemon=True,
        )
        thread.start()

    def _schedule_exit(self, signal: ExitSignal) -> None:
        api_key, strategy_name = self._resolve_order_api_key(signal.symbol)
        if not api_key:
            self._log_keyless_throttled(signal.symbol, "exit")
            self.engine.clear_pending_exit(signal.symbol)
            return

        if signal.reason == "stop_loss" and self.config.sl_confirm_seconds > 0:
            if signal.symbol in self._sl_timers:
                return
            timer = threading.Timer(
                self.config.sl_confirm_seconds,
                self._confirm_and_place_exit,
                args=(signal, api_key, strategy_name),
            )
            self._sl_timers[signal.symbol] = timer
            timer.start()
            return

        thread = threading.Thread(
            target=self._place_exit_order,
            args=(signal, api_key, strategy_name),
            name=f"SimplifiedExit-{signal.symbol}",
            daemon=True,
        )
        thread.start()

    def _confirm_and_place_exit(self, signal: ExitSignal, api_key: str, strategy_name: str) -> None:
        try:
            with self._lock:
                pos = self.engine.positions.get(signal.symbol)
                price = self.engine.last_prices.get(signal.symbol)
                if not pos or price is None or price > pos.stop_loss:
                    self.engine.clear_pending_exit(signal.symbol)
                    return
            self._place_exit_order(signal, api_key, strategy_name)
        finally:
            self._sl_timers.pop(signal.symbol, None)

    def _entry_held_by_override(self) -> bool:
        """Mode-only: consult the ephemeral ``strategy_runtime_override`` table.

        True iff a non-expired ``pause`` / ``kill_switch`` override is holding new
        entries for this strategy (set by automated safety guards — data-health
        auto-pause, daily kill-switch). Overrides block ENTRIES only; exits and
        EOD always run. Fail-open — any read error returns False (not blocked) so
        a lookup failure never silently halts trading. There is no intent axis
        anymore: with no active override, entries proceed."""
        try:
            from database.strategy_runtime_override_db import is_entry_blocked

            blocked, ov = is_entry_blocked("simplified_engine")
            if blocked and ov:
                logger.info(
                    "[SIMPLIFIED-OVERRIDE] entries held by %s (reason=%s, expires=%s)",
                    ov.get("override_type"),
                    ov.get("reason"),
                    ov.get("expires_at"),
                )
            return blocked
        except Exception:
            logger.debug("simplified runtime-override resolve failed; not blocking", exc_info=True)
            return False

    def _place_entry_order(self, signal: EntrySignal, api_key: str, strategy_name: str) -> None:
        # Mode-only safety gate: an active runtime override (pause/kill_switch)
        # holds new entries. Exits are never blocked here.
        if self._entry_held_by_override():
            logger.info(
                "[SIMPLIFIED-OVERRIDE] Entry blocked for %s by active runtime override",
                signal.symbol,
            )
            with self._lock:
                self.engine.clear_pending_entry(signal.symbol)
            return
        # Stage-0 daily circuit breaker. Sits *before* the disabled-mode
        # short-circuit so disabled-mode tracing also respects the gate (so
        # an operator can see "engine would have skipped this anyway").
        # Fail-safe: a metric read error returns (False, "") and we proceed
        # as if the gate is clear — see services/risk_service.py.
        from services.risk_service import daily_circuit_breaker_tripped

        tripped, reason = daily_circuit_breaker_tripped()
        if tripped:
            logger.warning(
                "[SIMPLIFIED-RISK] Entry blocked for %s by daily circuit breaker: %s",
                signal.symbol,
                reason,
            )
            with self._lock:
                self.engine.clear_pending_entry(signal.symbol)
            return

        if self.mode == MODE_DISABLED:
            logger.info(
                "[SIMPLIFIED-DISABLED] %s %s qty=%s ref=%.2f (no order sent)",
                signal.action,
                signal.symbol,
                signal.quantity,
                signal.reference_price,
            )
            with self._lock:
                self.engine.confirm_entry(signal.symbol, signal.reference_price)
            return

        # Stage 1 — LLM veto layer. Returns (proceed, decision_id). In shadow
        # mode the decision is recorded but always proceed=True. In active
        # mode a 'skip' short-circuits before the order is dispatched. Off
        # mode bypasses the reviewer entirely. decision_id may be None when
        # persistence failed or the reviewer was skipped — the mark_*
        # helper tolerates None.
        proceed, decision_id = self._run_pre_order_review(signal, strategy_name)
        if not proceed:
            with self._lock:
                self.engine.clear_pending_entry(signal.symbol)
            self._mark_review_outcome(decision_id, taken=False)
            return

        # Live-mode funds gate: refuse to send an opening order if the
        # broker's reported available cash is below the floor. Sandbox and
        # disabled modes don't hit this path -- they're handled above.
        if self.mode == MODE_LIVE:
            ok, available, reason = self._check_live_funds(api_key)
            if not ok:
                logger.warning(
                    "[SIMPLIFIED-ENTRY] Funds gate blocked %s: available=%.2f floor=%.2f reason=%s",
                    signal.symbol,
                    available,
                    self.config.effective_funds_floor,
                    reason,
                )
                with self._lock:
                    self.engine.clear_pending_entry(signal.symbol)
                self._mark_review_outcome(decision_id, taken=False)
                return

        payload = self._order_payload(signal, strategy_name)
        success, response = self._dispatch_order(payload, api_key, is_entry=True)
        if not success:
            logger.error("[SIMPLIFIED-ENTRY] Order failed for %s: %s", signal.symbol, response)
            with self._lock:
                self.engine.clear_pending_entry(signal.symbol)
            self._mark_review_outcome(decision_id, taken=False)
            self._notify_anomaly(
                source="simplified_engine.entry_order",
                message=(f"Entry order rejected for {signal.symbol} ({signal.action}): {response}"),
                severity="error",
            )
            return

        order_id = response.get("orderid")

        # Stage 2 — write the trade-journal entry row now that the broker has
        # accepted the order. Belt-and-braces try/except: the service layer
        # is already fail-safe but the order path must never break on an
        # audit miss.
        journal_id = self._journal_record_entry(signal, decision_id, order_id)

        executed_price = self._wait_for_fill(api_key, strategy_name, order_id)
        if executed_price is None:
            logger.warning(
                "[SIMPLIFIED-ENTRY] Fill not confirmed for %s (mode=%s, orderid=%s)",
                signal.symbol,
                self.mode,
                order_id,
            )
            with self._lock:
                self.engine.clear_pending_entry(signal.symbol)
            # Order was sent to the broker even if we couldn't confirm the
            # fill — flag actually_taken=True so the audit row reflects reality.
            self._mark_review_outcome(decision_id, taken=True)
            # Stamp the fill-attempt timestamp so reflection can tell apart
            # "order placed, fill never confirmed" from "order never sent."
            self._journal_update_entry_fill(journal_id, entry_price=None)
            return

        with self._lock:
            position = self.engine.confirm_entry(signal.symbol, executed_price)
        logger.info("[SIMPLIFIED-ENTRY] Position created (mode=%s): %s", self.mode, position)
        self._mark_review_outcome(decision_id, taken=True)
        self._journal_update_entry_fill(journal_id, entry_price=executed_price)
        self._notify_trade_opened(signal, executed_price)

    def _run_pre_order_review(
        self, signal: EntrySignal, strategy_name: str
    ) -> tuple[bool, int | None]:
        """Apply the Stage-1 LLM veto layer.

        Returns ``(proceed, decision_id)``:

        * ``mode='off'`` → ``(True, None)``. Reviewer is not called.
        * ``mode='shadow'`` → ``(True, decision_id)`` regardless of the LLM's
          answer. Decision is logged but never enforced.
        * ``mode='active'`` and reviewer says ``skip`` → ``(False, decision_id)``;
          ``actually_taken=False`` is recorded immediately.
        * ``mode='active'`` and reviewer says ``take`` → ``(True, decision_id)``.

        Any unexpected exception from the reviewer fails open: ``(True, None)``.
        The reviewer's own fail-safe-to-take semantics mean this branch is
        rare in practice — it's the safety net for "we couldn't even decide
        whether to fail safely."
        """
        try:
            from services import signal_review_service

            # Mode-aware veto default: sandbox enforces ('active') by default so
            # the layer is exercised on the virtual book; live is unchanged
            # ('shadow'). VETO_LAYER_MODE env overrides in every mode.
            mode = signal_review_service.get_veto_layer_mode(self.mode)
            if mode == "off":
                return True, None

            review = signal_review_service.review_signal(
                symbol=signal.symbol,
                source=strategy_name,
                direction=signal.action,
            )
        except Exception:
            logger.exception(
                "[SIMPLIFIED-ENTRY] Veto layer raised for %s; failing open",
                signal.symbol,
            )
            return True, None

        decision_id = review.get("id")
        decision = review.get("decision")
        reasoning = (review.get("reasoning") or "")[:80]

        if mode == "shadow":
            logger.info(
                "[veto/shadow] %s %s -> %s (%s)",
                signal.symbol,
                strategy_name,
                decision,
                reasoning,
            )
            return True, decision_id

        # mode == "active"
        if decision == "skip":
            logger.info("[veto/active] vetoed %s: %s", signal.symbol, reasoning)
            return False, decision_id

        logger.info("[veto/active] %s %s -> take (%s)", signal.symbol, strategy_name, reasoning)
        return True, decision_id

    @staticmethod
    def _mark_review_outcome(decision_id: int | None, *, taken: bool) -> None:
        """Update the signal_decision row's ``actually_taken`` flag.

        Best-effort: the order path must not fail because the audit hook
        couldn't write. The downstream helper already tolerates ``None``.
        """
        if decision_id is None:
            return
        try:
            from services import signal_review_service

            signal_review_service.mark_actually_taken(decision_id, taken)
        except Exception:
            logger.exception(
                "[SIMPLIFIED-ENTRY] Failed to mark_actually_taken for decision_id=%s",
                decision_id,
            )

    # ------------------------------------------------------------------
    # Stage 2 — trade-journal write hooks
    #
    # These wrap the trade_journal_service helpers (which are themselves
    # fail-safe) in another try/except so a bug in the journal module can
    # never block order placement or exit handling. The journal write is
    # informational; trade execution is the source of truth.
    #
    # The engine's runtime identity for the journal is the registered
    # strategy name (``trending_equity_intraday``). The chartink-supplied
    # strategy label is kept on the broker order payload but not in the
    # journal — reflection groups by the strategy implementation, not by
    # the screener tag that armed it.
    # ------------------------------------------------------------------

    JOURNAL_STRATEGY_NAME = "trending_equity_intraday"

    @staticmethod
    def _normalize_exit_reason(reason: str | None) -> str:
        """Map engine exit reasons to the journal's controlled vocabulary."""
        if not reason:
            return "other"
        if reason == "eod":
            return "eod_squareoff"
        return reason

    def _journal_record_entry(
        self, signal: EntrySignal, decision_id: int | None, order_id: str | None
    ) -> int:
        """Best-effort journal entry insert. Returns the new row id (or 0)."""
        try:
            from services import trade_journal_service

            return trade_journal_service.record_entry(
                symbol=signal.symbol,
                direction="LONG" if signal.action == DIRECTION_BUY else "SHORT",
                quantity=int(signal.quantity),
                strategy_name=self.JOURNAL_STRATEGY_NAME,
                signal_source="chartink",
                entry_price=float(signal.reference_price),
                # LTP the engine acted on. Captured before any fill so slippage
                # can later be measured against the actual fill price. entry_price
                # gets overwritten with the real fill via update_entry_fill;
                # ltp_at_signal stays pinned to the decision price.
                ltp_at_signal=float(signal.reference_price),
                entry_order_id=str(order_id) if order_id else None,
                signal_decision_id=decision_id,
            )
        except Exception:
            logger.exception(
                "[SIMPLIFIED-ENTRY] trade_journal.record_entry hook failed for %s",
                signal.symbol,
            )
            return 0

    def _journal_update_entry_fill(self, journal_id: int, entry_price: float | None) -> None:
        """Best-effort journal fill update."""
        if not journal_id:
            return
        try:
            from services import trade_journal_service

            trade_journal_service.update_entry_fill(journal_id, entry_price=entry_price)
        except Exception:
            logger.exception(
                "[SIMPLIFIED-ENTRY] trade_journal.update_entry_fill hook failed for jid=%s",
                journal_id,
            )

    # ------------------------------------------------------------------
    # Notification hooks (Stage 0/2 one-way Telegram). Every helper here
    # MUST be fail-safe — a notification miss is recoverable; a missed
    # exit isn't.
    # ------------------------------------------------------------------

    def _notify_trade_opened(self, signal: EntrySignal, executed_price: float) -> None:
        try:
            from services.notification_service import get_notification_service

            direction = "LONG" if signal.action == DIRECTION_BUY else "SHORT"
            get_notification_service().publish_trade_opened(
                symbol=signal.symbol,
                direction=direction,
                quantity=int(signal.quantity),
                entry_price=float(executed_price),
                strategy=self.JOURNAL_STRATEGY_NAME,
            )
        except Exception as e:  # noqa: BLE001 — fail-safe
            logger.warning(
                "[SIMPLIFIED-ENTRY] notification publish failed for %s: %s",
                signal.symbol,
                e,
            )

    def _notify_trade_closed(self, signal: ExitSignal, executed_price: float) -> None:
        try:
            from services import trade_journal_service
            from services.notification_service import get_notification_service

            # Re-read the row we just finalised so the notification carries
            # the canonical entry_price / pnl / hold duration the journal
            # service computed. Empty fields fall back to zeros — never
            # raise on stale state.
            recent = trade_journal_service.get_trades_for_symbol(signal.symbol, days=1)
            row: dict = recent[0] if recent else {}
            direction = (row.get("direction") or "").upper() or (
                "LONG" if str(getattr(signal, "action", "")).upper() in ("SELL",) else "SHORT"
            )
            entry_price = float(row.get("entry_price") or 0.0)
            pnl = float(row.get("pnl") or 0.0)
            hold = int(row.get("hold_duration_seconds") or 0)
            get_notification_service().publish_trade_closed(
                symbol=signal.symbol,
                direction=direction,
                entry_price=entry_price,
                exit_price=float(executed_price),
                pnl=pnl,
                exit_reason=str(signal.reason or "unknown"),
                hold_duration_seconds=hold,
            )
        except Exception as e:  # noqa: BLE001 — fail-safe
            logger.warning(
                "[SIMPLIFIED-EXIT] notification publish failed for %s: %s",
                signal.symbol,
                e,
            )

    def _notify_eod_summary(self, trades_snapshot: list[CompletedTrade]) -> None:
        try:
            from services import trade_journal_service
            from services.notification_service import get_notification_service

            summary = trade_journal_service.get_today_summary()
            if summary.get("count", 0) > 0:
                get_notification_service().publish_eod_summary(
                    trade_count=int(summary["count"]),
                    winners=int(summary["winners"]),
                    losers=int(summary["losers"]),
                    net_pnl=float(summary["total_pnl"]),
                    by_strategy=summary.get("by_strategy") or {},
                )
                return

            # Journal read returned no rows. Fall back to the engine's
            # in-memory ledger so the operator still gets a daily heartbeat.
            net = sum(t.gross_pnl for t in trades_snapshot)
            wins = sum(1 for t in trades_snapshot if t.gross_pnl > 0)
            losses = sum(1 for t in trades_snapshot if t.gross_pnl < 0)
            get_notification_service().publish_eod_summary(
                trade_count=len(trades_snapshot),
                winners=wins,
                losers=losses,
                net_pnl=float(net),
                by_strategy={
                    self.JOURNAL_STRATEGY_NAME: {
                        "count": len(trades_snapshot),
                        "pnl": float(net),
                    }
                },
            )
        except Exception as e:  # noqa: BLE001 — fail-safe
            logger.warning("[SIMPLIFIED-EOD-SUMMARY] notification publish failed: %s", e)

    def _notify_anomaly(self, *, source: str, message: str, severity: str) -> None:
        try:
            from services.notification_service import get_notification_service

            get_notification_service().publish_anomaly(
                source=source, message=message, severity=severity
            )
        except Exception as e:  # noqa: BLE001 — fail-safe
            logger.warning(
                "[SIMPLIFIED-ANOMALY] notification publish failed (source=%s): %s",
                source,
                e,
            )

    def _journal_record_exit(
        self,
        symbol: str,
        *,
        exit_price: float | None,
        exit_order_id: str | None,
        exit_reason: str | None,
    ) -> None:
        """Best-effort journal exit close-out. Looks up the open row for
        ``symbol`` and stamps the exit columns.
        """
        try:
            from services import trade_journal_service

            journal_id = trade_journal_service.get_open_journal_id_for_symbol(symbol)
            if not journal_id:
                # Nothing to close out — likely the entry write was lost or
                # the symbol was opened before journaling went live. Don't
                # treat this as an error; just trace and move on.
                logger.info(
                    "[SIMPLIFIED-EXIT] No open journal row for %s; skipping exit write",
                    symbol,
                )
                return
            trade_journal_service.record_exit(
                journal_id,
                exit_price=exit_price,
                exit_order_id=str(exit_order_id) if exit_order_id else None,
                exit_reason=self._normalize_exit_reason(exit_reason),
            )
        except Exception:
            logger.exception(
                "[SIMPLIFIED-EXIT] trade_journal.record_exit hook failed for %s",
                symbol,
            )

    def _place_exit_order(self, signal: ExitSignal, api_key: str, strategy_name: str) -> None:
        # Mode-only: exits are NEVER gated. Runtime overrides (pause/kill_switch)
        # hold new entries only — a held position must always be allowed to exit.
        if self.mode == MODE_DISABLED:
            logger.info(
                "[SIMPLIFIED-DISABLED] %s %s qty=%s reason=%s ref=%.2f (no order sent)",
                signal.action,
                signal.symbol,
                signal.quantity,
                signal.reason,
                signal.reference_price,
            )
            with self._lock:
                self.engine.confirm_exit(
                    signal.symbol,
                    exit_price=signal.reference_price,
                    reason=signal.reason,
                )
            # Disabled mode doesn't send an order, so no journal entry was
            # written at entry time either — skip the exit write too.
            return

        payload = self._order_payload(signal, strategy_name)
        success, response = self._dispatch_order(payload, api_key, is_entry=False)
        if not success:
            logger.error("[SIMPLIFIED-EXIT] Order failed for %s: %s", signal.symbol, response)
            with self._lock:
                self.engine.clear_pending_exit(signal.symbol)
            self._notify_anomaly(
                source="simplified_engine.exit_order",
                message=(
                    f"Exit order rejected for {signal.symbol} (reason={signal.reason}): {response}"
                ),
                severity="error",
            )
            return

        order_id = response.get("orderid")
        executed_price = self._wait_for_fill(api_key, strategy_name, order_id)
        if executed_price is None:
            logger.warning(
                "[SIMPLIFIED-EXIT] Fill not confirmed for %s (mode=%s, orderid=%s)",
                signal.symbol,
                self.mode,
                order_id,
            )
            with self._lock:
                self.engine.clear_pending_exit(signal.symbol)
            return

        with self._lock:
            self.engine.confirm_exit(signal.symbol, exit_price=executed_price, reason=signal.reason)
        logger.info(
            "[SIMPLIFIED-EXIT] Closed %s reason=%s price=%.2f (mode=%s)",
            signal.symbol,
            signal.reason,
            executed_price,
            self.mode,
        )
        # Stage 2 — close out the matching journal row.
        self._journal_record_exit(
            signal.symbol,
            exit_price=executed_price,
            exit_order_id=order_id,
            exit_reason=signal.reason,
        )
        self._notify_trade_closed(signal, executed_price)

    def _dispatch_order(
        self, payload: dict[str, Any], api_key: str, *, is_entry: bool
    ) -> tuple[bool, dict[str, Any]]:
        """Route an order payload according to the engine's mode.

        - sandbox: call sandbox_service.sandbox_place_order directly so the engine
          can run against virtual Rs1Cr capital regardless of the global
          analyze_mode setting.
        - live: call services.place_order_service.place_order, which still honors
          the global analyze_mode flag (so an operator can flip the whole
          installation into analyzer mode and the engine follows).

        Returns (success, response_dict). Both modes produce a response with an
        "orderid" on success; the caller uses _wait_for_fill to confirm.
        """
        kind = "ENTRY" if is_entry else "EXIT"

        if self.mode == MODE_SANDBOX:
            from services.sandbox_service import sandbox_place_order

            success, response, status_code = sandbox_place_order(
                payload, api_key=api_key, original_data=payload
            )
            if not success:
                logger.warning(
                    "[SIMPLIFIED-%s] Sandbox rejected order status=%s response=%s",
                    kind,
                    status_code,
                    response,
                )
            return success, response

        if self.mode == MODE_LIVE:
            from services.place_order_service import place_order

            success, response, _ = place_order(payload, api_key=api_key)
            return success, response

        # Should be unreachable -- disabled is short-circuited by callers, and
        # config validation rejects unknown modes. Treat defensively.
        logger.error("[SIMPLIFIED-%s] Unexpected mode=%r; refusing to send order", kind, self.mode)
        return False, {"status": "error", "message": f"unsupported mode {self.mode}"}

    def _check_live_funds(self, api_key: str) -> tuple[bool, float, str | None]:
        """Pre-flight funds check for live-mode opening trades.

        Returns (allow, available_cash, reason). Semantics:
        - allow=True with reason=None when broker reports enough cash.
        - allow=False with reason="insufficient" when broker reports cash below
          self.config.effective_funds_floor.
        - allow=True with reason="fetch_failed" or "unparseable" when the funds
          API errors out or returns unexpected data. We deliberately fail open
          and leave the cache empty so the next entry triggers a re-fetch.
        - allow=True with reason="cache" when a fresh cached reading is reused.

        available_cash is the float value used for the comparison (0.0 when
        fetch failed and there was no fresh cache).
        """
        floor = self.config.effective_funds_floor

        with self._lock:
            cached = self._funds_cache.get(api_key)
            now = time.time()
            if cached is not None and (now - cached[0]) < self.funds_cache_ttl_seconds:
                cached_value = cached[1]
                if cached_value < floor:
                    return False, cached_value, "insufficient_cache"
                return True, cached_value, "cache"

        try:
            from services.funds_service import get_funds

            success, response, status_code = get_funds(api_key=api_key)
        except Exception:
            logger.exception("[SIMPLIFIED-FUNDS] get_funds raised; failing open")
            return True, 0.0, "fetch_failed"

        if not success:
            logger.warning(
                "[SIMPLIFIED-FUNDS] Funds fetch failed status=%s response=%s; failing open",
                status_code,
                response,
            )
            return True, 0.0, "fetch_failed"

        data = response.get("data") or {}
        raw = data.get("availablecash")
        if raw is None:
            logger.warning(
                "[SIMPLIFIED-FUNDS] availablecash missing from funds response; failing open"
            )
            return True, 0.0, "unparseable"

        try:
            available = float(raw)
        except (TypeError, ValueError):
            logger.warning("[SIMPLIFIED-FUNDS] availablecash=%r is not numeric; failing open", raw)
            return True, 0.0, "unparseable"

        with self._lock:
            self._funds_cache[api_key] = (now, available)

        if available < floor:
            return False, available, "insufficient"
        return True, available, None

    def _maybe_flatten_eod(self) -> None:
        """Trigger broker-position-aware EOD flatten exactly once per day.

        Runs only in live mode and only after the engine's internal
        eod_exit_time has been reached. Sandbox mode is authoritative (its
        positions can't drift from the engine's view because both are written
        by the same process), and disabled mode never sends orders.
        """
        if self.mode != MODE_LIVE:
            return

        now = dt.datetime.now()
        if now.time() < self.config.eod_exit_time:
            return

        with self._lock:
            if self._eod_flatten_done_date == now.date():
                return
            self._eod_flatten_done_date = now.date()
            # Snapshot the api_keys + known engine positions under the lock so
            # we can release it before doing the (slow) positionbook fetch.
            api_keys = set(self._api_key_by_symbol.values()) | set(self._user_api_keys.values())
            known_qty_by_symbol = {symbol: pos.qty for symbol, pos in self.engine.positions.items()}
            strategy_label = next(
                iter(self._strategy_by_symbol.values()), "simplified_stock_engine"
            )

        if not api_keys:
            logger.info("[SIMPLIFIED-EOD] No api_keys registered; skipping broker flatten")
            return

        logger.info(
            "[SIMPLIFIED-EOD] Running broker-position flatten across %d api_key(s)",
            len(api_keys),
        )
        for api_key in api_keys:
            try:
                self._flatten_for_api_key(api_key, known_qty_by_symbol, strategy_label)
            except Exception:
                logger.exception(
                    "[SIMPLIFIED-EOD] Flatten failed for api_key=%s... (truncated)",
                    api_key[:6] if api_key else "?",
                )

    def _flatten_for_api_key(
        self,
        api_key: str,
        known_qty_by_symbol: dict[str, int],
        strategy_name: str,
    ) -> None:
        """Fetch the broker positionbook for one api_key and flatten drift.

        "Drift" here means: the broker reports an open position on the engine's
        configured exchange/product that the engine has no record of. The
        engine's own check_eod_exits already emits exits for positions it
        knows about, so this pass only catches the orphans.
        """
        from services.positionbook_service import get_positionbook

        success, response, status_code = get_positionbook(api_key=api_key)
        if not success:
            logger.warning(
                "[SIMPLIFIED-EOD] positionbook fetch failed status=%s response=%s",
                status_code,
                response,
            )
            return

        broker_positions = response.get("data") or []
        engine_exchange = self.config.exchange.upper()
        engine_product = self.config.product.upper()
        broker_open_symbols: set[str] = set()

        for pos in broker_positions:
            try:
                raw_qty = pos.get("quantity") or pos.get("netqty") or pos.get("net_qty") or 0
                qty = int(float(raw_qty))
            except (TypeError, ValueError):
                logger.warning("[SIMPLIFIED-EOD] Skipping malformed position: %r", pos)
                continue

            if qty == 0:
                continue

            symbol_raw = str(pos.get("symbol", "")).strip()
            symbol = normalize_chartink_symbol(symbol_raw)
            exchange = str(pos.get("exchange", "")).strip().upper()
            product = str(pos.get("product", "")).strip().upper()

            # Only flatten positions on the exchange/product the engine itself
            # would touch. Leave the rest of the user's broker positions alone.
            if exchange and exchange != engine_exchange:
                continue
            if product and product != engine_product:
                continue
            if not symbol:
                continue

            broker_open_symbols.add(symbol)
            engine_qty = known_qty_by_symbol.get(symbol)

            if engine_qty is not None and engine_qty != 0:
                # The engine knows about this position; its own EOD path will
                # close it. Skip to avoid double-issuing an exit. Log a hint if
                # quantities disagree (likely a partial-fill drift we are not
                # reconciling in v1).
                if abs(engine_qty) != abs(qty):
                    logger.warning(
                        "[SIMPLIFIED-EOD] Qty mismatch on %s: engine=%s broker=%s "
                        "(engine's exit will close the engine's view only)",
                        symbol,
                        engine_qty,
                        qty,
                    )
                continue

            # Drift case: broker has it, engine doesn't know.
            flatten_action = "SELL" if qty > 0 else "BUY"
            flatten_qty = abs(qty)
            logger.warning(
                "[SIMPLIFIED-EOD] Drift: broker has %s qty=%s but engine doesn't; issuing %s %s",
                symbol,
                qty,
                flatten_action,
                flatten_qty,
            )
            payload = {
                "strategy": strategy_name,
                "symbol": symbol,
                "exchange": engine_exchange,
                "action": flatten_action,
                "quantity": flatten_qty,
                "pricetype": self.config.order_pricetype,
                "product": engine_product,
                "price": 0,
                "trigger_price": 0,
                "disclosed_quantity": 0,
            }
            self._dispatch_order(payload, api_key, is_entry=False)

        # Surface engine-only orphans for visibility (positions the engine
        # thinks are open but the broker doesn't show). We don't issue any
        # orders here -- there's nothing to flatten -- just warn so operators
        # can investigate.
        for symbol, engine_qty in known_qty_by_symbol.items():
            if engine_qty != 0 and symbol not in broker_open_symbols:
                logger.warning(
                    "[SIMPLIFIED-EOD] Engine thinks %s qty=%s is open but broker "
                    "reports nothing; clearing internal state",
                    symbol,
                    engine_qty,
                )

    # ------------------------------------------------------------------
    # EOD trading summary (step 4)
    # ------------------------------------------------------------------

    def _maybe_log_eod_summary(self) -> None:
        """Log the daily trading summary once per day after eod_exit_time.

        Runs in every mode (sandbox/live/disabled) since each may have
        produced completed trades. Skips silently when no trades closed.
        """
        now = dt.datetime.now()
        if now.time() < self.config.eod_exit_time:
            return

        with self._lock:
            if self._eod_summary_done_date == now.date():
                return
            # Snapshot the ledger under the lock; mark the date so we don't
            # log it twice even if the snapshot is empty.
            trades_snapshot = list(self.engine.completed_trades)
            self._eod_summary_done_date = now.date()

        # Reconcile sandbox MIS auto-square-off closures into the journal BEFORE
        # summarizing, so the Telegram count + P&L include positions the engine
        # never journaled (it only writes exits it fired itself). No-op outside
        # sandbox mode and when no open rows remain. Must run before the
        # empty-snapshot early-return below: a day where the engine fired *zero*
        # exits (all closures via square-off) has an empty in-memory ledger but a
        # journal that reconciliation can still complete.
        self._maybe_reconcile_eod_journal(now.date())

        # Did reconciliation (or the engine) leave anything to report? Read the
        # journal aggregate so an all-square-off day still summarizes even though
        # the in-memory snapshot is empty.
        journal_count = 0
        try:
            from services import trade_journal_service

            journal_count = int(trade_journal_service.get_today_summary().get("count", 0))
        except Exception as e:  # noqa: BLE001 — fail-safe
            logger.warning("[SIMPLIFIED-EOD-SUMMARY] journal count read failed: %s", e)

        if not trades_snapshot and not journal_count:
            logger.info("[SIMPLIFIED-EOD-SUMMARY] No completed trades today (mode=%s)", self.mode)
            return

        if trades_snapshot:
            lines = self._build_eod_summary_lines(trades_snapshot, now.date())
            # Single multi-line log entry so it's easy to find in error.jsonl / files.
            logger.info("[SIMPLIFIED-EOD-SUMMARY]\n%s", "\n".join(lines))

        # Fail-safe Telegram fan-out. The journal-derived aggregate is the
        # canonical source of truth (handles partial fills, rejected exits,
        # multi-leg trades, AND sandbox square-offs reconciled just above) — the
        # engine's in-memory completed_trades is just a hot-path mirror. If the
        # journal read fails, fall back to a minimal payload from the snapshot.
        self._notify_eod_summary(trades_snapshot)

    def _maybe_reconcile_eod_journal(self, today: dt.date) -> None:
        """Pull sandbox EOD square-off closures into the journal. Fail-safe.

        Only meaningful in sandbox mode (it reads ``sandbox.db``); a no-op in
        live/disabled. Gated by ``ENGINE_EOD_RECONCILIATION_ENABLED`` (default
        on) so the operator can roll it back without code changes. Idempotent —
        safe to call repeatedly within the once-per-day EOD-summary guard.
        """
        if self.mode != MODE_SANDBOX:
            return
        if not _env_bool("ENGINE_EOD_RECONCILIATION_ENABLED", True):
            return
        try:
            from services.engine_eod_reconciliation_service import reconcile_engine_journal

            result = reconcile_engine_journal(today, strategy_name=self.JOURNAL_STRATEGY_NAME)
            logger.info(
                "[SIMPLIFIED-EOD-RECONCILE] checked=%d added=%d skipped=%d",
                result.entries_checked,
                result.exits_added,
                len(result.skipped),
            )
        except Exception as e:  # noqa: BLE001 — fail-safe; a missed reconcile
            # only under-reports, it never corrupts execution.
            logger.warning("[SIMPLIFIED-EOD-RECONCILE] reconciliation failed: %s", e)

    def _build_eod_summary_lines(self, trades: list[CompletedTrade], today: dt.date) -> list[str]:
        """Produce the per-trade rows + totals as a list of formatted strings.

        Factored out so tests can assert on individual rows without parsing log
        output. Charges are approximate (Zerodha NSE equity intraday); the
        compute_zerodha_intraday_charges docstring explains the caveats.
        """
        header = f"Trading summary for {today.isoformat()} (mode={self.mode}, trades={len(trades)})"
        col_header = (
            f"{'Symbol':<12} {'Side':<5} {'Qty':>5} {'Entry':>10} {'Exit':>10} "
            f"{'Gross':>10} {'Charges':>9} {'Net':>10}"
        )
        rows: list[str] = [header, col_header, "-" * len(col_header)]

        total_gross = 0.0
        total_charges = 0.0
        for trade in trades:
            charges = compute_zerodha_intraday_charges(trade.buy_value, trade.sell_value)
            gross = trade.gross_pnl
            net = gross - charges.total
            total_gross += gross
            total_charges += charges.total
            side = "LONG" if trade.is_long else "SHORT"
            rows.append(
                f"{trade.symbol:<12} {side:<5} {trade.abs_qty:>5} "
                f"{trade.entry_price:>10.2f} {trade.exit_price:>10.2f} "
                f"{gross:>10.2f} {charges.total:>9.2f} {net:>10.2f}"
            )

        rows.append("-" * len(col_header))
        rows.append(
            f"{'TOTAL':<12} {'':<5} {'':>5} {'':>10} {'':>10} "
            f"{total_gross:>10.2f} {total_charges:>9.2f} "
            f"{(total_gross - total_charges):>10.2f}"
        )
        return rows

    def _wait_for_fill(
        self, api_key: str, strategy_name: str, order_id: str | None
    ) -> float | None:
        if not order_id:
            return None

        for _ in range(self.order_poll_attempts):
            time.sleep(self.order_poll_interval)
            from services.orderstatus_service import get_order_status

            success, response, _ = get_order_status(
                {"strategy": strategy_name, "orderid": str(order_id)},
                api_key=api_key,
            )
            if not success:
                continue

            data = response.get("data", {})
            status = str(data.get("order_status") or data.get("status") or "").lower()
            if status == "complete":
                avg = data.get("average_price") or data.get("price")
                try:
                    return float(avg)
                except (TypeError, ValueError):
                    return None
            if status in {"rejected", "cancelled", "canceled"}:
                return None
        return None

    @staticmethod
    def _order_payload(signal: EntrySignal | ExitSignal, strategy_name: str) -> dict[str, Any]:
        return {
            "strategy": strategy_name,
            "symbol": signal.symbol,
            "exchange": signal.exchange,
            "action": signal.action,
            "quantity": signal.quantity,
            "pricetype": signal.pricetype,
            "product": signal.product,
            "price": 0,
            "trigger_price": 0,
            "disclosed_quantity": 0,
        }

    def _seed_history(self, symbol: str, api_key: str) -> dict[str, Any]:
        end_date = dt.datetime.now().date()
        start_date = end_date - dt.timedelta(days=max(self.history_lookback_days, 1))
        from services.history_service import get_history

        success, response, status_code = get_history(
            symbol=symbol,
            exchange=self.config.exchange,
            interval="5m",
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            api_key=api_key,
            source=self.history_source,
        )
        if not success:
            logger.warning(
                "[SIMPLIFIED-HISTORY] Failed for %s status=%s response=%s",
                symbol,
                status_code,
                response,
            )
            return {
                "status": "error",
                "message": response.get("message"),
                "status_code": status_code,
            }

        candles = [c for c in (self._row_to_candle(row) for row in response.get("data", [])) if c]
        with self._lock:
            self.engine.load_historical_candles(symbol, candles)
        return {"status": "success", "candles": len(candles)}

    def _subscribe_quote(self, user_id: str, api_key: str, symbol: str) -> dict[str, Any]:
        key = (user_id, self.config.exchange, symbol)
        if key in self._subscribed_symbols:
            return {"status": "success", "message": "already_subscribed"}

        from database.auth_db import get_broker_name
        from services.websocket_service import subscribe_to_symbols

        broker = get_broker_name(api_key) or ""
        success, response, status_code = subscribe_to_symbols(
            user_id,
            broker,
            [{"exchange": self.config.exchange, "symbol": symbol}],
            mode="Quote",
        )
        if success:
            self._subscribed_symbols.add(key)
        else:
            logger.warning(
                "[SIMPLIFIED-WS] Subscribe failed for %s status=%s response=%s",
                symbol,
                status_code,
                response,
            )
        return response

    def _ensure_websocket_callback(self, user_id: str) -> None:
        if user_id in self._user_callbacks_registered:
            return
        from services.websocket_service import register_market_data_callback

        if register_market_data_callback(user_id, self._on_market_data):
            self._user_callbacks_registered.add(user_id)

    def _on_market_data(self, message: dict[str, Any]) -> None:
        symbol = message.get("symbol")
        if not symbol:
            return
        exchange = message.get("exchange")
        if exchange and str(exchange).upper() != self.config.exchange:
            return
        data = message.get("data") if isinstance(message.get("data"), dict) else message
        self.on_quote(str(symbol), data)

    def _resolve_symbol(self, symbol: str) -> tuple[str | None, str | None]:
        normalized = normalize_chartink_symbol(symbol)
        if not normalized:
            return None, "empty_symbol"
        from database.token_db import get_token

        token = get_token(normalized, self.config.exchange)
        if token:
            return normalized, None
        return None, f"Symbol not found on {self.config.exchange}"

    @staticmethod
    def _row_to_candle(row: dict[str, Any]) -> Candle | None:
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
            logger.exception("[SIMPLIFIED-HISTORY] Could not parse candle row: %s", row)
            return None

    def _extract_price(self, quote: dict[str, Any]) -> float | None:
        for key in ("ltp", "last_price", "last_traded_price", "price"):
            if key in quote and quote.get(key) not in (None, ""):
                try:
                    return float(quote[key])
                except (TypeError, ValueError):
                    return None
        return None

    def _extract_volume(self, quote: dict[str, Any], symbol: str) -> int:
        for key in ("volume", "volume_traded", "cum_volume"):
            if key in quote and quote.get(key) not in (None, ""):
                try:
                    return int(float(quote[key]))
                except (TypeError, ValueError):
                    break
        return self.builder.last_cum_vol.get(symbol, 0)

    @staticmethod
    def _extract_timestamp(quote: dict[str, Any]) -> dt.datetime:
        for key in ("exchange_timestamp", "timestamp", "ltt"):
            value = quote.get(key)
            if value in (None, ""):
                continue
            try:
                if isinstance(value, dt.datetime):
                    return value.replace(tzinfo=None)
                if isinstance(value, (int, float)):
                    return dt.datetime.fromtimestamp(
                        value / 1000 if value > 10_000_000_000 else value
                    )
                if isinstance(value, str):
                    return date_parser.parse(value).replace(tzinfo=None)
            except Exception:  # nosec B112 — best-effort field-fallback; try the next key on any parse failure
                continue
        return dt.datetime.now()

    def _mode_label(self) -> str:
        """Human-readable mode for status payloads and logs.

        Returns one of: "disabled", "sandbox", or for live mode either "analyze"
        (when the global analyze_mode flag is on, since place_order would route
        to sandbox anyway) or "live".
        """
        if self.mode == MODE_DISABLED:
            return MODE_DISABLED
        if self.mode == MODE_SANDBOX:
            return MODE_SANDBOX
        # mode == MODE_LIVE: still surface whether the global toggle would
        # override us into sandbox.
        #
        # TODO(stage-0): this is a status-payload label only, not a routing
        # decision — actual order routing in this engine goes through
        # services.place_order which already uses resolve_effective_mode().
        # Migrating the label to the resolver is intertwined with the engine's
        # own mode flag (SIMPLIFIED_ENGINE_MODE) and the operator's
        # daily_intent. Defer to the Stage 2/3 cleanup of the simplified
        # engine's mode handling. See docs/SIMPLIFIED_ENGINE_HANDOFF.md.
        from database.settings_db import get_analyze_mode

        return "analyze" if get_analyze_mode() else MODE_LIVE

    # ------------------------------------------------------------------
    # P0 — EOD safety net (services/eod_watchdog_service.py is the caller).
    #
    # rehydrate_positions_from_journal restores in-memory positions from
    # today's open trade_journal rows. Without it, an OpenAlgo restart
    # mid-session wipes the engine's view of what the broker actually owns,
    # which is how NBCC got stranded past EOD on 2026-06-01.
    # ------------------------------------------------------------------

    def rehydrate_positions_from_journal(self) -> int:
        """Restore the engine's in-memory ``positions`` dict from today's
        open ``trade_journal`` rows.

        Stops, ATR window, direction_state, RR trailing — none of these are
        restored. They rebuild from ticks once the broker WebSocket starts
        delivering quotes again. The only purpose of this rehydrate is to
        keep the engine internally consistent with what the broker actually
        holds, so subsequent ticks, the EOD watchdog, and manual operator
        ops don't act on a phantom-empty state.

        Skipped silently for symbols already present in ``engine.positions``
        — the source of truth is what the engine itself recorded after a
        live ``confirm_entry``. Returns the number of positions added.
        """
        try:
            from services import trade_journal_service
        except Exception:
            logger.exception("[SIMPLIFIED-REHYDRATE] trade_journal_service import failed")
            return 0

        try:
            rows = trade_journal_service.get_open_trades_today(
                strategy_name=self.JOURNAL_STRATEGY_NAME
            )
        except Exception:
            logger.exception("[SIMPLIFIED-REHYDRATE] get_open_trades_today raised")
            return 0

        # Resolve a default api_key once (outside the lock — it may hit the DB)
        # so each rehydrated symbol gets an order mapping. Without this a
        # rehydrated position has a live position but no _api_key_by_symbol
        # entry, so its exit path can't place an order — the 2026-06-19 TCS
        # error storm. Single-user deployment, so the first available key is the
        # correct account. Best-effort: a None key just leaves the runtime
        # fallback in _resolve_order_api_key to cover it.
        rehydrate_key: str | None = None
        try:
            from database.auth_db import get_first_available_api_key

            rehydrate_key = get_first_available_api_key()
        except Exception:
            logger.exception("[SIMPLIFIED-REHYDRATE] get_first_available_api_key raised")

        added = 0
        with self._lock:
            for row in rows:
                symbol = row.get("symbol")
                direction = (row.get("direction") or "").upper()
                qty_raw = row.get("quantity")
                entry_price = row.get("entry_price")
                if not symbol or direction not in ("LONG", "SHORT"):
                    continue
                if entry_price is None:
                    # An entry whose fill never confirmed. Don't rehydrate — we
                    # can't size a stop without a reference price, and the
                    # row was already accounted for elsewhere (audit only).
                    logger.warning(
                        "[SIMPLIFIED-REHYDRATE] Skipping %s: journal row has no entry_price",
                        symbol,
                    )
                    continue
                try:
                    qty = int(qty_raw)
                except (TypeError, ValueError):
                    logger.warning(
                        "[SIMPLIFIED-REHYDRATE] Skipping %s: unparseable quantity=%r",
                        symbol,
                        qty_raw,
                    )
                    continue
                if qty <= 0:
                    continue

                if symbol in self.engine.positions:
                    # Engine already knows — its in-memory state wins.
                    continue

                signed_qty = qty if direction == "LONG" else -qty
                entry_time = self._parse_entered_at(row.get("placed_at"))
                pos = Position(
                    symbol=symbol,
                    entry_price=float(entry_price),
                    qty=signed_qty,
                    # Stop is not known here — set to entry_price so the
                    # engine's tick-driven SL won't fire spuriously before
                    # the first real tick arrives. The watchdog flatten path
                    # ignores `stop_loss` entirely; tick-driven exits will
                    # overwrite it once ATR rebuilds.
                    stop_loss=float(entry_price),
                    entry_time=entry_time,
                    risk_per_share=0.0,
                    max_rr=0.0,
                )
                self.engine.positions[symbol] = pos
                # Map the symbol so its exit path can place an order. setdefault
                # never clobbers a mapping a live scan already set.
                self._strategy_by_symbol.setdefault(symbol, "simplified_stock_engine")
                if rehydrate_key:
                    self._api_key_by_symbol.setdefault(symbol, rehydrate_key)
                added += 1
                logger.info(
                    "[SIMPLIFIED-REHYDRATE] %s %s qty=%s entry=%.2f (journal_id=%s)",
                    self.JOURNAL_STRATEGY_NAME,
                    f"LONG {symbol}" if signed_qty > 0 else f"SHORT {symbol}",
                    abs(signed_qty),
                    float(entry_price),
                    row.get("id"),
                )

        if added:
            logger.info("[SIMPLIFIED-REHYDRATE] Restored %d position(s) from trade_journal", added)
        return added

    @staticmethod
    def _parse_entered_at(raw: str | None) -> dt.datetime:
        if not raw:
            return dt.datetime.now()
        try:
            parsed = date_parser.parse(raw)
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except (TypeError, ValueError):
            return dt.datetime.now()


# ----------------------------------------------------------------------
# P0 — module-level EOD flatten entry point. The watchdog
# (services/eod_watchdog_service.py) calls this; the order path here is
# the in-process ``services.place_order_service.place_order`` so the
# flatten still works even when the Flask app's tick stream is dead.
# ----------------------------------------------------------------------


def flatten_strategy_positions(
    strategy_name: str, *, reason: str = "eod_watchdog"
) -> dict[str, Any]:
    """Flatten every open intraday position for ``strategy_name`` via the
    place_order REST path.

    Returns a summary::

        {
            "strategy": "<name>",
            "reason": "<reason>",
            "attempted": <int>,
            "succeeded": <int>,
            "failed": [{"symbol": ..., "error": ...}, ...],
            "skipped": [{"symbol": ..., "reason": ...}, ...],
        }

    Failures are logged loudly and notified via the notification service;
    successful flattens write an exit row to ``trade_journal`` via
    ``record_exit`` so the row no longer reads as "open".

    Note on side: the journal row carries the *original* direction
    (``LONG``/``SHORT``). To close it we issue the opposite side (a LONG is
    flattened by a ``SELL``, a SHORT by a ``BUY``). Always ``MARKET`` —
    the watchdog is the EOD backstop, not a price-sensitive exit.
    """
    summary: dict[str, Any] = {
        "strategy": strategy_name,
        "reason": reason,
        "attempted": 0,
        "succeeded": 0,
        "failed": [],
        "skipped": [],
    }

    try:
        from services import trade_journal_service
    except Exception:
        logger.exception("[EOD-FLATTEN] trade_journal_service import failed")
        return summary

    try:
        rows = trade_journal_service.get_open_trades_today(strategy_name=strategy_name)
    except Exception:
        logger.exception("[EOD-FLATTEN] get_open_trades_today raised")
        return summary

    if not rows:
        logger.info(
            "[EOD-FLATTEN] %s: no open positions today; nothing to flatten",
            strategy_name,
        )
        return summary

    # Resolve a usable api_key. The engine's per-symbol map is the most
    # accurate source — it's populated when the chartink webhook arms a
    # symbol. Fallbacks (in priority order): the engine's user_api_keys,
    # then the auth_db's first available api_key. If all three are empty,
    # we can't dispatch — log and bail.
    api_key = _resolve_api_key_for_flatten()
    if not api_key:
        logger.error(
            "[EOD-FLATTEN] %s: no api_key available; %d positions left open",
            strategy_name,
            len(rows),
        )
        for row in rows:
            summary["failed"].append({"symbol": row.get("symbol"), "error": "no_api_key"})
        _notify_watchdog_no_api_key(strategy_name, len(rows))
        return summary

    engine_service = get_simplified_stock_engine_service()
    exchange = engine_service.config.exchange.upper()
    product = engine_service.config.product.upper()

    for row in rows:
        symbol = row.get("symbol")
        direction = (row.get("direction") or "").upper()
        qty_raw = row.get("quantity")
        journal_id = row.get("id")

        if not symbol or direction not in ("LONG", "SHORT"):
            summary["skipped"].append({"symbol": symbol, "reason": "bad_row"})
            continue
        try:
            qty = int(qty_raw)
        except (TypeError, ValueError):
            summary["skipped"].append({"symbol": symbol, "reason": "bad_qty"})
            continue
        if qty <= 0:
            summary["skipped"].append({"symbol": symbol, "reason": "zero_qty"})
            continue

        action = "SELL" if direction == "LONG" else "BUY"
        payload = {
            "strategy": strategy_name,
            "symbol": symbol,
            "exchange": exchange,
            "action": action,
            "quantity": qty,
            "pricetype": "MARKET",
            "product": product,
            "price": 0,
            "trigger_price": 0,
            "disclosed_quantity": 0,
        }

        summary["attempted"] += 1
        try:
            from services.place_order_service import place_order

            success, response, status_code = place_order(payload, api_key=api_key)
        except Exception as e:
            logger.exception("[EOD-FLATTEN] %s %s place_order raised", strategy_name, symbol)
            summary["failed"].append({"symbol": symbol, "error": f"exception:{e}"})
            _notify_watchdog_exit_failure(strategy_name, symbol, str(e))
            continue

        if not success:
            err = response.get("message") if isinstance(response, dict) else str(response)
            logger.error(
                "[EOD-FLATTEN] %s %s rejected status=%s response=%s",
                strategy_name,
                symbol,
                status_code,
                response,
            )
            summary["failed"].append({"symbol": symbol, "error": err or "rejected"})
            _notify_watchdog_exit_failure(strategy_name, symbol, err or "rejected")
            continue

        order_id = response.get("orderid") if isinstance(response, dict) else None
        # The watchdog doesn't wait for the fill — markets are closing and
        # AMO/auto-square-off semantics differ per broker. Stamp the journal
        # row as exited with the flatten reason so the rehydrate path on next
        # boot skips it; the actual fill price will be reconciled when the
        # operator reviews the journal next morning.
        try:
            trade_journal_service.record_exit(
                journal_id,
                exit_price=None,
                exit_order_id=str(order_id) if order_id else None,
                exit_reason=reason,
            )
        except Exception:
            logger.exception(
                "[EOD-FLATTEN] record_exit failed for journal_id=%s symbol=%s",
                journal_id,
                symbol,
            )

        # Also clear the engine's in-memory position so subsequent ticks
        # don't re-trigger an exit against a now-flat broker.
        try:
            engine_service.engine.positions.pop(symbol, None)
        except Exception:
            pass

        logger.warning(
            "[EOD-FLATTEN] %s %s flattened: %s %s qty=%s orderid=%s",
            strategy_name,
            symbol,
            action,
            symbol,
            qty,
            order_id,
        )
        summary["succeeded"] += 1

    return summary


def _resolve_api_key_for_flatten() -> str | None:
    """Pick the api_key the watchdog should use for its flatten orders.

    Order of preference:

    1. The engine service's per-symbol map (most-recent symbol wins). This
       is what every other engine-issued order has been using all day, so
       reusing it keeps broker-side audit trails coherent.
    2. The engine service's ``_user_api_keys`` dict (populated by the
       chartink webhook path).
    3. ``database.auth_db.get_first_available_api_key()`` — the same helper
       the scanner pre-subscribe path uses. Last resort.

    Returns ``None`` only when none of the three yield a key, in which case
    the flatten cannot proceed and the watchdog reports failure.
    """
    try:
        svc = get_simplified_stock_engine_service()
        with svc._lock:
            if svc._api_key_by_symbol:
                # Most recently mapped key wins — dicts preserve insertion order.
                return next(reversed(svc._api_key_by_symbol.values()))
            if svc._user_api_keys:
                return next(reversed(svc._user_api_keys.values()))
    except Exception:
        logger.exception("[EOD-FLATTEN] engine service lookup failed")

    try:
        from database.auth_db import get_first_available_api_key

        return get_first_available_api_key()
    except Exception:
        logger.exception("[EOD-FLATTEN] get_first_available_api_key raised")
        return None


def _notify_watchdog_exit_failure(strategy_name: str, symbol: str, error: str) -> None:
    try:
        from services.notification_service import get_notification_service

        get_notification_service().publish_eod_watchdog_failure(
            strategy_name=strategy_name,
            error=f"flatten {symbol}: {error}",
        )
    except Exception as e:  # noqa: BLE001 — fail-safe
        logger.warning("[EOD-FLATTEN] watchdog failure notification failed: %s", e)


def _notify_watchdog_no_api_key(strategy_name: str, n_positions: int) -> None:
    try:
        from services.notification_service import get_notification_service

        get_notification_service().publish_eod_watchdog_failure(
            strategy_name=strategy_name,
            error=(
                f"No api_key available; {n_positions} open position(s) left "
                "untouched. Manual intervention required."
            ),
        )
    except Exception as e:  # noqa: BLE001 — fail-safe
        logger.warning("[EOD-FLATTEN] watchdog no-api-key notification failed: %s", e)


_service: SimplifiedStockEngineService | None = None
_service_lock = threading.Lock()


def get_simplified_stock_engine_service() -> SimplifiedStockEngineService:
    global _service
    with _service_lock:
        if _service is None:
            _service = SimplifiedStockEngineService()
        return _service
