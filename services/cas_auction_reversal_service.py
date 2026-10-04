"""cas_auction_reversal — buy the F&O stocks the closing auction pushes down (issue #752).

Research (R71b/R71c, issue #751): since NSE's Closing Auction Session
(2026-08-03), stocks whose auction close is >= 0.5% BELOW their last continuous
print gain +0.60% by the T+1 09:15 bar close (79% win, ~14 names/day, 9/9 weeks
positive, about +0.35% net of CNC delivery costs). Stock futures capture only
~4-30% of the dip, so the vehicle is CNC bought IN the auction. The short mirror
is not tradeable (no overnight CNC short; futures do not follow the auction).

Daily schedule (IST, mon-fri, trading days only):

* 09:00 ``daily_reset``.
* 09:16 ``exit`` (PROTECTED) — SELL CNC for every entry made on an earlier day.
  The sandbox moves yesterday's CNC position into HOLDINGS at the 03:00 session
  expiry, so the quantity is read from holdings + positions, both resolved with
  ``mode_key=STRATEGY_NAME`` (the #497 rule). A CNC SELL can never open a short
  (sandbox and broker both refuse to sell shares you do not hold), so an
  unreadable or flat book still sends the journalled quantity — stranding a
  position is the only failure, and it is the one this job exists to prevent.
* 09:20 ``exit_retry`` (PROTECTED) — the same pass for anything still open.
* 15:14:30 ``arm`` — builds the universe (tradeable F&O stocks, ``NSE`` only),
  starts the ``cas-auction-reversal-monitor`` thread: ONE batched quote every
  ``poll_interval_s`` until 15:33, every observation journaled to
  ``cas_ar_polls`` (last price, ``indicative_close_price``,
  ``total_imbalance_qty``). That table is the pre-registered 20-session
  measurement: how well the 15:20-15:25 indicative price predicts the close.
  The reference price is the frozen LTP after 15:15:00 = the last continuous
  print (the research's 15:14 bar close).
* 15:23:30 ``decide`` — candidates = indicative close <= ref x (1 - threshold);
  top ``max_positions`` by dislocation, ``floor(capital / price)`` shares.
  Every candidate is journaled (selected or not). LIVE: a CNC LIMIT BUY at the
  indicative price + a small buffer is placed into the auction now (market and
  limit orders are accepted 15:20-15:25); the exchange fills it at the single
  auction price. SANDBOX: the buys are queued (see 15:32).
* 15:32 ``fill`` — SANDBOX: the auction has printed, so each stock's LTP IS the
  auction close; a CNC LIMIT BUY at that LTP fills there, exactly what an
  auction participant pays (a sandbox MARKET order would take the ask, and the
  depth after 15:30 is not a market). Both modes: every entry is verified
  through the order book — an ACK is not a fill (#626); a refused entry is
  ``fill='paper'`` and never joins P&L.
* 15:45 ``eod_summary`` — the final auction close for every candidate (the
  measurement), entry-vs-close check, Telegram.

Mode: ``sandbox`` or ``live`` per order via ``resolve_order_mode(STRATEGY_NAME)``
(the /strategies toggle, default-deny sandbox). Live is NOT ready: the Zerodha
holdings mapper drops ``t1_quantity``, so a live T+1 exit would read 0 shares —
``config_snapshot.json`` keeps ``deployable: false`` until that is fixed.

Every external effect is injected with a production default so the decision
logic is unit-testable with no broker, no DB and no clock.
"""

from __future__ import annotations

import math
import os
import threading
import time as _time
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
STRATEGY_NAME = "cas_auction_reversal"
NOTIFY_EVENT = "cas_auction_reversal"
EXCHANGE = "NSE"
PRODUCT = "CNC"
TICK = 0.05

# Fixed schedule (code constants — the research measured exactly these points).
RESET_TIME = time(9, 0)
EXIT_TIME = time(9, 16, 0)  # research exit: T+1 09:15 bar close
EXIT_RETRY_TIME = time(9, 20, 0)
ARM_TIME = time(15, 14, 30)
CONTINUOUS_CLOSE = time(15, 15, 0)  # CAS stocks stop continuous trading
REF_AFTER = time(15, 15, 2)  # first poll after this freezes the reference price
DECIDE_TIME = time(15, 23, 30)  # inside the 15:20-15:25 market/limit window
FILL_TIME = time(15, 32, 0)  # after the auction prints (~15:29-15:30)
WINDOW_END = time(15, 33, 0)
SUMMARY_TIME = time(15, 45, 0)
STALE_POLL_S = 30  # decision re-polls when the last batch is older than this
FILL_VERIFY_ATTEMPTS = 4
FILL_VERIFY_DELAY_S = 1.5
LIVE_LIMIT_BUFFER_PCT = 0.2  # live auction LIMIT = iep x (1 + 0.2%)

_TERMINAL_UNFILLED = {"rejected", "cancelled", "canceled", "expired"}
_FILLED = {"complete", "completed", "filled", "executed", "traded"}


# --------------------------------------------------------------------------- #
# Tunables (env, clamped) — see docs/PARAMETER_LOG.md
# --------------------------------------------------------------------------- #
def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.getenv(name, default))
    except (TypeError, ValueError):
        v = default
    return min(max(v, lo), hi)


