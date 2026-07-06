"""EOD reconciliation — pull sandbox auto-square-off closures back into the journal.

The simplified stock engine only writes ``trade_journal.exited_at`` when *it*
fires an exit (stop-loss / target / trailing / its own EOD flatten). Positions
still open at the close are silently flattened by **sandbox's own MIS
auto-square-off**, and the engine never reconciles those fills back into
``trade_journal``. Result: the Telegram EOD summary (which counts journal
round-trips) under-reports both the closed-trade count and the realized P&L on
any day the engine didn't fire its own exit before the close.

Confirmed live on 2026-06-10: 4 entries fired, the engine journaled only 1 exit
(JINDALSTEL stop-loss); the other 3 (OIL, HINDZINC, TATAELXSI) were sandbox MIS
square-offs with no journal exit row, so Telegram showed +₹352 instead of the
actual +₹8,327.

This module closes that gap. :func:`reconcile_engine_journal` runs two passes:

1. **Open-row pass** — finds open journal rows for the day, checks ``sandbox.db``
   for a flat position + matching closing fills, and stamps the exit columns on
   the open row with ``exit_reason='sandbox_eod_squareoff'``.
2. **Watchdog price backfill (issue #350)** — the EOD watchdog closes its rows
   with ``record_exit(exit_price=None)`` immediately after placing the MARKET
   flatten (it doesn't wait for the fill), so those rows are invisible to pass 1
   and their P&L read as ₹0 on the strategies dashboard (/positions showed
   +₹386.65 vs /strategies/simplified_engine −₹2,142.40 on 2026-07-06 — the delta
   was exactly the four unpriced watchdog rows). This pass finds
   ``exited_at IS NOT NULL AND pnl IS NULL AND exit_reason='eod_watchdog'`` rows
   and writes ``exit_price`` + gross ``pnl`` from the sandbox fill matching the
   stamped ``exit_order_id``, preserving ``exited_at`` / ``exit_reason``.

Safety / scope contract:

* **Read-only on ``sandbox.db``** — it only ever ``SELECT``s from
  ``sandbox_trades`` / ``sandbox_positions``.
* **Writes only exit columns** on an *already-existing* open ``trade_journal``
  row (via :func:`trade_journal_service.record_exit`). It never creates an entry
  row — entry creation stays the engine's responsibility.
* **Idempotent.** Pass 1's filter (``exited_at IS NULL``) and pass 2's filter
  (``pnl IS NULL``) are the dedup keys: once a row is closed/priced it is no
  longer returned, so a second run is a no-op.
* **Mid-day safe.** A symbol whose sandbox position is still non-flat is skipped,
  so running this before the actual square-off writes nothing.
* **Strategy-scoped.** Defaults to the simplified engine's journal strategy so a
  positional/T+1 strategy's open rows are never force-closed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# The simplified engine journals under this strategy name. Reconciliation is
# scoped to it so a T+1 / positional strategy's legitimately-open rows are never
# touched. (sector_follow keeps its own ``sector_follow_trades`` journal.)
DEFAULT_STRATEGY_NAME = "trending_equity_intraday"

# New controlled-vocabulary exit reason for closures the engine never fired but
# sandbox's MIS auto-square-off did.
EXIT_REASON_SANDBOX_EOD = "sandbox_eod_squareoff"


@dataclass
class ReconcileResult:
    """Outcome of one :func:`reconcile_engine_journal` pass."""

    date: str
    entries_checked: int = 0
    exits_added: int = 0
    # One dict per exit written (or that *would* be written under dry_run):
    # {symbol, journal_id, direction, quantity, entry_price, exit_price,
    #  exit_order_id, exit_time, pnl, fills}
    exit_details: list[dict] = field(default_factory=list)
    # Pass 2 (issue #350): watchdog-closed rows whose NULL exit_price/pnl got
    # backfilled from the sandbox fill matching their exit_order_id.
    backfills_checked: int = 0
    backfills_added: int = 0
    backfill_details: list[dict] = field(default_factory=list)
    # One dict per row we examined but did NOT write: {symbol, reason}
    skipped: list[dict] = field(default_factory=list)
    dry_run: bool = False


def _sandbox():
    """Resolve the sandbox DB module at call time so tests can rebind it."""
    from database import sandbox_db

    return sandbox_db


def _closing_action(direction: str) -> str:
    """The sandbox order action that *closes* a position in ``direction``.

    A LONG was opened with a BUY, so it closes with a SELL; a SHORT was opened
    with a SELL, so it closes with a BUY.
    """
    return "SELL" if (direction or "").upper() == "LONG" else "BUY"


def _gross_pnl(direction: str, entry_price: float, exit_price: float, qty: int) -> float:
    """Gross P&L (no charges) — identical formula to the engine-driven exit path
    in ``trade_journal_service.record_exit``.
    """
    if (direction or "").upper() == "SHORT":
        return (float(entry_price) - float(exit_price)) * int(qty)
    return (float(exit_price) - float(entry_price)) * int(qty)


def _entry_dt(row: dict) -> dt.datetime | None:
    """Best-effort parse of the entry timestamp (fill first, else placement)."""
    for key in ("entry_fill_at", "placed_at"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            return dt.datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
    return None


def _sandbox_position_qty(sess, sandbox_db, symbol: str) -> int | None:
    """Net sandbox position quantity for ``symbol`` summed across products.

    Returns ``None`` when no position row exists (treated as flat-eligible), or
    the signed net quantity otherwise. Read-only.
    """
    rows = (
        sess.query(sandbox_db.SandboxPositions)
        .filter(sandbox_db.SandboxPositions.symbol == symbol)
        .all()
    )
    if not rows:
        return None
    return sum(int(r.quantity or 0) for r in rows)


def _closing_fills(sess, sandbox_db, symbol: str, direction: str, target_date: dt.date):
    """Closing-action sandbox fills for ``symbol`` within a ±1 day window of
    ``target_date``. Window (not exact-date) keeps us robust to the UTC/local
    ambiguity of ``func.now()`` while still scoping to the trading day.

    Returns the list of ``SandboxTrades`` rows ordered by timestamp.
    """
    action = _closing_action(direction)
    lo = dt.datetime.combine(target_date - dt.timedelta(days=1), dt.time.min)
    hi = dt.datetime.combine(target_date + dt.timedelta(days=1), dt.time.max)
    return (
        sess.query(sandbox_db.SandboxTrades)
        .filter(sandbox_db.SandboxTrades.symbol == symbol)
        .filter(sandbox_db.SandboxTrades.action == action)
        .filter(sandbox_db.SandboxTrades.trade_timestamp >= lo)
        .filter(sandbox_db.SandboxTrades.trade_timestamp <= hi)
        .order_by(sandbox_db.SandboxTrades.trade_timestamp.asc())
        .all()
    )


def _fills_for_order(sess, sandbox_db, symbol: str, orderid: str):
    """Sandbox fills for one specific order id (+ symbol as a sanity guard).

    Order-id matching is what keeps the watchdog backfill precise on a symbol
    with multiple round-trips in the day (e.g. an earlier stop-loss SELL and a
    later watchdog SELL): a date-window action scan would blend both fills into
    the average and corrupt the price. Read-only.
    """
    return (
        sess.query(sandbox_db.SandboxTrades)
        .filter(sandbox_db.SandboxTrades.orderid == str(orderid))
        .filter(sandbox_db.SandboxTrades.symbol == symbol)
        .order_by(sandbox_db.SandboxTrades.trade_timestamp.asc())
        .all()
    )


def _backfill_unpriced_watchdog_exits(
    sess,
    sandbox_db,
    unpriced_rows: list[dict],
    result: ReconcileResult,
    *,
    dry_run: bool,
) -> None:
    """Pass 2 (issue #350): price watchdog-stamped exits from their sandbox fill.

    The EOD watchdog closes a journal row with ``exit_price=None`` right after
    placing its MARKET flatten (it doesn't wait for the fill), so the row is
    invisible to pass 1's ``exited_at IS NULL`` filter and its P&L reads as ₹0
    on every dashboard. For each such row, look up the fill by the stamped
    ``exit_order_id`` and write ``exit_price`` + gross ``pnl`` back, preserving
    the row's ``exited_at`` and ``exit_reason``. Idempotent: once ``pnl`` is set
    the row no longer matches the unpriced filter.
    """
    from services import trade_journal_service

    for row in unpriced_rows:
        symbol = row.get("symbol")
        direction = (row.get("direction") or "").upper()
        entry_qty = int(row.get("quantity") or 0)
        journal_id = int(row.get("id") or 0)
        entry_price = row.get("entry_price")
        exit_order_id = row.get("exit_order_id")

        result.backfills_checked += 1

        if not symbol or entry_qty <= 0 or journal_id <= 0:
            result.skipped.append({"symbol": symbol, "reason": "backfill_malformed_row"})
            continue
        if not exit_order_id:
            # Watchdog stamped no order id (order rejected / response unparsed)
            # — there is no fill to price from.
            result.skipped.append({"symbol": symbol, "reason": "backfill_no_exit_order_id"})
            continue
        if entry_price is None:
            result.skipped.append({"symbol": symbol, "reason": "backfill_no_entry_price"})
            continue

        try:
            fills = _fills_for_order(sess, sandbox_db, symbol, exit_order_id)
        except Exception as e:  # noqa: BLE001 — fail-safe per-symbol
            logger.warning("[EOD-RECONCILE] %s backfill fills read failed: %s", symbol, e)
            result.skipped.append({"symbol": symbol, "reason": "backfill_fills_read_error"})
            continue

        filled_qty = sum(int(f.quantity or 0) for f in fills)
        if not fills or filled_qty <= 0:
            result.skipped.append({"symbol": symbol, "reason": "backfill_no_fill_for_order"})
            continue
        if filled_qty != entry_qty:
            # The fill doesn't cover the journaled quantity (e.g. the watchdog's
            # store reconcile clamped the flatten). Writing a P&L against the
            # journal qty would be invented — alert and leave it unpriced.
            try:
                from services.source_divergence_alerts import check_and_alert

                check_and_alert(
                    service="eod_reconciliation",
                    symbol=symbol,
                    source_a_label="journal_exit_qty",
                    source_a_value=float(entry_qty),
                    source_b_label="watchdog_order_fill_qty",
                    source_b_value=float(filled_qty),
                )
            except Exception:  # noqa: BLE001 — observability must never break reconcile
                logger.exception("[EOD-RECONCILE] backfill divergence alert failed for %s", symbol)
            result.skipped.append({"symbol": symbol, "reason": "backfill_qty_mismatch"})
            continue

        notional = sum(float(f.price or 0.0) * int(f.quantity or 0) for f in fills)
        exit_price = notional / filled_qty
        pnl = round(_gross_pnl(direction, float(entry_price), exit_price, entry_qty), 2)

        detail = {
            "symbol": symbol,
            "journal_id": journal_id,
            "direction": direction,
            "quantity": entry_qty,
            "entry_price": entry_price,
            "exit_price": round(exit_price, 2),
            "exit_order_id": str(exit_order_id),
            "exit_time": row.get("exited_at"),
            "pnl": pnl,
            "fills": len(fills),
        }

        if dry_run:
            result.backfill_details.append(detail)
            result.backfills_added += 1
            continue

        try:
            # Preserve the watchdog's exited_at stamp and exit_reason — only the
            # price/outcome columns were missing.
            trade_journal_service.record_exit(
                journal_id,
                exit_price=exit_price,
                exited_at=row.get("exited_at"),
                pnl=pnl,
            )
        except Exception as e:  # noqa: BLE001 — fail-safe per-symbol
            logger.warning("[EOD-RECONCILE] %s backfill record_exit failed: %s", symbol, e)
            result.skipped.append({"symbol": symbol, "reason": "backfill_record_exit_error"})
            continue

        result.backfill_details.append(detail)
        result.backfills_added += 1
        logger.info(
            "[EOD-RECONCILE] Backfilled watchdog exit %s %s qty=%d exit=%.2f pnl=%s "
            "(jid=%d, order=%s)",
            direction,
            symbol,
            entry_qty,
            exit_price,
            pnl,
            journal_id,
            exit_order_id,
        )


def reconcile_engine_journal(
    date: dt.date | str | None = None,
    *,
    strategy_name: str = DEFAULT_STRATEGY_NAME,
    dry_run: bool = False,
) -> ReconcileResult:
    """Close open journal rows that sandbox already flattened via MIS square-off.

    For each open (``exited_at IS NULL``) journal row entered on ``date`` for
    ``strategy_name``:

    1. Read the symbol's net sandbox position. If still non-flat → skip
       (mid-day / not yet squared off).
    2. Read the symbol's closing-action fills. If their summed quantity covers
       the entry quantity → the position was closed; write one exit row with the
       quantity-weighted average closing price, the last fill's timestamp +
       order id, ``exit_reason='sandbox_eod_squareoff'``, and gross P&L.

    Args:
        date: IST trading day to reconcile. Defaults to IST today. Accepts a
            ``date`` or an ISO ``YYYY-MM-DD`` string.
        strategy_name: Journal strategy to scope to (default the simplified
            engine). Pass ``None`` to reconcile every strategy's open rows.
        dry_run: When ``True``, computes everything and populates
            ``exit_details`` but writes nothing — used by the backfill script.

    Returns:
        :class:`ReconcileResult`. Never raises — DB faults are logged and the
        affected symbol is recorded under ``skipped``.
    """
    if date is None:
        target_date = dt.datetime.now(IST).date()
    elif isinstance(date, str):
        target_date = dt.date.fromisoformat(date)
    else:
        target_date = date

    result = ReconcileResult(date=target_date.isoformat(), dry_run=dry_run)

    from services import trade_journal_service

    try:
        # Date-parameterized (not "today") so the operator backfill for a past
        # date finds rows that predate the current IST day. For the live job
        # target_date == today, so this is equivalent to get_open_trades_today.
        open_rows = trade_journal_service.get_open_trades_for_date(
            target_date.isoformat(), strategy_name=strategy_name
        )
    except Exception as e:  # noqa: BLE001 — fail-safe
        logger.warning("[EOD-RECONCILE] get_open_trades_for_date failed: %s", e)
        return result

    result.entries_checked = len(open_rows)

    # Pass 2 input (issue #350): watchdog-closed rows that never got a price.
    try:
        unpriced_rows = trade_journal_service.get_unpriced_exits_for_date(
            target_date.isoformat(), strategy_name=strategy_name
        )
    except Exception as e:  # noqa: BLE001 — fail-safe
        logger.warning("[EOD-RECONCILE] get_unpriced_exits_for_date failed: %s", e)
        unpriced_rows = []

    if not open_rows and not unpriced_rows:
        return result

    sandbox_db = _sandbox()
    sess = sandbox_db.db_session
    try:
        for row in open_rows:
            symbol = row.get("symbol")
            direction = (row.get("direction") or "").upper()
            entry_qty = int(row.get("quantity") or 0)
            journal_id = int(row.get("id") or 0)
            entry_price = row.get("entry_price")

            if not symbol or entry_qty <= 0 or journal_id <= 0:
                result.skipped.append({"symbol": symbol, "reason": "malformed_journal_row"})
                continue

            # 1) Position must be flat (or the row gone) before we close it.
            try:
                net_qty = _sandbox_position_qty(sess, sandbox_db, symbol)
            except Exception as e:  # noqa: BLE001 — fail-safe per-symbol
                logger.warning("[EOD-RECONCILE] %s position read failed: %s", symbol, e)
                result.skipped.append({"symbol": symbol, "reason": "position_read_error"})
                continue
            if net_qty not in (None, 0):
                result.skipped.append({"symbol": symbol, "reason": "still_open"})
                continue

            # 2) There must be closing fills that cover the entry quantity.
            try:
                fills = _closing_fills(sess, sandbox_db, symbol, direction, target_date)
            except Exception as e:  # noqa: BLE001 — fail-safe per-symbol
                logger.warning("[EOD-RECONCILE] %s fills read failed: %s", symbol, e)
                result.skipped.append({"symbol": symbol, "reason": "fills_read_error"})
                continue

            closed_qty = sum(int(f.quantity or 0) for f in fills)
            if not fills or closed_qty < entry_qty:
                # Flat per the position row but no covering close fill we can
                # price from — leave it open rather than invent an exit.
                # Issue #231: this is a hard divergence — journal expected
                # ``entry_qty`` closed (it shows the row open against that
                # quantity) but sandbox's covering fills cover ``closed_qty``.
                # Alert the operator so the data gap is visible within
                # seconds instead of waiting for the EOD summary mismatch.
                try:
                    from services.source_divergence_alerts import check_and_alert

                    check_and_alert(
                        service="eod_reconciliation",
                        symbol=symbol,
                        source_a_label="journal_expected_closed_qty",
                        source_a_value=float(entry_qty),
                        source_b_label="sandbox_covering_fill_qty",
                        source_b_value=float(closed_qty),
                    )
                except Exception:  # noqa: BLE001 — observability must never break reconcile
                    logger.exception(
                        "[EOD-RECONCILE] divergence alert dispatch failed for %s", symbol
                    )
                result.skipped.append({"symbol": symbol, "reason": "no_covering_close_fill"})
                continue

            # Quantity-weighted average close price across (possibly partial) fills.
            notional = sum(float(f.price or 0.0) * int(f.quantity or 0) for f in fills)
            exit_price = notional / closed_qty if closed_qty else None
            if exit_price is None:
                result.skipped.append({"symbol": symbol, "reason": "zero_close_qty"})
                continue

            last = fills[-1]
            exit_order_id = str(last.orderid) if last.orderid else None
            exit_time = (
                last.trade_timestamp.isoformat()
                if isinstance(last.trade_timestamp, dt.datetime)
                else None
            )

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
                "fills": len(fills),
            }

            if dry_run:
                result.exit_details.append(detail)
                result.exits_added += 1
                continue

            try:
                trade_journal_service.record_exit(
                    journal_id,
                    exit_price=exit_price,
                    exit_order_id=exit_order_id,
                    exit_reason=EXIT_REASON_SANDBOX_EOD,
                    exited_at=exit_time,
                    pnl=pnl,
                )
            except Exception as e:  # noqa: BLE001 — fail-safe per-symbol
                logger.warning("[EOD-RECONCILE] %s record_exit failed: %s", symbol, e)
                result.skipped.append({"symbol": symbol, "reason": "record_exit_error"})
                continue

            result.exit_details.append(detail)
            result.exits_added += 1
            logger.info(
                "[EOD-RECONCILE] Journaled sandbox square-off %s %s qty=%d exit=%.2f pnl=%s "
                "(jid=%d, %d fill(s))",
                direction,
                symbol,
                entry_qty,
                exit_price,
                pnl,
                journal_id,
                len(fills),
            )

        # Pass 2 (issue #350): price the watchdog-stamped exits from their fills.
        _backfill_unpriced_watchdog_exits(sess, sandbox_db, unpriced_rows, result, dry_run=dry_run)
    finally:
        try:
            sess.remove()
        except Exception:
            pass

    logger.info(
        "[EOD-RECONCILE] date=%s strategy=%s checked=%d added=%d "
        "backfill_checked=%d backfilled=%d skipped=%d dry_run=%s",
        result.date,
        strategy_name,
        result.entries_checked,
        result.exits_added,
        result.backfills_checked,
        result.backfills_added,
        len(result.skipped),
        dry_run,
    )
    return result
