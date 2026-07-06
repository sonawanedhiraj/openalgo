"""Boot-time scanner aggregator seeding (issue #156 Phase 2 / R3; #199; #340).

Why this file exists
--------------------
On every restart, the scanner's ``MultiIntervalAggregator`` starts empty.
The aggregator only accumulates bars from live WS ticks, so the first
several 5m bar closes after restart have ``bar_count`` below the
indicator window:

* RSI(14) needs 14 bars (~70 minutes of trading)
* SMA(20) needs 20 bars (~100 minutes)
* 15m RSI(14) needs 14 fifteen-minute bars (~3h 30min of trading)

For the first ~100 minutes after a mid-session restart, every scan rule
call rejects at the warm-up guard (`len(bars_5m) < 8` /
`len(bars_15m) < 15`). For the first 3h 30min, the 15m RSI gate can't
evaluate at all. The scanner produces no signals — looks identical to
"no setups" but is actually "warming up empty".

The fix is to **seed the aggregator's rolling state at boot** from
historical 1m bars. The scanner runs against a pre-warmed window the
moment the first live tick lands.

Session-aware lookback (issue #340)
------------------------------------
The lookback window used to be computed as raw WALL-CLOCK minutes
(``now - lookback_min``). That is fine for a mid-session restart (most of
the window is inside today's live session) but is silently empty for a
PRE-MARKET boot: at 08:26 IST with the default 500-minute lookback,
``now - 500min`` lands at ~00:06 IST — a window with zero trading bars.
This produced the 2026-07-06 incident: the seeder recorded 0/227 symbols
seeded, and the 15m RSI warm-up gate then rejected the ENTIRE universe
until enough LIVE 15m bars accumulated intra-session (~13:00 IST for a
09:15 open). ``session_aware_start_ts`` fixes this by walking backward
through TRADING SESSIONS (09:15-15:30 IST, weekdays only — holidays are
not modelled, matching ``services.data_freshness_service``'s existing
business-day convention) instead of raw wall-clock time, so
``SCANNER_AGGREGATOR_SEED_LOOKBACK_MIN`` now means **trading minutes**,
not wall-clock minutes. The unchanged default of 500 trading minutes
yields ~33 closed 15m bars (well above the 15-bar RSI(14)+margin floor)
regardless of what time of day the seeder runs.

Data source (issue #199)
------------------------
**Two-tier read with broker fallback.** The seeder first reads from
``historify.duckdb`` (fast, free, no rate limit). For symbols where
historify has insufficient bars (the scanner universe is ~227 symbols
but only the sector_follow subset's ~30 stocks + a few indices have
recent 1m bars in historify during market hours — the scanner-side
1m backfill only runs in the 15:30-17:00 IST window), it falls back to
the broker's historical API via ``services.history_service.get_history``.
The broker fetch is rate-limited (~3 req/sec) so 227 symbols take ~75s.

Implementation outline
----------------------
1. Wait for a broker session to come up (mirrors the sector_follow boot
   convergence pattern — no point seeding if we can't trade either).
2. For each symbol in the aggregator's configured set:
   a. Read the last N minutes of 1m bars from ``historify.duckdb``.
   b. If historify returned < N/3 bars, fall back to the broker API
      (yesterday + today, trimmed to the requested lookback).
3. Call ``aggregator.replay_bars(symbol, bars)`` to fold them in. The
   aggregator's ``replay_bars`` is idempotent (dedups by timestamp) so a
   subsequent ws_recovery replay never double-counts.
4. Telegram a single completion summary — symbols seeded (with the
   per-source breakdown), total bars folded, average bars per symbol,
   empty symbols, errors.

Gating + safety
---------------
* Master flag ``SCANNER_AGGREGATOR_SEED_ENABLED`` (default ``true``).
  Flip off if a future bug surfaces; the legacy warm-up-from-empty
  behaviour is preserved.
* ``SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED`` (default ``true``,
  issue #199) — independently gates only the broker-fetch arm so the
  operator can disable broker calls without losing historify seeding.
* Per-symbol fetch failures are logged via ``logger.exception`` and
  skipped — never all-or-nothing. The aggregator's slot for that symbol
  stays empty, which is exactly today's behaviour.
* ``SCANNER_AGGREGATOR_SEED_LOOKBACK_MIN`` (default 500) — how far back
  per symbol, in TRADING minutes (issue #340 — session-aware, not
  wall-clock); trimmed to that many bars before fold.
* ``SCANNER_AGGREGATOR_SEED_TIMEOUT_SEC`` (default 90) — bounded wait for
  the broker session; if missed, seeder logs a warning and exits without
  seeding (aggregator warms up from empty — pre-#156 behaviour).
* Runs on a daemon thread so the boot path is never blocked.

This is **additive** — the seeder doesn't change any existing aggregator
or scanner code; it just calls ``replay_bars`` with bars from historify
or the broker the same way ``ws_recovery_service`` does after a reconnect.
"""

