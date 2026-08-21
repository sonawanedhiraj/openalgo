"""One-shot operator backfill for broker-rejected open15 entries (issue #548).

Repairs rows written BEFORE #548 shipped, when a rejected entry was journaled
`status='error'` with no message and `flatten` skipped it forever — leaving no
exit, no P&L and no trace of the cause outside the raw app log. The known
population is the three 2026-08-05 static-IP rejections (JUBLFOOD, GODREJPROP,
DLF), but the query is generic.

Post-#548 the live path handles this itself: `flatten` prices the paper exit
from a live quote at the exit time. This script exists only because those
quotes are long gone for historical rows, so the exit has to be reconstructed
from the broker's 1m bars instead.

Dry-run by default; pass ``--apply`` to write. NOT wired into the runtime.

    uv run python -m services.open15_rejection_backfill --date 2026-08-05
    uv run python -m services.open15_rejection_backfill --date 2026-08-05 --apply
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

from utils.logging import get_logger

logger = get_logger(__name__)


def _reconstruct(row) -> dict | None:
    """Price a rejected row's would-have-been exit from broker 1m bars.

    Entry uses the premium/price CAPTURED AT THE TRIGGER SECOND (already on the
    row) rather than a bar open: that is the quote the order was actually sized
    and sent against, so it is the honest "what this order would have got".
    Only the exit — never observed, because no flatten ran — comes from bars.
    """
    from services.open15_breakout_service import mis_round_trip_charges
    from services.open15_option_shadow import (
        _fetch_1m_bars,
        option_round_trip_charges,
        premiums_from_bars,
    )

    is_option = (row.instrument or "stock") == "option"
    symbol = row.opt_symbol if is_option else row.symbol
    entry_px = row.opt_entry_premium if is_option else row.trigger_price
    if not symbol or not entry_px or not row.trigger_minute:
        return None

    bars = _fetch_1m_bars(symbol, row.trade_date) if is_option else None
    if is_option:
        if not bars:
            logger.warning("open15 backfill: no bars for %s — left unpriced", symbol)
            return None
        _entry_from_bars, exit_px = premiums_from_bars(bars, row.trigger_minute)
    else:
        from database.auth_db import get_first_available_api_key
        from services.history_service import get_history

        api_key = get_first_available_api_key()
        ok, data, _ = get_history(
            symbol=symbol,
            exchange="NSE",
            interval="1m",
            start_date=row.trade_date,
            end_date=row.trade_date,
            api_key=api_key,
        )
        if not ok:
            logger.warning("open15 backfill: history failed for %s: %s", symbol, data)
            return None
        _e, exit_px = premiums_from_bars(data.get("data") or [], row.trigger_minute)
    if not exit_px:
        logger.warning("open15 backfill: no exit price for %s — left unpriced", symbol)
        return None

    qty = row.quantity or 0
    if is_option:
        # both directions are premium BUYS — the strategy never sells options
        pnl = (exit_px - entry_px) * qty
        charges = option_round_trip_charges(entry_px * qty, exit_px * qty)
        lot = row.opt_lot_size or 1
        lots = qty // lot if lot else 1
        per_lot = round(charges / lots, 2) if charges and lots else None
        extra = {
            "opt_exit_premium": exit_px,
            "opt_charges_inr": per_lot,
            "opt_pnl": round((exit_px - entry_px) * lot - (per_lot or 0.0), 2),
        }
    else:
        d = (exit_px - entry_px) if row.side == "L" else (entry_px - exit_px)
        pnl = d * qty
        buy_px = entry_px if row.side == "L" else exit_px
        sell_px = exit_px if row.side == "L" else entry_px
        charges = mis_round_trip_charges(buy_px * qty, sell_px * qty)
        extra = {"exit_price": exit_px}
    return {
        "status": "rejected",
        "fill": "paper",
        "exit_status": "not_placed",
        "exit_order_id": "",
        "exit_ts": f"{row.trade_date}T09:30:00+05:30",
        "pnl": round(pnl, 2),
        "charges_inr": charges,
        **extra,
    }


def _paper_rows(date: str) -> list[dict]:
    """Every paper row for a date, read back from the journal.

    Read from the DB rather than from this run's work list so the day-log
    repair is independent of it — the rows may have been fixed by an earlier
    invocation (or a partial one), and the log still needs its events.
    """
    from database.open15_breakout_db import Open15Trade, db_session

    try:
        rows = (
            db_session.query(Open15Trade)
            .filter(
                Open15Trade.trade_date == date,
                Open15Trade.status == "rejected",
                Open15Trade.fill == "paper",
            )
            .order_by(Open15Trade.id)
            .all()
        )
        is_opt = lambda r: (r.instrument or "stock") == "option"  # noqa: E731
        return [
            {
                "symbol": r.symbol,
                "instrument": r.instrument,
                "opt_symbol": r.opt_symbol,
                "quantity": r.quantity,
                "entry_price": r.opt_entry_premium if is_opt(r) else r.trigger_price,
                "exit_price": r.opt_exit_premium if is_opt(r) else r.exit_price,
                "pnl": r.pnl,
                "charges_inr": r.charges_inr,
                # the per-lot net the UI shows for option rows; stock rows net
                # the modelled MIS round trip off the gross
                "net_pnl": (
                    r.opt_pnl
                    if is_opt(r)
                    else (round(r.pnl - (r.charges_inr or 0.0), 2) if r.pnl is not None else None)
                ),
                "error_message": r.error_message,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("open15 backfill: paper-row read failed for %s", date)
        return []
    finally:
        db_session.remove()


def _repair_day_log(date: str, repaired: list[dict], apply: bool) -> str:
    """Inject the missing ``entry_rejected`` / ``exit_paper`` events (issue #548).

    Repairing only the journal rows is not enough: the decision log IS the
    operator-facing record, and the ``/logs`` page builds its rejection banner
    and outcome rows from these events. A pre-#548 day has the `entry` event
    with ``order_status='error'`` and nothing after it, so without this the page
    still shows a dangling entry that appears to have opened a position.

    Idempotent — a log already carrying ``entry_rejected`` is left alone.
    """
    from database.open15_breakout_db import get_day_log, save_day_log

    events = get_day_log(date)
    if not events:
        return "no day log stored"
    if any(e.get("event") == "entry_rejected" for e in events):
        return "already repaired"

    by_symbol = {r["symbol"]: r for r in repaired}
    out: list[dict] = []
    for ev in events:
        # the exit events belong at the exit time, i.e. just before the
        # end-of-window bookkeeping the service writes at 09:30
        if ev.get("event") in ("watch_stats", "no_entry", "summary") and by_symbol:
            for sym, r in by_symbol.items():
                out.append(
                    {
                        "ts": "09:30:00.000",
                        "event": "exit_paper",
                        "symbol": sym,
                        "instrument": r.get("instrument") or "stock",
                        "contract": r.get("opt_symbol"),
                        "qty": r.get("quantity"),
                        "exit_price": r.get("exit_price"),
                        "gross": r.get("pnl"),
                        "charges": r.get("charges_inr"),
                        "pnl": r.get("net_pnl"),
                        "fill": "paper",
                        "reason": "entry_rejected",
                        "note": "order was rejected — sandbox-equivalent, no money moved",
                        "backfilled": True,
                    }
                )
            by_symbol = {}
        out.append(ev)
        if ev.get("event") == "entry" and ev.get("symbol") in {r["symbol"] for r in repaired}:
            r = next(x for x in repaired if x["symbol"] == ev["symbol"])
            out.append(
                {
                    "ts": ev.get("ts"),
                    "event": "entry_rejected",
                    "symbol": ev["symbol"],
                    "instrument": r.get("instrument") or "stock",
                    "contract": r.get("opt_symbol"),
                    "qty": r.get("quantity"),
                    "entry_price": r.get("entry_price"),
                    "watch_source": ev.get("watch_source") or "seed",
                    "error": r.get("error_message"),
                    "fill": "paper",
                    "paper_capped": False,
                    "slot_released": True,
                    "backfilled": True,
                }
            )
    if apply and not save_day_log(date, out):
        return "SAVE FAILED"
    return f"{'wrote' if apply else 'would write'} {len(out) - len(events)} events"


def backfill(date: str | None, error_message: str | None, apply: bool) -> dict:
    """Repair pre-#548 rejected rows. Returns a small status dict."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db, update_trade

    # the `fill` / `error_message` columns land via `_ensure_columns` at app
    # boot; a backfill run against a not-yet-restarted install must add them
    # itself or every query below fails on the missing column
    init_db()

    repaired, unpriced, skipped = 0, 0, 0
    try:
        q = db_session.query(Open15Trade).filter(
            Open15Trade.status == "error",
            Open15Trade.entry_status == "error",
        )
        if date:
            q = q.filter(Open15Trade.trade_date == date)
        # snapshot to plain objects BEFORE any write: `update_trade` ends with
        # `db_session.remove()`, which detaches these instances — reading an
        # attribute off row N+1 after updating row N raises DetachedInstanceError
        cols = (
            "id trade_date symbol side instrument quantity trigger_price trigger_minute "
            "opt_symbol opt_lot_size opt_entry_premium error_message"
        ).split()
        rows = [
            SimpleNamespace(**{c: getattr(r, c) for c in cols})
            for r in q.order_by(Open15Trade.id).all()
        ]
        db_session.remove()
        if not rows:
            # not an early exit: the journal rows may already be repaired from
            # an earlier run while the day LOG still lacks its events
            print("no pre-#548 rejected rows matched")
        for row in rows:
            fields = _reconstruct(row)
            if fields is None:
                # still made terminal — an unpriced paper row beats a dangling
                # `error` row that flatten will never look at again
                fields = {
                    "status": "rejected",
                    "fill": "paper",
                    "exit_status": "not_placed",
                    "exit_ts": f"{row.trade_date}T09:30:00+05:30",
                }
                unpriced += 1
            else:
                repaired += 1
            fields["reason"] = "entry_rejected"
            if error_message and not row.error_message:
                fields["error_message"] = error_message
            print(
                f"{'APPLY ' if apply else 'DRY   '} id={row.id} {row.trade_date} "
                f"{row.symbol} {row.opt_symbol or ''} qty={row.quantity} "
                f"-> status=rejected fill=paper pnl={fields.get('pnl')} "
                f"charges={fields.get('charges_inr')}"
            )
            if apply and not update_trade(row.id, **fields):
                skipped += 1
        out = {"repaired": repaired, "unpriced": unpriced, "skipped": skipped}
        if date:
            out["day_log"] = _repair_day_log(date, _paper_rows(date), apply)
        return out
    except Exception:
        logger.exception("open15 backfill: failed")
        return {"repaired": repaired, "unpriced": unpriced, "skipped": skipped, "error": True}
    finally:
        db_session.remove()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="restrict to one trade_date (YYYY-MM-DD)")
    ap.add_argument(
        "--error-message",
        help="broker rejection text to stamp on rows that have none "
        "(recoverable from the day's app log)",
    )
    ap.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    args = ap.parse_args()
    result = backfill(args.date, args.error_message, args.apply)
    print(("APPLIED " if args.apply else "DRY-RUN ") + str(result))


if __name__ == "__main__":
    main()
