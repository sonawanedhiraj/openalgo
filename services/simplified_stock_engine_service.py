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
    EntrySignal,
    ExitSignal,
    FiveMinuteCandleBuilder,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
)
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
    """Resolve the engine routing mode from environment.

    Preference order:
    1. SIMPLIFIED_ENGINE_MODE explicitly set to disabled|sandbox|live.
    2. Backward-compat: SIMPLIFIED_ENGINE_DRY_RUN. true -> sandbox, false -> live.
       (A deprecation warning is logged when this fallback is used.)
    3. Default: sandbox (safe -- never sends live orders without explicit opt-in).
    """
    raw_mode = os.getenv("SIMPLIFIED_ENGINE_MODE")
    if raw_mode is not None:
        normalized = raw_mode.strip().lower()
        if normalized in VALID_MODES:
            return normalized
        logger.warning(
            "Invalid SIMPLIFIED_ENGINE_MODE=%r; expected one of %s. Falling back to sandbox.",
            raw_mode,
            VALID_MODES,
        )
        return MODE_SANDBOX

    raw_dry_run = os.getenv("SIMPLIFIED_ENGINE_DRY_RUN")
    if raw_dry_run is not None:
        logger.warning(
            "SIMPLIFIED_ENGINE_DRY_RUN is deprecated; use SIMPLIFIED_ENGINE_MODE=sandbox or live instead."
        )
        return MODE_SANDBOX if raw_dry_run.strip().lower() in {"1", "true", "yes", "on"} else MODE_LIVE

    return MODE_SANDBOX


