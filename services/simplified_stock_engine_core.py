import datetime as dt
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from utils.logging import get_logger

logger = get_logger(__name__)


DIRECTION_BUY = "BUY"
DIRECTION_SELL = "SELL"


@dataclass(frozen=True)
class SimplifiedEngineConfig:
    account_capital: float = 20000.0
    account_leverage: float = 5.0
    max_risk_per_trade: float = 500.0
    min_risk_per_share: float = 1.0
    max_trades_per_day: int = 6
    exchange: str = "NSE"
    product: str = "MIS"
    order_pricetype: str = "MARKET"
    no_new_openings_time: dt.time = dt.time(15, 10)
    eod_exit_time: dt.time = dt.time(15, 20)
    elapsed_pct_entry: float = 0.70
    candle_seconds: int = 300
    history_candidate_count: int = 3
    history_market_cutoff: dt.time = dt.time(9, 30)
    reference_candle_expiry_seconds: int = 20 * 60
    atr_period: int = 14
    atr_sl_mult: float = 1.2
    atr_entry_min_mult: float = 0.5
    volume_multiplier: float = 2.5
    trail_atr_mult: float = 0.5
    rr_trail_start_r: float = 0.6
    global_profit_lock_mult: float = 4.2
    lock_profit_pct: float = 0.95
    enable_global_profit_lock: bool = True
    sl_confirm_seconds: float = 3.0


@dataclass
class Candle:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    elapsed_pct: float

    def is_red(self) -> bool:
        return self.close < self.open or math.isclose(self.close, self.open, abs_tol=1e-9)


@dataclass
class ReferenceCandleRecord:
    candle: Candle
    detected_at: dt.datetime


@dataclass
class Position:
    symbol: str
    entry_price: float
    qty: int
    stop_loss: float
    entry_time: dt.datetime
    risk_per_share: float
    max_rr: float = 0.0


@dataclass
class EntrySignal:
    symbol: str
    action: str
    quantity: int
    reference_price: float
    stop_loss: float
    risk_per_share: float
    candle_ts: dt.datetime
    exchange: str
    product: str
    pricetype: str


@dataclass
class ExitSignal:
    symbol: str
    action: str
    quantity: int
    reason: str
    reference_price: float
    exchange: str
    product: str
    pricetype: str


class FiveMinuteCandleBuilder:
    def __init__(
        self,
        on_candle: Callable[[str, Candle], None],
        candle_seconds: int = 300,
    ):
        self.on_candle = on_candle
        self.candle_seconds = candle_seconds
        self.current: dict[str, dict] = {}
        self.last_cum_vol: dict[str, int] = {}

    @staticmethod
    def bucket(ts: dt.datetime) -> dt.datetime:
        return ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)

    def on_tick(self, symbol: str, price: float, cumulative_volume: int, ts: dt.datetime) -> None:
        bucket = self.bucket(ts)
        price = float(price)
        cumulative_volume = int(cumulative_volume or 0)

        if symbol not in self.current:
            self.current[symbol] = {
                "bucket": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0,
            }
            self.last_cum_vol[symbol] = cumulative_volume
            return

        current = self.current[symbol]
        if bucket != current["bucket"]:
            self._emit(symbol, 1.0)
            self.current[symbol] = {
                "bucket": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0,
            }
            self.last_cum_vol[symbol] = cumulative_volume
            return

        delta = max(cumulative_volume - self.last_cum_vol.get(symbol, cumulative_volume), 0)
        self.last_cum_vol[symbol] = cumulative_volume
        current["high"] = max(current["high"], price)
        current["low"] = min(current["low"], price)
        current["close"] = price
        current["volume"] += delta

        elapsed = max((ts - current["bucket"]).total_seconds(), 0)
        self._emit(symbol, min(elapsed / float(self.candle_seconds), 0.999))

    def _emit(self, symbol: str, elapsed_pct: float) -> None:
        current = self.current[symbol]
        candle = Candle(
            ts=current["bucket"],
            open=current["open"],
            high=current["high"],
            low=current["low"],
            close=current["close"],
            volume=int(current["volume"]),
            elapsed_pct=elapsed_pct,
        )
        self.on_candle(symbol, candle)