def resolve_config() -> dict:
    return {
        "threshold_pct": _env_float("CAS_AR_THRESHOLD_PCT", 0.5, 0.2, 5.0),
        "max_positions": int(_env_float("CAS_AR_MAX_POSITIONS", 10, 1, 30)),
        "capital_per_trade_inr": _env_float(
            "CAS_AR_CAPITAL_PER_TRADE_INR", 50_000, 5_000, 1_000_000
        ),
        "poll_interval_s": int(_env_float("CAS_AR_POLL_INTERVAL_S", 15, 5, 60)),
    }


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def round_to_tick(px: float, tick: float = TICK) -> float:
    return round(round(px / tick) * tick, 2)


def dislocation_pct(iep: float | None, ref: float | None) -> float | None:
    if not iep or not ref or iep <= 0 or ref <= 0:
        return None
    return (iep / ref - 1.0) * 100.0


def select_candidates(
    ref: dict[str, float], last: dict[str, dict], threshold_pct: float, max_positions: int
) -> list[dict]:
    """Rank every symbol whose indicative close sits >= threshold below its
    reference print. Returns ALL crossing symbols (rank 1 = deepest), each with
    ``selected`` set for the first ``max_positions``."""
    rows = []
    for sym, q in last.items():
        d = dislocation_pct(q.get("iep"), ref.get(sym))
        if d is None or d > -threshold_pct:
            continue
        rows.append(
            {
                "symbol": sym,
                "ref_price": ref[sym],
                "iep": q["iep"],
                "dislocation_pct": round(d, 4),
                "imbalance_qty": q.get("imbalance_qty"),
            }
        )
    rows.sort(key=lambda r: r["dislocation_pct"])
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
        r["selected"] = i <= max_positions
        r["skip_reason"] = None if r["selected"] else "max_positions"
    return rows


def size_qty(capital_inr: float, price: float | None) -> int:
    if not price or price <= 0:
        return 0
    return int(math.floor(capital_inr / price))


def cnc_round_trip_charges(buy_value: float, sell_value: float) -> float:
    """Modelled Zerodha equity-DELIVERY round trip (zerodha.com/charges, verified
    2026-10-04): zero brokerage, STT 0.1% both sides, NSE txn 0.00307% of
    turnover, SEBI Rs10/crore, stamp 0.015% of the buy, 18% GST on txn + SEBI,
    DP Rs15.34 per scrip on the sell day."""
    turnover = buy_value + sell_value
    stt = 0.001 * (buy_value + sell_value)
    txn = 0.0000307 * turnover
    sebi = 0.000001 * turnover
    stamp = 0.00015 * buy_value
    gst = 0.18 * (txn + sebi)
    return round(stt + txn + sebi + stamp + gst + 15.34, 2)


def book_quantities(holdings: list[dict] | None, positions: list[dict] | None) -> dict[str, int]:
    """Long CNC/equity shares per symbol. ``max`` (never sum) of the two books:
    a broker can show the same overnight CNC lot in both, and the sandbox moves
    it from one to the other at the 03:00 settlement."""
    hold: dict[str, int] = {}
    pos: dict[str, int] = {}
    for src, out, product_filter in ((holdings, hold, False), (positions, pos, True)):
        for p in src or []:
            try:
                if str(p.get("exchange", EXCHANGE)).upper() not in ("", EXCHANGE):
                    continue
                if product_filter and str(p.get("product", "")).upper() not in ("", PRODUCT):
                    continue
                q = int(float(p.get("quantity") or 0))
            except (TypeError, ValueError):
                continue
            if q > 0:
                s = str(p.get("symbol", "")).strip()
                out[s] = out.get(s, 0) + q
    return {s: max(hold.get(s, 0), pos.get(s, 0)) for s in set(hold) | set(pos)}


# --------------------------------------------------------------------------- #
# Production seams
# --------------------------------------------------------------------------- #
def _api_key() -> str | None:
    try:
        from database.auth_db import get_first_available_api_key

        return get_first_available_api_key()
    except Exception:
        logger.debug("cas_ar: api key lookup failed", exc_info=True)
        return None


def production_universe() -> list[str]:
    """Tradeable F&O STOCKS (the #647 master-contract filter), indices excluded —
    only cash equities go through the closing auction."""
    from services.scanner_presubscribe import resolve_exchange_for_symbol
    from services.scanner_universe import tradeable_universe

    return sorted(s for s in tradeable_universe() if resolve_exchange_for_symbol(s) == EXCHANGE)


