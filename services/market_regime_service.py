"""Stage 1.7 regime classifier — scaffold only.

This module computes a 5-dimension :class:`MarketRegime` from the
data we already have (historify duckdb for index bars, broker
WebSocket for live VIX once subscribed, system clock for time-of-day)
and persists snapshots to ``market_intel`` for later journaling.

**Not wired into the engine entry path.** The activator
(``services.strategy_activator_service.is_strategy_active_now``) reads
the current regime and a strategy's declared
:class:`strategies.base.RegimeProfile`, but no production code calls
the activator yet. Opt-in is per-strategy and gated by a single line
edit in ``simplified_stock_engine_service`` — see the operator README
note at the bottom of this file.

Data sources for v1
-------------------

* **trend** — 20/50-day EMA crossover on NIFTY daily bars from
  ``database.historify_db``. Falls back to ``range_bound`` when
  history is missing.
* **volatility** — bucketed India VIX value. v1 reads from
  ``REGIME_VIX_FALLBACK`` env (default ``"medium"``) because the
  ``INDIAVIX`` quote isn't on the WS subscription list yet. Switching
  to live VIX is a one-spot follow-up — see the ``TODO`` in
  :func:`_classify_volatility`.
* **breadth** — % of F&O universe symbols above their 20-day MA from
  the historify daily store. Without a configured universe we return
  ``mixed`` so the scaffold stays safe-by-default.
* **sector_leaders** — top-3 NIFTY sector indices ranked by today's
  % change. Data source priority is live broker REST quote (via
  ``services.quotes_service.get_quotes``) → historify daily-bar
  fallback → empty list. The tracked universe comes from
  ``REGIME_SECTOR_SYMBOLS`` env (Zerodha-canonical names like
  ``BANKNIFTY``, ``FINNIFTY``, ``NIFTYPSUBANK``). Concentration metric
  is ``(top_pct - median_pct) / (|top_pct| + 0.01)`` — >0.5 ⇒ dominant
  leader, <0.2 ⇒ broad rotation.
* **time_of_day** — IST clock buckets per the design doc.

Cache + persistence
-------------------

* :func:`compute_current_regime` is the source of truth.
* :func:`get_cached_regime` is the cheap accessor. It serves a
  process-local snapshot if it's younger than ``max_age_minutes``;
  otherwise it recomputes and refreshes the cache.
* :func:`log_regime_snapshot` writes the current regime to the
  ``market_intel`` sidecar table (``kind='regime'``).

All public functions are import-time safe — they don't touch the DB,
the broker, or the historify store until called.
"""

from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

import pytz

from strategies.base import RegimeProfile
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class MarketRegime:
    """Snapshot of the 5 regime dimensions at ``timestamp``.

    ``raw_metrics`` carries the underlying numbers so reflection can
    explain *why* a category was assigned (e.g. "trend=bullish because
    EMA20 (24102.3) > EMA50 (23876.1)").
    """

    timestamp: datetime
    trend: str
    volatility: str
    breadth: str
    sector_leaders: list[str]
    sector_leader_concentration: float
    time_of_day: str
    raw_metrics: dict = field(default_factory=dict)

    def matches(self, profile: RegimeProfile | None) -> bool:
        """Return True if ``self`` satisfies ``profile``'s constraints.

        ``None`` (the default profile on :class:`~strategies.base.BaseStrategy`)
        matches every regime. Each non-None field on the profile must
        accept the regime's value for that dimension.
        """
        if profile is None:
            return True
        if profile.trend is not None and self.trend not in profile.trend:
            return False
        if profile.volatility is not None and self.volatility not in profile.volatility:
            return False
        if profile.breadth is not None and self.breadth not in profile.breadth:
            return False
        if profile.time_of_day is not None and self.time_of_day not in profile.time_of_day:
            return False
        return True

    def to_payload(self) -> dict:
        """Render to a JSON-safe dict for persistence / wire transport."""
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


# ---------------------------------------------------------------------------
# Time-of-day bucketing
# ---------------------------------------------------------------------------

