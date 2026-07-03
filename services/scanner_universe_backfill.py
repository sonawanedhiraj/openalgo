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

from services.data_freshness_service import (
    _DEFAULT_DUCKDB_PATH,
    _prev_or_same_business_day,
    compute_incremental_start_date,
    compute_stale_symbols,
    is_transient_lock_error,
)
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

# Issue #304 — a second, explicit ceiling on how far back a single convergence
# catch-up may reach, independent of the per-interval _LOOKBACK_DAYS above.
# compute_incremental_start_date already caps at ref-lookback_days, but that cap
# is silent and interval-specific; this one is operator-tunable and logs a
# WARNING (pointing at the manual CLI) whenever a stale symbol's gap is wider
# than the cap, so a months-stale symbol can never trigger a huge automatic
# fetch and the clamp is diagnosable from the logs.
_DEFAULT_MAX_CATCHUP_DAYS = 7


def max_catchup_days() -> int:
    """``SCANNER_BACKFILL_MAX_CATCHUP_DAYS`` env override (default 7, floor 1)."""
    try:
        return max(
            1, int(os.getenv("SCANNER_BACKFILL_MAX_CATCHUP_DAYS", str(_DEFAULT_MAX_CATCHUP_DAYS)))
        )
    except (TypeError, ValueError):
        return _DEFAULT_MAX_CATCHUP_DAYS


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
    incremental: bool = True,
) -> dict:
    """Download ``interval`` bars for the scanner universe over [start, end].

    Additive — routes through the historify job pipeline. Each symbol carries its
    own exchange so the interleaved indices download under ``NSE_INDEX``.
    Per-symbol failures (e.g. an expired broker session, or an index with no
    history) are handled inside ``create_and_start_job`` and never raise here.

    ``symbols`` restricts the fetch to a subset (used by the stale-check
    convergence path, which only re-fetches the symbols that are behind). When
    ``None`` the full ``SCANNER_SYMBOLS`` universe is fetched (lookback wrapper /
    CLI).

    ``incremental`` (default True) fetches only the missing tail — correct for
    the convergence path that just closes a gap. Pass ``incremental=False`` to
    force a **full re-download** of [start, end] that OVERWRITES bars already
    present. This is required by ``resettle_recent_daily`` to correct a daily bar
    that was written intraday as a provisional/running value (the incremental
    path SKIPS any day whose bar already exists, so it can never re-settle it).

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
            incremental=incremental,
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


def _apply_catchup_cap(
    start_date: date,
    ref: date,
    interval: str,
    stale: list[str],
) -> date:
    """Clamp ``start_date`` to ``max_catchup_days()`` before ``ref`` (issue #304).

    ``compute_incremental_start_date`` already caps at the per-interval
    ``_LOOKBACK_DAYS`` floor, but that cap is silent and was never wide enough
    to surface *why* a months-stale symbol only got a partial catch-up. This is
    a second, operator-tunable ceiling (``SCANNER_BACKFILL_MAX_CATCHUP_DAYS``,
    default 7) that logs a WARNING naming the affected symbols and pointing at
    the manual CLI whenever it actually clamps the window.
    """
    cap_days = max_catchup_days()
    cap_floor = ref - timedelta(days=cap_days)
    if start_date >= cap_floor:
        return start_date
    logger.warning(
        "scanner universe %s catch-up window clamped to SCANNER_BACKFILL_MAX_CATCHUP_DAYS=%d "
        "(would have started %s, clamped to %s) for %d symbol(s) — the automatic convergence "
        "will not close this gap; back-fill it manually with "
        "`uv run python -m services.scanner_universe_backfill --from %s --to %s --interval %s` "
        "— symbols=%s",
        interval,
        cap_days,
        start_date.isoformat(),
        cap_floor.isoformat(),
        len(stale),
        start_date.isoformat(),
        ref.isoformat(),
        interval,
        stale,
    )
    return cap_floor


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
    incremental catch-up over the per-interval lookback window, capped at
    ``SCANNER_BACKFILL_MAX_CATCHUP_DAYS`` (default 7); the rest are skipped.
    **Idempotent** — when every symbol is fresh this is a no-op. **Fail-graceful**
    — a fetch failure (e.g. an expired broker session) is
    ``logger.exception``-logged and recorded in ``errors``, never raised.

    Issue #304 — ``refreshed`` is no longer set purely because the download job
    was *submitted*: after the job reaches a terminal state, MAX(timestamp) per
    symbol is re-read and a symbol only counts as refreshed if its coverage
    actually advanced to (or past) the requested ``end`` date. Symbols that
    remain behind after a completed job are reported in ``still_stale`` rather
    than silently counted as done, and a >20% still-stale rate after a completed
    run escalates via ``services.notification_service``.

    Returns ``{status, interval, stale_symbols, refreshed, still_stale, errors,
    skipped_fresh}``.
    """
    ref = today or date.today()
    path = duckdb_path or _DEFAULT_DUCKDB_PATH
    universe = scanner_universe_symbols()

    result: dict = {
        "status": "ok",
        "interval": interval,
        "stale_symbols": [],
        "refreshed": [],
        "still_stale": [],
        "errors": [],
        "skipped_fresh": [],
    }
    if not universe:
        logger.info("scanner universe stale-check: empty universe (SCANNER_SYMBOLS unset) — no-op")
        return result

    try:
        stale, fresh, details = compute_stale_symbols(
            path,
            universe,
            today=ref,
            max_staleness_business_days=max_staleness_business_days,
            interval=interval,
        )
    except Exception as e:  # never let a freshness read crash the caller
        if is_transient_lock_error(e):
            # historify briefly held read-write elsewhere (e.g. a separate CLI
            # backfill process). Skip this cycle quietly — no Telegram anomaly —
            # the boot/next-tick convergence catches up.
            logger.info(
                "scanner universe %s stale-check skipped — historify briefly locked: %s",
                interval,
                e,
            )
            result["status"] = "skipped_locked"
            return result
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

    # Issue #193 — fetch only the incremental gap, not a fixed lookback window.
    # See compute_incremental_start_date docstring for the contract.
    lookback = _LOOKBACK_DAYS.get(interval, 4)
    end = ref.strftime("%Y-%m-%d")
    start_date = compute_incremental_start_date(details, stale, ref, lookback)
    # Issue #304 — a second, explicit, operator-tunable ceiling on top of the
    # per-interval lookback floor above; logs a WARNING when it actually clamps.
    start_date = _apply_catchup_cap(start_date, ref, interval, stale)
    start = start_date.strftime("%Y-%m-%d")
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

    # Propagate the submitted job_id to the caller (issue #154). Without this,
    # boot_convergence's wait_for_jobs (PR #152) sees job_id=None for each
    # interval arm and exits immediately — the lock then releases while the
    # 5-worker pool is still mid-download.
    if bf.get("job_id"):
        result["job_id"] = bf["job_id"]
    if bf.get("status") == "success":
        # Issue #304 — submission success is NOT completion. Block until the
        # job reaches a terminal state, then re-read freshness and only count a
        # symbol as refreshed if its coverage actually advanced. Without this,
        # a job that starts cleanly but fails/partially-completes mid-download
        # (dead token mid-batch, per-symbol broker error) was reported
        # `errors=0` with every stale symbol marked refreshed.
        _verify_and_report_refresh(result, bf.get("job_id"), path, stale, ref, interval)
    elif bf.get("status") == "ok":
        # Empty-universe / no-op success variant — nothing to refresh, not an error.
        pass
    else:
        # Tier-1 Fix #2: name the affected symbols + reason at WARNING instead of
        # only recording it in the returned dict. An expired broker session can
        # fail every stale symbol; without this the only trace was a quiet error
        # key the periodic loop swallowed (FM-11 in the in-house deep analysis).
        msg = bf.get("message", "unknown backfill error")
        logger.warning(
            "scanner universe %s catch-up FAILED for %d symbol(s) — %s — symbols=%s",
            interval,
            len(stale),
            msg,
            stale,
        )
        result["status"] = "error"
        result["errors"].append(msg)
        result["still_stale"] = list(stale)
    return result


def _verify_and_report_refresh(
    result: dict,
    job_id: str | None,
    duckdb_path: str,
    stale: list[str],
    ref: date,
    interval: str,
) -> None:
    """Wait for the submitted job, then verify each stale symbol actually advanced.

    Mutates ``result`` in place: ``refreshed`` only contains symbols whose
    MAX(timestamp) advanced to (or past) ``ref``'s business day after the job
    finished; the remainder land in ``still_stale``. A verification read failure
    is fail-graceful — it falls back to submission-based reporting (the
    pre-#304 behavior) rather than raising, but is loudly logged so the
    degraded verification is diagnosable.
    """
    if job_id:
        try:
            from services.historify_service import wait_for_jobs

            wait_for_jobs([job_id])
        except Exception:  # waiting must never break the caller
            logger.exception(
                "scanner universe %s catch-up: wait_for_jobs raised for job_id=%s — "
                "verifying freshness anyway",
                interval,
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
            interval=interval,
        )
    except Exception as e:  # verification read failure — fail open to submission-based
        logger.exception(
            "scanner universe %s catch-up: post-job freshness verification failed (%s) — "
            "falling back to submission-based reporting for %d symbol(s)",
            interval,
            e,
            len(stale),
        )
        result["refreshed"] = list(stale)
        return

    result["refreshed"] = verified_fresh
    result["still_stale"] = still_stale
    logger.info(
        "scanner universe %s catch-up verified: verified_fresh=%d still_stale=%d",
        interval,
        len(verified_fresh),
        len(still_stale),
    )

    if not stale:
        return
    still_stale_pct = len(still_stale) / len(stale)
    if still_stale_pct > 0.20:
        msg = (
            f"scanner universe {interval} catch-up: {len(still_stale)}/{len(stale)} "
            f"({still_stale_pct:.0%}) symbols still stale after a completed job — "
            f"symbols={still_stale[:20]}"
        )
        logger.warning(msg)
        # Land in `errors` (and flip status) so the scheduler's existing
        # _log_and_alert (services.scanner_backfill_scheduler) picks this up in
        # its batch anomaly alert — no separate publish_anomaly call here to
        # avoid double-alerting the same run.
        result["errors"].append(msg)
        result["status"] = "error"


# --------------------------------------------------------------------------- #
# Daily-D re-settle — correct provisional (intraday-captured) daily closes
# --------------------------------------------------------------------------- #
def _daily_resettle_enabled() -> bool:
    """``SCANNER_DAILY_RESETTLE_ENABLED`` env flag (default true)."""
    return os.getenv("SCANNER_DAILY_RESETTLE_ENABLED", "true").lower() == "true"


def _daily_resettle_days() -> int:
    """How many trailing settled trading days to re-fetch (``SCANNER_DAILY_RESETTLE_DAYS``,
    default 2). Bounded to >= 1."""
    try:
        return max(1, int(os.getenv("SCANNER_DAILY_RESETTLE_DAYS", "2")))
    except (TypeError, ValueError):
        return 2


def _nth_prev_business_day(d: date, n: int) -> date:
    """The business day ``n`` trading days at/before ``d`` (``n``>=0).

    ``n=0`` rolls a weekend back to the preceding Friday; each further step goes
    back one more trading day. Holidays are not modelled (matches
    ``data_freshness_service``) — a holiday just widens the fetch window by a day,
    which is harmless for an overwrite re-fetch.
    """
    cur = _prev_or_same_business_day(d)
    for _ in range(max(0, n)):
        cur = _prev_or_same_business_day(cur - timedelta(days=1))
    return cur


# --------------------------------------------------------------------------- #
# Issue #314 — feed the re-settle's broker-verified closes into the
# scanner_reference_data prev-close registry, unconditionally on every
# boot/convergence run (not just when the aggregator_seeder's broker
# fallback happens to fire).
# --------------------------------------------------------------------------- #
def _daily_bar_ist_date(ts) -> date | None:
    """IST calendar date of a historify daily-bar ``timestamp`` (epoch seconds).

    Mirrors ``services.scan_rules._today_running._to_ist_date`` without
    importing the rules package (this module sits below scan_rules in the
    dependency graph — the backfill/registry layer must not depend on rule
    code). Returns ``None`` on anything unparseable.
    """
    if ts is None:
        return None
    try:
        import pandas as pd  # noqa: PLC0415 — keep module import light

        if pd.isna(ts):
            return None
        return pd.Timestamp(float(ts), unit="s", tz="UTC").tz_convert("Asia/Kolkata").date()
    except (TypeError, ValueError):
        return None
    except Exception:  # noqa: BLE001 — this is an observability helper, never raise
        return None


def _record_prev_closes_from_provider(universe: list[str], today: date) -> dict:
    """Record each symbol's T-1 settled close from the just-refreshed
    ``ScannerHistoryProvider`` daily cache into the broker prev-close registry
    (``services.scanner_reference_data``).

    Reuses the daily-D bars the re-settle already fetched and the provider
    already re-read from DuckDB — zero new broker API load. This is what
    makes registry coverage unconditional: today it is populated ONLY when
    ``scanner_aggregator_seeder``'s broker fallback fires (historify 1m short
    at boot); the re-settle runs at BOTH boot and post-close regardless of 1m
    health, so wiring it here closes that coverage gap on every run.

    CRITICAL semantic trap (do not regress): the re-settle runs post-close too,
    where the most recent SETTLED bar is TODAY's own close, not yesterday's.
    Recording that as "today's prev-close" would poison the registry for the
    rest of today's rule evaluations (today's T-1 is yesterday's close, never
    today's own). So only the close of the latest bar dated STRICTLY BEFORE
    ``today`` is ever recorded — mirroring
    ``scanner_reference_data.record_prev_close_from_bars``'s ``bar_day < today``
    semantics exactly, just sourced from the provider's DataFrame instead of a
    raw broker bar list.

    PROVENANCE RULE (do not regress): the registry has two possible sources
    with different trust levels — the aggregator_seeder's
    ``record_prev_close_from_bars`` is **broker-DIRECT** (an independent broker
    1m fetch, deliberately decoupled from historify — that independence is the
    whole point of the #305 cross-check), while this path is
    **historify-DERIVED** (the provider cache after the re-settle). Broker-direct
    wins; historify-derived only fills gaps. So if a same-day entry already
    exists, we SKIP (counted as ``kept_existing``) rather than overwrite:
    otherwise a re-settle that PARTIALLY fails for a symbol (per-symbol broker
    error — ``provider.refresh()`` still succeeds because refresh success only
    means the cache re-read worked) would serve the OLD stale daily row and
    clobber the seeder's true broker value, making the certificate compare
    stale-vs-stale and CERTIFY the exact 2026-07-02 incident class it exists to
    reject. The reverse ordering is safe as-is: if this path records first, the
    seeder's later unconditional record correctly replaces the historify-derived
    value with the broker-direct one (an upgrade).

    Fail-graceful: a per-symbol read/parse failure is logged and skipped; the
    whole helper never raises into ``resettle_recent_daily``.

    Returns ``{"recorded": [...], "kept_existing": [...], "skipped": [...], "errors": int}``.
    """
    outcome: dict = {"recorded": [], "kept_existing": [], "skipped": [], "errors": 0}
    try:
        from datetime import time as _time

        from services.scanner_history_provider import get_provider
        from services.scanner_reference_data import (
            _IST,
            get_broker_prev_close,
            record_broker_prev_close,
        )
    except Exception:
        logger.exception(
            "scanner daily-D resettle: prev-close registry wiring unavailable — skipping"
        )
        outcome["errors"] += 1
        return outcome

    # Anchor the recording's day-scoping to `today` (the resettle's own
    # reference day), not wall-clock `datetime.now()` — the registry's
    # same-day-only serving contract (services.scanner_reference_data) must
    # key off the day the resettle considers "today", so a boot run that
    # crosses midnight (or a test driving a fixed `today`) records under the
    # correct day rather than whatever instant the process happens to run at.
    record_as_of = datetime.combine(today, _time(hour=8, minute=30), tzinfo=_IST)

    try:
        provider = get_provider()
    except Exception:
        logger.exception(
            "scanner daily-D resettle: could not obtain ScannerHistoryProvider — "
            "prev-close registry not updated"
        )
        outcome["errors"] += 1
        return outcome

    for sym in universe:
        try:
            # Provenance guard: a same-day entry already in the registry is
            # broker-direct (seeder) or an earlier run of this path — never
            # overwrite it with a historify-derived value (see docstring).
            if get_broker_prev_close(sym, today=today) is not None:
                outcome["kept_existing"].append(sym)
                continue

            daily = provider.get_daily(sym)
            if daily is None or daily.empty or "timestamp" not in daily.columns:
                outcome["skipped"].append(sym)
                continue

            prev_close = None
            # Ascending-by-construction (ScannerHistoryProvider._fetch tails the
            # DuckDB query), but iterate defensively rather than assume order.
            for _, row in daily.iterrows():
                bar_day = _daily_bar_ist_date(row.get("timestamp"))
                if bar_day is None or bar_day >= today:
                    continue
                close = row.get("close")
                if close is None:
                    continue
                try:
                    close_f = float(close)
                except (TypeError, ValueError):
                    continue
                if prev_close is None or bar_day >= prev_close[1]:
                    prev_close = (close_f, bar_day)

            if prev_close is None:
                outcome["skipped"].append(sym)
                continue

            record_broker_prev_close(sym, prev_close[0], as_of=record_as_of)
            outcome["recorded"].append(sym)
        except Exception:  # noqa: BLE001 — one bad symbol must never break the batch
            logger.exception(
                "scanner daily-D resettle: prev-close registry record failed for %s", sym
            )
            outcome["errors"] += 1

    logger.info(
        "scanner daily-D resettle: prev-close registry updated for %d/%d symbols "
        "(%d kept broker-direct, %d skipped, %d errors)",
        len(outcome["recorded"]),
        len(universe),
        len(outcome["kept_existing"]),
        len(outcome["skipped"]),
        outcome["errors"],
    )
    return outcome


def resettle_recent_daily(
    today: date | None = None,
    *,
    days: int | None = None,
    refresh_provider: bool = True,
) -> dict:
    """Re-fetch and OVERWRITE the last ``days`` settled trading days of daily-D
    bars for the whole scanner universe, then refresh ``ScannerHistoryProvider``.

    Why this exists — a daily-D bar written *intraday* (the historify feed
    captured a running/provisional close, e.g. the #277 09:45 freeze) is never
    corrected by the normal convergence: ``compute_stale_symbols`` sees "a bar
    for that day exists → fresh" and the incremental download SKIPS any day
    already present. So the provisional close persists all the way into the
    scanner's ``yest_d`` gate and manufactures phantom gap-ups/downs (the
    2026-07-02 DELHIVERY false BUY: stored 07-01 close 475.4 vs settled 507.7).

    This forces a **non-incremental** re-download of the trailing settled window
    (the broker's daily API returns the settled close post-close), which the
    upsert write path overwrites in place. It then calls ``get_provider().refresh()``
    so the in-memory daily cache — warmed at boot from the corrupt bar and never
    re-read during the session — serves the corrected values.

    Idempotent (a re-fetch of already-correct bars is a harmless overwrite),
    fail-graceful (a dead broker session / fetch error is logged, never raised).
    Gated by ``SCANNER_DAILY_RESETTLE_ENABLED`` (default true).

    Returns ``{status, interval, window, resettled, job_id?, provider_symbols_loaded?, errors}``.
    """
    result: dict = {
        "status": "ok",
        "interval": "D",
        "window": None,
        "resettled": False,
        "errors": [],
    }
    if not _daily_resettle_enabled():
        logger.info("scanner daily-D resettle disabled (SCANNER_DAILY_RESETTLE_ENABLED!=true)")
        result["status"] = "disabled"
        return result

    universe = scanner_universe_symbols()
    if not universe:
        logger.info("scanner daily-D resettle: empty universe (SCANNER_SYMBOLS unset) — no-op")
        return result

    ref = today or date.today()
    n = days if days is not None else _daily_resettle_days()
    start = _nth_prev_business_day(ref, n).strftime("%Y-%m-%d")
    end = ref.strftime("%Y-%m-%d")
    result["window"] = f"{start}..{end}"
    logger.info(
        "scanner daily-D resettle: overwrite re-fetch of settled daily bars %s..%s (%d symbols)",
        start,
        end,
        len(universe),
    )

    try:
        bf = backfill_scanner_universe(start, end, interval="D", incremental=False)
    except Exception as e:  # backfill is defensive; belt-and-braces
        logger.exception("scanner daily-D resettle: backfill raised: %s", e)
        result["status"] = "error"
        result["errors"].append(str(e))
        return result

    if bf.get("status") not in ("success", "ok"):
        msg = bf.get("message", "unknown backfill error")
        logger.warning("scanner daily-D resettle: backfill did not start cleanly — %s", msg)
        result["status"] = "error"
        result["errors"].append(msg)
        return result

    job_id = bf.get("job_id")
    if job_id:
        result["job_id"] = job_id
        # Wait for the re-download to LAND before refreshing the in-memory cache,
        # otherwise the provider re-reads the pre-resettle (still-corrupt) rows.
        try:
            from services.historify_service import wait_for_jobs

            finals = wait_for_jobs([job_id])
            logger.info("scanner daily-D resettle: job final status: %s", finals)
        except Exception:  # waiting must never break the resettle path
            logger.exception("scanner daily-D resettle: wait_for_jobs raised (continuing)")

    result["resettled"] = True

    if refresh_provider:
        try:
            from services.scanner_history_provider import get_provider

            refreshed = get_provider().refresh()
            result["provider_symbols_loaded"] = refreshed.get("symbols_loaded", 0)
            logger.info(
                "scanner daily-D resettle: provider daily cache refreshed (%d symbols, %d errors)",
                refreshed.get("symbols_loaded", 0),
                len(refreshed.get("errors", [])),
            )
        except Exception as e:  # provider refresh failure must not raise
            logger.exception("scanner daily-D resettle: provider refresh failed: %s", e)
            result["errors"].append(f"provider_refresh: {e}")
        else:
            # Issue #314 — feed the broker-verified settled closes the re-settle
            # just fetched (and the provider just re-read) into the prev-close
            # registry, so certificate coverage no longer depends on the
            # aggregator_seeder's broker fallback ever firing. Only attempted
            # after a successful provider refresh — a stale/pre-resettle daily
            # cache must never seed the registry. Never raises.
            try:
                registry_outcome = _record_prev_closes_from_provider(universe, ref)
                result["prev_close_registry"] = registry_outcome
            except Exception as e:  # belt-and-braces — helper is already defensive
                logger.exception(
                    "scanner daily-D resettle: prev-close registry wiring raised: %s", e
                )
                result["errors"].append(f"prev_close_registry: {e}")

    return result


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="scanner_universe_backfill",
        description="One-shot 1m or D backfill for the in-house scanner SCANNER_SYMBOLS universe.",
    )
    parser.add_argument(
        "--resettle",
        action="store_true",
        help="force a non-incremental overwrite re-fetch of the last N settled daily-D "
        "days for the whole universe + refresh the scanner provider cache "
        "(corrects provisional intraday-captured closes); ignores --from/--to/--interval",
    )
    parser.add_argument(
        "--resettle-days",
        type=int,
        default=None,
        help="trailing settled trading days to re-fetch with --resettle "
        "(default SCANNER_DAILY_RESETTLE_DAYS or 2)",
    )
    parser.add_argument("--from", dest="from_date", required=False, help="start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=False, help="end date YYYY-MM-DD")
    parser.add_argument(
        "--interval",
        default="1m",
        choices=list(STORAGE_INTERVALS),
        help="storage interval to backfill (default 1m)",
    )
    args = parser.parse_args(argv)

    if args.resettle:
        result = resettle_recent_daily(days=args.resettle_days)
        print(json.dumps(result, default=str, indent=2))
        return 0 if result.get("status") in ("success", "ok", "disabled") else 1

    if not args.from_date or not args.to_date:
        parser.error("--from and --to are required unless --resettle is used")

    result = backfill_scanner_universe(args.from_date, args.to_date, interval=args.interval)
    print(json.dumps(result, default=str, indent=2))
    return 0 if result.get("status") in ("success", "ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
