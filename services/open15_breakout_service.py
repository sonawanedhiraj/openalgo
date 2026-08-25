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


def _residual_sizing_enabled_default() -> bool:
    """Spend the cash left over after earlier fills (issue #643).

    First-boot seed only — the ``open15_config`` row wins once it is set.
    Default OFF: it changes position size, which is the one thing
    ``clamp_slots_to_funds`` was written to hold constant.
    """
    return os.getenv("OPEN15_RESIDUAL_SIZING", "false").lower() == "true"


def clamp_residual_reserve_pct(value) -> float:
    """Headroom kept back from the residual, 0..25 % (issue #643).

    Charges are not in the premium debit, and #626's lesson is that the broker
    checks the CUMULATIVE requirement — so sizing an entry at exactly the last
    rupee is how a residual entry earns a rejection.
    """
    try:
        return min(max(float(value), 0.0), 25.0)
    except (TypeError, ValueError):
        return 3.0


def clamp_residual_min_lots(value) -> int:
    """Smallest residual entry worth taking, 1..10 lots (issue #643).

    1 = take whatever the cash affords. Raise it if quarter-size rows prove to
    distort the per-trade statistics this deployment measures.
    """
    try:
        return min(max(int(value), 1), 10)
    except (TypeError, ValueError):
        return 1


def _residual_reserve_pct_default() -> float:
    return clamp_residual_reserve_pct(os.getenv("OPEN15_RESIDUAL_RESERVE_PCT", "3"))


def _residual_min_lots_default() -> int:
    return clamp_residual_min_lots(os.getenv("OPEN15_RESIDUAL_MIN_LOTS", "1"))


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


def _atm_lot_cost_enabled() -> bool:
    """Daily ATM lot-cost coverage-ladder event at arm (issue #591, observational)."""
    return os.getenv("OPEN15_ATM_LOT_COST_ENABLED", "true").lower() == "true"


_COVERAGE_TARGET_DEFAULT = 90


def clamp_coverage_target(v) -> int:
    """Coverage-target % for the lot-cost ladder, clamped 50..100 (issue #591)."""
    try:
        return max(50, min(int(float(v)), 100))
    except (TypeError, ValueError):
        return _COVERAGE_TARGET_DEFAULT


def _coverage_target_default() -> int:
    return clamp_coverage_target(os.getenv("OPEN15_COVERAGE_TARGET_PCT", "90"))


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


# Shadow-log the side ``trade_side`` excludes (issue #581). OFF by default —
# merging this is a no-op on a running install until the operator enables it
# from /open15_vol_breakout/logs.
_SHADOW_MAX_TRADES_MAX = 10


def clamp_shadow_max_trades(value) -> int:
    """Clamp the daily shadow-row cap into 0..10. Bad input -> 3.

    Zero is a legal value and means "shadow nothing" — it is the same as the
    feature being off, expressed as a budget. Clamped server-side for the same
    reason the rolling cadence is: each shadow row spends one broker quote at
    entry and another at exit on the tick thread, so the cap is what bounds the
    work a bad POST can schedule there.
    """
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return 3
    return max(0, min(v, _SHADOW_MAX_TRADES_MAX))


def _shadow_excluded_side_default() -> bool:
    return os.getenv("OPEN15_SHADOW_EXCLUDED_SIDE", "false").lower() == "true"


def _shadow_max_trades_default() -> int:
    return clamp_shadow_max_trades(os.getenv("OPEN15_SHADOW_MAX_TRADES", "3"))


def shadow_side_for(trade_side: str, enabled: bool) -> str | None:
    """The side letter that is watched but NEVER traded, or ``None``.

    ``both`` has no excluded side, so shadowing is meaningless there and the
    answer is ``None`` however the flag is set — that is what the UI greys the
    checkbox out for, expressed where it is load-bearing rather than in the
    form.
    """
    if not enabled:
        return None
    return {"long_only": "S", "short_only": "L"}.get(trade_side)


# ---- option-liquidity gate (issue #583) ---------------------------------- #
# Defaults come from the measured distribution, not from a guess: on 20-day medians
# for 2026-08-07, p20 excludes 33 of 208 CE names. The re-entry band exists because a
# name sitting on the threshold would otherwise flap in and out on alternate days.


