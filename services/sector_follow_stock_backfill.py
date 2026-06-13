"""Universe-stock 1m feed wiring for sector_follow_cap5_vol.

The signal evaluator (``services.sector_follow_service.duckdb_metrics_provider``)
needs current 1m bars for every universe **stock** (intraday return + 20d volume
ratio), in addition to the mapped sector **indices** kept fresh by
``services.sector_follow_index_backfill``. The 30 universe stocks' 1m backfill
was *manual* until 2026-06-13, which let the stock feed sit stale — on 2026-06-12
every universe stock was 2 business days behind (last bar 2026-06-10) and the
strategy's freshness gate held all entries.

This module closes that gap, mirroring ``sector_follow_index_backfill`` exactly:

  * ``check_and_refresh_if_stale`` — the state-convergence entry used by the
    boot-time hook and the in-process periodic loop (see
    ``services.sector_follow_backfill_scheduler``). It reads MAX(timestamp) per
    stock from ``historify.duckdb`` and fetches only the stocks behind today's
    expected close. This **supersedes the 16:10 IST cron job** (removed together
    with the 16:05 index cron — see commit ``5c2a06eff`` and earlier).
  * ``refresh_sector_follow_stocks`` — a thin lookback wrapper, retained as a
    convenience / programmatic entry (no longer registered on any scheduler).
  * a one-shot CLI to catch up a historical gap manually::

        uv run python -m services.sector_follow_stock_backfill --from 2026-06-10 --to 2026-06-13

All paths route through the same ``services.historify_service.create_and_start_job``
pipeline the index backfill uses, so this is purely **additive** — it never
replaces or duplicates the watchlist download. The symbol set is derived from the
strategy's ``config_snapshot.json`` universe (so it tracks the locked-static-30
set automatically). Stocks trade on the ``NSE`` exchange (OpenAlgo symbol format
— see CLAUDE.md), unlike the indices' ``NSE_INDEX``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

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
    symbols: list[str] | None = None,
) -> dict:
    """Download 1m bars for universe stocks over [start_date, end_date].

    Additive — goes through the historify job pipeline (incremental, so it only
    fetches the missing tail). Per-symbol failures (e.g. an expired broker
    session) are handled inside ``create_and_start_job`` and never raise here.

    ``symbols`` restricts the fetch to a subset (used by the stale-check
    convergence path, which only re-fetches the stocks that are behind). When
    ``None`` the full locked-static-30 universe is fetched (lookback wrapper / CLI).

    Returns a small status dict: ``{status, symbols, job_id?, message?}``.
    """
    from database.auth_db import get_first_available_api_key
    from services.historify_service import create_and_start_job

    api_key = api_key or get_first_available_api_key()
    if not api_key:
        logger.error("sector_follow stock backfill: no API key available — skipping")
        return {"status": "error", "message": "no api key available", "symbols": []}

    syms = sorted(set(symbols)) if symbols is not None else sector_follow_stock_symbols()
    symbols_payload = [{"symbol": s, "exchange": exchange} for s in syms]
    logger.info(
        "[sector_follow_stock_backfill] %d stocks %s..%s (%s)",
        len(symbols_payload),
        start_date,
        end_date,
        exchange,
    )
    try:
        success, response, status_code = create_and_start_job(
            job_type="scheduled",
            symbols=symbols_payload,
            interval="1m",
            start_date=start_date,
            end_date=end_date,
            api_key=api_key,
            config={"source": "sector_follow_stock_backfill"},
            incremental=True,
        )
    except Exception as e:  # never let a feed hiccup crash the caller
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
    """Lookback 1m refresh for the universe stocks over a small trailing window.

    Retained as a convenience / programmatic entry (no longer registered on a
    scheduler — the boot + periodic ``check_and_refresh_if_stale`` path replaced
    the 16:10 IST cron). Uses a small lookback so it self-heals a missed window.
    """
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=_DAILY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    logger.info("[sector_follow_stock_backfill] 1m lookback refresh starting (%s..%s)", start, end)
    return backfill_sector_follow_stocks(start, end)


def check_and_refresh_if_stale(
    today: date | None = None,
    *,
    duckdb_path: str | None = None,
    max_staleness_business_days: int = 0,
) -> dict:
    """Refresh only the universe stocks whose 1m feed is behind today's close.

    The state-convergence replacement for the 16:10 IST cron. Reads MAX(timestamp)
    per stock from ``historify.duckdb``; any stock more than
    ``max_staleness_business_days`` business days behind the latest trading day
    (default 0 — wants today's close) is queued for an incremental catch-up over a
    small lookback window; the rest are skipped. **Idempotent** — when every stock
    is fresh this is a no-op. **Fail-graceful** — a fetch failure (e.g. an expired
    broker session) is ``logger.exception``-logged and recorded in ``errors``,
    never raised.

    Returns ``{status, stale_symbols, refreshed, errors, skipped_fresh}``.
    """
    from services.data_freshness_service import _DEFAULT_DUCKDB_PATH, compute_stale_symbols

    ref = today or date.today()
    path = duckdb_path or _DEFAULT_DUCKDB_PATH
    universe = sector_follow_stock_symbols()

    result: dict = {
        "status": "ok",
        "stale_symbols": [],
        "refreshed": [],
        "errors": [],
        "skipped_fresh": [],
    }
    try:
        stale, fresh, _details = compute_stale_symbols(
            path, universe, today=ref, max_staleness_business_days=max_staleness_business_days
        )
    except Exception as e:  # never let a freshness read crash the caller
        logger.exception("sector_follow stock stale-check failed to read freshness: %s", e)
        result["status"] = "error"
        result["errors"].append(f"freshness_read: {e}")
        return result

    result["stale_symbols"] = stale
    result["skipped_fresh"] = fresh
    if not stale:
        logger.info("sector_follow stock feed fresh (%d stocks) — no refresh", len(fresh))
        return result

    end = ref.strftime("%Y-%m-%d")
    start = (ref - timedelta(days=_DAILY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    logger.info(
        "sector_follow stock feed stale: %d/%d behind — catching up %s..%s: %s",
        len(stale),
        len(universe),
        start,
        end,
        stale,
    )
    try:
        bf = backfill_sector_follow_stocks(start, end, symbols=stale)
    except Exception as e:  # backfill is already defensive; belt-and-braces
        logger.exception("sector_follow stock catch-up raised: %s", e)
        result["status"] = "error"
        result["errors"].append(str(e))
        return result

    if bf.get("status") == "success":
        result["refreshed"] = stale
    else:
        result["status"] = "error"
        result["errors"].append(bf.get("message", "unknown backfill error"))
    return result


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
