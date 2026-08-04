"""Live/sandbox service for the intraday_pullback_top2 combined strategy (issue #394).

Wraps the validated pure evaluator (``intraday_pullback_core``) with real data, order placement,
a shared 2-slot margin pool, journaling, scheduler jobs and a control surface — mirroring the
``futures_follow_service`` pattern (injectable providers with ``production_*`` defaults so unit
tests stay hermetic).

Data sources, by need:
  * 09:30 selection returns + intraday gate readings -> broker batched quotes (``get_multiquotes``,
    reusing sector_follow's provider), fallback to the scanner aggregator.
  * per-candle 5m trigger series -> the scanner aggregator (``ScannerService.get_today_bars``).
  * previous-day close -> historify 1m (last prior-day close).
  * order placement -> shared ``place_order`` (mode=sandbox routes to sandbox.db downstream).

Two books are mutually exclusive by day (NIFTY up -> long; down -> short), so the 2-slot pool is
never over-committed. Everything flattens at 15:15 via a tick-independent watchdog.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path

from apscheduler.triggers.cron import CronTrigger

from database import intraday_pullback_db as journal
from services.intraday_pullback_core import (
    TRADE_SIDES,
    EntryAction,
    ExitAction,
    GateContext,
    PullbackConfig,
    StockState,
    select_top2,
    trade_side_allows,
)
from utils.logging import get_logger

logger = get_logger(__name__)

STRATEGY_NAME = "intraday_pullback_top2"
VALID_MODES = ("sandbox", "live", "observe")
_IST = timezone(timedelta(hours=5, minutes=30))
_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "strategies" / STRATEGY_NAME / "config_snapshot.json"
)


# --------------------------------------------------------------------------------------------
# production providers (defaults; injectable for tests)
# --------------------------------------------------------------------------------------------


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except Exception:  # noqa: BLE001
        logger.exception("intraday_pullback: failed to load config_snapshot.json")
        return {}


def _load_sector_map() -> dict:
    try:
        p = _CONFIG_PATH.parent / "sector_map.json"
        data = json.loads(p.read_text())
        return data.get("mapping", data) if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        logger.exception("intraday_pullback: failed to load sector_map.json")
        return {}


def production_now() -> datetime:
    return datetime.now(_IST)


def production_broker_session_checker() -> bool:
    try:
        from database.auth_db import get_first_available_api_key

        return bool(get_first_available_api_key())
    except Exception:  # noqa: BLE001
        logger.debug("broker session check failed", exc_info=True)
        return False


def _prev_closes(all_syms: list[str], as_of: datetime) -> dict[str, float | None]:
    """Prior-day close per symbol from historify 1m (last close of most recent prior day)."""
    try:
        from services.sector_follow_service import _historical_metrics, production_history_reader

        window_start = int(as_of.timestamp()) - 25 * 86400
        raw = production_history_reader(all_syms, window_start)
        as_of_date = as_of.astimezone(_IST).date()
        out: dict[str, float | None] = {}
        for sym in all_syms:
            bars = raw.get(sym, [])
            prior_close, _ = _historical_metrics(bars, as_of_date, 20)
            out[sym] = prior_close
        return out
    except Exception:  # noqa: BLE001
        logger.exception("intraday_pullback: _prev_closes failed")
        return dict.fromkeys(all_syms)


def make_production_price_provider(universe: list[str], sector_map: dict):
    """Return ``price(symbol, as_of) -> float | None`` — broker batched quotes, aggregator fallback."""
    try:
        from services.sector_follow_service import make_quotes_intraday_provider

        prov = make_quotes_intraday_provider(universe=universe, sector_map=sector_map)

        def price(symbol: str, as_of: datetime):
            try:
                res = prov(symbol, as_of)
                return res[0] if isinstance(res, tuple) else res
            except Exception:  # noqa: BLE001
                logger.debug("price provider failed for %s", symbol, exc_info=True)
                return None

        return price
    except Exception:  # noqa: BLE001
        logger.exception("intraday_pullback: quotes provider unavailable; aggregator only")

        def price(symbol: str, as_of: datetime):
            try:
                from services.scanner_service import get_scanner_service

                svc = get_scanner_service()
                if svc is None:
                    return None
                c, _v = svc.get_today_ohlcv(symbol, as_of.astimezone(_IST).date())
                return c
            except Exception:  # noqa: BLE001
                return None

        return price


def _ts_to_ist(ts) -> datetime | None:
    """Normalize a get_history timestamp (epoch int/float or ISO str) to an IST datetime."""
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, _IST)
        d = datetime.fromisoformat(str(ts))
        return d if d.tzinfo else d.replace(tzinfo=_IST)
    except Exception:  # noqa: BLE001
        return None


def production_history_provider(symbol: str, exchange: str, interval: str, date_str: str) -> list:
    """Today's historical bars for ``symbol`` via the broker/historify history API.

    Returns ``[(ist_datetime, open, high, low, close, volume), ...]`` sorted by time, or [] on
    failure. Used only for RESUME (late boot / restart) — a normal intraday run uses the live
    aggregator/quotes. get_history enforces the broker 3 req/sec limit internally.
    """
    try:
        from database.auth_db import get_first_available_api_key
        from services.history_service import get_history

        ok, payload, _ = get_history(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            start_date=date_str,
            end_date=date_str,
            api_key=get_first_available_api_key(),
        )
        if not ok:
            return []
        out = []
        for r in (payload or {}).get("data") or []:
            ts = _ts_to_ist(r.get("timestamp"))
            if ts is None:
                continue
            out.append(
                (
                    ts,
                    r.get("open"),
                    r.get("high"),
                    r.get("low"),
                    float(r.get("close")),
                    float(r.get("volume") or 0),
                )
            )
        out.sort(key=lambda b: b[0])
        return out
    except Exception:  # noqa: BLE001
        logger.debug("history provider failed for %s", symbol, exc_info=True)
        return []


def production_bars_provider(symbol: str, as_of: datetime) -> list:
    """Today's CLOSED 5m bars for ``symbol`` from the scanner aggregator."""
    try:
        from services.scanner_service import get_scanner_service

        svc = get_scanner_service()
        if svc is None:
            return []
        return svc.get_today_bars(symbol, as_of.astimezone(_IST).date())
    except Exception:  # noqa: BLE001
        logger.debug("bars provider failed for %s", symbol, exc_info=True)
        return []


