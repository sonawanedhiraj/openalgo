"""open15_vol_breakout — opening 15-min mid-bar volume-surge breakout (issue #425).

SANDBOX strategy AND live measurement instrument. Round 58 established that on
1-minute bars this signal has no honest edge: the bar-close-confirmed volume
gate forces entry at the bar CLOSE (entry at the trigger level is look-ahead),
and the close entry loses (−0.16%/trade). The ONE untested legal variant is the
mid-bar entry: watch volume ACCUMULATE within the minute via live ticks and
enter the moment ``cumvol_in_minute >= 1.5x running-avg full-minute volume``
AND price is beyond the first-candle level — both facts are true in real time
at that second, so the entry is legal. Whether it captures enough of the
~0.54% intra-bar burst is unknowable from bars; this deployment measures it
with real (sandbox) fills. Full spec: ``strategies/open15_vol_breakout/SPEC.md``.

Rules (locked; see SPEC):
  - Window opens 09:15 IST. Universe = SCANNER_SYMBOLS F&O stocks
    (indices excluded via ``resolve_exchange_for_symbol``).
  - First candle (09:15) open/H1/L1 comes from ONE batched broker quote
    snapshot at 09:16 (issue #502). Ticks remain the source of the WITHIN-minute
    volume accumulation — the one thing bars cannot provide and the whole
    reason this strategy is tick-driven — but they are a ~1/sec sample that
    starts whenever the first tick arrives, so they must not define the open
    or the breakout level. Per-symbol fail-open to the tick-built candle.
  - Selection at 09:16: top-N gainers (LONG) / top-N losers (SHORT) by
    gap = 09:15 open / prev daily close − 1 (prev close from historify D).
  - Entry: tick-driven mid-bar trigger, once per symbol, MARKET MIS. New
    entries stop at the UI-configurable ``no_entry_after`` cutoff (default
    09:29 = the measured SPEC window; issue #451).
  - Exit: hard flatten at the UI-configurable ``exit_time`` (default 09:30;
    retry backstop +2 min, capped 15:10). No stop/target. A non-default
    window departs from the R58-measured 09:29/09:30 convention — the day's
    ``armed`` decision-log event records the effective window.
  - Journal (``open15_trades``) records level / trigger second / trigger price /
    entry-minute close — the captured-drift measurement.

Tick source: own additive ZMQ SUB on the proxy bus (tcp://127.0.0.1:5555) —
mirrors ``ScannerService``'s subscription; touches no scanner code. The
subscriber thread only processes ticks inside a small window around the
session open, so its off-hours cost is one poll timeout per second.

Ops note: the app must be RUNNING BEFORE 09:15 IST for the first candle to be
observed. A late boot on a trading day marks the day skipped (loud WARNING).
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from statistics import mean
from typing import Any

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
STRATEGY_NAME = "open15_vol_breakout"

_FIRST_MIN = 9 * 60 + 15  # 555 = 09:15
_ENTRY_FROM = _FIRST_MIN + 1  # 09:16
_ENTRY_TO = 9 * 60 + 29  # 09:29 inclusive (default; UI-overridable, issue #451)
_EXIT_MIN = 9 * 60 + 30  # 09:30 (default; UI-overridable, issue #451)

# UI-configurable window defaults (issue #451). Exit is capped at 15:10 so the
# +2 min retry backstop always lands before the 15:15 MIS square-off cutoff.
_NO_ENTRY_AFTER_DEFAULT = "09:29"
_EXIT_TIME_DEFAULT = "09:30"
_EXIT_LATEST_MIN = 15 * 60 + 10  # 15:10


def parse_hhmm(value) -> int | None:
    """``"HH:MM"`` -> minutes since midnight, or None on malformed input."""
    try:
        hh, mm = str(value).strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, AttributeError):
        pass
    return None


def validate_window(no_entry_after: str, exit_time: str) -> list[str]:
    """Validation errors for a proposed entry/exit window (empty list = valid)."""
    errors: list[str] = []
    nea_min, exit_min = parse_hhmm(no_entry_after), parse_hhmm(exit_time)
    if nea_min is None:
        errors.append("no_entry_after must be HH:MM (24h IST)")
    if exit_min is None:
        errors.append("exit_time must be HH:MM (24h IST)")
    if nea_min is None or exit_min is None:
        return errors
    if nea_min < _ENTRY_FROM:
        errors.append("no_entry_after must be 09:16 or later")
    if exit_min > _EXIT_LATEST_MIN:
        errors.append("exit_time must be 15:10 or earlier (15:15 is the MIS square-off cutoff)")
    if nea_min >= exit_min:
        errors.append("exit_time must be after no_entry_after")
    return errors


def _enabled() -> bool:
    return os.getenv("OPEN15_ENABLED", "true").lower() == "true"


def _mode() -> str:
    """Effective mode: env ``observe`` (dry-run kill switch) wins; otherwise the
    persistent ``strategy_mode`` row (what the strategies-page toggle writes)
    governs, defaulting to ``sandbox``. Note a ``live`` row still routes through
    ``place_order``'s platform-global gate, so it cannot silently go live while
    the global gate is sandbox (same contract as intraday_pullback SPEC §9)."""
    env = os.getenv("OPEN15_MODE", "").lower()
    if env == "observe":
        return "observe"
    try:
        from database.strategy_mode_db import StrategyMode, db_session

        row = db_session.query(StrategyMode).filter_by(strategy_name=STRATEGY_NAME).first()
        if row and row.mode in ("sandbox", "live"):
            return row.mode
    except Exception:
        logger.exception("open15: strategy_mode read failed — falling back to env/sandbox")
    finally:
        try:
            from database.strategy_mode_db import db_session

            db_session.remove()
        except Exception:
            pass
    return env if env in ("sandbox",) else "sandbox"


def _vol_mult() -> float:
    try:
        return float(os.getenv("OPEN15_VOL_MULT", "1.5"))
    except ValueError:
        return 1.5


def _top_n() -> int:
    try:
        return int(os.getenv("OPEN15_TOP_N", "3"))
    except ValueError:
        return 3


def _tick_capture_enabled() -> bool:
    """Persist ticks for the day's SELECTED symbols (backtest replay data)."""
    return os.getenv("OPEN15_TICK_CAPTURE", "true").lower() == "true"


def _opt_shadow_enabled() -> bool:
    """ATM option shadow pricing on journal rows (issue #435, research-only)."""
    return os.getenv("OPEN15_OPT_SHADOW_ENABLED", "true").lower() == "true"


def _instrument_default() -> str:
    """What the entry BUYS: the stock itself, or its ATM option (issue #437)."""
    v = os.getenv("OPEN15_INSTRUMENT", "stock").lower()
    return v if v in ("stock", "atm_option") else "stock"


TRADE_SIDES = ("both", "long_only", "short_only")


def _trade_side_default() -> str:
    """Which sides the day may select: both / long_only / short_only (issue #503)."""
    v = os.getenv("OPEN15_TRADE_SIDE", "both").lower()
    return v if v in TRADE_SIDES else "both"


def _max_trades_default() -> int:
    """Daily entry cap across both sides (issue #437). Clamped 1..6."""
    try:
        v = int(os.getenv("OPEN15_MAX_TRADES", "3"))
    except ValueError:
        v = 3
    return max(1, min(v, 6))


def _no_entry_after_default() -> str:
    """Env default for the entry cutoff (issue #451)."""
    v = os.getenv("OPEN15_NO_ENTRY_AFTER", _NO_ENTRY_AFTER_DEFAULT)
    return v if parse_hhmm(v) is not None else _NO_ENTRY_AFTER_DEFAULT


def _exit_time_default() -> str:
    """Env default for the hard-flatten time (issue #451)."""
    v = os.getenv("OPEN15_EXIT_TIME", _EXIT_TIME_DEFAULT)
    return v if parse_hhmm(v) is not None else _EXIT_TIME_DEFAULT


def _notional() -> float:
    """Per-trade notional = margin/slot x leverage (defaults 30k x 5 = 150k)."""
    try:
        margin = float(os.getenv("OPEN15_MARGIN_PER_SLOT", "30000"))
        lev = float(os.getenv("OPEN15_LEVERAGE", "5"))
        return margin * lev
    except ValueError:
        return 150_000.0


def mis_round_trip_charges(buy_value: float, sell_value: float) -> float | None:
    """Modelled Zerodha MIS equity round-trip charges in Rs (issue #433).

    Brokerage min(Rs20, 0.03%) per executed leg, STT 0.025% of the sell leg,
    NSE txn 0.00297% of total turnover, SEBI Rs10/crore, stamp 0.003% of the
    buy leg, 18% GST on brokerage + exchange txn + SEBI. Pure; direction is
    encoded by which leg value is the buy vs the sell.
    """
    if not buy_value or not sell_value:
        return None
    turnover = buy_value + sell_value
    brokerage = min(20.0, 0.0003 * buy_value) + min(20.0, 0.0003 * sell_value)
    stt = 0.00025 * sell_value
    exch_txn = 0.0000297 * turnover
    sebi = 0.000001 * turnover
    stamp = 0.00003 * buy_value
    gst = 0.18 * (brokerage + exch_txn + sebi)
    return round(brokerage + stt + exch_txn + sebi + stamp + gst, 2)