from __future__ import annotations

import os
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Defaults — overridable via env.
_DEFAULT_LOOKBACK_MIN = 500
_DEFAULT_TIMEOUT_SEC = 90
_DEFAULT_POLL_SEC = 5
# Resolution of how often we re-check broker session readiness while waiting.

# --------------------------------------------------------------------------- #
# Session-aware lookback (issue #340)
# --------------------------------------------------------------------------- #
# The NSE cash session is 09:15-15:30 IST = 375 minutes/day. A wall-clock
# ``now - lookback_min`` window (the pre-#340 behaviour) is fine for an
# afternoon restart (the window is mostly inside today's live session) but
# is CATASTROPHIC for a pre-market boot: at 08:26 IST with the default
# lookback of 500 minutes, ``now - 500min`` lands at ~00:06 IST — a window
# that contains ZERO trading bars. The seeder recorded 0/227 symbols seeded
# on the 2026-07-06 boot for exactly this reason (see issue #340 investigation
# notes). The fix walks backward through TRADING SESSIONS (weekdays only —
# holidays are not modelled, matching the existing business-day convention in
# ``services.data_freshness_service``) rather than raw wall-clock minutes, so
# the requested number of *trading* minutes is always available regardless of
# what time of day (or how early in the morning) the seeder runs.
_SESSION_START = (9, 15)
_SESSION_END = (15, 30)
_SESSION_MINUTES = (15 * 60 + 30) - (9 * 60 + 15)  # 375


def _is_trading_day(d: date) -> bool:
    """Weekday check only — holidays are not modelled (existing convention)."""
    return d.weekday() < 5  # 0=Mon .. 4=Fri


def _session_bounds(d: date) -> tuple[datetime, datetime]:
    """Naive-IST (09:15, 15:30) datetimes for trading day ``d``."""
    start = datetime(d.year, d.month, d.day, *_SESSION_START)
    end = datetime(d.year, d.month, d.day, *_SESSION_END)
    return start, end


def session_aware_start_ts(now: datetime, lookback_min: int) -> int:
    """Compute the epoch ``start_ts`` that guarantees ``lookback_min`` TRADING
    minutes of history ending at ``now`` (issue #340).

    Walks backward day-by-day from ``now``'s IST calendar date, accumulating
    each trading day's session minutes (weekdays only) until the requested
    total is reached, then returns the epoch second inside that day's session
    where the accumulated total first satisfies the request. This means:

    * A pre-market boot (before 09:15 IST) contributes 0 minutes from "today"
      and reaches all the way back into the prior trading session(s) — the
      exact case that produced 0 seeded bars on 2026-07-06.
    * A mid-session restart contributes today's elapsed session minutes first,
      then reaches back into prior day(s) only for the remainder — so a
      mid-session restart still gets "today's earlier bars + prior day" in one
      contiguous request, same as the historify/broker read already returns
      (both simply return "everything between start_ts and end_ts").
    * Weekends/holidays are skipped entirely (weekday-only, matching
      ``data_freshness_service._prev_or_same_business_day``).

    Never raises. Falls back to the pre-#340 wall-clock computation if given
    a degenerate ``lookback_min`` (<=0).
    """
    if lookback_min <= 0:
        return int((now - timedelta(minutes=500)).timestamp())

    remaining = lookback_min
    cursor_date = now.date()
    # How many minutes of TODAY's session have elapsed as of `now`?
    today_start, today_end = _session_bounds(cursor_date)
    if _is_trading_day(cursor_date) and now > today_start:
        today_elapsed = (min(now, today_end) - today_start).total_seconds() / 60.0
        today_elapsed = max(0.0, today_elapsed)
    else:
        today_elapsed = 0.0

    if today_elapsed >= remaining:
        # All of what we need is inside today's already-elapsed session.
        start_dt = today_start + timedelta(minutes=today_elapsed - remaining)
        return int(start_dt.replace(tzinfo=_IST).timestamp())

    remaining -= today_elapsed

    # Walk backward through prior trading days until `remaining` is covered.
    prior_date = cursor_date - timedelta(days=1)
    # Safety valve: never walk back more than ~30 calendar days (covers any
    # holiday cluster) so a pathological lookback can't loop indefinitely.
    for _ in range(30):
        if _is_trading_day(prior_date):
            if remaining <= _SESSION_MINUTES:
                day_start, day_end = _session_bounds(prior_date)
                start_dt = day_end - timedelta(minutes=remaining)
                return int(start_dt.replace(tzinfo=_IST).timestamp())
            remaining -= _SESSION_MINUTES
        prior_date -= timedelta(days=1)

    # Exhausted the safety valve (pathological input) — fall back to a wide
    # wall-clock window rather than raising.
    return int((now - timedelta(minutes=lookback_min * 3)).timestamp())


