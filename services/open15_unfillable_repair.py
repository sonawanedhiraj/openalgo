"""One-shot operator repair for open15 rows that were PAPER-priced on a
structural broker refusal (issue #715).

Before #715 every broker rejection became a PAPER fill (#548) and was priced
at the exit time as if the day had run in sandbox. That is the right answer
for a refusal the same order could have survived — a static-IP 403, an RMS
funds rejection — and the wrong one for Zerodha's OI floor (#595): a contract
with fewer than 500 lots of open interest cannot be bought under ANY variant
of the strategy, so its paper P&L is money that was never obtainable.
MAXHEALTH on 2026-09-09 carried +Rs15,855 of it; nine rows across five days
carry the same broker message.

This script reclassifies those rows the way the FIXED live path now journals
them — ``fill='none'``, ``reason='entry_rejected_unfillable'``, no pricing —
using the same ``classify_unfillable_rejection`` the live path uses, so the
repair and the runtime cannot disagree about what counts as unfillable.

The day log is repaired too. The ``/logs`` page renders the DECISION LOG, not
the journal (#548 learned this): the ``exit_paper`` event for the symbol is
dropped, its ``entry_rejected`` event is re-labelled, and the day's ``summary``
counts move from ``paper`` to ``unfillable``.

Dry-run by default; pass ``--apply`` to write. NOT wired into the runtime.

    uv run python -m services.open15_unfillable_repair --date 2026-09-09
    uv run python -m services.open15_unfillable_repair --all --apply
"""

from __future__ import annotations

import argparse

from utils.logging import get_logger

logger = get_logger(__name__)

# exit-side pricing the paper path wrote and an unfillable row must not carry
_CLEARED_FIELDS = {
    "pnl": None,
    "charges_inr": None,
    "opt_pnl": None,
    "opt_charges_inr": None,
    "exit_ts": None,
    "exit_price": None,
    "exit_order_id": None,
    "exit_status": None,
    "opt_exit_premium": None,
    "opt_exit_bid": None,
    "opt_exit_ask": None,
    "pnl_source": None,
    "sim_quantity": None,
    "fill_reconcile_status": "not_applicable",
}


def find_rows(date: str | None) -> list[dict]:
    """Rejected rows whose stored broker message is a structural refusal.

    Any rejected row not already carrying the unfillable reason qualifies —
    priced paper rows AND the unpriced ``entry_rejected_paper_cap`` shape,
    which spent a paper-cap slot it was never entitled to. Real fills are
    never touched: a row the broker filled is not a rejection whatever its
    message says.
    """
    from database.open15_breakout_db import Open15Trade, db_session
    from services.open15_liquidity import UNFILLABLE_REASON, classify_unfillable_rejection

    try:
        q = db_session.query(Open15Trade).filter(
            Open15Trade.status == "rejected",
            Open15Trade.error_message.isnot(None),
        )
        if date:
            q = q.filter(Open15Trade.trade_date == date)
        out = []
        for r in q.order_by(Open15Trade.trade_date, Open15Trade.id).all():
            if r.reason == UNFILLABLE_REASON:
                continue
            reason = classify_unfillable_rejection(r.error_message)
            if not reason:
                continue
            out.append(
                {
                    "id": r.id,
                    "date": r.trade_date,
                    "symbol": r.symbol,
                    "fill": r.fill,
                    "old_reason": r.reason,
                    "pnl": r.pnl,
                    "unfillable": reason,
                }
            )
        return out
    finally:
        db_session.remove()


def _repair_day_log(date: str, by_symbol: dict[str, str], apply: bool) -> str:
    """Re-label the day's ``entry_rejected`` events and drop their paper exits.

    ``by_symbol`` maps symbol -> unfillable reason. Idempotent: an event
    already carrying ``unfillable`` is left alone and its exit is already gone.
    """
    from database.open15_breakout_db import get_day_log, save_day_log

    events = get_day_log(date)
    if not events:
        return "no day log stored"
    out: list[dict] = []
    relabelled = dropped = already = 0
    for ev in events:
        kind = ev.get("event")
        sym = ev.get("symbol")
        if kind == "entry_rejected" and sym in by_symbol and ev.get("unfillable"):
            already += 1
        if kind == "entry_rejected" and sym in by_symbol and not ev.get("unfillable"):
            out.append(
                {
                    **ev,
                    "fill": "none",
                    "paper_capped": False,
                    "unfillable": by_symbol[sym],
                    "repaired": "715",
                }
            )
            relabelled += 1
            continue
        if kind == "exit_paper" and sym in by_symbol:
            dropped += 1
            continue
        if kind == "summary" and relabelled:
            n_paper = int(ev.get("paper") or 0)
            out.append(
                {
                    **ev,
                    "paper": max(0, n_paper - relabelled),
                    "unfillable": int(ev.get("unfillable") or 0) + relabelled,
                }
            )
            continue
        out.append(ev)
    if not relabelled and not dropped:
        if already:
            return "already repaired"
        # a destroyed log (#612) has nothing to relabel — say so rather than
        # claiming a repair that never happened
        return f"no entry_rejected/exit_paper events for these symbols ({len(events)} events)"
    if apply and not save_day_log(date, out):
        return "SAVE FAILED"
    return (
        f"{'relabelled' if apply else 'would relabel'} {relabelled} entry_rejected, "
        f"{'dropped' if apply else 'would drop'} {dropped} exit_paper"
    )


def repair(date: str | None, apply: bool) -> dict:
    """Reclassify every qualifying row (one date, or all) and its day log."""
    from database.open15_breakout_db import update_trade
    from services.open15_liquidity import UNFILLABLE_REASON

    rows = find_rows(date)
    result: dict = {"date": date or "all", "apply": apply, "found": rows, "day_logs": {}}
    if not rows:
        result["status"] = "nothing to repair"
        return result
    dates: dict[str, dict[str, str]] = {}
    for r in rows:
        dates.setdefault(r["date"], {})[r["symbol"]] = r["unfillable"]
    if apply:
        for r in rows:
            ok = update_trade(
                r["id"],
                fill="none",
                reason=UNFILLABLE_REASON,
                status="rejected",
                **_CLEARED_FIELDS,
            )
            r["written"] = ok
            if not ok:
                logger.error("open15 unfillable repair: row %s update FAILED", r["id"])
    for d, by_symbol in dates.items():
        result["day_logs"][d] = _repair_day_log(d, by_symbol, apply)
    result["status"] = "repaired" if apply else "dry-run — pass --apply to write"
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="trade date YYYY-MM-DD")
    g.add_argument("--all", action="store_true", help="every date in the journal")
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()
    res = repair(None if args.all else args.date, args.apply)
    print(f"[{res['status']}] {res['date']}")
    for r in res["found"]:
        print(
            f"  {r['date']} {r['symbol']:<12} id={r['id']:<4} {r['fill'] or '-':<6} "
            f"{r['old_reason']:<26} pnl={r['pnl']!s:<10} -> none / {r['unfillable']}"
            + ("" if r.get("written", True) else "  ** UPDATE FAILED **")
        )
    for d, msg in res["day_logs"].items():
        print(f"  day log {d}: {msg}")


if __name__ == "__main__":
    main()
