"""Daily post-market review — orchestration, scheduling, and the replay CLI.

Runs once per trading day, after the close, and records what the day actually
did: which scheduled jobs fired, what each strategy journalled, what the feeds
and scanners reported, and a compacted view of the day's logs. The result is
persisted to ``postmarket_review`` and summarised to the operator over Telegram.

**Phase 1 scope (issue #511).** This is the foundation only — collect, persist,
report. It deliberately contains *no verdicts*:

* Phase 2 adds expectation contracts (deterministic pass/fail per strategy).
* Phase 3 adds the ``claude -p`` triage layer over the failures.
* Phase 4/5 add GitHub issue filing and the closed-issue validation sweep.

The load-bearing rule those phases inherit: **Python detects, the LLM triages.**
Invariants are asserted in code and are either true or false; the model only
ranks, correlates, and drafts prose. It never decides whether something is
broken. Keeping that split is what stops the report from inventing problems on
a quiet day.

Why 17:15 IST by default
------------------------

The post-market chain is already busy — 15:35 trading-day funnel, 15:45 scanner
comparison, 16:00 historify, 16:30 sector_follow data health — and the scanner
backfill *convergence loop* runs until 17:00. Reviewing before that window
closes would read half-written data and report phantom staleness. 17:15 is the
first moment everything the digest reads has settled.
"""

from __future__ import annotations

import json
import os
from datetime import date as date_cls
from datetime import datetime
from typing import Any

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

_DEFAULT_TIME = "17:15"
_JOB_ID = "postmarket_review"
_EVENT_TYPE = "postmarket_review"

# Job fires accumulate on every scheduler tick; the review prunes its own audit
# table so it cannot grow without bound.
_DEFAULT_JOB_RUN_RETENTION_DAYS = 90


def _enabled() -> bool:
    """Per-fire gate, read inside the job so a flip needs no re-registration."""
    return os.environ.get("POSTMARKET_REVIEW_ENABLED", "true").strip().lower() == "true"


def _parse_hh_mm(raw: str, default: str = _DEFAULT_TIME) -> tuple[int, int]:
    """Parse ``HH:MM``; fall back to ``default`` on anything malformed."""
    for candidate in (raw, default):
        try:
            hour_s, minute_s = str(candidate).strip().split(":", 1)
            hour, minute = int(hour_s), int(minute_s)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except (ValueError, AttributeError):
            continue
    return 17, 15


def _today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def _job_run_retention_days() -> int:
    try:
        return int(os.environ.get("JOB_RUN_RETENTION_DAYS", _DEFAULT_JOB_RUN_RETENTION_DAYS))
    except (TypeError, ValueError):
        return _DEFAULT_JOB_RUN_RETENTION_DAYS


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _fmt_count(value: Any) -> str:
    """``?`` for a section that failed, so a gap never reads as a zero."""
    return "?" if value is None else str(value)


