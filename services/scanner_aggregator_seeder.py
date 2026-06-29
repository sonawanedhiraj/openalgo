"""Boot-time scanner aggregator seeding (issue #156 Phase 2 / R3; #199).

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
  per symbol; trimmed to that many bars before fold.
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
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Defaults — overridable via env.
_DEFAULT_LOOKBACK_MIN = 500
_DEFAULT_TIMEOUT_SEC = 90
_DEFAULT_POLL_SEC = 5
# Resolution of how often we re-check broker session readiness while waiting.


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
    """Read the last ``lookback_min`` 1m bars for ``symbol`` from historify.

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
    start_ts = int((now - timedelta(minutes=lookback_min)).timestamp())

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


def _read_1m_bars_from_broker(
    symbol: str, exchange: str, lookback_min: int, api_key: str
) -> list[dict]:
    """Fetch the last ``lookback_min`` 1m bars from the broker historical API.

    Mirrors ``services.ws_recovery_service._default_history_fetcher`` but spans
    yesterday + today so an early-session restart still has enough history to
    clear the 15m RSI(14) warm-up (which needs ~3h30min = 210 1m bars). The
    broker API enforces its own ~3 req/sec rate limit; the caller paces by
    iterating symbols sequentially.

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
    # 2-calendar-day window covers any restart time + the requested lookback.
    # ``get_history`` returns the broker's intraday + prior-day 1m series; we
    # trim to the last ``lookback_min`` bars below.
    start_date = (now - timedelta(days=2)).strftime("%Y-%m-%d")
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
    if len(broker_bars) > len(historify_bars):
        logger.info(
            "aggregator_seeder: %s — historify had %d bars (<%d), broker fallback returned %d",
            symbol,
            len(historify_bars),
            min_required,
            len(broker_bars),
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


def seed_aggregator(aggregator: Any, symbols: list[str]) -> dict:
    """Seed ``aggregator`` with historical 1m bars for ``symbols``.

    Reads ``SCANNER_AGGREGATOR_SEED_LOOKBACK_MIN`` and the broker-fallback flag
    once at the top. Resolves the broker API key once (used by the fallback
    arm). Returns a summary dict for the caller to log/Telegram:
    ``{"seeded_symbols": N, "empty_symbols": [...], "total_bars": M,
       "avg_bars_per_symbol": M/N, "errors": E}``.

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
        }
    if not symbols:
        return {
            "seeded_symbols": 0,
            "empty_symbols": [],
            "total_bars": 0,
            "avg_bars_per_symbol": 0.0,
            "errors": 0,
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

    avg = (total_bars / seeded) if seeded else 0.0
    return {
        "seeded_symbols": seeded,
        "empty_symbols": empty,
        "total_bars": total_bars,
        "avg_bars_per_symbol": round(avg, 1),
        "errors": errors,
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


def _boot_worker(aggregator: Any, symbols: list[str]) -> None:
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
    summary = seed_aggregator(aggregator, symbols)
    elapsed = _time.monotonic() - start_ts
    summary["elapsed_sec"] = round(elapsed, 1)

    logger.info(
        "aggregator_seeder: seeded %d/%d symbols, %d bars total (avg %.1f/symbol, %d empty, %d errors) in %.1fs",
        summary["seeded_symbols"],
        len(symbols),
        summary["total_bars"],
        summary["avg_bars_per_symbol"],
        len(summary["empty_symbols"]),
        summary["errors"],
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
        f"{summary['total_bars']} bars ({summary['avg_bars_per_symbol']:.0f}/symbol avg) in "
        f"{elapsed:.1f}s. Empty: {len(summary['empty_symbols'])}. Errors: {summary['errors']}."
    )


def init_scanner_aggregator_seeder(aggregator: Any, symbols: list[str]) -> None:
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
        args=(aggregator, list(symbols)),
        daemon=True,
        name="ScannerAggregatorSeed",
    ).start()
    logger.info("aggregator_seeder: boot daemon launched (%d symbols)", len(symbols))