def _first_candle_source() -> str:
    """``OPEN15_FIRST_CANDLE_SOURCE`` — ``quotes`` (default, issue #502) sources
    the 09:15 open/high/low from ONE batched broker quote snapshot at 09:16;
    ``ticks`` restores the pre-#502 tick-built candle."""
    v = os.getenv("OPEN15_FIRST_CANDLE_SOURCE", "quotes").strip().lower()
    return v if v in ("quotes", "ticks") else "quotes"


def _baseline_includes_first_minute() -> bool:
    """``OPEN15_BASELINE_INCLUDE_FIRST_MINUTE`` (default false, issue #502) —
    the rollback switch for keeping the 09:15 minute in the volume baseline."""
    return os.getenv("OPEN15_BASELINE_INCLUDE_FIRST_MINUTE", "false").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def fetch_first_candles(universe: set[str]) -> dict[str, dict[str, float]]:
    """ONE batched broker quote call at 09:16 -> ``{symbol: {open, high, low}}``.

    The quote payload's ``open`` is the exchange's official day open (the
    pre-open auction print), and at 09:16:00 the running day ``high``/``low``
    ARE the 09:15 candle's extremes. Both are exact where the tick-sampled
    candle is a ~1/sec approximation that starts whenever the first tick
    happens to arrive (issue #502 bugs 1 and 2).

    NEVER raises. Any failure logs a WARNING and returns ``{}``; the caller
    falls back to the tick-built candle per symbol.
    """
    if _first_candle_source() != "quotes":
        return {}
    try:
        from database.auth_db import get_first_available_api_key
        from services.quotes_service import get_multiquotes

        api_key = get_first_available_api_key()
        if not api_key:
            logger.warning(
                "open15 first-candle snapshot: no API key (broker session down?) — "
                "falling back to the tick-built candle"
            )
            return {}
        payload = [{"symbol": s, "exchange": "NSE"} for s in sorted(universe)]
        success, resp, _status = get_multiquotes(payload, api_key=api_key)
        if not success:
            logger.warning(
                "open15 first-candle snapshot: get_multiquotes failed: %s — "
                "falling back to the tick-built candle",
                (resp or {}).get("message", "unknown error"),
            )
            return {}
        out: dict[str, dict[str, float]] = {}
        for item in (resp or {}).get("results") or []:
            sym = item.get("symbol")
            data = item.get("data")
            if not sym or not isinstance(data, dict):
                continue
            try:
                o, h, low = (
                    float(data.get("open") or 0),
                    float(data.get("high") or 0),
                    float(data.get("low") or 0),
                )
            except (TypeError, ValueError):
                continue
            if o > 0 and h >= low > 0:
                out[sym] = {"open": o, "high": h, "low": low}
        logger.info(
            "open15 first-candle snapshot: %d/%d symbols from the 09:16 quote call",
            len(out),
            len(universe),
        )
        return out
    except Exception:
        logger.exception(
            "open15 first-candle snapshot failed — falling back to the tick-built candle"
        )
        return {}