_TIME_OF_DAY_BUCKETS: list[tuple[time, time, str]] = [
    (time(9, 15), time(10, 0), "opening"),
    (time(10, 0), time(11, 30), "mid_morning"),
    (time(11, 30), time(13, 0), "lunch"),
    (time(13, 0), time(14, 30), "afternoon"),
    (time(14, 30), time(15, 15), "power_hour"),
]


def _classify_time_of_day(now_ist: datetime) -> str:
    """Bucket the IST wall-clock time. ``eod`` covers 15:15 onward —
    matches the design-doc range (`15:15+`) and the engine's default
    flatten cutoff. Anything before 09:15 falls into ``opening`` so the
    scaffold has a sensible default during pre-market boot."""
    t = now_ist.time()
    if t >= time(15, 15):
        return "eod"
    if t < time(9, 15):
        # Pre-market / early-morning warmup — treat as opening so
        # downstream activators don't trip on a missing bucket.
        return "opening"
    for start, end, label in _TIME_OF_DAY_BUCKETS:
        if start <= t < end:
            return label
    return "eod"


# ---------------------------------------------------------------------------
# Trend (NIFTY EMA crossover)
# ---------------------------------------------------------------------------

_NIFTY_SYMBOL = "NIFTY"
_NIFTY_EXCHANGE = "NSE_INDEX"
_TREND_FAST = 20
_TREND_SLOW = 50


def _classify_trend() -> tuple[str, dict[str, Any]]:
    """Compute trend from NIFTY 20-day vs 50-day EMA crossover.

    Returns ``("bullish" | "bearish" | "range_bound", raw_dict)``.
    ``range_bound`` is used when the two EMAs are within 0.5% of each
    other, or when there's not enough history to compute them.
    """
    raw: dict[str, Any] = {
        "fast_period": _TREND_FAST,
        "slow_period": _TREND_SLOW,
    }
    try:
        from database.historify_db import get_ohlcv
        from services.indicators import ema

        df = get_ohlcv(_NIFTY_SYMBOL, _NIFTY_EXCHANGE, "D")
        if df is None or df.empty or len(df) < _TREND_SLOW:
            raw["reason"] = "insufficient_history"
            raw["rows"] = 0 if df is None else len(df)
            return "range_bound", raw

        close = df["close"].astype(float)
        ema_fast = float(ema(close, _TREND_FAST).iloc[-1])
        ema_slow = float(ema(close, _TREND_SLOW).iloc[-1])
        raw["ema_fast"] = ema_fast
        raw["ema_slow"] = ema_slow
        raw["last_close"] = float(close.iloc[-1])

        if not math.isfinite(ema_fast) or not math.isfinite(ema_slow) or ema_slow == 0:
            raw["reason"] = "non_finite_emas"
            return "range_bound", raw

        spread_pct = (ema_fast - ema_slow) / ema_slow * 100.0
        raw["spread_pct"] = spread_pct
        if abs(spread_pct) < 0.5:
            return "range_bound", raw
        return ("bullish" if spread_pct > 0 else "bearish"), raw
    except Exception as exc:
        logger.warning("trend classification failed: %s", exc)
        raw["error"] = str(exc)
        return "range_bound", raw


# ---------------------------------------------------------------------------
# Volatility (India VIX bucketing)
# ---------------------------------------------------------------------------

_VIX_BUCKETS = (
    (12.0, "low"),
    (18.0, "medium"),
    (25.0, "high"),
)


def _classify_volatility() -> tuple[str, dict[str, Any]]:
    """Bucket the latest India VIX reading.

    v1 reads ``REGIME_VIX_FALLBACK`` from the env (default ``"medium"``)
    because ``INDIAVIX`` isn't on the WebSocket subscription list yet.

    TODO(stage-1.7-followup): subscribe to INDIAVIX (NSE_INDEX) in the
    broker WS adapter, then read the last quote here instead of the
    fallback. Surface ``REGIME_VIX_SYMBOL`` env if multiple brokers
    require different symbol strings.
    """
    fallback = os.getenv("REGIME_VIX_FALLBACK", "medium").lower()
    if fallback not in {"low", "medium", "high", "extreme"}:
        fallback = "medium"
    raw: dict[str, Any] = {
        "source": "env_fallback",
        "fallback_label": fallback,
    }
    return fallback, raw


