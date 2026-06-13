"""Scanner-universe historical backfill (1m AND daily) for the in-house screener.

Background — the two supply bugs the 2026-06-13 Friday replay surfaced
----------------------------------------------------------------------
The in-house Chartink-equivalent screener evaluates BUY/SELL rules across the
full ``SCANNER_SYMBOLS`` F&O universe (~200 names interleaved with a handful of
``NSE_INDEX`` indices). Two persisted-data gaps left it running on incomplete
inputs, independent of the live tick path (see
``docs/research/strategy/screener/2026-06-13_friday_replay_with_backfilled_data.md``):

  * **Bug A — the scanner universe was never backfilled.** The
    ``sector_follow`` boot+periodic convergence service refreshes only
    ``sector_follow``'s locked-static-30 stocks + 8 sector indices. The
    scanner's own ``SCANNER_SYMBOLS`` list was *unfed* — Friday 1m existed for
    only 38 / 238 historify symbols, so a replay could reconstruct only 1 of
    Chartink's 8 Friday names.
  * **Bug B — the stored daily (``D``) interval was universally stale**
    (ending 2026-06-04 for 229 symbols). ``ScannerHistoryProvider`` sources its
    daily gates from the stored ``D`` interval via
    ``historify_db.get_ohlcv(interval='D')``, so on any given day the daily
    gap/volume gates were computed against ~6-trading-day-old bars — silently
    corrupting BUY/SELL selection regardless of tick health.

This module is the scanner-side analogue of
``services.sector_follow_index_backfill`` / ``...stock_backfill``: it keeps the
scanner's universe fresh in *both* storage intervals (``1m`` and ``D``) through
the same ``services.historify_service.create_and_start_job`` pipeline, so it is
purely **additive** — it never replaces or duplicates the watchlist download.

Three callers, mirroring the sector_follow backfills:

  * ``check_and_refresh_if_stale(today, interval=...)`` — the state-convergence
    entry used by the boot hook and the in-process periodic loop (see
    ``services.scanner_backfill_scheduler``). Reads MAX(timestamp) per symbol
    for the given interval from ``historify.duckdb`` and fetches only the
    symbols behind today's expected close. **Idempotent** (fresh → no-op),
    **fail-graceful** (a dead broker session is logged + reported, never raised).
  * ``refresh_scanner_universe(interval=...)`` — a thin lookback wrapper,
    retained as a convenience / programmatic entry (not registered on any
    scheduler).
  * a one-shot CLI to catch up a historical gap manually (e.g. the initial deep
    1m backfill for the ~200 scanner symbols that were never fetched)::

        uv run python -m services.scanner_universe_backfill \
            --from 2026-05-01 --to 2026-06-13 --interval 1m
        uv run python -m services.scanner_universe_backfill \
            --from 2026-06-04 --to 2026-06-13 --interval D

The convergence path only closes a *small* trailing gap (the daily lookback
windows below). The first deep backfill of never-fetched 1m history for the full
universe is a one-time CLI run; after that the boot+periodic check keeps it
current — exactly as documented for the sector_follow feeds.

Symbol set is derived live from the ``SCANNER_SYMBOLS`` env var (the same source
``ScannerHistoryProvider`` and ``scanner_presubscribe`` read), and each symbol is
routed to ``NSE`` or ``NSE_INDEX`` via
``services.scanner_presubscribe.resolve_exchange_for_symbol`` so the index names
interleaved in the universe download under the correct exchange.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

from utils.logging import get_logger

logger = get_logger(__name__)

# Only the two storage intervals are downloadable (5m/15m/… are computed from 1m
# on read — see services.historify_service). The scanner needs both: 1m for the
# intraday tape and D for ScannerHistoryProvider's daily gates.
STORAGE_INTERVALS = ("1m", "D")

# Small trailing lookback for the daily incremental refresh, per interval. 1m
# self-heals a missed run/weekend; D uses a wider window so a multi-day stored-D
# gap (Bug B left it ~6 trading days behind) closes on the first convergence run
# without needing the CLI. Deep history (never-fetched symbols) is a CLI job.
_LOOKBACK_DAYS = {"1m": 4, "D": 15}


def scanner_universe_symbols() -> list[str]:
    """Unique scanner-universe symbols to keep fresh, sorted for stability.

    Derived live from the ``SCANNER_SYMBOLS`` env var (comma-separated) — the
    same source ``services.scanner_history_provider.get_provider`` and
    ``services.scanner_presubscribe`` read, so the backfill universe tracks the
    scanner config automatically.
    """
    raw = os.getenv("SCANNER_SYMBOLS", "")
    return sorted({s.strip().upper() for s in raw.split(",") if s.strip()})


def _symbols_payload(symbols: list[str]) -> list[dict[str, str]]:
    """Build the per-symbol ``{symbol, exchange}`` payload, routing indices.

    The scanner universe interleaves a few ``NSE_INDEX`` indices with the NSE
    equities; ``create_and_start_job`` downloads each symbol under its own
    exchange, so a single mixed job is correct.
    """
    from services.scanner_presubscribe import resolve_exchange_for_symbol

    return [{"symbol": s, "exchange": resolve_exchange_for_symbol(s)} for s in symbols]


def backfill_scanner_universe(
    start_date: str,
    end_date: str,
    interval: str = "1m",
    api_key: str | None = None,
    symbols: list[str] | None = None,
) -> dict:
    """Download ``interval`` bars for the scanner universe over [start, end].

    Additive — routes through the historify job pipeline (incremental, so it
    only fetches the missing tail). Each symbol carries its own exchange so the
    interleaved indices download under ``NSE_INDEX``. Per-symbol failures (e.g.
    an expired broker session, or an index with no history) are handled inside
    ``create_and_start_job`` and never raise here.

    ``symbols`` restricts the fetch to a subset (used by the stale-check
    convergence path, which only re-fetches the symbols that are behind). When
    ``None`` the full ``SCANNER_SYMBOLS`` universe is fetched (lookback wrapper /
    CLI).

    Returns a small status dict: ``{status, symbols, interval, job_id?, message?}``.
    """
    from database.auth_db import get_first_available_api_key
    from services.historify_service import create_and_start_job

    if interval not in STORAGE_INTERVALS:
        return {
            "status": "error",
            "message": f"interval must be one of {STORAGE_INTERVALS}, got {interval!r}",
            "symbols": [],
            "interval": interval,
        }

    api_key = api_key or get_first_available_api_key()
    if not api_key:
        logger.error("scanner universe backfill: no API key available — skipping")
        return {
            "status": "error",
            "message": "no api key available",
            "symbols": [],
            "interval": interval,
        }

    syms = sorted(set(symbols)) if symbols is not None else scanner_universe_symbols()
    if not syms:
        logger.info("scanner universe backfill: empty universe (SCANNER_SYMBOLS unset) — skipping")
        return {"status": "ok", "symbols": [], "interval": interval, "message": "empty universe"}

    symbols_payload = _symbols_payload(syms)
    logger.info(
        "[scanner_universe_backfill] %d symbols %s..%s (%s)",
        len(symbols_payload),
        start_date,
        end_date,
        interval,
    )
    try:
        success, response, _status_code = create_and_start_job(
            job_type="scheduled",
            symbols=symbols_payload,
            interval=interval,
            start_date=start_date,
            end_date=end_date,
            api_key=api_key,
            config={"source": "scanner_universe_backfill"},
            incremental=True,
        )
    except Exception as e:  # never let a feed hiccup crash the caller
        logger.exception("[scanner_universe_backfill] failed to start: %s", e)
        return {"status": "error", "message": str(e), "symbols": syms, "interval": interval}

    if success:
        job_id = (response or {}).get("job_id")
        logger.info("[scanner_universe_backfill] started: job_id=%s (%s)", job_id, interval)
        return {"status": "success", "job_id": job_id, "symbols": syms, "interval": interval}

    msg = (response or {}).get("message", "unknown error")
    logger.error("[scanner_universe_backfill] rejected: %s", msg)
    return {"status": "error", "message": msg, "symbols": syms, "interval": interval}


def refresh_scanner_universe(interval: str = "1m") -> dict:
    """Lookback refresh for the scanner universe over a small trailing window.

    Convenience / programmatic entry (not registered on a scheduler — the boot +
    periodic ``check_and_refresh_if_stale`` path is the live caller). Uses a
    per-interval lookback so it self-heals a missed window.
    """
    lookback = _LOOKBACK_DAYS.get(interval, 4)
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=lookback)).strftime("%Y-%m-%d")
    logger.info(
        "[scanner_universe_backfill] %s lookback refresh starting (%s..%s)", interval, start, end
    )
    return backfill_scanner_universe(start, end, interval=interval)


def check_and_refresh_if_stale(
    today: date | None = None,
    *,
    interval: str = "1m",
    duckdb_path: str | None = None,
    max_staleness_business_days: int = 0,
) -> dict:
    """Refresh only the scanner symbols whose ``interval`` feed is behind today.

    The state-convergence entry for the boot hook + periodic loop. Reads
    MAX(timestamp) per symbol for ``interval`` from ``historify.duckdb``; any
    symbol more than ``max_staleness_business_days`` business days behind the
    latest trading day (default 0 — wants today's close) is queued for an
    incremental catch-up over the per-interval lookback window; the rest are
    skipped. **Idempotent** — when every symbol is fresh this is a no-op.
    **Fail-graceful** — a fetch failure (e.g. an expired broker session) is
    ``logger.exception``-logged and recorded in ``errors``, never raised.

    Returns ``{status, interval, stale_symbols, refreshed, errors, skipped_fresh}``.
    """
    from services.data_freshness_service import _DEFAULT_DUCKDB_PATH, compute_stale_symbols

    ref = today or date.today()
    path = duckdb_path or _DEFAULT_DUCKDB_PATH
    universe = scanner_universe_symbols()

    result: dict = {
        "status": "ok",
        "interval": interval,
        "stale_symbols": [],
        "refreshed": [],
        "errors": [],
        "skipped_fresh": [],
    }
    if not universe:
        logger.info("scanner universe stale-check: empty universe (SCANNER_SYMBOLS unset) — no-op")
        return result

    try:
        stale, fresh, _details = compute_stale_symbols(
            path,
            universe,
            today=ref,
            max_staleness_business_days=max_staleness_business_days,
            interval=interval,
        )
    except Exception as e:  # never let a freshness read crash the caller
        logger.exception(
            "scanner universe %s stale-check failed to read freshness: %s", interval, e
        )
        result["status"] = "error"
        result["errors"].append(f"freshness_read: {e}")
        return result

    result["stale_symbols"] = stale
    result["skipped_fresh"] = fresh
    if not stale:
        logger.info(
            "scanner universe %s feed fresh (%d symbols) — no refresh", interval, len(fresh)
        )
        return result

    lookback = _LOOKBACK_DAYS.get(interval, 4)
    end = ref.strftime("%Y-%m-%d")
    start = (ref - timedelta(days=lookback)).strftime("%Y-%m-%d")
    logger.info(
        "scanner universe %s feed stale: %d/%d behind — catching up %s..%s",
        interval,
        len(stale),
        len(universe),
        start,
        end,
    )
    try:
        bf = backfill_scanner_universe(start, end, interval=interval, symbols=stale)
    except Exception as e:  # backfill is already defensive; belt-and-braces
        logger.exception("scanner universe %s catch-up raised: %s", interval, e)
        result["status"] = "error"
        result["errors"].append(str(e))
        return result

    if bf.get("status") == "success":
        result["refreshed"] = stale
    elif bf.get("status") == "ok":
        # Empty-universe / no-op success variant — nothing to refresh, not an error.
        pass
    else:
        result["status"] = "error"
        result["errors"].append(bf.get("message", "unknown backfill error"))
    return result


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="scanner_universe_backfill",
        description="One-shot 1m or D backfill for the in-house scanner SCANNER_SYMBOLS universe.",
    )
    parser.add_argument("--from", dest="from_date", required=True, help="start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="end date YYYY-MM-DD")
    parser.add_argument(
        "--interval",
        default="1m",
        choices=list(STORAGE_INTERVALS),
        help="storage interval to backfill (default 1m)",
    )
    args = parser.parse_args(argv)

    result = backfill_scanner_universe(args.from_date, args.to_date, interval=args.interval)
    print(json.dumps(result, default=str, indent=2))
    return 0 if result.get("status") in ("success", "ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
