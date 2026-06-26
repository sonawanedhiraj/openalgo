"""Boot-time orphan-exit reconciliation (issue #157, R4 of #156).

What it fixes
-------------
``services.trade_journal_service.record_exit`` stamps ``exit_reason`` and
``exited_at`` on the journal row even if the actual exit fill never
landed (broker token expired at 15:14 IST, sandbox MIS auto-square-off
race, network glitch). The row ends up half-updated:

    exit_reason = 'eod_watchdog'  exited_at = '...15:14...'  exit_price = NULL

After this, every subsequent restart re-loads "open" trades by some
in-engine heuristic (or by a scheduler job that still has the symbol
queued), tries to exit them via ``place_order``, and the order is
rejected — the operator sees the same recurring error on every boot:

    [SIMPLIFIED-ENGINE] No api_key resolvable for TCS exit — order skipped

The 2026-06-26 evidence in #157 lists 7 such rows over 12 days, the
oldest from 2026-06-12.

What this service does
----------------------
At boot, after the broker session comes up (so we don't reconcile during
the 3 AM Zerodha re-login window when everything looks expired), it:

1. Reads every ``trade_journal`` row where
   ``exit_reason IS NOT NULL`` AND ``exit_price IS NULL`` AND
   ``placed_at < today (IST)``.
2. For each, sets ``exit_reason = 'abandoned_' || original_exit_reason``.
   Leaves ``exit_price`` NULL — there is no "true" exit price we can
   make up; the row is forensically marked as a known orphan instead.
3. Telegrams the operator with the full list.

Idempotent: the second boot finds no rows where the reason starts with
``abandoned_`` (we filter those out before matching), so re-running is a
noop.

The fix at the upstream call site (``record_exit`` two-phase) is a
follow-up. This service is the safety net that stops the engine from
re-attempting forever, and gives the operator a clean view of the
historical orphans.

Safety
------
* Master flag ``ORPHAN_EXIT_RECONCILE_ENABLED`` (default ``true``).
* Best-effort: per-row exceptions logged + counted, never raised.
* Daemon thread — boot is not blocked.
* Bounded broker-session wait (default 90s; on timeout exit without
  reconciling — pre-fix behaviour, the next restart will catch up).
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

_DEFAULT_TIMEOUT_SEC = 90
_DEFAULT_POLL_SEC = 5
_ABANDONED_PREFIX = "abandoned_"


def _flag_enabled() -> bool:
    return os.environ.get("ORPHAN_EXIT_RECONCILE_ENABLED", "true").lower() == "true"


def _timeout_sec() -> int:
    try:
        return max(
            10,
            int(os.environ.get("ORPHAN_EXIT_RECONCILE_TIMEOUT_SEC", str(_DEFAULT_TIMEOUT_SEC))),
        )
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SEC


def find_orphan_exits(today_iso_prefix: str | None = None) -> list[dict[str, Any]]:
    """Return rows where ``exit_reason`` is set but ``exit_price`` is NULL
    and ``placed_at < today_iso_prefix`` (so we never reclassify a fill
    that's legitimately still in flight today).

    Already-reconciled rows (reason starting with ``abandoned_``) are
    filtered out so the second boot is a noop.

    Returns ``[]`` on DB failure — the caller's reconcile then does
    nothing.
    """
    if today_iso_prefix is None:
        today_iso_prefix = datetime.now(_IST).strftime("%Y-%m-%d")

    try:
        from database.trade_journal_db import TradeJournal, db_session

        sess = db_session()
        try:
            rows = (
                sess.query(TradeJournal)
                .filter(TradeJournal.exit_reason.isnot(None))
                .filter(TradeJournal.exit_price.is_(None))
                .filter(TradeJournal.placed_at < today_iso_prefix)
                .all()
            )
            out: list[dict[str, Any]] = []
            for r in rows:
                reason = (r.exit_reason or "").strip()
                if reason.startswith(_ABANDONED_PREFIX):
                    continue
                out.append(
                    {
                        "id": r.id,
                        "symbol": r.symbol,
                        "direction": r.direction,
                        "placed_at": r.placed_at,
                        "exited_at": r.exited_at,
                        "exit_reason": reason,
                    }
                )
            return out
        finally:
            try:
                sess.close()
            except Exception:
                pass
    except Exception:
        logger.exception("orphan_exit_reconciliation: find_orphan_exits failed")
        return []


def reconcile_orphan_exits(today_iso_prefix: str | None = None) -> dict[str, Any]:
    """Mark every orphan row's exit_reason as ``abandoned_<original>``.

    Returns a summary dict ``{"orphans": N, "reconciled": M, "errors": E,
    "symbols": [...]}``. Never raises. Per-row failures are counted +
    logged but do not abort the batch.
    """
    orphans = find_orphan_exits(today_iso_prefix=today_iso_prefix)
    if not orphans:
        return {"orphans": 0, "reconciled": 0, "errors": 0, "symbols": []}

    try:
        from database.trade_journal_db import TradeJournal, db_session
    except Exception:
        logger.exception("orphan_exit_reconciliation: trade_journal_db unavailable")
        return {"orphans": len(orphans), "reconciled": 0, "errors": len(orphans), "symbols": []}

    reconciled = 0
    errors = 0
    symbols: list[str] = []

    sess = db_session()
    try:
        for orphan in orphans:
            try:
                row = sess.query(TradeJournal).filter_by(id=orphan["id"]).first()
                if row is None:
                    continue
                # Recompute prefix-safely in case of mid-loop concurrency.
                if (row.exit_reason or "").startswith(_ABANDONED_PREFIX):
                    continue
                row.exit_reason = _ABANDONED_PREFIX + (row.exit_reason or "unknown")
                sess.commit()
                reconciled += 1
                symbols.append(orphan["symbol"])
            except Exception:
                logger.exception(
                    "orphan_exit_reconciliation: failed to reconcile id=%s sym=%s",
                    orphan["id"],
                    orphan["symbol"],
                )
                errors += 1
                try:
                    sess.rollback()
                except Exception:
                    pass
    finally:
        try:
            sess.close()
        except Exception:
            pass

    return {
        "orphans": len(orphans),
        "reconciled": reconciled,
        "errors": errors,
        "symbols": symbols,
    }


def _wait_for_broker_session(deadline_sec: int) -> bool:
    """Poll until the broker session is live or the deadline passes."""
    try:
        from services.broker_session_health import is_live_broker_session
    except Exception:
        logger.exception("orphan_exit_reconciliation: broker_session_health unavailable")
        return False

    deadline = _time.monotonic() + deadline_sec
    while _time.monotonic() < deadline:
        try:
            if is_live_broker_session():
                return True
        except Exception:
            logger.exception("orphan_exit_reconciliation: live session probe raised")
        _time.sleep(_DEFAULT_POLL_SEC)
    return False


def _notify(message: str) -> None:
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("orphan_exit_reconciliation", message)
    except Exception:
        logger.exception("orphan_exit_reconciliation: notify failed")


def _boot_worker() -> None:
    if not _flag_enabled():
        logger.info("orphan_exit_reconciliation: disabled via ORPHAN_EXIT_RECONCILE_ENABLED=false")
        return

    timeout = _timeout_sec()
    logger.info(
        "orphan_exit_reconciliation: waiting up to %ds for broker session "
        "before scanning trade_journal for orphan exits",
        timeout,
    )
    if not _wait_for_broker_session(timeout):
        logger.warning(
            "orphan_exit_reconciliation: no broker session after %ds — skipping (the engine "
            "will keep re-attempting stale exits until next restart with a live session)",
            timeout,
        )
        return

    summary = reconcile_orphan_exits()

    if summary["orphans"] == 0:
        logger.info("orphan_exit_reconciliation: no orphans found — clean state")
        return

    logger.warning(
        "orphan_exit_reconciliation: %d orphan(s) found, %d reconciled, %d errors. Symbols: %s",
        summary["orphans"],
        summary["reconciled"],
        summary["errors"],
        ", ".join(summary["symbols"]) if summary["symbols"] else "(none)",
    )

    icon = "🧹" if summary["errors"] == 0 else "⚠️"
    sym_list = ", ".join(summary["symbols"]) if summary["symbols"] else "(none)"
    _notify(
        f"{icon} Trade journal orphan exits reconciled: "
        f"{summary['reconciled']}/{summary['orphans']} marked abandoned_*. "
        f"Errors: {summary['errors']}. Symbols: {sym_list}. "
        f"The engine will no longer retry these on restart."
    )


def init_orphan_exit_reconciliation() -> None:
    """Boot entry — fires reconciliation on a daemon thread. Non-blocking.

    Call once from ``app.py`` boot, after broker plugin registration. The
    daemon will wait for a live broker session (so the 3 AM re-login
    window doesn't trip the check), then scan and reclassify orphans
    once per boot.
    """
    threading.Thread(
        target=_boot_worker,
        daemon=True,
        name="OrphanExitReconciler",
    ).start()
    logger.info("orphan_exit_reconciliation: boot daemon launched")