def render_summary(digest: dict[str, Any], contracts: dict[str, Any] | None = None) -> str:
    """Render the operator-facing summary for one day's digest.

    Deliberately terse: this is a Telegram message read on a phone, not a
    report. Anything needing detail is in the persisted digest.
    """
    date = digest.get("date", "?")
    lines: list[str] = [f"📋 Post-market review — {date}"]

    if digest.get("is_trading_day") is False:
        lines.append("Non-trading day — nothing to review.")
        return "\n".join(lines)

    # Verdict first: the operator should see whether anything is WRONG before
    # reading counts. A clean day says so explicitly rather than staying silent —
    # "no findings" must never be indistinguishable from "the review didn't run",
    # which is precisely how journal_reflection's dead schedule hid for months.
    if contracts is not None and contracts.get("evaluated"):
        violations = contracts.get("violations") or []
        if violations:
            lines.append(f"🚨 {len(violations)} expectation(s) violated:")
            for v in violations[:8]:
                lines.append(
                    f"  [{v['severity']}] {v['strategy']}/{v['contract_id']}: {v['summary']}"
                )
            if len(violations) > 8:
                lines.append(f"  …and {len(violations) - 8} more")
        else:
            lines.append("✅ All expectations passed.")
        unknown = contracts.get("unknown_contracts") or []
        if unknown:
            lines.append(f"  ({len(unknown)} contract(s) indeterminate — missing inputs)")

    jobs = digest.get("jobs")
    if jobs is None:
        lines.append("Jobs: ? (section failed)")
    else:
        parts = [f"{jobs.get('recorded', 0)} jobs recorded"]
        if jobs.get("jobs_with_errors"):
            parts.append(f"⚠️ errors: {', '.join(jobs['jobs_with_errors'][:4])}")
        if jobs.get("jobs_missed"):
            parts.append(f"⚠️ missed: {', '.join(jobs['jobs_missed'][:4])}")
        lines.append("Jobs: " + " | ".join(parts))

    tj = digest.get("trade_journal")
    if tj is None:
        lines.append("Trades: ? (section failed)")
    else:
        by_strategy = tj.get("by_strategy") or {}
        if not by_strategy:
            lines.append("Trades: none journalled")
        else:
            lines.append(f"Trades: {tj.get('total_rows', 0)} rows")
            for name, entry in sorted(by_strategy.items()):
                lines.append(
                    f"  {name}: {entry['placed']} placed / {entry['closed']} closed / "
                    f"{entry['open_at_eod']} open · ₹{entry['net_pnl']:,.0f}"
                )

    fc = digest.get("futures_carry")
    if fc and (fc.get("open_lots_carried") or fc.get("entries_today")):
        carry_note = (
            f"  futures carry: {fc['open_lots_carried']} lots open "
            f"(in {fc['entries_today']} / out {fc['exits_today']} today)"
        )
        if fc.get("carry_age_days") is not None:
            carry_note += f", oldest {fc['carry_age_days']}d"
        lines.append(carry_note)

    signals = digest.get("signals")
    if signals:
        lines.append(
            f"Signals: {_fmt_count(signals.get('total'))} "
            f"({_fmt_count(signals.get('taken'))} taken / "
            f"{_fmt_count(signals.get('not_taken'))} not)"
        )

    scanner = digest.get("scanner")
    if scanner:
        by_source = scanner.get("distinct_symbols_by_source") or {}
        if by_source:
            lines.append("Scanner: " + ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())))

    logs = digest.get("logs")
    if logs is None:
        lines.append("Logs: ? (section failed)")
    else:
        errors = logs.get("errors") or {}
        app_log = logs.get("app_log") or {}
        warn = (app_log.get("by_level") or {}).get("WARNING", 0)
        lines.append(
            f"Logs: {errors.get('total', 0)} errors "
            f"({errors.get('distinct_templates', 0)} distinct), {warn} warnings"
        )
        top = (errors.get("top_templates") or [])[:3]
        for entry in top:
            lines.append(f"  {entry['count']}× {entry['logger']}: {entry['template'][:70]}")

    failed = digest.get("sources_failed") or []
    if failed:
        lines.append(f"⚠️ Degraded sections: {', '.join(failed)}")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run_review_for_date(
    date: str | date_cls | None = None,
    dispatch_telegram: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    """Build, persist and report the review for one date.

    Args:
        date: IST calendar date. Defaults to today in IST.
        dispatch_telegram: Send the operator summary.
        persist: Write the ``postmarket_review`` row.

    Returns:
        ``{"date", "digest", "summary", "persisted", "telegram_sent", "skipped"}``.
        A non-trading day returns early with ``skipped="non_trading_day"`` and
        writes nothing — an empty row for a Saturday is noise, not a record.
    """
    from services.postmarket_day_digest import build_day_digest

    target = (
        date.strftime("%Y-%m-%d")
        if isinstance(date, date_cls)
        else (str(date) if date else _today_ist())
    )
    started = datetime.utcnow()

    digest = build_day_digest(target)

    if digest.get("is_trading_day") is False:
        logger.info("postmarket_review: %s is not a trading day — skipping", target)
        return {
            "date": target,
            "digest": digest,
            "contracts": {
                "violations": [],
                "counts": {},
                "unknown_contracts": [],
                "evaluated": False,
            },
            "summary": None,
            "persisted": False,
            "telegram_sent": False,
            "skipped": "non_trading_day",
        }

    try:
        from services.strategy_expectations import evaluate_expectations

        contracts = evaluate_expectations(digest)
    except Exception:
        logger.exception("postmarket_review: contract evaluation failed")
        contracts = {"violations": [], "counts": {}, "unknown_contracts": [], "evaluated": False}

    summary = render_summary(digest, contracts)
    elapsed_ms = int((datetime.utcnow() - started).total_seconds() * 1000)

    telegram_sent = False
    if dispatch_telegram:
        try:
            from services.notification_service import get_notification_service

            get_notification_service().notify(_EVENT_TYPE, summary)
            telegram_sent = True
        except Exception:
            logger.exception("postmarket_review: telegram dispatch failed")

    persisted = False
    if persist:
        from database import postmarket_review_db as prdb

        row_id = prdb.upsert_review(
            review_date=target,
            digest=digest,
            summary_text=summary,
            is_trading_day=digest.get("is_trading_day"),
            elapsed_ms=elapsed_ms,
            telegram_sent=telegram_sent,
            contracts=contracts,
        )
        persisted = bool(row_id)

    logger.info(
        "postmarket_review: %s done in %dms (violations=%d, persisted=%s, telegram=%s, degraded=%s)",
        target,
        elapsed_ms,
        len(contracts.get("violations") or []),
        persisted,
        telegram_sent,
        digest.get("sources_failed") or "none",
    )
    return {
        "date": target,
        "digest": digest,
        "contracts": contracts,
        "summary": summary,
        "persisted": persisted,
        "telegram_sent": telegram_sent,
        "skipped": None,
    }


def _postmarket_review_job() -> None:
    """APScheduler entry point. Module-level and never raises.

    Module-level because the shared scheduler uses a SQLAlchemy jobstore, which
    pickles job callables — a closure or bound method would not survive.
    """
    if not _enabled():
        logger.info("postmarket_review: disabled via POSTMARKET_REVIEW_ENABLED — skipping")
        return
    try:
        run_review_for_date(date=None, dispatch_telegram=True, persist=True)
    except Exception:
        logger.exception("postmarket_review: job failed")

    # Housekeeping for the audit table this review depends on. Separate try so a
    # prune failure never masks a successful review.
    try:
        from database import job_run_db

        job_run_db.prune_older_than(_job_run_retention_days())
    except Exception:
        logger.exception("postmarket_review: job_run prune failed")


def register_jobs(scheduler=None) -> None:
    """Register the daily review on the shared scheduler. Idempotent."""
    sched = scheduler
    if sched is None:
        from services.historify_scheduler_service import get_historify_scheduler

        sched = get_historify_scheduler().scheduler
    if sched is None:
        logger.warning("postmarket_review: no scheduler available — job not registered")
        return

    from apscheduler.triggers.cron import CronTrigger

    hour, minute = _parse_hh_mm(os.environ.get("POSTMARKET_REVIEW_TIME", _DEFAULT_TIME))
    sched.add_job(
        _postmarket_review_job,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=hour, minute=minute, timezone="Asia/Kolkata"
        ),
        id=_JOB_ID,
        replace_existing=True,
        name=f"Post-market review ({hour:02d}:{minute:02d} IST)",
    )
    logger.info("postmarket_review: registered daily job at %02d:%02d IST", hour, minute)


