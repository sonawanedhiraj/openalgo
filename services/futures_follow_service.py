"""Futures Follow CAP50 strategy service.

A **leveraged broad-market-beta** sleeve. At 15:20 IST it reuses the
``sector_follow_cap5_vol`` C1×W2+E4 signal evaluator to find today's ≤5 gate-passing
stock signals, and for each signal — greedily in vol-ratio order — buys **one
NIFTY near-month index future lot**, subject to a HARD CAP of 50% of capital as
overnight SPAN margin. Positions are held to the **next trading day 15:25 IST**
(T+1) MARKET sell. NO stop loss (Phase-1 proved hard stops are net-negative on this
signal class); the EOD watchdog at 15:14 IST is the only safety backstop.

**It is ACTIVELY trading in sandbox by default** — from boot it places real orders
into ``sandbox.db`` (the virtual ₹1Cr book). There is no observe-only / scaffold
state; the mode flag is only ``sandbox`` or ``live``.

Honest caveat (carried from the backtest, do not lose it): the signal does NOT
predict NIFTY direction (hit-rate 53.4% < 55%, corr 0.295). The 14.44% CAGR comes
from leveraging the small positive broad-market drift on bullish signal-days — it
is **leveraged beta, not the sector_follow stock-selection alpha.** A flat/bear
NIFTY year has no stock-selection edge to fall back on. Keep the CNC T+1 equity
book (sector_follow_cap5_vol) as the alpha primary.

Mode flag (env): FUTURES_FOLLOW_MODE = sandbox | live
  sandbox (default): orders to sandbox.db (virtual ₹1Cr) — active trading
  live: real broker orders

Backtest reference (NIFTY-only CAP50): CAGR 14.44%, Sharpe 1.27, MaxDD −8.0% on
₹10L over 2024-01..2026-06. See
docs/research/strategy/sector_follow_cap5_vol/2026-06-14_sector_matched_futures_10L.md
and 2026-06-14_futures_10L.md.

Plan / decisions: strategies/futures_follow_cap50/ (LEARNINGS.md, PLAN.md,
config_snapshot.json).

Testability: all I/O (signal evaluation, contract resolution, order placement,
notifications, trade journal) is injected with production defaults, mirroring
services/sector_follow_service.py — unit tests drive the pure decision logic
without a live broker or DuckDB.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

_STRATEGY_DIR = Path(__file__).resolve().parents[1] / "strategies" / "futures_follow_cap50"
_DEFAULT_CONFIG_PATH = _STRATEGY_DIR / "config_snapshot.json"
# Day-N EOD markdown reports (mirror of the 15:30 IST Telegram summary). Path is
# hardcoded (no env var); the instance attribute below lets tests redirect it.
_EOD_REPORTS_DIR = _STRATEGY_DIR / "eod_reports"

STRATEGY_NAME = "futures_follow_cap50"
VALID_MODES = ("sandbox", "live")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class FuturesFollowConfig:
    """Strategy configuration. Mirrors config_snapshot.json (sandbox defaults)."""

    capital_inr: float = 1_000_000.0  # ₹10L (the backtest book size)
    cap_margin_pct: float = 0.50  # HARD cap: max 50% of capital as overnight margin
    nifty_lot_size: int = 75  # NIFTY lot size (post-2024-11-20 SEBI revision)
    nifty_lot_margin_inr: float = 250_000.0  # per-lot overnight SPAN margin estimate
    margin_rate: float = 0.14  # SPAN+exposure proxy (for the observability estimate)
    lots_per_signal: int = 1  # always 1 lot per signal up to the cap
    max_signals_per_day: int = 5  # inherited K5 cap from the sector_follow signal set
    daily_loss_kill_pct: float = 3.0
    cost_pct_round_trip: float = 0.030  # ~0.03% of notional (~₹530/lot) — see charge model
    underlying: str = "NIFTY"
    broker: str = "zerodha"
    exchange: str = "NFO"
    product: str = "NRML"  # futures carry — NOT MIS, NOT CNC
    strategy_id: int | None = None

    @property
    def cap_margin_inr(self) -> float:
        """Absolute hard margin cap in rupees (cap_margin_pct × capital)."""
        return self.cap_margin_pct * self.capital_inr


def load_config(path: str | Path = _DEFAULT_CONFIG_PATH) -> FuturesFollowConfig:
    """Load config_snapshot.json into a FuturesFollowConfig (missing keys -> defaults)."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return FuturesFollowConfig(
        capital_inr=float(raw.get("capital_inr", 1_000_000.0)),
        cap_margin_pct=float(raw.get("cap_margin_pct", 0.50)),
        nifty_lot_size=int(raw.get("nifty_lot_size", 75)),
        nifty_lot_margin_inr=float(raw.get("nifty_lot_margin_inr", 250_000.0)),
        margin_rate=float(raw.get("margin_rate", 0.14)),
        lots_per_signal=int(raw.get("lots_per_signal", 1)),
        max_signals_per_day=int(raw.get("max_signals_per_day", 5)),
        daily_loss_kill_pct=float(raw.get("daily_loss_kill_pct", 3.0)),
        cost_pct_round_trip=float(raw.get("cost_pct_round_trip", 0.030)),
        underlying=str(raw.get("underlying", "NIFTY")),
        broker=str(raw.get("broker", "zerodha")),
        exchange=str(raw.get("exchange", "NFO")),
        product=str(raw.get("product", "NRML")),
        strategy_id=raw.get("strategy_id"),
    )


# --------------------------------------------------------------------------- #
# Pure decision logic
# --------------------------------------------------------------------------- #
def compute_lots_to_buy(
    lots_already_filled: int,
    capital: float,
    lot_margin: float,
    cap_margin_pct: float = 0.50,
    lots_per_signal: int = 1,
) -> int:
    """Lots to buy for the next signal, honoring the 50%-of-capital margin cap.

    Each signal buys ``lots_per_signal`` lot(s) (default 1). The day's cumulative
    overnight margin must never exceed ``cap_margin_pct × capital``. If adding one
    more lot would breach the cap, the signal is skipped (returns 0).
    """
    if lot_margin <= 0 or capital <= 0:
        return 0
    cap_margin = cap_margin_pct * capital
    used = lots_already_filled * lot_margin
    remaining = cap_margin - used
    if remaining < lot_margin:
        return 0  # cap hit; skip this (late-arriving) signal
    return lots_per_signal


def compute_futures_charges(buy_notional: float, sell_notional: float) -> float:
    """Modelled Zerodha round-trip charges on one NIFTY futures position.

    Per the documented charge model (~₹530/lot ≈ 0.03% of ~₹18L notional):
      - Brokerage ₹20 × 2 legs = ₹40 (flat)
      - STT 0.02% on the SELL leg
      - Exchange txn 0.0019% on BOTH legs
      - SEBI ₹10/Cr = 0.0001% on BOTH legs
      - Stamp duty 0.002% on the BUY leg
      - GST 18% on (brokerage + exchange txn + SEBI)
    Returns total charges in rupees. Pure — no I/O.
    """
    both = buy_notional + sell_notional
    brokerage = 40.0
    stt = 0.0002 * sell_notional
    exch_txn = 0.000019 * both
    sebi = 0.000001 * both
    stamp = 0.00002 * buy_notional
    gst = 0.18 * (brokerage + exch_txn + sebi)
    return brokerage + stt + exch_txn + sebi + stamp + gst


