"""Futures Follow CAP50 strategy service.

A **leveraged broad-market-beta** sleeve. At 15:20 IST it reuses the
``sector_follow_cap5_vol`` C1×W2+E4 signal evaluator to find today's ≤5 gate-passing
stock signals, and for each signal — greedily in vol-ratio order — buys **one
NIFTY near-month index future lot**, subject to a HARD CAP of 50% of capital as
overnight SPAN margin. Positions are held to the **next trading day 15:25 IST**
(T+1) MARKET sell. NO stop loss (Phase-1 proved hard stops are net-negative on this
signal class); the EOD watchdog at 15:28 IST — AFTER the 15:25 primary exit,
before the 15:30 NFO close — is the only safety backstop (#334).

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
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
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

# R3 (#318): cumulative wall-clock budget for the Stage-1 LLM reviews inside one
# 15:20 entry batch. Worst case 5 signals × VETO_CLAUDE_TIMEOUT_SECONDS (25s)
# ≈ 125s sequential; once the batch has spent this budget the remaining signals
# are placed UNREVIEWED with a WARNING (fail-open — MARKET fills must land well
# before the close). Code constant by design (no new env var per the review).
VETO_REVIEW_BUDGET_SECONDS = 180.0


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
def futures_intraday_source() -> str:
    """``FUTURES_FOLLOW_INTRADAY_SOURCE`` env flag: ``quotes`` (default) |
    ``aggregator`` (issue #332).

    ``quotes``: today's per-symbol (close, volume) comes from ONE batched broker
    quote snapshot at decision time (``get_multiquotes``) — the single-decision
    strategy no longer depends on the all-day WS-fed scanner aggregator being
    healthy at 15:20. ``aggregator``: the pre-#332 behavior (byte-identical).
    Unknown values fall back to ``quotes`` with a WARNING. Applies ONLY to
    futures_follow_cap50 — the sector_follow_cap5_vol equity book is untouched.
    """
    val = os.getenv("FUTURES_FOLLOW_INTRADAY_SOURCE", "quotes").strip().lower()
    if val not in ("quotes", "aggregator"):
        logger.warning("Unknown FUTURES_FOLLOW_INTRADAY_SOURCE=%s — falling back to 'quotes'", val)
        return "quotes"
    return val


def production_signal_evaluator(as_of: datetime | None = None) -> list[dict]:
    """Today's ≤5 gate-passing stock signals, via the LIVE sector_follow evaluator.

    Reuses ``services.sector_follow_service`` (the canonical production gate
    evaluator) — config, sector map, metrics provider, ``passes_gates`` and
    ``select_entries`` — so the futures sleeve fires on exactly the signal set the
    equity book sees. Each returned dict carries ``symbol`` + ``vol_ratio`` (the
    greedy ordering key for the margin cap). Lazily imported so importing this
    module never pulls the DuckDB stack.

    Issue #332: today's intraday (close, volume) source is selected by
    ``FUTURES_FOLLOW_INTRADAY_SOURCE``. With ``quotes`` (default) the metrics
    provider is built around the batched quotes-snapshot intraday provider
    (fallback chain quotes → aggregator → historify → none, WARNING per hop);
    with ``aggregator`` the pre-#332 ``duckdb_metrics_provider`` path runs
    unchanged. Gate logic, selection (K5 / vol-ratio greedy), and sizing are
    identical in both cases.

    NOTE: the production sector_follow evaluator ships the C1 base gate; the
    W2+E4 refinements that define the backtested "C1×W2+E4 K5" set live in the
    research harness, exactly as for sector_follow itself. The futures sleeve
    inherits whatever the live evaluator produces — see LEARNINGS.md.
    """
    from services.sector_follow_service import (
        duckdb_metrics_provider,
        make_duckdb_metrics_provider,
        make_quotes_intraday_provider,
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
    source = futures_intraday_source()
    if source == "quotes":
        quotes_provider = make_quotes_intraday_provider(
            universe=sf_config.universe, sector_map=sector_map
        )
        metrics_provider = make_duckdb_metrics_provider(intraday_provider=quotes_provider)
        metrics = metrics_provider(as_of, sf_config.universe, sector_map, sf_config)
        # Observability (issue #332): one INFO line per eval summarizing where
        # each symbol's intraday data actually came from.
        n_by_src = {"quotes": 0, "aggregator": 0, "historify": 0, "none": 0}
        for m in metrics.values():
            src = m.get("intraday_source")
            if src in n_by_src:
                n_by_src[src] += 1
        logger.info(
            "futures_follow intraday source=quotes fetched=%d/%d fallback_aggregator=%d "
            "fallback_historify=%d none=%d",
            n_by_src["quotes"],
            len(metrics),
            n_by_src["aggregator"],
            n_by_src["historify"],
            n_by_src["none"],
        )
    else:
        # aggregator: the pre-#332 path, byte-identical.
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


def production_signal_evaluator_details(as_of: datetime | None = None) -> dict:
    """Per-symbol decision-input breakdown for the SAME evaluation cycle
    ``production_signal_evaluator`` runs (issue #352).

    STRICTLY additive / observability-only — this rebuilds the identical metrics
    dict (same config, sector map, intraday source, metrics provider) that
    ``production_signal_evaluator`` uses and reports gate-level detail for every
    universe symbol. It does NOT feed back into gate logic, signal selection, or
    order placement; ``run_entry`` calls this separately (best-effort, after
    ``evaluate_signals``) purely to persist "why zero/N signals today".

    Returns ``{n_by_source: {...}, symbols: [{symbol, sector_index, sector_ret,
    stock_ret, vol_ratio, current_price, intraday_source, passed, fail_reason}]}``.
    """
    from services.sector_follow_service import (
        _gate_fail_reason,
        duckdb_metrics_provider,
        make_duckdb_metrics_provider,
        make_quotes_intraday_provider,
        passes_gates,
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
    source = futures_intraday_source()
    if source == "quotes":
        quotes_provider = make_quotes_intraday_provider(
            universe=sf_config.universe, sector_map=sector_map
        )
        metrics_provider = make_duckdb_metrics_provider(intraday_provider=quotes_provider)
        metrics = metrics_provider(as_of, sf_config.universe, sector_map, sf_config)
    else:
        metrics = duckdb_metrics_provider(as_of, sf_config.universe, sector_map, sf_config)

    n_by_src = {"quotes": 0, "aggregator": 0, "historify": 0, "none": 0}
    symbols: list[dict] = []
    for symbol in sf_config.universe:
        m = metrics.get(symbol, {})
        src = m.get("intraday_source")
        if src in n_by_src:
            n_by_src[src] += 1
        passed = passes_gates(m, sf_config)
        symbols.append(
            {
                "symbol": symbol,
                "sector_index": sector_map.get(symbol, "NIFTY"),
                "sector_ret": m.get("sector_ret"),
                "stock_ret": m.get("stock_ret"),
                "vol_ratio": m.get("vol_ratio"),
                "current_price": m.get("current_price"),
                "intraday_source": src,
                "passed": passed,
                "fail_reason": None if passed else _gate_fail_reason(m, sf_config),
            }
        )
    return {"n_by_source": n_by_src, "symbols": symbols}


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
    success, response, _ = place_order(payload, api_key=api_key, mode_key=STRATEGY_NAME)
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


def production_news_context() -> str:
    """Injected default: recent market headlines for big-loss/kill-switch alerts.

    Read-only, fail-open, informational — see ``news_context_service``. Never
    triggers a trade; the headlines are DATA embedded in the operator alert only."""
    try:
        from services.news_context_service import get_recent_news_context

        return get_recent_news_context()
    except Exception:
        logger.debug("news context provider unavailable", exc_info=True)
        return ""


def big_loss_alert_pct() -> float:
    """``FUTURES_FOLLOW_BIG_LOSS_ALERT_PCT`` — realized daily loss (percent of
    capital) that triggers the news-enriched big-loss alert. Default 2.0."""
    try:
        return float(os.getenv("FUTURES_FOLLOW_BIG_LOSS_ALERT_PCT", "2.0"))
    except ValueError:
        logger.warning("bad FUTURES_FOLLOW_BIG_LOSS_ALERT_PCT — using 2.0")
        return 2.0


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


_DEFAULT_ENTRY_DEADLINE = "15:28"


def futures_entry_deadline_ist() -> dt_time:
    """``FUTURES_FOLLOW_ENTRY_DEADLINE_IST`` env tunable (HH:MM IST, default 15:28).

    Wall-clock deadline for the 15:20 entry job (#332 follow-up). A misfired
    cron (app down at 15:20 → APScheduler fires it on restart) must never place
    entries near/after the 15:30 NFO close: at ~15:35 the exchange rejects the
    order; after ~15:45 it could queue as an AMO and execute at tomorrow's open
    — an unintended overnight entry. Consult-time (re-read every fire).
    Fail-safe parse: a malformed value falls back to 15:28 with a WARNING — a
    typo must never disable the guard. No early-side check (cron cannot fire
    early; misfires only fire late).
    """
    raw = os.getenv("FUTURES_FOLLOW_ENTRY_DEADLINE_IST", _DEFAULT_ENTRY_DEADLINE)
    try:
        hh, mm = str(raw).strip().split(":")
        return dt_time(int(hh), int(mm))
    except (ValueError, TypeError):
        logger.warning(
            "Malformed FUTURES_FOLLOW_ENTRY_DEADLINE_IST=%r — falling back to %s",
            raw,
            _DEFAULT_ENTRY_DEADLINE,
        )
        hh, mm = _DEFAULT_ENTRY_DEADLINE.split(":")
        return dt_time(int(hh), int(mm))


def futures_smoke_check_enabled() -> bool:
    """``FUTURES_FOLLOW_SMOKE_CHECK_ENABLED`` env flag (default true).

    When off the 15:18 pre-entry smoke check is a no-op (returns ok=True, no
    override written). Set ``false`` only to disable the guard entirely — stale
    data at 15:20 will then be caught only by the per-entry
    ``_data_is_fresh_for_entry`` gate (which still blocks but does NOT alert)."""
    return os.getenv("FUTURES_FOLLOW_SMOKE_CHECK_ENABLED", "true").lower() == "true"


def futures_entry_mode() -> str:
    """``FUTURES_FOLLOW_ENTRY_MODE`` env flag — ``legacy`` (default) or ``same_minute``.

    ``legacy`` — the shipped behavior: entry job at 15:20, T+1 exit at 15:25. The
    still-open prior-day lot occupies the 50% cap at 15:20, under-sizing carry days
    (issue #405).

    ``same_minute`` — OPTION_C (issue #406): 15:20 job only *snapshots* the selected
    signals; the 15:25 job squares off the T+1 exit FIRST (freeing its margin) and
    THEN places the snapshotted entries in the same minute. Selection stays at 15:20
    (backtest-faithful), execution moves to 15:25, sizing is naturally fresh-cap
    (``lots_held``==0 after the exit) and margin never overlaps (≤50%). Backtest:
    +1.19pp CAGR / +0.07 Sharpe over legacy, peak margin 49.8% (vs OPTION_A's 98.7%).
    See ``docs/research/strategy/futures_follow_cap50/2026-07-14_entry_cap_carry_sizing.md``.

    Consult-time (re-read at ``register_jobs``). Any value other than ``same_minute``
    resolves to ``legacy`` (fail-safe: a typo keeps the conservative behavior)."""
    return (
        "same_minute"
        if os.getenv("FUTURES_FOLLOW_ENTRY_MODE", "legacy").strip().lower() == "same_minute"
        else "legacy"
    )


def production_broker_session_checker() -> bool:
    """True iff a broker session (API key) is configured (operator logged in).
    Best-effort; ``False`` on any error. Used by the 15:18 smoke check."""
    try:
        from database.auth_db import get_first_available_api_key

        return bool(get_first_available_api_key())
    except Exception:
        logger.debug("futures_follow broker-session check failed", exc_info=True)
        return False


def production_quote_probe() -> bool:
    """Dry-run quote probe for the 15:18 smoke check (issue #332).

    Fetches ONE universe symbol through the same quotes-snapshot provider path
    the 15:20 evaluator uses and asserts a sane ``ltp > 0`` served by the
    ``quotes`` source (an internal fallback to the aggregator means the session
    cannot serve quotes — that must FAIL the probe at 15:18, not silently
    degrade at 15:20). ``False`` on any failure; never raises.
    """
    try:
        from services.sector_follow_service import (
            load_config as _sf_load_config,
        )
        from services.sector_follow_service import (
            make_quotes_intraday_provider,
        )

        universe = _sf_load_config().universe
        if not universe:
            logger.warning("futures_follow quote probe: empty sector_follow universe")
            return False
        sym = universe[0]
        provider = make_quotes_intraday_provider(universe=[sym], sector_map={sym: "NIFTY"})
        close, _vol, src = provider(sym, datetime.now(_IST))
        ok = src == "quotes" and close is not None and float(close) > 0
        if not ok:
            logger.warning(
                "futures_follow quote probe FAILED for %s (close=%s source=%s)",
                sym,
                close,
                src,
            )
        return ok
    except Exception:
        logger.exception("futures_follow quote probe raised")
        return False


# --------------------------------------------------------------------------- #
# Stage-1 LLM veto (issue #318) — injected defaults
# --------------------------------------------------------------------------- #
def production_signal_reviewer(**kwargs) -> dict:
    """Strategy-aware Stage-1 LLM review (issue #318). Lazily imported so
    importing this module never pulls the LLM review stack. Injected into the
    live singleton by ``init_futures_follow_service``; ``None`` in unit tests
    (veto skipped entirely, hermetic — mirrors ``data_health_checker``)."""
    from services import signal_review_service

    return signal_review_service.review_signal(**kwargs)


def production_market_context() -> dict:
    """Best-effort ``{nifty_pct, india_vix, regime_snapshot}`` for the veto
    prompt, via the shared macro fetches in ``signal_review_service``."""
    from services import signal_review_service

    return signal_review_service.build_market_context()


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
        signal_evaluator_details: Callable[..., dict] | None = None,
        contract_resolver: Callable[..., dict | None] | None = None,
        order_placer: Callable[[str, dict], dict] | None = None,
        price_fetcher: Callable[[str, str], float | None] | None = None,
        notifier: Callable[[str], None] | None = None,
        trade_recorder: Callable[..., object] | None = None,
        now: Callable[[], datetime] | None = None,
        intent_resolver: Callable[[], object] | None = None,
        data_health_checker: Callable[..., tuple] | None = None,
        broker_session_checker: Callable[[], bool] | None = None,
        quote_probe: Callable[[], bool] | None = None,
        signal_reviewer: Callable[..., dict] | None = None,
        market_context_provider: Callable[[], dict] | None = None,
        news_context_provider: Callable[[], str] | None = None,
    ):
        self.app = app
        self.scheduler = scheduler
        self.config = config if config is not None else load_config()
        self.mode = (mode or os.getenv("FUTURES_FOLLOW_MODE", "sandbox")).lower()
        if self.mode not in VALID_MODES:
            logger.warning("Unknown FUTURES_FOLLOW_MODE=%s — forcing sandbox", self.mode)
            self.mode = "sandbox"

        self._signal_evaluator = signal_evaluator or production_signal_evaluator
        # Entry-breakdown capture (issue #352). ``None`` in unit tests by default
        # (observability capture skipped, hermetic — mirrors data_health_checker);
        # the live singleton injects ``production_signal_evaluator_details``.
        self._signal_evaluator_details = signal_evaluator_details
        self._contract_resolver = contract_resolver or production_contract_resolver
        self._order_placer = order_placer or production_order_placer
        self._price_fetcher = price_fetcher or production_price_fetcher
        self._notify = notifier or telegram_notifier
        # Read-only market-news context for big-loss / kill-switch alerts
        # (informational, human-in-the-loop; issue #399). Left injectable for
        # hermetic tests; the default reads market_intel(kind='news'), fail-open.
        self._news_context = news_context_provider or production_news_context
        # Per-day dedup so the news-enriched big-loss alert fires at most once/day.
        self._big_loss_alerted_date: str | None = None
        self._record_trade = trade_recorder or self._default_trade_recorder
        self._intent_resolver = intent_resolver or production_intent_resolver
        # Data-freshness gate. Left None in unit tests (gate skipped, hermetic);
        # the live singleton injects ``production_data_health_checker``.
        self._data_health_checker = data_health_checker
        # Broker-session checker for the 15:18 smoke check. Left None in unit tests
        # (treated as live); the live singleton uses ``production_broker_session_checker``.
        self._broker_session_checker = broker_session_checker or production_broker_session_checker
        # Dry-run quote probe for the 15:18 smoke check (issue #332) — only
        # consulted when FUTURES_FOLLOW_INTRADAY_SOURCE resolves to "quotes".
        self._quote_probe = quote_probe or production_quote_probe
        # Stage-1 LLM veto (issue #318). Left None in unit tests (veto skipped,
        # hermetic — mirrors data_health_checker); the live singleton injects
        # ``production_signal_reviewer`` + ``production_market_context``.
        self._signal_reviewer = signal_reviewer
        self._market_context_provider = market_context_provider
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
        # OPTION_C same-minute@15:25 (issue #406): the 15:20 job snapshots the
        # selected signals here; the 15:25 exit-then-entry job consumes them so
        # selection stays at 15:20 (backtest-faithful) while execution moves to
        # 15:25 (after the T+1 exit frees margin). None outside same_minute mode.
        self._pending_snapshot: tuple[datetime, list[dict]] | None = None

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
            ks_msg = (
                f"🛑 {STRATEGY_NAME} kill switch fired — daily P&L "
                f"₹{self.daily_pnl:,.0f} breached {self.config.daily_loss_kill_pct}% of capital. "
                "New entries blocked; open positions hold to scheduled exit."
            )
            try:
                news = self._news_context() or ""
            except Exception:
                logger.debug("futures_follow kill-switch news context failed", exc_info=True)
                news = ""
            self._notify(f"{ks_msg}\n\n{news}" if news else ks_msg)
            self._set_runtime_override(
                "kill_switch",
                self._end_of_today_ist(),
                self.kill_switch_reason or "daily loss kill",
            )
        return self.kill_switch_active

    def _maybe_alert_big_loss(self, realized_net: float, trigger: str) -> bool:
        """Send a news-enriched operator alert when the day's realized loss crosses
        ``FUTURES_FOLLOW_BIG_LOSS_ALERT_PCT`` of capital. At most once per day.

        STRICTLY informational + human-in-the-loop: it surfaces recent market
        headlines (read-only, fail-open) so the operator can see the likely macro
        driver of a big overnight loss and decide. It NEVER places or cancels an
        order — R54/R55 proved that reacting (stop/hedge/news-sell) is net-negative
        on this leveraged-beta sleeve. Returns True iff an alert was sent."""
        thr = -(big_loss_alert_pct() / 100.0) * self.config.capital_inr
        if realized_net > thr:
            return False
        today = self._now().date().isoformat()
        if self._big_loss_alerted_date == today:
            return False
        self._big_loss_alerted_date = today
        pct = (
            abs(realized_net) / self.config.capital_inr * 100.0 if self.config.capital_inr else 0.0
        )
        header = (
            f"⚠️ {STRATEGY_NAME} [{self.mode}] BIG LOSS ₹{realized_net:+,.0f} "
            f"({pct:.1f}% of capital) via {trigger}. This sleeve is leveraged "
            "long-NIFTY beta, so a large overnight loss usually means a broad "
            "down day — review the context below and decide (no auto-action taken)."
        )
        news = ""
        try:
            news = self._news_context() or ""
        except Exception:
            logger.debug("futures_follow big-loss news context failed", exc_info=True)
        body = f"{header}\n\n{news}" if news else header
        try:
            self._notify(body)
        except Exception:
            logger.exception("futures_follow big-loss alert failed")
        logger.warning("futures_follow BIG LOSS alert: net=%.0f trigger=%s", realized_net, trigger)
        return True

    def reset_daily_state(self) -> None:
        """09:00 IST reset: clear kill switch + daily P&L and the intraday journals.

        Does NOT clear ``manual_pause`` (an operator pause persists). Open positions
        in paper_book survive (they hold to their scheduled T+1 exit)."""
        self.kill_switch_active = False
        self.kill_switch_reason = None
        self.daily_pnl = 0.0
        self.today_entries = []
        self.today_exits = []
        self._big_loss_alerted_date = None
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
        decision_id: int | None = None,
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
                decision_id=decision_id,
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
            decision_id=decision_id,
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
        positionbook in live mode. Routing comes from ``mode_key=STRATEGY_NAME``
        (issue #507) — the same key that dispatched the entry order. Returns the
        guarded SELL quantity to place, or ``None`` to SUPPRESS the exit (store
        flat / opposite side).
        """
        try:
            from services import live_position_reconciliation_service as recon

            decision = recon.reconcile_exit(
                strategy=STRATEGY_NAME,
                mode_key=STRATEGY_NAME,
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
        the broker positionbook (routed by ``mode_key=STRATEGY_NAME``, #507)."""
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
        if status == "placed":
            self.paper_book.pop(pos_id, None)
        else:
            # #334: a rejected/exception SELL keeps the position in the book so
            # the 15:28 watchdog (or the next day's exit job) retries the
            # square-off. Safe against a double SELL: _reconcile_exit_qty checks
            # the position store first and SUPPRESSES the retry if the store is
            # already flat (the order actually filled despite the error reply).
            logger.warning(
                "futures_follow EXIT %s for %s — position retained in book for the "
                "15:28 watchdog retry",
                status,
                symbol,
            )
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

    # ----- Stage-1 LLM veto (issue #318) ---------------------------------- #
    def _resolve_veto_mode(self) -> str:
        """Resolve the LLM veto enforcement mode for this strategy.

        Resolution (issue #266 Phase 2): per-strategy ``strategy_llm_config``
        row → ``VETO_LAYER_MODE`` env → mode-aware default where **sandbox =
        active/enforcing** (B4 — the veto is exercised on the virtual book
        before it ever gates live money). Returns ``'off'`` when no reviewer is
        wired (unit tests) or on any resolver error (fail-open).
        """
        if self._signal_reviewer is None:
            return "off"
        try:
            from services.signal_review_service import get_veto_layer_mode

            return get_veto_layer_mode(self.mode, strategy_name=STRATEGY_NAME)
        except Exception:
            logger.exception("futures_follow veto-mode resolve failed; failing open (off)")
            return "off"

    def _build_review_context(self, signal: dict, contract: dict) -> dict:
        """Operator + signal + market context for the strategy-aware veto prompt.

        Combines (locked operator decision, #318) the source sector_follow stock
        signal metrics AND the resolved NIFTY-future/book state from
        ``get_status()``. Best-effort — a status/market failure degrades to a
        partial context (missing fields render 'unknown'/'unavailable'), never
        raises into the entry path.
        """
        ctx: dict = {
            "vol_ratio": signal.get("vol_ratio"),
            "stock_ret": signal.get("stock_ret"),
            "sector_ret": signal.get("sector_ret"),
            "contract_symbol": contract.get("symbol"),
            "lots_held": None,
            "margin_used_inr": None,
            "margin_cap_inr": None,
            "pnl_today": None,
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_reason": self.kill_switch_reason,
            "mode": self.mode,
        }
        try:
            status = self.get_status()
            ctx["lots_held"] = status.get("lots_held")
            ctx["margin_used_inr"] = status.get("margin_used_inr")
            ctx["margin_cap_inr"] = status.get("margin_cap_inr")
            ctx["pnl_today"] = status.get("today_pnl_net")
        except Exception:
            logger.exception("futures_follow veto context: get_status failed (partial context)")
        if self._market_context_provider is not None:
            try:
                market = self._market_context_provider()
                if isinstance(market, dict):
                    ctx.update(market)
            except Exception:
                logger.exception("futures_follow veto context: market fetch failed (partial)")
        return ctx

    def _review_entry_signal(
        self,
        signal: dict,
        contract: dict,
        lots: int,
        entry_price: float,
        veto_mode: str,
        elapsed_s: float,
    ) -> tuple[bool, int | None, float]:
        """Run the Stage-1 LLM veto for one selected signal (issue #318).

        Returns ``(proceed, decision_id, cumulative_review_seconds)``:

        * ``veto_mode='off'`` / kill-switch / pause / review budget exhausted →
          ``(True, None, ...)`` — no review runs.
        * ``shadow`` → decision logged, always proceed.
        * ``active`` + ``'skip'`` → ``(False, id, ...)`` — the lot is NOT
          placed, the margin cap is NOT consumed (later signals may use it),
          the skip is journalled (``status='veto_skip'``) and
          ``actually_taken=False`` is recorded.
        * any reviewer failure → fail-open ``(True, None, ...)`` with an
          exception log — mirrors the simplified engine's failsafe philosophy.
        """
        symbol = signal.get("symbol")
        if veto_mode == "off" or self._signal_reviewer is None:
            return True, None, elapsed_s
        if self.kill_switch_active or self.manual_pause:
            # place_entry refuses these anyway — don't burn an LLM call.
            return True, None, elapsed_s
        if elapsed_s >= VETO_REVIEW_BUDGET_SECONDS:
            # R3: bounded 15:20 latency — place the remaining signals UNREVIEWED.
            logger.warning(
                "futures_follow veto review budget exhausted (%.1fs >= %.0fs) — "
                "placing %s UNREVIEWED (fail-open)",
                elapsed_s,
                VETO_REVIEW_BUDGET_SECONDS,
                symbol,
            )
            return True, None, elapsed_s
        review_s = 0.0
        try:
            context = self._build_review_context(signal, contract)
            started = time.monotonic()
            review = self._signal_reviewer(
                symbol=symbol,
                source=STRATEGY_NAME,
                direction="BUY",
                context=context,
                strategy_name=STRATEGY_NAME,
            )
            review_s = time.monotonic() - started
            elapsed_s += review_s
        except Exception:
            logger.exception("futures_follow veto layer raised for %s; failing open", symbol)
            return True, None, elapsed_s
        review = review or {}
        decision_id = review.get("id")
        decision = review.get("decision")
        reasoning = (review.get("reasoning") or "")[:160]
        # R3: per-review latency is logged so the 15:20 budget is observable.
        logger.info(
            "futures_follow veto review %s -> %s (mode=%s, %.1fs this review, "
            "%.1fs cumulative of %.0fs budget): %s",
            symbol,
            decision,
            veto_mode,
            review_s,
            elapsed_s,
            VETO_REVIEW_BUDGET_SECONDS,
            reasoning,
        )
        if veto_mode == "shadow":
            return True, decision_id, elapsed_s
        # veto_mode == "active"
        if decision == "skip":
            logger.warning(
                "futures_follow VETO SKIP %s — NIFTY lot NOT placed (margin cap not consumed): %s",
                symbol,
                reasoning,
            )
            self._journal_veto_skip(signal, contract, lots, entry_price, reasoning, decision_id)
            self._mark_review_outcome(decision_id, taken=False)
            return False, decision_id, elapsed_s
        return True, decision_id, elapsed_s

    def _journal_veto_skip(
        self,
        signal: dict,
        contract: dict,
        lots: int,
        entry_price: float,
        reasoning: str | None = None,
        decision_id: int | None = None,
    ) -> None:
        """Audit-trail a veto-skipped entry in ``futures_follow_trades``.

        ``status='veto_skip'`` with NO order id, NO margin consumed and NO
        in-memory position — a pure audit row (never a phantom position).
        """
        try:
            self._record_trade(
                side="BUY",
                nifty_symbol=contract.get("symbol"),
                lots=lots,
                quantity=lots * int(contract.get("lot_size") or self.config.nifty_lot_size),
                entry_price=entry_price,
                entry_date=self._now().date().isoformat(),
                exchange=self.config.exchange,
                product=self.config.product,
                signal_id=signal.get("signal_id") or signal.get("symbol"),
                decision_id=decision_id,
                vol_ratio=signal.get("vol_ratio") or 0.0,
                margin_inr=0.0,
                order_id=None,
                status="veto_skip",
                error_message=(reasoning or "LLM veto skip")[:255],
                note="llm_veto_skip",
            )
        except Exception:
            logger.exception(
                "futures_follow: failed to journal veto skip for %s", signal.get("symbol")
            )

    @staticmethod
    def _mark_review_outcome(decision_id: int | None, *, taken: bool) -> None:
        """Record the placement outcome on the ``signal_decision`` audit row.

        Best-effort — the order path must never break on a bookkeeping miss.
        """
        if decision_id is None:
            return
        try:
            from services import signal_review_service

            signal_review_service.mark_actually_taken(decision_id, taken)
        except Exception:
            logger.exception(
                "futures_follow: mark_actually_taken failed for decision_id=%s", decision_id
            )

    # ----- scheduled job bodies ------------------------------------------ #
    def run_entry(self, signals: list[dict] | None = None) -> list[dict]:
        """15:20 IST: evaluate signals, resolve the contract, buy 1 lot/signal up to
        the 50%-margin cap (subject to the lateness guard, override gate, kill
        switch + freshness).

        ``signals`` — when provided (OPTION_C same-minute@15:25, issue #406), these
        pre-selected 15:20 signals are used instead of re-evaluating; the caller
        (``run_exit_then_entry``) has already run the T+1 exit, so ``lots_held``==0
        and the cap is naturally fresh. ``None`` (legacy) re-evaluates now."""
        # Wall-clock entry-lateness guard (#332 follow-up) — FIRST, before any
        # gate/evaluation/order. A misfired cron (app down at 15:20 →
        # APScheduler fires on restart) must never place entries near/after the
        # 15:30 NFO close: at ~15:35 the exchange rejects; after ~15:45 the
        # order could queue as an AMO and execute at tomorrow's open. Entries
        # ONLY — run_exit / run_eod_watchdog / close_all are never gated (a
        # held T+1 position is riskier than a rejected exit order).
        now = self._now()
        now_ist = now.astimezone(_IST) if now.tzinfo else now
        deadline = futures_entry_deadline_ist()
        if now_ist.time() > deadline:
            fired_at = now_ist.strftime("%H:%M:%S")
            deadline_str = deadline.strftime("%H:%M")
            logger.error(
                "futures_follow ENTRY SKIPPED — entry job fired LATE at %s IST "
                "(deadline %s); likely restart/scheduler misfire. No orders placed.",
                fired_at,
                deadline_str,
            )
            try:
                self._notify(
                    f"🚫 {STRATEGY_NAME} entry job fired LATE at {fired_at} IST "
                    f"(deadline {deadline_str}) — skipping today's entries (likely "
                    "restart/scheduler misfire); no orders placed"
                )
            except Exception:
                logger.exception("futures_follow late-entry alert failed")
            return []

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

        signals = signals if signals is not None else self.evaluate_signals()
        veto_mode = self._resolve_veto_mode()
        if signals:
            # R2 (#318): make the resolved enforcement mode loud at every entry —
            # with no strategy_llm_config row and no VETO_LAYER_MODE env, sandbox
            # defaults to ACTIVE (enforcing) per B4. Operator disable: dashboard
            # LLM toggle (llm_mode=off) or VETO_LAYER_MODE=off.
            logger.info(
                "futures_follow Stage-1 LLM veto mode=%s for %d signal(s) [mode=%s] "
                "(sandbox defaults to ACTIVE when no llm_mode row is set — B4)",
                veto_mode,
                len(signals),
                self.mode,
            )
        cap_margin = self._effective_cap_margin(decision)
        lot_margin = self.lot_margin_estimate()
        # Greedy in vol-ratio order (signals already sorted vol-ratio-desc), filling
        # 1 lot/signal up to the cap; signals beyond the cap are skipped.
        lots_filled = self.lots_held()  # count any positions held into the session
        placed: list[dict] = []
        skipped = 0
        vetoed = 0
        review_elapsed_s = 0.0
        # Issue #352: per-symbol outcome for the entry-breakdown snapshot —
        # observability only, never consulted by the placement logic above.
        signal_outcomes: dict[str, str] = {}
        for sig in signals:
            symbol = sig.get("symbol")
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
                if symbol:
                    signal_outcomes[symbol] = "cap_skipped"
                logger.info(
                    "futures_follow CAP HIT — skipping signal %s (margin cap %.0f)",
                    sig.get("symbol"),
                    cap_margin,
                )
                continue
            # Stage-1 LLM veto (#318): an active 'skip' drops this lot WITHOUT
            # consuming the margin cap (lots_filled unchanged — later signals
            # may use the freed cap). Shadow logs only; failures fail open.
            proceed, decision_id, review_elapsed_s = self._review_entry_signal(
                sig, contract, lots, entry_price, veto_mode, review_elapsed_s
            )
            if not proceed:
                vetoed += 1
                if symbol:
                    signal_outcomes[symbol] = "vetoed"
                continue
            r = self.place_entry(sig, contract, lots, entry_price, decision_id=decision_id)
            self._mark_review_outcome(decision_id, taken=bool(r))
            if r:
                placed.append(r)
                lots_filled += lots
                if symbol:
                    signal_outcomes[symbol] = "in_cap_placed"
            elif symbol:
                signal_outcomes[symbol] = "placement_failed"
        logger.info(
            "futures_follow entry job placed %d lot-order(s), skipped %d (cap), "
            "vetoed %d (LLM), [mode=%s]",
            len(placed),
            skipped,
            vetoed,
            self.mode,
        )
        if placed:
            self._notify(
                f"📈 {STRATEGY_NAME} [{self.mode}] bought "
                f"{sum(p['lots'] for p in placed)} NIFTY lot(s) on "
                + ", ".join(p["signal_symbol"] or "?" for p in placed)
                + (f" (skipped {skipped} at 50% cap)" if skipped else "")
                + (f" (LLM vetoed {vetoed})" if vetoed else "")
            )
        elif vetoed:
            self._notify(
                f"🛑 {STRATEGY_NAME} [{self.mode}] Stage-1 LLM veto (mode={veto_mode}) "
                f"skipped all {vetoed} signal(s) today — no NIFTY lots bought"
            )
        # Issue #352: capture + persist the entry-evaluation breakdown AFTER all
        # placement decisions are final. Wrapped fail-graceful — a capture/persist
        # failure must never affect the entries already placed above.
        try:
            self._capture_entry_breakdown(
                now_ist=now_ist,
                signals=signals,
                signal_outcomes=signal_outcomes,
                skipped=skipped,
                vetoed=vetoed,
            )
        except Exception:
            logger.exception("futures_follow: entry-breakdown capture/persist failed (ignored)")
        return placed

    def _capture_entry_breakdown(
        self,
        *,
        now_ist: datetime,
        signals: list[dict],
        signal_outcomes: dict[str, str],
        skipped: int,
        vetoed: int,
    ) -> None:
        """Build + persist the "why zero/N signals today" snapshot (issue #352).

        Best-effort observability: if no ``signal_evaluator_details`` is injected
        (unit tests, or a future evaluator swap that doesn't support it) this is a
        silent no-op — it never raises, and ``run_entry`` already wraps this call
        in a try/except as a second layer of defense.
        """
        if self._signal_evaluator_details is None:
            return
        details = self._signal_evaluator_details(now_ist) or {}
        n_by_source = details.get("n_by_source") or {}
        symbols_payload = []
        for row in details.get("symbols") or []:
            symbol = row.get("symbol")
            passed = bool(row.get("passed"))
            if symbol in signal_outcomes:
                # Every symbol in `signals` (the K5-selected candidates) reaches
                # exactly one outcome in the run_entry loop above — cap_skipped,
                # vetoed, in_cap_placed, or placement_failed.
                outcome = signal_outcomes[symbol]
            elif passed:
                # Passed the sector_follow gates but fell outside the K5
                # selection (e.g. a lower vol_ratio than the top-5 cut).
                outcome = "not_selected"
            else:
                reason = row.get("fail_reason") or ""
                outcome = "missing_data" if reason.startswith("None data") else "first_failed_gate"
            symbols_payload.append(
                {
                    "symbol": symbol,
                    "sector_index": row.get("sector_index"),
                    "sector_ret": row.get("sector_ret"),
                    "stock_ret": row.get("stock_ret"),
                    "vol_ratio": row.get("vol_ratio"),
                    "intraday_source": row.get("intraday_source"),
                    "outcome": outcome,
                    "fail_reason": row.get("fail_reason"),
                }
            )

        # Sort by "closeness to passing": placed/passing symbols first, then
        # symbols that failed exactly one gate ranked by how close their metrics
        # were to the thresholds (smallest shortfall first), then symbols missing
        # data last (nothing to rank them by). This is what makes the "closeness
        # to passing" table in the UI actually useful for the operator.
        placed_first = {
            "in_cap_placed": 0,
            "vetoed": 1,
            "cap_skipped": 1,
            "placement_failed": 1,
            "not_selected": 1,
        }

        def _shortfall(row: dict) -> float:
            """Fractional shortfall vs the tightest-missed gate threshold (issue
            #352's "closeness to passing" — lower is closer). Missing-data rows
            sort last via a large sentinel."""
            sector_ret, stock_ret, vol_ratio = (
                row.get("sector_ret"),
                row.get("stock_ret"),
                row.get("vol_ratio"),
            )
            if sector_ret is None or stock_ret is None or vol_ratio is None:
                return float("inf")
            try:
                from services.sector_follow_service import load_config as _sf_load_config

                sf_cfg = _sf_load_config()
                gaps = [
                    (sf_cfg.gate_sector_ret - sector_ret) / max(abs(sf_cfg.gate_sector_ret), 1e-9),
                    (sf_cfg.gate_stock_ret - stock_ret) / max(abs(sf_cfg.gate_stock_ret), 1e-9),
                    (sf_cfg.gate_vol_mult - vol_ratio) / max(abs(sf_cfg.gate_vol_mult), 1e-9),
                ]
                return max(gaps)
            except Exception:
                return float("inf")

        symbols_payload.sort(key=lambda r: (placed_first.get(r["outcome"], 2), _shortfall(r)))

        per_gate_fail_counts = {"sector": 0, "stock": 0, "vol": 0, "missing_data": 0}
        for row in symbols_payload:
            reason = row.get("fail_reason") or ""
            if row["outcome"] == "missing_data" or reason.startswith("None data"):
                per_gate_fail_counts["missing_data"] += 1
                continue
            if "sector " in reason:
                per_gate_fail_counts["sector"] += 1
            if "stock " in reason:
                per_gate_fail_counts["stock"] += 1
            if "vol " in reason:
                per_gate_fail_counts["vol"] += 1

        payload = {
            "eval_at": now_ist.isoformat(),
            "mode": self.mode,
            "n_signals": len(signals),
            "intraday_source_counts": {
                "quotes": n_by_source.get("quotes", 0),
                "aggregator": n_by_source.get("aggregator", 0),
                "historify": n_by_source.get("historify", 0),
                "none": n_by_source.get("none", 0),
            },
            "cap_skipped": skipped,
            "vetoed": vetoed,
            "per_gate_fail_counts": per_gate_fail_counts,
            "symbols": symbols_payload,
        }

        from database.futures_follow_eval_db import init_db as _init_eval_db
        from database.futures_follow_eval_db import upsert_snapshot

        _init_eval_db()
        eval_date = (
            now_ist.astimezone(_IST).date().isoformat()
            if now_ist.tzinfo
            else now_ist.date().isoformat()
        )
        ok = upsert_snapshot(STRATEGY_NAME, eval_date, payload, eval_at=now_ist)
        if not ok:
            logger.warning("futures_follow: entry-breakdown snapshot upsert reported failure")

    def run_exit(self) -> list[dict]:
        """15:25 IST: square off every position opened on a prior trading day (T+1).
        Exits are NEVER gated by an override — a held position must square off."""
        decision = self._resolve_decision()
        self._warn_if_stale_for_exit()
        self._apply_mode_override(decision)
        self._rehydrate_before_exit("exit job")
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
            # The kill switch is same-day and blind to T+1 overnight losses (the
            # 2026-07-07 war-day gap). This realized-loss alert is the T+1-aware
            # hook: on a big loss, surface recent market headlines so the operator
            # can see the macro reason and decide. Informational only — never trades.
            self._maybe_alert_big_loss(net, trigger="T+1 exit")
        return exited

    # ----- OPTION_C same-minute@15:25 (issue #406) ----------------------- #
    def run_signal_snapshot(self) -> list[dict]:
        """15:20 IST (same_minute mode): SELECT today's signals and stash them for
        the 15:25 exit-then-entry job — no orders placed here.

        Selection stays at 15:20 (what the 14.31% backtest measured); execution is
        deferred to 15:25 so the T+1 exit can free its margin first. Fail-graceful:
        on any evaluation error the snapshot is cleared (the 15:25 job then falls
        back to a fresh 15:25 evaluation)."""
        try:
            now_ist = self._now().astimezone(_IST) if self._now().tzinfo else self._now()
            signals = self.evaluate_signals()
            self._pending_snapshot = (now_ist, list(signals))
            logger.info(
                "futures_follow 15:20 signal snapshot (same_minute): %d signal(s) stashed "
                "for 15:25 exit-then-entry",
                len(signals),
            )
            return signals
        except Exception:
            logger.exception("futures_follow signal snapshot failed — cleared (15:25 will re-eval)")
            self._pending_snapshot = None
            return []

    def run_exit_then_entry(self) -> dict:
        """15:25 IST (same_minute mode): square off the T+1 exit FIRST, then place
        today's entries in the same minute (OPTION_C, issue #406).

        Exit-before-entry is load-bearing: after ``run_exit`` the in-memory
        ``paper_book`` is flat, so ``run_entry`` sizes against a fresh 50% cap
        (``lots_held``==0) and the two cohorts never overlap on margin. Entries use
        the 15:20 snapshot when present (backtest-faithful selection), else fall
        back to a fresh 15:25 evaluation. Exit and entry are independently
        fail-isolated so an entry error can never strand the exit."""
        exited = self.run_exit()
        snap = self._pending_snapshot
        self._pending_snapshot = None
        if snap is not None:
            _snap_ist, signals = snap
        else:
            logger.warning(
                "futures_follow 15:25: no 15:20 snapshot present — re-evaluating signals now "
                "(same_minute fallback)"
            )
            signals = None  # run_entry re-evaluates at 15:25
        placed = self.run_entry(signals=signals)
        return {"exited": exited, "placed": placed}

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

        Three checks (self-contained — no aggregator probe, because
        futures_follow does not own any historical data pipeline of its own):

        1. **Historify lookback freshness**: the sector_follow_cap5_vol historify
           feed is fresh for today's date, via the existing
           ``self._data_health_checker``. Under the ``quotes`` intraday source
           (issue #332) this arm guards ONLY the 20-day lookback (prior close,
           avg volume) — today's snapshot no longer depends on it.
        2. **Broker session live**: an API key is configured (operator logged
           in). Under ``quotes`` this is load-bearing for DATA, not just orders.
        3. **Dry-run quote probe** (only when ``FUTURES_FOLLOW_INTRADAY_SOURCE``
           resolves to ``quotes``): fetch 1 universe symbol through the quotes
           provider path and assert ``ltp > 0``. A session that authenticates
           but cannot serve quotes must fail here at 15:18, not silently
           degrade at 15:20.

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
        today_str = as_of_date.isoformat()

        if not futures_smoke_check_enabled():
            logger.debug("futures_follow 15:18 smoke check skipped (flag off)")
            return True, {"skipped": True}

        # ---- Check 1: historify lookback freshness (shared sector_follow checker) #
        data_ok = True
        stale_symbols: list[str] = []
        if self._data_health_checker is not None and data_freshness_enabled():
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

        # ---- Check 3: dry-run quote probe (issue #332, quotes source only) ---- #
        intraday_source = futures_intraday_source()
        quote_probe_ok = True
        if intraday_source == "quotes":
            try:
                quote_probe_ok = bool(self._quote_probe())
            except Exception:
                logger.exception("futures_follow smoke check: quote probe raised")
                quote_probe_ok = False

        ok = data_ok and session_ok and quote_probe_ok
        details: dict = {
            "data_ok": data_ok,
            "stale_symbols": stale_symbols,
            "broker_session_ok": session_ok,
            "intraday_source": intraday_source,
            "quote_probe_ok": quote_probe_ok,
        }

        if ok:
            # f-string (not %-args): a cross-suite logging-filter interaction can
            # leave a stray format arg on the record and make lazy "%s"-arg
            # formatting raise "not all arguments converted" here; eager
            # interpolation sidesteps it (details has no % chars).
            logger.info(f"futures_follow 15:18 smoke check PASSED: {details}")
            return True, details

        reasons = []
        if not data_ok:
            reasons.append(
                f"sector_follow historify feed stale ({today_str}) — guards the 20d lookback"
                + (f": {stale_symbols}" if stale_symbols else "")
            )
        if not session_ok:
            reasons.append("broker session not live")
        if not quote_probe_ok:
            reasons.append("dry-run quote probe failed (session cannot serve quotes)")
        reason = "; ".join(reasons)
        logger.error(f"futures_follow 15:18 SMOKE CHECK FAILED: {reason}")

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
        about, so the 15:25/15:28 exit jobs will square it off.

        ``mode_key=STRATEGY_NAME`` is load-bearing (issue #497): it resolves the
        book with the SAME ``resolve_order_mode`` that routed the entry, so this
        read can never land on a different book than the write. Without it the
        read fell through to the platform analyze overlay, and a ``sandbox``
        strategy with Analyze OFF read the LIVE broker book — an empty position
        list, an empty ``paper_book``, and four trading days of missed T+1 exits.

        Returns the number of positions rehydrated. Never raises.
        """
        try:
            from services.positionbook_service import get_positionbook

            api_key = _resolve_exit_api_key()
            if not api_key:
                logger.warning("futures_follow rehydrate: no api_key; skipping")
                return 0
            success, resp, _ = get_positionbook(api_key=api_key, mode_key=STRATEGY_NAME)
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
        # Rehydrated positions are stamped with the PREVIOUS TRADING DAY so the
        # T+1 exit jobs (which square off positions whose entry_date != today)
        # act on them. Must be a real trading day, not calendar-yesterday: a
        # Monday restart of a Friday position previously stamped Sunday (an
        # impossible entry session that then rode into the journal exit row —
        # issue #401). Weekend/holiday-aware via data_freshness_service.
        from services.data_freshness_service import previous_trading_day

        prior_day = previous_trading_day(self._now().date()).isoformat()

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
            # Ceiling-divide, not floor (#353): the store's net qty can cover
            # MULTIPLE journaled BUY lots that don't line up with the currently
            # configured ``nifty_lot_size`` (e.g. two legacy 65-qty BUYs nets to
            # a 130-qty store position; the current config's lot size is 75, and
            # floor division 130 // 75 == 1 silently drops a whole lot). Ceiling
            # division guarantees the derived lot count never undercounts open
            # exposure — worst case it overstates by a fraction of a lot when qty
            # isn't a clean multiple, which is safer than the previous
            # undercount for a value that only feeds sizing/observability here.
            lots = max(1, -(-qty // lot_size)) if lot_size else 1
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

    def _rehydrate_before_exit(self, reason: str) -> None:
        """Pull any open store position into ``paper_book`` before an exit run.

        Load-bearing backstop for the boot-race that missed the 2026-07-14 T+1
        exit (issue #403): boot rehydration checks the broker api_key ONCE and
        skips if the session isn't up yet (the normal morning case — OpenAlgo
        boots before the daily Zerodha login). Nothing re-ran it, so the open
        overnight leg was never in ``paper_book`` at 15:25 and the exit job
        squared off 0. Re-running rehydration at the top of every exit path makes
        the exit self-healing regardless of boot timing: ``rehydrate_paper_book_
        from_store`` is idempotent (dedups by symbol already held) and never
        raises, so this is a safe no-op when the book is already correct."""
        try:
            n = self.rehydrate_paper_book_from_store()
            if n:
                logger.warning(
                    "futures_follow %s: rehydrated %d open position(s) the boot path "
                    "missed (broker session likely landed after boot) — squaring off now",
                    reason,
                    n,
                )
        except Exception:
            logger.exception("futures_follow %s rehydrate-before-exit failed (ignored)", reason)

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
        """15:28 IST: post-primary-exit retry backstop (#334).

        Flattens any prior-day position STILL open after the 15:25 primary exit
        — a rejected exit order or a failed/missed 15:25 job — before the 15:30
        NFO close. It shares the exit predicate (``entry_date != today``) by
        design: with the 15:25 → 15:28 ordering, anything it finds SHOULD be
        flattened. (The old 15:14 slot was inherited from the simplified
        engine's MIS constraint; futures_follow trades NRML, accepted until the
        15:30 close, so that constraint does not apply — and firing before the
        primary exit made the watchdog the de-facto exit.) Exits are never
        gated."""
        self._rehydrate_before_exit("EOD watchdog")
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
            trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=28, timezone="Asia/Kolkata"),
            id="futures_follow_eod_watchdog",
            replace_existing=True,
            name="Futures Follow CAP50 EOD watchdog (15:28 IST)",
        )
        entry_mode = futures_entry_mode()
        if entry_mode == "same_minute":
            # OPTION_C (issue #406): 15:20 snapshots signals; 15:25 does the T+1 exit
            # FIRST then places entries in the same minute (fresh cap, no margin
            # overlap). A single 15:25 job guarantees exit-before-entry ordering.
            sched.add_job(
                _snapshot_job,
                trigger=CronTrigger(
                    day_of_week="mon-fri", hour=15, minute=20, timezone="Asia/Kolkata"
                ),
                id="futures_follow_entry",  # reuse id -> replaces the legacy entry job
                replace_existing=True,
                name="Futures Follow CAP50 15:20 signal snapshot (same_minute)",
            )
            sched.add_job(
                _exit_then_entry_job,
                trigger=CronTrigger(
                    day_of_week="mon-fri", hour=15, minute=25, timezone="Asia/Kolkata"
                ),
                id="futures_follow_exit",  # reuse id -> replaces the legacy exit job
                replace_existing=True,
                name="Futures Follow CAP50 15:25 exit-then-entry (same_minute)",
            )
        else:
            sched.add_job(
                _entry_job,
                trigger=CronTrigger(
                    day_of_week="mon-fri", hour=15, minute=20, timezone="Asia/Kolkata"
                ),
                id="futures_follow_entry",
                replace_existing=True,
                name="Futures Follow CAP50 entry (15:20 IST)",
            )
            sched.add_job(
                _exit_job,
                trigger=CronTrigger(
                    day_of_week="mon-fri", hour=15, minute=25, timezone="Asia/Kolkata"
                ),
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
            "futures_follow jobs registered (mode=%s, entry_mode=%s, strategy_id=%s, smoke_check=%s)",
            self.mode,
            entry_mode,
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


def _snapshot_job() -> None:
    if _SINGLETON is not None:
        _SINGLETON.run_signal_snapshot()


def _exit_then_entry_job() -> None:
    if _SINGLETON is not None:
        _SINGLETON.run_exit_then_entry()


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
        signal_reviewer=production_signal_reviewer,
        market_context_provider=production_market_context,
        signal_evaluator_details=production_signal_evaluator_details,
    )
    svc.register_jobs(scheduler)
    # R2 (#318): surface the veto enforcement default loudly at boot. With no
    # strategy_llm_config row and no VETO_LAYER_MODE env, sandbox resolves to
    # ACTIVE (enforcing) per B4 — a 'skip' verdict blocks the sandbox entry.
    try:
        logger.info(
            "futures_follow Stage-1 LLM veto wired (issue #318): enforcement mode=%s "
            "(resolution: strategy_llm_config row -> VETO_LAYER_MODE env -> mode-aware "
            "default, sandbox=ACTIVE per B4)",
            svc._resolve_veto_mode(),
        )
    except Exception:
        logger.exception("futures_follow veto boot log failed (ignored)")
    # Boot durability (#265, BOTH modes): rebuild paper_book from the
    # mode-appropriate position store (sandbox.db in sandbox, broker in live) so a
    # restart can't strand an open overnight NIFTY-futures long. Best-effort — a
    # rehydrate failure never blocks boot.
    try:
        svc.rehydrate_paper_book_from_store()
    except Exception:
        logger.exception("futures_follow boot rehydrate failed (ignored)")
    # Boot-race arming (#403): the boot rehydrate above checks the broker api_key
    # ONCE and skips if the session isn't up yet — the normal morning case, since
    # OpenAlgo boots before the daily Zerodha login. On 2026-07-14 that skipped
    # the open leg and the 15:25 exit found an empty book. Re-arm on the
    # in-process broker_session_refreshed event (published by
    # utils.auth_utils.notify_broker_session_refreshed the instant login lands) so
    # rehydration re-runs the moment the session appears — mirroring
    # scanner_presubscribe / ws_recovery_service. The run_exit / watchdog
    # rehydrate-before-exit backstop covers the exit itself; this keeps the live
    # paper_book (and the "Open" status card) correct through the day too.
    try:
        from utils.event_bus import bus as _bus

        def _rehydrate_on_session_refreshed(_event) -> None:
            try:
                n = svc.rehydrate_paper_book_from_store()
                logger.info(
                    "futures_follow: broker_session_refreshed -> rehydrated %d open position(s)",
                    n,
                )
            except Exception:
                logger.exception("futures_follow session-refreshed rehydrate failed (ignored)")

        _bus.subscribe(
            "broker_session_refreshed",
            _rehydrate_on_session_refreshed,
            name="futures_follow_rehydrate",
        )
    except Exception:
        logger.exception("futures_follow broker-session rehydrate arming failed (ignored)")
    if app is not None:
        app.futures_follow_service = svc
    return svc
