"""Chartink-equivalent SELL rule — mirror of ``fno_intraday_buy_chartink``.

Mirrors the operator's live Chartink ``alert-for-intraday-sell-fno`` formula.
Inequalities are directionally flipped from BUY, with a **simpler volume
gate**. As with BUY, evaluation short-circuits on the first miss.

Key differences from the BUY rule (worth flagging):
  * Gates 3, 5, 9, 10 are inequality-flipped (the directional mirror).
  * Gate 1 uses ``* (1 - sell_pct/100)`` (a gap **DOWN**) instead of BUY's
    ``* (1 + buy_pct/100)``.
  * Volume is a single gate ``daily volume > 1 day ago volume`` — a deliberate
    Chartink design choice for the SELL leg. BUY's two daily-volume gates
    (SMA(50) + SMA(200)) AND its 5m-volume-surge gate (g13) are **all absent**
    here. So this SELL rule needs NO 200-day SMA warm-up, and there is no
    5-minute volume condition at all.
  * BUY's tautological gate 11 (``open > low``) mirrors to ``open < high`` for
    SELL — equally tautological, so it is likewise skipped.
  * BUY's Gate 4 (``[-1] 5m Supertrend >= 1d-ago close``) is **absent** from
    the Chartink SELL screener — the SELL screener has a SINGLE 5m-Supertrend
    condition ("Crossed above Daily Close"), captured by Gate 3 alone.
    Earlier revisions of this rule carried a phantom Gate 4 mirrored from
    BUY; it has been removed (Issue #197).

That leaves **9 active gates** (vs BUY's 12).

Gates (source frame / lookback):
  6  daily close > 100                              today              1
  12 daily close < 5000                             today              1
  1  daily close < 1d-ago close * (1-sell_pct/100)  today + settled    2
  9  daily open  < 1d-ago close                     today + settled    2
  10 daily open  < pivot (H+L+C of yesterday) / 3   today + settled    2
  V  daily volume > 1d-ago volume                   today + settled    2
  7  weekly ATR(21) > 5% * daily close              weekly             22
  5  15m RSI(14) < 50                               bars_15m           15
  3  5m Supertrend(7,3)[0]  > today's running close bars_5m            >=8

Today's running close/open/volume are derived from today's 5m bars when
``bars_daily.iloc[-1]`` is yesterday-or-older (the production case during
the trading session). See ``services.scan_rules._today_running``.

Insufficient warm-up rejects the symbol (no gate skipping). Indicator NaN
during warm-up is treated as a rejection, not a silent pass — every indicator
value is ``pd.isna``-checked before use.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta

import pandas as pd
import pytz

from services.indicators import atr, rsi, supertrend
from services.scan_rules._today_running import derive_today_and_yest
from services.scanner_service import scan_rule
from utils.logging import get_logger

logger = get_logger(__name__)

_IST = pytz.timezone("Asia/Kolkata")

# Per-thread snapshot of the last successful evaluation's gate values (#205).
# Mirrors the BUY rule — see ``fno_intraday_buy_chartink._last_eval`` for the
# rationale and contract with the scanner PASS log site.
_last_eval = threading.local()


def get_last_eval_snapshot() -> dict | None:
    """Return the calling thread's last successful-evaluation gate snapshot, or
    ``None`` if no successful evaluation has run on this thread yet."""
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
    symbol (no hit) — a stale-data SELL can never fire on a frozen morning
    crash even if Path B in ``derive_today_and_yest`` regresses.
    Defense-in-depth on top of the WARNING.
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
    logger.warning("fno_intraday_sell_chartink %s: rejecting — %s", symbol, reason)
    return False


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
# (issue #305). Mirrors the BUY rule's ``_uncertified_warned`` — the rule
# re-fires every 5m bar close, so a bare WARNING would flood the log.
_uncertified_warned: set[tuple[str, str]] = set()
_uncertified_lock = threading.Lock()


def _reject_uncertified_reference(indicators: dict) -> None:
    """Log a dedup'd once-per-(symbol, IST-day) WARNING + fire the CRIT
    source-divergence Telegram for an explicitly-uncertified settled
    reference (issue #305). See the BUY rule's helper for the rationale —
    a stale yest_d.close manufactures phantom gap signals in BOTH
    directions. Best-effort — never raises into rule evaluation."""
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
                "fno_intraday_sell_chartink %s: REJECTING — settled reference close "
                "NOT certified against broker prev-close (settled=%s broker=%s "
                "divergence=%s%%) — stale historify-D reference (issue #305)",
                sym,
                settled,
                broker,
                div,
            )
        # CRIT Telegram — dedup is per-(service, symbol, day) inside the
        # helper (shared 'scanner_reference' service key with the BUY rule so
        # the operator gets ONE alert per symbol per day, not one per side).
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
                    "fno_intraday_sell_chartink %s: reference divergence alert dispatch failed",
                    sym,
                )
    except Exception:  # noqa: BLE001 — observability must never break rule eval
        logger.exception(
            "fno_intraday_sell_chartink %s: uncertified-reference reporting failed", sym
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
    "fno_intraday_sell_chartink",
    "sell",
    "10-gate Chartink SELL mirror (gap-down + simple volume + downtrend confirmation).",
)
def rule(bars: pd.DataFrame, indicators: dict) -> bool:
    """10-gate Chartink SELL mirror. Returns ``True`` only if every gate passes."""
    try:
        return _evaluate(bars, indicators)
    except Exception:
        # An indicator computation raised (e.g. ATR over a NaN-laden series).
        # Reject this symbol rather than crash the scan loop.
        logger.debug("fno_intraday_sell_chartink: evaluation raised, rejecting", exc_info=True)
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
    # gate can fire on it. A MISSING key is certified (backward compat: unit
    # tests / other callers build the indicators dict without it).
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
    sell_pct = float(p.get("gap_pct", os.environ.get("CHARTINK_RULE_SELL_GAP_PCT", "3.0")))
    atr_thresh = float(p.get("atr_pct", "5.0")) / 100.0
    rsi_thresh = float(p.get("rsi_threshold", "50.0"))
    st_period = int(p.get("supertrend_period", "7"))
    st_mult = float(p.get("supertrend_mult", "3.0"))
    price_min = float(p.get("price_min", "100.0"))
    price_max = float(p.get("price_max", "5000.0"))

    # --- Warm-up guards: insufficient history rejects (does NOT skip gates) ---
    # Unlike BUY, SELL has no SMA(volume, 200) gate, so daily needs only enough
    # rows for [-1]/[-2] (post-settle) or [-2]/[-3] (pre-settle) indexing.
    # Tier-1 Fix #2: a None frame (data missing) is WARNING; a short-but-present
    # frame (warm-up) is DEBUG, so the loud signal is specifically "no data".
    sym = indicators.get("symbol", "?")
    if bars_daily is None:
        return _reject_missing(sym, "bars_daily is None (no daily-D data)")
    if len(bars_daily) < 3:
        logger.debug(
            "fno_intraday_sell_chartink %s: daily warm-up (%d<3 rows)", sym, len(bars_daily)
        )
        return False
    if bars_weekly is None:
        return _reject_missing(sym, "bars_weekly is None")
    if len(bars_weekly) < 22:
        logger.debug("fno_intraday_sell_chartink %s: weekly warm-up (%d<22)", sym, len(bars_weekly))
        return False
    if bars_5m is None:
        return _reject_missing(sym, "bars_5m is None")
    if len(bars_5m) < 8:  # Supertrend(7) warm-up (period + ATR seed)
        logger.debug("fno_intraday_sell_chartink %s: 5m warm-up (%d<8)", sym, len(bars_5m))
        return False
    if bars_15m is None:
        return _reject_missing(sym, "bars_15m is None")
    if len(bars_15m) < 15:  # RSI(14) warm-up
        logger.debug("fno_intraday_sell_chartink %s: 15m warm-up (%d<15)", sym, len(bars_15m))
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
                    "fno_intraday_sell_chartink %s: today_d.close=%.2f diverges from "
                    "latest 5m close=%.2f (>%.2f%%) — possible stale daily source",
                    sym,
                    today_d.close,
                    last_5m_close,
                    threshold_pct,
                )
                # Issue #231: Telegram alert (dedup per-(scanner_rule_sell,
                # symbol, day) in the helper, so re-firing each 5m bar close
                # does NOT flood the operator).
                try:
                    from services.source_divergence_alerts import check_and_alert

                    check_and_alert(
                        service="scanner_rule_sell",
                        symbol=sym,
                        source_a_label="bars_daily_today_close",
                        source_a_value=float(today_d.close),
                        source_b_label="live_5m_last_close",
                        source_b_value=float(last_5m_close),
                    )
                except Exception:  # noqa: BLE001 — observability must never break rule eval
                    logger.exception(
                        "fno_intraday_sell_chartink %s: divergence alert dispatch failed", sym
                    )
                # Defense-in-depth: REJECT the symbol on divergence when the
                # block flag is on. A stale morning-crash snapshot can never
                # fire a SELL after the live price has rebounded, even if the
                # Path-B `ts`-column fix in derive_today_and_yest regresses.
                if _divergence_block_enabled():
                    logger.warning(
                        "fno_intraday_sell_chartink %s: REJECTING on divergence "
                        "(block flag on) — today_d.close=%.2f vs 5m=%.2f",
                        sym,
                        today_d.close,
                        last_5m_close,
                    )
                    return False
        except (TypeError, ValueError, KeyError, IndexError):
            logger.debug("fno_intraday_sell_chartink %s: divergence check skipped", sym)

    # --- D-bar-date verify (Tier-1 Fix #1) ---
    # The stale-D defense fires when the LATEST SETTLED bar is itself older
    # than yesterday — e.g. the 2026-06-15 FM-4/FM-6 condition where no
    # backfill ran for several sessions. Skipped when the frame carries no
    # timestamp to check (synthetic test frames).
    if yest_idx == -1 and _dbar_date_verify_enabled():
        bar_date = _daily_bar_date(bars_daily, yest_idx)
        if bar_date is not None and bar_date < now_ist.date() - timedelta(days=5):
            logger.warning(
                "fno_intraday_sell_chartink %s: latest settled daily bar is "
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
        yest_d.volume,
    ):
        return False

    # Gate 6: daily close > price_min
    if today_d.close <= price_min:
        return False
    # Gate 12: daily close < price_max
    if today_d.close >= price_max:
        return False
    # Gate 1: daily close < 1d-ago close * sell_mult (default 3% gap DOWN) — flipped
    # from BUY. Threshold is read via three-tier resolution: parameters dict → env var → default.
    sell_mult = 1.0 - sell_pct / 100.0
    if today_d.close >= yest_d.close * sell_mult:
        return False
    # Gate 9: daily open < 1d-ago close — flipped from BUY
    if today_d.open >= yest_d.close:
        return False
    # Gate 10: daily open < typical pivot = (H + L + C of 1d-ago) / 3 — flipped from BUY
    pivot = (yest_d.high + yest_d.low + yest_d.close) / 3.0
    if today_d.open >= pivot:
        return False

    # Volume gate: daily volume > 1d-ago volume (simpler than BUY's SMA(50)+SMA(200))
    if today_d.volume <= yest_d.volume:
        return False

    # Gate 7: weekly ATR(21) > atr_thresh * daily close (same as BUY).
    # Exclude the current (potentially partial) week when we have a spare row.
    weekly_for_atr = bars_weekly.iloc[:-1] if len(bars_weekly) > 22 else bars_weekly
    weekly_atr = atr(weekly_for_atr, period=21).iloc[-1]
    if _any_nan(weekly_atr):
        return False
    if weekly_atr <= today_d.close * atr_thresh:
        return False

    # Gate 5: 15m RSI(14) < rsi_thresh — flipped from BUY
    rsi_15m = rsi(bars_15m["close"], period=14).iloc[-1]
    if _any_nan(rsi_15m):
        return False
    if rsi_15m >= rsi_thresh:
        return False

    # Gate 3: 5m Supertrend(st_period, st_mult)[0] > today's running close
    # (supertrend ABOVE price = downtrend confirmation). The Chartink SELL
    # screener phrases this as "Crossed above Daily Close"; we encode it as
    # the steady-state "currently above" check on every 5m bar close, since
    # the in-house scanner re-evaluates on every bar (Chartink runs on a
    # ~15-min cadence and naturally fires fewer times). Issue #197 dropped
    # the phantom Gate 4 that mirrored BUY's [-1] supertrend condition —
    # the Chartink SELL screener has only this single supertrend gate.
    st = supertrend(bars_5m, period=st_period, multiplier=st_mult)
    if len(st) < 1:
        return False
    st_now = st["line"].iloc[-1]
    if _any_nan(st_now):
        return False
    if st_now <= today_d.close:
        return False

    # --- Gate-snapshot stash (Issue #205) ---
    # See ``fno_intraday_buy_chartink._evaluate`` for the rationale and
    # contract. SELL has no daily volume SMA gates, so ``sma_vol_*`` are
    # omitted; ``st_prev`` is not used by SELL (single supertrend gate).
    _last_eval.snapshot = {
        "today_d_close": float(today_d.close),
        "yest_d_close": float(yest_d.close),
        "today_d_open": float(today_d.open),
        "today_d_volume": float(today_d.volume),
        "yest_d_volume": float(yest_d.volume),
        "pivot": float(pivot),
        "rsi_15m": float(rsi_15m),
        "st_now": float(st_now),
        "weekly_atr": float(weekly_atr),
    }
    return True


def _any_nan(*values: float) -> bool:
    """True if any value is NaN — used to reject rather than silently pass a gate."""
    return any(pd.isna(v) for v in values)