def clamp_pctile(raw, default: float) -> float:
    """0..100. The UI number input is a hint, never a trust boundary."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return min(100.0, max(0.0, v))


def clamp_days(raw, default: int, lo: int = 0, hi: int = 120) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, v))


def _liq_gate_enabled_default() -> bool:
    """OFF by default — the placebo failed it (2026-08-09).

    Replayed against the R60 July backtest, restricted to option-leg trades so the
    comparison is fair, excluding the real bottom quintile was **indistinguishable
    from excluding the same number of symbols at random** in both arms (placebo >=
    real 48.4% and 51.8%). Worse, in the larger arm the excluded trades averaged
    +Rs 834 against +Rs 743 for the kept ones — the gate removes ABOVE-average
    trades, which is #488's inversion showing up again on a bigger sample.

    The scoring stays on and stays logged (``universe_excluded`` with
    ``enforced=false``), because the measurement is cheap, harmless, and the only
    thing that can eventually overturn this. Enforcement waits for evidence.
    """
    return os.getenv("OPEN15_LIQUIDITY_GATE_ENABLED", "false").lower() == "true"


def _liq_min_pctile_default() -> float:
    return clamp_pctile(os.getenv("OPEN15_LIQUIDITY_MIN_PCTILE", "20"), 20.0)


def _liq_reentry_pctile_default() -> float:
    return clamp_pctile(os.getenv("OPEN15_LIQUIDITY_REENTRY_PCTILE", "25"), 25.0)


def _liq_reentry_days_default() -> int:
    return clamp_days(os.getenv("OPEN15_LIQUIDITY_REENTRY_DAYS", "3"), 3, 1, 30)


def _liq_min_days_default() -> int:
    return clamp_days(os.getenv("OPEN15_LIQUIDITY_MIN_DAYS", "10"), 10, 1, 120)


def _liq_max_staleness_default() -> int:
    return clamp_days(os.getenv("OPEN15_LIQUIDITY_MAX_STALENESS_DAYS", "3"), 3, 0, 60)


def _liq_backfill_rank_default() -> bool:
    return os.getenv("OPEN15_LIQUIDITY_BACKFILL_RANK", "true").lower() == "true"


def clamp_min_oi_lots(raw) -> int:
    """0..5000 lots; 0 disables the check. The UI number input is a hint,
    never a trust boundary."""
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return 500
    return min(5000, max(0, v))


def _min_oi_lots_default() -> int:
    """Zerodha blocks MIS orders on stock option contracts whose OI is below
    500 LOTS (issue #595 — on 2026-08-13 it rejected 4 of 5 entries this way,
    and ``oi/lot_size < 500`` separated every rejection from the one fill).
    The default mirrors the broker's rule exactly; raise it for headroom
    against the snapshot-timing skew between our quote read and their list.
    """
    return clamp_min_oi_lots(os.getenv("OPEN15_MIN_OI_LOTS", "500"))


def _impact_gate_enabled_default() -> bool:
    return os.getenv("OPEN15_IMPACT_GATE_ENABLED", "true").lower() == "true"


def _impact_max_pct_default() -> float:
    """SEBI's Liquidity Enhancement Scheme calls a security illiquid at a mean impact
    cost >= 2% for a Rs 1 lakh order. Same shape, our slot size."""
    return clamp_pctile(os.getenv("OPEN15_IMPACT_MAX_PCT", "2.0"), 2.0)


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


def clamp_slots_to_funds(
    margin_per_slot: float, max_trades: int, available_cash: float | None
) -> tuple[int, str | None]:
    """How many slots the account can actually pay for (issue #626).

    Returns ``(effective_max_trades, note)``; ``note`` is None when nothing was
    clamped.

    The strategy's budget was `margin_per_slot x max_trades` and was never
    compared with the money in the account. On 2026-08-18 that budget was
    5 x Rs60,000 = Rs3,00,000 against Rs1,22,252.80 of cash: the first two
    entries filled, the third asked for Rs62,000 the account did not have, and
    Zerodha refused it — "Margin required: 149255.00" being the CUMULATIVE
    requirement of all three, not the third alone.

    **`max_trades` is what shrinks, never `margin_per_slot`.** Cutting the slot
    instead would silently change the position size this deployment exists to
    measure, making the day incomparable to every day before it. Fewer trades at
    the configured size stays comparable; smaller trades do not.

    Fails OPEN (returns `max_trades` unchanged) when the balance is unknown. An
    unreadable funds call must not be able to switch the strategy off — the
    broker still enforces the real limit, and a rejection is now handled
    correctly rather than published as a fill.
    """
    if available_cash is None or margin_per_slot <= 0:
        return max_trades, None
    affordable = int(available_cash // margin_per_slot)
    if affordable >= max_trades:
        return max_trades, None
    note = (
        f"funds clamp: Rs{available_cash:,.0f} available covers {affordable} of "
        f"{max_trades} slots at Rs{margin_per_slot:,.0f} each"
    )
    return max(affordable, 0), note


def resolve_entry_budget(
    slot_capital: float, cash_remaining: float | None, reserve_pct: float
) -> tuple[float, str]:
    """What this ONE entry may spend, and what that size is derived from (#643).

    Returns ``(budget, basis)`` where ``basis`` is ``"slot"`` or ``"residual"``.

    The full slot is always preferred: residual sizing exists to spend money the
    account genuinely has left, not to shrink trades. On 2026-08-19 two fills
    consumed Rs1,21,635 of Rs1,61,365 and the remaining Rs39,730 — two lots of
    the third signal's contract — was simply not spent, because
    ``clamp_slots_to_funds`` had already cut the day to 2 slots.

    **Fails OPEN to the full slot** when the balance is unknown (``None``): the
    ledger not knowing must never silently halve a position. The broker is the
    backstop, and a rejection is journaled correctly since #548/#626.

    ``reserve_pct`` is held back from the residual only. A full slot is a
    configured number the operator chose; the residual is our own arithmetic
    against a balance that also has to cover brokerage and STT.
    """
    if cash_remaining is None:
        return slot_capital, "slot"
    usable = max(cash_remaining, 0.0) * (1.0 - min(max(reserve_pct, 0.0), 100.0) / 100.0)
    if usable >= slot_capital:
        return slot_capital, "slot"
    return max(usable, 0.0), "residual"


def read_available_cash() -> float | None:
    """The spendable cash OF THE BOOK THIS STRATEGY TRADES (issue #626).

    Routed by ``resolve_order_mode(STRATEGY_NAME)`` — the same resolver that
    routes the orders — which is the #497 rule. ``funds_service.get_funds``
    cannot be used directly here: it dispatches on ``resolve_effective_mode()``,
    the *analyze overlay*, which returns LIVE whenever the navbar toggle is off
    regardless of what this strategy is set to. A sandbox open15 would then size
    itself against the REAL broker balance instead of the virtual Rs1Cr book and
    silently clamp a measurement run that has no funding constraint at all.

    None means "we do not know" and is never coerced to 0 — a zero would clamp
    the strategy to no trades on a transient funds-API failure.
    """
    try:
        from database.auth_db import get_first_available_api_key
        from services.mode_service import EffectiveMode, resolve_order_mode

        api_key = get_first_available_api_key()
        if not api_key:
            return None

        if resolve_order_mode(STRATEGY_NAME) is EffectiveMode.SANDBOX:
            from services.sandbox_service import sandbox_get_funds

            ok, resp, _ = sandbox_get_funds(api_key, {"apikey": api_key})
        else:
            from database.auth_db import get_auth_token_broker
            from services.funds_service import get_funds_with_auth

            auth_token, broker = get_auth_token_broker(api_key)
            if not auth_token:
                return None
            # no ``original_data`` on purpose: that argument is what makes
            # get_funds_with_auth consult the analyze overlay, and the routing
            # decision has already been made above by the correct resolver
            ok, resp, _ = get_funds_with_auth(auth_token, broker)

        if not ok:
            logger.warning("open15: funds read failed — sizing not clamped: %s", resp)
            return None
        cash = (resp or {}).get("data", {}).get("availablecash")
        return float(cash) if cash is not None else None
    except Exception:
        logger.exception("open15: funds read raised — sizing not clamped")
        return None


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
    # shadow-log the excluded side (issue #581) — same ``is None`` treatment as
    # the rolling flag, so an explicit stored ``false`` beats a ``true`` env
    shadow_cfg = cfg.get("shadow_excluded_side")
    shadow_enabled = _shadow_excluded_side_default() if shadow_cfg is None else bool(shadow_cfg)
    shadow_max_cfg = cfg.get("shadow_max_trades")
    shadow_max_trades = (
        _shadow_max_trades_default()
        if shadow_max_cfg is None
        else clamp_shadow_max_trades(shadow_max_cfg)
    )
    # residual-cash sizing (issue #643) — ``is None`` again, so a stored false
    # beats a true env seed and vice versa
    residual_cfg = cfg.get("residual_sizing_enabled")
    residual_enabled = (
        _residual_sizing_enabled_default() if residual_cfg is None else bool(residual_cfg)
    )
    residual_reserve_cfg = cfg.get("residual_reserve_pct")
    residual_reserve_pct = (
        _residual_reserve_pct_default()
        if residual_reserve_cfg is None
        else clamp_residual_reserve_pct(residual_reserve_cfg)
    )
    residual_min_lots_cfg = cfg.get("residual_min_lots")
    residual_min_lots = (
        _residual_min_lots_default()
        if residual_min_lots_cfg is None
        else clamp_residual_min_lots(residual_min_lots_cfg)
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
        "shadow_excluded_side": shadow_enabled,
        "shadow_max_trades": shadow_max_trades,
        # ---- residual-cash sizing (issue #643) -----------------------------
        "residual_sizing_enabled": residual_enabled,
        "residual_reserve_pct": residual_reserve_pct,
        "residual_min_lots": residual_min_lots,
        # derived, so exactly ONE place decides which side is shadow-only and
        # every consumer (core, entry branch, day log, UI) reads that one answer
        "shadow_side": shadow_side_for(trade_side, shadow_enabled),
        # ---- option-liquidity gates (issue #583) --------------------------
        # ``is None`` rather than ``or``, so a stored false/0 beats a true env
        # default instead of being silently overridden by it.
        "option_liquidity_gate_enabled": (
            _liq_gate_enabled_default()
            if cfg.get("option_liquidity_gate_enabled") is None
            else bool(cfg.get("option_liquidity_gate_enabled"))
        ),
        "option_liquidity_min_pctile": (
            _liq_min_pctile_default()
            if cfg.get("option_liquidity_min_pctile") is None
            else clamp_pctile(cfg.get("option_liquidity_min_pctile"), _liq_min_pctile_default())
        ),
        "option_liquidity_reentry_pctile": (
            _liq_reentry_pctile_default()
            if cfg.get("option_liquidity_reentry_pctile") is None
            else clamp_pctile(
                cfg.get("option_liquidity_reentry_pctile"), _liq_reentry_pctile_default()
            )
        ),
        "option_liquidity_reentry_days": (
            _liq_reentry_days_default()
            if cfg.get("option_liquidity_reentry_days") is None
            else clamp_days(cfg.get("option_liquidity_reentry_days"), 3, 1, 30)
        ),
        "option_liquidity_min_days": (
            _liq_min_days_default()
            if cfg.get("option_liquidity_min_days") is None
            else clamp_days(cfg.get("option_liquidity_min_days"), 10, 1, 120)
        ),
        "option_liquidity_max_staleness_days": (
            _liq_max_staleness_default()
            if cfg.get("option_liquidity_max_staleness_days") is None
            else clamp_days(cfg.get("option_liquidity_max_staleness_days"), 3, 0, 60)
        ),
        "option_liquidity_backfill_rank": (
            _liq_backfill_rank_default()
            if cfg.get("option_liquidity_backfill_rank") is None
            else bool(cfg.get("option_liquidity_backfill_rank"))
        ),
        "option_impact_gate_enabled": (
            _impact_gate_enabled_default()
            if cfg.get("option_impact_gate_enabled") is None
            else bool(cfg.get("option_impact_gate_enabled"))
        ),
        "option_impact_max_pct": (
            _impact_max_pct_default()
            if cfg.get("option_impact_max_pct") is None
            else clamp_pctile(cfg.get("option_impact_max_pct"), _impact_max_pct_default())
        ),
        # broker OI floor (issue #595) — Zerodha's own per-CONTRACT MIS rule
        # (OI >= 500 lots), mirrored at watch-list construction so a name that
        # can never fill does not occupy a seed/rolling slot. 0 = off.
        "option_min_oi_lots": (
            _min_oi_lots_default()
            if cfg.get("option_min_oi_lots") is None
            else clamp_min_oi_lots(cfg.get("option_min_oi_lots"))
        ),
        # ATM lot-cost coverage ladder (issue #591) — the "most of the universe"
        # the operator wants covered, as a percentile of priced names
        "coverage_target_pct": (
            _coverage_target_default()
            if cfg.get("coverage_target_pct") is None
            else clamp_coverage_target(cfg.get("coverage_target_pct"))
        ),
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
        shadow_side: str | None = None,
        liquidity_gate=None,
        liquidity_backfill_rank: bool = True,
        oi_filter_fn=None,
    ):
        self.prev_closes = prev_closes
        # Gate 1 stage 2 (issue #583): the per-SIDE option-liquidity check, applied
        # the moment a side is assigned. There are TWO such moments — the 09:16 gap
        # ranking and every rolling addition — and both are patched, because
        # ``maybe_rerank`` assigns sides independently of ``_finalize_selection``.
        # The core still decides nothing about orders; it drops a symbol from the
        # WATCH list and records why.
        self.liquidity_gate = liquidity_gate
        self.liquidity_backfill_rank = bool(liquidity_backfill_rank)
        self.liquidity_exclusions: list[dict[str, Any]] = []
        # Broker-OI mirror (issue #595): a service-injected BATCH callable —
        # ``fn(candidates) -> {(symbol, side): verdict}`` — consulted at the same
        # two moments as the gate above. Injected rather than imported so the
        # core stays unit-testable; ``None`` (stock mode / disabled) checks
        # nothing. Applied to shadow candidates too (operator decision, #595):
        # a shadow fill on a contract the broker would block is unrealizable
        # P&L, which is exactly what the #581 cohort must not accumulate.
        self.oi_filter_fn = oi_filter_fn
        # (symbol, side) -> verdict, cached for the day so a persistently-thin
        # name is quoted once, not on every 30s rolling pass
        self.oi_verdicts: dict[tuple[str, str], dict] = {}
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
        # ...unless it is being SHADOWED (issue #581): the excluded side is then
        # watched and triggered normally, and every action it produces carries
        # ``shadow=True``. The core stays pure — it decides nothing about
        # orders; it only labels the action so the service can branch before it
        # ever reaches ``order_placer``. Shadowing a side the config does not
        # exclude is meaningless, so it is rejected here rather than trusted.
        self.shadow_side = (
            shadow_side if shadow_side == shadow_side_for(self.trade_side, True) else None
        )
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
        # issue #677: selection can finalize from TWO threads — the ZMQ tick
        # path and the scheduler's 09:17 deadline — so the guard is a lock,
        # not a bare flag check.
        self._finalize_lock = threading.Lock()

    def ensure_finalized(self) -> bool:
        """Finalize selection exactly once, from whichever path gets here first.

        Issue #677: the tick-path "hard fail-open deadline" needs a tick to
        run, so a totally dead feed silently skipped the day (2026-08-25 —
        zero seeds, no alert, and when ticks resumed at 09:25 the selection
        only started then). The scheduler's minute job now calls this at/after
        09:17, so the watch list exists with or without ticks. Returns True
        when THIS call performed the finalize.
        """
        with self._finalize_lock:
            if self.finalized:
                return False
            self._finalize_selection()
            return True

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

    def _watches(self, side: str) -> bool:
        """Is ``side`` on the watch list at all — to trade OR to shadow (#581)?

        Selection and the rolling re-rank both ask this, so the traded-vs-
        shadowed distinction lives in exactly one predicate. Whether a watched
        symbol produces an ORDER is not decided here (see ``is_shadow``): the
        core is pure and never places anything.
        """
        if side == self.shadow_side:
            return True
        return self.trade_side != ("short_only" if side == "L" else "long_only")

    def is_shadow(self, side: str) -> bool:
        """True when ``side`` is watched for measurement only (issue #581)."""
        return self.shadow_side is not None and side == self.shadow_side

    def _side_excluded(self, symbol: str, side: str, watch_source: str) -> bool:
        """Gate-1 stage 2. Records the exclusion and returns True when it fires."""
        if self.liquidity_gate is None:
            return False
        try:
            # ``would_exclude`` ignores the enabled flag so a DISABLED gate still
            # RECORDS its verdict; only the return value below respects it. The
            # placebo failed (2026-08-09), so the gate measures rather than acts.
            ex = self.liquidity_gate.would_exclude(symbol, side)
            enforced = bool(getattr(self.liquidity_gate, "enabled", False))
        except Exception:
            # a broken gate must never cost a selection — fail OPEN
            logger.exception("open15: liquidity gate raised for %s/%s", symbol, side)
            return False
        if ex is None:
            return False
        rec = ex.as_event()
        rec["watch_source"] = watch_source
        rec["stage"] = 2
        rec["enforced"] = enforced
        self.liquidity_exclusions.append(rec)
        return enforced

    def _candidate_spot(self, symbol: str) -> float | None:
        """Best current price for ATM-strike resolution: last tick, else the
        09:15 candle. ``None`` means the OI check cannot run — fail open."""
        px = self.last_price.get(symbol)
        if px:
            return px
        fc = self.first_candle(symbol)
        if fc:
            return fc.get("close") or fc.get("open")
        return None

    def _prefetch_oi_verdicts(self, pairs: list[tuple[str, str]]) -> None:
        """One batched broker call for every (symbol, side) not yet judged (#595).

        Verdicts cache for the day. A failed batch caches NOTHING, so the next
        selection moment retries — transient broker trouble heals instead of
        silently waving the whole day through unchecked.
        """
        if self.oi_filter_fn is None:
            return
        candidates = []
        for sym, side in pairs:
            if (sym, side) in self.oi_verdicts:
                continue
            spot = self._candidate_spot(sym)
            if spot:
                candidates.append({"symbol": sym, "side": side, "spot": spot})
        if not candidates:
            return
        try:
            verdicts = self.oi_filter_fn(candidates) or {}
        except Exception:
            # a broken OI check must never cost a selection — fail OPEN (#390)
            logger.exception("open15: oi filter raised — OI check skipped this pass")
            return
        for key, v in verdicts.items():
            if isinstance(key, tuple) and isinstance(v, dict):
                self.oi_verdicts[key] = v

    def _oi_blocked(self, symbol: str, side: str, watch_source: str) -> bool:
        """Consume the prefetched verdict; record the exclusion when it fires.

        No verdict = fail open. Always-promote (no ``liquidity_backfill_rank``
        branch): an OI-blocked contract cannot fill under ANY variant of the
        strategy, so leaving its slot empty would measure nothing — unlike the
        percentile gate, where promoting #4 is arguably a different signal.
        """
        v = self.oi_verdicts.get((symbol, side))
        if not v or not v.get("blocked"):
            return False
        self.liquidity_exclusions.append(
            {
                "symbol": symbol,
                "reason": "oi_below_broker_min",
                "side": "long" if side == "L" else "short",
                "oi_lots": v.get("oi_lots"),
                "min_lots": v.get("min_lots"),
                "opt_symbol": v.get("opt_symbol"),
                "watch_source": watch_source,
                "stage": 3,
                "enforced": True,
            }
        )
        return True

    def _take_top_n(self, ranked: list[str], side: str) -> list[str]:
        """The first ``top_n`` names on ``ranked`` that clear the per-side gate.

        With ``liquidity_backfill_rank`` on (the default) an excluded name is replaced
        by the next candidate, so a funded slot is not silently left empty; with it off
        the slot is simply forgone. Promoting #4 IS a different signal from #3, which
        is why it is a flag rather than a decision baked in here.
        """
        out: list[str] = []
        for s in ranked:
            if len(out) >= self.top_n:
                break
            if self._side_excluded(s, side, "seed"):
                if not self.liquidity_backfill_rank:
                    # consume the slot without filling it
                    out.append(None)  # type: ignore[arg-type]
                continue
            if self._oi_blocked(s, side, "seed"):
                continue
            out.append(s)
        return [s for s in out if s is not None]

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
        # broker-OI mirror (issue #595): judge the whole candidate pool in ONE
        # batched call, so a blocked name's backfill promotion needs no second
        # round trip. Pool = top_n + 5 per side; anything ranked deeper than
        # that arrives unjudged and fails open.
        pool: list[tuple[str, str]] = []
        if self._watches("L"):
            pool += [(s, "L") for s in pos[: self.top_n + 5]]
        if self._watches("S"):
            pool += [(s, "S") for s in neg[: self.top_n + 5]]
        self._prefetch_oi_verdicts(pool)
        if self._watches("L"):
            for s in self._take_top_n(pos, "L"):
                self.selected[s] = "L"
        if self._watches("S"):
            for s in self._take_top_n(neg, "S"):
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
          - ``trade_side`` is honoured, so an excluded side is never added —
            unless it is being shadowed (issue #581), in which case it is added
            and every action it yields is labelled ``shadow`` so the service
            journals it without placing an order;
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
        if self._watches("L"):
            sides.append(("L", sorted((s for s in pct if pct[s] > 0), key=lambda s: -pct[s])))
        if self._watches("S"):
            sides.append(("S", sorted((s for s in pct if pct[s] < 0), key=lambda s: pct[s])))
        pending: list[tuple[str, str, int]] = []
        for side, ranked in sides:
            for rank, sym in enumerate(ranked[: self.rolling_top_n], start=1):
                if sym in self.selected or self.first_candle(sym) is None:
                    continue
                # Gate-1 stage 2 on the ROLLING path (issue #583). This assigns a
                # side at ``:941`` independently of ``_finalize_selection``, so a
                # check placed only there would leave half the watch list ungated.
                # No backfill here, matching the slice above and the existing
                # first_candle skip: the rolling list is additive instrumentation
                # with its own cap, not a funded max_trades slot.
                if self._side_excluded(sym, side, "rolling"):
                    continue
                pending.append((sym, side, rank))
        # broker-OI mirror (issue #595): one batched call covers everything this
        # pass wants to add; day-cached verdicts make repeat passes free.
        self._prefetch_oi_verdicts([(s, sd) for s, sd, _ in pending])
        for sym, side, rank in pending:
            if self._oi_blocked(sym, side, "rolling"):
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
                # so the UI panel can say which additions can never trade
                "shadow": self.is_shadow(side),
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
                self.ensure_finalized()

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
                # measurement-only side (issue #581). The core still emits a
                # normal action — identical trigger, identical legality — and
                # the service is what refuses to send an order for it. Keeping
                # the gate out here is deliberate: the shadow cohort is only
                # comparable to the traded one if it was decided identically.
                "shadow": self.is_shadow(side),
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


def production_oi_filter(candidates: list[dict], min_lots: int, trade_date: str) -> dict:
    """Broker-OI verdicts for watch-list candidates (issue #595), ONE batched call.

    Zerodha blocks MIS orders on stock option contracts with OI < 500 LOTS —
    a per-contract, absolute rule that no per-name percentile can reproduce
    (2026-08-13: KALYANKJIL at p96 was blocked; its 605CE held 433 lots).
    This mirrors that rule at the only moment slots are allocated.

    ``candidates`` are ``{"symbol", "side" ("L"/"S"), "spot"}``; returns
    ``{(symbol, side): {"blocked", "oi_lots", "opt_symbol", "min_lots"}}``.
    Fail-open contract, three ways: a candidate whose ATM contract cannot be
    resolved gets NO verdict (gate-1's no-contract check owns that fact); a
    resolved contract whose quote is missing or reports ``oi`` of 0/None is
    "unknown", never "thin" — the Zerodha mapper defaults absent fields to 0
    (#555); and any raised failure returns ``{}`` so a broken batch call can
    never dark the seed list (#390). The broker's own rejection + the #548
    paper path remain the backstop for whatever slips through.
    """
    from services.open15_option_shadow import resolve_atm_option

    resolved: dict[tuple[str, str], dict] = {}
    for c in candidates:
        try:
            contract = resolve_atm_option(c["symbol"], c["side"], c["spot"], trade_date)
        except Exception:
            logger.exception("open15 oi-filter: contract resolve raised for %s", c.get("symbol"))
            contract = None
        if contract and contract.get("lotsize"):
            resolved[(c["symbol"], c["side"])] = contract
    if not resolved:
        return {}
    try:
        from database.auth_db import get_first_available_api_key
        from services.quotes_service import get_multiquotes

        api_key = get_first_available_api_key()
        if not api_key:
            logger.warning("open15 oi-filter: no api key — OI check FAILS OPEN")
            return {}
        payload = [{"symbol": ct["symbol"], "exchange": "NFO"} for ct in resolved.values()]
        ok, data, _status = get_multiquotes(payload, api_key=api_key)
        if not ok:
            logger.warning("open15 oi-filter: batch quote failed — OI check FAILS OPEN")
            return {}
        quotes = {
            r.get("symbol"): (r.get("data") or {})
            for r in ((data or {}).get("results") or [])
            if isinstance(r, dict)
        }
    except Exception:
        logger.exception("open15 oi-filter: batch quote raised — OI check FAILS OPEN")
        return {}
    out: dict[tuple[str, str], dict] = {}
    for key, ct in resolved.items():
        oi = _as_int((quotes.get(ct["symbol"]) or {}).get("oi"))
        lot = int(ct["lotsize"])
        oi_lots = (oi / lot) if oi and lot else None  # 0/None = unknown, never "thin"
        out[key] = {
            "blocked": bool(oi_lots is not None and oi_lots < min_lots),
            "oi_lots": round(oi_lots, 1) if oi_lots is not None else None,
            "opt_symbol": ct["symbol"],
            "min_lots": min_lots,
        }
    return out


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
        # ---- cash ledger (issue #643) --------------------------------------
        # ``_cash_at_arm`` is the broker balance read once at 09:10; the per-
        # symbol reservations are what entries have committed of it. Remaining
        # cash = arm balance - sum(reservations), so sizing never has to make a
        # broker call on the ZMQ tick thread.
        self._cash_at_arm: float | None = None
        self._cash_reserved: dict[str, float] = {}
        self._cash_refetched = False
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
        # separate dedup for an entry that RAISED (issue #643) — a code fault
        # must not be silenced by an earlier broker rejection
        self._entry_error_alert_date: str | None = None
        # ---- feed health (issue #677) --------------------------------------
        # Written ONLY by the ZMQ tick thread (single writer, cheap ops);
        # read by the scheduler minute job and get_status. `_feed_state` is
        # the last state the scheduler *reported* — None until the first
        # non-ok observation so a normal day journals nothing extra.
        self._feed_ticks = 0
        self._feed_symbols: set[str] = set()
        self._feed_last_tick: dt.datetime | None = None
        self._feed_state: str | None = None
        self._feed_alert_date: str | None = None
        self._selection_source: str | None = None

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

    def _drain_liquidity_exclusions(self, core) -> None:
        """Emit and clear whatever stage 2 recorded. Never raises."""
        try:
            pending, core.liquidity_exclusions = core.liquidity_exclusions, []
            for rec in pending:
                self._log_event("universe_excluded", **rec)
        except Exception:
            logger.exception("open15: liquidity exclusion logging failed")

    def _apply_fno_filter(self, prev: dict) -> None:
        """Drop watched symbols that have no NFO option contracts (issue #647).

        Option mode only — in stock mode there is nothing to require, and
        shrinking the universe there would silently change what the strategy
        watches.

        Mutates ``self.universe`` in place, which is what makes the exclusion
        total: ``_handle_raw`` discards any tick whose symbol is not in the
        universe, so a dropped name never reaches ``core.last_price`` and the
        rolling re-rank can never see it either. One check here covers seed AND
        rolling, which is why no per-stage check is needed anywhere else.

        ``prev`` is pruned alongside so the ``armed`` event's ``universe`` and
        ``prev_closes`` counts describe the same set of symbols.

        Fail-open is delegated to ``filter_to_fno``: on any degraded read the
        universe is returned untouched and nothing is dropped.
        """
        if self.day_config.get("instrument") != "atm_option":
            return
        try:
            from services.fno_universe import filter_to_fno

            kept, dropped, degraded = filter_to_fno(self.universe)
        except Exception:
            logger.exception("open15: F&O universe filter raised — universe untouched")
            return
        if degraded or not dropped:
            return
        self.universe = kept
        for sym in dropped:
            prev.pop(sym, None)
        # NOT an illiquidity verdict: SCANNER_SYMBOLS has drifted from the master
        # contract. Enforced regardless, and the operator is told so they can
        # prune the list (or let #648 do it for every consumer).
        logger.error(
            "open15: %d watched symbols have NO NFO option contracts and were "
            "DROPPED from today's universe — SCANNER_SYMBOLS is stale: %s",
            len(dropped),
            ", ".join(dropped),
        )
        self._log_event(
            "universe_excluded",
            stage=0,
            reason="not_in_fno",
            n_excluded=len(dropped),
            n_watched=len(kept),
            enforced=True,
            symbols=[{"symbol": s, "reason": "not_in_fno", "side": "both"} for s in dropped],
        )

    def _apply_liquidity_stage1(self, today: dt.date):
        """Gate 1 stage 1 — drop symbols that fail on BOTH option sides.

        Returns ``(gate, excluded_records)``. Mutates ``self.universe`` in place, which
        is what makes the exclusion total: a dropped symbol is discarded by the tick
        gate in ``_handle_raw``, so it never reaches ``core.last_price`` and the rolling
        re-rank can never see it either. One check here covers seed AND rolling.

        Never raises. Every failure path leaves the universe untouched — a broken gate
        must not cost a trading day.
        """
        excluded: list[dict] = []
        if self.day_config.get("instrument") != "atm_option":
            return None, excluded
        try:
            from services.open15_liquidity_gate import build_gate

            gate = build_gate(self.day_config, today)
        except Exception:
            logger.exception("open15: liquidity gate build failed — gate OFF for the day")
            return None, excluded
        keep: set[str] = set()
        for sym in sorted(self.universe):
            try:
                # ``would_exclude`` ignores the enabled flag, so a DISABLED gate still
                # records its verdict. The placebo failed on 2026-08-09, so the gate
                # ships measuring rather than acting — and switching a rule off must
                # not switch off the data that could overturn it.
                ex = gate.would_exclude(sym)
            except Exception:
                logger.exception("open15: stage-1 gate raised for %s — keeping it", sym)
                ex = None
            if ex is None or not gate.enabled:
                keep.add(sym)
            if ex is not None:
                rec = ex.as_event()
                rec["stage"] = 1
                rec["enforced"] = bool(gate.enabled)
                excluded.append(rec)
        if excluded:
            self.universe = keep
            self._log_event(
                "universe_excluded",
                stage=1,
                n_excluded=len(excluded),
                n_watched=len(keep),
                min_pctile=self.day_config["option_liquidity_min_pctile"],
                enforced=bool(gate.enabled),
                symbols=excluded,
            )
            no_contracts = [e["symbol"] for e in excluded if e["reason"] == "no_option_contracts"]
            if no_contracts:
                # NOT an illiquidity verdict: SCANNER_SYMBOLS has drifted from the
                # master contract and should be corrected by an operator.
                logger.error(
                    "open15: %d watched symbols have NO NFO option contracts — "
                    "SCANNER_SYMBOLS is stale: %s",
                    len(no_contracts),
                    ", ".join(no_contracts),
                )
        return gate, excluded

    def _build_oi_filter(self, now: dt.datetime):
        """The #595 broker-OI mirror for the core, or ``None`` (= no check).

        Option mode only — Zerodha's 500-lot MIS rule is about option
        contracts, so in stock mode there is nothing to mirror. A floor of 0
        is the operator's off switch.
        """
        if self.day_config.get("instrument") != "atm_option":
            return None
        try:
            min_lots = int(self.day_config.get("option_min_oi_lots") or 0)
        except (TypeError, ValueError):
            min_lots = 0
        if min_lots <= 0:
            return None
        trade_date = now.strftime("%Y-%m-%d")
        return lambda candidates: production_oi_filter(candidates, min_lots, trade_date)

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
        now = self._now_ist()
        self._log_date = now.strftime("%Y-%m-%d")
        self._first_min_buffer = {}
        self._capture_flushed = False
        if now.time() >= dt.time(9, 15, 30):
            self.day_status = "skipped_late_boot"
            # issue #597: a mid-day RESTART on a date that already has a
            # persisted day log (the day traded before the restart) must not
            # replace that log with a lone skip event — load it and append a
            # restart marker instead, so the real selections/fills survive.
            existing = self._load_persisted_day_log(self._log_date)
            if existing:
                self.day_log = existing
                self._log_event(
                    "late_boot_restart",
                    armed_at=now.strftime("%H:%M:%S"),
                    preserved_events=len(existing),
                )
                logger.warning(
                    "open15: re-armed at %s after 09:15 IST — day log for %s already "
                    "has %d events; preserved (restart marker appended). This process "
                    "will not trade today.",
                    now.strftime("%H:%M:%S"),
                    self._log_date,
                    len(existing),
                )
                return
            self.day_log = []
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
        self.day_log = []
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
        # The budget must fit the ACCOUNT, not just the config (issue #626).
        # Done here rather than in resolve_day_config because it needs a broker
        # read and that function is pure. Once per day, at arm.
        configured_max = self.day_config["max_trades"]
        residual_sizing = bool(self.day_config.get("residual_sizing_enabled"))
        # ALWAYS read the balance (issue #651): the clamp is not optional. It was
        # briefly flag-gated as a rollback switch for #626, which made "size the
        # day without ever looking at the account" a setting — the exact state
        # that put 5 x Rs60,000 of orders against Rs1,22,252 on 2026-08-18.
        available_cash = read_available_cash()
        eff_max, clamp_note = clamp_slots_to_funds(
            self.day_config["margin_effective"], configured_max, available_cash
        )
        if clamp_note:
            logger.warning("open15: %s", clamp_note)
            # With residual sizing ON the slot count is no longer the budget —
            # the CASH is (issue #643). Dropping the whole third trade because it
            # cannot afford a full slot is what left Rs39,730 idle on 2026-08-19.
            # The note is still computed and logged: it is what the /logs capital
            # card reports, and it is the rollback path when the feature is off.
            if not residual_sizing:
                self.day_config["max_trades"] = eff_max
        # seed the cash ledger for the day (issue #643)
        self._cash_at_arm = available_cash
        self._cash_reserved = {}
        self._cash_refetched = False
        # NOT IN F&O -> NOT WATCHED (issue #647). A fact from today's master
        # contract, applied unconditionally in option mode: a symbol with no NFO
        # contracts cannot fill under any variant, so watching it produces no
        # data — only a wasted watch slot and a trigger that dies at
        # `no_option_contract` (SAMMAANCAP, 2026-08-19 09:17:38). This ran ahead
        # of the liquidity gate on purpose: existence is not a liquidity verdict
        # and must not inherit that gate's off switch, which is exactly how the
        # verdict came to be recorded as `enforced: false` and ignored.
        self._apply_fno_filter(prev)
        # Gate 1 stage 1 (issue #583) — side-INDEPENDENT, so it can run before the
        # 09:16 ranking assigns one. Only drops symbols that fail on BOTH sides (or
        # have no NFO contracts at all); the per-side half is stage 2, inside the
        # core. Option-mode only: in stock mode option liquidity is irrelevant and
        # must not shrink the universe.
        gate, stage1_excluded = self._apply_liquidity_stage1(now.date())
        oi_filter_fn = self._build_oi_filter(now)
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
                shadow_side=self.day_config["shadow_side"],
                liquidity_gate=gate,
                liquidity_backfill_rank=self.day_config["option_liquidity_backfill_rank"],
                oi_filter_fn=oi_filter_fn,
            )
            self.positions = {}
            self.day_status = "armed"
            self._feed_ticks = 0
            self._feed_symbols = set()
            self._feed_last_tick = None
            self._feed_state = None
            self._selection_source = None
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
            # shadow-logged excluded side (issue #581) — the effective values, so
            # a past day can always say whether its short rows were measurable
            shadow_excluded_side=self.day_config["shadow_excluded_side"],
            shadow_side=self.day_config["shadow_side"],
            shadow_max_trades=self.day_config["shadow_max_trades"],
            sizing_mode=self.day_config["sizing_mode"],
            margin_per_slot=self.day_config["margin_per_slot"],
            margin_effective=self.day_config["margin_effective"],
            notional=self.day_config["notional"],
            cum_realized_pnl=self.day_config["cum_realized_pnl"],
            config_source="ui" if cfg_row else "env_defaults",
            # what the account could actually pay for (issue #626). Stamped even
            # when nothing was clamped, so a past day can always say whether the
            # signal or the balance was what limited it.
            max_trades_configured=configured_max,
            max_trades_effective=self.day_config["max_trades"],
            # issue #643 — what the page's capital card renders for a past day
            residual_sizing=residual_sizing,
            residual_reserve_pct=self.day_config["residual_reserve_pct"],
            residual_min_lots=self.day_config["residual_min_lots"],
            available_cash=available_cash,
            funds_clamp=clamp_note,
            tick_capture=bool(self._tick_writer),
            tick_capture_universe=bool(self._tick_writer) and self._capture_universe,
            prev_close_check=prev_check,
            first_candle_source=_first_candle_source(),
            baseline_includes_first_minute=_baseline_includes_first_minute(),
            # option-liquidity gate (issue #583) — the effective thresholds AND the
            # symbols stage 1 dropped, so a past round can always reconstruct the
            # universe that produced it. The arm-time filter CHANGES selection, so
            # without this stamp a day is not comparable to any earlier baseline.
            option_liquidity_gate_enabled=self.day_config["option_liquidity_gate_enabled"],
            option_liquidity_min_pctile=self.day_config["option_liquidity_min_pctile"],
            option_liquidity_backfill_rank=self.day_config["option_liquidity_backfill_rank"],
            option_liquidity_excluded=sorted(e["symbol"] for e in stage1_excluded),
            option_liquidity_universe_after=len(self.universe),
            # broker-OI mirror (issue #595) — the effective floor, so a past
            # day's selection is replayable against the rule it actually ran
            option_min_oi_lots=self.day_config["option_min_oi_lots"],
            oi_filter_active=oi_filter_fn is not None,
        )
        # ATM lot-cost coverage ladder (issue #591) — what capital/slot covers
        # how much of the universe, priced from the latest EOD option-liquidity
        # sweep (zero broker calls). Purely observational: a failure or a
        # missing/stale sweep skips the event, never the trading day.
        if _atm_lot_cost_enabled():
            try:
                from services.open15_atm_lot_cost import compute_event

                ev = compute_event(
                    self.universe,
                    self.day_config["margin_effective"],
                    self.day_config["coverage_target_pct"],
                    now.date(),
                )
                if ev:
                    self._log_event("atm_lot_cost", **ev)
                else:
                    logger.info("open15: atm_lot_cost skipped — no usable liquidity sweep")
            except Exception:
                logger.exception("open15: atm_lot_cost ladder failed — skipped (observational)")
        self._ensure_zmq_thread()
        # catch-up pricing for rows the 09:35 broker-lag left unpriced (the #435
        # shadow premiums and the #555 fill prices). Unconditional since #651 —
        # fill reconciliation is how a published P&L becomes the broker's own
        # number rather than a quote, which is not an operator preference. The
        # shadow half still checks its own flag inside.
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
        """Close a non-traded row as a PAPER, SIM or SHADOW fill (#548, #555, #581).

        Places NO order — there is no position to close. Prices the exit exactly
        where a real flatten would have, so the row carries the full
        sandbox-equivalent measurement (exit price, gross P&L, modelled charges)
        and the day stays comparable to one that traded.

        Serves all three non-traded classes because the *pricing* is identical;
        only the labels differ. A ``sim`` or ``shadow`` row keeps its ``skipped``
        status and its original reason — neither was rejected, and relabelling
        would blame the broker for a decision we made.
        """
        from database.open15_breakout_db import update_trade

        is_option = pos.get("instrument") == "option"
        fill = pos.get("fill")
        # ``sim`` and ``shadow`` are both "we never sent an order"; only ``paper``
        # means the broker refused one. Deriving the labels from ONE mapping
        # keeps a future fifth class from silently inheriting `rejected`.
        not_ordered = fill in ("sim", "shadow")
        fields = {
            "exit_ts": dt.datetime.now(IST).isoformat(),
            "exit_order_id": "",
            "exit_status": "not_placed",
            "status": "skipped" if not_ordered else "rejected",
            "fill": fill if not_ordered else "paper",
            "pnl_source": "quote",
            "fill_reconcile_status": "not_applicable",
            "reason": (
                pos.get("sim_reason", reason)
                if not_ordered
                else (reason if reason != "eod_0930" else "entry_rejected")
            ),
            "entry_minute_close": self.core.entry_minute_close(symbol) if self.core else None,
        }
        if not_ordered:
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
            # a DISTINCT event name per bucket, not a flag on `exit_paper`
            # (issues #555, #581): the digest sums events by name, so folding
            # one bucket into another's event would silently merge the buckets
            # the operator asked to keep apart
            {"sim": "exit_sim", "shadow": "exit_shadow"}.get(fill, "exit_paper"),
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
            fill=fields["fill"],
            reason=pos.get("sim_reason") if not_ordered else reason,
            note=(
                f"no order was placed ({pos.get('sim_reason')}) — priced at "
                f"{pos['quantity']} for measurement only, no money moved"
                if not_ordered
                else "order was rejected — sandbox-equivalent, no money moved"
            ),
        )

    def verify_entries(self) -> int:
        """Ask the broker what really happened to each ACK'd entry (issue #626).

        An ACK is not a fill. Zerodha returns HTTP 200 with an order id and its
        RMS can refuse the order afterwards — on 2026-08-18 TIINDIA was accepted,
        rejected for insufficient funds, and sat in `self.positions` as a live
        `open` position for ten minutes: it consumed a `max_trades` slot it was
        not entitled to, and `flatten` later tried to square it off.

        Deferred by construction, and that is the whole reason this is a job
        rather than a line in `_enter`. Entries are placed from the ZMQ tick
        callback; a synchronous broker round-trip there would stall every other
        symbol's tick processing — the same constraint that keeps fill
        reconciliation at the summary job.

        Demotes ONLY on a terminal-unfilled answer (`rejected` / `cancelled`).
        `open` and `complete` are both left alone: the first is still working,
        and the second is exactly what we want. An unreadable answer changes
        nothing — this runs every minute, and `flatten` re-checks the book.

        Returns the number of positions demoted.
        """
        from database.open15_breakout_db import update_trade
        from services.open15_fill_reconcile import fetch_fill, is_terminal_unfilled

        try:
            from database.auth_db import get_first_available_api_key

            api_key = get_first_available_api_key()
        except Exception:
            logger.exception("open15 entry-verify: api key lookup failed")
            return 0
        if not api_key:
            return 0

        with self._lock:
            pending = [
                (sym, dict(pos))
                for sym, pos in self.positions.items()
                if pos.get("status") == "open"
                and pos.get("entry_order_id")
                and not pos.get("entry_verified")
            ]

        demoted = 0
        for symbol, pos in pending:
            leg = fetch_fill(pos["entry_order_id"], api_key)
            if leg is None:
                continue
            status = leg["order_status"]
            if not is_terminal_unfilled(status):
                # `complete` is settled; `open` may still fill, so it is not
                # marked verified and gets asked again on the next tick.
                if status == "complete":
                    with self._lock:
                        if symbol in self.positions:
                            self.positions[symbol]["entry_verified"] = True
                    # The broker just told us the REAL entry fill — persist it
                    # instead of discarding it (issue #641: on 2026-08-19 this
                    # job saw MANKIND's true entry 35.7 at 09:25 and threw it
                    # away, so the 09:30 exit still priced the entry from a
                    # stale quote). The exit-time reconcile then only has the
                    # exit leg left to resolve. P&L is NOT recomputed here —
                    # reconcile_fills stays the single writer of pnl/pnl_source.
                    if leg["price"]:
                        update_trade(
                            pos["row_id"],
                            entry_fill_price=leg["price"],
                            entry_fill_qty=leg["qty"],
                        )
                continue

            msg = leg["message"] or f"broker reported the entry {status}"
            # The position dict is what `_count_fills` reads for the max_trades
            # budget and what `flatten` dispatches on, so both have to move — a
            # journal-only update would free the slot in the report and not in
            # the run.
            with self._lock:
                if symbol in self.positions:
                    self.positions[symbol].update(status="paper", fill="paper", entry_verified=True)
            # RMS refused it after the ACK — nothing was bought, so give the cash
            # back to the ledger (issue #643). Without this a post-ACK rejection
            # keeps shrinking every later entry for the rest of the morning.
            self._release_cash(symbol)
            update_trade(
                pos["row_id"],
                status="rejected",
                entry_status="rejected",
                fill="paper",
                error_message=msg[:255],
                reason="entry_rejected",
            )
            # `entry_rejected`, NOT a new event name. The digest and the row
            # builder in open15_log_view already render this one, and the /logs
            # page has twice gone dark on an event nobody taught it (#615, #622).
            # It IS a rejected entry — only the moment of discovery differs.
            self._log_event(
                "entry_rejected",
                symbol=symbol,
                instrument=pos.get("instrument") or "stock",
                contract=pos.get("opt_symbol"),
                qty=pos.get("quantity"),
                entry_price=pos.get("opt_entry_premium") or pos.get("trigger_price"),
                watch_source=pos.get("watch_source") or "seed",
                order_id=pos["entry_order_id"],
                order_status=status,
                error=msg,
                fill="paper",
                paper_capped=False,
                slot_released=True,
                # what separates this from a placement-time rejection: the
                # broker had already ACKNOWLEDGED the order when we entered it
                post_ack=True,
            )
            self._alert_rejection(symbol, pos.get("quantity") or 0, msg)
            # Gap A (issue #659): our entry was refused, but the child mirror
            # fired at ACK time and may have filled. No parent exit will ever
            # fire for a paper row, so close the child now, not at 15:20.
            self._flatten_child_mirrors_for(
                symbol, pos, "parent entry demoted to paper (post-ACK reject)"
            )
            demoted += 1
        return demoted

    def _entry_never_filled(self, symbol: str, pos: dict) -> bool:
        """True only when the book AFFIRMATIVELY says we hold nothing (issue #626).

        Deliberately not the inverse of "confirmed held". ``_broker_qty`` returns
        None for an unreadable book, and None must NOT stop the square-off of a
        position we believe is real — that is the direction that strands an
        overnight lot. Only a definite 0 diverts to paper.
        """
        is_option = pos.get("instrument") == "option"
        sym = pos["opt_symbol"] if is_option else symbol
        qty = self._broker_qty(sym, "NFO" if is_option else "NSE")
        return qty == 0

    def _flatten_child_mirrors_for(
        self, symbol: str, pos: dict, reason: str, corroborated: bool = False
    ) -> None:
        """Close a child-account mirror stranded by a paper demotion (issue #659).

        The child mirror fired at ACK time and can be FILLED in the child's own
        account even though OUR entry was refused — the child has its own
        balance and RMS. Demoting the parent to paper suppresses the parent
        exit, which is the only trigger the child exit had; without this call
        the child position rides unmanaged until the broker's MIS square-off.

        ``corroborated=False`` re-checks our own book first: ``flatten`` can
        RE-promote a demoted row when the book disagrees with the reject
        verdict (#626 "the book wins"), and in that case the parent WILL exit
        and the normal exit mirror handles the child — sweeping it early would
        leave that later exit mirror echoing against a flat child. Only an
        AFFIRMATIVE 0 sweeps; an unreadable book defers to the post-summary
        sweep. Never raises — this must not disturb the demotion it rides on.
        """
        try:
            is_option = pos.get("instrument") == "option"
            mirrored = pos.get("opt_symbol") if is_option else symbol
            if not mirrored:
                return
            if not corroborated and self._broker_qty(mirrored, "NFO" if is_option else "NSE") != 0:
                return
            from services.account_fanout_service import flatten_stranded_child_mirrors

            flatten_stranded_child_mirrors(STRATEGY_NAME, symbols=[mirrored], reason=reason)
        except Exception:
            logger.exception("open15: child-mirror orphan flatten failed for %s", symbol)

    def _reconcile_and_log(self, attempts: int = 1, delay_s: float = 0.0) -> None:
        """Run the broker fill reconcile and log its events (issues #555/#641).

        The one writer of the ``fill_reconcile_row`` / ``fill_reconcile`` event
        shape, shared by the post-flatten immediate pass and the summary-job
        backstop so the two cannot drift apart. Retries up to ``attempts``
        times while legs are still ``pending`` (fills propagate to the broker
        orderbook within seconds of a MARKET exit). Never raises into the
        scheduler.
        """
        import time

        from services.open15_fill_reconcile import reconcile_fills

        for attempt in range(attempts):
            if attempt:
                time.sleep(delay_s)
            try:
                res = reconcile_fills(self._trade_date())
            except Exception:
                logger.exception("open15: fill reconciliation failed")
                return
            # one event per row FIRST, so the per-symbol table and the CSV
            # carry the transacted prices, then the roll-up
            for detail in res.pop("rows", []):
                self._log_event("fill_reconcile_row", **detail)
            self._log_event("fill_reconcile", **res)
            if not res.get("pending"):
                return

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
            # SIM (#555) and SHADOW (#581) are both "no order was ever sent"
            unordered_pos = {
                s: p for s, p in self.positions.items() if p.get("status") in ("sim", "shadow")
            }
        for symbol, pos in paper_pos.items():
            if self._resolve_paper_position(symbol, pos):
                self._flatten_paper(symbol, pos, reason)
        # These rows skip the book verification entirely: no order was ever sent
        # for them, so there is nothing that could have half-reached the
        # exchange. Reading the book here could only surface an unrelated
        # same-symbol position and promote a trade we never placed into a live
        # square-off — the one failure mode this whole path must not have.
        for symbol, pos in unordered_pos.items():
            self._flatten_paper(symbol, pos, reason)

        with self._lock:
            open_pos = {s: p for s, p in self.positions.items() if p.get("status") == "open"}
        exit_orders_sent = 0
        for symbol, pos in open_pos.items():
            is_option = pos.get("instrument") == "option"
            # An ACK is not a fill (issue #626). Zerodha accepts the order, hands
            # back an order id, and RMS can still refuse it downstream — which is
            # exactly what happened to TIINDIA on 2026-08-18. The row said
            # `status='open'` and this loop sent a SELL for 800 calls we did not
            # own: a NAKED SHORT, which the broker priced at Rs4.45L of SPAN and
            # refused only because the funds were not there either.
            #
            # So the #548 rule — only an AFFIRMATIVE non-zero position book
            # justifies a square-off — applies here too, not just to rows already
            # labelled paper. Zero means the entry never filled: paper it and send
            # nothing. An UNREADABLE book (None) still squares off, because the
            # asymmetry runs the other way once an entry is believed filled — an
            # unsent exit strands a real overnight position, while an unnecessary
            # one is caught by the broker's own 15:15 MIS auto-square-off.
            if self._entry_never_filled(symbol, pos):
                logger.error(
                    "open15: %s entry never filled (book flat) — papering instead of "
                    "sending an unbacked %s",
                    symbol,
                    "SELL" if is_option or pos["side"] == "L" else "BUY",
                )
                pos["fill"] = "paper"
                self._flatten_paper(symbol, pos, "entry_rejected")
                # Gap A (issue #659): `_entry_never_filled` just AFFIRMED our
                # book is flat (corroborated), but the child mirror fired at
                # the ACK and may hold a real position with no exit coming.
                self._flatten_child_mirrors_for(
                    symbol, pos, "parent entry papered at exit (book flat)", corroborated=True
                )
                continue
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
            exit_orders_sent += 1
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
        if exit_orders_sent:
            # Publish the FILL-true P&L now, not at the exit+5 summary (issue
            # #641). The quote-derived numbers written above are estimates made
            # BEFORE the market order filled; on 2026-08-19 they overstated the
            # day by ~Rs6,470 for five minutes. This thread is an APScheduler
            # worker — not the ZMQ tick thread the #555 "deferred, never
            # synchronous" rule protects — and it already made a synchronous
            # broker quote call per position above, so one more round-trip here
            # costs nothing new. A MARKET fill usually reaches the orderbook in
            # a second or two; one short in-pass retry covers the lag, and the
            # summary job + next-day arm remain the backstops for anything
            # still pending.
            self._reconcile_and_log(attempts=2, delay_s=4.0)
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
        n_real, n_paper, n_sim, n_shadow = self._count_fills()
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
            # the switched-off side, priced at full slot size (issue #581)
            shadow=n_shadow,
            # seed vs rolling split (issue #529): `selected` counts the whole
            # watch list, so without this the seed cohort is unreadable
            rolling_added=len(self.core.rolling_adds),
            day=self.day_status,
            captured_drift=drifts,
        )
        # Broker fill reconciliation backstop (issues #555/#641). The primary
        # pass now runs at the tail of `flatten` — seconds after the exits —
        # so this normally finds nothing left to do; it exists for legs the
        # broker had not reported yet (they stay `pending` and are retried by
        # the next 09:10 arm) and for days where the flatten-time pass failed.
        # Kept OFF the tick thread like everything broker-synchronous here.
        self._reconcile_and_log()
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

    @staticmethod
    def _load_persisted_day_log(trade_date: str) -> list[dict[str, Any]]:
        """Persisted decision log for a date, or []. Never raises (issue #597)."""
        try:
            from database.open15_breakout_db import get_day_log

            return get_day_log(trade_date) or []
        except Exception:
            logger.exception("open15: existing day-log read failed for %s", trade_date)
            return []

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
            (
                # Every minute from the first possible entry to the exit, so the
                # last entry of the day is still checked before `flatten`
                # dispatches on it. Cheap: the job early-returns without touching
                # the broker unless the day is armed AND holds an unverified
                # entry, of which there are at most `max_trades` in a day.
                #
                # Bounded to 09:xx on purpose. A configured exit later than that
                # (the window is operator-editable up to 15:10) leaves the tail
                # unverified, and that is acceptable: verification only buys back
                # a `max_trades` slot sooner. Correctness does not rest on it —
                # the exit-time book check and the summary reconciliation both
                # still catch an unfilled entry.
                "open15_entry_verify",
                CronTrigger(
                    hour=9,
                    minute=f"17-{max(em, 17) if eh == 9 else 59}",
                    day_of_week="mon-fri",
                    timezone=tz,
                ),
                _entry_verify_job,
            ),
        ]
        for job_id, trigger, fn in jobs:
            sched.add_job(fn, trigger, id=job_id, replace_existing=True, misfire_grace_time=60)
        logger.info(
            "open15: 6 scheduler jobs registered (arm 09:10 / first-candles 09:16 / "
            "exit %02d:%02d / retry %02d:%02d "
            "/ summary %02d:%02d IST / entry-verify every 1 min in the entry window)",
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
        # feed-health counters (issue #677): single-writer (this thread), read
        # by the scheduler minute job — three cheap ops, no locks, no broker.
        self._feed_ticks += 1
        self._feed_symbols.add(symbol)
        self._feed_last_tick = now
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
        if not was_finalized and core.finalized and self._selection_source is None:
            try:
                self._selection_source = "tick"
                self._log_selection_event(core, source="tick")
            except Exception:
                logger.exception("open15: selection event log failed")
        # Gate-1 stage-2 exclusions (issue #583) — drained rather than re-read, so
        # each is logged exactly once whether it came from the seed ranking above or
        # from a rolling addition below.
        self._drain_liquidity_exclusions(core)
        # additive re-rank (issue #529) — self-throttled to the configured
        # cadence, and a no-op entirely when the feature is off
        if core.rolling_enabled:
            try:
                for add in core.maybe_rerank(tick_ts):
                    self._log_event("watchlist_add", **add)
                self._drain_liquidity_exclusions(core)
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
            try:
                self._enter(action)
            except Exception as exc:
                # A trigger ALWAYS produces exactly one terminal event (#643).
                # Before this, a raise anywhere in ``_enter`` unwound to the ZMQ
                # loop's generic handler and the symbol simply vanished: no
                # journal row, no event, and a /logs row still showing the
                # 'no trigger' default it was given at selection — beside a
                # green volume cell saying the gate had been cleared.
                self._journal_entry_error(action, exc)

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

    def _log_selection_event(self, core: Open15Core, source: str = "tick") -> None:
        """Record the 09:16 SEED picks in the decision log (issue #545).

        ``watch_source`` is the authoritative seed-vs-rolling split, so the
        filter here holds even if a future change reorders the caller — the
        ordering fragility it guards against is exactly what caused #545.

        ``source`` (issue #677) says which path finalized: ``tick`` (normal)
        or ``scheduler`` (the 09:17 deadline ran because no tick had) — on a
        dead-feed day it is the tell that selection was rescued by the clock.
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
            source=source,
        )

    # ---- feed health (issue #677) ----------------------------------------- #
    FEED_DEGRADED_FRACTION = 0.5

    def check_feed_health(self) -> None:
        """Minute-cadence feed-health check + the clock-based selection deadline.

        Runs on the scheduler thread (never the ZMQ tick thread — the #626
        rule; it makes no broker calls either way). Two jobs:

        1. **Deadline finalize.** The tick-path fail-open needs a tick to run,
           so a dead feed used to skip the day silently (2026-08-25, #673).
           Past 09:17 an armed-but-unfinalized day is finalized here from the
           09:16 quote snapshot — so a mid-window feed recovery finds the
           watch list already armed instead of losing the day.
        2. **State transitions.** ``dead`` (zero ticks since arm) /
           ``degraded`` (ticking fraction below ``FEED_DEGRADED_FRACTION``) /
           ``ok``. Each transition journals ONE ``feed_health`` event (the
           initial transition INTO ``ok`` is deliberately silent — a normal
           day must not grow a daily noise row), and ``dead`` additionally
           Telegram-alerts once per day.
        """
        core = self.core
        if core is None or self.day_status != "armed":
            return
        now = self._now_ist()
        if not core.finalized and now.time() >= dt.time(9, 17):
            try:
                if core.ensure_finalized():
                    self._selection_source = "scheduler"
                    self._log_selection_event(core, source="scheduler")
            except Exception:
                logger.exception("open15: scheduler deadline finalize failed")
        universe_n = len(self.universe)
        ticking = len(self._feed_symbols)
        if self._feed_ticks == 0:
            state = "dead"
        elif universe_n and ticking / universe_n < self.FEED_DEGRADED_FRACTION:
            state = "degraded"
        else:
            state = "ok"
        prev = self._feed_state
        if state == prev or (prev is None and state == "ok"):
            self._feed_state = state
            return
        self._feed_state = state
        last_tick = self._feed_last_tick.strftime("%H:%M:%S") if self._feed_last_tick else None
        self._log_event(
            "feed_health",
            state=state,
            prev=prev,
            ticks=self._feed_ticks,
            symbols_ticking=ticking,
            universe=universe_n,
            last_tick=last_tick,
            selection_source=self._selection_source,
        )
        if state == "dead":
            logger.error(
                "open15 FEED DEAD: 0 ticks since the %s arm (universe %d) — "
                "selection %s; entries cannot trigger until ticks resume",
                self._log_date,
                universe_n,
                "finalized from the 09:16 quote snapshot"
                if core.finalized
                else "NOT finalized yet",
            )
            today = now.strftime("%Y-%m-%d")
            if self._feed_alert_date != today:
                self._feed_alert_date = today
                try:
                    from services.notification_service import get_notification_service

                    get_notification_service().notify(
                        "open15_breakout",
                        f"🚨 open15_vol_breakout [{_mode()}]: FEED DEAD — 0 ticks since "
                        f"the 09:10 arm (universe {universe_n}). Selection was finalized "
                        f"from the 09:16 quote snapshot so the watch list is armed, but "
                        f"no entry can trigger until ticks resume. Check the WS proxy / "
                        f"tick-liveness watchdog (#673 class).",
                    )
                except Exception:
                    logger.exception("open15: feed-dead alert failed")
        elif state == "degraded":
            logger.warning(
                "open15 feed DEGRADED: %d/%d symbols ticking (last tick %s)",
                ticking,
                universe_n,
                last_tick,
            )
        else:
            logger.info(
                "open15 feed RECOVERED: %d/%d symbols ticking after %s", ticking, universe_n, prev
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

    def _count_fills(self) -> tuple[int, int, int, int]:
        """``(real, paper, sim, shadow)`` position counts (issues #548, #555, #581).

        A rejected entry frees its ``max_trades`` slot — it is not a trade, and
        on 2026-08-05 three rejections consumed the whole daily cap. Paper fills
        carry their own cap so a persistently-rejecting broker (static IP, RMS
        block) cannot simulate an unbounded number of trades and leave the day
        incomparable to a normal one.

        ``sim`` rows (triggers no order was ever sent for) are counted apart from
        ``paper`` and carry their own cap: they are not evidence of a broker
        problem, so letting them consume the paper budget would mask one.

        ``shadow`` rows (the side ``trade_side`` switched off, issue #581) are a
        fourth bucket with a fourth cap. **``real`` is computed by subtracting
        every non-real class**, so a new class MUST be subtracted here as well
        as added to ``NON_REAL_FILLS`` — miss this and shadow rows would consume
        the real ``max_trades`` budget and be reported as ``filled``.
        """
        with self._lock:
            vals = list(self.positions.values())
        paper = sum(1 for p in vals if p.get("fill") == "paper")
        sim = sum(1 for p in vals if p.get("fill") == "sim")
        shadow = sum(1 for p in vals if p.get("fill") == "shadow")
        return len(vals) - paper - sim - shadow, paper, sim, shadow

    # ---- cash ledger (issue #643) ------------------------------------------ #
    def _reserve_cash(self, symbol: str, amount: float) -> None:
        """Commit ``amount`` of the day's cash to ``symbol``'s entry.

        Called immediately before the order is placed, so a second trigger in
        the same second cannot be sized against money the first one has already
        spent. Entries are serialised on the ZMQ thread; the lock is for the
        status reader.
        """
        with self._lock:
            self._cash_reserved[symbol] = self._cash_reserved.get(symbol, 0.0) + max(amount, 0.0)

    def _release_cash(self, symbol: str) -> float:
        """Give ``symbol``'s reservation back — the entry never stood.

        Three callers, and all three are load-bearing: a placement rejection
        (#548), a post-ACK rejection found by ``verify_entries`` (#626), and an
        ``entry_error`` (#643). Miss one and the ledger drifts down all morning
        until the strategy quietly stops sizing anything.

        Exits deliberately do NOT release: the entry window closes at 09:29 and
        the flatten is 09:30, so nothing can be recycled inside a session.
        """
        with self._lock:
            return self._cash_reserved.pop(symbol, 0.0)

    def _cash_remaining(self) -> float | None:
        """Spendable cash left today, or ``None`` when it is unknown (#643).

        ``None`` is not zero — it means "do not clamp", and every caller treats
        it as the full slot. Only meaningful with residual sizing on; with the
        feature off this returns ``None`` so sizing is bit-for-bit what it was.

        One mid-day broker re-read, at the only moment precision matters: the
        first time the ledger says less than a full slot is left. Other
        strategies share the account, so the 09:10 balance can be stale by then
        — but re-reading on EVERY entry would put a synchronous broker call on
        the tick thread, which is what #626 forbids. The lower of the two wins.
        """
        if not (self.day_config or {}).get("residual_sizing_enabled"):
            return None
        if self._cash_at_arm is None:
            return None
        with self._lock:
            remaining = self._cash_at_arm - sum(self._cash_reserved.values())
        slot = float((self.day_config or {}).get("margin_effective") or 0.0)
        if remaining >= slot or self._cash_refetched:
            return remaining
        self._cash_refetched = True
        fresh = read_available_cash()
        if fresh is None:
            logger.warning(
                "open15: residual funds re-read failed — using the ledger (Rs%.0f)", remaining
            )
            return remaining
        if fresh < remaining:
            logger.warning(
                "open15: broker says Rs%.0f left, ledger said Rs%.0f — using the broker's figure",
                fresh,
                remaining,
            )
        return min(remaining, fresh)

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

        # the order never reached the market, so the cash it reserved is still
        # ours to spend on a later trigger (issue #643)
        self._release_cash(action["symbol"])
        _real, n_paper, _n_sim, _n_shadow = self._count_fills()
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
        _real, _paper, n_sim, _n_shadow = self._count_fills()
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
        # a DICT since #555 — this line unpacked it as the old 3-tuple until
        # #643, so every option-mode call raised ValueError. The raise unwound
        # past ``_enter`` into the ZMQ loop's generic handler, which is why
        # GVT&D produced no journal row and no event at all on 2026-08-19:
        # the page showed a green (gate-cleared) volume beside "no trigger".
        liq = self._option_liquidity(contract["symbol"])
        premium = liq["ltp"]
        if not premium:
            return {}
        return {
            "opt_symbol": contract["symbol"],
            "opt_lot_size": int(contract["lotsize"]),
            "opt_entry_premium": premium,
            "opt_entry_volume": liq["volume"],
            "opt_entry_oi": liq["oi"],
            # free — they rode in the quote response above (issue #555), and a
            # sim row without them cannot be compared with a real one
            "opt_entry_bid": liq["bid"],
            "opt_entry_ask": liq["ask"],
            "opt_tick_size": contract.get("ticksize"),
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

    def _shadow_sizing(self, action: dict, cfg: dict) -> tuple[dict, int, str] | None:
        """``(row_extra, quantity, reason)`` for a shadow row, or ``None``.

        Sizes the row EXACTLY as ``_enter`` / ``_enter_option`` would have sized
        a real one — full slot capital, not the 1-lot ``sim`` convention — which
        is the only way the shadow cohort is comparable to the traded cohort and
        to ``config_snapshot.json``'s ``parity_target``.

        The one place the two conventions meet is an unaffordable contract: a
        real entry would have been skipped there, so the row falls back to 1 lot
        and SAYS SO in ``reason``. Leaving that silent would hide a sizing split
        inside one bucket — the defect shape #552 exists to prevent.

        Returns ``None`` when the contract or its premium is unavailable; the
        caller journals the bare skip rather than inventing a price.
        """
        if cfg.get("instrument") != "atm_option":
            notional = cfg.get("notional") or _notional()
            return {"instrument": "stock"}, max(int(notional / action["price"]), 1), "side_excluded"

        from services.open15_option_shadow import resolve_atm_option

        contract = resolve_atm_option(
            action["symbol"], action["side"], action["price"], self._trade_date()
        )
        if not contract:
            return None
        liq = self._option_liquidity(contract["symbol"])
        premium = liq["ltp"]
        if not premium:
            return None
        lot = int(contract["lotsize"])
        extra = {
            "instrument": "option",
            "opt_symbol": contract["symbol"],
            "opt_lot_size": lot,
            "opt_entry_premium": premium,
            "opt_entry_volume": liq["volume"],
            "opt_entry_oi": liq["oi"],
            "opt_entry_bid": liq["bid"],
            "opt_entry_ask": liq["ask"],
            "opt_tick_size": contract.get("ticksize"),
        }
        slot_capital = float(cfg.get("margin_effective") or cfg.get("margin_per_slot") or 30_000)
        lots = int(slot_capital // (premium * lot))
        if lots < 1:
            return extra, lot, "side_excluded_unaffordable"
        return extra, lots * lot, "side_excluded"

    def _journal_shadow(self, action: dict, cfg: dict) -> None:
        """Journal a trigger on the switched-off side — NO ORDER (issue #581).

        This method never calls ``order_placer``. That is the whole contract:
        the operator trades one side with real money and wants the other side
        measured, so a shadow row is a priced counterfactual and nothing more.

        The row is registered as a position so the normal exit-time flatten
        prices its exit, carrying ``no_order_attempted`` — nothing was sent, so
        ``flatten`` must not read the position book for it (reading could only
        surface an unrelated same-symbol position and promote a trade we never
        placed into a live square-off).

        Defensive by design: a failure here is logged and swallowed. Shadowing
        is instrumentation bolted onto a strategy trading real money, and it
        must never be able to cost the traded side an entry.
        """
        from database.open15_breakout_db import insert_trade

        try:
            _real, _paper, _sim, n_shadow = self._count_fills()
            shadow_max = cfg.get("shadow_max_trades")
            shadow_max = (
                _shadow_max_trades_default()
                if shadow_max is None
                else clamp_shadow_max_trades(shadow_max)
            )
            sized = None if n_shadow >= shadow_max else self._shadow_sizing(action, cfg)
            if sized is None:
                # Capped, or no contract/premium to price against. Journaled
                # unpriced as ``none`` — the documented "deliberately not
                # measured" class — so the day still records that the excluded
                # side triggered here, without a fabricated P&L.
                reason = "shadow_cap" if n_shadow >= shadow_max else "shadow_unpriceable"
                insert_trade(
                    trade_date=self._trade_date(),
                    symbol=action["symbol"],
                    side=action["side"],
                    mode=_mode(),
                    instrument="option" if cfg.get("instrument") == "atm_option" else "stock",
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
                    fill="none",
                )
                self._log_event(
                    "entry_skipped",
                    symbol=action["symbol"],
                    reason=reason,
                    watch_source=action.get("watch_source") or "seed",
                    fill="none",
                    shadow=True,
                )
                return
            extra, qty, reason = sized
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
                # ``quantity`` records what was ORDERED, and nothing was.
                # ``sim_quantity`` is the size the P&L is priced on — the same
                # column the sim bucket uses, at a different (full-slot) size,
                # which ``reason`` makes explicit on every row.
                quantity=0,
                sim_quantity=qty,
                watch_source=action.get("watch_source") or "seed",
                status="skipped",
                reason=reason,
                fill="shadow",
                **extra,
            )
            with self._lock:
                self.positions[action["symbol"]] = {
                    **action,
                    "trigger_price": action["price"],
                    "quantity": int(qty),
                    "row_id": row_id,
                    "status": "shadow",
                    "fill": "shadow",
                    "sim_reason": reason,
                    "no_order_attempted": True,
                    **{
                        k: extra[k]
                        for k in ("instrument", "opt_symbol", "opt_lot_size", "opt_entry_premium")
                        if k in extra
                    },
                }
            self._log_event(
                "entry_shadow",
                symbol=action["symbol"],
                side="BUY" if extra.get("instrument") == "option" else action["side"],
                contract=extra.get("opt_symbol"),
                instrument=extra.get("instrument"),
                watch_source=action.get("watch_source") or "seed",
                qty=qty,
                trigger_price=round(action["price"], 2),
                entry_price=extra.get("opt_entry_premium") or round(action["price"], 2),
                level=round(action["level"], 2),
                at=f"{action['trigger_minute']}:{action['trigger_second']:02d}",
                cumvol=int(action["cum_vol_at_trigger"]),
                baseline=int(action["baseline_vol"]),
                vol_ratio=round(action["cum_vol_at_trigger"] / max(action["baseline_vol"], 1), 2),
                reason=reason,
                fill="shadow",
                note=(
                    "no order placed — this side is switched off by trade_side; "
                    "priced for measurement only, no money moved"
                ),
            )
        except Exception:
            logger.exception(
                "open15: shadow journaling failed for %s — the traded side is unaffected",
                action.get("symbol"),
            )

    def _enter(self, action: dict) -> None:
        from database.open15_breakout_db import insert_trade

        cfg = self.day_config or {}
        # SHADOW SIDE (issue #581) — checked FIRST, ahead of every path that can
        # reach ``order_placer``. This side is switched off by ``trade_side``;
        # it is watched only so the excluded cohort can be scored against the
        # traded one, and no order is ever sent for it. It is also checked ahead
        # of the ``max_trades`` cap because that cap is a REAL-money budget: a
        # shadow trigger must neither consume a slot nor be diverted into a
        # ``max_trades_cap`` sim row.
        if action.get("shadow"):
            self._journal_shadow(action, cfg)
            return
        max_trades = int(cfg.get("max_trades") or _max_trades_default())
        # only REAL fills consume the daily cap (issue #548) — a broker-rejected
        # entry is not a trade, and letting rejections eat the cap is how three
        # static-IP 403s used up the whole 2026-08-05 budget
        n_real, _n_paper, _n_sim, _n_shadow = self._count_fills()
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

        # Stock mode is leveraged intraday, so the CASH a slot consumes is
        # ``notional / leverage`` = the slot itself. Sized through the same
        # ``resolve_entry_budget`` as the option path (issue #643) so the two
        # branches can never disagree about what "the residual" means.
        leverage = float(cfg.get("leverage") or 1.0) or 1.0
        slot_capital = float(cfg.get("margin_effective") or cfg.get("margin_per_slot") or 30_000)
        budget, sizing_basis = resolve_entry_budget(
            slot_capital,
            self._cash_remaining(),
            float(cfg.get("residual_reserve_pct") or 0.0),
        )
        notional = (
            (cfg.get("notional") or _notional()) if sizing_basis == "slot" else budget * leverage
        )
        qty = max(int(notional / action["price"]), 1)
        side_word = "BUY" if action["side"] == "L" else "SELL"
        # commit the cash BEFORE placing (issue #643)
        self._reserve_cash(action["symbol"], qty * action["price"] / leverage)
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
            "sizing_basis": sizing_basis,
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
                # kept so the deferred verification can ask the broker what
                # really happened to this order (issue #626)
                "entry_order_id": str(resp.get("orderid") or ""),
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
            sizing_basis=sizing_basis,
            slot_capital_used=round(budget, 2),
            order_status=resp.get("status"),
            order_id=resp.get("orderid"),
        )

    def _journal_entry_error(self, action: dict, exc: BaseException) -> None:
        """Terminal row + event for a trigger whose entry RAISED (issue #643).

        Not a rejection and not a skip: the broker never gave us an answer, so
        the row is ``status='error'`` with no fill class — it must never join
        any P&L bucket, real or simulated. Any cash the entry had already
        committed is released, otherwise a crash halfway through sizing would
        silently shrink every later entry.

        Deliberately does not re-raise, and never itself raises into the tick
        thread: one symbol's failure must not cost every other symbol its ticks.
        """
        symbol = action.get("symbol", "?")
        self._release_cash(symbol)
        msg = f"{type(exc).__name__}: {exc}"
        logger.exception("open15: entry FAILED for %s — %s", symbol, msg)
        try:
            from database.open15_breakout_db import insert_trade

            insert_trade(
                trade_date=self._trade_date(),
                symbol=symbol,
                side=action.get("side"),
                mode=_mode(),
                gap_pct=action.get("gap_pct"),
                level=action.get("level"),
                baseline_vol=action.get("baseline_vol"),
                cum_vol_at_trigger=action.get("cum_vol_at_trigger"),
                trigger_minute=action.get("trigger_minute"),
                trigger_second=action.get("trigger_second"),
                trigger_price=action.get("price"),
                quantity=0,
                watch_source=action.get("watch_source") or "seed",
                status="error",
                reason="entry_error",
                error_message=msg[:255],
            )
        except Exception:
            logger.exception("open15: entry-error journal write failed for %s", symbol)
        try:
            self._log_event(
                "entry_error",
                symbol=symbol,
                watch_source=action.get("watch_source") or "seed",
                trigger_price=round(float(action.get("price") or 0.0), 2),
                at=f"{action.get('trigger_minute')}:{int(action.get('trigger_second') or 0):02d}",
                error=msg,
                # the slot was never consumed — nothing was ordered
                slot_released=True,
            )
        except Exception:
            logger.exception("open15: entry-error event log failed for %s", symbol)
        self._alert_entry_error(symbol, msg)

    def _alert_entry_error(self, symbol: str, msg: str) -> None:
        """Telegram once per day. Never raises into the entry path."""
        logger.error("[%s] open15 ENTRY ERROR %s — %s", _mode(), symbol, msg)
        today = dt.datetime.now(IST).strftime("%Y-%m-%d")
        # its OWN dedup key, not the rejection one: a broker rejection and a
        # code fault are different failures and the operator must see both
        if self._entry_error_alert_date == today:
            return
        self._entry_error_alert_date = today
        try:
            from services.notification_service import get_notification_service

            get_notification_service().notify(
                "open15_breakout",
                f"⚠ open15_vol_breakout [{_mode()}]: entry FAILED for {symbol}"
                f"\n{msg}\n\n"
                f"The trigger was legal and no order was placed — the entry code "
                f"raised. The row is journaled status='error' and is excluded from "
                f"every P&L bucket.",
            )
        except Exception:
            logger.exception("open15: entry-error alert failed for %s", symbol)

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

    def _option_impact(self, opt_symbol: str, qty: int, liq: dict) -> dict:
        """Cost of crossing the ask for ``qty``, and whether that disqualifies it.

        ``{"blocked": bool, "columns": {...}}``. **Fails OPEN** — a depth call that
        errors, times out or comes back empty proceeds to place the order, mirroring
        ``_option_liquidity``'s own rule that a broken snapshot must never cost an
        entry. Entry only: nothing here may ever slow a square-off.
        """
        cols: dict = {}
        cfg = self.day_config or {}
        if not cfg.get("option_impact_gate_enabled", True):
            return {"blocked": False, "columns": cols}
        try:
            from services.depth_service import get_depth
            from services.open15_liquidity import impact_cost

            success, resp, _status = get_depth(opt_symbol, "NFO")
            data = (resp or {}).get("data") if success else None
            asks = (data or {}).get("asks") or []
            bid, ask = liq.get("bid"), liq.get("ask")
            mid = (float(bid) + float(ask)) / 2 if bid and ask else None
            ic = impact_cost(asks, qty, reference=mid)
        except Exception:
            logger.exception(
                "open15: depth probe failed for %s — impact gate FAILS OPEN", opt_symbol
            )
            return {"blocked": False, "columns": cols}

        cols = {
            "opt_impact_pct": ic["impact_pct"],
            "opt_depth_levels_used": ic["levels_used"],
            "opt_depth_exhausted": 1 if ic["exhausted"] else 0,
        }
        if ic["filled_qty"] <= 0:
            # no readable depth at all: a data gap, not a verdict
            logger.warning("open15: no depth for %s — impact gate FAILS OPEN", opt_symbol)
            return {"blocked": False, "columns": cols}

        limit = float(cfg.get("option_impact_max_pct", 2.0))
        # Exhaustion is disqualifying on its own: five visible levels could not fill
        # us, so impact_pct is computed on a PARTIAL fill and understates the cost.
        blocked = bool(ic["exhausted"]) or (
            ic["impact_pct"] is not None and ic["impact_pct"] > limit
        )
        if blocked:
            logger.warning(
                "open15: %s impact %.2f%% on %d qty (limit %.2f%%, levels=%d, exhausted=%s)"
                " — entry skipped",
                opt_symbol,
                ic["impact_pct"] or -1,
                qty,
                limit,
                ic["levels_used"],
                ic["exhausted"],
            )
        return {"blocked": blocked, "columns": cols}

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
        # what THIS entry may spend (issue #643): the full slot when the cash is
        # there, otherwise whatever is genuinely left. ``basis`` is journaled so
        # a smaller row is never mistaken for a full-size one.
        budget, sizing_basis = resolve_entry_budget(
            slot_capital,
            self._cash_remaining(),
            float(cfg.get("residual_reserve_pct") or 0.0),
        )
        lots = int(budget // (premium * lot))
        min_lots = int(cfg.get("residual_min_lots") or 1)
        if sizing_basis == "residual" and lots < min_lots:
            # the residual bought something, but less than the operator judged
            # worth trading — say WHICH constraint bound, never a bare "unaffordable"
            lots = 0
        if lots < 1:
            # the contract and its premium are already in hand, so pricing this
            # at 1 lot (issue #555) costs one extra quote at exit and nothing
            # more — and it is the only way to learn whether the slot capital,
            # not the signal, is what is capping the strategy
            _real, _paper, n_sim, _n_shadow = self._count_fills()
            simulate = _sim_skipped_enabled() and n_sim < _paper_sim_max()
            self._journal_skip(
                action,
                "unaffordable_residual" if sizing_basis == "residual" else "unaffordable",
                instrument="option",
                opt_symbol=contract["symbol"],
                opt_lot_size=lot,
                opt_entry_premium=premium,
                opt_entry_volume=opt_volume,
                opt_entry_oi=opt_oi,
                sizing_basis=sizing_basis,
                **liq_kw,
                **({"sim_quantity": lot} if simulate else {}),
            )
            return
        qty = lots * lot
        # Gate 2 (issue #583) — what will this MARKET order actually pay? The
        # structural score in Gate 1 is a per-NAME aggregate and cannot see a strike
        # that is thin RIGHT NOW: POWERINDIA, the #488 bad exit, sits at p80 on it.
        # Only the live book at the trigger catches that, so this runs on the sized
        # order, immediately before it is sent.
        impact = self._option_impact(contract["symbol"], qty, liq)
        if impact.get("blocked"):
            self._journal_skip(
                action,
                "illiquid_option_book",
                instrument="option",
                opt_symbol=contract["symbol"],
                opt_lot_size=lot,
                opt_entry_premium=premium,
                opt_entry_volume=opt_volume,
                opt_entry_oi=opt_oi,
                **liq_kw,
                **impact["columns"],
            )
            self._log_event(
                "entry_skipped",
                symbol=action["symbol"],
                reason="illiquid_option_book",
                opt_symbol=contract["symbol"],
                qty=qty,
                **impact["columns"],
            )
            return
        # commit the cash BEFORE placing (issue #643) — the next trigger in the
        # same second must be sized against what is left, not what was left
        self._reserve_cash(action["symbol"], premium * qty)
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
            "sizing_basis": sizing_basis,
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
                # kept so the deferred verification can ask the broker what
                # really happened to this order (issue #626)
                "entry_order_id": str(resp.get("orderid") or ""),
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
            # issue #643 — a residual row is a DIFFERENT SIZE from a slot row,
            # and the page badges it so the two are never read as comparable
            sizing_basis=sizing_basis,
            slot_capital_used=round(budget, 2),
            order_status=resp.get("status"),
            order_id=resp.get("orderid"),
            # issue #669 — inside the broker's physical-delivery block window
            # (expiry day + the trading day before) the contract is next-month,
            # a structurally different book (lower gamma, wider spread, thinner
            # OI); the flag lets research segment those rows without parsing
            # opt_symbol
            **(
                {"expiry_rolled": True, "rolled_from": contract.get("rolled_from")}
                if contract.get("expiry_rolled")
                else {}
            ),
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
            # live cash ledger (issue #643) — what the /logs capital card reads
            # DURING the window; a past day reads the same facts off the
            # ``armed`` event plus its journal rows.
            "cash_at_arm": self._cash_at_arm,
            "cash_reserved": round(sum(self._cash_reserved.values()), 2),
            "cash_remaining": (
                None
                if self._cash_at_arm is None
                else round(self._cash_at_arm - sum(self._cash_reserved.values()), 2)
            ),
            "residual_sizing": bool((self.day_config or {}).get("residual_sizing_enabled")),
            "trade_side": (self.day_config or {}).get("trade_side") or _trade_side_default(),
            # shadow-logged excluded side (issue #581). ``shadow_side`` is the
            # letter that is watched but never traded — ``None`` whenever the
            # feature is off OR ``trade_side`` is ``both`` (nothing to exclude).
            "shadow_excluded_side": bool(
                (self.day_config or {}).get("shadow_excluded_side", _shadow_excluded_side_default())
            ),
            "shadow_side": (self.day_config or {}).get("shadow_side"),
            "shadow_max_trades": (self.day_config or {}).get("shadow_max_trades")
            or _shadow_max_trades_default(),
            "universe_size": len(self.universe),
            "selected": dict(core.selected) if core else {},
            "gaps_pct": {s: round(g * 100, 2) for s, g in (core.gaps or {}).items()}
            if core
            else {},
            "entered": list(core.entered) if core else [],
            # live near-miss stats so the decision-log UI can fill `max vol×`
            # during the window instead of waiting for the exit job (issue #524)
            "watch_stats": core.watch_snapshot() if core else {},
            # feed health (issue #677) — what the /logs chip reads live. State
            # is computed HERE (not the minute job's transition memory) so the
            # chip is current even between job ticks.
            "feed_health": {
                "state": (
                    "dead"
                    if self._feed_ticks == 0
                    else (
                        "degraded"
                        if self.universe
                        and len(self._feed_symbols) / len(self.universe)
                        < self.FEED_DEGRADED_FRACTION
                        else "ok"
                    )
                )
                if self.day_status == "armed"
                else None,
                "ticks": self._feed_ticks,
                "symbols_ticking": len(self._feed_symbols),
                "universe": len(self.universe),
                "last_tick": self._feed_last_tick.strftime("%H:%M:%S")
                if self._feed_last_tick
                else None,
                "finalized": bool(core.finalized) if core else False,
                "selection_source": self._selection_source,
            },
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
                # ``fill`` included (issue #581) so a mid-window reader can tell
                # a real position from a shadow one without waiting for the exit
                s: {k: p.get(k) for k in ("side", "quantity", "trigger_price", "status", "fill")}
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


def _entry_verify_job() -> None:
    """Minute-cadence post-ACK entry verification (issue #626) + feed health
    and the clock-based selection deadline (issue #677)."""
    svc = get_open15_service()
    if svc is None or svc.day_status != "armed":
        return
    try:
        # health first: with a dead feed there are no entries to verify, and
        # the deadline finalize must not wait behind a broker call
        svc.check_feed_health()
    except Exception:
        logger.exception("open15: feed-health check raised")
    try:
        svc.verify_entries()
    except Exception:
        # never let a verification failure escape into the scheduler; the exit
        # path re-checks the book and the reconciler re-checks at summary
        logger.exception("open15: entry verification raised")


def _summary_job() -> None:
    svc = get_open15_service()
    if svc is not None:
        svc.summary()
        # Orphan-flatten sweep (issue #659, gap B): a child whose exit mirror
        # was REJECTED has nothing retrying it — the exit-retry job re-flattens
        # parent rows only. Deliberately at summary time (exit+5), NOT at the
        # exit/retry jobs: exit mirrors are fire-and-forget on the fan-out
        # pool, and a sweep racing them could double-exit a child. By now every
        # mirror of the day has journaled; any non-zero net is stranded. No-op
        # in sandbox mode (fan-out never fires, so there are no rows).
        try:
            from services.account_fanout_service import flatten_stranded_child_mirrors

            flatten_stranded_child_mirrors(STRATEGY_NAME, reason="open15 post-summary sweep")
        except Exception:
            logger.exception("open15: post-summary child-mirror sweep failed")


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
