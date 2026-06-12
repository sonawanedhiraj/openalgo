"""Sector Follow CAP5_VOL strategy service.

Entry rule (15:20 IST eval): sector index >+1% intraday AND stock >+0.5% intraday
AND volume >1x 20d avg. Buy at MARKET ~15:20-15:25. Exit T+1 at 15:25 close MARKET.
Max 5 concurrent positions; tiebreaker = volume ratio descending.

Mode flag (env): SECTOR_FOLLOW_CAP5_VOL_MODE = scaffold | sandbox | live
  scaffold: compute signals, log, NO orders (default)
  sandbox: orders to sandbox.db only
  live: real broker orders

Plan / decisions: strategies/sector_follow_cap5_vol/ (LEARNINGS.md, config_snapshot.json,
sector_map.json, data_coverage.md, strategy_id_design.md).

Testability: all I/O (market-data metrics, order placement, notifications, trade
journal) is injected with production defaults, mirroring the policy-injection
pattern in services/scanner_ws_watchdog.py — unit tests drive the pure decision
logic without a live broker or DuckDB.
"""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

_STRATEGY_DIR = Path(__file__).resolve().parents[1] / "strategies" / "sector_follow_cap5_vol"
_DEFAULT_CONFIG_PATH = _STRATEGY_DIR / "config_snapshot.json"
_DEFAULT_SECTOR_MAP_PATH = _STRATEGY_DIR / "sector_map.json"
# Day-N EOD markdown reports (mirror of the 15:30 IST Telegram summary). Path is
# hardcoded (no env var); the instance attribute below lets tests redirect it.
_EOD_REPORTS_DIR = _STRATEGY_DIR / "eod_reports"

VALID_MODES = ("scaffold", "sandbox", "live")


# --------------------------------------------------------------------------- #
# Config + universe loaders
# --------------------------------------------------------------------------- #
@dataclass
class SectorFollowConfig:
    """Strategy configuration. Mirrors config_snapshot.json (scaffold defaults)."""

    capital_inr: float = 250000.0
    max_position_inr: float = 50000.0
    max_concurrent_positions: int = 5
    gate_sector_pct: float = 1.0  # sector index intraday return gate, in percent
    gate_stock_pct: float = 0.5  # stock intraday return gate, in percent
    gate_vol_mult: float = 1.0  # volume / 20d-avg gate
    daily_loss_kill_pct: float = 3.0
    tiebreaker: str = "volume_ratio_desc"
    cost_pct_round_trip: float = 0.0857
    vol_avg_lookback_days: int = 20
    broker: str = "zerodha"
    exchange: str = "NSE"
    product: str = "CNC"
    universe: list[str] = field(default_factory=list)
    strategy_id: int | None = None

    @property
    def gate_sector_ret(self) -> float:
        """Sector gate as a fraction (1.0% -> 0.01)."""
        return self.gate_sector_pct / 100.0

    @property
    def gate_stock_ret(self) -> float:
        """Stock gate as a fraction (0.5% -> 0.005)."""
        return self.gate_stock_pct / 100.0


def load_config(path: str | Path = _DEFAULT_CONFIG_PATH) -> SectorFollowConfig:
    """Load config_snapshot.json into a SectorFollowConfig (missing keys -> defaults)."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return SectorFollowConfig(
        capital_inr=float(raw.get("capital_inr", 250000.0)),
        max_position_inr=float(raw.get("max_position_inr", 50000.0)),
        max_concurrent_positions=int(raw.get("max_concurrent_positions", 5)),
        gate_sector_pct=float(raw.get("gate_sector_pct", 1.0)),
        gate_stock_pct=float(raw.get("gate_stock_pct", 0.5)),
        gate_vol_mult=float(raw.get("gate_vol_mult", 1.0)),
        daily_loss_kill_pct=float(raw.get("daily_loss_kill_pct", 3.0)),
        tiebreaker=str(raw.get("tiebreaker", "volume_ratio_desc")),
        cost_pct_round_trip=float(raw.get("cost_pct_round_trip", 0.0857)),
        vol_avg_lookback_days=int(raw.get("vol_avg_lookback_days", 20)),
        broker=str(raw.get("broker", "zerodha")),
        exchange=str(raw.get("exchange", "NSE")),
        product=str(raw.get("product", "CNC")),
        universe=list(raw.get("universe", [])),
        strategy_id=raw.get("strategy_id"),
    )


def load_sector_map(path: str | Path = _DEFAULT_SECTOR_MAP_PATH) -> dict[str, str]:
    """Load sector_map.json -> {stock_symbol: index_symbol} (locked-static-30)."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {sym: entry["index"] for sym, entry in raw.get("map", {}).items()}


# --------------------------------------------------------------------------- #
# Pure decision logic
# --------------------------------------------------------------------------- #
def passes_gates(metrics: dict, config: SectorFollowConfig) -> bool:
    """True iff all three entry gates are met.

    metrics: {sector_ret, stock_ret, vol_ratio} as fractions/ratios. A missing or
    None value fails closed (no entry).
    """
    sector_ret = metrics.get("sector_ret")
    stock_ret = metrics.get("stock_ret")
    vol_ratio = metrics.get("vol_ratio")
    if sector_ret is None or stock_ret is None or vol_ratio is None:
        return False
    return (
        sector_ret > config.gate_sector_ret
        and stock_ret > config.gate_stock_ret
        and vol_ratio > config.gate_vol_mult
    )


def select_entries(
    candidates: list[dict], open_positions: set[str], max_concurrent: int
) -> list[dict]:
    """Pick entries from gate-passing candidates.

    - Drop any symbol already open.
    - Sort by vol_ratio descending (tiebreaker), then symbol ascending for stability.
    - Take at most (max_concurrent - len(open_positions)) names.
    """
    slots = max_concurrent - len(open_positions)
    if slots <= 0:
        return []
    fresh = [c for c in candidates if c["symbol"] not in open_positions]
    fresh.sort(key=lambda c: (-c.get("vol_ratio", 0.0), c["symbol"]))
    return fresh[:slots]


def compute_qty(max_position_inr: float, price: float) -> int:
    """Integer shares: floor(max_position_inr / price). Backtest used 1-unit equal
    weight; real money needs whole shares. Non-positive price -> 0 (skip)."""
    if price is None or price <= 0:
        return 0
    return int(math.floor(max_position_inr / price))


# --------------------------------------------------------------------------- #
# Production market-data metrics provider (read-only on historify.duckdb)
# --------------------------------------------------------------------------- #
def _ist_date(epoch: float) -> date:
    return datetime.fromtimestamp(epoch, _IST).date()


def _series_metrics(bars: list[tuple], as_of_epoch: float, as_of_date: date, lookback: int):
    """Compute intraday return + volume ratio from 1m (timestamp, close, volume) bars.

    bars: ascending by timestamp. Returns (today_close, ret, vol_ratio) or
    (None, None, None) when there is insufficient history.
    """
    by_day: dict[date, list[tuple]] = {}
    for ts, close, vol in bars:
        if ts > as_of_epoch:
            continue
        d = _ist_date(ts)
        by_day.setdefault(d, []).append((ts, float(close), float(vol or 0.0)))

    today = by_day.get(as_of_date)
    prior_days = sorted(d for d in by_day if d < as_of_date)
    if not today or not prior_days:
        return None, None, None

    today_close = today[-1][1]
    prior_close = by_day[prior_days[-1]][-1][1]
    if prior_close <= 0:
        return None, None, None
    ret = today_close / prior_close - 1.0

    today_vol = sum(v for _, _, v in today)
    recent = prior_days[-lookback:]
    daily_vols = [sum(v for _, _, v in by_day[d]) for d in recent]
    avg_vol = sum(daily_vols) / len(daily_vols) if daily_vols else 0.0
    vol_ratio = (today_vol / avg_vol) if avg_vol > 0 else None
    return today_close, ret, vol_ratio