# ---------------------------------------------------------------------------
# Breadth (% of F&O universe above 20-day MA)
# ---------------------------------------------------------------------------

_BREADTH_PERIOD = 20
_BREADTH_EXCHANGE = "NSE"


def _classify_breadth(
    *,
    universe_loader=None,
    bars_loader=None,
) -> tuple[str, dict[str, Any]]:
    """Compute breadth as the share of universe symbols whose last close
    is above their 20-day SMA.

    ``universe_loader`` and ``bars_loader`` are injected for tests. In
    production they default to:

    * ``universe_loader`` — read from ``REGIME_BREADTH_UNIVERSE`` env
      (comma-separated symbols). Empty universe returns ``mixed``.
    * ``bars_loader`` — wraps :func:`database.historify_db.get_ohlcv`
      for the daily interval.

    TODO(stage-1.7-followup): wire ``REGIME_BREADTH_UNIVERSE`` to the
    canonical F&O list (the same list the simplified engine uses) once
    that's exposed as a constant — today it's only checked in as
    ``_fno_universe.txt`` ad-hoc.
    """
    universe = (universe_loader or _default_breadth_universe)()
    raw: dict[str, Any] = {
        "period": _BREADTH_PERIOD,
        "universe_size": len(universe),
        "above_count": 0,
        "evaluated": 0,
    }
    if not universe:
        raw["reason"] = "empty_universe"
        return "mixed", raw

    loader = bars_loader or _default_breadth_bars_loader

    above = 0
    evaluated = 0
    for symbol in universe:
        try:
            df = loader(symbol, _BREADTH_EXCHANGE, "D")
        except Exception as exc:
            logger.debug("breadth loader failed for %s: %s", symbol, exc)
            continue
        if df is None or df.empty or len(df) < _BREADTH_PERIOD:
            continue
        evaluated += 1
        sma = df["close"].astype(float).rolling(_BREADTH_PERIOD).mean().iloc[-1]
        last = float(df["close"].iloc[-1])
        if math.isfinite(sma) and last > sma:
            above += 1

    raw["above_count"] = above
    raw["evaluated"] = evaluated
    if evaluated == 0:
        raw["reason"] = "no_evaluable_symbols"
        return "mixed", raw

    pct = above / evaluated * 100.0
    raw["pct_above_ma"] = pct
    if pct > 65.0:
        return "wide", raw
    if pct < 35.0:
        return "narrow", raw
    return "mixed", raw


def _default_breadth_universe() -> list[str]:
    raw = os.getenv("REGIME_BREADTH_UNIVERSE", "").strip()
    if not raw:
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _default_breadth_bars_loader(symbol: str, exchange: str, interval: str):
    from database.historify_db import get_ohlcv

    return get_ohlcv(symbol, exchange, interval)


# ---------------------------------------------------------------------------
# Sector rotation
# ---------------------------------------------------------------------------

_SECTOR_EXCHANGE = "NSE_INDEX"


def _default_sector_symbols() -> list[str]:
    """Comma-separated env override; default empty list (graceful degrade)."""
    raw = os.getenv("REGIME_SECTOR_SYMBOLS", "").strip()
    if not raw:
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _live_sector_quote_pct(symbol: str) -> float | None:
    """% change vs prev_close from a live broker REST quote.

    Returns None on any failure (no api key, broker error, no prev_close).
    Mirrors the pattern used by ``signal_review_service._fetch_nifty_pct``
    so a single working broker session feeds both.
    """
    try:
        from database.auth_db import get_first_available_api_key
        from services.quotes_service import get_quotes

        api_key = get_first_available_api_key()
        if not api_key:
            return None
        success, response, _ = get_quotes(
            symbol=symbol, exchange=_SECTOR_EXCHANGE, api_key=api_key
        )
        if not success:
            return None
        data = response.get("data") or {}
        ltp = float(data.get("ltp") or 0.0)
        prev_close = float(data.get("prev_close") or 0.0)
        if prev_close == 0.0:
            return None
        return (ltp - prev_close) / prev_close * 100.0
    except Exception as exc:
        logger.debug("sector live quote failed for %s: %s", symbol, exc)
        return None