def init_postmarket_review_service(scheduler=None) -> None:
    """Boot entry point."""
    register_jobs(scheduler)


# --------------------------------------------------------------------------
# Replay CLI
# --------------------------------------------------------------------------


def _main() -> int:
    """``uv run python -m services.postmarket_review_service --date YYYY-MM-DD``."""
    import argparse
    import sys

    # The summary carries emoji (it is written for Telegram). A Windows console
    # defaults to cp1252 and would raise UnicodeEncodeError on the first line.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Build the post-market review for a date (replay-safe).",
    )
    parser.add_argument("--date", default=None, help="IST date YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print only — no DB write, no Telegram. The default for replays.",
    )
    parser.add_argument("--telegram", action="store_true", help="Send the summary (implies write)")
    parser.add_argument("--json", action="store_true", help="Print the full digest as JSON")
    args = parser.parse_args()

    # The replay path runs outside the app, so the tables it reads and writes may
    # not exist yet in a fresh checkout.
    for module_name in ("database.job_run_db", "database.postmarket_review_db"):
        try:
            __import__(module_name, fromlist=["init_db"]).init_db()
        except Exception:
            logger.exception("postmarket_review CLI: init failed for %s", module_name)

    result = run_review_for_date(
        date=args.date,
        dispatch_telegram=bool(args.telegram),
        persist=not args.dry_run,
    )

    if result.get("skipped"):
        print(f"{result['date']}: skipped ({result['skipped']})")
        return 0

    if args.json:
        print(json.dumps(result["digest"], indent=2, default=str))
    else:
        print(result["summary"])
        print()
        print(
            f"[digest {len(json.dumps(result['digest'], default=str))} bytes | "
            f"persisted={result['persisted']} | telegram={result['telegram_sent']}]"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
