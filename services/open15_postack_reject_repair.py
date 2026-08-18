"""One-shot operator repair for post-ACK broker rejections (issue #626).

Repairs rows written BEFORE #626 shipped, when an order the broker ACKNOWLEDGED
and then RMS-REJECTED was published as a filled, profitable trade. The known
population is TIINDIA on 2026-08-18 — journaled ``fill='real'``,
``pnl_source='fill'``, +Rs7,680 — but the detection is generic: it asks the
broker about every ``real`` row and repairs whatever comes back terminal.

**It repairs by re-running the FIXED production path, not by hand-editing.**
The row is reset to the state reconciliation would have found it in — quote
prices restored, fill columns cleared, ``fill_reconcile_status='pending'`` — and
then ``reconcile_fills`` is called. So the repair and the live behaviour cannot
drift apart, and if the fix is wrong the repair is wrong in the same way and the
tests catch both.

**Requires the broker orderbook to still carry the order**, which for Zerodha
means running it on the SAME TRADING DAY. There is no historical order-status
API; a later run reports the row as unreadable and leaves it alone rather than
guessing.

The day log is repaired too. The ``/logs`` page renders the DECISION LOG, not
the journal, so fixing only the row leaves the page still showing the trade as
entered and profitable (issue #548 learned this the same way).

Dry-run by default; pass ``--apply`` to write. NOT wired into the runtime.

    uv run python -m services.open15_postack_reject_repair --date 2026-08-18
    uv run python -m services.open15_postack_reject_repair --date 2026-08-18 --apply
"""

from __future__ import annotations

import argparse

from utils.logging import get_logger

logger = get_logger(__name__)


def _quote_derived_pnl(row) -> tuple[float | None, float | None]:
    """Recompute the row's P&L from the QUOTE prices it captured at the time.

    A demoted row is a paper row, and a paper row is priced exactly as a sandbox
    run would have been (#548). The corrupt rows carry a fill-derived P&L
    computed from limit prices, which is neither the quote estimate nor a real
    fill — so it has to be rebuilt from the premiums/prices the strategy
    actually observed.
    """
    from services.open15_breakout_service import mis_round_trip_charges
    from services.open15_option_shadow import option_round_trip_charges

    qty = int(row.quantity or 0)
    if not qty:
        return None, None

    if (row.instrument or "stock") == "option":
        entry, exit_ = row.opt_entry_premium, row.opt_exit_premium
        if entry is None or exit_ is None:
            return None, None
        return (exit_ - entry) * qty, option_round_trip_charges(entry * qty, exit_ * qty)

    entry, exit_ = row.trigger_price, row.exit_price
    if entry is None or exit_ is None:
        return None, None
    long_side = row.side == "L"
    gross = ((exit_ - entry) if long_side else (entry - exit_)) * qty
    buy_value = (entry if long_side else exit_) * qty
    sell_value = (exit_ if long_side else entry) * qty
    return gross, mis_round_trip_charges(buy_value, sell_value)


def find_corrupt(date: str) -> list[dict]:
    """Rows the broker says were never filled but which we published as real."""
    from database.auth_db import get_first_available_api_key
    from database.open15_breakout_db import Open15Trade, db_session
    from services.open15_fill_reconcile import fetch_fill, is_terminal_unfilled

    api_key = get_first_available_api_key()
    if not api_key:
        logger.warning("open15 repair: no API key / broker session")
        return []

    try:
        rows = (
            db_session.query(Open15Trade)
            .filter(Open15Trade.trade_date == date, Open15Trade.fill == "real")
            .all()
        )
        out = []
        for row in rows:
            if not row.entry_order_id:
                continue
            leg = fetch_fill(row.entry_order_id, api_key)
            if leg is None or not is_terminal_unfilled(leg["order_status"]):
                continue
            gross, charges = _quote_derived_pnl(row)
            out.append(
                {
                    "id": row.id,
                    "symbol": row.symbol,
                    "published_pnl": row.pnl,
                    "published_source": row.pnl_source,
                    "quote_pnl": gross,
                    "quote_charges": charges,
                    "order_status": leg["order_status"],
                    "message": leg["message"],
                }
            )
        return out
    finally:
        db_session.remove()


