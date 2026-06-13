"""Universe-stock 1m feed wiring for sector_follow_cap5_vol.

The signal evaluator (``services.sector_follow_service.duckdb_metrics_provider``)
needs current 1m bars for every universe **stock** (intraday return + 20d volume
ratio), in addition to the mapped sector **indices** kept fresh by
``services.sector_follow_index_backfill``. Until now only the index feed had a
daily after-close refresh; the 30 universe stocks' 1m backfill was *manual* (a
hand-rolled ``historify_service.create_and_start_job`` call). That gap let the
stock feed sit stale — on 2026-06-12 every universe stock was 2 business days
behind (last bar 2026-06-10) and the strategy's freshness gate held all entries.

This module closes that gap, mirroring ``sector_follow_index_backfill`` exactly:

  * a daily APScheduler job (``refresh_sector_follow_stocks``) registered by
    ``HistorifyScheduler`` to run at 16:10 IST mon-fri (5 min after the 16:05
    index refresh) — keeps the 30 universe stocks' 1m current.
  * a one-shot CLI to catch up a historical gap manually::

        uv run python -m services.sector_follow_stock_backfill --from 2026-06-10 --to 2026-06-13

Both paths route through the same ``services.historify_service.create_and_start_job``
pipeline the index backfill uses, so this is purely **additive** — it never
replaces or duplicates the watchlist download. The symbol set is derived from the
strategy's ``config_snapshot.json`` universe (so it tracks the locked-static-30
set automatically). Stocks trade on the ``NSE`` exchange (OpenAlgo symbol format
— see CLAUDE.md), unlike the indices' ``NSE_INDEX``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from utils.logging import get_logger

logger = get_logger(__name__)

# Universe stocks live on the NSE exchange (OpenAlgo symbol format — see CLAUDE.md;
# the strategy config's ``exchange`` field is "NSE").
_STOCK_EXCHANGE = "NSE"

# Small lookback for the daily incremental refresh — covers a missed run/weekend
# without re-pulling the whole history (downloads are incremental).
_DAILY_LOOKBACK_DAYS = 4


def sector_follow_stock_symbols() -> list[str]:
    """Unique universe-stock symbols to keep fresh, sorted for stability.

    Derived live from the strategy's ``config_snapshot.json`` universe (the
    locked-static-30 set), so it tracks the universe automatically.
    """
    from services.sector_follow_service import load_config

    return sorted(set(load_config().universe))


def backfill_sector_follow_stocks(
    start_date: str,
    end_date: str,
    api_key: str | None = None,
    exchange: str = _STOCK_EXCHANGE,
) -> dict:
    """Download 1m bars for every universe stock over [start_date, end_date].

    Additive — goes through the historify job pipeline (incremental, so it only
    fetches the missing tail). Per-symbol failures (e.g. an expired broker
    session) are handled inside ``create_and_start_job`` and never raise here.

    Returns a small status dict: ``{status, symbols, job_id?, message?}``.
    """
    from database.auth_db import get_first_available_api_key
    from services.historify_service import create_and_start_job

    api_key = api_key or get_first_available_api_key()
    if not api_key:
        logger.error("sector_follow stock backfill: no API key available — skipping")
        return {"status": "error", "message": "no api key available", "symbols": []}

    syms = sector_follow_stock_symbols()
    symbols = [{"symbol": s, "exchange": exchange} for s in syms]
    logger.info(
        "[sector_follow_stock_backfill] %d stocks %s..%s (%s)",
        len(symbols),
        start_date,
        end_date,
        exchange,
    )
    try:
        success, response, status_code = create_and_start_job(
            job_type="scheduled",
            symbols=symbols,
            interval="1m",
            start_date=start_date,
            end_date=end_date,
            api_key=api_key,
            config={"source": "sector_follow_stock_backfill"},
            incremental=True,
        )
    except Exception as e:  # never let a feed hiccup crash the scheduler
        logger.exception("[sector_follow_stock_backfill] failed to start: %s", e)
        return {"status": "error", "message": str(e), "symbols": syms}

    if success:
        job_id = (response or {}).get("job_id")
        logger.info("[sector_follow_stock_backfill] started: job_id=%s", job_id)
        return {"status": "success", "job_id": job_id, "symbols": syms}

    msg = (response or {}).get("message", "unknown error")
    logger.error("[sector_follow_stock_backfill] rejected: %s", msg)
    return {"status": "error", "message": msg, "symbols": syms}


def refresh_sector_follow_stocks() -> dict:
    """APScheduler job body: daily after-close 1m refresh for the universe stocks.

    Module-level so the SQLAlchemy jobstore can serialize the function reference.
    Uses a small lookback so a missed run / weekend self-heals on the next fire.
    """
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=_DAILY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    logger.info("[sector_follow_stock_backfill] scheduled 1m refresh starting (%s..%s)", start, end)
    return backfill_sector_follow_stocks(start, end)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="sector_follow_stock_backfill",
        description="One-shot 1m backfill for sector_follow_cap5_vol universe stocks.",
    )
    parser.add_argument("--from", dest="from_date", required=True, help="start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="end date YYYY-MM-DD")
    args = parser.parse_args(argv)

    result = backfill_sector_follow_stocks(args.from_date, args.to_date)
    print(json.dumps(result, default=str, indent=2))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