# --------------------------------------------------------------------------- #
# Production signal evaluator — reuses the sector_follow_cap5_vol evaluator
# --------------------------------------------------------------------------- #
def production_signal_evaluator(as_of: datetime | None = None) -> list[dict]:
    """Today's ≤5 gate-passing stock signals, via the LIVE sector_follow evaluator.

    Reuses ``services.sector_follow_service`` (the canonical production gate
    evaluator) — config, sector map, DuckDB metrics provider, ``passes_gates`` and
    ``select_entries`` — so the futures sleeve fires on exactly the signal set the
    equity book sees. Each returned dict carries ``symbol`` + ``vol_ratio`` (the
    greedy ordering key for the margin cap). Lazily imported so importing this
    module never pulls the DuckDB stack.

    NOTE: the production sector_follow evaluator ships the C1 base gate; the
    W2+E4 refinements that define the backtested "C1×W2+E4 K5" set live in the
    research harness, exactly as for sector_follow itself. The futures sleeve
    inherits whatever the live evaluator produces — see LEARNINGS.md.
    """
    from services.sector_follow_service import (
        duckdb_metrics_provider,
        passes_gates,
        select_entries,
    )
    from services.sector_follow_service import (
        load_config as _sf_load_config,
    )
    from services.sector_follow_service import (
        load_sector_map as _sf_load_sector_map,
    )

    as_of = as_of or datetime.now(_IST)
    sf_config = _sf_load_config()
    sector_map = _sf_load_sector_map()
    metrics = duckdb_metrics_provider(as_of, sf_config.universe, sector_map, sf_config)
    candidates: list[dict] = []
    for symbol, m in metrics.items():
        if passes_gates(m, sf_config):
            candidates.append(
                {
                    "symbol": symbol,
                    "vol_ratio": m.get("vol_ratio"),
                    "stock_ret": m.get("stock_ret"),
                    "sector_ret": m.get("sector_ret"),
                }
            )
    # Cap at K5 + vol-ratio-desc tiebreaker — identical selection to the equity book.
    return select_entries(candidates, set(), sf_config.max_concurrent_positions)


# --------------------------------------------------------------------------- #
# Production NIFTY near-month future contract resolver — injected default
# --------------------------------------------------------------------------- #
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def _parse_expiry(expiry: str) -> date | None:
    """Parse a broker expiry string (``DD-MMM-YY`` / ``DD-MMM-YYYY``) to a date.

    Returns None on any unparseable value (so a bad row never crashes resolution).
    """
    if not expiry:
        return None
    parts = str(expiry).strip().upper().replace("/", "-").split("-")
    if len(parts) != 3:
        return None
    try:
        day = int(parts[0])
        mon = _MONTHS.get(parts[1][:3])
        yr = int(parts[2])
        if mon is None:
            return None
        if yr < 100:
            yr += 2000
        return date(yr, mon, day)
    except (ValueError, KeyError):
        return None


def production_contract_resolver(
    underlying: str = "NIFTY", exchange: str = "NFO", as_of: datetime | None = None
) -> dict | None:
    """Resolve the current NEAR-MONTH NIFTY index future from the master contract.

    NIFTY index futures are MONTHLY (there are no weekly NIFTY futures — only weekly
    NIFTY *options*). The deployable contract is the front-month (nearest non-expired)
    monthly FUT, the most liquid. Returns
    ``{symbol, brsymbol, token, expiry, lot_size}`` or None when no contract is found
    (so the caller fails closed and places no order). Lazily imported.
    """
    from database.symbol import fno_search_symbols_db

    as_of_date = (as_of or datetime.now(_IST)).astimezone(_IST).date()
    rows = fno_search_symbols_db(underlying=underlying, exchange=exchange, instrumenttype="FUT")
    # Keep only rows whose OpenAlgo symbol is a plain underlying future (e.g.
    # NIFTY26DEC24FUT) — excludes NIFTYNXT50/BANKNIFTY/etc. that share the prefix.
    dated: list[tuple[date, dict]] = []
    for r in rows:
        if (r.get("name") or "").strip().upper() != underlying.upper():
            continue
        exp = _parse_expiry(r.get("expiry"))
        # Skip contracts expiring today OR tomorrow — the strategy holds the
        # position T+1 overnight (buy 15:20, sell next day 15:25), so a contract
        # that won't survive until tomorrow's 15:25 exit cannot be used. On a
        # monthly-expiry Thursday this skips the current-month contract and the
        # next-month future is picked automatically.
        if exp is None or exp <= as_of_date + timedelta(days=1):
            continue
        dated.append((exp, r))
    if not dated:
        logger.error(
            "futures_follow: no non-expired %s future found in master contract (exchange=%s)",
            underlying,
            exchange,
        )
        return None
    dated.sort(key=lambda t: t[0])
    exp, r = dated[0]
    return {
        "symbol": r.get("symbol"),
        "brsymbol": r.get("brsymbol"),
        "token": r.get("token"),
        "expiry": r.get("expiry"),
        "lot_size": int(r.get("lotsize") or 75),
    }