class SimplifiedStockEngine:
    def __init__(
        self,
        config: SimplifiedEngineConfig | None = None,
        now_provider: Callable[[], dt.datetime] | None = None,
    ):
        self.config = config or SimplifiedEngineConfig()
        self.now_provider = now_provider or dt.datetime.now
        self.symbol_direction: dict[str, str] = {}
        self.red_candles: dict[str, ReferenceCandleRecord] = {}
        self.green_candles: dict[str, ReferenceCandleRecord] = {}
        self.recent_candles: dict[str, list[Candle]] = {}
        self.positions: dict[str, Position] = {}
        self.pending_entries: dict[str, EntrySignal] = {}
        self.pending_exits: dict[str, ExitSignal] = {}
        self.bought_in_bucket: dict[str, dt.datetime] = {}
        self.last_prices: dict[str, float] = {}
        self.trades_today = 0
        self.trades_day = self.now_provider().date()
        self.eod_done_date: dt.date | None = None
        self.global_profit_actioned = False

        self._tr_deques: dict[str, deque[float]] = {}
        self._atr_map: dict[str, float] = {}
        self._prev_close: dict[str, float] = {}

    def activate_buy_symbol(self, symbol: str) -> None:
        self.symbol_direction[symbol] = DIRECTION_BUY
        self.green_candles.pop(symbol, None)

    def activate_sell_symbol(self, symbol: str) -> None:
        self.symbol_direction[symbol] = DIRECTION_SELL
        self.red_candles.pop(symbol, None)

    def deactivate_symbol(self, symbol: str) -> None:
        self.symbol_direction.pop(symbol, None)
        self.red_candles.pop(symbol, None)
        self.green_candles.pop(symbol, None)

    def clear_pending_entry(self, symbol: str) -> None:
        self.pending_entries.pop(symbol, None)

    def clear_pending_exit(self, symbol: str) -> None:
        self.pending_exits.pop(symbol, None)

    def load_historical_candles(self, symbol: str, candles: list[Candle]) -> None:
        clean = sorted([c for c in candles if isinstance(c.ts, dt.datetime)], key=lambda c: c.ts)
        if not clean:
            self.red_candles.pop(symbol, None)
            self.green_candles.pop(symbol, None)
            self.recent_candles.pop(symbol, None)
            return

        self._tr_deques.pop(symbol, None)
        self._atr_map.pop(symbol, None)
        self._prev_close.pop(symbol, None)

        for candle in clean:
            final_candle = Candle(
                ts=self._normalize_bucket(candle.ts),
                open=float(candle.open),
                high=float(candle.high),
                low=float(candle.low),
                close=float(candle.close),
                volume=int(candle.volume or 0),
                elapsed_pct=1.0,
            )
            self._update_atr_wilder(symbol, final_candle)

        candidates = self._after_market_cutoff(clean)[-self.config.history_candidate_count :]
        self.recent_candles[symbol] = candidates
        direction = self.symbol_direction.get(symbol)
        if direction == DIRECTION_SELL:
            self._refresh_green_reference(symbol)
        else:
            self._refresh_red_reference(symbol)

    def on_new_candle(self, symbol: str, candle: Candle) -> EntrySignal | None:
        if self._is_eod_done():
            return None

        direction = self.symbol_direction.get(symbol)

        if candle.elapsed_pct >= 0.999:
            final_candle = Candle(
                ts=self._normalize_bucket(candle.ts),
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                elapsed_pct=1.0,
            )
            self._update_recent(symbol, final_candle)
            self._update_atr_wilder(symbol, final_candle)
            if direction == DIRECTION_SELL:
                self._refresh_green_reference(symbol)
            else:
                self._refresh_red_reference(symbol)

        if direction == DIRECTION_BUY:
            record = self.red_candles.get(symbol)
            if not record:
                return None
            if self._is_reference_expired(record):
                self.red_candles.pop(symbol, None)
                return None
            return self._attempt_buy_entry(symbol, candle, record)

        if direction == DIRECTION_SELL:
            record = self.green_candles.get(symbol)
            if not record:
                return None
            if self._is_reference_expired(record):
                self.green_candles.pop(symbol, None)
                return None
            return self._attempt_sell_entry(symbol, candle, record)

        return None

    def on_price_update(self, symbol: str, price: float) -> list[ExitSignal]:
        price = float(price)
        self.last_prices[symbol] = price
        exits: list[ExitSignal] = []

        self.apply_simple_rr_trailing(symbol, price)

        pos = self.positions.get(symbol)
        if pos and symbol not in self.pending_exits:
            if pos.qty > 0 and price <= pos.stop_loss:
                exits.append(self._build_exit_signal(symbol, price, "stop_loss"))
            elif pos.qty < 0 and price >= pos.stop_loss:
                exits.append(self._build_exit_signal(symbol, price, "stop_loss"))

        if self.config.enable_global_profit_lock:
            exits.extend(self._check_global_profit_lock())

        exits.extend(self.check_eod_exits())
        return exits

    def check_eod_exits(self) -> list[ExitSignal]:
        now = self.now_provider()
        if now.time() < self.config.eod_exit_time:
            return []
        if self.eod_done_date == now.date():
            return []

        exits = []
        for symbol, pos in list(self.positions.items()):
            if symbol in self.pending_exits:
                continue
            price = self.last_prices.get(symbol, pos.entry_price)
            exits.append(self._build_exit_signal(symbol, price, "eod"))
        self.eod_done_date = now.date()
        return exits

    def confirm_entry(self, symbol: str, executed_price: float | None = None) -> Position | None:
        signal = self.pending_entries.pop(symbol, None)
        if not signal:
            return None

        price = float(executed_price or signal.reference_price)
        risk_per_share = max(float(signal.risk_per_share), self.config.min_risk_per_share)
        if signal.action == DIRECTION_SELL:
            qty = -abs(int(signal.quantity))
            stop_loss = round(price + risk_per_share, 2)
        else:
            qty = abs(int(signal.quantity))
            stop_loss = round(price - risk_per_share, 2)
        position = Position(
            symbol=symbol,
            entry_price=price,
            qty=qty,
            stop_loss=stop_loss,
            entry_time=self.now_provider(),
            risk_per_share=risk_per_share,
        )
        self.positions[symbol] = position
        self.bought_in_bucket[symbol] = signal.candle_ts
        self._reset_trade_day_if_needed()
        self.trades_today += 1
        return position

    def confirm_exit(self, symbol: str) -> None:
        self.pending_exits.pop(symbol, None)
        self.positions.pop(symbol, None)
        self.bought_in_bucket[symbol] = FiveMinuteCandleBuilder.bucket(self.now_provider())

    def apply_simple_rr_trailing(self, symbol: str, current_price: float) -> None:
        pos = self.positions.get(symbol)
        if not pos or pos.qty == 0:
            return

        is_long = pos.qty > 0
        abs_qty = abs(int(pos.qty))
        if is_long:
            per_share_profit = float(current_price) - float(pos.entry_price)
        else:
            per_share_profit = float(pos.entry_price) - float(current_price)

        total_profit = per_share_profit * abs_qty
        if total_profit < (self.config.rr_trail_start_r * self.config.max_risk_per_trade):
            return

        ratio = total_profit / self.config.max_risk_per_trade if self.config.max_risk_per_trade > 0 else 0.0
        lock_pct = 0.10 + (0.95 - 0.10) * min(max(ratio / 3.0, 0.0), 1.0)
        locked_profit = total_profit * lock_pct
        money_locked_per_share = locked_profit / max(abs_qty, 1)
        atr_floor = float(self._atr_map.get(symbol) or 0.0) * self.config.trail_atr_mult
        trail_distance = max(money_locked_per_share, atr_floor)

        if is_long:
            candidate_sl = round(float(current_price) - trail_distance, 2)
            tightens = candidate_sl > pos.stop_loss
        else:
            candidate_sl = round(float(current_price) + trail_distance, 2)
            tightens = candidate_sl < pos.stop_loss

        if tightens:
            logger.info(
                "[SIMPLIFIED-TRAIL] %s price=%.2f total_pnl=%.2f SL %.2f -> %.2f",
                symbol,
                current_price,
                total_profit,
                pos.stop_loss,
                candidate_sl,
            )
            pos.stop_loss = candidate_sl

    def _attempt_buy_entry(
        self, symbol: str, candle: Candle, record: ReferenceCandleRecord
    ) -> EntrySignal | None:
        if not self._is_entry_window_open(symbol, candle):
            return None

        red_open = float(record.candle.open)
        if float(candle.close) <= red_open:
            return None

        passed, reason = self._passes_atr_entry_filter(symbol, candle, record)
        if not passed:
            logger.info("[SIMPLIFIED-NO-TRADE] %s rejected by entry filter: %s", symbol, reason)
            return None

        atr = float(self._atr_map.get(symbol) or 0.0)
        if atr > 0:
            risk_per_share = max(atr * self.config.atr_sl_mult, self.config.min_risk_per_share)
        else:
            risk_per_share = max(
                float(record.candle.open) - float(record.candle.close),
                self.config.min_risk_per_share,
            )

        qty, _, _, _ = self.calculate_qty(float(candle.close), risk_per_share)
        if qty <= 0:
            return None

        signal = EntrySignal(
            symbol=symbol,
            action=DIRECTION_BUY,
            quantity=qty,
            reference_price=float(candle.close),
            stop_loss=round(float(candle.close) - risk_per_share, 2),
            risk_per_share=float(risk_per_share),
            candle_ts=candle.ts,
            exchange=self.config.exchange,
            product=self.config.product,
            pricetype=self.config.order_pricetype,
        )
        self.pending_entries[symbol] = signal
        return signal

    def _attempt_sell_entry(
        self, symbol: str, candle: Candle, record: ReferenceCandleRecord
    ) -> EntrySignal | None:
        if not self._is_entry_window_open(symbol, candle):
            return None

        green_open = float(record.candle.open)
        if float(candle.close) >= green_open:
            return None

        passed, reason = self._passes_atr_entry_filter(symbol, candle, record)
        if not passed:
            logger.info("[SIMPLIFIED-NO-TRADE-SELL] %s rejected by entry filter: %s", symbol, reason)
            return None

        atr = float(self._atr_map.get(symbol) or 0.0)
        if atr > 0:
            risk_per_share = max(atr * self.config.atr_sl_mult, self.config.min_risk_per_share)
        else:
            risk_per_share = max(
                float(record.candle.close) - float(record.candle.open),
                self.config.min_risk_per_share,
            )

        qty, _, _, _ = self.calculate_qty(float(candle.close), risk_per_share)
        if qty <= 0:
            return None

        signal = EntrySignal(
            symbol=symbol,
            action=DIRECTION_SELL,
            quantity=qty,
            reference_price=float(candle.close),
            stop_loss=round(float(candle.close) + risk_per_share, 2),
            risk_per_share=float(risk_per_share),
            candle_ts=candle.ts,
            exchange=self.config.exchange,
            product=self.config.product,
            pricetype=self.config.order_pricetype,
        )
        self.pending_entries[symbol] = signal
        return signal

    def _is_entry_window_open(self, symbol: str, candle: Candle) -> bool:
        now = self.now_provider()
        if now.time() >= self.config.no_new_openings_time:
            return False

        self._reset_trade_day_if_needed()
        if self.trades_today >= self.config.max_trades_per_day:
            return False

        if self.bought_in_bucket.get(symbol) == candle.ts:
            return False
        if symbol in self.positions or symbol in self.pending_entries:
            return False
        if candle.elapsed_pct < self.config.elapsed_pct_entry:
            return False

        return True

    def _passes_atr_entry_filter(
        self, symbol: str, candle: Candle, record: ReferenceCandleRecord
    ) -> tuple[bool, str]:
        base_vol = int(record.candle.volume or 0)
        if base_vol <= 0:
            recent = self.recent_candles.get(symbol) or []
            if recent:
                base_vol = int(sum(int(c.volume or 0) for c in recent) / len(recent))

        required_vol = int(base_vol * self.config.volume_multiplier)
        if int(candle.volume or 0) < required_vol:
            return False, "low_volume"

        atr = self._atr_map.get(symbol)
        if atr is None:
            return False, "no_atr"

        candle_range = abs(float(candle.high) - float(candle.low))
        if candle_range < (self.config.atr_entry_min_mult * atr):
            return False, "small_range_vs_atr"

        return True, "ok"

    def calculate_qty(self, price: float, risk: float) -> tuple[int, float, int, int]:
        capital = self.config.account_capital * self.config.account_leverage
        qty_by_capital = int(capital // price) if price > 0 else 0
        qty_by_risk = int(self.config.max_risk_per_trade // risk) if risk > 0 else 0
        return min(qty_by_capital, qty_by_risk), capital, qty_by_capital, qty_by_risk

    def _build_exit_signal(self, symbol: str, price: float, reason: str) -> ExitSignal:
        pos = self.positions[symbol]
        # Long position is closed by SELL; short position is closed by BUY.
        action = DIRECTION_SELL if pos.qty > 0 else DIRECTION_BUY
        signal = ExitSignal(
            symbol=symbol,
            action=action,
            quantity=abs(int(pos.qty)),
            reason=reason,
            reference_price=float(price),
            exchange=self.config.exchange,
            product=self.config.product,
            pricetype=self.config.order_pricetype,
        )
        self.pending_exits[symbol] = signal
        return signal

    def _check_global_profit_lock(self) -> list[ExitSignal]:
        if self.global_profit_actioned or not self.positions:
            return []

        total_unrealized = 0.0
        for symbol, pos in self.positions.items():
            price = self.last_prices.get(symbol, pos.entry_price)
            # qty is signed: long => positive, short => negative.
            total_unrealized += (price - pos.entry_price) * pos.qty

        threshold = self.config.global_profit_lock_mult * self.config.max_risk_per_trade
        if total_unrealized < threshold:
            return []

        self.global_profit_actioned = True
        exits = []
        for symbol, pos in list(self.positions.items()):
            price = self.last_prices.get(symbol, pos.entry_price)
            is_long = pos.qty > 0
            per_share_profit = (price - pos.entry_price) if is_long else (pos.entry_price - price)
            if per_share_profit < 0 and symbol not in self.pending_exits:
                exits.append(self._build_exit_signal(symbol, price, "global_profit_lock_loser"))
            elif per_share_profit > 0:
                lock_amount = per_share_profit * self.config.lock_profit_pct
                if is_long:
                    candidate_sl = round(
                        min(pos.entry_price + lock_amount, price - 0.01), 2
                    )
                    if candidate_sl > pos.stop_loss:
                        pos.stop_loss = candidate_sl
                else:
                    candidate_sl = round(
                        max(pos.entry_price - lock_amount, price + 0.01), 2
                    )
                    if candidate_sl < pos.stop_loss:
                        pos.stop_loss = candidate_sl
        return exits

    def _refresh_red_reference(self, symbol: str) -> None:
        candidates = [
            c
            for c in self._after_market_cutoff(self.recent_candles.get(symbol, []))
            if c.is_red() and int(c.volume or 0) > 0
        ]
        if not candidates:
            self.red_candles.pop(symbol, None)
            return

        chosen = min(candidates, key=lambda c: int(c.volume or 0))
        self.red_candles[symbol] = ReferenceCandleRecord(chosen, self.now_provider())

    def _refresh_green_reference(self, symbol: str) -> None:
        eps = 1e-9
        candidates = [
            c
            for c in self._after_market_cutoff(self.recent_candles.get(symbol, []))
            if int(c.volume or 0) > 0
            and (c.close > c.open or math.isclose(c.close, c.open, abs_tol=eps))
        ]
        if not candidates:
            self.green_candles.pop(symbol, None)
            return

        chosen = min(candidates, key=lambda c: int(c.volume or 0))
        self.green_candles[symbol] = ReferenceCandleRecord(chosen, self.now_provider())

    def _update_recent(self, symbol: str, candle: Candle) -> None:
        recent = list(self.recent_candles.get(symbol, []))
        recent = [c for c in recent if self._normalize_bucket(c.ts) != self._normalize_bucket(candle.ts)]
        recent.append(candle)
        recent.sort(key=lambda c: c.ts)
        self.recent_candles[symbol] = recent[-self.config.history_candidate_count :]

    def _update_atr_wilder(self, symbol: str, candle: Candle) -> None:
        high = float(candle.high)
        low = float(candle.low)
        close = float(candle.close)
        prev_close = self._prev_close.get(symbol)
        tr = max(high - low, abs(high - (prev_close if prev_close is not None else close)), abs(low - (prev_close if prev_close is not None else close)))

        dq = self._tr_deques.setdefault(symbol, deque(maxlen=self.config.atr_period))
        dq.append(float(tr))
        prev_atr = self._atr_map.get(symbol)
        if prev_atr is None:
            atr = sum(dq) / float(len(dq))
        else:
            atr = (prev_atr * (self.config.atr_period - 1) + tr) / float(self.config.atr_period)

        self._atr_map[symbol] = float(atr)
        self._prev_close[symbol] = close

    def _after_market_cutoff(self, candles: list[Candle]) -> list[Candle]:
        return [
            c
            for c in candles
            if isinstance(c.ts, dt.datetime)
            and c.ts.replace(tzinfo=None).time() >= self.config.history_market_cutoff
        ]

    def _is_reference_expired(self, record: ReferenceCandleRecord) -> bool:
        red_ts = record.candle.ts.replace(tzinfo=None)
        return (self.now_provider().replace(tzinfo=None) - red_ts).total_seconds() > self.config.reference_candle_expiry_seconds

    def _is_eod_done(self) -> bool:
        return self.eod_done_date == self.now_provider().date()

    def _reset_trade_day_if_needed(self) -> None:
        today = self.now_provider().date()
        if today != self.trades_day:
            self.trades_day = today
            self.trades_today = 0
            self.eod_done_date = None
            self.global_profit_actioned = False

    @staticmethod
    def _normalize_bucket(ts: dt.datetime) -> dt.datetime:
        return FiveMinuteCandleBuilder.bucket(ts.replace(tzinfo=None))