def production_order_placer(mode: str, order: dict) -> dict:
    """Place a MARKET order via the shared path. mode=sandbox routes to sandbox.db downstream."""
    if mode == "observe":
        return {"status": "success", "orderid": None, "observe": True}
    try:
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
    except Exception as e:  # noqa: BLE001
        logger.exception("intraday_pullback order placement failed: %s", e)
        return {"status": "error", "message": str(e)}


def production_notifier(message: str) -> None:
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("intraday_pullback", message)
    except Exception:  # noqa: BLE001
        logger.debug("notify failed", exc_info=True)


def _is_trading_day(d) -> bool:
    try:
        from services.data_freshness_service import is_trading_day

        return bool(is_trading_day(d))
    except Exception:  # noqa: BLE001
        return d.weekday() < 5


def _charges(buy_value: float, sell_value: float) -> float:
    try:
        from services.simplified_stock_engine_core import compute_zerodha_intraday_charges

        return compute_zerodha_intraday_charges(buy_value, sell_value).total
    except Exception:  # noqa: BLE001
        logger.debug("charges calc failed", exc_info=True)
        return 0.0


# --------------------------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------------------------


class IntradayPullbackService:
    def __init__(
        self,
        app=None,
        scheduler=None,
        *,
        mode: str | None = None,
        config: dict | None = None,
        sector_map: dict | None = None,
        price_provider=None,
        bars_provider=None,
        prev_close_provider=None,
        history_provider=None,
        order_placer=None,
        notifier=None,
        broker_session_checker=None,
        now=None,
    ):
        self.app = app
        self.scheduler = scheduler
        raw = config if config is not None else _load_config()
        self.raw_config = raw
        self.sector_map = sector_map if sector_map is not None else _load_sector_map()

        cap = raw.get("capital", {})
        self.base_capital = float(cap.get("base_capital", 60000))
        self.slots = int(cap.get("slots", 2))
        self.leverage = float(cap.get("leverage", 5))
        self.sizing_mode = str(cap.get("sizing_mode", "fixed")).lower()

        self.cfg = _build_pullback_config(raw)
        self._apply_editable_config()  # layer operator UI overrides over the JSON defaults
        self.universe = sorted(self.sector_map.keys())
        self.index_syms = sorted(set(self.sector_map.values()) | {"NIFTY"})

        # Mode resolution mirrors futures_follow / sector_follow: env var is the default,
        # a persistent strategy_mode[STRATEGY_NAME] row (set via strategy_mode_service.flip_mode
        # / the strategies dashboard toggle) overrides it. Actual sandbox-vs-live ORDER routing
        # is the per-strategy gate (place_order -> resolve_order_mode(STRATEGY_NAME), issue
        # #440), identical to every other strategy. An explicit constructor `mode` (tests) wins.
        self._mode_forced = mode is not None
        self.mode = (mode or os.getenv("INTRADAY_PULLBACK_MODE", "sandbox")).lower()
        if self.mode not in VALID_MODES:
            logger.warning("Unknown INTRADAY_PULLBACK_MODE=%s — forcing sandbox", self.mode)
            self.mode = "sandbox"
        self._apply_mode_override()

        self._price = price_provider or make_production_price_provider(
            self.universe + self.index_syms, self.sector_map
        )
        self._bars = bars_provider or production_bars_provider
        self._prev_closes = prev_close_provider or _prev_closes
        self._history = history_provider or production_history_provider
        self._place = order_placer or production_order_placer
        self._notify = notifier or production_notifier
        self._session_ok = broker_session_checker or production_broker_session_checker
        self._now = now or production_now

        self.strategy_id = _seed_strategy_id()
        self._reset_state()

    # -- state -------------------------------------------------------------------------------

    def _reset_state(self):
        self.side: str | None = None  # 'L' | 'S' | None
        self.picks: list[str] = []
        self.states: dict[str, StockState] = {}
        self.prev_close: dict[str, float] = {}
        self.nifty_930: float | None = None
        self.open_positions: dict[
            str, dict
        ] = {}  # symbol -> {trade_id, entry_price, qty, stop, side}
        self.open_count = 0
        self.last_fed: dict[str, int] = {}
        self.pick_meta: dict[
            str, dict
        ] = {}  # sym -> {gain930, sector, sector930} for the breakdown
        self.selected = False
        # Why the day produced no picks, when that was a deliberate gate rather
        # than a data gap (issue #509) — surfaced on get_status/entry_breakdown
        # so a trade_side skip is never mistaken for a broken feed.
        self.skip_reason: str | None = None
        self.manual_pause = False
        self.kill_switch = False
        self.today_realized = 0.0
        self.today_date = self._now().astimezone(_IST).date().isoformat()

    def _apply_mode_override(self):
        """Escalate/downgrade self.mode from a persistent strategy_mode row (aligned with
        futures_follow / sector_follow). Only a row with source=='strategy_mode' overrides;
        env/default sources leave the env-resolved mode untouched. Never raises. A constructor-
        forced mode (tests) is never overridden. Re-run each daily reset so an operator flip via
        the strategies dashboard takes effect next trading day without a restart."""
        if self._mode_forced:
            return
        try:
            from services.mode_service import resolve_mode

            rm = resolve_mode(STRATEGY_NAME)
            if rm.source == "strategy_mode" and rm.mode in ("live", "sandbox"):
                if rm.mode != self.mode:
                    logger.info(
                        "intraday_pullback mode override: %s -> %s (strategy_mode row)",
                        self.mode,
                        rm.mode,
                    )
                self.mode = rm.mode
        except Exception:  # noqa: BLE001
            logger.debug("intraday_pullback mode override resolve failed", exc_info=True)

    def _apply_editable_config(self):
        """Layer operator-editable settings (base capital, sizing mode, no-trade + afternoon
        windows) from the intraday_pullback_config DB table over the config_snapshot.json
        defaults. Windows map: morning=[09:30, no_trade_start]; afternoon=[afternoon_start,
        afternoon_end] (no_trade_end == afternoon_start, contiguous). Never raises."""
        try:
            from dataclasses import replace

            from database.intraday_pullback_config_db import get_config

            # 1) reset editable fields to the config_snapshot.json defaults, so a deleted row
            #    (Reset to defaults) reverts cleanly on the next apply.
            cap = self.raw_config.get("capital", {})
            self.base_capital = float(cap.get("base_capital", 60000))
            self.sizing_mode = str(cap.get("sizing_mode", "fixed")).lower()
            self.cfg = _build_pullback_config(self.raw_config)
            # 2) layer any persisted operator overrides on top.
            row = get_config(STRATEGY_NAME)
            if not row:
                return
            if row.get("base_capital"):
                self.base_capital = float(row["base_capital"])
            if row.get("sizing_mode") in ("fixed", "compound", "capped"):
                self.sizing_mode = row["sizing_mode"]
            m_end = _parse_time(row.get("no_trade_start")) or self.cfg.morning[1]
            a_start = _parse_time(row.get("afternoon_start")) or self.cfg.afternoon[0]
            a_end = _parse_time(row.get("afternoon_end")) or self.cfg.afternoon[1]
            # issue #509: a stored value outside the enum is ignored (keep the
            # env/JSON default) rather than darkening a book on bad data.
            side_sel = str(row.get("trade_side") or "").lower()
            trade_side = side_sel if side_sel in TRADE_SIDES else self.cfg.trade_side
            self.cfg = replace(
                self.cfg,
                morning=(self.cfg.morning[0], m_end),
                afternoon=(a_start, a_end),
                trade_side=trade_side,
            )
        except Exception:  # noqa: BLE001
            logger.debug("intraday_pullback editable-config load failed", exc_info=True)

    def current_settings(self) -> dict:
        """The effective editable settings + computed fields, for the settings UI GET."""
        realized = _cumulative_realized(self.strategy_id, self.mode)
        return {
            "base_capital": self.base_capital,
            "sizing_mode": self.sizing_mode,
            "trade_side": self.cfg.trade_side,
            "slots": self.slots,
            "margin_per_slot": round(self.base_capital / self.slots, 0),
            "morning": [
                self.cfg.morning[0].strftime("%H:%M"),
                self.cfg.morning[1].strftime("%H:%M"),
            ],
            "no_trade": [
                self.cfg.morning[1].strftime("%H:%M"),
                self.cfg.afternoon[0].strftime("%H:%M"),
            ],
            "afternoon": [
                self.cfg.afternoon[0].strftime("%H:%M"),
                self.cfg.afternoon[1].strftime("%H:%M"),
            ],
            "eod_flatten": self.cfg.eod_flatten.strftime("%H:%M"),
            "realized_pnl_to_date": round(realized, 0),
            "deployable_capital": round(self.deployable_capital(), 0),
        }

    def run_daily_reset(self):
        self._apply_mode_override()
        self._apply_editable_config()
        logger.info(
            "intraday_pullback daily reset (mode=%s cap=%.0f sizing=%s trade_side=%s)",
            self.mode,
            self.base_capital,
            self.sizing_mode,
            self.cfg.trade_side,
        )
        self._reset_state()

    # -- sizing ------------------------------------------------------------------------------

    def deployable_capital(self) -> float:
        if self.sizing_mode == "fixed":
            return self.base_capital
        realized = _cumulative_realized(self.strategy_id, self.mode)
        equity = self.base_capital + realized
        if self.sizing_mode == "capped":
            return min(equity, self.base_capital)
        return max(equity, 0.0)  # compound

    def _notional_per_slot(self) -> float:
        return (self.deployable_capital() / self.slots) * self.leverage

    # -- override / kill ---------------------------------------------------------------------

    def _entry_blocked(self) -> bool:
        if self.manual_pause or self.kill_switch:
            return True
        try:
            from database.strategy_runtime_override_db import is_entry_blocked

            blocked, _ = is_entry_blocked(STRATEGY_NAME)
            return bool(blocked)
        except Exception:  # noqa: BLE001
            return False

    def _check_kill_switch(self):
        thr = -0.03 * self.base_capital
        if not self.kill_switch and self.today_realized < thr:
            self.kill_switch = True
            msg = f"🚨 intraday_pullback kill switch: today realized {self.today_realized:+.0f} < {thr:.0f}"
            logger.warning(msg)
            self._notify(msg)

    # -- selection ---------------------------------------------------------------------------

    def _price_0930_hist(self, sym: str, date_str: str) -> float | None:
        """The stock's price AT 09:30 today from the history API (last 1m close <= 09:30).

        Used on a LATE boot so the 09:30 gain is measured at 09:30, not at the current time."""
        exch = "NSE_INDEX" if sym in self.index_syms else "NSE"
        p = None
        for ts, _o, _h, _lo, c, _v in self._history(sym, exch, "1m", date_str):
            if ts.astimezone(_IST).time() <= self.cfg.morning[0]:
                p = c
            else:
                break
        return p

    def run_selection(self, now: datetime | None = None, historical: bool = False):
        now = now or self._now()
        all_syms = self.universe + self.index_syms
        date_str = now.astimezone(_IST).date().isoformat()
        prev = self._prev_closes(all_syms, now)
        self.prev_close = {k: v for k, v in prev.items() if v}
        rets: dict[str, float] = {}
        for sym in all_syms:
            pc = self.prev_close.get(sym)
            if not (pc and pc > 0):
                continue
            px = self._price_0930_hist(sym, date_str) if historical else self._price(sym, now)
            if px:
                rets[sym] = (px / pc - 1.0) * 100.0
        nifty = rets.get("NIFTY")
        if nifty is None:
            logger.warning("intraday_pullback: no NIFTY 09:30 reading — skipping day")
            self.selected = True
            return
        self.nifty_930 = nifty
        if nifty > 0:
            self.side = "L"
        elif nifty < 0:
            self.side = "S"
        else:
            logger.info("intraday_pullback: NIFTY flat at 09:30 — no book today")
            self.selected = True
            return
        # Operator trade-side gate (issue #509). The books are mutually exclusive
        # by the day gate above, so an excluded side means NO TRADING today —
        # not a switch to the other book. Enforced here, before selection, so the
        # excluded side is never picked, never watched, never journals a row.
        if not trade_side_allows(self.cfg.trade_side, self.side):
            logger.info(
                "intraday_pullback: NIFTY %s at 09:30 -> %s book, but trade_side=%s "
                "— no trading today",
                "up" if self.side == "L" else "down",
                "long" if self.side == "L" else "short",
                self.cfg.trade_side,
            )
            self.skip_reason = f"trade_side={self.cfg.trade_side}"
            self.selected = True
            return
        sector_returns = {idx: rets.get(idx) for idx in self.index_syms}
        stock_returns = {s: rets.get(s) for s in self.universe if rets.get(s) is not None}
        self.picks = select_top2(
            self.side, stock_returns, self.sector_map, sector_returns, self.cfg
        )
        self.states = {s: StockState(self.side, self.cfg) for s in self.picks}
        self.last_fed = dict.fromkeys(self.picks, 0)
        self.pick_meta = {
            s: {
                "gain930": round(rets.get(s), 3) if rets.get(s) is not None else None,
                "sector": self.sector_map.get(s),
                "sector930": (
                    round(rets.get(self.sector_map.get(s)), 3)
                    if rets.get(self.sector_map.get(s)) is not None
                    else None
                ),
            }
            for s in self.picks
        }
        self.selected = True
        logger.info(
            "intraday_pullback selection: side=%s nifty930=%.2f%% picks=%s",
            self.side,
            nifty,
            self.picks,
        )

    # -- evaluation tick ---------------------------------------------------------------------

    def run_eval_tick(self, now: datetime | None = None):
        now = now or self._now()
        ist = now.astimezone(_IST)
        if not _is_trading_day(ist.date()):
            return
        # roll to a new day defensively
        if ist.date().isoformat() != self.today_date:
            self.run_daily_reset()
        if ist.time() < self.cfg.morning[0] or ist.time() >= self.cfg.eod_flatten:
            return
        if not self.selected:
            if self._entry_blocked():
                logger.info("intraday_pullback: entries held by override — skipping selection")
                self.selected = True
                return
            # Resume if OpenAlgo booted late / restarted mid-session (past the live 09:30 tick):
            # reconstruct the day's state from the journal, or historically re-select, instead of
            # measuring the "09:30 gain" at the wrong (current) time.
            if ist.time() > _time_plus(self.cfg.morning[0], 3):
                self._resume(now)
            else:
                self.run_selection(now)
        managed = set(self.picks) | set(self.open_positions)
        if not managed:
            return
        for sym in self.picks + [s for s in self.open_positions if s not in self.picks]:
            self._feed_symbol(sym, now)

    # -- resume (late boot / restart) --------------------------------------------------------

    def _resume(self, now: datetime):
        """Rebuild the day's state after a late boot / mid-session restart.

        If today's journal has this strategy's rows -> reconstruct picks/side/attempts/open
        positions from them (fast, authoritative — no re-selection). Otherwise (booted late,
        never traded) -> historically re-select at 09:30 so it can still trade the remaining
        windows. Idempotent: reconciled positions are managed (stop/EOD) and prior entries count
        toward max-attempts so nothing is double-placed."""
        today = now.astimezone(_IST).date().isoformat()
        try:
            rows = [
                r
                for r in journal.get_trades(self.strategy_id, trade_date=today)
                if r["mode"] == self.mode
            ]
        except Exception:  # noqa: BLE001
            logger.exception("intraday_pullback resume: journal read failed")
            rows = []
        if rows:
            self._reconstruct_from_journal(now, rows)
        else:
            logger.info("intraday_pullback resume: no journal rows today — historical re-select")
            self.run_selection(now, historical=True)
        self.selected = True

    def _reconstruct_from_journal(self, now: datetime, rows: list):
        self.side = rows[0]["side"]
        for r in rows:
            g = r.get("gate") or {}
            if g.get("nifty_930") is not None:
                self.nifty_930 = g["nifty_930"]
                break
        picks: list[str] = []
        for r in rows:
            if r["symbol"] not in picks:
                picks.append(r["symbol"])
        self.picks = picks
        needed = set(picks) | {self.sector_map.get(s, "NIFTY") for s in picks} | {"NIFTY"}
        prev = self._prev_closes(sorted(needed), now)
        self.prev_close = {k: v for k, v in prev.items() if v}
        self.states = {}
        for sym in picks:
            st = StockState(self.side, self.cfg)
            srows = [r for r in rows if r["symbol"] == sym]
            st.attempts = len(srows)
            if st._noreentry and any(
                r["status"] == "closed" and r["exit_reason"] == "SL" for r in srows
            ):
                st.done = True
            openr = next((r for r in srows if r["status"] == "open"), None)
            if openr is not None:
                ets = _parse_iso(openr.get("entry_time")) or now
                st.pos = (ets, openr["entry_price"], openr.get("stop_price"))
                self.open_positions[sym] = {
                    "trade_id": openr["id"],
                    "entry_price": openr["entry_price"],
                    "qty": openr["quantity"],
                    "stop": openr.get("stop_price"),
                    "side": self.side,
                }
                self.open_count += 1
            self.states[sym] = st
            self.pick_meta[sym] = {
                "sector": (srows[0].get("gate") or {}).get("sector") or self.sector_map.get(sym),
                "gain930": None,  # not recoverable from the journal
                "sector930": None,
            }
            self.last_fed[sym] = 0
        logger.info(
            "intraday_pullback resume: reconstructed from journal side=%s picks=%s open=%d",
            self.side,
            picks,
            self.open_count,
        )

    def _feed_symbol(self, sym: str, now: datetime):
        st = self.states.get(sym)
        if st is None:
            return
        bars = self._bars(sym, now)
        start = self.last_fed.get(sym, 0)
        if len(bars) <= start:
            return
        nf_now = self._intraday_ret("NIFTY", now)
        sector = self.sector_map.get(sym, "NIFTY")
        sec_now = self._intraday_ret(sector, now)
        for candle in bars[start:]:
            ctx = GateContext(
                nifty_ret_now=nf_now,
                sector_ret_now=sec_now,
                nifty_ret_930=self.nifty_930,
                slot_available=(self.open_count < self.slots) and not self._entry_blocked(),
            )
            for action in st.process_candle(candle, ctx):
                self._apply(sym, sector, action)
        self.last_fed[sym] = len(bars)

    def _intraday_ret(self, sym: str, now: datetime) -> float | None:
        pc = self.prev_close.get(sym)
        px = self._price(sym, now)
        if pc and pc > 0 and px:
            return (px / pc - 1.0) * 100.0
        return None

    # -- order actions -----------------------------------------------------------------------

    def _apply(self, sym: str, sector: str, action):
        if isinstance(action, EntryAction):
            self._place_entry(sym, sector, action)
        elif isinstance(action, ExitAction):
            self._place_exit(sym, action)

    def _place_entry(self, sym: str, sector: str, action: EntryAction):
        if self.open_count >= self.slots:
            return
        qty = int(self._notional_per_slot() / action.price) if action.price else 0
        if qty <= 0:
            logger.warning("intraday_pullback: qty<=0 for %s @ %.2f — skip", sym, action.price)
            return
        broker_action = "BUY" if action.side == "L" else "SELL"
        resp = self._place(
            self.mode,
            {
                "symbol": sym,
                "exchange": "NSE",
                "action": broker_action,
                "product": "MIS",
                "quantity": qty,
            },
        )
        status = (resp or {}).get("status")
        order_id = (resp or {}).get("orderid")
        gate = {"nifty_930": self.nifty_930, "sector": sector, "stop": action.stop}
        if status != "success":
            journal.record_entry(
                strategy_id=self.strategy_id,
                mode=self.mode,
                side=action.side,
                symbol=sym,
                trade_date=self.today_date,
                quantity=qty,
                sector=sector,
                entry_time=action.ts,
                entry_price=action.price,
                stop_price=action.stop,
                status="rejected",
                error_message=str((resp or {}).get("message"))[:250],
                gate=gate,
            )
            logger.warning("intraday_pullback entry REJECTED %s: %s", sym, resp)
            return
        session = "MORNING" if action.ts.time() < self.cfg.morning[1] else "AFT"
        tid = journal.record_entry(
            strategy_id=self.strategy_id,
            mode=self.mode,
            side=action.side,
            symbol=sym,
            trade_date=self.today_date,
            quantity=qty,
            sector=sector,
            session=session,
            entry_time=action.ts,
            entry_price=action.price,
            stop_price=action.stop,
            entry_order_id=order_id,
            status="open",
            gate=gate,
        )
        self.open_positions[sym] = {
            "trade_id": tid,
            "entry_price": action.price,
            "qty": qty,
            "stop": action.stop,
            "side": action.side,
        }
        self.open_count += 1
        logger.info(
            "intraday_pullback ENTRY %s %s qty=%d @ %.2f stop=%.2f",
            sym,
            broker_action,
            qty,
            action.price,
            action.stop,
        )

    def _place_exit(self, sym: str, action: ExitAction, *, watchdog: bool = False):
        pos = self.open_positions.get(sym)
        if pos is None:
            return
        qty = pos["qty"]
        side = pos["side"]
        exit_action = "SELL" if side == "L" else "BUY"
        resp = self._place(
            self.mode,
            {
                "symbol": sym,
                "exchange": "NSE",
                "action": exit_action,
                "product": "MIS",
                "quantity": qty,
            },
        )
        entry = pos["entry_price"]
        px = action.price
        gross = (px - entry) * qty if side == "L" else (entry - px) * qty
        buy_val = entry * qty if side == "L" else px * qty
        sell_val = px * qty if side == "L" else entry * qty
        ch = _charges(buy_val, sell_val)
        net = gross - ch
        journal.close_trade(
            pos["trade_id"],
            exit_time=action.ts,
            exit_price=px,
            exit_reason=action.reason,
            gross_pnl=round(gross, 2),
            charges_inr=round(ch, 2),
            net_pnl=round(net, 2),
            exit_order_id=(resp or {}).get("orderid"),
            status="closed",
        )
        self.today_realized += net
        self.open_positions.pop(sym, None)
        self.open_count = max(0, self.open_count - 1)
        self._check_kill_switch()
        logger.info(
            "intraday_pullback EXIT %s %s qty=%d @ %.2f [%s] net=%.0f%s",
            sym,
            exit_action,
            qty,
            px,
            action.reason,
            net,
            " (watchdog)" if watchdog else "",
        )

    # -- EOD ---------------------------------------------------------------------------------

    def run_eod_flatten(self, now: datetime | None = None):
        now = now or self._now()
        if not self.open_positions:
            return
        logger.info("intraday_pullback EOD flatten watchdog: %d open", len(self.open_positions))
        for sym in list(self.open_positions.keys()):
            st = self.states.get(sym)
            px = self._price(sym, now) or self.open_positions[sym]["entry_price"]
            if st is not None:
                st.force_eod(now, px)
            self._place_exit(sym, ExitAction(ts=now, price=px, reason="EOD"), watchdog=True)

    def run_eod_summary(self, now: datetime | None = None):
        try:
            perf = journal.performance_by_side(
                self.strategy_id, date_from=self.today_date, date_to=self.today_date, mode=self.mode
            )
            c = perf["combined"]
            msg = (
                f"📊 intraday_pullback EOD {self.today_date} ({self.mode}): "
                f"{c['trades']} trades, WR {c['win_rate']}%, PF {c['profit_factor']}, "
                f"net ₹{c['net_pnl']:.0f} | long {perf['long']['net_pnl']:.0f} / "
                f"short {perf['short']['net_pnl']:.0f}"
            )
            logger.info(msg)
            self._notify(msg)
        except Exception:  # noqa: BLE001
            logger.exception("intraday_pullback EOD summary failed")
        # persist the per-pick evaluation breakdown so a zero-signal day stays explainable
        try:
            from database import intraday_pullback_eval_db

            intraday_pullback_eval_db.upsert_snapshot(
                STRATEGY_NAME, self.today_date, self.entry_breakdown()
            )
        except Exception:  # noqa: BLE001
            logger.exception("intraday_pullback EOD breakdown persist failed")

    # -- smoke check -------------------------------------------------------------------------

    def assert_data_pipeline_healthy(self, now: datetime | None = None) -> tuple[bool, dict]:
        now = now or self._now()
        session_ok = self._session_ok()
        n_have = 0
        for sym in self.universe:
            if self._price(sym, now) is not None:
                n_have += 1
        cov = n_have / len(self.universe) if self.universe else 0.0
        min_cov = float(os.getenv("INTRADAY_PULLBACK_SMOKE_MIN_COVERAGE", "0.5"))
        ok = session_ok and cov >= min_cov
        detail = {"session_ok": session_ok, "coverage": round(cov, 3), "min_coverage": min_cov}
        if not ok:
            reason = f"session_ok={session_ok} coverage={cov:.0%}<{min_cov:.0%}"
            self._hold_today(now, reason)
            self._notify(
                f"🚨 intraday_pullback 09:18 SMOKE CHECK FAILED ({now.astimezone(_IST).date()}): "
                f"{reason}. Holding today's entries."
            )
        return ok, detail

    def _hold_today(self, now: datetime, reason: str):
        try:
            from database.strategy_runtime_override_db import set_override

            expires = now.astimezone(_IST).replace(hour=15, minute=30, second=0, microsecond=0)
            set_override(
                STRATEGY_NAME,
                "pause",
                expires.astimezone(UTC).replace(tzinfo=None),
                reason=f"smoke_check_failed: {reason}",
                set_by="intraday_pullback",
            )
        except Exception:  # noqa: BLE001
            logger.exception("intraday_pullback: failed to write hold override")

    # -- control -----------------------------------------------------------------------------

    def pause(self):
        self.manual_pause = True
        return {"status": "success", "manual_pause": True}

    def resume(self):
        self.manual_pause = False
        self.kill_switch = False
        try:
            from database.strategy_runtime_override_db import clear_override

            clear_override(STRATEGY_NAME)
        except Exception:  # noqa: BLE001
            pass
        return {"status": "success", "manual_pause": False}

    def close_all(self, now: datetime | None = None):
        now = now or self._now()
        closed = list(self.open_positions.keys())
        self.run_eod_flatten(now)
        return closed

    def entry_breakdown(self) -> dict:
        """Per-pick evaluation for the day, so a zero-signal day explains itself.

        For each 09:30-selected stock: its selection numbers, the running trigger diagnostics
        (references formed, breakouts seen, gate/slot blocks, entries/exits), a one-line reason,
        and its current position status. Live from in-memory state; a snapshot is persisted at EOD.
        """
        evaluation = []
        for sym in self.picks:
            st = self.states.get(sym)
            meta = self.pick_meta.get(sym, {})
            pos = self.open_positions.get(sym)
            status = "none"
            if pos:
                status = "open"
            elif st and st.attempts > 0:
                status = "closed"
            evaluation.append(
                {
                    "symbol": sym,
                    "sector": meta.get("sector"),
                    "gain_930_pct": meta.get("gain930"),
                    "sector_930_pct": meta.get("sector930"),
                    "diag": dict(st.diag) if st else None,
                    "reason": st.reason() if st else "not evaluated",
                    "position": status,
                }
            )
        return {
            "date": self.today_date,
            "mode": self.mode,
            "side_today": self.side,
            "nifty_930_pct": self.nifty_930,
            "selected": self.selected,
            # issue #509: an operator trade-side gate is a DELIBERATE no-trade
            # day. Naming it here keeps it distinguishable from a data gap.
            "trade_side": self.cfg.trade_side,
            "skip_reason": self.skip_reason,
            "picks": self.picks,
            "n_trades_today": sum(1 for e in evaluation if e["position"] != "none"),
            "evaluation": evaluation,
        }

    def get_status(self) -> dict:
        perf = journal.performance_by_side(
            self.strategy_id, date_from=self.today_date, date_to=self.today_date, mode=self.mode
        )
        return {
            "strategy": STRATEGY_NAME,
            "mode": self.mode,
            "side_today": self.side,
            "nifty_930": self.nifty_930,
            "picks": self.picks,
            "selected": self.selected,
            "manual_pause": self.manual_pause,
            "kill_switch": self.kill_switch,
            "open_count": self.open_count,
            "slots": self.slots,
            "base_capital": self.base_capital,
            "deployable_capital": round(self.deployable_capital(), 0),
            "sizing_mode": self.sizing_mode,
            "trade_side": self.cfg.trade_side,
            "skip_reason": self.skip_reason,
            "today_realized_net": round(self.today_realized, 0),
            "open_positions": [
                {"symbol": s, **{k: v for k, v in p.items() if k != "trade_id"}}
                for s, p in self.open_positions.items()
            ],
            "today_evaluation": self.entry_breakdown(),
            "performance": perf,
        }

    # -- scheduler ---------------------------------------------------------------------------

    def register_jobs(self, scheduler=None):
        global _SINGLETON
        sched = scheduler or self.scheduler
        if sched is None:
            from services.historify_scheduler_service import get_historify_scheduler

            sched = get_historify_scheduler().scheduler
        _SINGLETON = self
        tz = "Asia/Kolkata"
        sched.add_job(
            _daily_reset_job,
            CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=tz),
            id="intraday_pullback_daily_reset",
            replace_existing=True,
            name="Intraday Pullback daily reset (09:00 IST)",
        )
        if os.getenv("INTRADAY_PULLBACK_SMOKE_CHECK_ENABLED", "true").lower() == "true":
            sched.add_job(
                _smoke_check_job,
                CronTrigger(day_of_week="mon-fri", hour=9, minute=18, timezone=tz),
                id="intraday_pullback_smoke_check",
                replace_existing=True,
                name="Intraday Pullback smoke check (09:18 IST)",
            )
        sched.add_job(
            _eval_tick_job,
            CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5", timezone=tz),
            id="intraday_pullback_eval",
            replace_existing=True,
            name="Intraday Pullback 5m evaluation",
        )
        sched.add_job(
            _eod_flatten_job,
            CronTrigger(day_of_week="mon-fri", hour=15, minute=15, timezone=tz),
            id="intraday_pullback_eod_flatten",
            replace_existing=True,
            name="Intraday Pullback EOD flatten (15:15 IST)",
        )
        sched.add_job(
            _eod_summary_job,
            CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone=tz),
            id="intraday_pullback_eod_summary",
            replace_existing=True,
            name="Intraday Pullback EOD summary (15:30 IST)",
        )
        logger.info("intraday_pullback jobs registered (mode=%s)", self.mode)
        self._start_boot_resume()

    def _start_boot_resume(self):
        """On boot, if we're already inside the trading session (late start / restart), wait for a
        broker session and run one eval tick immediately so the day resumes without waiting for the
        next 5-min tick. Daemon thread; never blocks boot. Gated by INTRADAY_PULLBACK_BOOT_RESUME."""
        if os.getenv("INTRADAY_PULLBACK_BOOT_RESUME_ENABLED", "true").lower() != "true":
            return
        try:
            ist = self._now().astimezone(_IST)
        except Exception:  # noqa: BLE001
            return
        if not (self.cfg.morning[0] < ist.time() < self.cfg.eod_flatten):
            return  # only a boot DURING the session needs an immediate resume

        import threading
        import time as _time

        def _worker():
            from services.thread_registry import beat as _beat

            _beat("IntradayPullbackBootResume")
            for _ in range(40):  # ~10 min: wait for a broker session to appear
                if self._session_ok():
                    break
                _time.sleep(15)
            try:
                logger.info("intraday_pullback boot-resume: running immediate eval tick")
                self.run_eval_tick()
            except Exception:  # noqa: BLE001
                logger.exception("intraday_pullback boot-resume tick failed")

        threading.Thread(target=_worker, name="IntradayPullbackBootResume", daemon=True).start()