def _repair_day_log(date: str, symbols: set[str], apply: bool) -> str:
    """Relabel the day log's `entry`/`exit` events for the repaired symbols.

    The page reads THESE, not the journal. An `entry` that stays
    ``order_status='success'`` keeps the symbol in the digest's entered count
    and its outcome row keeps a real fill; the `exit` event keeps publishing the
    P&L chip. Both have to move.

    Idempotent — a log already carrying a `post_ack` rejection is left alone.
    """
    from database.open15_breakout_db import get_day_log, save_day_log

    events = get_day_log(date)
    if not events:
        return "no day log stored"
    if any(e.get("event") == "entry_rejected" and e.get("post_ack") for e in events):
        return "already repaired"

    out: list[dict] = []
    added = 0
    for ev in events:
        sym = ev.get("symbol")
        kind = ev.get("event")
        if kind == "entry" and sym in symbols:
            out.append(ev)
            out.append(
                {
                    "ts": ev.get("ts"),
                    "event": "entry_rejected",
                    "symbol": sym,
                    "instrument": ev.get("instrument") or "stock",
                    "contract": ev.get("contract"),
                    "qty": ev.get("qty"),
                    "entry_price": ev.get("premium") or ev.get("trigger_price"),
                    "watch_source": ev.get("watch_source") or "seed",
                    "order_id": ev.get("order_id"),
                    "error": "broker rejected the order after acknowledging it",
                    "fill": "paper",
                    "paper_capped": False,
                    "slot_released": True,
                    "post_ack": True,
                    "backfilled": True,
                }
            )
            added += 1
            continue
        if kind == "exit" and sym in symbols:
            # the exit order was rejected too — nothing was sold, so this is a
            # paper close. `pnl` stays the NET the row renders (issue #552).
            out.append({**ev, "event": "exit_paper", "fill": "paper", "backfilled": True})
            continue
        out.append(ev)

    if apply and not save_day_log(date, out):
        return "SAVE FAILED"
    return f"{'wrote' if apply else 'would write'} {added} events, relabelled the exits"


def repair(date: str, apply: bool) -> dict:
    """Detect, reset, re-reconcile through the fixed path, repair the day log."""
    from database.open15_breakout_db import update_trade
    from services.open15_fill_reconcile import reconcile_fills

    corrupt = find_corrupt(date)
    result: dict = {"date": date, "apply": apply, "found": corrupt}
    if not corrupt:
        result["status"] = "nothing to repair"
        return result

    if not apply:
        result["status"] = "dry-run — pass --apply to write"
        result["day_log"] = _repair_day_log(date, {r["symbol"] for r in corrupt}, apply=False)
        return result

    for row in corrupt:
        # back to what reconciliation would have seen before it mis-priced the
        # row, so the FIXED path makes the decision rather than this script
        update_trade(
            row["id"],
            pnl=round(row["quote_pnl"], 2) if row["quote_pnl"] is not None else None,
            charges_inr=row["quote_charges"],
            pnl_source="quote",
            entry_fill_price=None,
            exit_fill_price=None,
            entry_fill_qty=None,
            exit_fill_qty=None,
            fill_reconcile_status="pending",
        )
    result["reconcile"] = reconcile_fills(date, max_rows=100)
    result["day_log"] = _repair_day_log(date, {r["symbol"] for r in corrupt}, apply=True)
    result["status"] = "repaired"
    return result


def main() -> None:  # pragma: no cover - operator one-shot
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="trade_date, YYYY-MM-DD")
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = parser.parse_args()

    out = repair(args.date, args.apply)
    print(f"date={out['date']} apply={out['apply']} status={out['status']}")
    for row in out["found"]:
        print(
            f"  #{row['id']} {row['symbol']}: published {row['published_pnl']} "
            f"({row['published_source']}) -> broker says {row['order_status']}"
            f" | {row['message']}"
        )
    if "reconcile" in out:
        print(f"  reconcile: {out['reconcile']}")
    print(f"  day log: {out.get('day_log')}")


if __name__ == "__main__":  # pragma: no cover - operator one-shot
    main()