def production_quote_batch(symbols: list[str]) -> dict[str, dict] | None:
    """``{symbol: {ltp, iep, imbalance_qty, volume}}`` via ONE ``get_multiquotes``."""
    try:
        from services.quotes_service import get_multiquotes

        api_key = _api_key()
        if not api_key:
            logger.warning("cas_ar: no API key / broker session for quotes")
            return None
        ok, data, _ = get_multiquotes(
            [{"symbol": s, "exchange": EXCHANGE} for s in symbols], api_key=api_key
        )
        if not ok:
            logger.warning("cas_ar: batch quote failed: %s", data)
            return None

        def _px(v):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if f > 0 else None

        def _n(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None

        out: dict[str, dict] = {}
        for r in (data or {}).get("results") or []:
            if not isinstance(r, dict) or r.get("error"):
                continue
            d = r.get("data") or {}
            out[r.get("symbol")] = {
                "ltp": _px(d.get("ltp")),
                "iep": _px(d.get("indicative_close_price")),
                "imbalance_qty": _n(d.get("total_imbalance_qty")),
                "volume": _n(d.get("volume")),
            }
        return out
    except Exception:
        logger.exception("cas_ar: batch quote raised")
        return None


def production_order_placer(order: dict) -> dict:
    """Route one order through ``place_order`` with the strategy's own mode key."""
    from services.place_order_service import place_order

    api_key = _api_key()
    if not api_key:
        return {"status": "error", "message": "no api key available"}
    payload = {
        "apikey": api_key,
        "strategy": STRATEGY_NAME,
        "symbol": order["symbol"],
        "exchange": EXCHANGE,
        "action": order["action"],
        "product": PRODUCT,
        "pricetype": order.get("pricetype", "MARKET"),
        "quantity": str(order["quantity"]),
    }
    if payload["pricetype"] == "LIMIT":
        payload["price"] = str(order["price"])
    success, response, _ = place_order(payload, api_key=api_key, mode_key=STRATEGY_NAME)
    response = dict(response or {})
    response.setdefault("status", "success" if success else "error")
    return response


def production_fill_reader(order_id: str) -> dict | None:
    """``{status: filled|rejected|pending, price, qty, message}``. ``average_price``
    is the only "what was transacted" field; ``filled_quantity`` is read by
    presence (#626). ``get_order_status`` answers a sandbox id from sandbox.db."""
    try:
        from services.orderstatus_service import get_order_status

        api_key = _api_key()
        if not api_key:
            return None
        ok, resp, _ = get_order_status(
            {"strategy": STRATEGY_NAME, "orderid": str(order_id)}, api_key=api_key
        )
        if not ok:
            return None
        data = (resp or {}).get("data") or {}
        status = str(data.get("order_status") or data.get("status") or "").lower()
        try:
            avg = float(data["average_price"]) if data.get("average_price") is not None else None
        except (TypeError, ValueError):
            avg = None
        fq = data.get("filled_quantity")
        if fq is None:
            fq = data.get("quantity")
        try:
            fq = int(float(fq)) if fq is not None else None
        except (TypeError, ValueError):
            fq = None
        msg = str(data.get("status_message") or data.get("message") or "").strip() or None
        if status in _TERMINAL_UNFILLED:
            return {"status": "rejected", "price": None, "qty": 0, "message": msg}
        if (status in _FILLED or (avg and avg > 0)) and avg and avg > 0:
            return {"status": "filled", "price": avg, "qty": fq, "message": msg}
        return {"status": "pending", "price": None, "qty": None, "message": msg}
    except Exception:
        logger.exception("cas_ar: order status raised for %s", order_id)
        return None


def production_book_reader() -> tuple[list[dict] | None, list[dict] | None]:
    """``(holdings, positions)`` from the book the strategy's OWN mode resolves
    to (``mode_key=STRATEGY_NAME``); ``None`` for an unreadable side."""
    api_key = _api_key()
    if not api_key:
        return None, None
    holdings = positions = None
    try:
        from services.holdings_service import get_holdings

        ok, resp, _ = get_holdings(api_key=api_key, mode_key=STRATEGY_NAME)
        if ok:
            holdings = list(((resp or {}).get("data") or {}).get("holdings") or [])
    except Exception:
        logger.exception("cas_ar: holdings read raised")
    try:
        from services.positionbook_service import get_positionbook

        ok, resp, _ = get_positionbook(api_key=api_key, mode_key=STRATEGY_NAME)
        if ok:
            positions = list((resp or {}).get("data") or [])
    except Exception:
        logger.exception("cas_ar: position book read raised")
    return holdings, positions


def production_mode_resolver() -> str:
    from services.mode_service import resolve_order_mode

    return resolve_order_mode(STRATEGY_NAME).value


def production_notifier(message: str) -> None:
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify(NOTIFY_EVENT, message)
    except Exception:
        logger.debug("cas_ar: notify failed", exc_info=True)


def production_trading_day_checker(d: date) -> bool:
    from services.data_freshness_service import is_trading_day

    return is_trading_day(d)


def _beat(name: str) -> None:
    try:
        from services.thread_registry import beat

        beat(name)
    except Exception:
        logger.debug("cas_ar: heartbeat failed", exc_info=True)


def _done(name: str) -> None:
    try:
        from services.thread_registry import done

        done(name)
    except Exception:
        logger.debug("cas_ar: done mark failed", exc_info=True)


MONITOR_THREAD = "cas-auction-reversal-monitor"


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #
class CasAuctionReversalService:
    """Closing-auction reversal evaluator + scheduler glue (see module docstring)."""

    def __init__(
        self,
        app=None,
        scheduler=None,
        *,
        universe_provider: Callable[[], list[str]] | None = None,
        quote_batch: Callable[[list[str]], dict | None] | None = None,
        order_placer: Callable[[dict], dict] | None = None,
        fill_reader: Callable[[str], dict | None] | None = None,
        book_reader: Callable[[], tuple] | None = None,
        mode_resolver: Callable[[], str] | None = None,
        notifier: Callable[[str], None] | None = None,
        trading_day_checker: Callable[[date], bool] | None = None,
        config_reader: Callable[[], dict] | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        journal: bool = True,
    ):
        self.app = app
        self.scheduler = scheduler
        self._universe = universe_provider or production_universe
        self._quote_batch = quote_batch or production_quote_batch
        self._order_placer = order_placer or production_order_placer
        self._fill_reader = fill_reader or production_fill_reader
        self._book_reader = book_reader or production_book_reader
        self._mode_resolver = mode_resolver or production_mode_resolver
        self._notify = notifier or production_notifier
        self._trading_day = trading_day_checker or production_trading_day_checker
        self._config_reader = config_reader or resolve_config
        self._now = now or (lambda: datetime.now(_IST))
        self._sleep = sleep or _time.sleep
        self._journal = journal
        self._lock = threading.RLock()
        self._monitor_thread: threading.Thread | None = None
        self.manual_pause = False
        self.day: dict[str, Any] = self._empty_day()

    # ----- state ---------------------------------------------------------- #
    @staticmethod
    def _empty_day() -> dict:
        return {
            "trade_date": None,
            "armed": False,
            "trading_day": None,
            "universe": [],
            "config": None,
            "mode": None,
            "ref_pre": {},  # last LTP seen before 15:15:00
            "ref": {},  # frozen LTP after 15:15:00 = last continuous print
            "last": {},  # latest poll per symbol
            "last_poll_at": None,
            "n_polls": 0,
            "decided": False,
            "candidates": [],
            "pending": [],  # sandbox buys queued for the 15:32 fill
            "entries": [],  # {symbol, qty, row_id, order_id, ...}
            "filled": False,
            "summary": None,
            "errors": [],
        }

    def trade_date(self) -> str:
        return self._now().date().isoformat()

    def current_mode(self) -> str:
        try:
            return self._mode_resolver()
        except Exception:
            logger.exception("cas_ar: mode resolve failed — sandbox")
            return "sandbox"

    def _utc_naive(self, dt_ist: datetime) -> datetime:
        return dt_ist.astimezone(UTC).replace(tzinfo=None)

    def _ensure_day(self) -> str:
        tds = self.trade_date()
        with self._lock:
            if self.day.get("trade_date") != tds:
                self.day = self._empty_day()
                self.day["trade_date"] = tds
        return tds

    # ----- pause / resume (durable override, the cas_straddle pattern) ----- #
    def _entry_held_by_override(self) -> bool:
        if self.manual_pause:
            return True
        try:
            from database.strategy_runtime_override_db import is_entry_blocked

            blocked, _ov = is_entry_blocked(STRATEGY_NAME)
            return bool(blocked)
        except Exception:
            logger.debug("cas_ar runtime-override resolve failed; not blocking", exc_info=True)
            return False

    def pause(self) -> dict:
        self.manual_pause = True
        try:
            from database.strategy_runtime_override_db import set_override

            expires = self._now().replace(hour=23, minute=59, second=0, microsecond=0)
            set_override(
                STRATEGY_NAME,
                "pause",
                self._utc_naive(expires),
                reason="operator manual pause",
                set_by="cas_auction_reversal",
            )
        except Exception:
            logger.exception("cas_ar: failed to write pause override")
        logger.warning("cas_ar MANUALLY PAUSED — new entries halted (T+1 exits still run)")
        return {"status": "success", "manual_pause": True}

    def resume(self) -> dict:
        self.manual_pause = False
        try:
            from database.strategy_runtime_override_db import clear_override

            clear_override(STRATEGY_NAME)
        except Exception:
            logger.exception("cas_ar: failed to clear overrides")
        logger.info("cas_ar RESUMED")
        return {"status": "success", "manual_pause": False}

    # ----- 09:00 reset ---------------------------------------------------- #
    def run_daily_reset(self) -> None:
        with self._lock:
            self.day = self._empty_day()
        logger.info("cas_ar daily reset")

    # ----- 15:14:30 arm --------------------------------------------------- #
    def arm(self) -> dict:
        tds = self._ensure_day()
        today = self._now().date()
        try:
            trading = bool(self._trading_day(today))
        except Exception:
            logger.exception("cas_ar: trading-day check raised — assuming trading day")
            trading = True
        with self._lock:
            self.day["trading_day"] = trading
        if not trading:
            logger.info("cas_ar: %s is not a trading day — idle", tds)
            return {"status": "skipped", "reason": "not_trading_day"}
        try:
            universe = list(self._universe())
        except Exception:
            logger.exception("cas_ar: universe build raised")
            universe = []
        cfg = self._config_reader()
        mode = self.current_mode()
        with self._lock:
            self.day.update(universe=universe, config=cfg, mode=mode, armed=bool(universe))
        if not universe:
            self._alert(f"⚠️ cas_auction_reversal: empty universe on {tds} — not armed")
            return {"status": "error", "reason": "empty_universe"}
        logger.info(
            "cas_ar ARMED %s — universe %d, mode %s, threshold %.2f%%, max %d, Rs%.0f/trade",
            tds,
            len(universe),
            mode,
            cfg["threshold_pct"],
            cfg["max_positions"],
            cfg["capital_per_trade_inr"],
        )
        self._ensure_monitor_thread()
        return {"status": "armed", "universe": len(universe), "mode": mode}

    def _ensure_monitor_thread(self) -> None:
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="cas-auction-reversal-monitor", daemon=True
        )
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        interval = (self.day.get("config") or resolve_config())["poll_interval_s"]
        while self._now().time() < WINDOW_END:
            _beat(MONITOR_THREAD)
            try:
                self.poll_once()
            except Exception:
                logger.exception("cas_ar: poll raised")
            self._sleep(interval)
        _done(MONITOR_THREAD)

    # ----- one batched poll ----------------------------------------------- #
    def poll_once(self) -> dict | None:
        universe = self.day.get("universe") or []
        if not universe:
            return None
        now = self._now()
        quotes = self._quote_batch(universe)
        if not quotes:
            logger.warning("cas_ar: poll returned no quotes at %s", now.strftime("%H:%M:%S"))
            return None
        t = now.time()
        tds = self.day["trade_date"]
        ts = self._utc_naive(now)
        rows = []
        with self._lock:
            for sym, q in quotes.items():
                if sym is None:
                    continue
                ltp = q.get("ltp")
                if ltp:
                    if t < CONTINUOUS_CLOSE:
                        self.day["ref_pre"][sym] = ltp
                    elif t >= REF_AFTER and sym not in self.day["ref"]:
                        self.day["ref"][sym] = ltp
                self.day["last"][sym] = dict(q, at=now.isoformat())
                rows.append(
                    {
                        "ts": ts,
                        "trade_date": tds,
                        "symbol": sym,
                        "ltp": ltp,
                        "iep": q.get("iep"),
                        "imbalance_qty": q.get("imbalance_qty"),
                        "volume": q.get("volume"),
                    }
                )
            self.day["last_poll_at"] = now
            self.day["n_polls"] += 1
        if self._journal and rows:
            try:
                from database.cas_auction_reversal_db import record_polls

                record_polls(rows)
            except Exception:
                logger.exception("cas_ar: poll journal failed")
        return quotes

    def _reference(self) -> dict[str, float]:
        ref = dict(self.day.get("ref_pre") or {})
        ref.update(self.day.get("ref") or {})  # the frozen post-15:15 print wins
        return ref

    # ----- 15:23:30 decide ------------------------------------------------ #
    def run_decide(self) -> dict:
        self._ensure_day()
        if not self.day.get("armed"):
            logger.info("cas_ar decide: not armed today — skipping")
            return {"status": "skipped", "reason": "not_armed"}
        if self.day.get("decided"):
            return {"status": "skipped", "reason": "already_decided"}
        last_at = self.day.get("last_poll_at")
        if last_at is None or (self._now() - last_at).total_seconds() > STALE_POLL_S:
            self.poll_once()
        cfg = self.day["config"]
        cands = select_candidates(
            self._reference(),
            self.day.get("last") or {},
            cfg["threshold_pct"],
            cfg["max_positions"],
        )
        paused = self._entry_held_by_override()
        mode = self.current_mode()
        tds = self.day["trade_date"]
        orders = []
        for c in cands:
            qty = size_qty(cfg["capital_per_trade_inr"], c["iep"])
            if c["selected"] and paused:
                c.update(selected=False, skip_reason="paused")
            elif c["selected"] and qty < 1:
                c.update(selected=False, skip_reason="qty_zero")
            c["qty"] = qty if c["selected"] else 0
            if c["selected"]:
                orders.append(c)
        with self._lock:
            self.day.update(decided=True, candidates=cands, mode=mode)
        if self._journal:
            try:
                from database.cas_auction_reversal_db import upsert_candidate

                for c in cands:
                    upsert_candidate(
                        tds,
                        c["symbol"],
                        ref_price=c["ref_price"],
                        iep_at_decision=c["iep"],
                        dislocation_pct=c["dislocation_pct"],
                        imbalance_qty=c["imbalance_qty"],
                        rank=c["rank"],
                        selected=1 if c["selected"] else 0,
                        skip_reason=c["skip_reason"],
                    )
            except Exception:
                logger.exception("cas_ar: candidate journal failed")
        logger.info(
            "cas_ar DECIDE %s mode=%s: %d crossed -%.2f%%, %d selected%s",
            tds,
            mode,
            len(cands),
            cfg["threshold_pct"],
            len(orders),
            " (PAUSED)" if paused else "",
        )
        if not orders:
            return {"status": "no_entries", "candidates": len(cands)}
        if mode == "live":
            # Into the auction now: the exchange fills at the single auction price.
            for c in orders:
                price = round_to_tick(c["iep"] * (1 + LIVE_LIMIT_BUFFER_PCT / 100.0))
                self._place_entry(c, pricetype="LIMIT", price=price, mode=mode)
            return {"status": "placed", "mode": mode, "orders": len(orders)}
        with self._lock:
            self.day["pending"] = orders
        return {"status": "queued", "mode": mode, "orders": len(orders)}

    # ----- 15:32 fill ----------------------------------------------------- #
    def run_fill(self) -> dict:
        """SANDBOX: place the queued buys at the printed auction close. Both
        modes: verify every entry against the order book."""
        self._ensure_day()
        if self.day.get("filled"):
            return {"status": "skipped", "reason": "already_filled"}
        pending = list(self.day.get("pending") or [])
        placed = 0
        if pending:
            quotes = self._quote_batch([c["symbol"] for c in pending]) or {}
            for c in pending:
                close = (quotes.get(c["symbol"]) or {}).get("ltp")
                if not close:
                    logger.error(
                        "cas_ar fill: no post-auction price for %s — not bought", c["symbol"]
                    )
                    self._record_skip(c, "no_auction_price")
                    continue
                qty = size_qty(self.day["config"]["capital_per_trade_inr"], close)
                if qty < 1:
                    self._record_skip(c, "qty_zero")
                    continue
                c["qty"] = qty
                self._place_entry(c, pricetype="LIMIT", price=round_to_tick(close), mode="sandbox")
                placed += 1
        verified = self._verify_entries()
        with self._lock:
            self.day["filled"] = True
            self.day["pending"] = []
        return {"status": "done", "placed": placed, **verified}

    def _record_skip(self, c: dict, reason: str) -> None:
        if not self._journal:
            return
        try:
            from database.cas_auction_reversal_db import upsert_candidate

            upsert_candidate(self.day["trade_date"], c["symbol"], selected=0, skip_reason=reason)
        except Exception:
            logger.exception("cas_ar: candidate skip journal failed")

    def _place_entry(self, c: dict, *, pricetype: str, price: float | None, mode: str) -> None:
        now = self._now()
        order = {
            "symbol": c["symbol"],
            "action": "BUY",
            "quantity": c["qty"],
            "pricetype": pricetype,
        }
        if price is not None:
            order["price"] = price
        try:
            resp = self._order_placer(order)
        except Exception as e:
            logger.exception("cas_ar: entry order raised for %s", c["symbol"])
            resp = {"status": "error", "message": str(e)}
        ok = str(resp.get("status")).lower() == "success" and resp.get("orderid")
        entry = {
            "symbol": c["symbol"],
            "qty": c["qty"],
            "order_id": resp.get("orderid"),
            "status": "placed" if ok else "rejected",
            "message": None if ok else str(resp.get("message") or resp)[:250],
            "row_id": None,
        }
        if self._journal:
            try:
                from database.cas_auction_reversal_db import record_trade

                entry["row_id"] = record_trade(
                    mode=mode,
                    trade_date=self.day["trade_date"],
                    symbol=c["symbol"],
                    exchange=EXCHANGE,
                    product=PRODUCT,
                    quantity=c["qty"],
                    ref_price=c.get("ref_price"),
                    iep_at_decision=c.get("iep"),
                    dislocation_pct=c.get("dislocation_pct"),
                    imbalance_qty=c.get("imbalance_qty"),
                    entry_order_id=entry["order_id"],
                    entry_at=self._utc_naive(now),
                    status="placed" if ok else "rejected",
                    fill="real" if ok else "paper",
                    error_message=entry["message"],
                )
            except Exception:
                logger.exception("cas_ar: entry journal failed for %s", c["symbol"])
        if not ok:
            logger.error("cas_ar: entry REJECTED %s: %s", c["symbol"], entry["message"])
            self._alert(
                f"⚠️ cas_auction_reversal: entry rejected {c['symbol']} — {entry['message']}"
            )
        with self._lock:
            self.day["entries"].append(entry)

    def _verify_entries(self) -> dict:
        """An ACK is not a fill (#626): read each entry back from the book."""
        filled = rejected = pending = 0
        for e in self.day.get("entries") or []:
            if e["status"] != "placed" or not e.get("order_id"):
                continue
            res = None
            for attempt in range(FILL_VERIFY_ATTEMPTS):
                res = self._fill_reader(e["order_id"])
                if res and res["status"] in ("filled", "rejected"):
                    break
                if attempt < FILL_VERIFY_ATTEMPTS - 1:
                    self._sleep(FILL_VERIFY_DELAY_S)
            fields: dict[str, Any] = {}
            if res and res["status"] == "filled":
                e["status"] = "open"
                e["entry_price"] = res["price"]
                e["entry_qty"] = res["qty"] if res.get("qty") is not None else e["qty"]
                fields = {
                    "status": "open",
                    "entry_price": res["price"],
                    "entry_qty": e["entry_qty"],
                }
                filled += 1
            elif res and res["status"] == "rejected":
                e["status"] = "rejected"
                fields = {
                    "status": "rejected",
                    "fill": "paper",
                    "error_message": res.get("message"),
                }
                rejected += 1
                self._alert(
                    f"⚠️ cas_auction_reversal: entry refused post-ACK {e['symbol']} — {res.get('message')}"
                )
            else:
                pending += 1
            if fields and e.get("row_id") and self._journal:
                try:
                    from database.cas_auction_reversal_db import update_trade

                    update_trade(e["row_id"], **fields)
                except Exception:
                    logger.exception("cas_ar: entry verify journal failed")
        return {"filled": filled, "rejected": rejected, "unverified": pending}

    # ----- 15:45 EOD summary ---------------------------------------------- #
    def run_eod_summary(self) -> dict:
        self._ensure_day()
        tds = self.day["trade_date"]
        if not self.day.get("armed"):
            return {"status": "skipped", "reason": "not_armed"}
        if any(e["status"] == "placed" for e in self.day.get("entries") or []):
            self._verify_entries()  # backstop for slow propagation
        cands = self.day.get("candidates") or []
        closes = self._quote_batch([c["symbol"] for c in cands]) if cands else {}
        closes = closes or {}
        for c in cands:
            close = (closes.get(c["symbol"]) or {}).get("ltp")
            c["auction_close"] = close
            c["close_dislocation_pct"] = (
                round((close / c["ref_price"] - 1) * 100, 4) if close and c["ref_price"] else None
            )
        if self._journal:
            try:
                from database.cas_auction_reversal_db import (
                    trades_for_date,
                    update_trade,
                    upsert_candidate,
                )

                for c in cands:
                    upsert_candidate(
                        tds,
                        c["symbol"],
                        auction_close=c["auction_close"],
                        close_dislocation_pct=c["close_dislocation_pct"],
                    )
                for r in trades_for_date(tds):
                    close = (closes.get(r.symbol) or {}).get("ltp")
                    if close:
                        update_trade(r.id, auction_close=close)
            except Exception:
                logger.exception("cas_ar: EOD journal failed")
        n_open = sum(1 for e in self.day.get("entries") or [] if e["status"] == "open")
        hit = [c for c in cands if c.get("close_dislocation_pct") is not None]
        still = sum(
            1 for c in hit if c["close_dislocation_pct"] <= -self.day["config"]["threshold_pct"]
        )
        summary = {
            "trade_date": tds,
            "mode": self.day.get("mode"),
            "universe": len(self.day.get("universe") or []),
            "polls": self.day.get("n_polls"),
            "candidates": len(cands),
            "selected": sum(1 for c in cands if c["selected"]),
            "entries_open": n_open,
            "still_below_threshold_at_close": still,
        }
        with self._lock:
            self.day["summary"] = summary
        lines = [
            f"📉 cas_auction_reversal {tds} ({summary['mode']})",
            f"polls {summary['polls']} · candidates {summary['candidates']} · bought {n_open}",
            f"IEP→close: {still}/{len(hit)} still ≥{self.day['config']['threshold_pct']}% below the last print at the close",
        ]
        for c in [c for c in cands if c["selected"]][:12]:
            lines.append(
                f"  {c['symbol']}: IEP {c['dislocation_pct']:+.2f}% → close "
                f"{c['close_dislocation_pct'] if c['close_dislocation_pct'] is not None else 'n/a'}%"
            )
        self._alert("\n".join(lines))
        return summary

    # ----- 09:16 T+1 exit ------------------------------------------------- #
    def run_exit(self, reason: str = "t1_exit") -> dict:
        today = self._now().date()
        try:
            if not self._trading_day(today):
                return {"status": "skipped", "reason": "not_trading_day"}
        except Exception:
            logger.exception("cas_ar exit: trading-day check raised — proceeding")
        if not self._journal:
            return {"status": "skipped", "reason": "journal_disabled"}
        from database.cas_auction_reversal_db import open_positions, update_trade

        rows = open_positions(before_date=today.isoformat())
        if not rows:
            return {"status": "no_positions"}
        try:
            holdings, positions = self._book_reader()
        except Exception:
            logger.exception("cas_ar exit: book read raised")
            holdings = positions = None
        readable = holdings is not None or positions is not None
        book = book_quantities(holdings, positions) if readable else {}
        if not readable:
            logger.error(
                "cas_ar exit: book UNREADABLE — sending the journalled quantities "
                "(a CNC SELL cannot open a short; not sending could strand %d position(s))",
                len(rows),
            )
        out = []
        for r in rows:
            want = int(r.entry_qty if r.entry_qty is not None else r.quantity)
            qty = want
            if readable:
                have = book.get(r.symbol, 0)
                if have <= 0:
                    logger.warning(
                        "cas_ar exit: %s journal %d but the book shows 0 — sending anyway "
                        "(CNC SELL cannot short; the venue refuses if truly flat)",
                        r.symbol,
                        want,
                    )
                else:
                    qty = min(want, have)
                    book[r.symbol] = have - qty
            if qty <= 0:
                continue
            try:
                resp = self._order_placer(
                    {"symbol": r.symbol, "action": "SELL", "quantity": qty, "pricetype": "MARKET"}
                )
            except Exception as e:
                logger.exception("cas_ar exit: sell raised for %s", r.symbol)
                resp = {"status": "error", "message": str(e)}
            ok = str(resp.get("status")).lower() == "success" and resp.get("orderid")
            if not ok:
                msg = str(resp.get("message") or resp)[:250]
                update_trade(r.id, error_message=f"exit failed: {msg}")
                self._alert(f"🚨 cas_auction_reversal: T+1 SELL failed {r.symbol} x{qty} — {msg}")
                out.append({"symbol": r.symbol, "status": "error", "message": msg})
                continue
            res = None
            for attempt in range(FILL_VERIFY_ATTEMPTS):
                res = self._fill_reader(resp["orderid"])
                if res and res["status"] in ("filled", "rejected"):
                    break
                if attempt < FILL_VERIFY_ATTEMPTS - 1:
                    self._sleep(FILL_VERIFY_DELAY_S)
            if res and res["status"] == "filled":
                entry_px = float(r.entry_price or 0)
                charges = cnc_round_trip_charges(entry_px * qty, float(res["price"]) * qty)
                update_trade(
                    r.id,
                    exit_order_id=resp["orderid"],
                    exit_price=res["price"],
                    exit_at=self._utc_naive(self._now()),
                    exit_reason=reason,
                    charges_inr=charges,
                    entry_qty=qty if r.entry_qty is None else r.entry_qty,
                    status="closed",
                )
                out.append({"symbol": r.symbol, "status": "closed", "price": res["price"]})
            else:
                msg = (res or {}).get("message") or "exit not confirmed"
                update_trade(r.id, exit_order_id=resp["orderid"], error_message=f"exit {msg}")
                self._alert(f"⚠️ cas_auction_reversal: T+1 SELL not confirmed {r.symbol} — {msg}")
                out.append({"symbol": r.symbol, "status": "unconfirmed", "message": msg})
        logger.info("cas_ar %s: %s", reason, out)
        closed = [o for o in out if o["status"] == "closed"]
        if closed:
            self._alert(
                f"🔁 cas_auction_reversal {reason}: sold {len(closed)}/{len(rows)} — "
                + ", ".join(f"{o['symbol']}@{o['price']}" for o in closed[:12])
            )
        return {"status": "done", "results": out}

    def run_exit_retry(self) -> dict:
        return self.run_exit(reason="retry")

    # ----- misc ----------------------------------------------------------- #
    def _alert(self, message: str) -> None:
        try:
            self._notify(message)
        except Exception:
            logger.debug("cas_ar: alert failed", exc_info=True)

    def get_status(self) -> dict:
        d = self.day
        return {
            "strategy": STRATEGY_NAME,
            "trade_date": d.get("trade_date"),
            "mode": d.get("mode") or self.current_mode(),
            "armed": d.get("armed"),
            "manual_pause": self.manual_pause,
            "config": d.get("config") or resolve_config(),
            "universe": len(d.get("universe") or []),
            "polls": d.get("n_polls"),
            "last_poll_at": d["last_poll_at"].isoformat() if d.get("last_poll_at") else None,
            "decided": d.get("decided"),
            "candidates": d.get("candidates"),
            "entries": d.get("entries"),
            "summary": d.get("summary"),
            "schedule": {
                "exit": EXIT_TIME.isoformat(),
                "exit_retry": EXIT_RETRY_TIME.isoformat(),
                "arm": ARM_TIME.isoformat(),
                "decide": DECIDE_TIME.isoformat(),
                "fill": FILL_TIME.isoformat(),
                "summary": SUMMARY_TIME.isoformat(),
            },
        }

    def register_jobs(self, scheduler=None) -> None:
        sched = scheduler or self.scheduler
        if sched is None:
            from services.historify_scheduler_service import get_historify_scheduler

            sched = get_historify_scheduler().scheduler
        self.scheduler = sched
        from apscheduler.triggers.cron import CronTrigger

        global _SINGLETON
        _SINGLETON = self
        if self._journal:
            try:
                from database.cas_auction_reversal_db import init_db

                init_db()
            except Exception:
                logger.exception("cas_ar: journal init failed")

        def _cron(t: time):
            return CronTrigger(
                day_of_week="mon-fri",
                hour=t.hour,
                minute=t.minute,
                second=t.second,
                timezone="Asia/Kolkata",
            )

        jobs = (
            (
                _daily_reset_job,
                RESET_TIME,
                "cas_ar_daily_reset",
                "CAS auction reversal reset (09:00 IST)",
            ),
            (_exit_job, EXIT_TIME, "cas_ar_exit", "CAS auction reversal T+1 exit (09:16 IST)"),
            (
                _exit_retry_job,
                EXIT_RETRY_TIME,
                "cas_ar_exit_retry",
                "CAS auction reversal exit retry (09:20 IST)",
            ),
            (_arm_job, ARM_TIME, "cas_ar_arm", "CAS auction reversal arm (15:14:30 IST)"),
            (
                _decide_job,
                DECIDE_TIME,
                "cas_ar_decide",
                "CAS auction reversal decide (15:23:30 IST)",
            ),
            (_fill_job, FILL_TIME, "cas_ar_fill", "CAS auction reversal fill/verify (15:32 IST)"),
            (
                _eod_summary_job,
                SUMMARY_TIME,
                "cas_ar_eod_summary",
                "CAS auction reversal EOD summary (15:45 IST)",
            ),
        )
        for fn, t, job_id, name in jobs:
            sched.add_job(fn, trigger=_cron(t), id=job_id, replace_existing=True, name=name)
        logger.info("cas_ar jobs registered (mode=%s)", self.current_mode())