def _prevclose_check_enabled() -> bool:
    """``OPEN15_PREVCLOSE_REGISTRY_CHECK_ENABLED`` (default true, issue #456)."""
    return os.getenv("OPEN15_PREVCLOSE_REGISTRY_CHECK_ENABLED", "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def _prevclose_divergence_max_pct() -> float:
    """``OPEN15_PREVCLOSE_DIVERGENCE_MAX_PCT`` (default 0.05, issue #456).

    Historify-D and the broker registry read the same broker daily API, so a
    genuine settled close matches to rounding; anything beyond a few bps means
    the historify slot is provisional/stale and the broker value must win.
    """
    try:
        return float(os.getenv("OPEN15_PREVCLOSE_DIVERGENCE_MAX_PCT", "0.05"))
    except ValueError:
        return 0.05


def _prevclose_quotes_enabled() -> bool:
    """``OPEN15_PREVCLOSE_QUOTES_ENABLED`` (default true, issue #456)."""
    return os.getenv("OPEN15_PREVCLOSE_QUOTES_ENABLED", "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def fetch_broker_prev_closes(universe: set[str]) -> dict[str, float]:
    """ONE batched broker quote call -> ``{symbol: prev_close}`` (issue #456).

    The broker quote payload carries the previous day's SETTLED close
    (``prev_close`` = Zerodha ``ohlc.close``), so at the 09:10 arm this is the
    authoritative selection reference for every symbol — fetched at the moment
    of use, independent of historify state and resettle timing. Every value is
    also recorded into the #305 broker prev-close registry, so the scanner's
    reference certificate benefits for the rest of the day.

    NEVER raises. Any failure (no API key / broker session down, broker error,
    malformed response) logs a WARNING and returns ``{}`` — the caller falls
    back to the registry-verified historify chain (#457 commit 1).
    """
    if not _prevclose_quotes_enabled():
        return {}
    try:
        from database.auth_db import get_first_available_api_key
        from services.quotes_service import get_multiquotes
        from services.scanner_reference_data import record_broker_prev_close

        api_key = get_first_available_api_key()
        if not api_key:
            logger.warning(
                "open15 prev-close quotes: no API key (broker session down?) — "
                "falling back to registry-verified historify"
            )
            return {}
        payload = [{"symbol": s, "exchange": "NSE"} for s in sorted(universe)]
        success, resp, _status = get_multiquotes(payload, api_key=api_key)
        if not success:
            logger.warning(
                "open15 prev-close quotes: get_multiquotes failed: %s — falling "
                "back to registry-verified historify",
                (resp or {}).get("message", "unknown error"),
            )
            return {}
        out: dict[str, float] = {}
        for item in (resp or {}).get("results") or []:
            sym = item.get("symbol")
            data = item.get("data")
            if not sym or not isinstance(data, dict):
                continue
            try:
                pc = float(data.get("prev_close") or 0)
            except (TypeError, ValueError):
                continue
            if pc > 0:
                out[sym] = pc
                record_broker_prev_close(sym, pc)
        logger.info(
            "open15 prev-close quotes: %d/%d symbols from the live quote snapshot",
            len(out),
            len(universe),
        )
        return out
    except Exception:
        logger.exception(
            "open15 prev-close quotes: snapshot failed — falling back to "
            "registry-verified historify"
        )
        return {}


def verify_prev_closes(closes: dict[str, float], today: dt.date) -> tuple[dict[str, float], dict]:
    """Cross-check historify prev-closes against the broker prev-close registry
    (issue #456 — the 2026-07-23 arm-vs-daily-D-resettle race).

    The 09:10 arm can read historify-D while the #299 resettle is still
    overwriting provisional closes, silently shifting the gap ranking. The
    scanner's broker prev-close registry (issue #305, day-scoped, populated by
    the boot seeder + resettle from broker bars) is an independent copy of the
    T-1 SETTLED close — so on a confirmed divergence the broker value wins.

    Pure and fail-open per symbol: no registry entry today -> historify value
    kept (counted in provenance); any internal error -> input returned
    unchanged. Returns ``(verified_closes, provenance)`` where provenance is
    logged verbatim into the ``armed`` decision-log event so every day's
    prev-close trust status is auditable without tick forensics.
    """
    if not _prevclose_check_enabled():
        return closes, {"enabled": False}
    try:
        from services.scanner_reference_data import get_broker_prev_close

        max_pct = _prevclose_divergence_max_pct()
        out = dict(closes)
        overrides: list[dict] = []
        checked = 0
        missing = 0
        for sym, hist_close in closes.items():
            entry = get_broker_prev_close(sym, today=today)
            if entry is None:
                missing += 1
                continue
            checked += 1
            broker_close = float(entry[0])
            div_pct = abs(hist_close - broker_close) / max(abs(broker_close), 1e-9) * 100.0
            if div_pct > max_pct:
                out[sym] = broker_close
                overrides.append(
                    {
                        "symbol": sym,
                        "historify": round(hist_close, 4),
                        "broker": round(broker_close, 4),
                        "divergence_pct": round(div_pct, 3),
                    }
                )
        if overrides:
            logger.warning(
                "open15: prev-close registry OVERRODE %d/%d symbols (historify stale/"
                "provisional — the #456 race class): %s",
                len(overrides),
                checked,
                ", ".join(
                    f"{o['symbol']} {o['historify']}->{o['broker']} ({o['divergence_pct']}%)"
                    for o in overrides[:10]
                ),
            )
        provenance = {
            "enabled": True,
            "max_divergence_pct": max_pct,
            "checked": checked,
            "no_registry_entry": missing,
            "overridden": len(overrides),
            # cap the embedded detail so one bad morning can't bloat the day log
            "overrides": overrides[:20],
        }
        return out, provenance
    except Exception:
        logger.exception("open15: prev-close verification failed — using historify values as-is")
        return closes, {"enabled": True, "error": True}


def resolve_day_config(cfg_row: dict | None, cum_realized_pnl: float) -> dict:
    """Merge the UI config row over env defaults into today's effective config.

    Pure (unit-testable). Sizing modes:
      - ``fixed``    — margin/slot is the configured base every day.
      - ``compound`` — margin/slot = base + cumulative realized P&L (research
        P&L from ``open15_trades``), floored at 25% of base so a drawdown can
        shrink but never zero the strategy.
    """
    cfg = cfg_row or {}
    base_margin = float(cfg.get("margin_per_slot") or os.getenv("OPEN15_MARGIN_PER_SLOT", "30000"))
    vol_mult = float(cfg.get("vol_mult") or os.getenv("OPEN15_VOL_MULT", "1.5"))
    sizing_mode = (cfg.get("sizing_mode") or os.getenv("OPEN15_SIZING_MODE", "fixed")).lower()
    if sizing_mode not in ("fixed", "compound"):
        sizing_mode = "fixed"
    lev = float(os.getenv("OPEN15_LEVERAGE", "5"))
    margin_eff = base_margin
    if sizing_mode == "compound":
        margin_eff = max(base_margin + cum_realized_pnl, 0.25 * base_margin)
    instrument = (cfg.get("instrument") or _instrument_default()).lower()
    if instrument not in ("stock", "atm_option"):
        instrument = "stock"
    trade_side = (cfg.get("trade_side") or _trade_side_default()).lower()
    if trade_side not in TRADE_SIDES:
        trade_side = "both"
    try:
        max_trades = int(cfg.get("max_trades") or _max_trades_default())
    except (TypeError, ValueError):
        max_trades = _max_trades_default()
    max_trades = max(1, min(max_trades, 6))
    no_entry_after = cfg.get("no_entry_after") or _no_entry_after_default()
    exit_time = cfg.get("exit_time") or _exit_time_default()
    if validate_window(no_entry_after, exit_time):
        logger.warning(
            "open15: invalid entry/exit window %r/%r — using defaults %s/%s",
            no_entry_after,
            exit_time,
            _NO_ENTRY_AFTER_DEFAULT,
            _EXIT_TIME_DEFAULT,
        )
        no_entry_after, exit_time = _NO_ENTRY_AFTER_DEFAULT, _EXIT_TIME_DEFAULT
    return {
        "margin_per_slot": base_margin,
        "margin_effective": round(margin_eff, 2),
        "sizing_mode": sizing_mode,
        "vol_mult": vol_mult,
        "leverage": lev,
        "notional": round(margin_eff * lev, 2),
        "cum_realized_pnl": round(cum_realized_pnl, 2),
        "instrument": instrument,
        "max_trades": max_trades,
        "no_entry_after": no_entry_after,
        "exit_time": exit_time,
        "trade_side": trade_side,
    }


# --------------------------------------------------------------------------- #
# Pure tick-driven core (no I/O — unit-testable)
# --------------------------------------------------------------------------- #
class Open15Core:
    """Per-day state machine. Feed IST-time ticks; get entry actions back.

    Legality invariant (the whole point): every quantity an entry decision
    reads — prev close, first-candle OHLC, completed-minute volumes, and the
    CURRENT minute's volume accumulated SO FAR — is known at the tick that
    triggers the entry. Nothing from later in the minute is consulted. The
    09:16 broker first-candle snapshot (issue #502) keeps this invariant: the
    09:15 candle is fully settled history by the time it is read.
    """

    def __init__(
        self,
        prev_closes: dict[str, float],
        vol_mult: float = 1.5,
        top_n: int = 3,
        entry_to_min: int = _ENTRY_TO,
        track_to_min: int = _EXIT_MIN,
        baseline_includes_first_minute: bool = False,
        await_snapshot: bool = False,
        trade_side: str = "both",
    ):
        self.prev_closes = prev_closes
        self.vol_mult = vol_mult
        self.top_n = top_n
        # which sides may be selected at all (issue #503) — an excluded side is
        # never watched, so it produces no ticks, no entries and no journal rows
        self.trade_side = trade_side if trade_side in TRADE_SIDES else "both"
        # entries allowed through entry_to_min; price/minute tracking continues
        # through track_to_min (the exit minute) so the flatten's research
        # exit_price stays fresh on an extended window (issue #451)
        self.entry_to_min = entry_to_min
        self.track_to_min = max(track_to_min, entry_to_min)
        # issue #502 bug 3: the 09:15 minute is the day's busiest AND its
        # tick cumulative volume carries the pre-open auction, so leaving it in
        # the running mean inflates the baseline 1.06x-1.67x and turns the
        # configured 1.5x gate into ~2.5x. Excluding it also drops the auction,
        # because every later minute is a cumulative DIFFERENCE.
        self.baseline_includes_first_minute = baseline_includes_first_minute
        # issue #502 bugs 1+2: broker-authoritative first candles, applied at
        # 09:16 via ``apply_first_candles``. Until they land (or the deadline
        # passes) selection is deferred — finalizing on the tick-built candle
        # is exactly the bug.
        self.first_candles: dict[str, dict[str, float]] = {}
        self.first_candle_source = "ticks"
        self._snapshot_applied = not await_snapshot
        self.sym: dict[str, dict[str, Any]] = {}
        self.selected: dict[str, str] = {}  # symbol -> "L"/"S"
        self.gaps: dict[str, float] = {}
        self.finalized = False
        self.entered: dict[str, dict[str, Any]] = {}
        self.last_price: dict[str, float] = {}
        # per-selected-symbol near-miss stats for the decision log: how close a
        # non-entered watch got (max cumvol/baseline ratio, ever beyond level)
        self.watch_stats: dict[str, dict[str, Any]] = {}

    def apply_first_candles(self, candles: dict[str, dict[str, float]]) -> None:
        """Install the broker's 09:15 open/high/low and release the selection.

        Always releases the deferral, even on an empty/partial dict — a failed
        snapshot must fall back to the tick-built candle, never stall the day.
        """
        clean: dict[str, dict[str, float]] = {}
        for sym, c in (candles or {}).items():
            try:
                o, h, low = float(c["open"]), float(c["high"]), float(c["low"])
            except (KeyError, TypeError, ValueError):
                continue
            if o > 0 and h >= low > 0:
                clean[sym] = {"open": o, "high": h, "low": low}
        self.first_candles = clean
        if clean:
            self.first_candle_source = "quotes"
        self._snapshot_applied = True

    def first_candle(self, symbol: str) -> dict[str, float] | None:
        """Broker candle when the snapshot carried this symbol, else the
        tick-built fallback (fail-open per symbol)."""
        fc = self.first_candles.get(symbol)
        if fc is not None:
            return fc
        st = self.sym.get(symbol)
        return st["fc"] if st else None

    def _st(self, symbol: str) -> dict[str, Any]:
        st = self.sym.get(symbol)
        if st is None:
            st = {
                "fc": None,  # first candle {"open","high","low"}
                "minute_vols": [],  # completed minute volumes since 09:15
                "cur_min": _FIRST_MIN,
                "cum_prev_end": 0.0,  # cumvol at end of last completed minute
                "last_cum": 0.0,
                "min_last_price": {},  # minute -> last ltp seen in it
            }
            self.sym[symbol] = st
        return st

    def _roll_minutes(self, st: dict, minute: int) -> None:
        """Close out completed minutes up to (excluding) ``minute``.

        The 09:15 minute is skipped for the baseline unless
        ``baseline_includes_first_minute`` (issue #502) — ``cum_prev_end``
        still advances, so the within-minute accumulation is unaffected.
        """
        while st["cur_min"] < minute:
            if st["cur_min"] != _FIRST_MIN or self.baseline_includes_first_minute:
                st["minute_vols"].append(max(st["last_cum"] - st["cum_prev_end"], 0.0))
            st["cum_prev_end"] = st["last_cum"]
            st["cur_min"] += 1

    def _finalize_selection(self) -> None:
        self.finalized = True
        # union: a symbol the broker snapshot covers is rankable even if its
        # own ticks were slow to arrive (the MPHASIS class, issue #502)
        for s in {*self.sym, *self.first_candles}:
            fc = self.first_candle(s)
            pc = self.prev_closes.get(s)
            if fc and pc:
                self.gaps[s] = fc["open"] / pc - 1.0
        pos = sorted((s for s in self.gaps if self.gaps[s] > 0), key=lambda s: -self.gaps[s])
        neg = sorted((s for s in self.gaps if self.gaps[s] < 0), key=lambda s: self.gaps[s])
        if self.trade_side != "short_only":
            for s in pos[: self.top_n]:
                self.selected[s] = "L"
        if self.trade_side != "long_only":
            for s in neg[: self.top_n]:
                self.selected[s] = "S"
        # NB: never pass a bare dict as the sole logging arg — logging's
        # single-mapping special case turns it into `msg % dict` and raises.
        sel = ", ".join(f"{s}:{d}{self.gaps[s] * 100:+.2f}%" for s, d in self.selected.items())
        logger.info("open15: selection finalized — %s", sel)
        # Seed the near-miss stats for every selected symbol (issue #524). Two
        # jobs: (1) the key set is fixed here, at 09:16, so ``watch_snapshot``
        # can copy it from a Flask request thread while ticks mutate values on
        # the ZMQ thread; (2) ``None`` keeps "no ticks arrived" distinguishable
        # from a genuine 0.0 ratio in the UI.
        for s in self.selected:
            self.watch_stats.setdefault(
                s, {"max_vol_ratio": None, "max_vol_ratio_beyond": None, "level_broken": False}
            )

    def on_tick(self, symbol: str, price: float, cumvol: float, ts: dt.datetime) -> dict | None:
        """Process one tick (ts must be IST-naive or IST-aware). Returns an
        entry-action dict the first time a selected symbol triggers, else None."""
        minute = ts.hour * 60 + ts.minute
        if minute < _FIRST_MIN or minute > self.track_to_min:
            return None
        self.last_price[symbol] = price
        st = self._st(symbol)

        # Defer selection until the broker first-candle snapshot lands (issue
        # #502). The deadline is a hard fail-open: past 09:16 we finalize on
        # whatever we have rather than lose the day to a slow quote call.
        if not self.finalized and minute >= _ENTRY_FROM:
            if self._snapshot_applied or minute > _ENTRY_FROM:
                self._finalize_selection()

        if minute == _FIRST_MIN:
            fc = st["fc"]
            if fc is None:
                st["fc"] = {"open": price, "high": price, "low": price}
            else:
                fc["high"] = max(fc["high"], price)
                fc["low"] = min(fc["low"], price)
            st["last_cum"] = cumvol
            st["min_last_price"][minute] = price
            return None

        # entry window
        self._roll_minutes(st, minute)
        st["last_cum"] = cumvol
        st["min_last_price"][minute] = price

        if minute > self.entry_to_min:
            return None  # tracking only past the cutoff — no new entries

        side = self.selected.get(symbol)
        fc = self.first_candle(symbol)
        if side is None or symbol in self.entered or fc is None:
            return None
        vols = st["minute_vols"]
        if not vols:
            return None
        baseline = mean(vols)
        if baseline <= 0:
            return None
        cum_in_min = max(cumvol - st["cum_prev_end"], 0.0)
        level = fc["high"] if side == "L" else fc["low"]
        beyond = price > level if side == "L" else price < level
        ws = self.watch_stats.setdefault(
            symbol, {"max_vol_ratio": None, "max_vol_ratio_beyond": None, "level_broken": False}
        )
        ratio = cum_in_min / baseline
        ws["max_vol_ratio"] = max(ws["max_vol_ratio"] or 0.0, ratio)
        if beyond:
            ws["level_broken"] = True
            ws["max_vol_ratio_beyond"] = max(ws["max_vol_ratio_beyond"] or 0.0, ratio)
        if beyond and cum_in_min >= self.vol_mult * baseline:
            action = {
                "symbol": symbol,
                "side": side,
                "price": price,
                "level": level,
                "gap_pct": self.gaps.get(symbol, 0.0) * 100,
                "baseline_vol": baseline,
                "cum_vol_at_trigger": cum_in_min,
                "trigger_minute": f"{ts.hour:02d}:{ts.minute:02d}",
                "trigger_second": ts.second,
                "trigger_min_idx": minute,
            }
            self.entered[symbol] = action
            return action
        return None

    def watch_snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-selected-symbol near-miss stats, safe to read off-thread (#524).

        Keys are seeded at ``_finalize_selection`` and never change afterwards,
        so the ``list(...)`` copy cannot race the tick thread's value updates.
        ``max_vol_ratio`` is ``None`` until the symbol's first in-window tick —
        blank means "no data", NOT a 0.0 ratio.

        Note the value FREEZES at entry for a symbol that triggered (``on_tick``
        returns early once ``symbol in self.entered``), so an entered symbol's
        max is its ratio at trigger. It is also final at the entry cutoff, since
        the ``minute > entry_to_min`` return sits above the update.
        """
        return {
            sym: {
                "max_vol_ratio": None
                if ws.get("max_vol_ratio") is None
                else round(ws["max_vol_ratio"], 2),
                "max_vol_ratio_beyond": None
                if ws.get("max_vol_ratio_beyond") is None
                else round(ws["max_vol_ratio_beyond"], 2),
                "level_broken": bool(ws.get("level_broken", False)),
                "entered": sym in self.entered,
            }
            for sym, ws in list(self.watch_stats.items())
        }

    def entry_minute_close(self, symbol: str) -> float | None:
        """Last price seen inside the symbol's entry minute (call after exit)."""
        rec = self.entered.get(symbol)
        if not rec:
            return None
        return self.sym[symbol]["min_last_price"].get(rec["trigger_min_idx"])


# --------------------------------------------------------------------------- #
# Runtime service (ZMQ subscriber + scheduler jobs + orders + journal)
# --------------------------------------------------------------------------- #
def production_order_placer(mode: str, order: dict) -> dict:
    """Place a MARKET order via the shared path. Sandbox routing is downstream."""
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
            "exchange": order.get("exchange", "NSE"),
            "action": order["action"],
            "product": "MIS",
            "pricetype": "MARKET",
            "quantity": str(order["quantity"]),
        }
        success, response, _ = place_order(payload, api_key=api_key, mode_key=STRATEGY_NAME)
        response = dict(response or {})
        response.setdefault("status", "success" if success else "error")
        return response
    except Exception as e:  # noqa: BLE001
        logger.exception("open15: order placement failed")
        return {"status": "error", "message": str(e)}


def _as_int(value: Any) -> int | None:
    """Coerce a broker quote field to int; None when absent or unparseable.

    Deliberately does NOT default to 0 — a field the broker omitted must read as
    "unknown", not as "zero liquidity", or the research rows become misleading.
    """
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def production_quote_snapshot(symbol: str, exchange: str) -> dict | None:
    """Full quote for the option paths: last price plus the contract's own
    traded volume and open interest (issue #488).

    ``volume``/``oi`` are recorded for research only — nothing gates on them.
    Returns None when the quote is unavailable; callers treat that as "unknown"
    and fall back to the price-only path.
    """
    try:
        from database.auth_db import get_first_available_api_key
        from services.quotes_service import get_quotes

        api_key = get_first_available_api_key()
        if not api_key:
            return None
        ok, data, _code = get_quotes(symbol, exchange, api_key=api_key)
        if not ok:
            logger.warning("open15: quote failed for %s:%s — %s", exchange, symbol, data)
            return None
        d = data.get("data") or {}
        ltp = d.get("ltp")
        if not ltp:
            return None
        return {
            "ltp": float(ltp),
            "volume": _as_int(d.get("volume")),
            "oi": _as_int(d.get("oi")),
        }
    except Exception:
        logger.exception("open15: quote raised for %s:%s", exchange, symbol)
        return None


def production_quote_ltp(symbol: str, exchange: str) -> float | None:
    """Last traded price via the shared quotes path (option-mode sizing/fills)."""
    snap = production_quote_snapshot(symbol, exchange)
    return snap["ltp"] if snap else None


class Open15BreakoutService:
    def __init__(
        self,
        order_placer=production_order_placer,
        quote_fn=production_quote_ltp,
        quote_snapshot_fn=production_quote_snapshot,
    ):
        self.order_placer = order_placer
        self.quote_fn = quote_fn
        self.quote_snapshot_fn = quote_snapshot_fn
        self.core: Open15Core | None = None
        self._sched = None  # scheduler handle from register_jobs (arm reschedules on it)
        self.positions: dict[str, dict[str, Any]] = {}  # symbol -> journal/fill info
        self.day_status = "idle"  # idle / armed / skipped_late_boot / done
        self.universe: set[str] = set()
        self._lock = threading.Lock()
        self._zmq_thread: threading.Thread | None = None
        self._stop = threading.Event()
        # decision log: ordered events for the UI (persisted on every event,
        # so a mid-window crash never loses the day — issue #444)
        self.day_log: list[dict[str, Any]] = []
        self._log_date: str | None = None
        self.day_config: dict[str, Any] = resolve_day_config(None, 0.0)
        # targeted tick capture: only the day's SELECTED symbols are persisted,
        # plus their 09:15 first-minute ticks (buffered until selection).
        self._tick_writer = None
        self._first_min_buffer: dict[str, list[tuple]] = {}
        self._capture_flushed = False

    def _log_event(self, event: str, **detail: Any) -> None:
        rec = {
            "ts": dt.datetime.now(IST).strftime("%H:%M:%S.%f")[:-3],
            "event": event,
            **detail,
        }
        self.day_log.append(rec)
        if len(self.day_log) > 500:
            del self.day_log[: len(self.day_log) - 500]
        logger.info("open15 [%s] %s", event, detail if detail else "")
        # events are rare (<~20/day) so the per-event upsert is cheap; it means
        # a crash mid-window loses nothing (issue #444)
        self._persist_day_log()

    # ---- universe / data ------------------------------------------------- #
    @staticmethod
    def _load_universe() -> set[str]:
        from services.scanner_presubscribe import resolve_exchange_for_symbol

        raw = os.getenv("SCANNER_SYMBOLS", "")
        syms = {s.strip() for s in raw.split(",") if s.strip()}
        return {s for s in syms if resolve_exchange_for_symbol(s) == "NSE"}

    @staticmethod
    def _load_prev_closes(universe: set[str], today: dt.date) -> dict[str, float]:
        """Last settled daily close per symbol from historify (read-only)."""
        from services.data_freshness_service import connect_historify_readonly

        path = os.getenv("HISTORIFY_DATABASE_PATH", "db/historify.duckdb")
        cutoff = (today - dt.date(1970, 1, 1)).days * 86400 - 19800
        out: dict[str, float] = {}
        try:
            con = connect_historify_readonly(path)
            rows = con.execute(
                "SELECT symbol, arg_max(close, timestamp) FROM market_data "
                "WHERE interval='D' AND timestamp < ? AND symbol IN ("
                + ",".join("?" * len(universe))
                + ") GROUP BY symbol",
                [cutoff, *sorted(universe)],
            ).fetchall()
            out = {r[0]: float(r[1]) for r in rows if r[1]}
        except Exception:
            logger.exception("open15: prev-close load failed — day will be skipped")
        return out

    # ---- scheduler jobs -------------------------------------------------- #
    def arm(self) -> None:
        """09:10 IST — build today's core; refuse to run on a late boot."""
        if not _enabled():
            self.day_status = "idle"
            return
        now = dt.datetime.now(IST)
        self.day_log = []
        self._log_date = now.strftime("%Y-%m-%d")
        self._first_min_buffer = {}
        self._capture_flushed = False
        if now.time() >= dt.time(9, 15, 30):
            self.day_status = "skipped_late_boot"
            self._log_event(
                "skipped_late_boot",
                armed_at=now.strftime("%H:%M:%S"),
                fix="boot OpenAlgo before 09:15 IST",
            )
            logger.warning(
                "open15: armed at %s — after 09:15 IST, first candle unobservable; "
                "day SKIPPED. Boot OpenAlgo before 09:15 for this strategy.",
                now.strftime("%H:%M:%S"),
            )
            self._persist_day_log()
            return
        self.universe = self._load_universe()
        prev = self._load_prev_closes(self.universe, now.date())
        # issue #456: PRIMARY = one batched broker quote call at the moment of
        # use (prev_close = the settled T-1 close, immune to the historify/
        # resettle race). Quote values win the merge and are recorded into the
        # #305 registry; whatever the call missed stays on historify and is
        # cross-checked against the registry below (broker wins on divergence).
        quote_closes = fetch_broker_prev_closes(self.universe)
        prev = {**prev, **quote_closes}
        if len(prev) < 20:
            self.day_status = "skipped_late_boot"
            self._log_event("skipped_no_prev_closes", available=len(prev))
            self._persist_day_log()
            return
        prev, prev_check = verify_prev_closes(prev, now.date())
        prev_check["from_live_quotes"] = len(quote_closes)
        if _tick_capture_enabled() and self._tick_writer is None:
            try:
                from services.simplified_stock_engine_ticklog import TickLogWriter

                self._tick_writer = TickLogWriter(
                    enabled=True, directory="tick_logs/open15", retention_days=365
                )
            except Exception:
                logger.exception("open15: tick writer init failed — capture disabled today")
        try:
            from database.open15_breakout_db import get_config, total_realized_pnl

            cfg_row = get_config()
            cum_pnl = (
                total_realized_pnl()
                if (cfg_row or {}).get("sizing_mode") == "compound"
                or (os.getenv("OPEN15_SIZING_MODE", "fixed") == "compound")
                else 0.0
            )
        except Exception:
            logger.exception("open15: config load failed — using env defaults")
            cfg_row, cum_pnl = None, 0.0
        self.day_config = resolve_day_config(cfg_row, cum_pnl)
        nea_min, exit_min = self._window_minutes()
        self._apply_exit_schedule()
        with self._lock:
            self.core = Open15Core(
                prev,
                vol_mult=self.day_config["vol_mult"],
                top_n=_top_n(),
                entry_to_min=nea_min,
                track_to_min=exit_min,
                baseline_includes_first_minute=_baseline_includes_first_minute(),
                await_snapshot=_first_candle_source() == "quotes",
                trade_side=self.day_config["trade_side"],
            )
            self.positions = {}
            self.day_status = "armed"
        self._log_event(
            "armed",
            universe=len(self.universe),
            prev_closes=len(prev),
            vol_mult=self.day_config["vol_mult"],
            top_n=_top_n(),
            mode=_mode(),
            no_entry_after=self.day_config["no_entry_after"],
            exit_time=self.day_config["exit_time"],
            trade_side=self.day_config["trade_side"],
            sizing_mode=self.day_config["sizing_mode"],
            margin_per_slot=self.day_config["margin_per_slot"],
            margin_effective=self.day_config["margin_effective"],
            notional=self.day_config["notional"],
            cum_realized_pnl=self.day_config["cum_realized_pnl"],
            config_source="ui" if cfg_row else "env_defaults",
            tick_capture=bool(self._tick_writer),
            prev_close_check=prev_check,
            first_candle_source=_first_candle_source(),
            baseline_includes_first_minute=_baseline_includes_first_minute(),
        )
        self._ensure_zmq_thread()
        if _opt_shadow_enabled():
            # catch-up pricing for rows the 09:35 broker-lag left unpriced
            # (and the one-off #435 backfill) — daemon thread, never blocks arm
            threading.Thread(
                target=self._opt_shadow_catchup, name="open15-opt-shadow", daemon=True
            ).start()
        logger.info(
            "open15: ARMED for %s — universe %d, prev-closes %d, vol_mult %.2f, top_n %d, mode %s",
            now.date(),
            len(self.universe),
            len(prev),
            _vol_mult(),
            _top_n(),
            _mode(),
        )

    def capture_first_candles(self) -> None:
        """09:16:00 IST — install the broker's 09:15 candle into today's core.

        This is the fix for issue #502 bugs 1 and 2: selection and the breakout
        level stop depending on when the first tick happened to arrive. The
        core defers finalizing until this lands (hard deadline 09:17), and a
        failed/partial snapshot falls back to the tick-built candle per symbol,
        so the day never stalls on it.
        """
        core = self.core
        if core is None or self.day_status != "armed":
            return
        if _first_candle_source() != "quotes":
            core.apply_first_candles({})
            return
        candles = fetch_first_candles(self.universe)
        core.apply_first_candles(candles)
        self._log_event(
            "first_candles",
            source=core.first_candle_source,
            covered=len(candles),
            universe=len(self.universe),
        )

    def _opt_shadow_catchup(self) -> None:
        """Daemon-thread wrapper for the arm-time option-shadow catch-up."""
        try:
            from services.open15_option_shadow import enrich_missing

            res = enrich_missing()
            if res.get("priced"):
                logger.info("open15 opt-shadow catch-up: %s", res)
        except Exception:
            logger.exception("open15: option-shadow catch-up failed")

    def _window_minutes(self) -> tuple[int, int]:
        """Today's effective (entry-cutoff, exit) as minutes since midnight."""
        cfg = self.day_config or {}
        nea = parse_hhmm(cfg.get("no_entry_after"))
        ex = parse_hhmm(cfg.get("exit_time"))
        return (nea if nea is not None else _ENTRY_TO, ex if ex is not None else _EXIT_MIN)

    def _apply_exit_schedule(self) -> None:
        """(Re)point the exit/retry/summary jobs at the effective exit time.

        Called from ``arm`` so a window saved after boot applies at the next
        arm (same contract as every other config field). No-op when
        ``register_jobs`` hasn't run (unit tests drive ``arm`` directly).
        """
        if self._sched is None:
            return
        from apscheduler.triggers.cron import CronTrigger

        for job_id, (h, m) in zip(
            ("open15_exit", "open15_exit_retry", "open15_summary"),
            _exit_schedule_times(self.day_config),
            strict=True,
        ):
            try:
                self._sched.reschedule_job(
                    job_id,
                    trigger=CronTrigger(
                        hour=h, minute=m, second=0, day_of_week="mon-fri", timezone="Asia/Kolkata"
                    ),
                )
            except Exception:
                logger.exception("open15: reschedule failed for %s", job_id)

    def flatten(self, reason: str = "eod_0930") -> None:
        """Configured exit time (default 09:30 IST) — market-out every open
        position (also the +2 min retry backstop)."""
        from database.open15_breakout_db import update_trade

        with self._lock:
            open_pos = {s: p for s, p in self.positions.items() if p.get("status") == "open"}
        for symbol, pos in open_pos.items():
            is_option = pos.get("instrument") == "option"
            if is_option:
                # option exit is always a SELL of the bought CE/PE (issue #437)
                action = "SELL"
                resp = self.order_placer(
                    _mode(),
                    {
                        "symbol": pos["opt_symbol"],
                        "action": "SELL",
                        "quantity": pos["quantity"],
                        "exchange": "NFO",
                    },
                )
            else:
                action = "SELL" if pos["side"] == "L" else "BUY"
                resp = self.order_placer(
                    _mode(), {"symbol": symbol, "action": action, "quantity": pos["quantity"]}
                )
            ok = resp.get("status") == "success"
            last_px = (self.core.last_price.get(symbol) if self.core else None) or pos[
                "trigger_price"
            ]
            pnl = None
            charges = None
            fields = {
                "exit_ts": dt.datetime.now(IST).isoformat(),
                "exit_price": last_px,
                "exit_order_id": str(resp.get("orderid") or ""),
                "exit_status": "success" if ok else "error",
                "status": "closed" if ok else "error",
                "reason": reason,
                "entry_minute_close": self.core.entry_minute_close(symbol) if self.core else None,
            }
            if is_option:
                # real trade P&L is on the option premium, not the stock path
                from services.open15_option_shadow import option_round_trip_charges

                entry_prem = pos.get("opt_entry_premium")
                exit_prem, exit_volume, exit_oi = self._option_liquidity(pos["opt_symbol"])
                lot = pos.get("opt_lot_size") or 1
                lots = pos["quantity"] // lot if lot else 1
                # recorded even when the premium is missing — an unpriced exit is
                # exactly the case the liquidity data is meant to explain
                fields.update(opt_exit_volume=exit_volume, opt_exit_oi=exit_oi)
                if entry_prem and exit_prem:
                    pnl = (exit_prem - entry_prem) * pos["quantity"]
                    charges = option_round_trip_charges(
                        entry_prem * pos["quantity"], exit_prem * pos["quantity"]
                    )
                    per_lot_charges = round(charges / lots, 2) if charges and lots else None
                    fields.update(
                        opt_exit_premium=exit_prem,
                        opt_charges_inr=per_lot_charges,
                        opt_pnl=round((exit_prem - entry_prem) * lot - (per_lot_charges or 0.0), 2),
                    )
                else:
                    logger.warning(
                        "open15: option exit premium unavailable for %s — pnl unpriced",
                        pos.get("opt_symbol"),
                    )
            elif last_px:
                d = (
                    (last_px - pos["trigger_price"])
                    if pos["side"] == "L"
                    else (pos["trigger_price"] - last_px)
                )
                pnl = d * pos["quantity"]
                buy_px = pos["trigger_price"] if pos["side"] == "L" else last_px
                sell_px = last_px if pos["side"] == "L" else pos["trigger_price"]
                charges = mis_round_trip_charges(
                    buy_px * pos["quantity"], sell_px * pos["quantity"]
                )
            fields["pnl"] = pnl
            fields["charges_inr"] = charges
            if pos.get("row_id"):
                update_trade(pos["row_id"], **fields)
            with self._lock:
                pos["status"] = "closed" if ok else "open"  # keep open on failure for retry
            self._log_event(
                "exit",
                symbol=symbol,
                action=action,
                instrument="option" if is_option else "stock",
                qty=pos["quantity"],
                exit_price=last_px if not is_option else fields.get("opt_exit_premium"),
                pnl=round(pnl, 0) if pnl is not None else None,
                order_status=resp.get("status"),
                reason=reason,
            )
        if reason == "eod_0930":
            self.day_status = "done"
            # loud dead-feed diagnostics: an armed day with no/partial ticks must
            # be visually distinct from a genuine no-trigger day (issue #428).
            core = self.core
            if core is not None and not core.sym:
                self._log_event(
                    "no_ticks_received",
                    hint="ZMQ feed delivered ZERO ticks in the window — check WS proxy (8765), "
                    "broker session, scanner presubscribe",
                )
            elif core is not None and not core.finalized:
                self._log_event(
                    "selection_never_finalized",
                    symbols_with_ticks=len(core.sym),
                    hint="ticks stopped before 09:16 — selection could not run",
                )
            # the full near-miss picture for EVERY selected symbol, entered ones
            # included (issue #524) — `no_entry` below covers non-entered symbols
            # only, which left the UI's `max vol×` column blank on entered rows.
            if core and core.selected:
                self._log_event(
                    "watch_stats",
                    stats=core.watch_snapshot(),
                    needed=self._vol_needed(),
                )
            # log why each selected-but-not-entered watch never fired (near-miss)
            if core:
                for sym, side in core.selected.items():
                    if sym in core.entered:
                        continue
                    ws = core.watch_stats.get(sym, {})
                    self._log_event(
                        "no_entry",
                        symbol=sym,
                        side=side,
                        level_broken=ws.get("level_broken", False),
                        max_vol_ratio=round(ws.get("max_vol_ratio") or 0.0, 2),
                        max_vol_ratio_while_beyond=round(ws.get("max_vol_ratio_beyond") or 0.0, 2),
                        needed=self._vol_needed(),
                    )
            self._persist_day_log()

    def summary(self) -> None:
        """Exit+5 min — one-line research summary of today's measurement."""
        if not self.core:
            return
        n_sel = len(self.core.selected)
        n_ent = len(self.core.entered)
        drifts = []
        for s, rec in self.core.entered.items():
            close = self.core.entry_minute_close(s)
            if close and rec["level"]:
                sgn = 1 if rec["side"] == "L" else -1
                drifts.append(
                    {
                        "symbol": s,
                        "trigger_vs_level_pct": round(
                            sgn * (rec["price"] / rec["level"] - 1) * 100, 3
                        ),
                        "minclose_vs_level_pct": round(sgn * (close / rec["level"] - 1) * 100, 3),
                    }
                )
        self._log_event(
            "summary",
            selected=n_sel,
            entered=n_ent,
            day=self.day_status,
            captured_drift=drifts,
        )
        if _opt_shadow_enabled():
            # ATM option shadow pricing (issue #435). Broker current-day 1m
            # history lags ~5-15 min, so rows left unpriced here are retried
            # by the next 09:10 arm's catch-up call.
            try:
                from services.open15_option_shadow import enrich_missing

                self._log_event("opt_shadow", **enrich_missing())
            except Exception:
                logger.exception("open15: option-shadow enrichment failed")
        if self._tick_writer is not None:
            try:
                flushed = self._tick_writer.flush_now()
                self._log_event("tick_capture_flushed", records=flushed)
            except Exception:
                logger.exception("open15: tick flush failed")
        self._persist_day_log()

    def _persist_day_log(self) -> None:
        """Snapshot today's decision log so the UI can query past days.

        Called from ``_log_event`` on every event (plus the explicit
        end-of-window calls kept as belt-and-braces). The list is copied
        before serializing so a concurrent append from another thread can't
        mutate it mid-dump.
        """
        try:
            from database.open15_breakout_db import save_day_log

            if self._log_date and self.day_log:
                save_day_log(self._log_date, list(self.day_log))
        except Exception:
            logger.exception("open15: day-log persist failed")

    def register_jobs(self, scheduler=None) -> None:
        """Register the 5 cron jobs.

        The shared historify APScheduler uses a persistent SQLAlchemyJobStore,
        which PICKLES job callables — so these MUST be module-level functions
        (``_arm_job`` etc., dereferencing the singleton), never bound methods
        or lambdas. Bound methods drag ``self`` (holding a ``threading.Lock``)
        into the pickle and raise ``cannot pickle '_thread.lock' object`` —
        the bug that silently killed the 2026-07-20 first session (issue #428).
        """
        from apscheduler.triggers.cron import CronTrigger

        sched = scheduler
        if sched is None:
            from services.historify_scheduler_service import get_historify_scheduler

            sched = get_historify_scheduler().scheduler
        self._sched = sched
        tz = "Asia/Kolkata"
        # exit/retry/summary follow the configured exit time (issue #451) —
        # resolved from the DB config row + env here (boot), re-applied at every
        # 09:10 arm so a window saved mid-day takes effect at the next arm
        (eh, em), (rh, rm), (sh, sm) = _exit_schedule_times()
        jobs = [
            (
                "open15_arm",
                CronTrigger(hour=9, minute=10, day_of_week="mon-fri", timezone=tz),
                _arm_job,
            ),
            (
                "open15_first_candles",
                CronTrigger(hour=9, minute=16, second=0, day_of_week="mon-fri", timezone=tz),
                _first_candles_job,
            ),
            (
                "open15_exit",
                CronTrigger(hour=eh, minute=em, second=0, day_of_week="mon-fri", timezone=tz),
                _eod_exit_job,
            ),
            (
                "open15_exit_retry",
                CronTrigger(hour=rh, minute=rm, day_of_week="mon-fri", timezone=tz),
                _eod_retry_job,
            ),
            (
                "open15_summary",
                CronTrigger(hour=sh, minute=sm, day_of_week="mon-fri", timezone=tz),
                _summary_job,
            ),
        ]
        for job_id, trigger, fn in jobs:
            sched.add_job(fn, trigger, id=job_id, replace_existing=True, misfire_grace_time=60)
        logger.info(
            "open15: 5 scheduler jobs registered (arm 09:10 / first-candles 09:16 / "
            "exit %02d:%02d / retry %02d:%02d "
            "/ summary %02d:%02d IST)",
            eh,
            em,
            rh,
            rm,
            sh,
            sm,
        )

    # ---- tick pipeline --------------------------------------------------- #
    def _ensure_zmq_thread(self) -> None:
        if self._zmq_thread and self._zmq_thread.is_alive():
            return
        self._zmq_thread = threading.Thread(target=self._zmq_loop, name="open15-zmq", daemon=True)
        self._zmq_thread.start()

    @staticmethod
    def _now_ist() -> dt.datetime:
        """Indirected for testability (scanner-service pattern)."""
        return dt.datetime.now(IST)

    def _handle_raw(self, topic_str: str, data_str: str, now: dt.datetime) -> None:
        """Process one raw ZMQ frame — the ENTIRE per-tick pipeline.

        Extracted from the socket loop so the end-to-end test drives the exact
        same code path (parse → normalize → gate → core → capture → order).
        """
        import json

        from services.scanner_service import _normalize_tick, _parse_topic

        core = self.core
        if core is None or self.day_status != "armed":
            return
        _nea_min, exit_min = self._window_minutes()
        if not (dt.time(9, 14, 50) <= now.time() <= dt.time(exit_min // 60, exit_min % 60, 5)):
            return
        parsed = _parse_topic(topic_str)
        if parsed is None:
            return
        _exch, symbol, _md = parsed
        if symbol not in self.universe:
            return
        tick = _normalize_tick(json.loads(data_str))
        if tick is None:
            return
        # minute attribution uses the tick's exchange timestamp (naive IST);
        # the wall-clock gate above only bounds the processing window.
        tick_ts = tick.get("ts") or now.replace(tzinfo=None)
        price = float(tick["price"])
        cumvol = float(tick.get("cumulative_volume") or 0)
        was_finalized = core.finalized
        action = core.on_tick(symbol, price, cumvol, tick_ts)
        self._capture_tick(core, symbol, price, cumvol, tick_ts, was_finalized)
        if action:
            self._enter(action)

    def _zmq_loop(self) -> None:
        """Own SUB socket on the proxy bus; active only around the open."""
        import zmq

        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.SUB)
        sock.connect(os.getenv("SCANNER_ZMQ_ENDPOINT", "tcp://127.0.0.1:5555"))
        sock.setsockopt_string(zmq.SUBSCRIBE, "")
        sock.setsockopt(zmq.RCVTIMEO, 1000)
        logger.info("open15: ZMQ subscriber up")
        while not self._stop.is_set():
            try:
                topic_b, data_b = sock.recv_multipart()
            except zmq.Again:
                continue
            except Exception:
                logger.exception("open15: ZMQ recv failed")
                continue
            try:
                self._handle_raw(
                    topic_b.decode("utf-8", errors="replace"),
                    data_b.decode("utf-8", errors="replace"),
                    self._now_ist(),
                )
            except Exception:
                logger.exception("open15: tick handling failed")

    def _capture_tick(
        self,
        core: Open15Core,
        symbol: str,
        price: float,
        cumvol: float,
        ts: dt.datetime,
        was_finalized: bool,
    ) -> None:
        """Persist ticks for SELECTED symbols only (backtest replay data).

        Before selection (09:15 minute) every universe symbol's ticks are
        buffered in memory; the moment selection finalizes, only the selected
        symbols' buffers are flushed to the writer and the rest are dropped.
        After that, selected-symbol ticks stream straight to the writer.
        """
        if self._tick_writer is None:
            return
        if not core.finalized:
            buf = self._first_min_buffer.setdefault(symbol, [])
            if len(buf) < 3000:
                buf.append((price, int(cumvol), ts))
            return
        if not was_finalized and not self._capture_flushed:
            # selection just finalized on this tick — flush buffers once
            self._capture_flushed = True
            for sym in core.selected:
                for p, v, t in self._first_min_buffer.get(sym, []):
                    self._tick_writer.enqueue(sym, p, v, t)
            self._first_min_buffer = {}
            self._log_event(
                "selection",
                selected=dict(core.selected),
                gaps_pct={s: round(core.gaps.get(s, 0) * 100, 2) for s in core.selected},
                # issue #456 provenance: the exact prev-close each gap divided by,
                # so a selection oddity is auditable straight from the day log
                prev_closes={s: core.prev_closes.get(s) for s in core.selected},
                candidates=len(core.gaps),
            )
        if symbol in core.selected:
            self._tick_writer.enqueue(symbol, price, int(cumvol), ts)

    def _journal_skip(self, action: dict, reason: str, **extra) -> None:
        """Journal a trigger that did NOT place an order (cap / sizing skips)."""
        from database.open15_breakout_db import insert_trade

        insert_trade(
            trade_date=dt.datetime.now(IST).strftime("%Y-%m-%d"),
            symbol=action["symbol"],
            side=action["side"],
            mode=_mode(),
            gap_pct=action["gap_pct"],
            level=action["level"],
            baseline_vol=action["baseline_vol"],
            cum_vol_at_trigger=action["cum_vol_at_trigger"],
            trigger_minute=action["trigger_minute"],
            trigger_second=action["trigger_second"],
            trigger_price=action["price"],
            quantity=0,
            status="skipped",
            reason=reason,
            **extra,
        )
        self._log_event("entry_skipped", symbol=action["symbol"], reason=reason, **extra)

    def _enter(self, action: dict) -> None:
        from database.open15_breakout_db import insert_trade

        cfg = self.day_config or {}
        max_trades = int(cfg.get("max_trades") or _max_trades_default())
        with self._lock:
            n_placed = len(self.positions)
        if n_placed >= max_trades:
            self._journal_skip(action, "max_trades_cap", instrument=cfg.get("instrument", "stock"))
            return
        if cfg.get("instrument") == "atm_option":
            self._enter_option(action, cfg)
            return

        notional = cfg.get("notional") or _notional()
        qty = max(int(notional / action["price"]), 1)
        side_word = "BUY" if action["side"] == "L" else "SELL"
        resp = self.order_placer(
            _mode(), {"symbol": action["symbol"], "action": side_word, "quantity": qty}
        )
        ok = resp.get("status") == "success"
        row_id = insert_trade(
            trade_date=dt.datetime.now(IST).strftime("%Y-%m-%d"),
            symbol=action["symbol"],
            side=action["side"],
            mode=_mode(),
            instrument="stock",
            gap_pct=action["gap_pct"],
            level=action["level"],
            baseline_vol=action["baseline_vol"],
            cum_vol_at_trigger=action["cum_vol_at_trigger"],
            trigger_minute=action["trigger_minute"],
            trigger_second=action["trigger_second"],
            trigger_price=action["price"],
            quantity=qty,
            entry_order_id=str(resp.get("orderid") or ""),
            entry_status="success" if ok else "error",
            status="open" if ok else "error",
        )
        with self._lock:
            self.positions[action["symbol"]] = {
                **action,
                "trigger_price": action["price"],
                "quantity": qty,
                "row_id": row_id,
                "status": "open" if ok else "error",
            }
        self._log_event(
            "entry",
            symbol=action["symbol"],
            side=side_word,
            qty=qty,
            trigger_price=round(action["price"], 2),
            level=round(action["level"], 2),
            at=f"{action['trigger_minute']}:{action['trigger_second']:02d}",
            cumvol=int(action["cum_vol_at_trigger"]),
            baseline=int(action["baseline_vol"]),
            vol_ratio=round(action["cum_vol_at_trigger"] / max(action["baseline_vol"], 1), 2),
            order_status=resp.get("status"),
            order_id=resp.get("orderid"),
        )

    def _option_liquidity(self, opt_symbol: str) -> tuple[float | None, int | None, int | None]:
        """``(ltp, volume, oi)`` for an option contract (issue #488).

        The volume/OI half is pure instrumentation — nothing gates on it. When the
        snapshot is unavailable this falls back to ``quote_fn`` so sizing and exit
        pricing behave exactly as before and the liquidity columns simply stay
        NULL. Never raises: a broken snapshot must not cost an entry or an exit.
        """
        snap = None
        try:
            if self.quote_snapshot_fn is not None:
                snap = self.quote_snapshot_fn(opt_symbol, "NFO")
        except Exception:
            logger.exception("open15: liquidity snapshot raised for %s", opt_symbol)
        if snap:
            return snap.get("ltp"), snap.get("volume"), snap.get("oi")
        return self.quote_fn(opt_symbol, "NFO"), None, None

    def _enter_option(self, action: dict, cfg: dict) -> None:
        """Option-mode entry (issue #437): BUY the ATM CE (L) / PE (S).

        Fit-to-capital sizing: lots = floor(slot capital / (premium x lot));
        0 lots -> journaled ``unaffordable`` skip. Both directions are premium
        BUYS — the strategy never sells options.
        """
        from database.open15_breakout_db import insert_trade
        from services.open15_option_shadow import resolve_atm_option

        today = dt.datetime.now(IST).strftime("%Y-%m-%d")
        contract = resolve_atm_option(action["symbol"], action["side"], action["price"], today)
        if not contract:
            self._journal_skip(action, "no_option_contract", instrument="option")
            return
        premium, opt_volume, opt_oi = self._option_liquidity(contract["symbol"])
        if not premium:
            self._journal_skip(
                action, "no_option_quote", instrument="option", opt_symbol=contract["symbol"]
            )
            return
        lot = int(contract["lotsize"])
        slot_capital = float(cfg.get("margin_effective") or cfg.get("margin_per_slot") or 30_000)
        lots = int(slot_capital // (premium * lot))
        if lots < 1:
            self._journal_skip(
                action,
                "unaffordable",
                instrument="option",
                opt_symbol=contract["symbol"],
                opt_lot_size=lot,
                opt_entry_premium=premium,
                opt_entry_volume=opt_volume,
                opt_entry_oi=opt_oi,
            )
            return
        qty = lots * lot
        resp = self.order_placer(
            _mode(),
            {"symbol": contract["symbol"], "action": "BUY", "quantity": qty, "exchange": "NFO"},
        )
        ok = resp.get("status") == "success"
        row_id = insert_trade(
            trade_date=today,
            symbol=action["symbol"],
            side=action["side"],
            mode=_mode(),
            instrument="option",
            gap_pct=action["gap_pct"],
            level=action["level"],
            baseline_vol=action["baseline_vol"],
            cum_vol_at_trigger=action["cum_vol_at_trigger"],
            trigger_minute=action["trigger_minute"],
            trigger_second=action["trigger_second"],
            trigger_price=action["price"],
            quantity=qty,
            opt_symbol=contract["symbol"],
            opt_lot_size=lot,
            opt_entry_premium=premium,
            opt_entry_volume=opt_volume,
            opt_entry_oi=opt_oi,
            entry_order_id=str(resp.get("orderid") or ""),
            entry_status="success" if ok else "error",
            status="open" if ok else "error",
        )
        with self._lock:
            self.positions[action["symbol"]] = {
                **action,
                "trigger_price": action["price"],
                "quantity": qty,
                "row_id": row_id,
                "status": "open" if ok else "error",
                "instrument": "option",
                "opt_symbol": contract["symbol"],
                "opt_lot_size": lot,
                "opt_entry_premium": premium,
            }
        self._log_event(
            "entry",
            symbol=action["symbol"],
            side="BUY",
            instrument="option",
            contract=contract["symbol"],
            lots=lots,
            lot_size=lot,
            premium=premium,
            qty=qty,
            trigger_price=round(action["price"], 2),
            at=f"{action['trigger_minute']}:{action['trigger_second']:02d}",
            order_status=resp.get("status"),
            order_id=resp.get("orderid"),
        )

    # ---- status for the blueprint ---------------------------------------- #
    def _vol_needed(self) -> float:
        """The multiplier that actually gated today's entries (issue #524).

        The day config wins: a UI override on `open15_config.vol_mult` is what
        `arm()` handed the core, so reporting the raw env `_vol_mult()` here
        would show a threshold the run never used.
        """
        cfg_val = (self.day_config or {}).get("vol_mult")
        try:
            return float(cfg_val) if cfg_val is not None else _vol_mult()
        except (TypeError, ValueError):
            return _vol_mult()

    def get_status(self) -> dict:
        core = self.core
        return {
            "strategy": STRATEGY_NAME,
            "enabled": _enabled(),
            "mode": _mode(),
            "day_status": self.day_status,
            # effective, not the raw env read (issue #524): every other field
            # here already prefers the day config, and reporting a threshold
            # the run never used sits badly next to `vol_needed` below
            "vol_mult": self._vol_needed(),
            "top_n": _top_n(),
            "notional_per_trade": _notional(),
            "instrument": (self.day_config or {}).get("instrument") or _instrument_default(),
            "max_trades": (self.day_config or {}).get("max_trades") or _max_trades_default(),
            "trade_side": (self.day_config or {}).get("trade_side") or _trade_side_default(),
            "universe_size": len(self.universe),
            "selected": dict(core.selected) if core else {},
            "gaps_pct": {s: round(g * 100, 2) for s, g in (core.gaps or {}).items()}
            if core
            else {},
            "entered": list(core.entered) if core else [],
            # live near-miss stats so the decision-log UI can fill `max vol×`
            # during the window instead of waiting for the exit job (issue #524)
            "watch_stats": core.watch_snapshot() if core else {},
            "vol_needed": self._vol_needed(),
            "positions": {
                s: {k: p.get(k) for k in ("side", "quantity", "trigger_price", "status")}
                for s, p in self.positions.items()
            },
            "tick_capture": bool(self._tick_writer),
            "config": dict(self.day_config or {}),
            "day_log": self.day_log[-100:],
        }


_service: Open15BreakoutService | None = None


def get_open15_service() -> Open15BreakoutService | None:
    return _service


def _exit_schedule_times(day_config: dict | None = None) -> tuple[tuple[int, int], ...]:
    """Effective (exit, retry, summary) job times as (hour, minute) tuples.

    Resolved from the passed day-config (arm path) or, when None, freshly from
    the DB config row + env defaults (boot path). Retry is exit+2, summary
    exit+5 — with exit capped at 15:10 the retry always precedes the 15:15
    MIS square-off cutoff (issue #451).
    """
    cfg = day_config
    if cfg is None:
        try:
            from database.open15_breakout_db import get_config

            cfg = resolve_day_config(get_config(), 0.0)
        except Exception:
            logger.exception("open15: config read failed for exit schedule — using defaults")
            cfg = None
    exit_min = parse_hhmm((cfg or {}).get("exit_time"))
    if exit_min is None:
        exit_min = _EXIT_MIN
    return tuple(divmod(m, 60) for m in (exit_min, exit_min + 2, exit_min + 5))


# --- module-level job callables (MUST stay module-level: the persistent
# SQLAlchemy jobstore pickles them by reference; see register_jobs docstring) ---
def _arm_job() -> None:
    svc = get_open15_service()
    if svc is not None:
        svc.arm()


def _first_candles_job() -> None:
    svc = get_open15_service()
    if svc is not None:
        svc.capture_first_candles()


def _eod_exit_job() -> None:
    svc = get_open15_service()
    if svc is not None:
        svc.flatten("eod_0930")


def _eod_retry_job() -> None:
    svc = get_open15_service()
    if svc is not None:
        svc.flatten("eod_retry_0932")


def _summary_job() -> None:
    svc = get_open15_service()
    if svc is not None:
        svc.summary()


def init_open15_breakout_service(app=None) -> Open15BreakoutService | None:
    """Boot hook (app.py). Builds the service + registers scheduler jobs."""
    global _service
    if not _enabled():
        logger.info("open15: disabled via OPEN15_ENABLED — not starting")
        return None
    _service = Open15BreakoutService()
    _service.register_jobs()
    # A boot inside 09:10..15:30 on a weekday arms immediately: before 09:15:30
    # this makes an 09:05-09:14 boot work; after it, arm() marks the day
    # skipped_late_boot LOUDLY (and persists it) instead of leaving the day
    # silently 'idle' because the 09:10 cron already passed (issue #428).
    now = dt.datetime.now(IST)
    if now.weekday() < 5 and dt.time(9, 10) <= now.time() < dt.time(15, 30):
        _service.arm()
    return _service