def duckdb_metrics_provider(
    as_of: datetime,
    universe: list[str],
    sector_map: dict[str, str],
    config: SectorFollowConfig,
    db_path: str = "db/historify.duckdb",
) -> dict[str, dict]:
    """Read-only intraday metrics for every universe stock from historify.duckdb.

    Derives daily from 1m (stored daily for stocks is the sparse 63-bar set —
    see strategies/sector_follow_cap5_vol/data_coverage.md). Sector return uses
    the mapped index's 1m; if an index has no 1m bars, sector_ret is None and the
    stock fails the gate (fail-closed). Returns
    {symbol: {sector_ret, stock_ret, vol_ratio, current_price}}.
    """
    import duckdb

    as_of_epoch = as_of.timestamp()
    as_of_date = as_of.astimezone(_IST).date()
    window_start = as_of_epoch - (config.vol_avg_lookback_days + 15) * 86400

    index_syms = sorted({sector_map.get(s, "NIFTY") for s in universe})
    all_syms = sorted(set(universe) | set(index_syms))

    raw: dict[str, list[tuple]] = {s: [] for s in all_syms}
    con = duckdb.connect(db_path, read_only=True)
    try:
        placeholders = ", ".join(["?"] * len(all_syms))
        rows = con.execute(
            f"""
            SELECT symbol, timestamp, close, volume
            FROM market_data
            WHERE symbol IN ({placeholders})
              AND interval = '1m'
              AND timestamp >= ?
            ORDER BY symbol, timestamp ASC
            """,
            [*all_syms, window_start],
        ).fetchall()
    finally:
        con.close()

    for symbol, ts, close, vol in rows:
        if symbol in raw:
            raw[symbol].append((ts, close, vol))

    lookback = config.vol_avg_lookback_days
    index_metrics: dict[str, float | None] = {}
    for idx in index_syms:
        _, idx_ret, _ = _series_metrics(raw.get(idx, []), as_of_epoch, as_of_date, lookback)
        index_metrics[idx] = idx_ret

    out: dict[str, dict] = {}
    for sym in universe:
        price, stock_ret, vol_ratio = _series_metrics(
            raw.get(sym, []), as_of_epoch, as_of_date, lookback
        )
        idx = sector_map.get(sym, "NIFTY")
        out[sym] = {
            "sector_ret": index_metrics.get(idx),
            "stock_ret": stock_ret,
            "vol_ratio": vol_ratio,
            "current_price": price,
        }
    return out


# --------------------------------------------------------------------------- #
# Production order placer (mode-aware) — injected default
# --------------------------------------------------------------------------- #
def production_order_placer(mode: str, order: dict) -> dict:
    """Route an order according to mode. Returns {status, orderid, ...}.

    scaffold: never reaches here (place_entry/exit short-circuit). sandbox/live
    both go through services.place_order_service.place_order — sandbox relies on
    the platform's analyze/daily-intent dispatch to land in sandbox.db, live
    places a real broker order. Kept thin and lazily-imported so importing this
    module never pulls the order stack.
    """
    from database.auth_db import get_first_available_api_key
    from services.place_order_service import place_order

    api_key = get_first_available_api_key()
    if not api_key:
        return {"status": "error", "message": "no api key available"}
    payload = {
        "apikey": api_key,
        "strategy": "sector_follow_cap5_vol",
        "symbol": order["symbol"],
        "exchange": order["exchange"],
        "action": order["action"],
        "product": order["product"],
        "pricetype": "MARKET",
        "quantity": str(order["quantity"]),
    }
    success, response, _ = place_order(payload, api_key=api_key)
    response = dict(response or {})
    response.setdefault("status", "success" if success else "error")
    return response


def production_price_fetcher(symbol: str, exchange: str) -> float | None:
    """Current LTP for one symbol via the broker quote API. None on any failure.

    Lazily imported so importing this module never pulls the quote/auth stack.
    Used by ``SectorFollowService._compute_mtm`` for live MTM in the status
    endpoint; never on the order path.
    """
    from database.auth_db import get_first_available_api_key
    from services.quotes_service import get_quotes

    api_key = get_first_available_api_key()
    if not api_key:
        return None
    success, resp, _ = get_quotes(symbol, exchange, api_key=api_key)
    if not success:
        return None
    data = (resp or {}).get("data") or {}
    ltp = data.get("ltp") or data.get("last_price") or data.get("close")
    return float(ltp) if ltp else None


def production_intent_resolver():
    """Resolve the unified {mode, intent} decision for this strategy.

    Thin wrapper around ``services.mode_service.resolve_strategy_mode`` so the
    service can inject a fake in tests. Lazily imported so importing this module
    never pulls the mode/settings stack."""
    from services.mode_service import resolve_strategy_mode

    return resolve_strategy_mode("sector_follow_cap5_vol")


def telegram_notifier(message: str) -> None:
    """Best-effort Telegram broadcast; silent when the bot is disabled/unconfigured."""
    try:
        from services.telegram_bot_service import telegram_bot_service as svc

        if svc is None or not getattr(svc, "is_running", False):
            return
        # Run the async broadcast on a real thread (eventlet-safe — see
        # telegram_bot_service._render_plotly_png for the pattern).
        import asyncio

        def _run():
            try:
                asyncio.run(svc.broadcast_message(message))
            except Exception:
                logger.debug("telegram broadcast skipped", exc_info=True)

        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        logger.debug("telegram notifier unavailable", exc_info=True)


# --------------------------------------------------------------------------- #
# Data-freshness gate — injected default
# --------------------------------------------------------------------------- #
def data_freshness_enabled() -> bool:
    """``DATA_FRESHNESS_VALIDATION_ENABLED`` env flag (default true)."""
    return os.getenv("DATA_FRESHNESS_VALIDATION_ENABLED", "true").lower() == "true"


def production_data_health_checker(
    strategy_name: str, date: str | None = None, index_only: bool = False
):
    """Production freshness checker injected into the live singleton.

    Thin wrapper over ``services.data_freshness_service.check_strategy_data_ready``.
    Fully defensive: any infrastructure failure (DuckDB unreadable, import error)
    fails OPEN — returns ``(True, {})`` — because a read error here must not block
    all trading; the strategy's own metrics provider already fails closed on a
    bad feed, and the daily 16:30 job is the durable safety net.
    """
    try:
        from services.data_freshness_service import check_strategy_data_ready

        return check_strategy_data_ready(strategy_name, date, index_only=index_only)
    except Exception:
        logger.exception("data-health checker failed (failing open)")
        return True, {}


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
@dataclass
class PaperPosition:
    """An open position tracked in-memory across all modes."""

    symbol: str
    quantity: int
    entry_price: float
    entry_date: str
    vol_ratio: float
    order_id: str | None = None


