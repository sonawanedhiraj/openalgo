"""Recover real exit price + P&L for abandoned journal exits from sandbox fills.

Issue #262. The simplified engine's EOD watchdog stamps ``exit_reason`` +
``exited_at`` on a ``trade_journal`` row even when its flatten order never
fills (rejected at 15:14 IST, sandbox MIS race, etc.). The boot-time
:mod:`services.orphan_exit_reconciliation_service` then relabels that half-updated
row ``exit_reason = 'abandoned_<original>'`` and — by design — leaves
``exit_price`` / ``pnl`` NULL as a forensic marker.

But in almost every real case the position *was* flattened — just by **sandbox's
own MIS auto-square-off** (or an operator UI exit), which the engine never
journaled. So the ``abandoned_*`` row has a real closing fill sitting in
``sandbox.db`` we can price from. Left NULL, those rows contribute 0 to the
``/strategies`` dashboard net P&L, which then under-reports (the operator symptom
in #262: 16 ``abandoned_eod_watchdog`` rows with ``pnl=NULL``).

This service closes that gap. :func:`recover_abandoned_exits` finds
``abandoned_% AND exit_price IS NULL`` journal rows, matches the **earliest
covering closing-action fills at/after the row's own entry time** in
``sandbox.db`` (capped at the entry quantity — this is what makes it correct on a
day with multiple round-trips of the same symbol, where summing *all* closing
fills would blend two unrelated exits), and stamps the real exit price + gross
P&L via :func:`trade_journal_service.record_exit` with
``exit_reason='recovered_from_sandbox'``.

Why a dedicated per-row match (not the ±1-day sum in
``engine_eod_reconciliation_service``): the abandoned rows are exactly the ones
that co-occur with extra fills — a failed watchdog BUY that re-opened a phantom
position (then auto-squared), an earlier stop-loss round-trip, an operator UI
exit. Summing every closing-action fill for the symbol/day over-counts and
mis-prices those. Anchoring on ``trade_timestamp >= entry_time`` and capping at
the entry quantity picks the fill that actually flattened *this* entry.

Safety / scope contract:

* **Read-only on ``sandbox.db``** — only ``SELECT``s from ``sandbox_trades``.
* **Writes only exit columns** on an already-existing row (via ``record_exit``);
  never creates an entry row.
* **Idempotent** — once ``exit_price`` is set the row no longer matches the
  ``exit_price IS NULL`` filter, so a second run is a no-op.
* **Strategy-scoped** to the simplified engine's journal by default.
* **Best-effort** — a per-row failure is logged + counted, never raised.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from dataclasses import dataclass, field

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
_IST_OFFSET = "+05:30"

# The simplified engine journals under this strategy name (mirrors
# engine_eod_reconciliation_service.DEFAULT_STRATEGY_NAME).
DEFAULT_STRATEGY_NAME = "trending_equity_intraday"

# Exit reason stamped on a row whose P&L we reconstructed from sandbox fills
# after the engine abandoned the exit. Distinct from 'sandbox_eod_squareoff'
# (which the same-day EOD reconciliation writes on rows the engine never
# stamped at all) so the two recovery paths stay forensically separable.
EXIT_REASON_RECOVERED = "recovered_from_sandbox"

_ABANDONED_PREFIX = "abandoned_"

_DEFAULT_TIMEOUT_SEC = 90
_DEFAULT_POLL_SEC = 5


@dataclass
class RecoverResult:
    """Outcome of one :func:`recover_abandoned_exits` pass."""

    rows_checked: int = 0
    rows_recovered: int = 0
    total_pnl: float = 0.0
    # One dict per recovered row: {symbol, journal_id, direction, quantity,
    #   entry_price, exit_price, exit_order_id, exit_time, pnl, fills}
    recovered: list[dict] = field(default_factory=list)
    # One dict per row we could NOT recover: {symbol, journal_id, reason}
    skipped: list[dict] = field(default_factory=list)
    dry_run: bool = False


def _flag_enabled() -> bool:
    return os.environ.get("ABANDONED_EXIT_RECOVERY_ENABLED", "true").lower() == "true"


def _sandbox():
    """Resolve the sandbox DB module at call time so tests can rebind it."""
    from database import sandbox_db

    return sandbox_db


def _closing_action(direction: str) -> str:
    """Sandbox order action that CLOSES a position in ``direction``.

    A LONG opened with BUY closes with SELL; a SHORT opened with SELL closes
    with BUY.
    """
    return "SELL" if (direction or "").upper() == "LONG" else "BUY"


def _gross_pnl(direction: str, entry_price: float, exit_price: float, qty: int) -> float:
    """Gross P&L (no charges) — same formula as the engine-driven exit path."""
    if (direction or "").upper() == "SHORT":
        return (float(entry_price) - float(exit_price)) * int(qty)
    return (float(exit_price) - float(entry_price)) * int(qty)


def _entry_dt(placed_at: str | None, entry_fill_at: str | None) -> dt.datetime | None:
    """Best-effort parse of the entry timestamp (fill first, else placement).

    Returns a NAIVE datetime (tz stripped) so it compares cleanly against the
    naive ``sandbox_trades.trade_timestamp`` wall-clock values.
    """
    for raw in (entry_fill_at, placed_at):
        if not raw:
            continue
        try:
            parsed = dt.datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        return parsed.replace(tzinfo=None)
    return None


def _ist_iso(ts: dt.datetime | None) -> str | None:
    """Format a naive sandbox wall-clock timestamp as a tz-aware IST ISO string
    (so ``record_exit``'s hold-duration parse doesn't mix naive/aware)."""
    if not isinstance(ts, dt.datetime):
        return None
    base = ts.replace(tzinfo=None).isoformat()
    return f"{base}{_IST_OFFSET}"


def _match_closing_fills(fills, entry_dt: dt.datetime | None, entry_qty: int):
    """Pick the earliest covering closing-action fills at/after ``entry_dt``,
    capped at ``entry_qty``.

    ``fills`` is a timestamp-ascending list of ``SandboxTrades`` rows (already
    filtered to the closing action + trading day). Returns
    ``(exit_price, exit_order_id, exit_time, used_fill_count)`` or ``None`` when
    the fills at/after entry don't cover the entry quantity.
    """
    remaining = int(entry_qty)
    notional = 0.0
    used_qty = 0
    matched: list = []
    for f in fills:
        if remaining <= 0:
            break
        # Skip fills strictly before the entry — they belong to an earlier
        # round-trip of the same symbol on the same day.
        if entry_dt is not None and isinstance(f.trade_timestamp, dt.datetime):
            if f.trade_timestamp.replace(tzinfo=None) < entry_dt:
                continue
        fill_qty = int(f.quantity or 0)
        if fill_qty <= 0:
            continue
        take = min(fill_qty, remaining)
        notional += float(f.price or 0.0) * take
        remaining -= take
        used_qty += take
        matched.append(f)

    if used_qty < int(entry_qty) or used_qty <= 0:
        return None

    exit_price = notional / used_qty
    last = matched[-1]
    exit_order_id = str(last.orderid) if last.orderid else None
    exit_time = _ist_iso(last.trade_timestamp)
    return exit_price, exit_order_id, exit_time, len(matched)


def _abandoned_rows(sess_factory, strategy_name: str | None, target_date: dt.date | None):
    """Journal rows that are ``abandoned_% AND exit_price IS NULL`` (optionally
    scoped to ``strategy_name`` and a single ``placed_at`` day). Newest first."""
    from database.trade_journal_db import TradeJournal

    sess = sess_factory()
    try:
        q = (
            sess.query(TradeJournal)
            .filter(TradeJournal.exit_reason.like(f"{_ABANDONED_PREFIX}%"))
            .filter(TradeJournal.exit_price.is_(None))
        )
        if strategy_name is not None:
            q = q.filter(TradeJournal.strategy_name == strategy_name)
        if target_date is not None:
            q = q.filter(TradeJournal.placed_at.like(f"{target_date.isoformat()}%"))
        rows = q.order_by(TradeJournal.placed_at.desc()).all()
        return [
            {
                "id": r.id,
                "symbol": r.symbol,
                "direction": r.direction,
                "quantity": r.quantity,
                "entry_price": r.entry_price,
                "placed_at": r.placed_at,
                "entry_fill_at": r.entry_fill_at,
            }
            for r in rows
        ]
    finally:
        try:
            sess.close()
        except Exception:
            pass


def recover_abandoned_exits(
    date: dt.date | str | None = None,
    *,
    strategy_name: str | None = DEFAULT_STRATEGY_NAME,
    dry_run: bool = False,
) -> RecoverResult:
    """Backfill exit price + P&L for abandoned journal rows from sandbox fills.

    Args:
        date: Restrict to rows whose ``placed_at`` falls on this IST day
            (``date`` or ISO ``YYYY-MM-DD`` string). ``None`` processes every
            abandoned-NULL row regardless of day (the historical backfill).
        strategy_name: Journal strategy to scope to (default the simplified
            engine). ``None`` processes every strategy's abandoned rows.
        dry_run: Compute + populate ``recovered`` but write nothing.

    Returns:
        :class:`RecoverResult`. Never raises — per-row faults are logged and
        recorded under ``skipped``.
    """
    if isinstance(date, str):
        target_date: dt.date | None = dt.date.fromisoformat(date)
    else:
        target_date = date

    result = RecoverResult(dry_run=dry_run)

    from database.trade_journal_db import db_session as journal_session
    from services import trade_journal_service

    try:
        rows = _abandoned_rows(journal_session, strategy_name, target_date)
    except Exception as e:  # noqa: BLE001 — fail-safe
        logger.warning("[ABANDONED-RECOVERY] listing abandoned rows failed: %s", e)
        return result

    result.rows_checked = len(rows)
    if not rows:
        return result

    sandbox_db = _sandbox()
    sess = sandbox_db.db_session
    try:
        for row in rows:
            symbol = row.get("symbol")
            direction = (row.get("direction") or "").upper()
            entry_qty = int(row.get("quantity") or 0)
            journal_id = int(row.get("id") or 0)
            entry_price = row.get("entry_price")
            placed_at = row.get("placed_at")

            if not symbol or entry_qty <= 0 or journal_id <= 0:
                result.skipped.append(
                    {"symbol": symbol, "journal_id": journal_id, "reason": "malformed_journal_row"}
                )
                continue

            day_prefix = (placed_at or "")[:10]
            try:
                day = dt.date.fromisoformat(day_prefix)
            except ValueError:
                result.skipped.append(
                    {"symbol": symbol, "journal_id": journal_id, "reason": "unparseable_placed_at"}
                )
                continue

            entry_dt = _entry_dt(placed_at, row.get("entry_fill_at"))
            action = _closing_action(direction)
            lo = dt.datetime.combine(day, dt.time.min)
            hi = dt.datetime.combine(day, dt.time.max)
            try:
                fills = (
                    sess.query(sandbox_db.SandboxTrades)
                    .filter(sandbox_db.SandboxTrades.symbol == symbol)
                    .filter(sandbox_db.SandboxTrades.action == action)
                    .filter(sandbox_db.SandboxTrades.trade_timestamp >= lo)
                    .filter(sandbox_db.SandboxTrades.trade_timestamp <= hi)
                    .order_by(sandbox_db.SandboxTrades.trade_timestamp.asc())
                    .all()
                )
            except Exception as e:  # noqa: BLE001 — fail-safe per-symbol
                logger.warning("[ABANDONED-RECOVERY] %s fills read failed: %s", symbol, e)
                result.skipped.append(
                    {"symbol": symbol, "journal_id": journal_id, "reason": "fills_read_error"}
                )
                continue

            matched = _match_closing_fills(fills, entry_dt, entry_qty)
            if matched is None:
                # No covering close fill at/after entry — genuinely un-flattened
                # in sandbox too. Leave the abandoned_* marker + NULL price.
                result.skipped.append(
                    {"symbol": symbol, "journal_id": journal_id, "reason": "no_covering_close_fill"}
                )
                continue

            exit_price, exit_order_id, exit_time, n_fills = matched
            pnl = None
            if entry_price is not None:
                pnl = round(_gross_pnl(direction, float(entry_price), exit_price, entry_qty), 2)

            detail = {
                "symbol": symbol,
                "journal_id": journal_id,
                "direction": direction,
                "quantity": entry_qty,
                "entry_price": entry_price,
                "exit_price": round(exit_price, 2),
                "exit_order_id": exit_order_id,
                "exit_time": exit_time,
                "pnl": pnl,
                "fills": n_fills,
            }

            if dry_run:
                result.recovered.append(detail)
                result.rows_recovered += 1
                if pnl is not None:
                    result.total_pnl += pnl
                continue

            try:
                trade_journal_service.record_exit(
                    journal_id,
                    exit_price=exit_price,
                    exit_order_id=exit_order_id,
                    exit_reason=EXIT_REASON_RECOVERED,
                    exited_at=exit_time,
                    pnl=pnl,
                )
            except Exception as e:  # noqa: BLE001 — fail-safe per-symbol
                logger.warning("[ABANDONED-RECOVERY] %s record_exit failed: %s", symbol, e)
                result.skipped.append(
                    {"symbol": symbol, "journal_id": journal_id, "reason": "record_exit_error"}
                )
                continue

            result.recovered.append(detail)
            result.rows_recovered += 1
            if pnl is not None:
                result.total_pnl += pnl
            logger.info(
                "[ABANDONED-RECOVERY] Recovered %s %s qty=%d exit=%.2f pnl=%s (jid=%d, %d fill(s))",
                direction,
                symbol,
                entry_qty,
                exit_price,
                pnl,
                journal_id,
                n_fills,
            )
    finally:
        try:
            sess.remove()
        except Exception:
            pass

    logger.info(
        "[ABANDONED-RECOVERY] checked=%d recovered=%d total_pnl=%.2f skipped=%d dry_run=%s",
        result.rows_checked,
        result.rows_recovered,
        result.total_pnl,
        len(result.skipped),
        dry_run,
    )
    return result


def _wait_for_broker_session(deadline_sec: int) -> bool:
    """Poll until a broker session is live or the deadline passes.

    Not strictly required (the recovery only reads sandbox.db), but it lets the
    boot daemon run *after* the orphan reconciliation has had its own
    broker-session window, so freshly-marked abandoned rows are picked up in the
    same boot rather than the next one.
    """
    try:
        from services.broker_session_health import is_live_broker_session
    except Exception:
        return False

    import time as _time

    deadline = _time.monotonic() + deadline_sec
    while _time.monotonic() < deadline:
        try:
            if is_live_broker_session():
                return True
        except Exception:
            logger.exception("[ABANDONED-RECOVERY] live session probe raised")
        _time.sleep(_DEFAULT_POLL_SEC)
    return False


def _notify(message: str) -> None:
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("abandoned_exit_recovery", message)
    except Exception:
        logger.exception("[ABANDONED-RECOVERY] notify failed")


def _boot_worker() -> None:
    if not _flag_enabled():
        logger.info(
            "[ABANDONED-RECOVERY] disabled via ABANDONED_EXIT_RECOVERY_ENABLED=false — not running"
        )
        return

    # Give the orphan reconciliation its broker-session window first so any row
    # it marks abandoned this boot is recoverable in the same pass.
    _wait_for_broker_session(_DEFAULT_TIMEOUT_SEC)

    result = recover_abandoned_exits()
    if result.rows_recovered == 0:
        logger.info(
            "[ABANDONED-RECOVERY] no abandoned rows recovered (checked=%d)", result.rows_checked
        )
        return

    logger.warning(
        "[ABANDONED-RECOVERY] recovered %d/%d abandoned exit(s) from sandbox, net P&L %.2f",
        result.rows_recovered,
        result.rows_checked,
        result.total_pnl,
    )
    syms = ", ".join(d["symbol"] for d in result.recovered) or "(none)"
    _notify(
        f"🧾 Recovered {result.rows_recovered} abandoned engine exit(s) from sandbox fills. "
        f"Net P&L reconciled: ₹{result.total_pnl:,.2f}. Symbols: {syms}. "
        "Dashboard net P&L is now complete for these rows."
    )


def init_abandoned_exit_recovery() -> None:
    """Boot entry — fires recovery on a daemon thread (non-blocking).

    Call once from ``app.py`` boot, after ``init_orphan_exit_reconciliation``.
    """
    if not _flag_enabled():
        logger.info("[ABANDONED-RECOVERY] disabled via ABANDONED_EXIT_RECOVERY_ENABLED=false")
        return
    threading.Thread(
        target=_boot_worker,
        daemon=True,
        name="AbandonedExitRecovery",
    ).start()
    logger.info("[ABANDONED-RECOVERY] boot daemon launched")
