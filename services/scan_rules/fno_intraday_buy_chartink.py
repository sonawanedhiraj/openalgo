"""Chartink-equivalent BUY rule.

Mirrors the operator's live Chartink ``fno-intraday-buy`` formula. All gates
must clear; evaluation short-circuits on the first miss. Gate 11 of the
original formula (``open > low``) is tautological and skipped — the internal
numbering below is preserved from the source formula for traceability, so the
order here is intentionally non-sequential.

Gates (source frame / lookback):
  6  daily close > 100                              today              1
  12 daily close < 5000                             today              1
  1  daily close > 1d-ago close * (1+buy_pct/100)   today + settled    2
  9  daily open  > 1d-ago close                     today + settled    2
  10 daily open  > pivot (H+L+C of yesterday) / 3   today + settled    2
  2  daily vol   > SMA(daily vol, 50)               daily SMA          50
  8  daily vol   > SMA(daily vol, 200)              daily SMA          200
  7  weekly ATR(21) > 5% * daily close              weekly             22
  5  15m RSI(14) > 50                               bars_15m           15
  3  5m Supertrend(7,3)[0]  < today's running close bars_5m            >=8
  4  5m Supertrend(7,3)[-1] >= 1d-ago daily close   bars_5m            >=8

Today's running close/open/volume are derived from today's 5m bars when
``bars_daily.iloc[-1]`` is yesterday-or-older (the production case during
the trading session). See ``services.scan_rules._today_running``.

The earlier Gate 13 (``5m vol > 2 * SMA(5m vol, 10)``) was a phantom — it is
NOT present in the Chartink BUY screener filter. It was the dominant blocker
of valid Chartink fires (e.g., GLENMARK on 2026-06-29 cleared every Chartink
condition at 11:40 IST but failed the in-house Gate 13). Removed under Issue
#197.

Insufficient warm-up rejects the symbol (no gate skipping). Indicator NaN
during warm-up is treated as a rejection, not a silent pass — a bare
``volume <= NaN`` comparison is ``False`` (would wrongly "pass" the gate), so
every indicator value is ``pd.isna``-checked before use.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta

import pandas as pd
import pytz

from services.indicators import atr, rsi, sma, supertrend
from services.scan_rules._today_running import derive_today_and_yest
from services.scanner_service import scan_rule
from utils.logging import get_logger

logger = get_logger(__name__)

_IST = pytz.timezone("Asia/Kolkata")

# Per-thread snapshot of the last successful evaluation's gate values (#205).
# The scanner reads this via ``get_last_eval_snapshot()`` after the rule
# returns True and folds the values into the PASS log line so production
# diagnosis becomes a ``grep "scanner PASS"`` instead of a re-instrument
# + restart cycle. Set ONLY at the end of ``_evaluate`` (just before
# ``return True``) so a partial / exception-aborted evaluation never
# pollutes the next caller's snapshot.
_last_eval = threading.local()


def get_last_eval_snapshot() -> dict | None:
    """Return the calling thread's last successful-evaluation gate snapshot, or
    ``None`` if no successful evaluation has run on this thread yet.

    Consumers (scanner PASS-log site) must tolerate ``None`` so a rule that
    has not yet been instrumented falls back to the prior log shape.
    """
    return getattr(_last_eval, "snapshot", None)


def _divergence_warn_enabled() -> bool:
    """``SCANNER_RULE_DIVERGENCE_WARN_ENABLED`` env flag (default true)."""
    return os.environ.get("SCANNER_RULE_DIVERGENCE_WARN_ENABLED", "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def _divergence_warn_pct() -> float:
    """``SCANNER_RULE_DIVERGENCE_WARN_PCT`` env knob (default 0.5%)."""
    try:
        return float(os.environ.get("SCANNER_RULE_DIVERGENCE_WARN_PCT", "0.5"))
    except (TypeError, ValueError):
        return 0.5


def _divergence_block_enabled() -> bool:
    """``SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED`` env flag (default true).

    When on, a today_d.close that diverges beyond
    ``SCANNER_RULE_DIVERGENCE_WARN_PCT`` from the latest 5m close REJECTS the
    symbol (no hit) — a stale-data signal can never fire even if Path B in
    ``derive_today_and_yest`` regresses. Defense-in-depth on top of the WARNING.
    """
    return os.environ.get("SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED", "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def _reject_missing(symbol: str, reason: str) -> bool:
    """Loudly log a missing-input rejection (Tier-1 Fix #2), then return False.

    A ``None`` daily/weekly/intraday frame means the data pipeline did not supply
    an input — a supply problem worth a WARNING, not the silent ``return False``
    that made the 2026-06-15 failures look like ordinary quiet days. (Short-but-
    present frames are normal warm-up and stay at DEBUG below.)"""
    logger.warning("fno_intraday_buy_chartink %s: rejecting — %s", symbol, reason)
    return False


# Per-(symbol, IST-day) dedup for the SMA(200) daily-depth WARNING (issue #280).
# The rule re-fires every 5m bar close for every symbol, so a bare WARNING would
# flood the log with the same line hundreds of times a day. We emit exactly one
# WARNING per (symbol, day); the dedup is an in-process set keyed on the IST date
# so a process restart or IST date rollover re-arms it.
_shallow_daily_warned: set[tuple[str, str]] = set()
_shallow_daily_lock = threading.Lock()


def _warn_shallow_daily_once(symbol: str, n_rows: int, required: int, day_ist: str) -> None:
    """Emit a once-per-(symbol, IST-day) WARNING for a too-short daily frame.

    This is the observability half of issue #280: the BUY rule's SMA(``vol_sma_l``)
    volume gate needs ``required`` (default 200) settled daily bars, but the
    scanner's ``ScannerHistoryProvider`` reads stored historify-D, which is often
    short for the F&O universe (the 'stored-D short/stale' data-supply gap). The
    old ``logger.debug`` warm-up line was invisible at production ``LOG_LEVEL=INFO``,
    so a whole universe silently rejecting looked byte-identical to a quiet market
    (0 BUY hits, no signal). A dedup'd WARNING surfaces the depth gap in
    ``errors.jsonl`` within one cycle so the deep scanner_universe_D backfill can
    be triggered. Best-effort — never raises into rule evaluation.
    """
    key = (symbol, day_ist)
    with _shallow_daily_lock:
        if key in _shallow_daily_warned:
            return
        _shallow_daily_warned.add(key)
    logger.warning(
        "fno_intraday_buy_chartink %s: daily frame too short for SMA(%d) volume gate "
        "(%d<%d rows) — stored historify-D depth gap; BUY cannot evaluate. Deep "
        "scanner_universe_D backfill needed (issue #280).",
        symbol,
        required,
        n_rows,
        required,
    )


def _dbar_date_verify_enabled() -> bool:
    """``SCANNER_DBAR_DATE_VERIFY_ENABLED`` env flag (default true). Gates the
    post-settle daily-bar-date staleness guard below."""
    return os.environ.get("SCANNER_DBAR_DATE_VERIFY_ENABLED", "true").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


# Per-(symbol, IST-day) dedup for the uncertified-reference rejection WARNING
# (issue #305). Same pattern as ``_warn_shallow_daily_once`` above — the rule
# re-fires every 5m bar close, so a bare WARNING would flood the log.
_uncertified_warned: set[tuple[str, str]] = set()
_uncertified_lock = threading.Lock()


def _reject_uncertified_reference(indicators: dict) -> None:
    """Log a dedup'd once-per-(symbol, IST-day) WARNING + fire the CRIT
    source-divergence Telegram for an explicitly-uncertified settled
    reference (issue #305 — the 2026-07-02 DELHIVERY 42x false-BUY class:
    yest_d.close came from a stale historify-D slot that diverged 6.8% from
    the broker-known prior close). The scanner computed the verdict centrally
    (``services.scanner_reference_data``); this helper only reports it.
    Best-effort — never raises into rule evaluation."""
    sym = indicators.get("symbol", "?")
    try:
        settled = indicators.get("reference_settled_close")
        broker = indicators.get("reference_broker_prev_close")
        div = indicators.get("reference_divergence_pct")
        day_ist = datetime.now(_IST).date().isoformat()
        key = (sym, day_ist)
        with _uncertified_lock:
            first_today = key not in _uncertified_warned
            _uncertified_warned.add(key)
        if first_today:
            logger.warning(
                "fno_intraday_buy_chartink %s: REJECTING — settled reference close "
                "NOT certified against broker prev-close (settled=%s broker=%s "
                "divergence=%s%%) — stale historify-D reference (issue #305)",
                sym,
                settled,
                broker,
                div,
            )
        # CRIT Telegram — dedup is per-(service, symbol, day) inside the
        # helper, so the rule re-firing every 5m bar close cannot flood.
        if settled is not None and broker is not None:
            try:
                from services.source_divergence_alerts import check_and_alert

                check_and_alert(
                    service="scanner_reference",
                    symbol=sym,
                    source_a_label="settled_reference_close",
                    source_a_value=float(settled),
                    source_b_label="broker_prev_close",
                    source_b_value=float(broker),
                )
            except Exception:  # noqa: BLE001 — observability must never break rule eval
                logger.exception(
                    "fno_intraday_buy_chartink %s: reference divergence alert dispatch failed",
                    sym,
                )
    except Exception:  # noqa: BLE001 — observability must never break rule eval
        logger.exception(
            "fno_intraday_buy_chartink %s: uncertified-reference reporting failed", sym
        )


def _daily_bar_date(bars_daily: pd.DataFrame, idx: int):
    """IST calendar date of the daily bar at ``idx``, or ``None`` when it cannot
    be derived.

    Reads the ``timestamp`` column that historify-sourced frames always carry
    (epoch seconds, or a datetime). Synthetic test frames that lack the column
    return ``None`` so the date guard skips them — it only fires where there is
    a real timestamp to check against (production reads).
    """
    cols = getattr(bars_daily, "columns", [])
    if "timestamp" not in cols:
        return None
    try:
        ts = bars_daily.iloc[idx].get("timestamp")
        if ts is None or pd.isna(ts):
            return None
        # Convert via pandas (not ``datetime.fromtimestamp``) so the conversion
        # is independent of the module-level ``datetime`` symbol — tests
        # monkeypatch that to freeze ``now`` and it has no ``fromtimestamp``.
        if isinstance(ts, (int, float)):
            return pd.Timestamp(float(ts), unit="s", tz="UTC").tz_convert(_IST).date()
        return pd.Timestamp(ts).date()
    except Exception:
        return None


@scan_rule(
    "fno_intraday_buy_chartink",
    "buy",
    "12-gate Chartink BUY mirror (gap-up + volume surge + trend confirmation).",
)
def rule(bars: pd.DataFrame, indicators: dict) -> bool:
    """12-gate Chartink BUY mirror. Returns ``True`` only if every gate passes."""
    try:
        return _evaluate(bars, indicators)
    except Exception:
        # An indicator computation raised (e.g. ATR over a NaN-laden series).
        # Reject this symbol rather than crash the scan loop.
        logger.debug("fno_intraday_buy_chartink: evaluation raised, rejecting", exc_info=True)
        return False


def _evaluate(bars: pd.DataFrame, indicators: dict) -> bool:
    # Issue #158 D2: skip index symbols silently — this is an F&O-stock rule,
    # and indices (NSE_INDEX) are subscribed for tick flow for the regime /
    # sector_follow services, never for evaluation here. Without this check,
    # every 5m bar close emits "bars_daily is None (no daily-D data)" for
    # NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY/NIFTYNXT50 → 470 daily WARNINGs.
    if indicators.get("exchange") == "NSE_INDEX":
        return False

    # --- Reference certificate gate (issue #305) ---
    # The scanner validated the settled reference close (yest_d.close) against
    # the broker prev-close captured at boot and passed the verdict in. An
    # EXPLICIT False means a confirmed stale reference — reject before any
    # gate can fire on it (the 2026-07-02 DELHIVERY 42x false-BUY class). A
    # MISSING key is certified (backward compat: unit tests / other callers
    # build the indicators dict without it).
    if indicators.get("reference_certified") is False:
        _reject_uncertified_reference(indicators)
        return False

    bars_5m = indicators.get("bars_5m")
    if bars_5m is None:
        bars_5m = bars  # rule_fn is called with the 5m frame as `bars`
    bars_15m = indicators.get("bars_15m")
    bars_daily = indicators.get("bars_daily")
    bars_weekly = indicators.get("bars_weekly")

    # --- Three-tier parameter resolution: parameters dict → env var → default ---
    p = indicators.get("parameters", {})
    buy_pct = float(p.get("gap_pct", os.environ.get("CHARTINK_RULE_BUY_GAP_PCT", "3.0")))
    atr_thresh = float(p.get("atr_pct", "5.0")) / 100.0
    rsi_thresh = float(p.get("rsi_threshold", "50.0"))
    st_period = int(p.get("supertrend_period", "7"))
    st_mult = float(p.get("supertrend_mult", "3.0"))
    price_min = float(p.get("price_min", "100.0"))
    price_max = float(p.get("price_max", "5000.0"))
    vol_sma_s = int(p.get("vol_sma_short", "50"))
    vol_sma_l = int(p.get("vol_sma_long", "200"))

    # --- Warm-up guards: insufficient history rejects (does NOT skip gates) ---
    # Daily needs vol_sma_l rows for SMA(volume, vol_sma_l) and >=2 rows for [-1]/[-2] indexing.
    # Tier-1 Fix #2: a None frame (data missing) is WARNING; a short-but-present
    # frame (warm-up) is DEBUG, so the loud signal is specifically "no data".
    sym = indicators.get("symbol", "?")
    if bars_daily is None:
        return _reject_missing(sym, "bars_daily is None (no daily-D data)")
    if len(bars_daily) < vol_sma_l:
        # Issue #280: this is NOT ordinary warm-up — during a live session the
        # scanner reads stored historify-D via ScannerHistoryProvider, which is
        # short/stale for much of the F&O universe. A silent DEBUG here made the
        # BUY screener emit 0 hits for the whole universe indistinguishably from
        # a quiet market. Surface it as a dedup'd WARNING so the depth gap is
        # visible in errors.jsonl and the deep scanner_universe_D backfill can be
        # triggered. Still a rejection (the SMA(vol_sma_l) reference is genuinely
        # not computable), but now an observable one.
        now_ist = datetime.now(_IST)
        _warn_shallow_daily_once(sym, len(bars_daily), vol_sma_l, now_ist.date().isoformat())
        return False
    if bars_weekly is None:
        return _reject_missing(sym, "bars_weekly is None")
    if len(bars_weekly) < 22:
        logger.debug("fno_intraday_buy_chartink %s: weekly warm-up (%d<22)", sym, len(bars_weekly))
        return False
    if bars_5m is None:
        return _reject_missing(sym, "bars_5m is None")
    if len(bars_5m) < 8:  # Supertrend(7) warm-up (period + ATR seed)
        logger.debug("fno_intraday_buy_chartink %s: 5m warm-up (%d<8)", sym, len(bars_5m))
        return False
    if bars_15m is None:
        return _reject_missing(sym, "bars_15m is None")
    if len(bars_15m) < 15:  # RSI(14) warm-up
        logger.debug("fno_intraday_buy_chartink %s: 15m warm-up (%d<15)", sym, len(bars_15m))
        return False

    # --- Today's running daily snapshot + yesterday's settled bar (Issue #197) ---
    # Production ``bars_daily`` is the ScannerHistoryProvider cache backfilled
    # from historify.duckdb at boot, and does NOT include today's bar until
    # the post-close backfill runs at 15:30-17:00 IST. The shared helper
    # ``derive_today_and_yest`` resolves the right pair regardless: it uses
    # ``iloc[-1]`` as today when its date matches (broker refreshed or
    # synthetic test frame) or aggregates today's 5m bars into a running
    # daily snapshot otherwise.
    now_ist = datetime.now(_IST)
    today_d, yest_d, yest_idx = derive_today_and_yest(bars_daily, bars_5m, now_ist)
    if today_d is None or yest_d is None:
        return _reject_missing(
            sym,
            "cannot derive today's running daily snapshot (no today 5m bars and "
            "bars_daily has no today-dated bar)",
        )

    # --- Divergence WARNING (Issue #205) ---
    # If today_d.close drifts more than SCANNER_RULE_DIVERGENCE_WARN_PCT
    # (default 0.5%) from the latest 5m close, we are reading stale data
    # somewhere — exactly the failure class behind the 2026-06-29 41-SELL
    # false-positive storm. Logging it at WARNING surfaces the regression
    # in ``errors.jsonl`` within minutes instead of requiring manual triage.
    if _divergence_warn_enabled() and bars_5m is not None and len(bars_5m):
        try:
            last_5m_close = float(bars_5m["close"].iloc[-1])
            threshold_pct = _divergence_warn_pct()
            if (
                last_5m_close
                and not pd.isna(last_5m_close)
                and abs(today_d.close - last_5m_close) / last_5m_close > threshold_pct / 100.0
            ):
                logger.warning(
                    "fno_intraday_buy_chartink %s: today_d.close=%.2f diverges from "
                    "latest 5m close=%.2f (>%.2f%%) — possible stale daily source",
                    sym,
                    today_d.close,
                    last_5m_close,
                    threshold_pct,
                )
                # Issue #231: also dispatch a Telegram alert so the operator
                # sees the divergence within seconds instead of grepping
                # errors.jsonl. Dedup is per-(scanner_rule, symbol, day) in
                # the helper, so the rule re-firing every 5m bar close does
                # NOT produce a Telegram flood.
                try:
                    from services.source_divergence_alerts import check_and_alert

                    check_and_alert(
                        service="scanner_rule_buy",
                        symbol=sym,
                        source_a_label="bars_daily_today_close",
                        source_a_value=float(today_d.close),
                        source_b_label="live_5m_last_close",
                        source_b_value=float(last_5m_close),
                    )
                except Exception:  # noqa: BLE001 — observability must never break rule eval
                    logger.exception(
                        "fno_intraday_buy_chartink %s: divergence alert dispatch failed", sym
                    )
                # Defense-in-depth: REJECT the symbol on divergence when the
                # block flag is on. A stale-data signal can never fire even if
                # the Path-B `ts`-column fix in derive_today_and_yest regresses.
                if _divergence_block_enabled():
                    logger.warning(
                        "fno_intraday_buy_chartink %s: REJECTING on divergence "
                        "(block flag on) — today_d.close=%.2f vs 5m=%.2f",
                        sym,
                        today_d.close,
                        last_5m_close,
                    )
                    return False
        except (TypeError, ValueError, KeyError, IndexError):
            # Don't let a malformed 5m frame turn the observability layer into
            # a rule-evaluation crash — log and continue.
            logger.debug("fno_intraday_buy_chartink %s: divergence check skipped", sym)

    # --- D-bar-date verify (Tier-1 Fix #1) ---
    # The stale-D defense fires when the LATEST SETTLED bar is itself older
    # than yesterday (Issue #197 reframing — see SELL rule for details).
    if yest_idx == -1 and _dbar_date_verify_enabled():
        bar_date = _daily_bar_date(bars_daily, yest_idx)
        if bar_date is not None and bar_date < now_ist.date() - timedelta(days=5):
            logger.warning(
                "fno_intraday_buy_chartink %s: latest settled daily bar is "
                "STALE (bar_date=%s > 5 days behind today=%s) — aborting",
                indicators.get("symbol", "?"),
                bar_date,
                now_ist.date(),
            )
            return False

    # Reject if any required daily field is NaN.
    if _any_nan(
        today_d.close,
        today_d.open,
        today_d.volume,
        yest_d.close,
        yest_d.high,
        yest_d.low,
    ):
        return False

    # Gate 6: daily close > price_min
    if today_d.close <= price_min:
        return False
    # Gate 12: daily close < price_max
    if today_d.close >= price_max:
        return False
    # Gate 1: daily close > 1d-ago close * buy_mult (default 3% gap). Threshold is
    # read via three-tier resolution: parameters dict → env var → hardcoded default.
    buy_mult = 1.0 + buy_pct / 100.0
    if today_d.close <= yest_d.close * buy_mult:
        return False
    # Gate 9: daily open > 1d-ago close
    if today_d.open <= yest_d.close:
        return False
    # Gate 10: daily open > typical pivot = (H + L + C of 1d-ago) / 3
    pivot = (yest_d.high + yest_d.low + yest_d.close) / 3.0
    if today_d.open <= pivot:
        return False

    # Gates 2 + 8: daily volume vs SMA(vol_sma_s) and SMA(vol_sma_l). The SMA
    # is computed at the LATEST SETTLED bar (yest_idx) so the reference is a
    # function of prior settled history; today's running volume is the test
    # value compared against it.
    sma_vol_50 = sma(bars_daily["volume"], vol_sma_s).iloc[yest_idx]
    sma_vol_200 = sma(bars_daily["volume"], vol_sma_l).iloc[yest_idx]
    if _any_nan(sma_vol_50, sma_vol_200):
        return False
    if today_d.volume <= sma_vol_50:
        return False
    if today_d.volume <= sma_vol_200:
        return False

    # Gate 7: weekly ATR(21) > atr_thresh * daily close.
    # Exclude the current (potentially partial) week when we have a spare row.
    weekly_for_atr = bars_weekly.iloc[:-1] if len(bars_weekly) > 22 else bars_weekly
    weekly_atr = atr(weekly_for_atr, period=21).iloc[-1]
    if _any_nan(weekly_atr):
        return False
    if weekly_atr <= today_d.close * atr_thresh:
        return False

    # Gate 5: 15m RSI(14) > rsi_thresh
    rsi_15m = rsi(bars_15m["close"], period=14).iloc[-1]
    if _any_nan(rsi_15m):
        return False
    if rsi_15m <= rsi_thresh:
        return False

    # Gates 3 + 4: 5m Supertrend(st_period, st_mult). [0] = iloc[-1] (current), [-1] = iloc[-2].
    st = supertrend(bars_5m, period=st_period, multiplier=st_mult)
    if len(st) < 2:
        return False
    st_now = st["line"].iloc[-1]
    st_prev = st["line"].iloc[-2]
    if _any_nan(st_now, st_prev):
        return False
    # Gate 3: current 5m Supertrend < daily close (price above the trend line)
    if st_now >= today_d.close:
        return False
    # Gate 4: prior 5m Supertrend >= 1d-ago daily close
    if st_prev < yest_d.close:
        return False

    # --- Gate-snapshot stash (Issue #205) ---
    # Every value below drove the match. The scanner reads this via
    # ``get_last_eval_snapshot()`` to enrich its PASS log line so any future
    # ``derive_today_and_yest``-class regression can be reproduced from logs
    # alone. Stashed only on a successful evaluation — failures leave the
    # prior snapshot in place (never read by the scanner, since it only
    # reads on PASS).
    _last_eval.snapshot = {
        "today_d_close": float(today_d.close),
        "yest_d_close": float(yest_d.close),
        "today_d_open": float(today_d.open),
        "today_d_volume": float(today_d.volume),
        "pivot": float(pivot),
        "rsi_15m": float(rsi_15m),
        "st_now": float(st_now),
        "st_prev": float(st_prev),
        "weekly_atr": float(weekly_atr),
        "sma_vol_short": float(sma_vol_50),
        "sma_vol_long": float(sma_vol_200),
    }
    return True


def _any_nan(*values: float) -> bool:
    """True if any value is NaN — used to reject rather than silently pass a gate."""
    return any(pd.isna(v) for v in values)