def config_from_env() -> SimplifiedEngineConfig:
    return SimplifiedEngineConfig(
        account_capital=_env_float("SIMPLIFIED_ENGINE_CAPITAL", 20000.0),
        account_leverage=_env_float("SIMPLIFIED_ENGINE_LEVERAGE", 5.0),
        max_risk_per_trade=_env_float("SIMPLIFIED_ENGINE_MAX_RISK_PER_TRADE", 500.0),
        min_risk_per_share=_env_float("SIMPLIFIED_ENGINE_MIN_RISK_PER_SHARE", 1.0),
        max_trades_per_day=_env_int("SIMPLIFIED_ENGINE_MAX_TRADES_PER_DAY", 6),
        exchange=os.getenv("SIMPLIFIED_ENGINE_EXCHANGE", "NSE").upper(),
        product=os.getenv("SIMPLIFIED_ENGINE_PRODUCT", "MIS").upper(),
        no_new_openings_time=_parse_time_env(
            "SIMPLIFIED_ENGINE_NO_NEW_ENTRIES_AFTER", dt.time(15, 10)
        ),
        eod_exit_time=_parse_time_env("SIMPLIFIED_ENGINE_EOD_EXIT_TIME", dt.time(15, 20)),
        atr_period=_env_int("SIMPLIFIED_ENGINE_ATR_PERIOD", 14),
        atr_sl_mult=_env_float("SIMPLIFIED_ENGINE_ATR_SL_MULT", 1.2),
        atr_entry_min_mult=_env_float("SIMPLIFIED_ENGINE_ATR_ENTRY_MIN_MULT", 0.5),
        volume_multiplier=_env_float("SIMPLIFIED_ENGINE_VOLUME_MULTIPLIER", 2.5),
        trail_atr_mult=_env_float("SIMPLIFIED_ENGINE_TRAIL_ATR_MULT", 0.5),
        sl_confirm_seconds=_env_float("SIMPLIFIED_ENGINE_SL_CONFIRM_SECONDS", 3.0),
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
        self.history_source = os.getenv("SIMPLIFIED_ENGINE_HISTORY_SOURCE", "api")
        self.history_lookback_days = _env_int("SIMPLIFIED_ENGINE_HISTORY_LOOKBACK_DAYS", 3)
        self.order_poll_attempts = _env_int("SIMPLIFIED_ENGINE_ORDER_POLL_ATTEMPTS", 5)
        self.order_poll_interval = _env_float("SIMPLIFIED_ENGINE_ORDER_POLL_INTERVAL", 1.0)
        # Funds-cache TTL in seconds. The live-mode entry gate caches the
        # broker's availablecash reading so a burst of entries within this
        # window doesn't hammer the broker's funds endpoint.
        self.funds_cache_ttl_seconds = _env_float(
            "SIMPLIFIED_ENGINE_FUNDS_CACHE_SECONDS", 30.0
        )
        # api_key -> (timestamp, available_cash). Populated on successful
        # funds-fetch only; failures leave the cache untouched so the next
        # entry triggers a re-fetch (fail-open-with-retry semantics).
        self._funds_cache: dict[str, tuple[float, float]] = {}
        self._lock = threading.RLock()
        self._user_api_keys: dict[str, str] = {}
        self._strategy_by_symbol: dict[str, str] = {}
        self._api_key_by_symbol: dict[str, str] = {}
        self._user_callbacks_registered: set[str] = set()
        self._subscribed_symbols: set[tuple[str, str, str]] = set()
        self._sl_timers: dict[str, threading.Timer] = {}
        # Tracks the date on which the live-mode broker-position-aware EOD
        # flatten has already run. Reset implicitly when the date rolls.
        self._eod_flatten_done_date: dt.date | None = None
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
            return {"status": "error", "message": "No symbols found", "processed": []}

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

        with self._lock:
            try:
                self.builder.on_tick(normalized, price, volume, ts)
            except Exception:
                logger.exception("[SIMPLIFIED-ENGINE] Candle builder failed for %s", normalized)

            exit_signals = self.engine.on_price_update(normalized, price)

        for signal in exit_signals:
            self._schedule_exit(signal)

        self._maybe_flatten_eod()

    def status(self) -> dict[str, Any]:
        with self._lock:
            buy_symbols = [
                s for s, d in self.engine.symbol_direction.items() if d == DIRECTION_BUY
            ]
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

    def _schedule_entry(self, signal: EntrySignal) -> None:
        api_key = self._api_key_by_symbol.get(signal.symbol)
        strategy_name = self._strategy_by_symbol.get(signal.symbol, "simplified_stock_engine")
        if not api_key:
            logger.error("[SIMPLIFIED-ENGINE] No API key mapped for %s", signal.symbol)
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
        api_key = self._api_key_by_symbol.get(signal.symbol)
        strategy_name = self._strategy_by_symbol.get(signal.symbol, "simplified_stock_engine")
        if not api_key:
            logger.error("[SIMPLIFIED-ENGINE] No API key mapped for %s exit", signal.symbol)
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

    def _confirm_and_place_exit(
        self, signal: ExitSignal, api_key: str, strategy_name: str
    ) -> None:
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

    def _place_entry_order(
        self, signal: EntrySignal, api_key: str, strategy_name: str
    ) -> None:
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
                return

        payload = self._order_payload(signal, strategy_name)
        success, response = self._dispatch_order(payload, api_key, is_entry=True)
        if not success:
            logger.error("[SIMPLIFIED-ENTRY] Order failed for %s: %s", signal.symbol, response)
            with self._lock:
                self.engine.clear_pending_entry(signal.symbol)
            return

        order_id = response.get("orderid")
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
            return

        with self._lock:
            position = self.engine.confirm_entry(signal.symbol, executed_price)
        logger.info(
            "[SIMPLIFIED-ENTRY] Position created (mode=%s): %s", self.mode, position
        )

    def _place_exit_order(self, signal: ExitSignal, api_key: str, strategy_name: str) -> None:
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
                self.engine.confirm_exit(signal.symbol)
            return

        payload = self._order_payload(signal, strategy_name)
        success, response = self._dispatch_order(payload, api_key, is_entry=False)
        if not success:
            logger.error("[SIMPLIFIED-EXIT] Order failed for %s: %s", signal.symbol, response)
            with self._lock:
                self.engine.clear_pending_exit(signal.symbol)
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
            self.engine.confirm_exit(signal.symbol)
        logger.info(
            "[SIMPLIFIED-EXIT] Closed %s reason=%s price=%.2f (mode=%s)",
            signal.symbol,
            signal.reason,
            executed_price,
            self.mode,
        )

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
            logger.warning(
                "[SIMPLIFIED-FUNDS] availablecash=%r is not numeric; failing open", raw
            )
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
            known_qty_by_symbol = {
                symbol: pos.qty for symbol, pos in self.engine.positions.items()
            }
            strategy_label = next(iter(self._strategy_by_symbol.values()), "simplified_stock_engine")

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
                "[SIMPLIFIED-EOD] Drift: broker has %s qty=%s but engine doesn't; "
                "issuing %s %s",
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
            return {"status": "error", "message": response.get("message"), "status_code": status_code}

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
                ts = dt.datetime.fromtimestamp(
                    raw_ts / 1000 if raw_ts > 10_000_000_000 else raw_ts
                )
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
            except Exception:
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
        from database.settings_db import get_analyze_mode

        return "analyze" if get_analyze_mode() else MODE_LIVE


_service: SimplifiedStockEngineService | None = None
_service_lock = threading.Lock()


def get_simplified_stock_engine_service() -> SimplifiedStockEngineService:
    global _service
    with _service_lock:
        if _service is None:
            _service = SimplifiedStockEngineService()
        return _service
