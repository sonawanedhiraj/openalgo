"""Declarative inventory of every scheduled job across all scheduler instances.

Why this exists
---------------

Scheduling is not one scheduler. It is **seven** APScheduler instances:

===============================  =========================================
Shared "historify" scheduler     services/historify_scheduler_service.py
EOD watchdog                     services/eod_watchdog_service.py
Sandbox square-off               sandbox/squareoff_thread.py
Flow                             services/flow_scheduler_service.py
Python strategies                blueprints/python_strategy.py
Chartink strategies              blueprints/chartink.py
Webhook strategies               blueprints/strategy.py
===============================  =========================================

Enumerating live jobs alone is not enough. A job whose registration was skipped
by an env flag is simply **absent** from the scheduler, which is precisely the
row an operator most wants to see ("why did nothing happen at 16:00?"). So this
module keeps a declarative catalog and merges it with live introspection:

* in catalog **and** live      -> registered, with a real ``next_run_time``
* in catalog, **not** live     -> ``not_registered`` (flag off, or never wired)
* live, **not** in catalog     -> ``unregistered`` (user-defined jobs land here)

The catalog also carries the **control tier** each job will get in Phase 2.
Nothing in this module can change scheduling: there is no ``add_job``,
``pause_job`` or ``remove_job`` call anywhere in it, by design.

Adding a job? Add it here too — ``test_scheduler_registry`` walks the source
for ``add_job(... id="...")`` literals and fails when one is missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from utils.logging import get_logger

logger = get_logger(__name__)

# Control tiers. Mirrors services/thread_registry.py.
#
# protected: never disableable from the UI. Disabling an exit or a square-off
#            strands real positions — issue #497 (4 trading days, 7 NIFTY lots
#            at 110% of book) is the reference failure.
# guarded:   disableable, but Phase 2 will require typed confirmation and a
#            mandatory expiry.
# free:      reports and summaries; a plain toggle is safe.
TIER_PROTECTED = "protected"
TIER_GUARDED = "guarded"
TIER_FREE = "free"

# Job states in a snapshot.
STATE_REGISTERED = "registered"
STATE_NOT_REGISTERED = "not_registered"
STATE_UNREGISTERED = "unregistered"

# Scheduler instance keys.
SCHED_SHARED = "shared"
SCHED_EOD_WATCHDOG = "eod_watchdog"
SCHED_SANDBOX = "sandbox"
SCHED_FLOW = "flow"
SCHED_PYTHON = "python_strategy"
SCHED_CHARTINK = "chartink"
SCHED_STRATEGY = "strategy"


@dataclass(frozen=True)
class JobSpec:
    """One expected scheduled job.

    Attributes:
        job_id: Exact APScheduler job id, or ``None`` when the family is
            dynamic (then ``id_prefix`` matches instead).
        id_prefix: Prefix for dynamically-named job families such as
            ``eod_watchdog_<strategy>`` or ``squareoff_<config>``.
        label: Human-readable name for the UI.
        group: Grouping key the page renders under.
        scheduler: Which instance the job lives on (a ``SCHED_*`` constant).
        schedule: Human text for the trigger, e.g. ``"15:20 mon-fri"``.
        tier: Control tier, recorded now and enforced in Phase 2.
        env_flag: Env var gating registration or per-fire execution.
        description: One line on what the job does.
        safety_note: Why a ``protected`` job is protected. Rendered as the
            tooltip on the disabled toggle in Phase 2.
    """

    label: str
    group: str
    scheduler: str
    schedule: str
    description: str
    job_id: str | None = None
    id_prefix: str | None = None
    tier: str = TIER_FREE
    env_flag: str | None = None
    safety_note: str | None = None


_FF = "strategy:futures_follow_cap50"
_SF = "strategy:sector_follow_cap5_vol"
_O15 = "strategy:open15_vol_breakout"
_IPB = "strategy:intraday_pullback_top2"
_SE = "strategy:simplified_engine"
_FEED = "data_feed"
_REPORTS = "reports"
_SANDBOX = "sandbox"
_PY = "python_strategy_host"


CATALOG: tuple[JobSpec, ...] = (
    # ---- futures_follow_cap50 -------------------------------------------
    JobSpec(
        job_id="futures_follow_daily_reset",
        label="Daily reset",
        group=_FF,
        scheduler=SCHED_SHARED,
        schedule="09:00 mon-fri",
        description="Clears per-day state and the kill-switch counter.",
        tier=TIER_PROTECTED,
        safety_note="Without the reset the kill switch carries yesterday's loss.",
    ),
    JobSpec(
        job_id="futures_follow_smoke_check",
        label="Pre-entry smoke check",
        group=_FF,
        scheduler=SCHED_SHARED,
        schedule="15:18 mon-fri",
        description="Dry-run quote probe before the 15:20 entry.",
        tier=TIER_GUARDED,
    ),
    JobSpec(
        job_id="futures_follow_entry",
        label="Entry",
        group=_FF,
        scheduler=SCHED_SHARED,
        schedule="15:20 mon-fri",
        description="Resolves signals and buys NIFTY futures within the 50% cap.",
        tier=TIER_GUARDED,
    ),
    JobSpec(
        job_id="futures_follow_exit",
        label="T+1 exit",
        group=_FF,
        scheduler=SCHED_SHARED,
        schedule="15:25 mon-fri",
        description="Closes yesterday's lots with a MARKET sell.",
        tier=TIER_PROTECTED,
        safety_note=(
            "NRML has no MIS auto-square-off underneath it. Issue #497: exits "
            "silently stopped for 4 trading days and open lots reached 110% of book."
        ),
    ),
    JobSpec(
        job_id="futures_follow_eod_watchdog",
        label="EOD watchdog",
        group=_FF,
        scheduler=SCHED_SHARED,
        schedule="15:28 mon-fri",
        description="Retries any exit the 15:25 job left unfilled.",
        tier=TIER_PROTECTED,
        safety_note="Last backstop before the 15:30 NFO close.",
    ),
    JobSpec(
        job_id="futures_follow_eod_summary",
        label="EOD summary",
        group=_FF,
        scheduler=SCHED_SHARED,
        schedule="15:30 mon-fri",
        description="Telegram + eod_reports mirror.",
        tier=TIER_FREE,
    ),
    # ---- sector_follow_cap5_vol -----------------------------------------
    JobSpec(
        job_id="sector_follow_daily_reset",
        label="Daily reset",
        group=_SF,
        scheduler=SCHED_SHARED,
        schedule="09:00 mon-fri",
        description="Clears per-day state and the kill-switch counter.",
        tier=TIER_PROTECTED,
        safety_note="Without the reset the kill switch carries yesterday's loss.",
    ),
    JobSpec(
        job_id="sector_follow_preentry_refresh",
        label="Pre-entry feed refresh",
        group=_SF,
        scheduler=SCHED_SHARED,
        schedule="15:02 mon-fri",
        description="Tops up the index + universe feed before the smoke check.",
        tier=TIER_GUARDED,
    ),
    JobSpec(
        job_id="sector_follow_smoke_check",
        label="Pre-entry smoke check",
        group=_SF,
        scheduler=SCHED_SHARED,
        schedule="15:03 mon-fri",
        description="Aggregator coverage + lookback + broker session gates.",
        tier=TIER_GUARDED,
        env_flag="SECTOR_FOLLOW_SMOKE_CHECK_ENABLED",
    ),
    JobSpec(
        job_id="sector_follow_entry",
        label="Entry",
        group=_SF,
        scheduler=SCHED_SHARED,
        schedule="15:05 mon-fri (CAS-clamped to 15:10)",
        description="Buys <=5 CNC names on the C1xW2+E4 gates.",
        tier=TIER_GUARDED,
    ),
    JobSpec(
        job_id="sector_follow_exit",
        label="T+1 exit",
        group=_SF,
        scheduler=SCHED_SHARED,
        schedule="15:10 mon-fri (CAS-clamped)",
        description="MARKET sell of yesterday's CNC positions.",
        tier=TIER_PROTECTED,
        safety_note=(
            "CNC has no MIS square-off backstop; a skipped exit carries to T+2. "
            "The 15:10 clamp keeps the order inside continuous trading (#512)."
        ),
    ),
    JobSpec(
        job_id="sector_follow_eod_summary",
        label="EOD summary",
        group=_SF,
        scheduler=SCHED_SHARED,
        schedule="15:30 mon-fri",
        description="Telegram + eod_reports mirror.",
        tier=TIER_FREE,
    ),
    JobSpec(
        job_id="sector_follow_data_health",
        label="Data-health check",
        group=_SF,
        scheduler=SCHED_SHARED,
        schedule="16:30 mon-fri",
        description="Stale-feed check; auto-pauses tomorrow's entries on failure.",
        tier=TIER_GUARDED,
        env_flag="DATA_FRESHNESS_VALIDATION_ENABLED",
    ),
    # ---- open15_vol_breakout --------------------------------------------
    JobSpec(
        job_id="open15_arm",
        label="Arm",
        group=_O15,
        scheduler=SCHED_SHARED,
        schedule="09:10 mon-fri",
        description="Picks the universe and opens the tick subscription.",
        tier=TIER_GUARDED,
        env_flag="OPEN15_ENABLED",
    ),
    JobSpec(
        job_id="open15_first_candles",
        label="First candles",
        group=_O15,
        scheduler=SCHED_SHARED,
        schedule="09:16 mon-fri",
        description="Seeds the 09:15 candle levels the breakout gate compares to.",
        tier=TIER_GUARDED,
    ),
    JobSpec(
        job_id="open15_exit",
        label="Exit",
        group=_O15,
        scheduler=SCHED_SHARED,
        schedule="configurable, default 09:30 mon-fri",
        description="Hard flatten of every open open15 position.",
        tier=TIER_PROTECTED,
        safety_note="Intraday strategy with no other flatten path before 15:15.",
    ),
    JobSpec(
        job_id="open15_exit_retry",
        label="Exit retry",
        group=_O15,
        scheduler=SCHED_SHARED,
        schedule="exit +2 min",
        description="Retries anything the exit job failed to close.",
        tier=TIER_PROTECTED,
        safety_note="Backstop for a rejected exit fill.",
    ),
    JobSpec(
        job_id="open15_summary",
        label="Summary",
        group=_O15,
        scheduler=SCHED_SHARED,
        schedule="exit +5 min",
        description="Per-day decision-log summary.",
        tier=TIER_FREE,
    ),
    # ---- intraday_pullback_top2 -----------------------------------------
    JobSpec(
        job_id="intraday_pullback_daily_reset",
        label="Daily reset",
        group=_IPB,
        scheduler=SCHED_SHARED,
        schedule="09:00 mon-fri",
        description="Clears per-day state.",
        tier=TIER_PROTECTED,
        safety_note="Without the reset the kill switch carries yesterday's loss.",
    ),
    JobSpec(
        job_id="intraday_pullback_smoke_check",
        label="Smoke check",
        group=_IPB,
        scheduler=SCHED_SHARED,
        schedule="09:18 mon-fri",
        description="Pre-session data-pipeline gate.",
        tier=TIER_GUARDED,
    ),
    JobSpec(
        job_id="intraday_pullback_eval",
        label="Evaluate",
        group=_IPB,
        scheduler=SCHED_SHARED,
        schedule="every 5 min, 09:00-15:59 mon-fri",
        description="Rolling signal evaluation and entry.",
        tier=TIER_GUARDED,
    ),
    JobSpec(
        job_id="intraday_pullback_eod_flatten",
        label="EOD flatten",
        group=_IPB,
        scheduler=SCHED_SHARED,
        schedule="15:15 mon-fri",
        description="Closes every open position before the venue square-off.",
        tier=TIER_PROTECTED,
        safety_note="Only flatten path for this strategy.",
    ),
    JobSpec(
        job_id="intraday_pullback_eod_summary",
        label="EOD summary",
        group=_IPB,
        scheduler=SCHED_SHARED,
        schedule="15:30 mon-fri",
        description="Telegram summary.",
        tier=TIER_FREE,
    ),
    # ---- simplified engine ----------------------------------------------
    JobSpec(
        id_prefix="eod_watchdog_",
        label="EOD watchdog (per strategy)",
        group=_SE,
        scheduler=SCHED_EOD_WATCHDOG,
        schedule="15:14 mon-fri",
        description=(
            "Tick-independent flatten of open trade_journal rows. Fires one "
            "minute before the 15:15 MIS square-off — do not move the cap later."
        ),
        tier=TIER_PROTECTED,
        env_flag="SIMPLIFIED_ENGINE_EOD_WATCHDOG_ENABLED",
        safety_note=(
            "At 15:15 sandbox and the broker reject MIS orders. A watchdog that "
            "fires after that strands positions (2026-06-10 OIL/HINDZINC/TATAELXSI)."
        ),
    ),
    # ---- data feed -------------------------------------------------------
    JobSpec(
        job_id="scanner_history_refresh",
        label="Scanner history refresh",
        group=_FEED,
        scheduler=SCHED_SHARED,
        schedule="16:00 daily",
        description="Refreshes the scanner history provider's daily cache.",
        tier=TIER_GUARDED,
    ),
    JobSpec(
        job_id="scanner_preentry_refresh",
        label="Scanner pre-entry refresh",
        group=_FEED,
        scheduler=SCHED_SHARED,
        schedule="09:16 mon-fri",
        description="Tops up stale 1m/D bars before the session gets going.",
        tier=TIER_GUARDED,
        env_flag="SCANNER_BACKFILL_ENABLED",
    ),
    JobSpec(
        job_id="scanner_smoke_check",
        label="Scanner smoke check",
        group=_FEED,
        scheduler=SCHED_SHARED,
        schedule="09:18 mon-fri",
        description="Arms the per-symbol post-hold when feed data is stale.",
        tier=TIER_GUARDED,
        env_flag="SCANNER_SMOKE_BLOCK_ENABLED",
    ),
    JobSpec(
        job_id="scanner_dry_tripwire",
        label="Scanner dry tripwire",
        group=_FEED,
        scheduler=SCHED_SHARED,
        schedule="hourly, 09:00-15:59 mon-fri",
        description="Alerts when the scanner produces no hits for too long.",
        tier=TIER_GUARDED,
    ),
    JobSpec(
        job_id="news_ingest_cycle",
        label="News ingest",
        group=_FEED,
        scheduler=SCHED_SHARED,
        schedule="every 5 min, 08:00-15:59 mon-fri",
        description="Pulls headlines into market_intel for alert context.",
        tier=TIER_FREE,
    ),
    # ---- reports and ops -------------------------------------------------
    JobSpec(
        job_id="scanner_comparison_eod",
        label="Scanner vs Chartink comparison",
        group=_REPORTS,
        scheduler=SCHED_SHARED,
        schedule="15:45 mon-fri",
        description="Scores in-house scanner hits against the Chartink lists.",
        tier=TIER_FREE,
        env_flag="SCANNER_COMPARISON_EOD_ENABLED",
    ),
    JobSpec(
        job_id="postmarket_review",
        label="Post-market review",
        group=_REPORTS,
        scheduler=SCHED_SHARED,
        schedule="17:15 mon-fri",
        description=(
            "Builds the day digest and prunes job_run. 17:15 because the "
            "backfill convergence loop runs until 17:00."
        ),
        tier=TIER_FREE,
        env_flag="POSTMARKET_REVIEW_ENABLED",
    ),
    JobSpec(
        job_id="trading_day_funnel",
        label="Trading-day funnel",
        group=_REPORTS,
        scheduler=SCHED_SHARED,
        schedule="mon-fri",
        description="Per-day signal funnel summary.",
        tier=TIER_FREE,
    ),
    JobSpec(
        job_id="nightly_reflection",
        label="Nightly LLM reflection",
        group=_REPORTS,
        scheduler=SCHED_SHARED,
        schedule="16:00 mon-fri",
        description=(
            "Posts the journal reflection to the Cowork bridge on :5001. Fails "
            "when the bridge is down, which is the normal state."
        ),
        tier=TIER_FREE,
    ),
    JobSpec(
        job_id="multi_account_login_reminder_0900",
        label="Child-account login reminder (09:00)",
        group=_REPORTS,
        scheduler=SCHED_SHARED,
        schedule="09:00 mon-fri",
        description="Telegram nudge for child accounts without a fresh session.",
        tier=TIER_FREE,
        env_flag="MULTI_ACCOUNT_ENABLED",
    ),
    JobSpec(
        job_id="multi_account_login_reminder_1500",
        label="Child-account login reminder (15:00)",
        group=_REPORTS,
        scheduler=SCHED_SHARED,
        schedule="15:00 mon-fri",
        description="Last call before the 15:20 entries.",
        tier=TIER_FREE,
        env_flag="MULTI_ACCOUNT_ENABLED",
    ),
    JobSpec(
        job_id="multi_account_eod_summary",
        label="Mirror EOD summary",
        group=_REPORTS,
        scheduler=SCHED_SHARED,
        schedule="15:35 mon-fri",
        description="One line per child account over today's account_orders.",
        tier=TIER_FREE,
        env_flag="MULTI_ACCOUNT_ENABLED",
    ),
    # ---- Python strategy host --------------------------------------------
    JobSpec(
        job_id="daily_trading_day_check",
        label="Trading-day check",
        group=_PY,
        scheduler=SCHED_PYTHON,
        schedule="00:01 daily",
        description="Stops scheduled Python strategies on weekends and holidays.",
        tier=TIER_PROTECTED,
        safety_note="Without it, scheduled strategies fire on non-trading days.",
    ),
    JobSpec(
        job_id="market_hours_enforcer",
        label="Market-hours enforcer",
        group=_PY,
        scheduler=SCHED_PYTHON,
        schedule="every 1 min",
        description="Stops scheduled Python strategies once the market closes.",
        tier=TIER_PROTECTED,
        safety_note="The only thing stopping a strategy from running after close.",
    ),
    JobSpec(
        job_id="reap_dead_strategies",
        label="Reap dead strategies",
        group=_PY,
        scheduler=SCHED_PYTHON,
        schedule="periodic",
        description=(
            "Clears crashed strategies out of RUNNING_STRATEGIES so a headless "
            "deploy does not pin their resources until someone opens /python."
        ),
        tier=TIER_GUARDED,
    ),
    # ---- sandbox ---------------------------------------------------------
    JobSpec(
        id_prefix="squareoff_",
        label="MIS auto square-off (per segment)",
        group=_SANDBOX,
        scheduler=SCHED_SANDBOX,
        schedule="per exchange square-off time",
        description="Force-closes open sandbox MIS positions at the venue time.",
        tier=TIER_PROTECTED,
        safety_note="The sandbox book's only square-off path.",
    ),
    JobSpec(
        job_id="squareoff_backup",
        label="Square-off backup sweep",
        group=_SANDBOX,
        scheduler=SCHED_SANDBOX,
        schedule="every 1 min",
        description="Catches square-offs the per-segment jobs missed.",
        tier=TIER_PROTECTED,
        safety_note="Backstop for the per-segment square-off.",
    ),
    JobSpec(
        job_id="t1_settlement",
        label="T+1 settlement",
        group=_SANDBOX,
        scheduler=SCHED_SANDBOX,
        schedule="daily",
        description="Settles T+1 holdings in the sandbox book.",
        tier=TIER_PROTECTED,
        safety_note="Sandbox accounting integrity.",
    ),
    JobSpec(
        job_id="auto_reset",
        label="Sandbox capital reset",
        group=_SANDBOX,
        scheduler=SCHED_SANDBOX,
        schedule="per sandbox config",
        description="Resets the virtual capital on the configured schedule.",
        tier=TIER_GUARDED,
    ),
    JobSpec(
        job_id="daily_pnl_snapshot",
        label="Daily P&L snapshot",
        group=_SANDBOX,
        scheduler=SCHED_SANDBOX,
        schedule="daily",
        description="Snapshots sandbox P&L.",
        tier=TIER_FREE,
    ),
    JobSpec(
        job_id="daily_pnl_reset",
        label="Daily P&L reset",
        group=_SANDBOX,
        scheduler=SCHED_SANDBOX,
        schedule="daily",
        description="Resets the sandbox daily P&L counter.",
        tier=TIER_GUARDED,
    ),
)

_BY_ID: dict[str, JobSpec] = {s.job_id: s for s in CATALOG if s.job_id}
_PREFIXES: tuple[JobSpec, ...] = tuple(s for s in CATALOG if s.id_prefix)


# (scheduler key, module path, attribute chain to the BackgroundScheduler).
# An attribute of ``None`` means "the module holds the scheduler directly".
_SOURCES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (SCHED_SHARED, "services.historify_scheduler_service", ("historify_scheduler", "_scheduler")),
    (SCHED_EOD_WATCHDOG, "services.eod_watchdog_service", ("_scheduler",)),
    (SCHED_SANDBOX, "sandbox.squareoff_thread", ("_scheduler",)),
    (SCHED_FLOW, "services.flow_scheduler_service", ("flow_scheduler", "_scheduler")),
    (SCHED_PYTHON, "blueprints.python_strategy", ("SCHEDULER",)),
    (SCHED_CHARTINK, "blueprints.chartink", ("scheduler",)),
    (SCHED_STRATEGY, "blueprints.strategy", ("scheduler",)),
)


def _resolve_schedulers() -> list[tuple[str, object]]:
    """Return ``(key, scheduler)`` for every instance that currently exists.

    Reads ``sys.modules`` rather than importing. Importing is not a neutral act
    here — ``blueprints.python_strategy`` runs a strategy cleanup at import, so
    an "observability" call that imported it would have real side effects. A
    module that is not loaded cannot own a running scheduler anyway, so
    skipping it loses nothing.

    Each source is independently wrapped: an instance that was never
    initialised (Flow disabled, no sandbox thread yet) is skipped rather than
    failing the whole snapshot.
    """
    import sys

    found: list[tuple[str, object]] = []
    for key, module_path, attrs in _SOURCES:
        module = sys.modules.get(module_path)
        if module is None:
            continue
        try:
            target: object | None = module
            for attr in attrs:
                target = getattr(target, attr, None)
                if target is None:
                    break
            if target is not None:
                found.append((key, target))
        except Exception:
            logger.debug("scheduler_registry: %s unavailable", key, exc_info=True)
    return found


def live_jobs() -> dict[str, dict]:
    """Introspect every scheduler instance for its currently-registered jobs.

    Returns:
        ``job_id -> {scheduler, name, next_run_time, trigger}``. Read-only:
        nothing here mutates a scheduler.
    """
    out: dict[str, dict] = {}
    for key, sched in _resolve_schedulers():
        try:
            jobs = sched.get_jobs()
        except Exception:
            logger.debug("scheduler_registry: get_jobs failed on %s", key, exc_info=True)
            continue
        for job in jobs:
            try:
                next_run = getattr(job, "next_run_time", None)
                out[str(job.id)] = {
                    "scheduler": key,
                    "name": getattr(job, "name", None),
                    "next_run_time": next_run.isoformat() if next_run else None,
                    "trigger": str(getattr(job, "trigger", "")) or None,
                }
            except Exception:
                logger.debug("scheduler_registry: could not read a job on %s", key, exc_info=True)
    return out


def _spec_for(job_id: str) -> JobSpec | None:
    """Exact id first, then the dynamic-family prefixes."""
    spec = _BY_ID.get(job_id)
    if spec is not None:
        return spec
    for candidate in _PREFIXES:
        if job_id.startswith(candidate.id_prefix or "\0"):
            return candidate
    return None


def _last_run(job_id: str) -> dict | None:
    try:
        from database import job_run_db

        return job_run_db.get_last_run(job_id)
    except Exception:
        logger.debug("scheduler_registry: last-run lookup failed for %s", job_id, exc_info=True)
        return None


def _row(spec: JobSpec | None, job_id: str, live: dict | None) -> dict:
    return {
        "job_id": job_id,
        "label": spec.label if spec else job_id,
        "group": spec.group if spec else "user",
        "scheduler": (live or {}).get("scheduler") or (spec.scheduler if spec else None),
        "schedule": spec.schedule if spec else None,
        "description": spec.description if spec else None,
        "tier": spec.tier if spec else TIER_FREE,
        "safety_note": spec.safety_note if spec else None,
        "env_flag": spec.env_flag if spec else None,
        "env_flag_value": (os.environ.get(spec.env_flag) if spec and spec.env_flag else None),
        # Precedence is (spec, live), not live-first: a live job with no catalog
        # entry is *unregistered* — that is how user-defined schedules are
        # distinguished from the ones this repo declares.
        "state": (
            (STATE_REGISTERED if live else STATE_NOT_REGISTERED) if spec else STATE_UNREGISTERED
        ),
        "next_run_time": (live or {}).get("next_run_time"),
        "trigger": (live or {}).get("trigger"),
        "live_name": (live or {}).get("name"),
        "last_run": _last_run(job_id),
    }


def snapshot() -> list[dict]:
    """Merge the catalog with live introspection and the job_run audit.

    Returns:
        One row per job. Catalogued-but-absent jobs render as
        ``not_registered``; live jobs the catalog does not know about render as
        ``unregistered`` (that is where user-defined historify / python /
        flow / chartink schedules land, without needing to be catalogued).
    """
    live = live_jobs()
    rows: list[dict] = []
    matched: set[str] = set()

    for job_id, info in sorted(live.items()):
        spec = _spec_for(job_id)
        rows.append(_row(spec, job_id, info))
        if spec is not None and spec.job_id:
            matched.add(spec.job_id)

    for spec in CATALOG:
        if spec.job_id and spec.job_id not in matched:
            rows.append(_row(spec, spec.job_id, None))

    rows.sort(key=lambda r: (r["group"] or "zz", r["label"]))
    return rows


def summarize(rows: list[dict] | None = None) -> dict:
    """Counts for the page header."""
    rows = snapshot() if rows is None else rows
    statuses = [(r.get("last_run") or {}).get("status") for r in rows if r.get("last_run")]
    return {
        "total": len(rows),
        "registered": sum(1 for r in rows if r["state"] == STATE_REGISTERED),
        "not_registered": sum(1 for r in rows if r["state"] == STATE_NOT_REGISTERED),
        "unregistered": sum(1 for r in rows if r["state"] == STATE_UNREGISTERED),
        "schedulers": len(_resolve_schedulers()),
        "last_run_error": sum(1 for s in statuses if s == "error"),
        "last_run_missed": sum(1 for s in statuses if s == "missed"),
    }
