"""Sector-index 1m feed wiring for sector_follow_cap5_vol.

The signal evaluator (``services.sector_follow_service.duckdb_metrics_provider``)
derives each mapped sector index's *intraday* return from its 1m bars. The index
1m feed was a one-off backfill (stale ~12 days as of 2026-06-10, per
``strategies/sector_follow_cap5_vol/index_data_coverage.md``); without a daily
refresh every universe stock fails-closed at the sector gate. This module closes
that gap with two callers:

  * a daily APScheduler job (``refresh_sector_follow_indices``) registered by
    ``HistorifyScheduler`` to run after close — keeps the 8 indices that already
    have 1m fresh, and defensively attempts the 2 that don't.
  * a one-shot CLI to catch up a historical gap manually::

        uv run python -m services.sector_follow_index_backfill --from 2026-05-29 --to 2026-06-10

Both paths route through the same ``services.historify_service.create_and_start_job``
pipeline the stock backfill uses, so this is purely **additive** — it never
replaces or duplicates the watchlist download. Symbol set is derived from
``sector_map.json`` (so it tracks the live map) unioned with the two known
1m-missing indices so the feed starts populating the moment the broker delivers
them.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from utils.logging import get_logger

logger = get_logger(__name__)

# Indices live on the NSE_INDEX exchange (OpenAlgo symbol format — see CLAUDE.md).
_INDEX_EXCHANGE = "NSE_INDEX"

# Two sector indices have no 1m history yet (NIFTYCONSRDURBL, NIFTYOILANDGAS).
# After the Phase 3 re-map (DIXON, RELIANCE -> NIFTY) no stock references them,
# but we keep attempting them defensively: if the broker ever returns their 1m,
# the feed begins populating with no further code change.
_ALWAYS_INCLUDE = ("NIFTYCONSRDURBL", "NIFTYOILANDGAS")

# Small lookback for the daily incremental refresh — covers a missed run/weekend
# without re-pulling the whole history (downloads are incremental).
_DAILY_LOOKBACK_DAYS = 4


def sector_index_symbols(sector_map_path: str | Path | None = None) -> list[str]:
    """Unique sector-index symbols to keep fresh, sorted for stability.

    Derived from ``sector_map.json``'s mapped index values unioned with the two
    known 1m-missing indices (defensive). Tracks the live map automatically.
    """
    from services.sector_follow_service import load_sector_map

    if sector_map_path is None:
        symbols = set(load_sector_map().values())
    else:
        symbols = set(load_sector_map(sector_map_path).values())
    symbols.update(_ALWAYS_INCLUDE)
    return sorted(symbols)


def backfill_sector_indices(
    start_date: str,
    end_date: str,
    api_key: str | None = None,
    exchange: str = _INDEX_EXCHANGE,
) -> dict:
    """Download 1m bars for every sector index over [start_date, end_date].

    Additive — goes through the historify job pipeline (incremental, so it only
    fetches the missing tail). Defensive: if the broker returns no data for an
    individual symbol (the two 1m-missing indices), ``create_and_start_job`` logs
    and moves on per-symbol; this function never raises on a partial feed.

    Returns a small status dict: ``{status, symbols, job_id?, message?}``.
    """
    from database.auth_db import get_first_available_api_key
    from services.historify_service import create_and_start_job

    api_key = api_key or get_first_available_api_key()
    if not api_key:
        logger.error("sector_follow index backfill: no API key available — skipping")
        return {"status": "error", "message": "no api key available", "symbols": []}

    syms = sector_index_symbols()
    symbols = [{"symbol": s, "exchange": exchange} for s in syms]
    logger.info(
        "sector_follow index 1m backfill: %d indices %s..%s (%s)",
        len(symbols), start_date, end_date, exchange,
    )
    try:
        success, response, status_code = create_and_start_job(
            job_type="scheduled",
            symbols=symbols,
            interval="1m",
            start_date=start_date,
            end_date=end_date,
            api_key=api_key,
            config={"source": "sector_follow_index_backfill"},
            incremental=True,
        )
    except Exception as e:  # never let a feed hiccup crash the scheduler
        logger.exception("sector_follow index backfill failed to start: %s", e)
        return {"status": "error", "message": str(e), "symbols": syms}

    if success:
        job_id = (response or {}).get("job_id")
        logger.info("sector_follow index backfill started: job_id=%s", job_id)
        return {"status": "success", "job_id": job_id, "symbols": syms}

    msg = (response or {}).get("message", "unknown error")
    logger.error("sector_follow index backfill rejected: %s", msg)
    return {"status": "error", "message": msg, "symbols": syms}


def refresh_sector_follow_indices() -> dict:
    """APScheduler job body: daily after-close 1m refresh for the sector indices.

    Module-level so the SQLAlchemy jobstore can serialize the function reference.
    Uses a small lookback so a missed run / weekend self-heals on the next fire.
    """
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=_DAILY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    logger.info("Scheduled sector_follow index 1m refresh starting (%s..%s)", start, end)
    return backfill_sector_indices(start, end)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="sector_follow_index_backfill",
        description="One-shot 1m backfill for sector_follow_cap5_vol sector indices.",
    )
    parser.add_argument("--from", dest="from_date", required=True, help="start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="end date YYYY-MM-DD")
    args = parser.parse_args(argv)

    result = backfill_sector_indices(args.from_date, args.to_date)
    print(json.dumps(result, default=str, indent=2))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
