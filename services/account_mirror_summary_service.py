"""Multi-account observability jobs — Phase 3 (issue #476).

Two concerns, three cron jobs on the shared APScheduler instance:

- **Login reminders** (09:00 IST + a 15:00 IST last-call nudge, mon-fri):
  enabled child accounts that selected at least one strategy but have no fresh
  broker session get a Telegram list. The 15:00 nudge exists because the last
  practical login window closes before the 15:20 entries.
- **EOD mirror summary** (15:35 IST mon-fri): one compact line per child
  account summarizing today's ``account_orders`` rows (placed / skipped /
  rejected / errors). Silent when no mirror activity happened.

Every job body is fire-time gated on ``MULTI_ACCOUNT_ENABLED`` and
trading-day awareness (``data_freshness_service.is_trading_day``) — a
single-account install or a market holiday produces zero notifications.
Message builders are pure functions for direct unit testing.
"""

from datetime import datetime

from utils.logging import get_logger

logger = get_logger(__name__)

LOGIN_REMINDER_TIMES = ((9, 0), (15, 0))  # IST
EOD_SUMMARY_TIME = (15, 35)  # IST — after the 15:25/15:28 exit/watchdog window
# IST. open15 exits at 09:30, so its mirrors are settled well before this;
# without a morning pass a post-ACK rejection on a 09:1x mirror would be
# reported as a placed trade until 15:35 (issue #637).
FILL_RECONCILE_TIME = (9, 40)


def _is_trading_day_today() -> bool:
    try:
        from services.data_freshness_service import is_trading_day

        return is_trading_day(datetime.now().date())
    except Exception:
        logger.exception("trading-day check failed — failing open (weekday behavior)")
        return datetime.now().weekday() < 5


def _notify(message: str) -> None:
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("multi_account_mirror", message)
    except Exception:
        logger.exception("multi-account observability notify failed")


def build_login_reminder(accounts: list[dict], nudge: bool = False) -> str | None:
    """Reminder text for enabled+strategy-selecting children without a fresh
    session, or None when everyone is connected (or nothing qualifies)."""
    pending = [
        a
        for a in accounts
        if a.get("is_enabled") and a.get("strategies") and not a.get("connected")
    ]
    if not pending:
        return None
    header = (
        "⏰ LAST CALL — child accounts not logged in (entries fire 15:20 IST):"
        if nudge
        else "⚠ Child accounts not logged in:"
    )
    lines = [header]
    for a in pending:
        strategies = ", ".join(a.get("strategies") or [])
        lines.append(f"  • {a['display_name']} ({a.get('broker_client_id') or '—'}) — {strategies}")
    lines.append("Connect at /accounts.")
    return "\n".join(lines)


def build_eod_summary(rows: list[dict], accounts: list[dict]) -> str | None:
    """Per-account one-liners for today's mirror attempts, or None if none."""
    if not rows:
        return None
    names = {a["id"]: a["display_name"] for a in accounts}
    per_account: dict[int, dict[str, int]] = {}
    for row in rows:
        counts = per_account.setdefault(row["account_id"], {})
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    lines = ["📋 Mirror orders today:"]
    for account_id, counts in sorted(per_account.items()):
        name = names.get(account_id, f"account {account_id}")
        placed = counts.get("placed", 0)
        skipped = sum(v for k, v in counts.items() if k.startswith("skipped"))
        bad = counts.get("rejected", 0) + counts.get("error", 0)
        parts = [f"{placed} placed"]
        if skipped:
            parts.append(f"{skipped} skipped")
        if bad:
            parts.append(f"{bad} REJECTED/error")
        lines.append(f"  • {name}: {', '.join(parts)}")
    lines.append("Details: /orderbook → Mirror Orders. P&L: each child's broker book.")
    return "\n".join(lines)


def _login_reminder_job(nudge: bool = False) -> None:
    try:
        from services.broker_accounts_service import is_multi_account_enabled, overview

        if not is_multi_account_enabled() or not _is_trading_day_today():
            return
        message = build_login_reminder(overview()["accounts"], nudge=nudge)
        if message:
            _notify(message)
    except Exception:
        logger.exception("multi-account login reminder job failed")


def _eod_summary_job() -> None:
    try:
        from database.account_orders_db import list_orders
        from services.broker_accounts_service import is_multi_account_enabled, overview

        if not is_multi_account_enabled() or not _is_trading_day_today():
            return
        today = datetime.utcnow().strftime("%Y-%m-%d")
        # Correct the record BEFORE summarising it (issue #637): `placed` is
        # only the broker's acknowledgement, and a summary built on unchecked
        # ACKs reports trades that never happened.
        _reconcile_now(today)
        message = build_eod_summary(list_orders(date_utc=today), overview()["accounts"])
        if message:
            _notify(message)
    except Exception:
        logger.exception("multi-account EOD mirror summary job failed")


def _reconcile_now(date_utc: str) -> None:
    """Run the child fill reconciliation; never let it break the caller."""
    try:
        from services.account_fill_reconcile import reconcile_account_fills

        result = reconcile_account_fills(date_utc=date_utc)
        if result.get("corrected"):
            logger.warning("multi-account: fill reconcile corrected %s row(s)", result["corrected"])
    except Exception:
        logger.exception("multi-account: fill reconcile failed")


def _fill_reconcile_job() -> None:
    try:
        from services.broker_accounts_service import is_multi_account_enabled

        if not is_multi_account_enabled() or not _is_trading_day_today():
            return
        _reconcile_now(datetime.utcnow().strftime("%Y-%m-%d"))
    except Exception:
        logger.exception("multi-account fill reconcile job failed")


def register_jobs(scheduler=None) -> None:
    """Register the 3 observability jobs. Idempotent (``replace_existing``).

    Registration is unconditional; the per-fire ``MULTI_ACCOUNT_ENABLED`` gate
    in each job body turns the work on/off without re-registration.
    """
    sched = scheduler
    if sched is None:
        from services.historify_scheduler_service import get_historify_scheduler

        sched = get_historify_scheduler().scheduler

    from apscheduler.triggers.cron import CronTrigger

    for index, (hour, minute) in enumerate(LOGIN_REMINDER_TIMES):
        nudge = index > 0
        sched.add_job(
            _login_reminder_job,
            trigger=CronTrigger(
                day_of_week="mon-fri", hour=hour, minute=minute, timezone="Asia/Kolkata"
            ),
            id=f"multi_account_login_reminder_{hour:02d}{minute:02d}",
            kwargs={"nudge": nudge},
            replace_existing=True,
            name=f"Multi-account login reminder ({hour:02d}:{minute:02d} IST)",
        )

    hour, minute = EOD_SUMMARY_TIME
    sched.add_job(
        _eod_summary_job,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=hour, minute=minute, timezone="Asia/Kolkata"
        ),
        id="multi_account_eod_summary",
        replace_existing=True,
        name=f"Multi-account EOD mirror summary ({hour:02d}:{minute:02d} IST)",
    )
    hour, minute = FILL_RECONCILE_TIME
    sched.add_job(
        _fill_reconcile_job,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=hour, minute=minute, timezone="Asia/Kolkata"
        ),
        id="multi_account_fill_reconcile",
        replace_existing=True,
        name=f"Multi-account child fill reconciliation ({hour:02d}:{minute:02d} IST)",
    )
    logger.info(
        "multi-account observability jobs registered "
        "(09:00/15:00 reminders, 09:40 fill reconcile, 15:35 EOD summary IST mon-fri)"
    )


def init_account_mirror_summary_service(scheduler=None) -> None:
    """Boot entry point. No-op-safe."""
    register_jobs(scheduler)