def _historify_sector_pct(symbol: str) -> float | None:
    """% change between the last two daily closes from historify cache.

    Useful at boot when WS hasn't connected yet — returns yesterday's
    close-to-close change for the sector. Returns None when fewer than
    two bars are available.
    """
    try:
        from database.historify_db import get_ohlcv

        df = get_ohlcv(symbol, _SECTOR_EXCHANGE, "D")
        if df is None or df.empty or len(df) < 2:
            return None
        last = float(df["close"].iloc[-1])
        prev = float(df["close"].iloc[-2])
        if prev == 0.0:
            return None
        return (last - prev) / prev * 100.0
    except Exception as exc:
        logger.debug("sector historify fallback failed for %s: %s", symbol, exc)
        return None


def _classify_sector_rotation(
    *,
    symbols_loader=None,
    live_quote_fn=None,
    historify_fn=None,
) -> tuple[list[str], float, dict[str, Any]]:
    """Rank tracked NIFTY sector indices by % change and return the leaders.

    Returns ``(sector_leaders, concentration, raw_metrics)``.

    Data source priority per symbol:

    1. Live broker REST quote (``services.quotes_service.get_quotes``)
       — % change vs ``prev_close``.
    2. Historify daily store — close-to-close % change between the two
       most recent bars. Used at boot or when WS isn't up.
    3. Omitted from the ranking if neither source has data.

    Concentration metric:
    ``max(0.0, top_pct - median_pct) / (|top_pct| + 0.01)``. Values >0.5
    indicate a dominant leader; values <0.2 indicate broad rotation.
    Empty when fewer than two sectors have data.

    All loaders are injected for tests; production callers pass nothing.
    """
    symbols = (symbols_loader or _default_sector_symbols)()
    raw: dict[str, Any] = {
        "universe": symbols,
        "live_count": 0,
        "historify_count": 0,
        "missing_count": 0,
        "sector_pct": {},
        "source_per_symbol": {},
    }
    if not symbols:
        raw["reason"] = "empty_universe"
        return [], 0.0, raw

    live_fn = live_quote_fn or _live_sector_quote_pct
    hist_fn = historify_fn or _historify_sector_pct

    pct_by_symbol: dict[str, float] = {}
    for sym in symbols:
        pct = live_fn(sym)
        if pct is not None:
            pct_by_symbol[sym] = pct
            raw["live_count"] += 1
            raw["source_per_symbol"][sym] = "live"
            continue
        pct = hist_fn(sym)
        if pct is not None:
            pct_by_symbol[sym] = pct
            raw["historify_count"] += 1
            raw["source_per_symbol"][sym] = "historify"
            continue
        raw["missing_count"] += 1
        raw["source_per_symbol"][sym] = "missing"

    if not pct_by_symbol:
        raw["reason"] = "no_data"
        return [], 0.0, raw

    raw["sector_pct"] = dict(pct_by_symbol)

    ranked = sorted(pct_by_symbol.items(), key=lambda kv: kv[1], reverse=True)
    leaders = [sym for sym, _ in ranked[:3]]

    if len(ranked) >= 2:
        top_pct = ranked[0][1]
        median_pct = ranked[len(ranked) // 2][1]
        concentration = max(0.0, top_pct - median_pct) / (abs(top_pct) + 0.01)
    else:
        concentration = 0.0

    raw["leaders"] = leaders
    raw["concentration"] = concentration
    return leaders, concentration, raw


# ---------------------------------------------------------------------------
# Top-level compute / cache
# ---------------------------------------------------------------------------


def compute_current_regime(
    *,
    now: datetime | None = None,
    universe_loader=None,
    bars_loader=None,
) -> MarketRegime:
    """Compute the current regime end-to-end.

    Test injection points are ``now`` (IST datetime override) and the
    breadth loaders. Production callers pass nothing.
    """
    ts = now if now is not None else datetime.now(IST)
    if ts.tzinfo is None:
        ts = IST.localize(ts)

    trend, trend_raw = _classify_trend()
    vol, vol_raw = _classify_volatility()
    breadth, breadth_raw = _classify_breadth(
        universe_loader=universe_loader,
        bars_loader=bars_loader,
    )
    leaders, concentration, sector_raw = _classify_sector_rotation()
    tod = _classify_time_of_day(ts)

    return MarketRegime(
        timestamp=ts,
        trend=trend,
        volatility=vol,
        breadth=breadth,
        sector_leaders=leaders,
        sector_leader_concentration=concentration,
        time_of_day=tod,
        raw_metrics={
            "trend": trend_raw,
            "volatility": vol_raw,
            "breadth": breadth_raw,
            "sector_rotation": sector_raw,
            "time_of_day": {"now_ist": ts.isoformat()},
        },
    )


# Module-level cache. Reads/writes are guarded by ``_cache_lock``. We
# keep this tiny on purpose — the regime is cheap enough to recompute
# every few minutes; the cache exists so a tight loop in the activator
# doesn't re-read duckdb on every call.
_cache_lock = threading.Lock()
_cached_regime: MarketRegime | None = None


def get_cached_regime(max_age_minutes: int = 5) -> MarketRegime | None:
    """Return a cached :class:`MarketRegime` if it's fresh enough,
    otherwise recompute, refresh the cache, and return that.

    Returns ``None`` only if compute itself fails — a defensive return
    so callers can degrade gracefully rather than raise mid-tick.
    """
    global _cached_regime
    cutoff = datetime.now(IST) - timedelta(minutes=max_age_minutes)
    with _cache_lock:
        if _cached_regime is not None and _cached_regime.timestamp >= cutoff:
            return _cached_regime
    try:
        regime = compute_current_regime()
    except Exception as exc:
        logger.exception("compute_current_regime failed: %s", exc)
        return None
    with _cache_lock:
        _cached_regime = regime
    return regime


def reset_cache() -> None:
    """Drop the cached regime — used by tests."""
    global _cached_regime
    with _cache_lock:
        _cached_regime = None


# ---------------------------------------------------------------------------
# Persistence (market_intel sidecar)
# ---------------------------------------------------------------------------

_init_lock = threading.Lock()
_init_done = False


def _ensure_intel_table() -> None:
    """Lazy init of the ``market_intel`` table. Idempotent."""
    global _init_done
    if _init_done:
        return
    with _init_lock:
        if _init_done:
            return
        from database.market_intel_db import init_db

        init_db()
        _init_done = True


def log_regime_snapshot(regime: MarketRegime | None = None) -> int | None:
    """Persist a regime snapshot to ``market_intel``. Returns the new
    row id or ``None`` on failure.

    If ``regime`` is omitted, the cached regime is used; if there's no
    cached value, the regime is computed first.
    """
    if regime is None:
        regime = get_cached_regime(max_age_minutes=5)
    if regime is None:
        return None
    try:
        _ensure_intel_table()
        from database.market_intel_db import insert_intel

        return insert_intel(
            kind="regime",
            payload_json=json.dumps(regime.to_payload()),
            captured_at=regime.timestamp.isoformat(),
        )
    except Exception as exc:
        logger.exception("failed to persist regime snapshot: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Operator opt-in note
# ---------------------------------------------------------------------------
#
# To gate a strategy by regime, two changes are needed (operator action,
# NOT part of this commit):
#
# 1. On the strategy class, set ``regime_profile``:
#
#        from strategies.base import RegimeProfile
#        class MyStrategy(BaseStrategy):
#            regime_profile = RegimeProfile.of(
#                trend={"bullish"},
#                volatility={"low", "medium"},
#            )
#
# 2. At the engine entry-decision site (likely
#    ``services.simplified_stock_engine_service``), call:
#
#        from services.strategy_activator_service import is_strategy_active_now
#        allowed, reason = is_strategy_active_now(strategy.name)
#        if not allowed:
#            logger.info("entry skipped: %s", reason)
#            return
#
# That's the entire opt-in. Until step 2 lands, this scaffold computes
# regime data but never blocks an entry.