# --------------------------------------------------------------------------- #
# Module-level scheduler entry points + singleton
# --------------------------------------------------------------------------- #
_SINGLETON: CasAuctionReversalService | None = None


def get_service() -> CasAuctionReversalService | None:
    return _SINGLETON


def _run(method: str) -> None:
    if _SINGLETON is None:
        return
    try:
        getattr(_SINGLETON, method)()
    except Exception:
        logger.exception("cas_ar %s job raised", method)


def _daily_reset_job() -> None:
    _run("run_daily_reset")


def _exit_job() -> None:
    _run("run_exit")


def _exit_retry_job() -> None:
    _run("run_exit_retry")


def _arm_job() -> None:
    _run("arm")


def _decide_job() -> None:
    _run("run_decide")


def _fill_job() -> None:
    _run("run_fill")


def _eod_summary_job() -> None:
    _run("run_eod_summary")


def init_cas_auction_reversal_service(app=None, scheduler=None) -> CasAuctionReversalService:
    """Build the singleton and register its jobs. Unconditional at boot — trades
    the sandbox book by default; the /strategies toggle is the only way live."""
    svc = CasAuctionReversalService(app=app, scheduler=scheduler)
    svc.register_jobs(scheduler)
    _start_boot_catchup(svc)
    return svc