def _flag_enabled() -> bool:
    return os.environ.get("SCANNER_AGGREGATOR_SEED_ENABLED", "true").lower() == "true"


def _lookback_min() -> int:
    try:
        return max(
            60,
            int(os.environ.get("SCANNER_AGGREGATOR_SEED_LOOKBACK_MIN", str(_DEFAULT_LOOKBACK_MIN))),
        )
    except (TypeError, ValueError):
        return _DEFAULT_LOOKBACK_MIN


def _timeout_sec() -> int:
    try:
        return max(
            10,
            int(os.environ.get("SCANNER_AGGREGATOR_SEED_TIMEOUT_SEC", str(_DEFAULT_TIMEOUT_SEC))),
        )
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SEC


def _broker_fallback_enabled() -> bool:
    """Issue #199 — independently gate the broker-fetch arm."""
    return (
        os.environ.get("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true").lower() == "true"
    )


def _get_api_key() -> str | None:
    """Resolve an OpenAlgo API key for broker history calls. Never raises."""
    try:
        from database.auth_db import get_first_available_api_key

        return get_first_available_api_key()
    except Exception:
        logger.exception("aggregator_seeder: failed to resolve API key")
        return None


def _read_1m_bars_from_historify(symbol: str, exchange: str, lookback_min: int) -> list[dict]:
    """Read the last ``lookback_min`` TRADING minutes of 1m bars for ``symbol``
    from historify (issue #340 — session-aware, not wall-clock).

    Returns a list of ``{ts, open, high, low, close, volume}`` records in the
    shape ``MultiIntervalAggregator.replay_bars`` expects (ts is a naive
    datetime in IST — same convention as the live tick path).

    Never raises — a failed read returns ``[]``. Exceptions are logged via
    ``logger.exception``.
    """
    try:
        from database.historify_db import get_ohlcv
    except Exception:
        logger.exception("aggregator_seeder: failed to import historify_db — seeding disabled")
        return []

    now = datetime.now(_IST)
    end_ts = int(now.timestamp())
    # Issue #340: session-aware window (walks back through trading sessions)
    # instead of a raw wall-clock ``now - lookback_min`` — the latter produces
    # a zero-bar window on a pre-market boot.
    start_ts = session_aware_start_ts(now.replace(tzinfo=None), lookback_min)

    try:
        df = get_ohlcv(symbol, exchange, "1m", start_timestamp=start_ts, end_timestamp=end_ts)
    except Exception:
        logger.exception(
            "aggregator_seeder: get_ohlcv raised for %s/%s — skipping",
            symbol,
            exchange,
        )
        return []

    if df is None or df.empty:
        return []

    bars: list[dict] = []
    for _i, row in df.iterrows():
        ts_value = row.get("timestamp") if hasattr(row, "get") else row["timestamp"]
        # historify stores epoch seconds; replay_bars wants a datetime.
        try:
            ts_dt = (
                datetime.fromtimestamp(int(ts_value), tz=_IST).replace(tzinfo=None)
                if not isinstance(ts_value, datetime)
                else ts_value
            )
        except (TypeError, ValueError, OSError):
            continue
        try:
            bars.append(
                {
                    "ts": ts_dt,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row.get("volume") or 0)
                    if hasattr(row, "get")
                    else int(row["volume"] or 0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue

    return bars


def _calendar_days_for_lookback(lookback_min: int) -> int:
    """How many CALENDAR days back the broker fetch window must span to be
    certain of covering ``lookback_min`` TRADING minutes (issue #340).

    The old fixed ``now - 2 calendar days`` window was sized for the
    ~3h30min (210min) 15m-RSI warm-up case, but silently under-covers any
    larger lookback (or a boot that lands right after a weekend/holiday
    cluster) — the fetch would return fewer than ``lookback_min`` bars and
    the count-based trim below has nothing to trim from. Sessions are
    ``_SESSION_MINUTES`` (375) minutes each and weekends contribute 0
    trading minutes, so the number of TRADING days needed is
    ``ceil(lookback_min / _SESSION_MINUTES)``. Calendar days are padded by a
    generous ``(trading_days // 5 + 1) * 2`` for weekends, plus 2 extra
    calendar days of slack for the current partial day + safety margin.
    """
    trading_days_needed = max(1, -(-lookback_min // _SESSION_MINUTES))  # ceil div
    weekend_padding = (trading_days_needed // 5 + 1) * 2
    return trading_days_needed + weekend_padding + 2


def _read_1m_bars_from_broker(
    symbol: str, exchange: str, lookback_min: int, api_key: str
) -> list[dict]:
    """Fetch the last ``lookback_min`` TRADING minutes of 1m bars from the
    broker historical API (issue #340 — session-aware sizing).

    Mirrors ``services.ws_recovery_service._default_history_fetcher`` but
    spans however many calendar days are needed to guarantee
    ``lookback_min`` trading minutes are available to trim from — not a
    fixed 2-day window (which under-covers any lookback bigger than the
    original 15m-RSI-warmup use case, and any boot that lands near a
    weekend/holiday cluster). The broker API enforces its own ~3 req/sec
    rate limit; the caller paces by iterating symbols sequentially.

    Never raises — failures are logged + return ``[]``.
    """
    try:
        from services.history_service import get_history
    except Exception:
        logger.exception(
            "aggregator_seeder: failed to import history_service — broker fallback disabled"
        )
        return []

    now = datetime.now(_IST)
    # Issue #340: window sized to the requested lookback (trading-minutes
    # aware), not a fixed 2 calendar days. ``get_history`` returns the
    # broker's full intraday + prior-day(s) 1m series; we trim to the last
    # ``lookback_min`` bars below (trimming by 1m-bar COUNT is inherently
    # trading-minute-aware — no calendar gaps are ever counted as bars).
    calendar_days_back = _calendar_days_for_lookback(lookback_min)
    start_date = (now - timedelta(days=calendar_days_back)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    try:
        success, payload, _code = get_history(
            symbol=symbol,
            exchange=exchange,
            interval="1m",
            start_date=start_date,
            end_date=end_date,
            api_key=api_key,
        )
    except Exception:
        logger.exception(
            "aggregator_seeder: broker get_history raised for %s/%s — skipping",
            symbol,
            exchange,
        )
        return []

    if not success:
        # Soft failure (e.g. token not found yet) — log at debug; the
        # per-symbol slot stays empty just like the historify path.
        logger.debug(
            "aggregator_seeder: broker get_history failed for %s/%s: %s",
            symbol,
            exchange,
            (payload or {}).get("message", "unknown"),
        )
        return []

    rows = (payload or {}).get("data") or []
    bars: list[dict] = []
    for row in rows:
        ts_value = row.get("timestamp")
        if ts_value is None:
            continue
        try:
            if isinstance(ts_value, datetime):
                # Strip tz so it matches the historify/live-tick naive-IST convention.
                ts_dt = ts_value.replace(tzinfo=None) if ts_value.tzinfo else ts_value
            else:
                ts_dt = datetime.fromtimestamp(int(ts_value), tz=_IST).replace(tzinfo=None)
        except (TypeError, ValueError, OSError):
            continue
        try:
            bars.append(
                {
                    "ts": ts_dt,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row.get("volume") or 0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue

    bars.sort(key=lambda b: b["ts"])
    return bars[-lookback_min:] if lookback_min else bars


def _read_1m_bars_for_symbol(
    symbol: str, exchange: str, lookback_min: int, api_key: str | None = None
) -> list[dict]:
    """Read the last ``lookback_min`` 1m bars for ``symbol``.

    Two-tier (issue #199):
      1. Try historify.duckdb (fast, free, no rate limit).
      2. If historify returned < ``lookback_min // 3`` bars AND the broker
         fallback is enabled AND an API key is available, fetch from the
         broker historical API and use that instead.

    Returns a list of ``{ts, open, high, low, close, volume}`` records in the
    shape ``MultiIntervalAggregator.replay_bars`` expects.

    Never raises — per-source failures fall through to the next tier.
    """
    historify_bars = _read_1m_bars_from_historify(symbol, exchange, lookback_min)
    min_required = max(60, lookback_min // 3)
    if len(historify_bars) >= min_required:
        return historify_bars

    if not _broker_fallback_enabled():
        return historify_bars

    key = api_key if api_key is not None else _get_api_key()
    if not key:
        return historify_bars

    broker_bars = _read_1m_bars_from_broker(symbol, exchange, lookback_min, key)
    # Issue #305: the broker series carries the symbol's REAL T-1 settled
    # close (the last bar dated before today) — record it in the reference
    # registry so the scanner can certify the rules' yest_d.close against it.
    # Recorded regardless of which source wins below: the cross-check is the
    # whole point when historify is the (possibly stale) chosen source.
    # Reuses this fetch — adds no broker API load. Never raises.
    if broker_bars:
        try:
            from services.scanner_reference_data import record_prev_close_from_bars

            record_prev_close_from_bars(symbol, broker_bars)
        except Exception:  # noqa: BLE001 — observability must never break the read
            logger.exception("aggregator_seeder: broker prev-close record failed for %s", symbol)
    if len(broker_bars) > len(historify_bars):
        logger.info(
            "aggregator_seeder: %s — historify had %d bars (<%d), broker fallback returned %d",
            symbol,
            len(historify_bars),
            min_required,
            len(broker_bars),
        )
        # Issue #231: alert the operator when the two sources disagree on
        # the most-recent overlapping bar's close. The fallback is *expected*
        # when historify is short; what is NOT expected is a >0.5% close
        # mismatch — that indicates a stale historify slot the seeder is now
        # silently replacing. The alert dedups per (service, symbol, day).
        if historify_bars and broker_bars:
            try:
                hist_close = float(historify_bars[-1].get("close"))
                brok_close = float(broker_bars[-1].get("close"))
                from services.source_divergence_alerts import check_and_alert

                check_and_alert(
                    service="aggregator_seeder",
                    symbol=symbol,
                    source_a_label="historify_last_close",
                    source_a_value=hist_close,
                    source_b_label="broker_last_close",
                    source_b_value=brok_close,
                )
            except Exception:  # noqa: BLE001 — observability must never break the read
                logger.exception(
                    "aggregator_seeder: divergence alert dispatch failed for %s", symbol
                )
        return broker_bars
    return historify_bars


def _resolve_exchange_for_symbol(symbol: str) -> str:
    """Look up the exchange for ``symbol`` using the scanner's resolver.

    Mirrors ``scanner_presubscribe.resolve_exchange_for_symbol`` so indices
    (NIFTYAUTO etc.) route to ``NSE_INDEX`` and stocks to ``NSE``. Falls
    back to ``NSE`` if the resolver is unavailable.
    """
    try:
        from services.scanner_presubscribe import resolve_exchange_for_symbol

        return resolve_exchange_for_symbol(symbol)
    except Exception:
        logger.exception(
            "aggregator_seeder: resolve_exchange_for_symbol failed for %s — defaulting to NSE",
            symbol,
        )
        return "NSE"


def _aggregate_1m_to_15m(bars_1m: list[dict]) -> list[dict]:
    """Group 1m bars into 15m OHLCV bars (issue #201).

    Each bucket = (ts.minute // 15). The bucket's ``ts`` is the bucket-start
    minute (e.g. 09:15, 09:30, ...). Only buckets with at least one 1m bar
    are emitted. Open = first 1m bar's open, close = last 1m bar's close,
    high = max, low = min, volume = sum.

    Returns bars sorted ascending by ``ts``. The closing bucket of the
    series may be partial — the caller decides whether to drop it (the
    scanner's live BarBuilder treats only ``elapsed_pct >= 1.0`` bars as
    closed; we mirror that by trimming the last bucket if it contains
    fewer than 15 1m rows).
    """
    if not bars_1m:
        return []

    buckets: dict = {}
    for b in bars_1m:
        ts = b.get("ts")
        if ts is None:
            continue
        # Bucket start = floor to 15-min boundary.
        try:
            bucket_minute = (ts.minute // 15) * 15
            bucket_ts = ts.replace(minute=bucket_minute, second=0, microsecond=0)
        except AttributeError:
            continue
        slot = buckets.setdefault(
            bucket_ts,
            {
                "ts": bucket_ts,
                "open": b.get("open"),
                "high": b.get("high"),
                "low": b.get("low"),
                "close": b.get("close"),
                "volume": 0,
                "_count": 0,
            },
        )
        # Track open from first 1m bar in the bucket (already set on
        # bucket creation), update close on every subsequent bar.
        slot["close"] = b.get("close")
        try:
            if b.get("high") is not None and (slot["high"] is None or b["high"] > slot["high"]):
                slot["high"] = b["high"]
            if b.get("low") is not None and (slot["low"] is None or b["low"] < slot["low"]):
                slot["low"] = b["low"]
            slot["volume"] += int(b.get("volume") or 0)
        except (TypeError, ValueError):
            pass
        slot["_count"] += 1

    # Sort buckets ascending; drop the closing bucket if it's partial (<15
    # 1m bars), mirroring the live BarBuilder's "only closed bars" guarantee.
    bars_15m = sorted(buckets.values(), key=lambda b: b["ts"])
    if bars_15m and bars_15m[-1]["_count"] < 15:
        bars_15m = bars_15m[:-1]
    for b in bars_15m:
        b.pop("_count", None)
    return bars_15m


def seed_aggregator(
    aggregator: Any,
    symbols: list[str],
    bar_15m_history: dict | None = None,
) -> dict:
    """Seed ``aggregator`` with historical 1m bars for ``symbols``, and
    optionally pre-warm each symbol's 15m bar deque (issue #201).

    Reads ``SCANNER_AGGREGATOR_SEED_LOOKBACK_MIN`` and the broker-fallback flag
    once at the top. Resolves the broker API key once (used by the fallback
    arm). When ``bar_15m_history`` is provided (the scanner's
    ``_bar_15m_history`` dict), this also aggregates the same 1m series into
    15m bars and calls ``_Rolling15mBars.seed_bars(bars_15m)`` so the rule's
    ``len(bars_15m) < 15`` warm-up guard clears on the first bar close after
    a mid-session restart.

    Returns a summary dict for the caller to log/Telegram:
    ``{"seeded_symbols": N, "empty_symbols": [...], "total_bars": M,
       "avg_bars_per_symbol": M/N, "errors": E, "seeded_15m_bars": K}``.

    Never raises. Per-symbol failures are caught and reported.
    """
    if aggregator is None:
        logger.warning("aggregator_seeder: aggregator is None — nothing to seed")
        return {
            "seeded_symbols": 0,
            "empty_symbols": [],
            "total_bars": 0,
            "avg_bars_per_symbol": 0.0,
            "errors": 0,
            "seeded_15m_bars": 0,
        }
    if not symbols:
        return {
            "seeded_symbols": 0,
            "empty_symbols": [],
            "total_bars": 0,
            "avg_bars_per_symbol": 0.0,
            "errors": 0,
            "seeded_15m_bars": 0,
        }

    lookback = _lookback_min()
    api_key = _get_api_key() if _broker_fallback_enabled() else None
    if _broker_fallback_enabled() and not api_key:
        logger.warning(
            "aggregator_seeder: broker fallback enabled but no API key resolved — "
            "broker arm will be skipped per symbol"
        )

    seeded = 0
    empty: list[str] = []
    total_bars = 0
    seeded_15m_bars = 0
    errors = 0

    for sym in symbols:
        exch = _resolve_exchange_for_symbol(sym)
        bars = _read_1m_bars_for_symbol(sym, exch, lookback, api_key=api_key)
        if not bars:
            empty.append(sym)
            continue
        try:
            n = aggregator.replay_bars(sym, bars)
        except Exception:
            logger.exception("aggregator_seeder: replay_bars raised for %s — skipping", sym)
            errors += 1
            continue
        if n > 0:
            seeded += 1
            total_bars += n

        # Issue #201: also seed the 15m bar deque from the same 1m series.
        if bar_15m_history is not None:
            roll15 = bar_15m_history.get(sym)
            if roll15 is not None:
                try:
                    bars_15m = _aggregate_1m_to_15m(bars)
                    if bars_15m:
                        seeded_15m_bars += roll15.seed_bars(bars_15m)
                except Exception:
                    logger.exception(
                        "aggregator_seeder: 15m seed raised for %s — skipping (5m seed still applied)",
                        sym,
                    )

    avg = (total_bars / seeded) if seeded else 0.0
    return {
        "seeded_symbols": seeded,
        "empty_symbols": empty,
        "total_bars": total_bars,
        "avg_bars_per_symbol": round(avg, 1),
        "errors": errors,
        "seeded_15m_bars": seeded_15m_bars,
    }


def _wait_for_broker_session(deadline_sec: int) -> bool:
    """Poll until the broker session is live or the deadline passes."""
    try:
        from services.broker_session_health import is_live_broker_session
    except Exception:
        logger.exception("aggregator_seeder: broker_session_health unavailable")
        return False

    deadline = _time.monotonic() + deadline_sec
    while _time.monotonic() < deadline:
        try:
            if is_live_broker_session():
                return True
        except Exception:
            logger.exception("aggregator_seeder: live session probe raised — retrying")
        _time.sleep(_DEFAULT_POLL_SEC)
    return False


def _notify(message: str) -> None:
    """Best-effort Telegram notify — failures are logged + swallowed."""
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("scanner_aggregator_seed", message)
    except Exception:
        logger.exception("aggregator_seeder: notify failed")


def _boot_worker(
    aggregator: Any,
    symbols: list[str],
    bar_15m_history: dict | None = None,
) -> None:
    """Boot daemon entry: wait for broker session, then seed."""
    if not _flag_enabled():
        logger.info("aggregator_seeder: disabled via SCANNER_AGGREGATOR_SEED_ENABLED=false")
        return
    if not symbols:
        logger.info("aggregator_seeder: empty symbol set — nothing to seed")
        return

    timeout = _timeout_sec()
    logger.info(
        "aggregator_seeder: waiting up to %ds for broker session "
        "(then will seed %d symbols from historify, lookback=%dmin)",
        timeout,
        len(symbols),
        _lookback_min(),
    )
    if not _wait_for_broker_session(timeout):
        logger.warning(
            "aggregator_seeder: no broker session after %ds — skipping seed (aggregator "
            "will warm up from empty; first ~%d min after restart will produce indicator "
            "warmup warnings, same as pre-#156 behaviour)",
            timeout,
            _lookback_min() // 5,
        )
        return

    start_ts = _time.monotonic()
    summary = seed_aggregator(aggregator, symbols, bar_15m_history=bar_15m_history)
    elapsed = _time.monotonic() - start_ts
    summary["elapsed_sec"] = round(elapsed, 1)

    logger.info(
        "aggregator_seeder: seeded %d/%d symbols, %d bars total (avg %.1f/symbol, %d empty, %d errors, %d 15m bars) in %.1fs",
        summary["seeded_symbols"],
        len(symbols),
        summary["total_bars"],
        summary["avg_bars_per_symbol"],
        len(summary["empty_symbols"]),
        summary["errors"],
        summary.get("seeded_15m_bars", 0),
        elapsed,
    )

    # One Telegram so the operator sees the boot outcome.
    icon = "📊"
    if summary["seeded_symbols"] == 0:
        icon = "⚠️"
    elif len(summary["empty_symbols"]) > 0.2 * len(symbols):
        icon = "⚠️"
    _notify(
        f"{icon} scanner aggregator seeded: "
        f"{summary['seeded_symbols']}/{len(symbols)} symbols, "
        f"{summary['total_bars']} bars ({summary['avg_bars_per_symbol']:.0f}/symbol avg, "
        f"{summary.get('seeded_15m_bars', 0)} 15m bars) in "
        f"{elapsed:.1f}s. Empty: {len(summary['empty_symbols'])}. Errors: {summary['errors']}."
    )


def init_scanner_aggregator_seeder(
    aggregator: Any,
    symbols: list[str],
    bar_15m_history: dict | None = None,
) -> None:
    """Boot entry — fires the seed on a daemon thread. Non-blocking.

    Call this from ``app.py`` right after ScannerService is constructed and
    started. The seeder will wait for the broker session (so historify reads
    against the correct user's API context) and then fold historical 1m bars
    into the aggregator's rolling state.

    Idempotent enough — if the boot fires twice somehow, the aggregator's
    own ``replay_bars`` dedups by ts so the second seed is a no-op.
    """
    threading.Thread(
        target=_boot_worker,
        args=(aggregator, list(symbols), bar_15m_history),
        daemon=True,
        name="ScannerAggregatorSeed",
    ).start()
    logger.info("aggregator_seeder: boot daemon launched (%d symbols)", len(symbols))