class SectorFollowService:
    """Sector-follow CAP5_VOL evaluator + scheduler glue.

    All external effects are injected (with production defaults) so the decision
    logic is unit-testable without a broker or DuckDB.
    """

    def __init__(
        self,
        app=None,
        scheduler=None,
        *,
        config: SectorFollowConfig | None = None,
        sector_map: dict[str, str] | None = None,
        mode: str | None = None,
        metrics_provider: Callable[..., dict[str, dict]] | None = None,
        order_placer: Callable[[str, dict], dict] | None = None,
        price_fetcher: Callable[[str, str], float | None] | None = None,
        notifier: Callable[[str], None] | None = None,
        trade_recorder: Callable[..., object] | None = None,
        open_positions_loader: Callable[[], set[str]] | None = None,
        now: Callable[[], datetime] | None = None,
        intent_resolver: Callable[[], object] | None = None,
        data_health_checker: Callable[..., tuple] | None = None,
    ):
        self.app = app
        self.scheduler = scheduler
        self.config = config if config is not None else load_config()
        self.sector_map = sector_map if sector_map is not None else load_sector_map()
        self.mode = (mode or os.getenv("SECTOR_FOLLOW_CAP5_VOL_MODE", "scaffold")).lower()
        if self.mode not in VALID_MODES:
            logger.warning("Unknown SECTOR_FOLLOW_CAP5_VOL_MODE=%s — forcing scaffold", self.mode)
            self.mode = "scaffold"

        self._metrics_provider = metrics_provider or duckdb_metrics_provider
        self._order_placer = order_placer or production_order_placer
        self._price_fetcher = price_fetcher or production_price_fetcher
        self._notify = notifier or telegram_notifier
        self._record_trade = trade_recorder or self._default_trade_recorder
        self._open_positions_loader = open_positions_loader
        self._intent_resolver = intent_resolver or production_intent_resolver
        # Data-freshness gate. Left None in unit tests (gate skipped, hermetic);
        # the live singleton injects ``production_data_health_checker`` so the gate
        # is active in production. New tests inject a stub to drive abort/allow.
        self._data_health_checker = data_health_checker
        self._now = now or (lambda: datetime.now(_IST))
        # Where the Day-N EOD markdown report is written. Hardcoded default;
        # tests override this attribute to point at a tmp dir.
        self.eod_reports_dir = _EOD_REPORTS_DIR

        # Mutable runtime state.
        self.paper_book: dict[str, PaperPosition] = {}
        self.kill_switch_active = False
        self.kill_switch_reason: str | None = None
        self.manual_pause = False  # operator-set; halts new entries, exits still run
        self.daily_pnl = 0.0
        # Intraday journals for observability + EOD summary (reset at 09:00 IST).
        self.today_entries: list[dict] = []
        self.today_exits: list[dict] = []
        self.strategy_id: int | None = self.config.strategy_id

    # ----- strategy DB seeding ------------------------------------------- #
    def seed_strategy(self, user_id: str = "internal") -> int | None:
        """Idempotently register the strategy in the `strategies` table; return its id.

        Looks up by the stable natural key name='sector_follow_cap5_vol' and only
        creates if absent, so restarts reuse the same auto-increment id.
        """
        try:
            from database.sector_follow_db import init_db as _init_journal
            from database.strategy_db import create_strategy, get_all_strategies

            _init_journal()  # ensure the trade-journal table exists

            for s in get_all_strategies():
                if s.name == "sector_follow_cap5_vol":
                    self.strategy_id = s.id
                    return s.id

            strat = create_strategy(
                name="sector_follow_cap5_vol",
                webhook_id=str(uuid.uuid4()),
                user_id=user_id,
                is_intraday=False,  # T+1 hold, not same-day square-off
                trading_mode="LONG",
                start_time="15:20",
                end_time="15:25",
                squareoff_time="15:25",
                platform="internal",
            )
            if strat is not None:
                # Keep scaffold strategies inactive until the operator flips mode.
                self.strategy_id = strat.id
                return strat.id
        except Exception as e:
            logger.exception(f"Failed to seed sector_follow strategy row: {e}")
        return None

    def _default_trade_recorder(self, **kwargs):
        from database.sector_follow_db import record_trade

        return record_trade(strategy_id=self.strategy_id, mode=self.mode, **kwargs)

    # ----- open-position view -------------------------------------------- #
    def open_position_symbols(self) -> set[str]:
        if self._open_positions_loader is not None:
            return set(self._open_positions_loader())
        return set(self.paper_book.keys())

    # ----- evaluation ---------------------------------------------------- #
    def evaluate_candidates(self, as_of: datetime | None = None) -> list[dict]:
        """Return gate-passing candidates at as_of (default: now IST)."""
        as_of = as_of or self._now()
        metrics = self._metrics_provider(as_of, self.config.universe, self.sector_map, self.config)
        candidates: list[dict] = []
        for symbol, m in metrics.items():
            if passes_gates(m, self.config):
                candidates.append(
                    {
                        "symbol": symbol,
                        "vol_ratio": m["vol_ratio"],
                        "stock_ret": m["stock_ret"],
                        "sector_ret": m["sector_ret"],
                        "current_price": m["current_price"],
                    }
                )
        logger.info(
            "sector_follow eval @ %s: %d/%d candidates passed gates",
            as_of.isoformat(),
            len(candidates),
            len(self.config.universe),
        )
        return candidates

    # ----- kill switch --------------------------------------------------- #
    def update_daily_pnl(self, realized_today: float, open_mtm: float) -> bool:
        """Recompute daily P&L and the kill switch. Returns kill_switch_active.

        Fires when daily_pnl / capital < -kill_pct/100. Once tripped it stays
        tripped for the session (new entries blocked; existing positions hold to
        their scheduled T+1 exit).
        """
        self.daily_pnl = realized_today + open_mtm
        threshold = -(self.config.daily_loss_kill_pct / 100.0) * self.config.capital_inr
        if not self.kill_switch_active and self.daily_pnl < threshold:
            self.kill_switch_active = True
            self.kill_switch_reason = (
                f"daily P&L ₹{self.daily_pnl:,.0f} breached "
                f"{self.config.daily_loss_kill_pct}% of capital"
            )
            logger.error(
                "sector_follow KILL SWITCH fired: daily_pnl=%.0f < %.0f (%.1f%% of capital)",
                self.daily_pnl,
                threshold,
                self.config.daily_loss_kill_pct,
            )
            self._notify(
                f"🛑 sector_follow_cap5_vol kill switch fired — daily P&L "
                f"₹{self.daily_pnl:,.0f} breached {self.config.daily_loss_kill_pct}% of capital. "
                "New entries blocked; open positions hold to scheduled exit."
            )
            # Durable mirror: hold entries via the engine's runtime-override gate
            # (same-day expiry; the 09:00 reset clears the in-memory flag).
            self._set_runtime_override(
                "kill_switch",
                self._end_of_today_ist(),
                self.kill_switch_reason or "daily loss kill",
            )
        return self.kill_switch_active

    def reset_daily_state(self) -> None:
        """09:00 IST reset: clear kill switch + daily P&L and the intraday journals.

        Does NOT clear ``manual_pause`` — an operator pause persists across the
        daily reset until the operator explicitly resumes. Existing open positions
        in paper_book survive (they hold to their scheduled T+1 exit)."""
        self.kill_switch_active = False
        self.kill_switch_reason = None
        self.daily_pnl = 0.0
        self.today_entries = []
        self.today_exits = []
        logger.info("sector_follow daily state reset (kill switch cleared, pnl=0)")

    # ----- runtime-override durability (mode-only safety guards) ---------- #
    def _utc_naive(self, ist_dt) -> datetime:
        """IST-aware datetime → naive UTC (strategy_runtime_override stores and
        compares naive UTC)."""
        # NB: `timezone` here is the imported class, not the datetime module — so
        # `timezone.utc` is correct and `datetime.UTC` would be an AttributeError.
        return ist_dt.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: UP017

    def _end_of_today_ist(self):
        """Today 23:59 IST — same-day expiry for an intraday hold (the in-memory
        flag still persists this session; this just makes the hold durable +
        visible to the engine gate and /status until the daily reset)."""
        return self._now().replace(hour=23, minute=59, second=0, microsecond=0)

    def _set_runtime_override(self, override_type: str, expires_ist, reason: str) -> None:
        """Durably record a safety hold in ``strategy_runtime_override`` so the
        engine's job-entry gate (and /status) see it across restarts. Fail-safe —
        a DB error never breaks the guard; the in-memory flag still blocks this
        session."""
        try:
            from database.strategy_runtime_override_db import set_override

            set_override(
                "sector_follow_cap5_vol",
                override_type,
                self._utc_naive(expires_ist),
                reason=reason,
                set_by="sector_follow",
            )
        except Exception:
            logger.exception("sector_follow: failed to write %s runtime override", override_type)

    def _clear_runtime_override(self) -> None:
        try:
            from database.strategy_runtime_override_db import clear_override

            clear_override("sector_follow_cap5_vol")
        except Exception:
            logger.exception("sector_follow: failed to clear runtime overrides")

    # ----- operator manual controls -------------------------------------- #
    def pause(self) -> dict:
        """Operator pause: halt new entries. Open positions hold to T+1 exit."""
        self.manual_pause = True
        # Durable mirror so the engine job-entry gate honors it across a restart.
        self._set_runtime_override("pause", self._end_of_today_ist(), "operator manual pause")
        logger.warning("sector_follow MANUALLY PAUSED — new entries halted (exits still run)")
        return {"status": "success", "manual_pause": True}

    def resume(self) -> dict:
        """Operator resume: clear both manual pause and the kill switch."""
        self.manual_pause = False
        self.kill_switch_active = False
        self.kill_switch_reason = None
        self._clear_runtime_override()
        logger.info("sector_follow RESUMED — manual pause + kill switch cleared")
        return {"status": "success", "manual_pause": False, "kill_switch_active": False}

    # ----- order placement (mode-aware) ---------------------------------- #
    def place_entry(self, candidate: dict, entry_date: str | None = None) -> dict | None:
        """Place (or paper-record) a single entry. Honors the kill switch.

        scaffold: log + in-memory paper book only, no order.
        sandbox/live: route via the injected order placer.
        All modes: write a trade-journal row.
        """
        if self.kill_switch_active:
            logger.info("sector_follow entry skipped (kill switch active): %s", candidate["symbol"])
            return None
        if self.manual_pause:
            logger.info("sector_follow entry skipped (manual pause): %s", candidate["symbol"])
            return None

        symbol = candidate["symbol"]
        price = candidate["current_price"]
        qty = compute_qty(self.config.max_position_inr, price)
        if qty <= 0:
            logger.warning("sector_follow entry skipped (qty=0): %s @ %s", symbol, price)
            return None

        entry_date = entry_date or self._now().date().isoformat()
        order_id = None
        status = "scaffold"
        error_message = None
        if self.mode == "scaffold":
            logger.info(
                "[scaffold] sector_follow ENTRY %s qty=%d @ %.2f (vol_ratio=%.2f) — NO ORDER",
                symbol,
                qty,
                price,
                candidate.get("vol_ratio", 0.0),
            )
        else:
            # Order placement can both throw AND return an error response. Treat
            # either as a failed attempt: journal it (so the operator sees what was
            # tried) but DO NOT create a phantom open position. An exception here
            # must not abort the rest of the entry batch either.
            try:
                resp = self._order_placer(
                    self.mode,
                    {
                        "symbol": symbol,
                        "exchange": self.config.exchange,
                        "action": "BUY",
                        "product": self.config.product,
                        "quantity": qty,
                    },
                )
            except Exception as e:
                logger.exception("sector_follow ENTRY placement raised: %s", symbol)
                resp = {"status": "error", "message": str(e)}
                status = "exception"
            resp = resp or {}
            order_id = resp.get("orderid")
            if status != "exception":
                status = (
                    "placed" if str(resp.get("status", "")).lower() == "success" else "rejected"
                )
            if status == "placed":
                logger.info(
                    "[%s] sector_follow ENTRY %s qty=%d @ %.2f order_id=%s",
                    self.mode,
                    symbol,
                    qty,
                    price,
                    order_id,
                )
            else:
                error_message = str(
                    resp.get("message") or resp.get("status") or "order placement failed"
                )[:255]
                logger.error(
                    "[%s] sector_follow ENTRY %s %s qty=%d @ %.2f — %s",
                    self.mode,
                    status.upper(),
                    symbol,
                    qty,
                    price,
                    error_message,
                )
                # Journal the failed attempt; no paper_book / today_entries row —
                # nothing actually opened.
                self._record_trade(
                    side="BUY",
                    symbol=symbol,
                    quantity=qty,
                    price=price,
                    entry_date=entry_date,
                    exchange=self.config.exchange,
                    product=self.config.product,
                    vol_ratio=candidate.get("vol_ratio"),
                    stock_ret=candidate.get("stock_ret"),
                    sector_ret=candidate.get("sector_ret"),
                    order_id=None,
                    status=status,
                    error_message=error_message,
                )
                return None

        self.paper_book[symbol] = PaperPosition(
            symbol=symbol,
            quantity=qty,
            entry_price=price,
            entry_date=entry_date,
            vol_ratio=candidate.get("vol_ratio", 0.0),
            order_id=order_id,
        )
        self._record_trade(
            side="BUY",
            symbol=symbol,
            quantity=qty,
            price=price,
            entry_date=entry_date,
            exchange=self.config.exchange,
            product=self.config.product,
            vol_ratio=candidate.get("vol_ratio"),
            stock_ret=candidate.get("stock_ret"),
            sector_ret=candidate.get("sector_ret"),
            order_id=order_id,
            status=status,
        )
        self.today_entries.append(
            {
                "symbol": symbol,
                "entry_time": self._now().isoformat(),
                "entry_price": price,
                "qty": qty,
                "vol_ratio": candidate.get("vol_ratio"),
                # Sector context for the EOD report's sector breakdown (observability
                # only — not read by any trading-decision path).
                "sector": self.sector_map.get(symbol, "NIFTY"),
                "sector_ret": candidate.get("sector_ret"),
                "stock_ret": candidate.get("stock_ret"),
            }
        )
        return {"symbol": symbol, "quantity": qty, "price": price, "order_id": order_id}

    def place_exit(self, position: PaperPosition, price: float | None = None) -> dict | None:
        """Square off one position (mode-aware). Exits are NOT blocked by the kill
        switch — open positions always run to their scheduled T+1 exit."""
        symbol = position.symbol
        exit_price = price if price is not None else position.entry_price
        order_id = None
        status = "scaffold"
        error_message = None
        if self.mode == "scaffold":
            logger.info(
                "[scaffold] sector_follow EXIT %s qty=%d @ %.2f — NO ORDER",
                symbol,
                position.quantity,
                exit_price,
            )
        else:
            # As with entries, a thrown or error-response placement must not abort
            # the rest of the exit batch and must still be journaled with its status.
            try:
                resp = self._order_placer(
                    self.mode,
                    {
                        "symbol": symbol,
                        "exchange": self.config.exchange,
                        "action": "SELL",
                        "product": self.config.product,
                        "quantity": position.quantity,
                    },
                )
            except Exception as e:
                logger.exception("sector_follow EXIT placement raised: %s", symbol)
                resp = {"status": "error", "message": str(e)}
                status = "exception"
            resp = resp or {}
            order_id = resp.get("orderid")
            if status != "exception":
                status = (
                    "placed" if str(resp.get("status", "")).lower() == "success" else "rejected"
                )
            if status == "placed":
                logger.info(
                    "[%s] sector_follow EXIT %s qty=%d @ %.2f order_id=%s",
                    self.mode,
                    symbol,
                    position.quantity,
                    exit_price,
                    order_id,
                )
            else:
                error_message = str(
                    resp.get("message") or resp.get("status") or "order placement failed"
                )[:255]
                logger.error(
                    "[%s] sector_follow EXIT %s %s qty=%d @ %.2f — %s",
                    self.mode,
                    status.upper(),
                    symbol,
                    position.quantity,
                    exit_price,
                    error_message,
                )

        self._record_trade(
            side="SELL",
            symbol=symbol,
            quantity=position.quantity,
            price=exit_price,
            entry_date=position.entry_date,
            exchange=self.config.exchange,
            product=self.config.product,
            order_id=order_id,
            status=status,
            error_message=error_message,
            note="t+1_exit",
        )
        pnl_pct = (exit_price / position.entry_price - 1.0) * 100.0 if position.entry_price else 0.0
        pnl_net = (exit_price - position.entry_price) * position.quantity
        self.today_exits.append(
            {
                "symbol": symbol,
                "exit_time": self._now().isoformat(),
                "exit_price": exit_price,
                "entry_date": position.entry_date,
                "qty": position.quantity,
                "pnl_pct": pnl_pct,
                "pnl_net": pnl_net,
            }
        )
        self.paper_book.pop(symbol, None)
        return {"symbol": symbol, "quantity": position.quantity, "order_id": order_id}

    # ----- unified daily intent (mode + intent gate) --------------------- #
    def _resolve_decision(self):
        """Resolve today's unified {mode, intent, daily_capital_cap, source}.

        Fail-open: any resolver error yields an env-default ``run`` decision so a
        scheduled job is never killed by the lookup."""
        try:
            return self._intent_resolver()
        except Exception:
            logger.debug("sector_follow intent resolve failed; defaulting to run", exc_info=True)
            from services.mode_service import EffectiveDecision

            return EffectiveDecision(
                mode="sandbox", intent="run", daily_capital_cap=None, source="default"
            )

    def _entry_held_by_override(self) -> bool:
        """Mode-only: True iff a non-expired ``pause``/``kill_switch`` row in
        ``strategy_runtime_override`` is holding new entries for this strategy.
        Fail-open on a read error (never kill a job on a lookup failure)."""
        try:
            from database.strategy_runtime_override_db import is_entry_blocked

            blocked, ov = is_entry_blocked("sector_follow_cap5_vol")
            if blocked and ov:
                logger.info(
                    "sector_follow entry held by %s (reason=%s, expires=%s)",
                    ov.get("override_type"),
                    ov.get("reason"),
                    ov.get("expires_at"),
                )
            return blocked
        except Exception:
            logger.debug(
                "sector_follow runtime-override resolve failed; not blocking", exc_info=True
            )
            return False

    def _apply_mode_override(self, decision) -> None:
        """Mode-only: honor the persistent ``strategy_mode`` row by mapping its
        mode onto the service's native routing mode. Env/default sources leave
        ``self.mode`` as the env value (so with no row the strategy keeps its
        scaffold/no-orders default — deploy is a no-op until the operator sets a
        ``strategy_mode`` row)."""
        if getattr(decision, "source", None) != "strategy_mode":
            return
        mapped = {"sandbox": "sandbox", "live": "live"}.get(decision.mode)
        if mapped and mapped != self.mode:
            logger.warning(
                "sector_follow mode override %s -> %s (strategy_mode row)", self.mode, mapped
            )
            self.mode = mapped

    def _effective_max_concurrent(self, decision) -> int:
        """Apply an optional ``daily_capital_cap`` to the position-slot count.

        With a cap set, the max concurrent positions is the lesser of the config
        default and ``floor(cap / max_position_inr)``. NULL cap = config default."""
        base = self.config.max_concurrent_positions
        cap = getattr(decision, "daily_capital_cap", None)
        if cap is None or self.config.max_position_inr <= 0:
            return base
        slots = int(cap // self.config.max_position_inr)
        return max(0, min(base, slots))

    # ----- scheduled job bodies ------------------------------------------ #
    def run_entry(self) -> list[dict]:
        """15:20 IST: evaluate, select, place entries (subject to intent gate,
        kill switch + caps)."""
        decision = self._resolve_decision()
        # Mode-only safety gate: an active runtime override (pause/kill_switch,
        # set by automated guards — data-health auto-pause, daily kill-switch, or
        # the /api/pause emergency override) holds new entries. Exits are never
        # blocked. Fail-open on a read error.
        if self._entry_held_by_override():
            return []
        # Data-freshness gate: a stale feed (the 2026-05-29 index-backfill gap)
        # would silently produce fail-closed or wrong signals. Abort entries —
        # both index AND stock feeds must be current.
        if not self._data_is_fresh_for_entry():
            return []
        self._apply_mode_override(decision)
        candidates = self.evaluate_candidates()
        open_syms = self.open_position_symbols()
        max_concurrent = self._effective_max_concurrent(decision)
        entries = select_entries(candidates, open_syms, max_concurrent)
        placed = [r for r in (self.place_entry(c) for c in entries) if r]
        logger.info("sector_follow entry job placed %d order(s) [mode=%s]", len(placed), self.mode)
        if placed:
            self._notify(
                f"📈 sector_follow_cap5_vol [{self.mode}] entries: "
                + ", ".join(f"{p['symbol']}x{p['quantity']}" for p in placed)
            )
        return placed

    def run_exit(self) -> list[dict]:
        """15:25 IST: square off everything opened on a prior trading day (T+1).

        Mode-only: exits are NEVER gated. Runtime overrides hold entries only —
        a held T+1 position must always be allowed to square off."""
        decision = self._resolve_decision()
        # Exits only need the index feed current (stocks supply last_close, and
        # the square-off price comes from the broker). Warn on staleness but NEVER
        # block — leaving a T+1 position open is riskier than a stale-feed exit.
        self._warn_if_stale_for_exit()
        self._apply_mode_override(decision)
        today = self._now().date().isoformat()
        to_exit = [p for p in list(self.paper_book.values()) if p.entry_date != today]
        exited = [r for r in (self.place_exit(p) for p in to_exit) if r]
        logger.info("sector_follow exit job squared off %d position(s)", len(exited))
        if exited:
            self._notify(
                f"📉 sector_follow_cap5_vol [{self.mode}] T+1 exits: "
                + ", ".join(p["symbol"] for p in exited)
            )
        return exited

    # ----- data-freshness gate ------------------------------------------- #
    def _data_is_fresh_for_entry(self) -> bool:
        """True iff the index+stock feeds are fresh enough to place entries.

        Skips (returns True) when no checker is injected (unit tests) or the
        feature flag is off. On staleness: logs, alerts, returns False so
        ``run_entry`` aborts before evaluating candidates."""
        if self._data_health_checker is None or not data_freshness_enabled():
            return True
        today = self._now().date().isoformat()
        ok, details = self._data_health_checker("sector_follow_cap5_vol", today)
        if ok:
            return True
        stale = sorted(s for s, d in details.items() if not d.get("ok", True))
        logger.error("sector_follow ENTRY ABORTED — stale data: %s", stale)
        try:
            self._notify(
                f"🚫 sector_follow_cap5_vol ENTRY ABORTED — stale data ({today}): "
                + ", ".join(stale)
            )
        except Exception:
            logger.exception("sector_follow stale-entry alert failed")
        return False

    def _warn_if_stale_for_exit(self) -> None:
        """Non-blocking index-only freshness warning for the exit job."""
        if self._data_health_checker is None or not data_freshness_enabled():
            return
        today = self._now().date().isoformat()
        try:
            ok, details = self._data_health_checker(
                "sector_follow_cap5_vol", today, index_only=True
            )
        except Exception:
            logger.exception("sector_follow exit freshness check raised (ignored)")
            return
        if not ok:
            stale = sorted(s for s, d in details.items() if not d.get("ok", True))
            logger.warning("sector_follow exits proceeding despite stale index data: %s", stale)

    def run_data_health_check(self) -> tuple[bool, dict]:
        """16:30 IST: validate the feed, persist the verdict, alert + auto-pause.

        Runs after the 16:05 backfill should have landed today's bars. On a stale
        feed it (1) Telegram-alerts the operator, (2) auto-pauses tomorrow's intent
        so the 15:20 run won't fire against bad data, and (3) records the verdict in
        ``data_health_check``. Feature-flagged; fail-open on infra error (does not
        auto-pause on our own bug)."""
        if not data_freshness_enabled():
            logger.debug("data-health check skipped (DATA_FRESHNESS_VALIDATION_ENABLED!=true)")
            return True, {}
        from services.data_freshness_service import (
            check_strategy_data_ready,
            format_freshness_report,
        )

        strat = "sector_follow_cap5_vol"
        today = self._now().date().isoformat()
        try:
            ok, details = check_strategy_data_ready(strat, today)
        except Exception as e:
            logger.exception("sector_follow data-health check failed: %s", e)
            return True, {}

        stale = sorted(s for s, d in details.items() if not d.get("ok", True))
        alert_sent = 0
        if not ok:
            report = format_freshness_report(strat, details)
            msg = (
                f"🚨 sector_follow_cap5_vol DATA STALE ({today})\n"
                f"Backfill likely failed; tomorrow 15:20 IST will fire-closed "
                f"unless fixed.\nAuto-pausing tomorrow's intent (override via "
                f"Telegram/SQL).\n\n" + report
            )
            try:
                self._notify(msg)
                alert_sent = 1
            except Exception:
                logger.exception("sector_follow data-health alert failed")
            self._auto_pause_tomorrow(today)

        try:
            from database.data_health_db import insert_check

            insert_check(
                strategy_name=strat,
                overall_ok=ok,
                stale_symbols=stale,
                details=details,
                alert_sent=alert_sent,
            )
        except Exception:
            logger.exception("sector_follow data-health insert_check failed")

        logger.info("sector_follow data-health check: ok=%s stale=%d", ok, len(stale))
        return ok, details

    def _auto_pause_tomorrow(self, today_iso: str) -> None:
        """Mode-only: on confirmed stale data, hold tomorrow's entries by writing
        a ``pause`` runtime override that expires after tomorrow's 15:20 entry
        window (15:30 IST). The persistent ``strategy_mode`` is untouched — only
        *entries* are held; exits/EOD still run. The engine's job-entry gate
        (``_entry_held_by_override``) enforces it. Self-expiring, so a one-off
        stale day never silently disables the strategy beyond tomorrow.

        Idempotent against an operator override: if a longer-lived pause/kill is
        already active for tomorrow's window, this no-ops (set_override upserts,
        but we keep the existing reason if it's already a hold)."""
        try:
            tomorrow_ist = self._now() + timedelta(days=1)
            expires_ist = tomorrow_ist.replace(hour=15, minute=30, second=0, microsecond=0)
            self._set_runtime_override(
                "pause",
                expires_ist,
                f"stale_feed: data health failed on {today_iso}",
            )
            logger.warning(
                "sector_follow auto-paused tomorrow's entries via runtime override "
                "(stale data on %s, expires %s IST)",
                today_iso,
                expires_ist.isoformat(),
            )
        except Exception:
            logger.exception("sector_follow auto-pause failed")

    def run_daily_reset(self) -> None:
        """09:00 IST: reset kill switch + daily P&L."""
        self.reset_daily_state()

    # ----- observability + EOD summary ----------------------------------- #
    def _config_view(self) -> dict:
        """A small, JSON-safe subset of config for the status endpoint."""
        c = self.config
        return {
            "capital_inr": c.capital_inr,
            "max_position_inr": c.max_position_inr,
            "max_concurrent_positions": c.max_concurrent_positions,
            "gate_sector_pct": c.gate_sector_pct,
            "gate_stock_pct": c.gate_stock_pct,
            "gate_vol_mult": c.gate_vol_mult,
            "daily_loss_kill_pct": c.daily_loss_kill_pct,
            "exchange": c.exchange,
            "product": c.product,
            "universe_size": len(c.universe),
        }

    def _compute_mtm(self, position: PaperPosition) -> dict:
        """Live mark-to-market for one open position.

        Fetches the current price via the injected price fetcher and returns
        ``{current_price, mtm_pnl_gross, mtm_pnl_net, mtm_error}``. ``gross`` is
        ``(current - entry) * qty``; ``net`` subtracts the round-trip cost
        (``cost_pct_round_trip`` is already the combined both-legs rate, charged
        once on the entry notional). Fully defensive: any price-fetch failure
        leaves every P&L field None and sets ``mtm_error`` — never raises."""
        try:
            price = self._price_fetcher(position.symbol, self.config.exchange)
        except Exception as e:
            logger.debug("mtm price fetch raised for %s: %s", position.symbol, e)
            price = None
        if price is None or price <= 0:
            return {
                "current_price": None,
                "mtm_pnl_gross": None,
                "mtm_pnl_net": None,
                "mtm_error": "price_unavailable",
            }
        gross = (price - position.entry_price) * position.quantity
        costs = (self.config.cost_pct_round_trip / 100.0) * position.entry_price * position.quantity
        return {
            "current_price": price,
            "mtm_pnl_gross": gross,
            "mtm_pnl_net": gross - costs,
            "mtm_error": None,
        }

    def open_positions_view(self) -> list[dict]:
        """Open positions as JSON dicts, each with live MTM (gross + net).

        ``mtm_pnl`` is kept as a legacy alias of ``mtm_pnl_net``. A per-position
        ``mtm_error`` flags a price-fetch failure (P&L fields None) so the caller
        can tell "flat" from "couldn't price it"."""
        out: list[dict] = []
        for p in self.paper_book.values():
            mtm = self._compute_mtm(p)
            out.append(
                {
                    "symbol": p.symbol,
                    "entry_date": p.entry_date,
                    "entry_price": p.entry_price,
                    "qty": p.quantity,
                    "vol_ratio": p.vol_ratio,
                    "order_id": p.order_id,
                    "current_price": mtm["current_price"],
                    "mtm_pnl_gross": mtm["mtm_pnl_gross"],
                    "mtm_pnl_net": mtm["mtm_pnl_net"],
                    "mtm_pnl": mtm["mtm_pnl_net"],  # legacy alias
                    "mtm_error": mtm["mtm_error"],
                }
            )
        return out

    def get_status(self) -> dict:
        """Current strategy state for the observability endpoint.

        ``today_pnl_net`` is realized (closed exits) + unrealized (live MTM net of
        open positions); positions that fail to price contribute 0 to the sum."""
        open_positions = self.open_positions_view()
        realized_net = sum(e.get("pnl_net", 0.0) for e in self.today_exits)
        unrealized_net = sum(
            p["mtm_pnl_net"] for p in open_positions if p.get("mtm_pnl_net") is not None
        )
        return {
            "mode": self.mode,
            "strategy_id": self.strategy_id,
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_reason": self.kill_switch_reason,
            "manual_pause": self.manual_pause,
            "today_entries": list(self.today_entries),
            "today_exits": list(self.today_exits),
            "open_positions": open_positions,
            "today_pnl_net": realized_net + unrealized_net,
            "today_pnl_realized_net": realized_net,
            "today_pnl_unrealized_net": unrealized_net,
            "capital_inr": self.config.capital_inr,
            "config": self._config_view(),
        }

    def close_all_positions(self) -> list[dict]:
        """Emergency square-off of every open position (mode-aware). Exits use the
        last known entry price as a reference when no live price is supplied —
        the actual fill is MARKET. Not blocked by the kill switch / pause."""
        results: list[dict] = []
        for pos in list(self.paper_book.values()):
            try:
                r = self.place_exit(pos)
                results.append(
                    {
                        "symbol": pos.symbol,
                        "status": "success" if r else "error",
                        "order_id": (r or {}).get("order_id"),
                    }
                )
            except Exception as e:
                logger.exception("close_all failed for %s: %s", pos.symbol, e)
                results.append({"symbol": pos.symbol, "status": "error", "message": str(e)})
        logger.warning("sector_follow close_all squared %d position(s)", len(results))
        return results

    def build_eod_summary(self, as_of: datetime | None = None) -> str:
        """Format the 15:30 IST EOD Telegram summary string."""
        as_of = as_of or self._now()
        entries = self.today_entries
        exits = self.today_exits
        open_pos = list(self.paper_book.values())
        today_pnl_net = sum(e.get("pnl_net", 0.0) for e in exits)

        entry_syms = ", ".join(e["symbol"] for e in entries) or "—"
        if exits:
            by_date: dict[str, list[str]] = {}
            for x in exits:
                tag = f"{x['symbol']} {x['pnl_pct']:+.2f}%"
                by_date.setdefault(x.get("entry_date", "?"), []).append(tag)
            exit_str = "; ".join(
                f"entered {d[5:] if len(d) >= 10 else d}: " + ", ".join(tags)
                for d, tags in sorted(by_date.items())
            )
        else:
            exit_str = "—"

        next_day = (as_of.date() + timedelta(days=1)).isoformat()
        ks = f"active ({self.kill_switch_reason})" if self.kill_switch_active else "inactive"
        pause = " · PAUSED" if self.manual_pause else ""
        return (
            f"📊 sector_follow_cap5_vol EOD {as_of.date().isoformat()}\n"
            f"Mode: {self.mode}{pause}\n"
            f"Entries: {len(entries)} ({entry_syms})\n"
            f"Exits: {len(exits)} ({exit_str})\n"
            f"Open EOD: {len(open_pos)} (T+1 exit {next_day})\n"
            f"Net PnL today: ₹{today_pnl_net:+,.0f}\n"
            f"Kill switch: {ks}"
        )

    def _format_eod_report_markdown(
        self, journal_rows: list, positions: list, kill_switch_state: dict
    ) -> str:
        """Build the Day-N markdown EOD report mirroring the Telegram summary.

        Args:
            journal_rows: the day's journal — ``today_entries`` + ``today_exits``
                concatenated. Entry rows carry an ``entry_time`` key; exit rows
                carry an ``exit_time`` key, which is how they are told apart.
            positions: open positions as ``open_positions_view()`` dicts (each
                with live MTM net).
            kill_switch_state: ``{active, reason, daily_pnl}`` at EOD.

        Returns:
            A markdown string. Pure formatting — no I/O, never raises on empty
            inputs.
        """
        as_of = self._now()
        date_str = as_of.date().isoformat()
        next_day = (as_of.date() + timedelta(days=1)).isoformat()

        entries = [r for r in journal_rows if "exit_time" not in r]
        exits = [r for r in journal_rows if "exit_time" in r]

        realized_pnl = sum(x.get("pnl_net", 0.0) for x in exits)
        capital_deployed = sum((e.get("entry_price") or 0.0) * (e.get("qty") or 0) for e in entries)

        ks_active = kill_switch_state.get("active", False)
        ks_reason = kill_switch_state.get("reason")
        ks_line = f"active ({ks_reason})" if ks_active else "inactive"

        lines: list[str] = []
        lines.append(f"# sector_follow_cap5_vol — EOD Report {date_str}")
        lines.append("")
        lines.append(f"- **Mode:** {self.mode}")
        lines.append(f"- **Generated:** {as_of.isoformat()}")
        if self.manual_pause:
            lines.append("- **Manual pause:** active")
        lines.append("")

        lines.append("## Summary")
        lines.append("")
        lines.append(f"- Signals fired / positions opened: {len(entries)}")
        lines.append(f"- Open at EOD: {len(positions)} (T+1 exit {next_day})")
        lines.append(f"- Exits today: {len(exits)}")
        lines.append(f"- Capital deployed (entry notional): ₹{capital_deployed:,.0f}")
        if exits:
            lines.append(f"- Realized net P&L: ₹{realized_pnl:+,.0f}")
        else:
            lines.append("- Realized net P&L: — (no exits today)")
        lines.append("")

        lines.append("## Sector breakdown")
        lines.append("")
        if entries:
            lines.append("| Sector index | Stocks | Sector intraday % |")
            lines.append("| --- | --- | --- |")
            by_sector: dict[str, dict] = {}
            for e in entries:
                sym = e.get("symbol")
                sector = e.get("sector") or self.sector_map.get(sym, "NIFTY")
                bucket = by_sector.setdefault(
                    sector, {"syms": [], "sector_ret": e.get("sector_ret")}
                )
                bucket["syms"].append(sym)
                if bucket["sector_ret"] is None:
                    bucket["sector_ret"] = e.get("sector_ret")
            for sector, info in sorted(by_sector.items()):
                sr = info["sector_ret"]
                sr_str = f"{sr * 100:+.2f}%" if isinstance(sr, (int, float)) else "—"
                lines.append(f"| {sector} | {', '.join(info['syms'])} | {sr_str} |")
        else:
            lines.append("_No entries today._")
        lines.append("")

        lines.append("## Positions")
        lines.append("")
        lines.append("| Symbol | Sector | Entry ₹ | Qty | Status | Exit ₹ | P&L ₹ |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for p in positions:
            sym = p.get("symbol")
            sector = self.sector_map.get(sym, "NIFTY")
            entry = p.get("entry_price")
            qty = p.get("qty")
            mtm = p.get("mtm_pnl_net")
            entry_str = f"{entry:,.2f}" if isinstance(entry, (int, float)) else "—"
            pnl_str = f"{mtm:+,.0f} (unrl)" if isinstance(mtm, (int, float)) else "— (unrl)"
            lines.append(f"| {sym} | {sector} | {entry_str} | {qty} | OPEN | — | {pnl_str} |")
        for x in exits:
            sym = x.get("symbol")
            sector = self.sector_map.get(sym, "NIFTY")
            qty = x.get("qty") or 0
            exit_price = x.get("exit_price")
            pnl = x.get("pnl_net", 0.0)
            # today_exits doesn't store the entry price; recover it exactly from
            # the realized P&L: entry = exit - pnl/qty.
            entry = (exit_price - pnl / qty) if (qty and exit_price is not None) else None
            entry_str = f"{entry:,.2f}" if entry is not None else "—"
            exit_str = f"{exit_price:,.2f}" if isinstance(exit_price, (int, float)) else "—"
            lines.append(
                f"| {sym} | {sector} | {entry_str} | {qty} | CLOSED | {exit_str} | {pnl:+,.0f} |"
            )
        if not positions and not exits:
            lines.append("| _none_ | | | | | | |")
        lines.append("")

        lines.append("## Kill switch (EOD)")
        lines.append("")
        lines.append(f"- State: {ks_line}")
        if isinstance(kill_switch_state.get("daily_pnl"), (int, float)):
            lines.append(f"- Daily P&L tracked: ₹{kill_switch_state['daily_pnl']:+,.0f}")
        lines.append(
            f"- Daily-loss kill threshold: {self.config.daily_loss_kill_pct:.1f}% of capital"
        )
        lines.append("")

        lines.append("## Note — expected vs R40 baseline")
        lines.append("")
        lines.append(
            "> R40 (`V_SF_CAP5_VOL`) backtest baseline was Sharpe ~2.19; the honest "
            "15:20-snapshot baseline is ~1.70 (see LEARNINGS.md). This report is "
            "observational — it does NOT recompute Sharpe or compare statistically."
        )
        lines.append("")
        return "\n".join(lines)

    def _write_eod_report(self) -> Path:
        """Write the Day-N markdown report to ``eod_reports/YYYY-MM-DD.md``.

        Returns the path written. Raises on I/O failure — the caller
        (``run_eod_summary``) wraps this best-effort so a write failure never
        blocks the Telegram summary.
        """
        report = self._format_eod_report_markdown(
            journal_rows=list(self.today_entries) + list(self.today_exits),
            positions=self.open_positions_view(),
            kill_switch_state={
                "active": self.kill_switch_active,
                "reason": self.kill_switch_reason,
                "daily_pnl": self.daily_pnl,
            },
        )
        out_dir = Path(self.eod_reports_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{self._now().date().isoformat()}.md"
        out_path.write_text(report, encoding="utf-8")
        logger.info("sector_follow EOD report written to %s", out_path)
        return out_path

    def run_eod_summary(self) -> str:
        """15:30 IST: write the Day-N markdown report to disk AND broadcast the
        Telegram summary. The two sinks are independent — one failing is logged
        but never blocks the other (best-effort)."""
        msg = self.build_eod_summary()
        # File sink (markdown Day-N report mirror of the Telegram summary).
        try:
            self._write_eod_report()
        except Exception as e:
            logger.exception("sector_follow EOD report file sink failed: %s", e)
        # Telegram summary (unchanged content/format).
        try:
            self._notify(msg)
        except Exception as e:
            logger.exception("sector_follow EOD Telegram summary failed: %s", e)
        logger.info("sector_follow EOD summary emitted")
        return msg

    # ----- scheduler registration ---------------------------------------- #
    def register_jobs(self, scheduler=None) -> None:
        """Register entry/exit/reset jobs on the shared APScheduler instance.

        Uses module-level job bodies (serializable for the SQLAlchemy jobstore),
        which resolve the live singleton at fire time. All replace_existing.
        """
        sched = scheduler or self.scheduler
        if sched is None:
            from services.historify_scheduler_service import get_historify_scheduler

            sched = get_historify_scheduler().scheduler

        from apscheduler.triggers.cron import CronTrigger

        global _SINGLETON
        _SINGLETON = self
        if self.strategy_id is None:
            self.seed_strategy()

        sched.add_job(
            _entry_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=20, timezone="Asia/Kolkata"),
            id="sector_follow_entry",
            replace_existing=True,
            name="Sector Follow CAP5_VOL entry (15:20 IST)",
        )
        sched.add_job(
            _exit_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=25, timezone="Asia/Kolkata"),
            id="sector_follow_exit",
            replace_existing=True,
            name="Sector Follow CAP5_VOL T+1 exit (15:25 IST)",
        )
        sched.add_job(
            _daily_reset_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone="Asia/Kolkata"),
            id="sector_follow_daily_reset",
            replace_existing=True,
            name="Sector Follow CAP5_VOL daily reset (09:00 IST)",
        )
        sched.add_job(
            _eod_summary_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone="Asia/Kolkata"),
            id="sector_follow_eod_summary",
            replace_existing=True,
            name="Sector Follow CAP5_VOL EOD summary (15:30 IST)",
        )
        sched.add_job(
            _data_health_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=30, timezone="Asia/Kolkata"),
            id="sector_follow_data_health",
            replace_existing=True,
            name="Sector Follow CAP5_VOL data freshness check (16:30 IST)",
        )
        logger.info(
            "sector_follow jobs registered (mode=%s, strategy_id=%s)",
            self.mode,
            self.strategy_id,
        )


# --------------------------------------------------------------------------- #
# Module-level scheduler entry points + singleton (serializable for jobstore)
# --------------------------------------------------------------------------- #
_SINGLETON: SectorFollowService | None = None


def get_service() -> SectorFollowService | None:
    return _SINGLETON


def _entry_job() -> None:
    if _SINGLETON is not None:
        _SINGLETON.run_entry()


def _exit_job() -> None:
    if _SINGLETON is not None:
        _SINGLETON.run_exit()


def _daily_reset_job() -> None:
    if _SINGLETON is not None:
        _SINGLETON.run_daily_reset()


def _eod_summary_job() -> None:
    if _SINGLETON is not None:
        _SINGLETON.run_eod_summary()


def _data_health_job() -> None:
    if _SINGLETON is not None:
        _SINGLETON.run_data_health_check()


def init_sector_follow_service(app=None, scheduler=None) -> SectorFollowService:
    """Build the singleton and register its scheduler jobs. Default mode=scaffold
    so loading this module changes no live trading behavior."""
    svc = SectorFollowService(
        app=app, scheduler=scheduler, data_health_checker=production_data_health_checker
    )
    svc.register_jobs(scheduler)
    if app is not None:
        app.sector_follow_service = svc
    return svc