# --------------------------------------------------------------------------- #
# Production order placer (mode-aware) — injected default
# --------------------------------------------------------------------------- #
def production_order_placer(mode: str, order: dict) -> dict:
    """Route an order according to mode. Returns {status, orderid, ...}.

    Both modes go through services.place_order_service.place_order — sandbox relies
    on the platform's analyze/daily-intent dispatch to land in sandbox.db, live
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
        "strategy": STRATEGY_NAME,
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


def _resolve_exit_api_key() -> str | None:
    """First available OpenAlgo api key for the live broker positionbook read (#265).

    Lazily imported so importing this module never pulls the auth stack. ``None``
    when no key is available — the reconciliation guard then fails closed."""
    try:
        from database.auth_db import get_first_available_api_key

        return get_first_available_api_key()
    except Exception:
        logger.exception("futures_follow: resolve exit api_key failed")
        return None


def production_price_fetcher(symbol: str, exchange: str) -> float | None:
    """Current LTP for one symbol via the broker quote API. None on any failure.

    Lazily imported so importing this module never pulls the quote/auth stack.
    Used by ``FuturesFollowService._compute_mtm`` for live MTM in the status
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
    service can inject a fake in tests. Lazily imported."""
    from services.mode_service import resolve_strategy_mode

    return resolve_strategy_mode(STRATEGY_NAME)


def telegram_notifier(message: str) -> None:
    """Best-effort Telegram broadcast; silent when the bot is disabled/unconfigured."""
    try:
        from services.telegram_bot_service import telegram_bot_service as svc

        if svc is None or not getattr(svc, "is_running", False):
            return
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

    The futures sleeve fires on the sector_follow signal set, so its feed health is
    the sector_follow feed health — delegate to that strategy's check. Fully
    defensive: any infrastructure failure fails OPEN (returns ``(True, {})``)."""
    try:
        from services.data_freshness_service import check_strategy_data_ready

        # Feed parity with the signal source.
        return check_strategy_data_ready("sector_follow_cap5_vol", date, index_only=index_only)
    except Exception:
        logger.exception("futures_follow data-health checker failed (failing open)")
        return True, {}


def futures_smoke_check_enabled() -> bool:
    """``FUTURES_FOLLOW_SMOKE_CHECK_ENABLED`` env flag (default true).

    When off the 15:18 pre-entry smoke check is a no-op (returns ok=True, no
    override written). Set ``false`` only to disable the guard entirely — stale
    data at 15:20 will then be caught only by the per-entry
    ``_data_is_fresh_for_entry`` gate (which still blocks but does NOT alert)."""
    return os.getenv("FUTURES_FOLLOW_SMOKE_CHECK_ENABLED", "true").lower() == "true"


def production_broker_session_checker() -> bool:
    """True iff a broker session (API key) is configured (operator logged in).
    Best-effort; ``False`` on any error. Used by the 15:18 smoke check."""
    try:
        from database.auth_db import get_first_available_api_key

        return bool(get_first_available_api_key())
    except Exception:
        logger.debug("futures_follow broker-session check failed", exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
@dataclass
class FuturesPosition:
    """An open NIFTY-futures position tracked in-memory across all modes."""

    nifty_symbol: str
    lots: int
    quantity: int  # lots * lot_size
    entry_price: float
    entry_date: str
    vol_ratio: float
    margin_inr: float
    signal_symbol: str | None = None  # the source stock signal (observability)
    order_id: str | None = None


class FuturesFollowService:
    """Futures-follow CAP50 evaluator + scheduler glue.

    All external effects are injected (with production defaults) so the decision
    logic is unit-testable without a broker or DuckDB.
    """

    def __init__(
        self,
        app=None,
        scheduler=None,
        *,
        config: FuturesFollowConfig | None = None,
        mode: str | None = None,
        signal_evaluator: Callable[..., list[dict]] | None = None,
        contract_resolver: Callable[..., dict | None] | None = None,
        order_placer: Callable[[str, dict], dict] | None = None,
        price_fetcher: Callable[[str, str], float | None] | None = None,
        notifier: Callable[[str], None] | None = None,
        trade_recorder: Callable[..., object] | None = None,
        now: Callable[[], datetime] | None = None,
        intent_resolver: Callable[[], object] | None = None,
        data_health_checker: Callable[..., tuple] | None = None,
        broker_session_checker: Callable[[], bool] | None = None,
    ):
        self.app = app
        self.scheduler = scheduler
        self.config = config if config is not None else load_config()
        self.mode = (mode or os.getenv("FUTURES_FOLLOW_MODE", "sandbox")).lower()
        if self.mode not in VALID_MODES:
            logger.warning("Unknown FUTURES_FOLLOW_MODE=%s — forcing sandbox", self.mode)
            self.mode = "sandbox"

        self._signal_evaluator = signal_evaluator or production_signal_evaluator
        self._contract_resolver = contract_resolver or production_contract_resolver
        self._order_placer = order_placer or production_order_placer
        self._price_fetcher = price_fetcher or production_price_fetcher
        self._notify = notifier or telegram_notifier
        self._record_trade = trade_recorder or self._default_trade_recorder
        self._intent_resolver = intent_resolver or production_intent_resolver
        # Data-freshness gate. Left None in unit tests (gate skipped, hermetic);
        # the live singleton injects ``production_data_health_checker``.
        self._data_health_checker = data_health_checker
        # Broker-session checker for the 15:18 smoke check. Left None in unit tests
        # (treated as live); the live singleton uses ``production_broker_session_checker``.
        self._broker_session_checker = broker_session_checker or production_broker_session_checker
        self._now = now or (lambda: datetime.now(_IST))
        self.eod_reports_dir = _EOD_REPORTS_DIR

        # Mutable runtime state.
        self.paper_book: dict[str, FuturesPosition] = {}  # keyed by a per-position id
        self.kill_switch_active = False
        self.kill_switch_reason: str | None = None
        self.manual_pause = False
        self.daily_pnl = 0.0
        self.today_entries: list[dict] = []
        self.today_exits: list[dict] = []
        self.strategy_id: int | None = self.config.strategy_id

    # ----- per-lot margin estimate -------------------------------------- #
    def lot_margin_estimate(self, price: float | None = None) -> float:
        """Per-lot overnight margin used for the CAP decision.

        Authoritative cap value is the config ``nifty_lot_margin_inr`` (a fixed,
        operator-tunable estimate refreshed from the broker SPAN margin) — this
        keeps the cap deterministic across the entry batch and across a session.
        When a live ``price`` is supplied, a dynamic
        ``price × lot_size × margin_rate`` estimate is returned for OBSERVABILITY
        only (the status endpoint), never for the cap math."""
        if price is not None and price > 0:
            return price * self.config.nifty_lot_size * self.config.margin_rate
        return self.config.nifty_lot_margin_inr

    # ----- strategy DB seeding ------------------------------------------- #
    def seed_strategy(self, user_id: str = "internal") -> int | None:
        """Idempotently register the strategy in the `strategies` table; return its id."""
        try:
            from database.futures_follow_db import init_db as _init_journal
            from database.strategy_db import create_strategy, get_all_strategies

            _init_journal()  # ensure the trade-journal table exists

            for s in get_all_strategies():
                if s.name == STRATEGY_NAME:
                    self.strategy_id = s.id
                    return s.id

            strat = create_strategy(
                name=STRATEGY_NAME,
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
                self.strategy_id = strat.id
                return strat.id
        except Exception as e:
            logger.exception(f"Failed to seed futures_follow strategy row: {e}")
        return None

    def _default_trade_recorder(self, **kwargs):
        from database.futures_follow_db import record_trade

        return record_trade(strategy_id=self.strategy_id, mode=self.mode, **kwargs)

    # ----- evaluation ---------------------------------------------------- #
    def evaluate_signals(self, as_of: datetime | None = None) -> list[dict]:
        """Return today's ≤5 stock signals (greedy vol-ratio order) from the
        injected evaluator (default: the sector_follow_cap5_vol evaluator)."""
        as_of = as_of or self._now()
        signals = self._signal_evaluator(as_of)
        logger.info(
            "futures_follow eval @ %s: %d signal(s) from sector_follow evaluator",
            as_of.isoformat(),
            len(signals),
        )
        return signals

    # ----- open-position view -------------------------------------------- #
    def lots_held(self) -> int:
        """Total NIFTY future lots currently open across all positions."""
        return sum(p.lots for p in self.paper_book.values())

    def margin_used(self) -> float:
        """Estimated overnight margin currently locked (cap-decision basis)."""
        return self.lots_held() * self.lot_margin_estimate()

    # ----- kill switch --------------------------------------------------- #
    def update_daily_pnl(self, realized_today: float, open_mtm: float) -> bool:
        """Recompute daily P&L and the kill switch. Returns kill_switch_active."""
        self.daily_pnl = realized_today + open_mtm
        threshold = -(self.config.daily_loss_kill_pct / 100.0) * self.config.capital_inr
        if not self.kill_switch_active and self.daily_pnl < threshold:
            self.kill_switch_active = True
            self.kill_switch_reason = (
                f"daily P&L ₹{self.daily_pnl:,.0f} breached "
                f"{self.config.daily_loss_kill_pct}% of capital"
            )
            logger.error(
                "futures_follow KILL SWITCH fired: daily_pnl=%.0f < %.0f (%.1f%% of capital)",
                self.daily_pnl,
                threshold,
                self.config.daily_loss_kill_pct,
            )
            self._notify(
                f"🛑 {STRATEGY_NAME} kill switch fired — daily P&L "
                f"₹{self.daily_pnl:,.0f} breached {self.config.daily_loss_kill_pct}% of capital. "
                "New entries blocked; open positions hold to scheduled exit."
            )
            self._set_runtime_override(
                "kill_switch",
                self._end_of_today_ist(),
                self.kill_switch_reason or "daily loss kill",
            )
        return self.kill_switch_active

    def reset_daily_state(self) -> None:
        """09:00 IST reset: clear kill switch + daily P&L and the intraday journals.

        Does NOT clear ``manual_pause`` (an operator pause persists). Open positions
        in paper_book survive (they hold to their scheduled T+1 exit)."""
        self.kill_switch_active = False
        self.kill_switch_reason = None
        self.daily_pnl = 0.0
        self.today_entries = []
        self.today_exits = []
        logger.info("futures_follow daily state reset (kill switch cleared, pnl=0)")

    # ----- runtime-override durability (mode-only safety guards) ---------- #
    def _utc_naive(self, ist_dt) -> datetime:
        """IST-aware datetime → naive UTC (strategy_runtime_override stores naive UTC)."""
        return ist_dt.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: UP017

    def _end_of_today_ist(self):
        """Today 23:59 IST — same-day expiry for an intraday hold flag."""
        return self._now().replace(hour=23, minute=59, second=0, microsecond=0)

    def _set_runtime_override(self, override_type: str, expires_ist, reason: str) -> None:
        """Durably record a safety hold in ``strategy_runtime_override``. Fail-safe."""
        try:
            from database.strategy_runtime_override_db import set_override

            set_override(
                STRATEGY_NAME,
                override_type,
                self._utc_naive(expires_ist),
                reason=reason,
                set_by="futures_follow",
            )
        except Exception:
            logger.exception("futures_follow: failed to write %s runtime override", override_type)

    def _clear_runtime_override(self) -> None:
        try:
            from database.strategy_runtime_override_db import clear_override

            clear_override(STRATEGY_NAME)
        except Exception:
            logger.exception("futures_follow: failed to clear runtime overrides")

    # ----- operator manual controls -------------------------------------- #
    def pause(self) -> dict:
        """Operator pause: halt new entries. Open positions hold to T+1 exit."""
        self.manual_pause = True
        self._set_runtime_override("pause", self._end_of_today_ist(), "operator manual pause")
        logger.warning("futures_follow MANUALLY PAUSED — new entries halted (exits still run)")
        return {"status": "success", "manual_pause": True}

    def resume(self) -> dict:
        """Operator resume: clear both manual pause and the kill switch."""
        self.manual_pause = False
        self.kill_switch_active = False
        self.kill_switch_reason = None
        self._clear_runtime_override()
        logger.info("futures_follow RESUMED — manual pause + kill switch cleared")
        return {"status": "success", "manual_pause": False, "kill_switch_active": False}

    # ----- order placement (mode-aware) ---------------------------------- #
    def place_entry(
        self,
        signal: dict,
        contract: dict,
        lots: int,
        entry_price: float,
        entry_date: str | None = None,
    ) -> dict | None:
        """Buy ``lots`` NIFTY future lot(s) for one signal in the active mode.

        Honors the kill switch + manual pause. Always routes via the injected order
        placer (sandbox → sandbox.db, live → broker) and writes a trade-journal row.
        A rejected/exception order journals the attempt but creates NO phantom
        position."""
        if self.kill_switch_active:
            logger.info("futures_follow entry skipped (kill switch active)")
            return None
        if self.manual_pause:
            logger.info("futures_follow entry skipped (manual pause)")
            return None
        if lots <= 0:
            return None

        symbol = contract["symbol"]
        lot_size = int(contract.get("lot_size") or self.config.nifty_lot_size)
        qty = lots * lot_size
        margin = lots * self.lot_margin_estimate()
        signal_symbol = signal.get("symbol")
        vol_ratio = signal.get("vol_ratio") or 0.0
        signal_id = signal.get("signal_id") or signal_symbol
        entry_date = entry_date or self._now().date().isoformat()

        order_id = None
        status = "placed"
        error_message = None
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
            logger.exception("futures_follow ENTRY placement raised: %s", symbol)
            resp = {"status": "error", "message": str(e)}
            status = "exception"
        resp = resp or {}
        order_id = resp.get("orderid")
        if status != "exception":
            status = "placed" if str(resp.get("status", "")).lower() == "success" else "rejected"
        if status == "placed":
            logger.info(
                "[%s] futures_follow ENTRY %s %dlot(s) qty=%d @ %.2f order_id=%s",
                self.mode,
                symbol,
                lots,
                qty,
                entry_price,
                order_id,
            )
        else:
            error_message = str(
                resp.get("message") or resp.get("status") or "order placement failed"
            )[:255]
            logger.error(
                "[%s] futures_follow ENTRY %s %s %dlot(s) @ %.2f — %s",
                self.mode,
                status.upper(),
                symbol,
                lots,
                entry_price,
                error_message,
            )
            self._record_trade(
                side="BUY",
                nifty_symbol=symbol,
                lots=lots,
                quantity=qty,
                entry_price=entry_price,
                entry_date=entry_date,
                exchange=self.config.exchange,
                product=self.config.product,
                signal_id=signal_id,
                vol_ratio=vol_ratio,
                margin_inr=margin,
                order_id=None,
                status=status,
                error_message=error_message,
            )
            return None

        pos_id = order_id or f"{symbol}:{signal_symbol}:{uuid.uuid4().hex[:8]}"
        self.paper_book[pos_id] = FuturesPosition(
            nifty_symbol=symbol,
            lots=lots,
            quantity=qty,
            entry_price=entry_price,
            entry_date=entry_date,
            vol_ratio=vol_ratio,
            margin_inr=margin,
            signal_symbol=signal_symbol,
            order_id=order_id,
        )
        self._record_trade(
            side="BUY",
            nifty_symbol=symbol,
            lots=lots,
            quantity=qty,
            entry_price=entry_price,
            entry_date=entry_date,
            exchange=self.config.exchange,
            product=self.config.product,
            signal_id=signal_id,
            vol_ratio=vol_ratio,
            margin_inr=margin,
            order_id=order_id,
            status=status,
        )
        self.today_entries.append(
            {
                "pos_id": pos_id,
                "nifty_symbol": symbol,
                "signal_symbol": signal_symbol,
                "entry_time": self._now().isoformat(),
                "entry_price": entry_price,
                "lots": lots,
                "qty": qty,
                "margin_inr": margin,
                "vol_ratio": vol_ratio,
            }
        )
        return {
            "pos_id": pos_id,
            "nifty_symbol": symbol,
            "signal_symbol": signal_symbol,
            "lots": lots,
            "quantity": qty,
            "price": entry_price,
            "margin_inr": margin,
            "order_id": order_id,
        }

    def _reconcile_exit_qty(self, position: FuturesPosition) -> int | None:
        """Position-store reconciliation for a T+1 exit (#265), BOTH modes.

        Reconciles the journalled close qty against the mode-appropriate position
        store: the ``sandbox.db`` virtual book in sandbox mode and the broker
        positionbook in live mode (routing is handled by ``get_open_position``'s
        own mode-awareness). Returns the guarded SELL quantity to place, or
        ``None`` to SUPPRESS the exit (store flat / opposite side).
        """
        try:
            from services import live_position_reconciliation_service as recon

            decision = recon.reconcile_exit(
                strategy=STRATEGY_NAME,
                api_key=_resolve_exit_api_key(),
                symbol=position.nifty_symbol,
                exchange=self.config.exchange,
                product=self.config.product,
                expected_close_side="SELL",
                journaled_qty=position.quantity,
            )
        except Exception:
            # Guard must never break the exit path; on an unexpected failure fall
            # back to the journalled qty (never MORE than journaled).
            logger.exception("futures_follow reconcile raised; proceeding with journaled qty")
            return position.quantity
        if not decision.should_place:
            logger.error(
                "futures_follow EXIT SUPPRESSED for %s (reason=%s, store_qty=%s, journaled=%d)",
                position.nifty_symbol,
                decision.reason,
                decision.broker_qty,
                position.quantity,
            )
            return None
        return decision.guarded_qty

    def place_exit(
        self, pos_id: str, position: FuturesPosition, price: float | None = None
    ) -> dict | None:
        """Square off one position (mode-aware). Exits are NOT blocked by the kill
        switch — open positions always run to their scheduled T+1 exit.

        The mode-appropriate position store's net qty is reconciled first (#265,
        BOTH modes): a phantom (store flat) is SUPPRESSED and a partial
        (store < journaled) is CLAMPED. Sandbox reads ``sandbox.db``; live reads
        the broker positionbook (routing handled by ``get_open_position``)."""
        symbol = position.nifty_symbol
        exit_price = price if price is not None else position.entry_price

        exit_qty = self._reconcile_exit_qty(position)
        if exit_qty is None:
            # Phantom / opposite-side: the store holds nothing to close. Drop the
            # in-memory position (its store leg is already flat) without placing
            # a SELL, so a repeat exit job doesn't retry it.
            self.paper_book.pop(pos_id, None)
            return None

        order_id = None
        status = "placed"
        error_message = None
        try:
            resp = self._order_placer(
                self.mode,
                {
                    "symbol": symbol,
                    "exchange": self.config.exchange,
                    "action": "SELL",
                    "product": self.config.product,
                    "quantity": exit_qty,
                },
            )
        except Exception as e:
            logger.exception("futures_follow EXIT placement raised: %s", symbol)
            resp = {"status": "error", "message": str(e)}
            status = "exception"
        resp = resp or {}
        order_id = resp.get("orderid")
        if status != "exception":
            status = "placed" if str(resp.get("status", "")).lower() == "success" else "rejected"
        if status == "placed":
            logger.info(
                "[%s] futures_follow EXIT %s %dlot(s) qty=%d @ %.2f order_id=%s",
                self.mode,
                symbol,
                position.lots,
                exit_qty,
                exit_price,
                order_id,
            )
        else:
            error_message = str(
                resp.get("message") or resp.get("status") or "order placement failed"
            )[:255]
            logger.error(
                "[%s] futures_follow EXIT %s %s %dlot(s) @ %.2f — %s",
                self.mode,
                status.upper(),
                symbol,
                position.lots,
                exit_price,
                error_message,
            )

        buy_notional = position.entry_price * exit_qty
        sell_notional = exit_price * exit_qty
        gross_pnl = (exit_price - position.entry_price) * exit_qty
        charges = compute_futures_charges(buy_notional, sell_notional)
        net_pnl = gross_pnl - charges
        self._record_trade(
            side="SELL",
            nifty_symbol=symbol,
            lots=position.lots,
            quantity=exit_qty,
            entry_price=position.entry_price,
            exit_price=exit_price,
            entry_date=position.entry_date,
            exchange=self.config.exchange,
            product=self.config.product,
            vol_ratio=position.vol_ratio,
            margin_inr=position.margin_inr,
            gross_pnl=gross_pnl,
            charges_inr=charges,
            net_pnl=net_pnl,
            order_id=order_id,
            status=status,
            error_message=error_message,
            note="t+1_exit",
        )
        self.today_exits.append(
            {
                "nifty_symbol": symbol,
                "signal_symbol": position.signal_symbol,
                "exit_time": self._now().isoformat(),
                "exit_price": exit_price,
                "entry_date": position.entry_date,
                "lots": position.lots,
                "qty": exit_qty,
                "gross_pnl": gross_pnl,
                "charges_inr": charges,
                "net_pnl": net_pnl,
            }
        )
        self.paper_book.pop(pos_id, None)
        return {
            "pos_id": pos_id,
            "nifty_symbol": symbol,
            "lots": position.lots,
            "net_pnl": net_pnl,
            "order_id": order_id,
        }

    # ----- unified daily intent (mode + intent gate) --------------------- #
    def _resolve_decision(self):
        """Resolve today's unified {mode, intent, daily_capital_cap, source}.
        Fail-open: any resolver error yields an env-default ``run`` decision."""
        try:
            return self._intent_resolver()
        except Exception:
            logger.debug("futures_follow intent resolve failed; defaulting to run", exc_info=True)
            from services.mode_service import EffectiveDecision

            return EffectiveDecision(
                mode="sandbox", intent="run", daily_capital_cap=None, source="default"
            )

    def _entry_held_by_override(self) -> bool:
        """Mode-only: True iff a non-expired ``pause``/``kill_switch`` row is holding
        new entries. Fail-open on a read error."""
        try:
            from database.strategy_runtime_override_db import is_entry_blocked

            blocked, ov = is_entry_blocked(STRATEGY_NAME)
            if blocked and ov:
                logger.info(
                    "futures_follow entry held by %s (reason=%s, expires=%s)",
                    ov.get("override_type"),
                    ov.get("reason"),
                    ov.get("expires_at"),
                )
            return blocked
        except Exception:
            logger.debug(
                "futures_follow runtime-override resolve failed; not blocking", exc_info=True
            )
            return False

    def _apply_mode_override(self, decision) -> None:
        """Mode-only: honor a persistent ``strategy_mode`` row by mapping its mode
        onto the service's native routing mode. Env/default sources leave the active
        sandbox default untouched (only a ``strategy_mode`` row can escalate to live)."""
        if getattr(decision, "source", None) != "strategy_mode":
            return
        mapped = {"sandbox": "sandbox", "live": "live"}.get(decision.mode)
        if mapped and mapped != self.mode:
            logger.warning(
                "futures_follow mode override %s -> %s (strategy_mode row)", self.mode, mapped
            )
            self.mode = mapped

    def _effective_cap_margin(self, decision) -> float:
        """Apply an optional ``daily_capital_cap`` to the margin cap.

        With a cap set, the effective margin cap is the lesser of
        ``cap_margin_pct × capital`` and the supplied cap. NULL cap = config cap."""
        base = self.config.cap_margin_inr
        cap = getattr(decision, "daily_capital_cap", None)
        if cap is None:
            return base
        return max(0.0, min(base, float(cap)))

    # ----- scheduled job bodies ------------------------------------------ #
    def run_entry(self) -> list[dict]:
        """15:20 IST: evaluate signals, resolve the contract, buy 1 lot/signal up to
        the 50%-margin cap (subject to the override gate, kill switch + freshness)."""
        decision = self._resolve_decision()
        if self._entry_held_by_override():
            return []
        if not self._data_is_fresh_for_entry():
            return []
        self._apply_mode_override(decision)

        contract = self._contract_resolver(
            self.config.underlying, self.config.exchange, self._now()
        )
        if not contract or not contract.get("symbol"):
            logger.error("futures_follow ENTRY ABORTED — could not resolve NIFTY future contract")
            self._notify(f"🚫 {STRATEGY_NAME} ENTRY ABORTED — no NIFTY future contract resolved")
            return []

        entry_price = self._resolve_entry_price(contract)
        if entry_price is None or entry_price <= 0:
            logger.error("futures_follow ENTRY ABORTED — no price for %s", contract.get("symbol"))
            self._notify(
                f"🚫 {STRATEGY_NAME} ENTRY ABORTED — no price for {contract.get('symbol')}"
            )
            return []

        signals = self.evaluate_signals()
        cap_margin = self._effective_cap_margin(decision)
        lot_margin = self.lot_margin_estimate()
        # Greedy in vol-ratio order (signals already sorted vol-ratio-desc), filling
        # 1 lot/signal up to the cap; signals beyond the cap are skipped.
        lots_filled = self.lots_held()  # count any positions held into the session
        placed: list[dict] = []
        skipped = 0
        for sig in signals:
            lots = compute_lots_to_buy(
                lots_filled,
                self.config.capital_inr,
                lot_margin,
                cap_margin_pct=cap_margin / self.config.capital_inr
                if self.config.capital_inr > 0
                else 0.0,
                lots_per_signal=self.config.lots_per_signal,
            )
            if lots <= 0:
                skipped += 1
                logger.info(
                    "futures_follow CAP HIT — skipping signal %s (margin cap %.0f)",
                    sig.get("symbol"),
                    cap_margin,
                )
                continue
            r = self.place_entry(sig, contract, lots, entry_price)
            if r:
                placed.append(r)
                lots_filled += lots
        logger.info(
            "futures_follow entry job placed %d lot-order(s), skipped %d (cap), [mode=%s]",
            len(placed),
            skipped,
            self.mode,
        )
        if placed:
            self._notify(
                f"📈 {STRATEGY_NAME} [{self.mode}] bought "
                f"{sum(p['lots'] for p in placed)} NIFTY lot(s) on "
                + ", ".join(p["signal_symbol"] or "?" for p in placed)
                + (f" (skipped {skipped} at 50% cap)" if skipped else "")
            )
        return placed

    def run_exit(self) -> list[dict]:
        """15:25 IST: square off every position opened on a prior trading day (T+1).
        Exits are NEVER gated by an override — a held position must square off."""
        decision = self._resolve_decision()
        self._warn_if_stale_for_exit()
        self._apply_mode_override(decision)
        today = self._now().date().isoformat()
        to_exit = [(pid, p) for pid, p in list(self.paper_book.items()) if p.entry_date != today]
        exited: list[dict] = []
        for pid, pos in to_exit:
            price = self._resolve_exit_price(pos)
            r = self.place_exit(pid, pos, price=price)
            if r:
                exited.append(r)
        logger.info("futures_follow exit job squared off %d position(s)", len(exited))
        if exited:
            net = sum(e["net_pnl"] for e in exited)
            self._notify(
                f"📉 {STRATEGY_NAME} [{self.mode}] T+1 exits: {len(exited)} position(s), "
                f"net ₹{net:+,.0f}"
            )
        return exited

    # ----- price helpers ------------------------------------------------- #
    def _resolve_entry_price(self, contract: dict) -> float | None:
        """Live price for the resolved contract (best-effort). None on failure."""
        try:
            return self._price_fetcher(contract["symbol"], self.config.exchange)
        except Exception:
            logger.debug("futures_follow entry price fetch failed", exc_info=True)
            return None

    def _resolve_exit_price(self, position: FuturesPosition) -> float | None:
        """Live price for an open position at exit (best-effort). Falls back to the
        entry price when the quote is unavailable (the actual fill is MARKET)."""
        try:
            p = self._price_fetcher(position.nifty_symbol, self.config.exchange)
            return p if (p and p > 0) else position.entry_price
        except Exception:
            logger.debug("futures_follow exit price fetch failed", exc_info=True)
            return position.entry_price

    # ----- data-freshness gate ------------------------------------------- #
    def _data_is_fresh_for_entry(self) -> bool:
        """True iff the (sector_follow) feed is fresh enough to place entries."""
        if self._data_health_checker is None or not data_freshness_enabled():
            return True
        today = self._now().date().isoformat()
        ok, details = self._data_health_checker("sector_follow_cap5_vol", today)
        if ok:
            return True
        stale = sorted(s for s, d in details.items() if not d.get("ok", True))
        logger.error("futures_follow ENTRY ABORTED — stale data: %s", stale)
        try:
            self._notify(
                f"🚫 {STRATEGY_NAME} ENTRY ABORTED — stale data ({today}): " + ", ".join(stale)
            )
        except Exception:
            logger.exception("futures_follow stale-entry alert failed")
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
            logger.exception("futures_follow exit freshness check raised (ignored)")
            return
        if not ok:
            stale = sorted(s for s, d in details.items() if not d.get("ok", True))
            logger.warning("futures_follow exits proceeding despite stale index data: %s", stale)

    # ----- 15:18 pre-entry smoke check (#292) ---------------------------- #
    def assert_data_pipeline_healthy(self) -> tuple[bool, dict]:
        """15:18 IST pre-entry smoke check for futures_follow_cap50.

        Two checks (self-contained — no aggregator, no historify probe, because
        futures_follow does not own any historical data pipeline of its own):

        1. **Data freshness**: the sector_follow_cap5_vol feed (which supplies our
           signal evaluator) is fresh for today's date, via the existing
           ``self._data_health_checker``.
        2. **Broker session live**: an API key is configured (operator logged in).

        On failure it writes a same-day ``pause`` runtime override (which the
        engine's ``_entry_held_by_override`` gate honors, blocking the 15:20
        entries) and Telegrams the operator. The override expires at 15:30 IST so
        it self-clears and never disables the strategy beyond today.

        Gated by ``FUTURES_FOLLOW_SMOKE_CHECK_ENABLED`` (default true) AND
        ``DATA_FRESHNESS_VALIDATION_ENABLED`` (the freshness arm is skipped when
        the master freshness flag is off). Returns ``(ok, details)``. Never
        raises — a check failure is logged + alerted, not an exception.
        """
        as_of = self._now()
        as_of_date = as_of.astimezone(_IST).date()

        if not futures_smoke_check_enabled():
            logger.debug("futures_follow 15:18 smoke check skipped (flag off)")
            return True, {"skipped": True}

        # ---- Check 1: data freshness (via the shared sector_follow checker) ---- #
        data_ok = True
        stale_symbols: list[str] = []
        if self._data_health_checker is not None and data_freshness_enabled():
            today_str = as_of_date.isoformat()
            try:
                data_ok, details_map = self._data_health_checker(
                    "sector_follow_cap5_vol", today_str
                )
                if not data_ok:
                    stale_symbols = sorted(
                        s for s, d in details_map.items() if not d.get("ok", True)
                    )
            except Exception:
                logger.exception("futures_follow smoke check: data-freshness probe raised")
                data_ok = True  # fail-open on infrastructure error

        # ---- Check 2: broker session live ---- #
        try:
            session_ok = bool(self._broker_session_checker())
        except Exception:
            logger.exception("futures_follow smoke check: broker-session probe raised")
            session_ok = False

        ok = data_ok and session_ok
        details: dict = {
            "data_ok": data_ok,
            "stale_symbols": stale_symbols,
            "broker_session_ok": session_ok,
        }

        if ok:
            logger.info("futures_follow 15:18 smoke check PASSED: %s", details)
            return True, details

        reasons = []
        if not data_ok:
            reasons.append(
                f"sector_follow feed stale ({today_str})"
                + (f": {stale_symbols}" if stale_symbols else "")
            )
        if not session_ok:
            reasons.append("broker session not live")
        reason = "; ".join(reasons)
        logger.error("futures_follow 15:18 SMOKE CHECK FAILED: %s", reason)

        # Hold today's 15:20 entries via a self-expiring pause override (expires 15:30).
        expires_ist = as_of.replace(hour=15, minute=30, second=0, microsecond=0)
        self._set_runtime_override("pause", expires_ist, f"smoke_check_failed: {reason}")
        try:
            self._notify(
                f"\U0001f6a8 {STRATEGY_NAME} 15:18 SMOKE CHECK FAILED "
                f"({as_of_date.isoformat()}): {reason}. Holding today's 15:20 entries "
                "(self-clears at 15:30)."
            )
        except Exception:
            logger.exception("futures_follow smoke-check alert failed")
        return False, details

    def run_daily_reset(self) -> None:
        """09:00 IST: reset kill switch + daily P&L."""
        self.reset_daily_state()

    # ----- boot durability (both modes) ---------------------------------- #
    def rehydrate_paper_book_from_store(self) -> int:
        """Rebuild ``paper_book`` from the mode-appropriate position store on boot (#265).

        A restart otherwise loses the in-memory ``paper_book``, stranding an open
        overnight NIFTY-futures long with no scheduled T+1 exit — in EITHER mode
        (a sandbox restart strands the sandbox.db paper leg exactly as a live
        restart strands the real broker leg). This reads the mode-appropriate
        store via ``get_positionbook`` (mode-aware: ``sandbox.db`` in sandbox, the
        broker positionbook in live) and reconstructs a position for every open
        NIFTY*FUT leg on NFO/NRML that the ``paper_book`` doesn't already know
        about, so the 15:20/15:25/15:14 exit jobs will square it off.

        Returns the number of positions rehydrated. Never raises.
        """
        try:
            from services.positionbook_service import get_positionbook

            api_key = _resolve_exit_api_key()
            if not api_key:
                logger.warning("futures_follow rehydrate: no api_key; skipping")
                return 0
            success, resp, _ = get_positionbook(api_key=api_key)
            if not success or not isinstance(resp, dict):
                logger.warning("futures_follow rehydrate: positionbook fetch failed: %r", resp)
                return 0
            positions = resp.get("data") or []
        except Exception:
            logger.exception("futures_follow rehydrate: positionbook read raised")
            return 0

        exchange = self.config.exchange.upper()
        product = self.config.product.upper()
        underlying = self.config.underlying.upper()
        lot_size = self.config.nifty_lot_size
        # Symbols already tracked in memory — do not double-count them.
        known_symbols = {p.nifty_symbol for p in self.paper_book.values()}
        rehydrated = 0
        today = self._now().date().isoformat()
        # Rehydrated positions are stamped with YESTERDAY's date so the T+1 exit
        # jobs (which square off positions whose entry_date != today) act on them.
        prior_day = (self._now().date() - timedelta(days=1)).isoformat()

        for pos in positions:
            try:
                raw_qty = pos.get("quantity") or pos.get("netqty") or pos.get("net_qty") or 0
                qty = int(float(raw_qty))
            except (TypeError, ValueError):
                continue
            if qty <= 0:  # only long NIFTY-futures legs are ours
                continue
            symbol = str(pos.get("symbol", "")).strip()
            pos_exchange = str(pos.get("exchange", "")).strip().upper()
            pos_product = str(pos.get("product", "")).strip().upper()
            if pos_exchange and pos_exchange != exchange:
                continue
            if pos_product and pos_product != product:
                continue
            # Only NIFTY futures (e.g. NIFTY30JUN26FUT), not options / other names.
            if not (symbol.startswith(underlying) and symbol.endswith("FUT")):
                continue
            if symbol in known_symbols:
                continue

            try:
                entry_price = float(pos.get("average_price") or pos.get("avgprice") or 0.0)
            except (TypeError, ValueError):
                entry_price = 0.0
            lots = max(1, qty // lot_size) if lot_size else 1
            pos_id = f"rehydrated:{symbol}:{uuid.uuid4().hex[:8]}"
            self.paper_book[pos_id] = FuturesPosition(
                nifty_symbol=symbol,
                lots=lots,
                quantity=qty,
                entry_price=entry_price,
                entry_date=prior_day,
                vol_ratio=0.0,
                margin_inr=lots * self.lot_margin_estimate(),
                signal_symbol="rehydrated",
            )
            known_symbols.add(symbol)
            rehydrated += 1
            logger.warning(
                "futures_follow REHYDRATED open %s position %s qty=%d lots=%d "
                "(entry_date stamped %s, T+1 exit due today %s)",
                self.mode,
                symbol,
                qty,
                lots,
                prior_day,
                today,
            )

        if rehydrated:
            try:
                self._notify(
                    f"♻️ {STRATEGY_NAME} [{self.mode}] rehydrated {rehydrated} open "
                    f"position(s) on boot — T+1 exit scheduled"
                )
            except Exception:
                logger.exception("futures_follow rehydrate notify failed")
        return rehydrated

    # ----- observability + EOD summary ----------------------------------- #
    def _config_view(self) -> dict:
        c = self.config
        return {
            "capital_inr": c.capital_inr,
            "cap_margin_pct": c.cap_margin_pct,
            "cap_margin_inr": c.cap_margin_inr,
            "nifty_lot_size": c.nifty_lot_size,
            "nifty_lot_margin_inr": c.nifty_lot_margin_inr,
            "lots_per_signal": c.lots_per_signal,
            "max_signals_per_day": c.max_signals_per_day,
            "daily_loss_kill_pct": c.daily_loss_kill_pct,
            "underlying": c.underlying,
            "exchange": c.exchange,
            "product": c.product,
        }

    def _compute_mtm(self, position: FuturesPosition) -> dict:
        """Live mark-to-market for one open position (gross + net of charges)."""
        try:
            price = self._price_fetcher(position.nifty_symbol, self.config.exchange)
        except Exception as e:
            logger.debug("mtm price fetch raised for %s: %s", position.nifty_symbol, e)
            price = None
        if price is None or price <= 0:
            return {
                "current_price": None,
                "mtm_pnl_gross": None,
                "mtm_pnl_net": None,
                "mtm_error": "price_unavailable",
            }
        gross = (price - position.entry_price) * position.quantity
        charges = compute_futures_charges(
            position.entry_price * position.quantity, price * position.quantity
        )
        return {
            "current_price": price,
            "mtm_pnl_gross": gross,
            "mtm_pnl_net": gross - charges,
            "mtm_error": None,
        }

    def open_positions_view(self) -> list[dict]:
        """Open positions as JSON dicts, each with live MTM (gross + net)."""
        out: list[dict] = []
        for pid, p in self.paper_book.items():
            mtm = self._compute_mtm(p)
            out.append(
                {
                    "pos_id": pid,
                    "nifty_symbol": p.nifty_symbol,
                    "signal_symbol": p.signal_symbol,
                    "entry_date": p.entry_date,
                    "entry_price": p.entry_price,
                    "lots": p.lots,
                    "qty": p.quantity,
                    "margin_inr": p.margin_inr,
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
        """Current strategy state for the observability endpoint."""
        open_positions = self.open_positions_view()
        realized_net = sum(e.get("net_pnl", 0.0) for e in self.today_exits)
        unrealized_net = sum(
            p["mtm_pnl_net"] for p in open_positions if p.get("mtm_pnl_net") is not None
        )
        margin_used = self.margin_used()
        return {
            "mode": self.mode,
            "strategy_id": self.strategy_id,
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_reason": self.kill_switch_reason,
            "manual_pause": self.manual_pause,
            "lots_held": self.lots_held(),
            "margin_used_inr": margin_used,
            "margin_cap_inr": self.config.cap_margin_inr,
            "margin_used_pct_of_capital": (
                100.0 * margin_used / self.config.capital_inr
                if self.config.capital_inr > 0
                else 0.0
            ),
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
        """Emergency square-off of every open position (mode-aware). Not blocked by
        the kill switch / pause."""
        results: list[dict] = []
        for pid, pos in list(self.paper_book.items()):
            try:
                price = self._resolve_exit_price(pos)
                r = self.place_exit(pid, pos, price=price)
                results.append(
                    {
                        "nifty_symbol": pos.nifty_symbol,
                        "status": "success" if r else "error",
                        "order_id": (r or {}).get("order_id"),
                    }
                )
            except Exception as e:
                logger.exception("close_all failed for %s: %s", pos.nifty_symbol, e)
                results.append(
                    {"nifty_symbol": pos.nifty_symbol, "status": "error", "message": str(e)}
                )
        logger.warning("futures_follow close_all squared %d position(s)", len(results))
        return results

    def build_eod_summary(self, as_of: datetime | None = None) -> str:
        """Format the 15:30 IST EOD Telegram summary string."""
        as_of = as_of or self._now()
        entries = self.today_entries
        exits = self.today_exits
        open_pos = list(self.paper_book.values())
        today_pnl_net = sum(e.get("net_pnl", 0.0) for e in exits)
        lots_bought = sum(e.get("lots", 0) for e in entries)
        entry_syms = ", ".join(e.get("signal_symbol") or "?" for e in entries) or "—"
        next_day = (as_of.date() + timedelta(days=1)).isoformat()
        ks = f"active ({self.kill_switch_reason})" if self.kill_switch_active else "inactive"
        pause = " · PAUSED" if self.manual_pause else ""
        return (
            f"📊 {STRATEGY_NAME} EOD {as_of.date().isoformat()}\n"
            f"Mode: {self.mode}{pause}\n"
            f"Lots bought: {lots_bought} on signals ({entry_syms})\n"
            f"Margin used: ₹{self.margin_used():,.0f} of ₹{self.config.cap_margin_inr:,.0f} cap\n"
            f"Exits: {len(exits)}\n"
            f"Open EOD: {len(open_pos)} (T+1 exit {next_day})\n"
            f"Net PnL today: ₹{today_pnl_net:+,.0f}\n"
            f"Kill switch: {ks}"
        )

    def _format_eod_report_markdown(
        self, journal_rows: list, positions: list, kill_switch_state: dict
    ) -> str:
        """Build the Day-N markdown EOD report mirroring the Telegram summary."""
        as_of = self._now()
        date_str = as_of.date().isoformat()
        next_day = (as_of.date() + timedelta(days=1)).isoformat()

        entries = [r for r in journal_rows if "exit_time" not in r]
        exits = [r for r in journal_rows if "exit_time" in r]
        realized_pnl = sum(x.get("net_pnl", 0.0) for x in exits)
        margin_deployed = sum((e.get("margin_inr") or 0.0) for e in entries)
        lots_bought = sum((e.get("lots") or 0) for e in entries)

        ks_active = kill_switch_state.get("active", False)
        ks_reason = kill_switch_state.get("reason")
        ks_line = f"active ({ks_reason})" if ks_active else "inactive"

        lines: list[str] = []
        lines.append(f"# {STRATEGY_NAME} — EOD Report {date_str}")
        lines.append("")
        lines.append(f"- **Mode:** {self.mode}")
        lines.append(f"- **Generated:** {as_of.isoformat()}")
        if self.manual_pause:
            lines.append("- **Manual pause:** active")
        lines.append("")

        lines.append("## Summary")
        lines.append("")
        lines.append(f"- NIFTY lots bought: {lots_bought} (1 lot/signal up to the 50% cap)")
        lines.append(f"- Open at EOD: {len(positions)} (T+1 exit {next_day})")
        lines.append(f"- Exits today: {len(exits)}")
        lines.append(
            f"- Overnight margin deployed: ₹{margin_deployed:,.0f} "
            f"(cap ₹{self.config.cap_margin_inr:,.0f} = {self.config.cap_margin_pct:.0%})"
        )
        if exits:
            lines.append(f"- Realized net P&L: ₹{realized_pnl:+,.0f}")
        else:
            lines.append("- Realized net P&L: — (no exits today)")
        lines.append("")

        lines.append("## Positions")
        lines.append("")
        lines.append("| NIFTY contract | Signal | Lots | Entry ₹ | Status | Exit ₹ | Net P&L ₹ |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for p in positions:
            mtm = p.get("mtm_pnl_net")
            entry = p.get("entry_price")
            entry_str = f"{entry:,.2f}" if isinstance(entry, (int, float)) else "—"
            pnl_str = f"{mtm:+,.0f} (unrl)" if isinstance(mtm, (int, float)) else "— (unrl)"
            lines.append(
                f"| {p.get('nifty_symbol')} | {p.get('signal_symbol') or '—'} | "
                f"{p.get('lots')} | {entry_str} | OPEN | — | {pnl_str} |"
            )
        for x in exits:
            entry = (
                x.get("exit_price") - x.get("gross_pnl", 0.0) / x.get("qty")
                if x.get("qty")
                else None
            )
            entry_str = f"{entry:,.2f}" if entry is not None else "—"
            exit_price = x.get("exit_price")
            exit_str = f"{exit_price:,.2f}" if isinstance(exit_price, (int, float)) else "—"
            lines.append(
                f"| {x.get('nifty_symbol')} | {x.get('signal_symbol') or '—'} | "
                f"{x.get('lots')} | {entry_str} | CLOSED | {exit_str} | "
                f"{x.get('net_pnl', 0.0):+,.0f} |"
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

        lines.append("## Note — leveraged beta, not alpha")
        lines.append("")
        lines.append(
            "> Backtest (NIFTY-only CAP50): CAGR 14.44%, Sharpe 1.27, MaxDD −8.0% on ₹10L. "
            "The signal does NOT predict NIFTY direction (hit-rate 53.4%, corr 0.295) — the "
            "return is leveraged broad-market drift on bullish signal-days, NOT stock-selection "
            "alpha. A flat/bear NIFTY year has no edge to fall back on. This report is "
            "observational — it does NOT recompute Sharpe."
        )
        lines.append("")
        return "\n".join(lines)

    def _write_eod_report(self) -> Path:
        """Write the Day-N markdown report to ``eod_reports/YYYY-MM-DD.md``."""
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
        logger.info("futures_follow EOD report written to %s", out_path)
        return out_path

    def run_eod_summary(self) -> str:
        """15:30 IST: write the Day-N markdown report AND broadcast the Telegram
        summary. The two sinks are independent (best-effort)."""
        msg = self.build_eod_summary()
        try:
            self._write_eod_report()
        except Exception as e:
            logger.exception("futures_follow EOD report file sink failed: %s", e)
        try:
            self._notify(msg)
        except Exception as e:
            logger.exception("futures_follow EOD Telegram summary failed: %s", e)
        logger.info("futures_follow EOD summary emitted")
        return msg

    # ----- EOD watchdog (tick-independent flatten backstop) -------------- #
    def run_eod_watchdog(self) -> list[dict]:
        """15:14 IST: tick-independent backstop that flattens any still-open T+1
        position the scheduled 15:25 exit would handle, BEFORE the broker MIS
        auto-square-off window. NRML futures are not MIS-squared-off, but the
        watchdog guarantees a held position is flattened even if the 15:25 job or
        the tick stream fails. Exits are never gated."""
        today = self._now().date().isoformat()
        to_flatten = [(pid, p) for pid, p in list(self.paper_book.items()) if p.entry_date != today]
        if not to_flatten:
            return []
        logger.warning("futures_follow EOD watchdog flattening %d position(s)", len(to_flatten))
        flattened: list[dict] = []
        for pid, pos in to_flatten:
            price = self._resolve_exit_price(pos)
            r = self.place_exit(pid, pos, price=price)
            if r:
                flattened.append(r)
        if flattened:
            self._notify(f"🛡️ {STRATEGY_NAME} EOD watchdog flattened {len(flattened)} position(s)")
        return flattened

    # ----- scheduler registration ---------------------------------------- #
    def register_jobs(self, scheduler=None) -> None:
        """Register entry/exit/reset/EOD/watchdog jobs on the shared APScheduler."""
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
            _daily_reset_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone="Asia/Kolkata"),
            id="futures_follow_daily_reset",
            replace_existing=True,
            name="Futures Follow CAP50 daily reset (09:00 IST)",
        )
        sched.add_job(
            _watchdog_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=14, timezone="Asia/Kolkata"),
            id="futures_follow_eod_watchdog",
            replace_existing=True,
            name="Futures Follow CAP50 EOD watchdog (15:14 IST)",
        )
        sched.add_job(
            _entry_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=20, timezone="Asia/Kolkata"),
            id="futures_follow_entry",
            replace_existing=True,
            name="Futures Follow CAP50 entry (15:20 IST)",
        )
        sched.add_job(
            _exit_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=25, timezone="Asia/Kolkata"),
            id="futures_follow_exit",
            replace_existing=True,
            name="Futures Follow CAP50 T+1 exit (15:25 IST)",
        )
        sched.add_job(
            _eod_summary_job,
            trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone="Asia/Kolkata"),
            id="futures_follow_eod_summary",
            replace_existing=True,
            name="Futures Follow CAP50 EOD summary (15:30 IST)",
        )
        if futures_smoke_check_enabled():
            sched.add_job(
                _smoke_check_job,
                trigger=CronTrigger(
                    day_of_week="mon-fri", hour=15, minute=18, timezone="Asia/Kolkata"
                ),
                id="futures_follow_smoke_check",
                replace_existing=True,
                name="Futures Follow CAP50 pre-entry smoke check (15:18 IST)",
            )
        logger.info(
            "futures_follow jobs registered (mode=%s, strategy_id=%s, smoke_check=%s)",
            self.mode,
            self.strategy_id,
            futures_smoke_check_enabled(),
        )


# --------------------------------------------------------------------------- #
# Module-level scheduler entry points + singleton (serializable for jobstore)
# --------------------------------------------------------------------------- #
_SINGLETON: FuturesFollowService | None = None


def get_service() -> FuturesFollowService | None:
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


def _watchdog_job() -> None:
    if _SINGLETON is not None:
        _SINGLETON.run_eod_watchdog()


def _smoke_check_job() -> None:
    """15:18 IST: run the data + broker-session smoke check.

    Swallows all exceptions so a bug in the smoke check can never crash the
    APScheduler thread and interrupt other scheduled jobs."""
    if _SINGLETON is not None:
        try:
            _SINGLETON.assert_data_pipeline_healthy()
        except Exception:
            logger.exception("futures_follow smoke-check job raised unexpectedly (ignored)")


def init_futures_follow_service(app=None, scheduler=None) -> FuturesFollowService:
    """Build the singleton and register its scheduler jobs. Default mode=sandbox so
    the strategy actively trades the virtual ₹1Cr sandbox book from boot (no live
    broker orders until the operator flips mode=live)."""
    svc = FuturesFollowService(
        app=app,
        scheduler=scheduler,
        data_health_checker=production_data_health_checker,
        broker_session_checker=production_broker_session_checker,
    )
    svc.register_jobs(scheduler)
    # Boot durability (#265, BOTH modes): rebuild paper_book from the
    # mode-appropriate position store (sandbox.db in sandbox, broker in live) so a
    # restart can't strand an open overnight NIFTY-futures long. Best-effort — a
    # rehydrate failure never blocks boot.
    try:
        svc.rehydrate_paper_book_from_store()
    except Exception:
        logger.exception("futures_follow boot rehydrate failed (ignored)")
    if app is not None:
        app.futures_follow_service = svc
    return svc
