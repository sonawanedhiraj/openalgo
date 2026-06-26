"""Boot-time scanner aggregator seeding (issue #156 Phase 2 / R3).

Why this file exists
--------------------
On every restart, the scanner's ``MultiIntervalAggregator`` starts empty.
The aggregator only accumulates bars from live WS ticks, so the first
several 5m bar closes after restart have ``bar_count`` below the
indicator window:

* RSI(14) needs 14 bars (~70 minutes of trading)
* SMA(20) needs 20 bars (~100 minutes)

For the first ~100 minutes, every scan rule call hits
``pandas_ta_classic.utils._core.verify_series`` which logs a WARNING per
indicator per symbol per bar close and returns ``None``. The result on
2026-06-26: **25,272 warnings in a single 10-minute window** plus a quiet
100-minute period where the scanner produces no signals — looks identical
to "no setups" but is actually "warming up empty".

The fix is to **seed the aggregator's rolling state at boot** from the
historical bars OpenAlgo already has in ``historify.duckdb``. The scanner
runs against a pre-warmed window the moment the first live tick lands.

Implementation outline
----------------------
1. Wait for a broker session to come up (mirrors the sector_follow boot
   convergence pattern — no point seeding if we can't trade either).
2. For each symbol in the aggregator's configured set, read the last N
   minutes of 1m bars from ``historify.duckdb`` (default 500 min = ~100
   5m bars, comfortable headroom over SMA(20)).
3. Call ``aggregator.replay_bars(symbol, bars)`` to fold them in. The
   aggregator's ``replay_bars`` is idempotent (dedups by timestamp) so a
   subsequent ws_recovery replay never double-counts.
4. Telegram a single completion summary — symbols seeded, total bars
   folded, average bars per symbol, any symbols that came back empty.

Gating + safety
---------------
* Master flag ``SCANNER_AGGREGATOR_SEED_ENABLED`` (default ``true``).
  Flip off if a future bug surfaces; the legacy warm-up-from-empty
  behaviour is preserved.
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
the same way ``ws_recovery_service`` does after a reconnect.
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


def _read_1m_bars_for_symbol(symbol: str, exchange: str, lookback_min: int) -> list[dict]:
    """Read the last ``lookback_min`` 1m bars for ``symbol`` from historify.

    Returns a list of ``{ts, open, high, low, close, volume}`` records in the
    shape ``MultiIntervalAggregator.replay_bars`` expects (ts is a naive
    datetime in IST — same convention as the live tick path).

    Never raises — a failed read returns ``[]`` so the aggregator slot stays
    empty (= today's pre-seeding behaviour for that symbol). Exceptions are
    logged via ``logger.exception``.
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

    Pure function — no env reads beyond the lookback. Returns a summary dict
    for the caller to log/Telegram:
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
    seeded = 0
    empty: list[str] = []
    total_bars = 0
    errors = 0

    for sym in symbols:
        exch = _resolve_exchange_for_symbol(sym)
        bars = _read_1m_bars_for_symbol(sym, exch, lookback)
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