BOOT_CATCHUP_DELAY_S = 90  # let the broker session / master contract settle
BOOT_EXIT_LATEST = time(15, 10)  # CNC sells must land before CAS (15:15)


def _start_boot_catchup(svc: CasAuctionReversalService) -> None:
    """A restart must neither lose the day nor strand a CNC position:
    booted inside [arm, decide) → arm now; booted after the 09:16 exit (until
    15:10) → run the T+1 exit (idempotent — closed rows are not re-sold)."""

    def _catchup():
        try:
            svc._sleep(BOOT_CATCHUP_DELAY_S)
            t = svc._now().time()
            if ARM_TIME <= t < DECIDE_TIME:
                logger.info("cas_ar boot catch-up: arming inside the window")
                svc.arm()
            elif EXIT_TIME <= t < BOOT_EXIT_LATEST:
                logger.info("cas_ar boot catch-up: running the T+1 exit")
                svc.run_exit(reason="boot_catchup")
        except Exception:
            logger.exception("cas_ar boot catch-up raised")
        finally:
            _done(BOOT_THREAD)

    threading.Thread(target=_catchup, name="cas-auction-reversal-boot", daemon=True).start()


BOOT_THREAD = "cas-auction-reversal-boot"


__all__ = [
    "CasAuctionReversalService",
    "STRATEGY_NAME",
    "book_quantities",
    "cnc_round_trip_charges",
    "dislocation_pct",
    "get_service",
    "init_cas_auction_reversal_service",
    "resolve_config",
    "select_candidates",
    "size_qty",
]
