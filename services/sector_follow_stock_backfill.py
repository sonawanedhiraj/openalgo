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

from services.data_freshness_service import (
    _DEFAULT_DUCKDB_PATH,
    compute_incremental_start_date,
    compute_stale_symbols,
    is_transient_lock_error,
)
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

    Issue #313 (ports the #304 scanner fix) — ``refreshed`` is no longer set
    purely because the download job was *submitted*: after the job reaches a
    terminal state, MAX(timestamp) per stock is re-read and a stock only counts
    as refreshed if its coverage actually advanced. Stocks that remain behind
    after a completed job are reported in ``still_stale`` rather than silently
    counted as done, and a >20% still-stale rate after a completed run lands in
    ``errors`` so the scheduler's anomaly-alert path fires.

    Returns ``{status, stale_symbols, refreshed, still_stale, errors,
    skipped_fresh}``.
    """
    ref = today or date.today()
    path = duckdb_path or _DEFAULT_DUCKDB_PATH
    universe = sector_follow_stock_symbols()

    result: dict = {
        "status": "ok",
        "stale_symbols": [],
        "refreshed": [],
        "still_stale": [],
        "errors": [],
        "skipped_fresh": [],
    }
    try:
        stale, fresh, details = compute_stale_symbols(
            path, universe, today=ref, max_staleness_business_days=max_staleness_business_days
        )
    except Exception as e:  # never let a freshness read crash the caller
        if is_transient_lock_error(e):
            # historify briefly held read-write elsewhere (e.g. a separate CLI
            # backfill process). Skip this cycle quietly — no Telegram anomaly —
            # the boot/next-tick convergence catches up.
            logger.info("sector_follow stock stale-check skipped — historify briefly locked: %s", e)
            result["status"] = "skipped_locked"
            return result
        logger.exception("sector_follow stock stale-check failed to read freshness: %s", e)
        result["status"] = "error"
        result["errors"].append(f"freshness_read: {e}")
        return result

    result["stale_symbols"] = stale
    result["skipped_fresh"] = fresh
    if not stale:
        logger.info("sector_follow stock feed fresh (%d stocks) — no refresh", len(fresh))
        return result

    # Issue #193 — fetch only the incremental gap, not a fixed 4-day window.
    # Pre-fix every Sunday boot pulled Wed-Fri data already on disk, costing
    # ~10s of broker quota per boot. compute_incremental_start_date folds
    # each stale symbol's stored ``last_date`` into the smallest necessary
    # ``[start, ref]`` window, capped by the lookback floor.
    end = ref.strftime("%Y-%m-%d")
    start_date = compute_incremental_start_date(details, stale, ref, _DAILY_LOOKBACK_DAYS)
    start = start_date.strftime("%Y-%m-%d")
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

    # Propagate the submitted job_id to the caller (issue #154). See sibling
    # sector_follow_index_backfill for the rationale.
    if bf.get("job_id"):
        result["job_id"] = bf["job_id"]
    if bf.get("status") == "success":
        # Issue #313 (ports #304) — submission success is NOT completion. Block
        # until the job reaches a terminal state, then re-read freshness and
        # only count a stock as refreshed if its coverage actually advanced.
        # Without this, a job that starts cleanly but fails/partially completes
        # mid-download (dead token mid-batch, per-symbol broker error) was
        # reported `errors=0` with every stale stock marked refreshed.
        _verify_and_report_refresh(result, bf.get("job_id"), path, stale, ref)
    else:
        logger.warning(
            "sector_follow stock catch-up FAILED for %d stock(s) — %s — symbols=%s",
            len(stale),
            bf.get("message", "unknown backfill error"),
            stale,
        )
        result["status"] = "error"
        result["errors"].append(bf.get("message", "unknown backfill error"))
        result["still_stale"] = list(stale)
    return result


def _verify_and_report_refresh(
    result: dict,
    job_id: str | None,
    duckdb_path: str,
    stale: list[str],
    ref: date,
) -> None:
    """Wait for the submitted job, then verify each stale stock actually advanced.

    Mutates ``result`` in place: ``refreshed`` only contains stocks whose
    MAX(timestamp) advanced to (or past) ``ref``'s business day after the job
    finished; the remainder land in ``still_stale``. A verification read failure
    is fail-graceful — it falls back to submission-based reporting (the
    pre-#313 behavior) rather than raising, but is loudly logged so the
    degraded verification is diagnosable.
    """
    if job_id:
        try:
            from services.historify_service import wait_for_jobs

            wait_for_jobs([job_id])
        except Exception:  # waiting must never break the caller
            logger.exception(
                "sector_follow stock catch-up: wait_for_jobs raised for job_id=%s — "
                "verifying freshness anyway",
                job_id,
            )

    try:
        # compute_stale_symbols returns (stale, fresh, details) — "stale" here
        # means "still behind after the completed job", "fresh" means "verified
        # refreshed". max_staleness_business_days=0 always re-checks against
        # today's expected close, independent of the caller's original threshold
        # (a job is either caught all the way up or it isn't).
        still_stale, verified_fresh, _details = compute_stale_symbols(
            duckdb_path,
            stale,
            today=ref,
            max_staleness_business_days=0,
        )
    except Exception as e:  # verification read failure — fail open to submission-based
        logger.exception(
            "sector_follow stock catch-up: post-job freshness verification failed (%s) — "
            "falling back to submission-based reporting for %d stock(s)",
            e,
            len(stale),
        )
        result["refreshed"] = list(stale)
        return

    result["refreshed"] = verified_fresh
    result["still_stale"] = still_stale
    logger.info(
        "sector_follow stock catch-up verified: verified_fresh=%d still_stale=%d",
        len(verified_fresh),
        len(still_stale),
    )

    if not stale:
        return
    still_stale_pct = len(still_stale) / len(stale)
    if still_stale_pct > 0.20:
        msg = (
            f"sector_follow stock catch-up: {len(still_stale)}/{len(stale)} "
            f"({still_stale_pct:.0%}) stocks still stale after a completed job — "
            f"symbols={still_stale[:20]}"
        )
        logger.warning(msg)
        # Land in `errors` (and flip status) so the scheduler's existing
        # _log_and_alert (services.sector_follow_backfill_scheduler) picks this
        # up in its batch anomaly alert — no separate publish_anomaly call here
        # to avoid double-alerting the same run.
        result["errors"].append(msg)
        result["status"] = "error"


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