# --------------------------------------------------------------------------------------------
# helpers + module-level singleton / job dispatchers
# --------------------------------------------------------------------------------------------


def _parse_time(s) -> time | None:
    """Parse 'HH:MM' -> datetime.time, or None if unparseable/empty."""
    if not s:
        return None
    try:
        hh, mm = str(s).split(":")
        return time(int(hh), int(mm))
    except Exception:  # noqa: BLE001
        return None


def _time_plus(t: time, mins: int) -> time:
    """Add minutes to a time-of-day (clamped within the day)."""
    total = t.hour * 60 + t.minute + mins
    return time((total // 60) % 24, total % 60)


def _parse_iso(s) -> datetime | None:
    """Parse an ISO datetime string (journal entry_time) -> datetime, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except Exception:  # noqa: BLE001
        return None


def _build_pullback_config(raw: dict) -> PullbackConfig:
    def _t(s, default):
        try:
            hh, mm = str(s).split(":")
            return time(int(hh), int(mm))
        except Exception:  # noqa: BLE001
            return default

    w = raw.get("windows_ist", {})
    lng = raw.get("long", {})
    sht = raw.get("short", {})
    return PullbackConfig(
        morning=(
            _t(w.get("morning", ["09:30"])[0], time(9, 30)),
            _t(w.get("morning", ["", "11:00"])[1], time(11, 0)),
        ),
        afternoon=(
            _t(w.get("afternoon", ["13:00"])[0], time(13, 0)),
            _t(w.get("afternoon", ["", "15:00"])[1], time(15, 0)),
        ),
        eod_flatten=_t(w.get("eod_flatten", "15:15"), time(15, 15)),
        long_band=tuple(lng.get("band_pct", [1.0, 2.5])),
        long_nf_mom=bool(lng.get("nf_mom", True)),
        long_noreentry_sl=bool(lng.get("noreentry_after_sl", True)),
        short_band=tuple(sht.get("band_pct", [-5.0, -3.0])),
        short_nf_mom=bool(sht.get("nf_mom", False)),
        short_noreentry_sl=bool(sht.get("noreentry_after_sl", False)),
        vol_multiplier=float(lng.get("vol_multiplier", 2.5)),
        vol_avg_window=int(lng.get("vol_avg_window", 6)),
        market_gate_pct=float(lng.get("fresh_gate_nifty_pct", 0.30)),
        stop_floor_pct=float(lng.get("stop_floor_pct", 0.3)),
        max_attempts=int(lng.get("max_attempts", 2)),
        trade_side=_env_trade_side(raw),
    )


def _env_trade_side(raw: dict) -> str:
    """Resolve the trade-side default: env var, else config_snapshot, else 'both'.

    An unrecognised value falls back to 'both' with a WARNING rather than
    darkening a book on a typo (issue #509).
    """
    raw_val = os.getenv("INTRADAY_PULLBACK_TRADE_SIDE") or raw.get("trade_side") or "both"
    val = str(raw_val).strip().lower()
    if val not in TRADE_SIDES:
        logger.warning("intraday_pullback: invalid trade_side %r — falling back to 'both'", raw_val)
        return "both"
    return val


def _seed_strategy_id() -> int | None:
    try:
        journal.init_db()
        from database import intraday_pullback_config_db, intraday_pullback_eval_db

        intraday_pullback_config_db.init_db()
        intraday_pullback_eval_db.init_db()
    except Exception:  # noqa: BLE001
        logger.debug("journal/config/eval init_db failed", exc_info=True)
    return None


def _cumulative_realized(strategy_id, mode) -> float:
    try:
        trades = journal.get_trades(strategy_id, mode=mode)
        return sum(
            t["net_pnl"] for t in trades if t["status"] == "closed" and t["net_pnl"] is not None
        )
    except Exception:  # noqa: BLE001
        return 0.0


_SINGLETON: IntradayPullbackService | None = None


def get_service() -> IntradayPullbackService | None:
    return _SINGLETON


def _safe(fn):
    try:
        if _SINGLETON is not None:
            fn(_SINGLETON)
    except Exception:  # noqa: BLE001
        logger.exception("intraday_pullback scheduled job failed")


def _daily_reset_job():
    _safe(lambda s: s.run_daily_reset())


def _smoke_check_job():
    _safe(lambda s: s.assert_data_pipeline_healthy())


def _eval_tick_job():
    _safe(lambda s: s.run_eval_tick())


def _eod_flatten_job():
    _safe(lambda s: s.run_eod_flatten())


def _eod_summary_job():
    _safe(lambda s: s.run_eod_summary())


def init_intraday_pullback_service(app=None, scheduler=None) -> IntradayPullbackService:
    svc = IntradayPullbackService(app=app, scheduler=scheduler)
    svc.register_jobs(scheduler)
    if app is not None:
        app.intraday_pullback_service = svc
    return svc
