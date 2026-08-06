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
  - Optional ROLLING additive watch list (issue #529, default OFF): every
    ``rolling_cadence_s`` (UI-editable, default 30s) inside the entry window,
    re-rank the universe on live LTP vs prev close and APPEND the current
    top-N movers. Purely additive — the 09:16 seed picks are never dropped —
    and the entry gate is unchanged, so added symbols compete for the same
    ``max_trades`` slots first-come-first-served. Shipped as a MEASUREMENT
    (journal column ``watch_source ∈ {seed, rolling}``), not a validated edge:
    the 2026-08-03 replay showed the 09:16 ranking misses the day's biggest
    movers but could NOT show the added names are profitable.
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
    """Master switch for tick persistence (backtest replay data)."""
    return os.getenv("OPEN15_TICK_CAPTURE", "true").lower() == "true"


def _tick_capture_universe_enabled() -> bool:
    """Capture EVERY universe symbol's ticks, not just the day's picks (#528).

    The strategy's own 09:15-09:30 window was previously only replayable for
    the 3 selected symbols, so any question about symbols outside the 09:16 gap
    ranking (e.g. adding intraday top gainers to the watch list) had no data.
    Off => the pre-#528 selected-symbols-only behaviour.
    """
    return os.getenv("OPEN15_TICK_CAPTURE_UNIVERSE", "true").lower() == "true"


def _opt_shadow_enabled() -> bool:
    """ATM option shadow pricing on journal rows (issue #435, research-only)."""
    return os.getenv("OPEN15_OPT_SHADOW_ENABLED", "true").lower() == "true"


def _sim_skipped_enabled() -> bool:
    """Price triggers no order was sent for (unaffordable / cap), issue #555."""
    return os.getenv("OPEN15_SIM_SKIPPED_ENABLED", "true").lower() == "true"


def _paper_sim_max() -> int:
    """Daily cap on simulated rows.

    They cost no money and place no orders, but each one spends a broker quote
    at entry and another at exit — so a 9-selection all-unaffordable day is
    bounded rather than left to fan out.
    """
    try:
        return max(int(os.getenv("OPEN15_PAPER_SIM_MAX", "10")), 0)
    except (TypeError, ValueError):
        return 10


def _fill_reconcile_enabled() -> bool:
    """Reconcile real rows against the broker's own fills (issue #555)."""
    return os.getenv("OPEN15_FILL_RECONCILE_ENABLED", "true").lower() == "true"


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


# Rolling additive watch list (issue #529). OFF by default — deploying this is a
# no-op until the operator enables it from /open15_vol_breakout/logs.
_ROLLING_CADENCE_MIN_S = 10
_ROLLING_CADENCE_MAX_S = 300
_ROLLING_TOP_N_MIN = 1
_ROLLING_TOP_N_MAX = 10


def clamp_rolling_cadence(value) -> int:
    """Clamp a proposed re-rank cadence (seconds) into 10..300. Bad input -> 30.

    Server-side clamping is deliberate: the UI number input is a hint, never a
    trust boundary — a hand-crafted POST must not be able to set a 1-second
    re-rank (a hot loop over ~210 symbols on the tick thread) or a cadence
    longer than the entry window itself.
    """
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return 30
    return max(_ROLLING_CADENCE_MIN_S, min(v, _ROLLING_CADENCE_MAX_S))


def clamp_rolling_top_n(value) -> int:
    """Clamp a proposed per-side additions-per-cycle into 1..10. Bad input -> 3."""
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return 3
    return max(_ROLLING_TOP_N_MIN, min(v, _ROLLING_TOP_N_MAX))


def _rolling_enabled_default() -> bool:
    return os.getenv("OPEN15_ROLLING_WATCHLIST_ENABLED", "false").lower() == "true"


def _rolling_cadence_default() -> int:
    return clamp_rolling_cadence(os.getenv("OPEN15_ROLLING_CADENCE_S", "30"))


def _rolling_top_n_default() -> int:
    return clamp_rolling_top_n(os.getenv("OPEN15_ROLLING_TOP_N", "3"))


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
    # rolling additive watch list (issue #529). ``is None`` — not truthiness —
    # so an explicit stored ``false`` beats an env default of ``true``.
    rolling_enabled_cfg = cfg.get("rolling_watchlist_enabled")
    rolling_enabled = (
        _rolling_enabled_default() if rolling_enabled_cfg is None else bool(rolling_enabled_cfg)
    )
    rolling_cadence_cfg = cfg.get("rolling_cadence_s")
    rolling_cadence_s = (
        _rolling_cadence_default()
        if rolling_cadence_cfg is None
        else clamp_rolling_cadence(rolling_cadence_cfg)
    )
    rolling_top_n_cfg = cfg.get("rolling_top_n")
    rolling_top_n = (
        _rolling_top_n_default()
        if rolling_top_n_cfg is None
        else clamp_rolling_top_n(rolling_top_n_cfg)
    )
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
        "rolling_watchlist_enabled": rolling_enabled,
        "rolling_cadence_s": rolling_cadence_s,
        "rolling_top_n": rolling_top_n,
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
        rolling_enabled: bool = False,
        rolling_cadence_s: int = 30,
        rolling_top_n: int = 3,
    ):
        self.prev_closes = prev_closes
        self.vol_mult = vol_mult
        self.top_n = top_n
        # rolling additive watch list (issue #529): every ``rolling_cadence_s``
        # inside the entry window, re-rank the universe on live LTP and APPEND
        # the current top-N movers. Nothing is ever removed, and the entry gate
        # is untouched — added symbols compete for the same ``max_trades`` slots.
        self.rolling_enabled = bool(rolling_enabled)
        self.rolling_cadence_s = clamp_rolling_cadence(rolling_cadence_s)
        self.rolling_top_n = clamp_rolling_top_n(rolling_top_n)
        self._last_rerank: dt.datetime | None = None
        # symbol -> "seed" (09:16 gap ranking) / "rolling" (intraday re-rank)
        self.watch_source: dict[str, str] = {}
        # ordered record of every addition, for the status API and the UI panel
        self.rolling_adds: list[dict[str, Any]] = []
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
            self.watch_source.setdefault(s, "seed")
            self.watch_stats.setdefault(
                s, {"max_vol_ratio": None, "max_vol_ratio_beyond": None, "level_broken": False}
            )

    def maybe_rerank(self, ts: dt.datetime) -> list[dict[str, Any]]:
        """Additive intraday re-rank (issue #529) — returns the symbols ADDED.

        Called once per tick from the service; it self-throttles to one pass
        every ``rolling_cadence_s``. Ranking is pure in-process arithmetic over
        state already held: the last LTP seen on the service's own ZMQ SUB
        divided by the arm-time prev close. No broker calls, no new feed.

        Invariants (all covered by tests):
          - **additive only** — an entry is only ever inserted into
            ``selected`` / ``watch_source``, never removed or re-sided, so the
            09:16 seed picks stay watched for the whole session;
          - ``trade_side`` is honoured, so an excluded side is never added;
          - a symbol with no usable breakout level (no broker first candle and
            no tick-built fallback) is skipped — watching it could never
            produce a legal entry.
        """
        if not self.rolling_enabled or not self.finalized:
            return []
        minute = ts.hour * 60 + ts.minute
        if minute < _ENTRY_FROM or minute > self.entry_to_min:
            return []
        if self._last_rerank is not None:
            elapsed = (ts - self._last_rerank).total_seconds()
            # a tick timestamp that jumps backwards (feed replay / clock skew)
            # must not wedge the cadence — treat a negative delta as "due"
            if 0 <= elapsed < self.rolling_cadence_s:
                return []
        self._last_rerank = ts

        pct: dict[str, float] = {}
        for sym, price in list(self.last_price.items()):
            pc = self.prev_closes.get(sym)
            if pc and pc > 0 and price > 0:
                pct[sym] = price / pc - 1.0

        adds: list[dict[str, Any]] = []
        sides: list[tuple[str, list[str]]] = []
        if self.trade_side != "short_only":
            sides.append(("L", sorted((s for s in pct if pct[s] > 0), key=lambda s: -pct[s])))
        if self.trade_side != "long_only":
            sides.append(("S", sorted((s for s in pct if pct[s] < 0), key=lambda s: pct[s])))
        for side, ranked in sides:
            for rank, sym in enumerate(ranked[: self.rolling_top_n], start=1):
                if sym in self.selected or self.first_candle(sym) is None:
                    continue
                # seed the stats key BEFORE publishing the symbol into
                # ``selected``, so a concurrent ``watch_snapshot`` read can
                # never see a watched symbol with no stats entry
                self.watch_stats.setdefault(
                    sym,
                    {"max_vol_ratio": None, "max_vol_ratio_beyond": None, "level_broken": False},
                )
                self.watch_source[sym] = "rolling"
                self.selected[sym] = side
                rec = {
                    "symbol": sym,
                    "side": side,
                    "pct_change": round(pct[sym] * 100, 2),
                    "rank": rank,
                    "watch_size": len(self.selected),
                    "at": f"{ts.hour:02d}:{ts.minute:02d}:{ts.second:02d}",
                }
                self.rolling_adds.append(rec)
                adds.append(rec)
        if adds:
            logger.info(
                "open15: rolling watch-list +%d (size %d) — %s",
                len(adds),
                len(self.selected),
                ", ".join(f"{a['symbol']}:{a['side']}{a['pct_change']:+.2f}%" for a in adds),
            )
        return adds

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
                # seed vs rolling cohort (issue #529) — journaled on the row so
                # the two can be scored separately later
                "watch_source": self.watch_source.get(symbol, "seed"),
            }
            self.entered[symbol] = action
            return action
        return None

    def watch_snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-watched-symbol near-miss stats, safe to read off-thread (#524).

        Keys are seeded at ``_finalize_selection`` — and, when the rolling watch
        list is on, at ``maybe_rerank`` (issue #529). Both writers run on the
        ZMQ tick thread and only ever ADD keys; the ``list(...)`` copy is a
        single C-level pass that cannot interleave with them, so a Flask reader
        sees a consistent snapshot. ``max_vol_ratio`` is ``None`` until the
        symbol's first in-window tick — blank means "no data", NOT a 0.0 ratio.

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
                "watch_source": self.watch_source.get(sym, "seed"),
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
    """Full quote for the option paths: last price, the contract's own traded
    volume and open interest (issue #488), and the top-of-book bid/ask (#555).

    The bid/ask pair costs NOTHING extra — ``get_quotes`` has always returned it
    in this same response and open15 simply dropped it on the floor. It is the
    one liquidity fact that directly costs money: the strategy sends MARKET
    orders, so it crosses the spread on entry and again on exit.

    All of it is recorded for research only — nothing gates on any of it (the
    #488 rule). Returns None when the quote is unavailable; callers treat that
    as "unknown" and fall back to the price-only path.
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
            # 0 is the mapper's "absent", not a price — keep it as unknown
            "bid": float(d["bid"]) if d.get("bid") else None,
            "ask": float(d["ask"]) if d.get("ask") else None,
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
        # tick capture. Universe mode (#528, default): every universe symbol's
        # ticks stream to disk for the whole window. Targeted mode (legacy):
        # only the day's SELECTED symbols are persisted, plus their 09:15
        # first-minute ticks (buffered in memory until selection finalizes).
        self._tick_writer = None
        self._capture_universe = False
        self._first_min_buffer: dict[str, list[tuple]] = {}
        self._capture_flushed = False
        # per-day dedup for the broker-rejection alert (issue #548): a static-IP
        # or RMS block rejects EVERY entry the same way, and three identical
        # Telegram messages is how an alert channel gets muted.
        self._rejection_alert_date: str | None = None

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

                # universe capture is ~130 ticks/s across ~210 symbols, and the
                # first-minute burst arrives as one flush — the default 10k
                # queue would drop under it, so widen queue + batch (#528).
                universe_capture = _tick_capture_universe_enabled()
                self._tick_writer = TickLogWriter(
                    enabled=True,
                    directory="tick_logs/open15",
                    retention_days=365,
                    max_queue=50000 if universe_capture else 10000,
                    batch_size=500 if universe_capture else 200,
                )
                self._capture_universe = universe_capture
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
                rolling_enabled=self.day_config["rolling_watchlist_enabled"],
                rolling_cadence_s=self.day_config["rolling_cadence_s"],
                rolling_top_n=self.day_config["rolling_top_n"],
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
            # what the day actually traded (issue #555). Absent before now, so a
            # past day's log could not say whether its P&L was on stock or on
            # option premium — the single most load-bearing fact for reading it.
            instrument=self.day_config.get("instrument"),
            # rolling additive watch list (issue #529) — the effective values,
            # so a day is replayable from its own log
            rolling_watchlist_enabled=self.day_config["rolling_watchlist_enabled"],
            rolling_cadence_s=self.day_config["rolling_cadence_s"],
            rolling_top_n=self.day_config["rolling_top_n"],
            sizing_mode=self.day_config["sizing_mode"],
            margin_per_slot=self.day_config["margin_per_slot"],
            margin_effective=self.day_config["margin_effective"],
            notional=self.day_config["notional"],
            cum_realized_pnl=self.day_config["cum_realized_pnl"],
            config_source="ui" if cfg_row else "env_defaults",
            tick_capture=bool(self._tick_writer),
            tick_capture_universe=bool(self._tick_writer) and self._capture_universe,
            prev_close_check=prev_check,
            first_candle_source=_first_candle_source(),
            baseline_includes_first_minute=_baseline_includes_first_minute(),
        )
        self._ensure_zmq_thread()
        if _opt_shadow_enabled() or _fill_reconcile_enabled():
            # catch-up pricing for rows the 09:35 broker-lag left unpriced (the
            # #435 shadow premiums and the #555 fill prices, each gated inside)
            # — daemon thread, never blocks arm
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
        """Daemon-thread wrapper for the arm-time catch-up passes.

        Two independent retries share this thread because they retry for the
        same reason — the broker had not published the data yet at 09:35:
        option-shadow premiums (1m history lags 5-15 min) and fill prices for a
        leg still reporting as open. Both are idempotent and fail-graceful, and
        neither may take the other down, so they are wrapped separately.
        """
        if _opt_shadow_enabled():
            try:
                from services.open15_option_shadow import enrich_missing

                res = enrich_missing()
                if res.get("priced"):
                    logger.info("open15 opt-shadow catch-up: %s", res)
            except Exception:
                logger.exception("open15: option-shadow catch-up failed")
        try:
            from services.open15_option_shadow import enrich_liquidity_paths

            res = enrich_liquidity_paths()
            if res.get("priced"):
                logger.info("open15 liquidity-path catch-up: %s", res)
        except Exception:
            logger.exception("open15: liquidity-path catch-up failed")
        if not _fill_reconcile_enabled():
            return
        try:
            from services.open15_fill_reconcile import reconcile_fills

            # no date filter: this is the catch-up for ANY day whose legs were
            # still pending, including one the app was restarted through
            res = reconcile_fills()
            if res.get("reconciled"):
                logger.info("open15 fill-reconcile catch-up: %s", res)
        except Exception:
            logger.exception("open15: fill-reconcile catch-up failed")

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

    def _broker_qty(self, symbol: str, exchange: str) -> int | None:
        """Net quantity this strategy holds in ``symbol``, or None if unknown.

        Used to decide whether a broker-rejected entry (issue #548) really left
        us flat. ``mode_key`` is mandatory: it routes the READ to the same book
        the order was routed to, which is the #497 rule — without it a sandbox
        strategy reads the empty LIVE book and concludes it is flat.

        Returns ``None`` on any failure — the caller papers an unverifiable
        symbol rather than squaring it off. That is the safer direction: an
        unnecessary square-off SELL against a position that was never opened is
        a naked short, whereas a genuinely-filled lot left as paper is still
        caught by the broker's own 15:15 MIS auto-square-off. Only an
        AFFIRMATIVE non-zero book quantity justifies sending an order.
        """
        try:
            from database.auth_db import get_first_available_api_key
            from services.positionbook_service import get_positionbook

            api_key = get_first_available_api_key()
            if not api_key:
                logger.warning("open15: no api key — cannot verify book for %s", symbol)
                return None
            ok, resp, _ = get_positionbook(api_key=api_key, mode_key=STRATEGY_NAME)
            if not ok:
                logger.warning("open15: positionbook read failed for %s: %s", symbol, resp)
                return None
            for row in (resp or {}).get("data") or []:
                if (
                    str(row.get("symbol") or "").upper() == symbol.upper()
                    and str(row.get("exchange") or "").upper() == exchange.upper()
                ):
                    return int(float(row.get("quantity") or 0))
            return 0
        except Exception:
            logger.exception("open15: positionbook verify raised for %s", symbol)
            return None

    def _resolve_paper_position(self, symbol: str, pos: dict) -> bool:
        """True if ``pos`` is confirmed flat and may be priced as paper (#548).

        A 403/RMS rejection is unambiguous, but a timeout or a dropped response
        is not: the order may have reached the exchange. Rather than guess, ask
        the book. Only an AFFIRMATIVE non-zero quantity promotes the row back to
        a real ``open`` position so the normal square-off runs. An unreadable
        book (``None``) is papered with a loud warning — sending a square-off we
        cannot justify would open a naked position, while a genuinely-filled lot
        left as paper is still caught by the 15:15 MIS auto-square-off.
        """
        is_option = pos.get("instrument") == "option"
        sym = pos["opt_symbol"] if is_option else symbol
        qty = self._broker_qty(sym, "NFO" if is_option else "NSE")
        if qty:
            logger.warning(
                "open15: rejected entry %s actually HOLDS %s in the book — "
                "squaring off for real instead of recording a paper fill",
                sym,
                qty,
            )
            with self._lock:
                self.positions[symbol]["status"] = "open"
                self.positions[symbol]["fill"] = "real"
            self._log_event(
                "rejection_unverified",
                symbol=symbol,
                contract=pos.get("opt_symbol"),
                book_qty=qty,
                action="squaring off as a real position",
            )
            return False
        if qty is None:
            logger.warning(
                "open15: could not read the position book for %s — recording the "
                "rejected entry as PAPER unverified (no square-off sent; a real "
                "fill would still be squared off by the 15:15 MIS auto-square-off)",
                sym,
            )
            self._log_event(
                "rejection_unverified",
                symbol=symbol,
                contract=pos.get("opt_symbol"),
                book_qty=None,
                action="papered without book confirmation",
            )
        return True

    def _flatten_paper(self, symbol: str, pos: dict, reason: str) -> None:
        """Close a non-traded row as a PAPER or SIM fill (issues #548, #555).

        Places NO order — there is no position to close. Prices the exit exactly
        where a real flatten would have, so the row carries the full
        sandbox-equivalent measurement (exit price, gross P&L, modelled charges)
        and the day stays comparable to one that traded.

        Serves both non-traded classes because the *pricing* is identical; only
        the labels differ. A ``sim`` row keeps its ``skipped`` status and its
        original skip reason — it was never rejected, and relabelling it would
        blame the broker for a budget decision.
        """
        from database.open15_breakout_db import update_trade

        is_option = pos.get("instrument") == "option"
        is_sim = pos.get("fill") == "sim"
        fields = {
            "exit_ts": dt.datetime.now(IST).isoformat(),
            "exit_order_id": "",
            "exit_status": "not_placed",
            "status": "skipped" if is_sim else "rejected",
            "fill": "sim" if is_sim else "paper",
            "pnl_source": "quote",
            "fill_reconcile_status": "not_applicable",
            "reason": (
                pos.get("sim_reason", reason)
                if is_sim
                else (reason if reason != "eod_0930" else "entry_rejected")
            ),
            "entry_minute_close": self.core.entry_minute_close(symbol) if self.core else None,
        }
        if is_sim:
            # the size the P&L is priced on; `quantity` stays 0 (nothing ordered)
            fields["sim_quantity"] = pos["quantity"]
        pnl = charges = None
        exit_px = None
        if is_option:
            from services.open15_option_shadow import option_round_trip_charges

            entry_prem = pos.get("opt_entry_premium")
            liq = self._option_liquidity(pos["opt_symbol"])
            exit_px = liq["ltp"]
            fields.update(
                opt_exit_volume=liq["volume"],
                opt_exit_oi=liq["oi"],
                opt_exit_bid=liq["bid"],
                opt_exit_ask=liq["ask"],
            )
            lot = pos.get("opt_lot_size") or 1
            lots = pos["quantity"] // lot if lot else 1
            if entry_prem and exit_px:
                pnl = (exit_px - entry_prem) * pos["quantity"]
                charges = option_round_trip_charges(
                    entry_prem * pos["quantity"], exit_px * pos["quantity"]
                )
                per_lot_charges = round(charges / lots, 2) if charges and lots else None
                fields.update(
                    opt_exit_premium=exit_px,
                    opt_charges_inr=per_lot_charges,
                    opt_pnl=round((exit_px - entry_prem) * lot - (per_lot_charges or 0.0), 2),
                )
            else:
                # left unpriced rather than guessed — the summary's `opt_shadow`
                # catch-up re-prices it from 1m bars once the broker publishes them
                logger.warning(
                    "open15: paper exit premium unavailable for %s — pnl unpriced",
                    pos.get("opt_symbol"),
                )
        else:
            exit_px = (self.core.last_price.get(symbol) if self.core else None) or pos[
                "trigger_price"
            ]
            fields["exit_price"] = exit_px
            d = (
                (exit_px - pos["trigger_price"])
                if pos["side"] == "L"
                else (pos["trigger_price"] - exit_px)
            )
            pnl = d * pos["quantity"]
            buy_px = pos["trigger_price"] if pos["side"] == "L" else exit_px
            sell_px = exit_px if pos["side"] == "L" else pos["trigger_price"]
            charges = mis_round_trip_charges(buy_px * pos["quantity"], sell_px * pos["quantity"])
        fields["pnl"] = pnl
        fields["charges_inr"] = charges
        if pos.get("row_id"):
            update_trade(pos["row_id"], **fields)
        with self._lock:
            pos["status"] = "closed"
        self._log_event(
            # a THIRD event name, not a flag on `exit_paper` (issue #555): the
            # digest sums events by name, so folding sim into the paper event
            # would silently merge the two buckets the operator asked to keep apart
            "exit_sim" if is_sim else "exit_paper",
            symbol=symbol,
            instrument="option" if is_option else "stock",
            contract=pos.get("opt_symbol"),
            qty=pos["quantity"],
            entry_price=pos.get("opt_entry_premium") if is_option else pos.get("trigger_price"),
            exit_price=exit_px,
            # both legs, same reason as the real `exit` event above
            stock_entry_price=round(pos["trigger_price"], 2),
            stock_exit_price=(self.core.last_price.get(symbol) if self.core else None),
            opt_entry_premium=pos.get("opt_entry_premium"),
            bid=fields.get("opt_exit_bid"),
            ask=fields.get("opt_exit_ask"),
            volume=fields.get("opt_exit_volume"),
            oi=fields.get("opt_exit_oi"),
            gross=round(pnl, 2) if pnl is not None else None,
            charges=charges,
            pnl=round(pnl - (charges or 0.0), 2) if pnl is not None else None,
            fill="sim" if is_sim else "paper",
            reason=pos.get("sim_reason") if is_sim else reason,
            note=(
                f"no order was placed ({pos.get('sim_reason')}) — priced at "
                f"{pos['quantity']} for measurement only, no money moved"
                if is_sim
                else "order was rejected — sandbox-equivalent, no money moved"
            ),
        )

    def flatten(self, reason: str = "eod_0930") -> None:
        """Configured exit time (default 09:30 IST) — market-out every open
        position (also the +2 min retry backstop).

        Broker-rejected entries (``status='paper'``, issue #548) are resolved
        here too: verified flat against the book, then priced as paper fills
        with NO order placed. They are handled first so a position promoted back
        to ``open`` by the verification is squared off in this same pass.
        """
        from database.open15_breakout_db import update_trade

        with self._lock:
            paper_pos = {s: p for s, p in self.positions.items() if p.get("status") == "paper"}
            sim_pos = {s: p for s, p in self.positions.items() if p.get("status") == "sim"}
        for symbol, pos in paper_pos.items():
            if self._resolve_paper_position(symbol, pos):
                self._flatten_paper(symbol, pos, reason)
        # SIM rows (issue #555) skip the book verification entirely: no order was
        # ever sent for them, so there is nothing that could have half-reached
        # the exchange. Reading the book here could only surface an unrelated
        # same-symbol position and promote a trade we never placed into a live
        # square-off — the one failure mode this whole path must not have.
        for symbol, pos in sim_pos.items():
            self._flatten_paper(symbol, pos, reason)

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
                "fill": "real",
                "reason": reason,
                "entry_minute_close": self.core.entry_minute_close(symbol) if self.core else None,
            }
            if is_option:
                # real trade P&L is on the option premium, not the stock path
                from services.open15_option_shadow import option_round_trip_charges

                entry_prem = pos.get("opt_entry_premium")
                liq = self._option_liquidity(pos["opt_symbol"])
                exit_prem = liq["ltp"]
                lot = pos.get("opt_lot_size") or 1
                lots = pos["quantity"] // lot if lot else 1
                # recorded even when the premium is missing — an unpriced exit is
                # exactly the case the liquidity data is meant to explain
                fields.update(
                    opt_exit_volume=liq["volume"],
                    opt_exit_oi=liq["oi"],
                    opt_exit_bid=liq["bid"],
                    opt_exit_ask=liq["ask"],
                )
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
                # BOTH legs (issue #555). In option mode `exit_price` above is
                # the premium the P&L is computed on, so without these the page
                # can show either the signal or the money, never both.
                stock_entry_price=round(pos["trigger_price"], 2),
                stock_exit_price=last_px,
                opt_entry_premium=pos.get("opt_entry_premium"),
                bid=fields.get("opt_exit_bid"),
                ask=fields.get("opt_exit_ask"),
                volume=fields.get("opt_exit_volume"),
                oi=fields.get("opt_exit_oi"),
                # gross/charges/pnl(NET) — the SAME shape `exit_paper` emits, so
                # the page never has to know which kind of exit it is reading
                # (issue #552: this used to be gross-rounded-to-0dp while its
                # sibling was net, which is how the chip and the rows diverged).
                gross=round(pnl, 2) if pnl is not None else None,
                charges=charges,
                pnl=round(pnl - (charges or 0.0), 2) if pnl is not None else None,
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
                        watch_source=core.watch_source.get(sym, "seed"),
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
        # `core.entered` counts TRIGGERS, not placements — on 2026-08-05 it read
        # 5 on a day with zero fills (2 unaffordable skips + 3 broker rejections).
        # Report what actually happened alongside it (issue #548).
        n_real, n_paper, n_sim = self._count_fills()
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
            filled=n_real,
            paper=n_paper,
            # triggers no order was sent for, priced at 1 lot (issue #555)
            sim=n_sim,
            # seed vs rolling split (issue #529): `selected` counts the whole
            # watch list, so without this the seed cohort is unreadable
            rolling_added=len(self.core.rolling_adds),
            day=self.day_status,
            captured_drift=drifts,
        )
        if _fill_reconcile_enabled():
            # Broker fill reconciliation (issue #555). Runs HERE and not in the
            # entry/exit paths: entries are placed from the ZMQ tick callback,
            # where a synchronous broker round-trip would stall every other
            # symbol's ticks. Legs the broker has not reported yet stay
            # `pending` and are retried by the next 09:10 arm.
            try:
                from services.open15_fill_reconcile import reconcile_fills

                res = reconcile_fills(self._trade_date())
                # one event per row FIRST, so the per-symbol table and the CSV
                # carry the transacted prices, then the roll-up
                for detail in res.pop("rows", []):
                    self._log_event("fill_reconcile_row", **detail)
                self._log_event("fill_reconcile", **res)
            except Exception:
                logger.exception("open15: fill reconciliation failed")
        if _opt_shadow_enabled():
            # ATM option shadow pricing (issue #435). Broker current-day 1m
            # history lags ~5-15 min, so rows left unpriced here are retried
            # by the next 09:10 arm's catch-up call.
            try:
                from services.open15_option_shadow import enrich_missing

                self._log_event("opt_shadow", **enrich_missing())
            except Exception:
                logger.exception("open15: option-shadow enrichment failed")
        # the contract's per-minute OI/volume path over the hold (issue #555) —
        # same 1m bars, same broker lag, so the same retry treatment. Gated
        # separately from the shadow: option-MODE rows have no shadow to price
        # but their contract's OI path is just as measurable.
        try:
            from services.open15_option_shadow import enrich_liquidity_paths

            res = enrich_liquidity_paths()
            for detail in res.pop("rows", []):
                self._log_event("liquidity_row", **detail)
            self._log_event("opt_liquidity_path", **res)
        except Exception:
            logger.exception("open15: liquidity-path enrichment failed")
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
        # Log the SEED selection the instant it finalizes — BEFORE the re-rank
        # below can append to ``core.selected`` (issue #545). This used to live
        # in ``_capture_tick``, which runs after the re-rank, so the first
        # pass's rolling adds were recorded as seed picks (and carried their
        # 09:15 open gap where their %-at-add belonged). Emitting it here also
        # means the decision log keeps its selection record when tick capture
        # is switched off — ``_capture_tick`` returns early with no writer.
        if not was_finalized and core.finalized:
            try:
                self._log_selection_event(core)
            except Exception:
                logger.exception("open15: selection event log failed")
        # additive re-rank (issue #529) — self-throttled to the configured
        # cadence, and a no-op entirely when the feature is off
        if core.rolling_enabled:
            try:
                for add in core.maybe_rerank(tick_ts):
                    self._log_event("watchlist_add", **add)
            except Exception:
                # the rolling watch list is additive instrumentation on top of
                # the measured strategy — it must never cost a seed entry
                logger.exception("open15: rolling watch-list re-rank failed")
        try:
            self._capture_tick(core, symbol, price, cumvol, tick_ts, was_finalized)
        except Exception:
            # capture is instrumentation — it must never cost an entry (#528)
            logger.exception("open15: tick capture failed")
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

    def _log_selection_event(self, core: Open15Core) -> None:
        """Record the 09:16 SEED picks in the decision log (issue #545).

        ``watch_source`` is the authoritative seed-vs-rolling split, so the
        filter here holds even if a future change reorders the caller — the
        ordering fragility it guards against is exactly what caused #545.
        """
        seed = [s for s in core.selected if core.watch_source.get(s, "seed") == "seed"]
        self._log_event(
            "selection",
            selected={s: core.selected[s] for s in seed},
            gaps_pct={s: round(core.gaps.get(s, 0) * 100, 2) for s in seed},
            # issue #456 provenance: the exact prev-close each gap divided by,
            # so a selection oddity is auditable straight from the day log
            prev_closes={s: core.prev_closes.get(s) for s in seed},
            candidates=len(core.gaps),
        )

    def _capture_tick(
        self,
        core: Open15Core,
        symbol: str,
        price: float,
        cumvol: float,
        ts: dt.datetime,
        was_finalized: bool,
    ) -> None:
        """Persist ticks for backtest replay.

        UNIVERSE mode (#528, default): every universe symbol's tick is written
        straight through for the whole processing window, so the strategy's own
        09:15-09:30 window is replayable for symbols outside the day's picks
        (e.g. testing intraday top gainers as watch candidates). No in-memory
        buffering is needed because nothing is ever discarded.

        TARGETED mode (legacy, ``OPEN15_TICK_CAPTURE_UNIVERSE=false``): before
        selection (09:15 minute) every universe symbol's ticks are buffered in
        memory; the moment selection finalizes, only the selected symbols'
        buffers are flushed to the writer and the rest are dropped. After that,
        selected-symbol ticks stream straight to the writer.
        """
        if self._tick_writer is None:
            return
        universe_mode = self._capture_universe
        if not core.finalized:
            if universe_mode:
                self._tick_writer.enqueue(symbol, price, int(cumvol), ts)
            else:
                buf = self._first_min_buffer.setdefault(symbol, [])
                if len(buf) < 3000:
                    buf.append((price, int(cumvol), ts))
            return
        if not was_finalized and not self._capture_flushed:
            # selection just finalized on this tick — flush buffers once
            self._capture_flushed = True
            if not universe_mode:
                for sym in core.selected:
                    for p, v, t in self._first_min_buffer.get(sym, []):
                        self._tick_writer.enqueue(sym, p, v, t)
            self._first_min_buffer = {}
        if universe_mode or symbol in core.selected:
            self._tick_writer.enqueue(symbol, price, int(cumvol), ts)

    # ---- broker-rejection handling (issue #548) --------------------------- #
    @staticmethod
    def _order_error(resp: dict) -> str:
        """The broker's rejection text, trimmed for storage/display.

        Falls back to a marker rather than an empty string: a rejection whose
        cause we failed to extract must still be visibly a rejection, not a
        blank cell that reads like "no reason given, probably fine".
        """
        msg = (resp or {}).get("message") or (resp or {}).get("error") or ""
        msg = str(msg).strip()
        return msg[:500] if msg else "broker rejected the order (no message returned)"

    def _count_fills(self) -> tuple[int, int, int]:
        """``(real, paper, sim)`` position counts (issues #548, #555).

        A rejected entry frees its ``max_trades`` slot — it is not a trade, and
        on 2026-08-05 three rejections consumed the whole daily cap. Paper fills
        carry their own cap so a persistently-rejecting broker (static IP, RMS
        block) cannot simulate an unbounded number of trades and leave the day
        incomparable to a normal one.

        ``sim`` rows (triggers no order was ever sent for) are counted apart from
        ``paper`` and carry their own cap: they are not evidence of a broker
        problem, so letting them consume the paper budget would mask one.
        """
        with self._lock:
            vals = list(self.positions.values())
        paper = sum(1 for p in vals if p.get("fill") == "paper")
        sim = sum(1 for p in vals if p.get("fill") == "sim")
        return len(vals) - paper - sim, paper, sim

    def _alert_rejection(self, symbol: str, qty: int, msg: str) -> None:
        """Log loudly + Telegram once per day. Never raises into the entry path."""
        logger.error(
            "[%s] open15 ENTRY REJECTED %s qty=%s — %s",
            _mode(),
            symbol,
            qty,
            msg,
        )
        today = dt.datetime.now(IST).strftime("%Y-%m-%d")
        if self._rejection_alert_date == today:
            return
        self._rejection_alert_date = today
        try:
            from services.notification_service import get_notification_service

            get_notification_service().notify(
                "open15_breakout",
                f"⚠ open15_vol_breakout [{_mode()}]: broker REJECTED entry {symbol} "
                f"qty={qty}\n{msg}\n\n"
                f"No position was taken. Today's rejected entries are recorded as "
                f"PAPER fills (simulated as if run in sandbox) so the day stays "
                f"measurable — they are excluded from realized P&L.",
            )
        except Exception:
            logger.exception("open15: rejection alert failed for %s", symbol)

    def _journal_rejection(self, action: dict, row_kw: dict, qty: int, msg: str) -> None:
        """Record a broker-rejected entry as a terminal PAPER row (issue #548).

        The row keeps ``mode`` as the run's real mode — a paper row is never
        disguised as a sandbox run — and is marked ``status='rejected'`` with
        ``fill='paper'``. It is registered in ``self.positions`` so ``flatten``
        prices the sandbox-equivalent exit at the normal exit time; beyond the
        paper cap it is journaled terminal immediately with no pricing.
        """
        from database.open15_breakout_db import insert_trade

        _real, n_paper, _n_sim = self._count_fills()
        max_trades = int((self.day_config or {}).get("max_trades") or _max_trades_default())
        priced = n_paper < max_trades
        row_id = insert_trade(
            **row_kw,
            quantity=qty,
            entry_order_id="",
            entry_status="error",
            status="rejected",
            fill="paper" if priced else "none",
            reason="entry_rejected" if priced else "entry_rejected_paper_cap",
            error_message=msg,
        )
        if priced:
            with self._lock:
                self.positions[action["symbol"]] = {
                    **action,
                    "trigger_price": action["price"],
                    "quantity": qty,
                    "row_id": row_id,
                    "status": "paper",
                    "fill": "paper",
                    **{
                        k: row_kw[k]
                        for k in ("instrument", "opt_symbol", "opt_lot_size", "opt_entry_premium")
                        if k in row_kw
                    },
                }
        self._log_event(
            "entry_rejected",
            symbol=action["symbol"],
            instrument=row_kw.get("instrument", "stock"),
            contract=row_kw.get("opt_symbol"),
            qty=qty,
            bid=row_kw.get("opt_entry_bid"),
            ask=row_kw.get("opt_entry_ask"),
            tick_size=row_kw.get("opt_tick_size"),
            entry_price=row_kw.get("opt_entry_premium") or round(action["price"], 2),
            watch_source=action.get("watch_source") or "seed",
            error=msg,
            fill="paper" if priced else "none",
            paper_capped=not priced,
            slot_released=True,
        )
        self._alert_rejection(action["symbol"], qty, msg)

    def _trade_date(self) -> str:
        """The day's ONE date key — journal row and day log must never differ.

        ``_log_date`` is stamped at arm (09:10 IST) and is what ``save_day_log``
        files the decision log under, so the journal reads it rather than
        re-deriving "today" from the clock (issue #553). Equivalent in
        production — arming precedes every entry — but it removes a second
        derivation of one fact, the same defect shape as #552. Falls back to the
        clock if the service journals before it ever armed.
        """
        return self._log_date or dt.datetime.now(IST).strftime("%Y-%m-%d")

    def _sim_context(self, action: dict, cfg: dict) -> dict:
        """Fields that let a NON-TRADED trigger be priced at 1 lot (issue #555).

        Returns ``{}`` when simulation is off or the day's sim budget is spent —
        the caller then journals exactly the bare skip row it always did.

        In option mode this resolves the contract and quotes its premium: the
        same single broker call ``_enter_option`` would have made for a real
        entry, so the tick thread does no work it was not already doing on a
        triggering symbol, and the sim cap bounds how often it happens.
        """
        if not _sim_skipped_enabled():
            return {}
        _real, _paper, n_sim = self._count_fills()
        if n_sim >= _paper_sim_max():
            return {}
        if cfg.get("instrument") != "atm_option":
            notional = cfg.get("notional") or _notional()
            return {"sim_quantity": max(int(notional / action["price"]), 1)}

        from services.open15_option_shadow import resolve_atm_option

        contract = resolve_atm_option(
            action["symbol"], action["side"], action["price"], self._trade_date()
        )
        if not contract:
            return {}
        premium, opt_volume, opt_oi = self._option_liquidity(contract["symbol"])
        if not premium:
            return {}
        return {
            "opt_symbol": contract["symbol"],
            "opt_lot_size": int(contract["lotsize"]),
            "opt_entry_premium": premium,
            "opt_entry_volume": opt_volume,
            "opt_entry_oi": opt_oi,
            # ONE lot: the minimum tradeable unit, which is what "would this have
            # paid?" means for a trade we could not afford. It also keeps rows
            # comparable across contracts whose lot sizes differ 30-fold
            # (SAIL 4700 vs HAL 150).
            "sim_quantity": int(contract["lotsize"]),
        }

    def _journal_skip(self, action: dict, reason: str, **extra) -> None:
        """Journal a trigger that did NOT place an order (cap / sizing skips).

        When ``extra`` carries a ``sim_quantity`` the row is also registered as a
        SIM position (issue #555) so ``flatten`` prices its exit at the normal
        exit time — that is what turns "skipped: unaffordable" from a dead end
        into the measurement of what the trade would have done.

        ``quantity`` stays 0: it records what was ORDERED, and nothing was.
        """
        from database.open15_breakout_db import insert_trade

        sim_qty = extra.get("sim_quantity")
        row_id = insert_trade(
            trade_date=self._trade_date(),
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
            watch_source=action.get("watch_source") or "seed",
            status="skipped",
            reason=reason,
            fill="sim" if sim_qty else None,
            **extra,
        )
        if sim_qty:
            with self._lock:
                self.positions[action["symbol"]] = {
                    **action,
                    "trigger_price": action["price"],
                    # the size the row's P&L is priced on — NOT an order quantity
                    "quantity": int(sim_qty),
                    "row_id": row_id,
                    "status": "sim",
                    "fill": "sim",
                    "sim_reason": reason,
                    # No order was ever sent, so there is nothing that could have
                    # half-reached the exchange: `flatten` must NOT run the book
                    # verification that exists for rejections. Reading the book
                    # here could only find someone else's same-symbol position
                    # and promote this row into a live square-off.
                    "no_order_attempted": True,
                    "instrument": "option" if extra.get("opt_symbol") else "stock",
                    **{
                        k: extra[k]
                        for k in ("opt_symbol", "opt_lot_size", "opt_entry_premium")
                        if k in extra
                    },
                }
        self._log_event(
            "entry_skipped",
            symbol=action["symbol"],
            reason=reason,
            watch_source=action.get("watch_source") or "seed",
            fill="sim" if sim_qty else None,
            **extra,
        )

    def _enter(self, action: dict) -> None:
        from database.open15_breakout_db import insert_trade

        cfg = self.day_config or {}
        max_trades = int(cfg.get("max_trades") or _max_trades_default())
        # only REAL fills consume the daily cap (issue #548) — a broker-rejected
        # entry is not a trade, and letting rejections eat the cap is how three
        # static-IP 403s used up the whole 2026-08-05 budget
        n_real, _n_paper, _n_sim = self._count_fills()
        if n_real >= max_trades:
            # priced at 1 lot (issue #555) so a capped day still answers "what
            # did the trades I had no room for do?" — the cap is a budget
            # decision, and its cost is only knowable if the misses are measured
            self._journal_skip(
                action,
                "max_trades_cap",
                # the journal's own vocabulary is stock/option — `atm_option` is
                # the CONFIG value and writing it here left cap-skipped rows
                # disagreeing with every other row's instrument label
                instrument="option" if cfg.get("instrument") == "atm_option" else "stock",
                **self._sim_context(action, cfg),
            )
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
        row_kw = {
            "trade_date": self._trade_date(),
            "symbol": action["symbol"],
            "side": action["side"],
            "mode": _mode(),
            "instrument": "stock",
            "gap_pct": action["gap_pct"],
            "level": action["level"],
            "baseline_vol": action["baseline_vol"],
            "cum_vol_at_trigger": action["cum_vol_at_trigger"],
            "trigger_minute": action["trigger_minute"],
            "trigger_second": action["trigger_second"],
            "trigger_price": action["price"],
            "watch_source": action.get("watch_source") or "seed",
        }
        if not ok:
            self._journal_rejection(action, row_kw, qty, self._order_error(resp))
            return
        row_id = insert_trade(
            **row_kw,
            quantity=qty,
            entry_order_id=str(resp.get("orderid") or ""),
            entry_status="success",
            status="open",
            fill="real",
        )
        with self._lock:
            self.positions[action["symbol"]] = {
                **action,
                "trigger_price": action["price"],
                "quantity": qty,
                "row_id": row_id,
                "status": "open",
                "fill": "real",
            }
        self._log_event(
            "entry",
            symbol=action["symbol"],
            side=side_word,
            watch_source=action.get("watch_source") or "seed",
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

    def _option_liquidity(self, opt_symbol: str) -> dict:
        """``{ltp, volume, oi, bid, ask}`` for an option contract (#488, #555).

        Everything but ``ltp`` is pure instrumentation — nothing gates on it.
        When the snapshot is unavailable this falls back to ``quote_fn`` so
        sizing and exit pricing behave exactly as before and the liquidity
        columns simply stay NULL. Never raises: a broken snapshot must not cost
        an entry or an exit.

        Returns a dict rather than a tuple (it was ``(ltp, volume, oi)``) so
        adding a field cannot silently mis-bind a positional unpack at a call
        site someone forgot to update.
        """
        snap = None
        try:
            if self.quote_snapshot_fn is not None:
                snap = self.quote_snapshot_fn(opt_symbol, "NFO")
        except Exception:
            logger.exception("open15: liquidity snapshot raised for %s", opt_symbol)
        if snap:
            return {
                "ltp": snap.get("ltp"),
                "volume": snap.get("volume"),
                "oi": snap.get("oi"),
                "bid": snap.get("bid"),
                "ask": snap.get("ask"),
            }
        return {
            "ltp": self.quote_fn(opt_symbol, "NFO"),
            "volume": None,
            "oi": None,
            "bid": None,
            "ask": None,
        }

    def _enter_option(self, action: dict, cfg: dict) -> None:
        """Option-mode entry (issue #437): BUY the ATM CE (L) / PE (S).

        Fit-to-capital sizing: lots = floor(slot capital / (premium x lot));
        0 lots -> journaled ``unaffordable`` skip. Both directions are premium
        BUYS — the strategy never sells options.
        """
        from database.open15_breakout_db import insert_trade
        from services.open15_option_shadow import resolve_atm_option

        today = self._trade_date()
        contract = resolve_atm_option(action["symbol"], action["side"], action["price"], today)
        if not contract:
            self._journal_skip(action, "no_option_contract", instrument="option")
            return
        liq = self._option_liquidity(contract["symbol"])
        premium, opt_volume, opt_oi = liq["ltp"], liq["volume"], liq["oi"]
        if not premium:
            self._journal_skip(
                action, "no_option_quote", instrument="option", opt_symbol=contract["symbol"]
            )
            return
        lot = int(contract["lotsize"])
        # captured at BOTH decision moments (issue #555) — free, they ride in the
        # quote response already fetched above
        liq_kw = {
            "opt_entry_bid": liq["bid"],
            "opt_entry_ask": liq["ask"],
            "opt_tick_size": contract.get("ticksize"),
        }
        slot_capital = float(cfg.get("margin_effective") or cfg.get("margin_per_slot") or 30_000)
        lots = int(slot_capital // (premium * lot))
        if lots < 1:
            # the contract and its premium are already in hand, so pricing this
            # at 1 lot (issue #555) costs one extra quote at exit and nothing
            # more — and it is the only way to learn whether the slot capital,
            # not the signal, is what is capping the strategy
            _real, _paper, n_sim = self._count_fills()
            simulate = _sim_skipped_enabled() and n_sim < _paper_sim_max()
            self._journal_skip(
                action,
                "unaffordable",
                instrument="option",
                opt_symbol=contract["symbol"],
                opt_lot_size=lot,
                opt_entry_premium=premium,
                opt_entry_volume=opt_volume,
                opt_entry_oi=opt_oi,
                **liq_kw,
                **({"sim_quantity": lot} if simulate else {}),
            )
            return
        qty = lots * lot
        resp = self.order_placer(
            _mode(),
            {"symbol": contract["symbol"], "action": "BUY", "quantity": qty, "exchange": "NFO"},
        )
        ok = resp.get("status") == "success"
        row_kw = {
            "trade_date": today,
            "symbol": action["symbol"],
            "side": action["side"],
            "mode": _mode(),
            "instrument": "option",
            "gap_pct": action["gap_pct"],
            "level": action["level"],
            "baseline_vol": action["baseline_vol"],
            "cum_vol_at_trigger": action["cum_vol_at_trigger"],
            "trigger_minute": action["trigger_minute"],
            "trigger_second": action["trigger_second"],
            "trigger_price": action["price"],
            "watch_source": action.get("watch_source") or "seed",
            "opt_symbol": contract["symbol"],
            "opt_lot_size": lot,
            "opt_entry_premium": premium,
            "opt_entry_volume": opt_volume,
            "opt_entry_oi": opt_oi,
            **liq_kw,
        }
        if not ok:
            self._journal_rejection(action, row_kw, qty, self._order_error(resp))
            return
        row_id = insert_trade(
            **row_kw,
            quantity=qty,
            entry_order_id=str(resp.get("orderid") or ""),
            entry_status="success",
            status="open",
            fill="real",
        )
        with self._lock:
            self.positions[action["symbol"]] = {
                **action,
                "trigger_price": action["price"],
                "quantity": qty,
                "row_id": row_id,
                "status": "open",
                "fill": "real",
                "instrument": "option",
                "opt_symbol": contract["symbol"],
                "opt_lot_size": lot,
                "opt_entry_premium": premium,
            }
        self._log_event(
            "entry",
            symbol=action["symbol"],
            side="BUY",
            watch_source=action.get("watch_source") or "seed",
            instrument="option",
            contract=contract["symbol"],
            lots=lots,
            lot_size=lot,
            premium=premium,
            # top of book at the entry instant (issue #555) — the MARKET order
            # crosses this, so the page can show what the fill really cost
            bid=liq["bid"],
            ask=liq["ask"],
            tick_size=contract.get("ticksize"),
            volume=opt_volume,
            oi=opt_oi,
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
            # rolling additive watch list (issue #529) — the effective config and
            # today's additions so far, readable mid-window
            "rolling": {
                "enabled": bool((self.day_config or {}).get("rolling_watchlist_enabled")),
                "cadence_s": (self.day_config or {}).get("rolling_cadence_s"),
                "top_n": (self.day_config or {}).get("rolling_top_n"),
                "adds": list(core.rolling_adds) if core else [],
            },
            "watch_source": dict(core.watch_source) if core else {},
            "positions": {
                s: {k: p.get(k) for k in ("side", "quantity", "trigger_price", "status")}
                for s, p in self.positions.items()
            },
            "tick_capture": bool(self._tick_writer),
            "tick_capture_universe": bool(self._tick_writer) and self._capture_universe,
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
