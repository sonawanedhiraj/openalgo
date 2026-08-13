"""Reconcile open15_vol_breakout journal rows against the broker's own fills.

Issue #555. Every price the strategy journals at decision time is a *quote* or a
*tick*: ``trigger_price`` is the tick that fired the volume gate,
``opt_entry_premium`` is the option quote at that instant, ``exit_price`` is the
last tick before the flatten. None of them is what the broker transacted, so the
P&L the logs page published was an estimate that had never been checked.

This module asks the broker what it actually filled (``average_price`` per leg),
recomputes the row's gross P&L and modelled charges from that, and records the
broker's own realized P&L alongside as a cross-check.

Three properties, each load-bearing:

**One P&L convention survives (issue #552).** ``pnl`` stays THE gross number and
``charges_inr`` THE charges; reconciliation OVERWRITES them in place and stamps
``pnl_source='fill'``. No consumer changes, and the chip / digest / rows cannot
drift apart again. The quote prices are untouched in their own columns, so
``fill - quote`` remains measurable as slippage.

**Deferred, never on the tick thread.** Entries are placed from the ZMQ
subscriber callback; a synchronous broker round-trip there would stall every
other symbol's tick processing. Reconciliation runs at the summary job (exit+5
min) and is retried by the next 09:10 arm for legs the broker had not yet
reported — the same catch-up shape as ``open15_option_shadow``.

**Charges stay modelled and say so.** No broker exposes per-order charges
through its API (Zerodha publishes them on the next-day contract note only), so
the modelled Zerodha formula is kept — but applied to the ACTUAL fill turnover
instead of the quote turnover. Gross matches the broker exactly; charges are an
accurate estimate and are labelled as such everywhere they are shown.

Fail-graceful throughout: a row that cannot be reconciled keeps its quote-derived
P&L and is retried, never zeroed and never raised into the scheduler.
"""

from __future__ import annotations

import os

from utils.logging import get_logger

logger = get_logger(__name__)

STRATEGY_NAME = "open15_vol_breakout"

# Order states in which asking again is pointless — the broker has given its
# final answer and no fill price is ever coming.
_TERMINAL_UNFILLED = ("rejected", "cancelled", "canceled")


def _enabled() -> bool:
    return os.getenv("OPEN15_FILL_RECONCILE_ENABLED", "true").lower() == "true"


def _as_float(value) -> float | None:
    """Coerce a broker price field to float; None when absent or unusable.

    A zero ``average_price`` means "not filled / not reported", never "filled at
    zero" — treating it as a price would compute a P&L equal to the whole
    notional and publish it as reconciled truth.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def fetch_fill(order_id: str, api_key: str) -> dict | None:
    """``{price, qty, order_status}`` for one order leg, or None if unreadable.

    Routing is automatic and correct for both books: ``get_order_status``
    resolves the analyze overlay and, failing that, checks
    ``sandbox_order_exists(orderid, apikey)`` — so a sandbox-book order id is
    answered from ``sandbox.db`` and a live one from the broker orderbook,
    without this caller having to know which. Sandbox's own order manager
    returns ``average_price`` too, so sandbox rows reconcile identically.
    """
    try:
        from services.orderstatus_service import get_order_status

        ok, resp, _code = get_order_status(
            {"strategy": STRATEGY_NAME, "orderid": str(order_id)}, api_key=api_key
        )
        if not ok:
            logger.info(
                "open15 fill-reconcile: order status unavailable for %s — %s",
                order_id,
                (resp or {}).get("message"),
            )
            return None
        data = (resp or {}).get("data") or {}
        return {
            "price": _as_float(data.get("average_price") or data.get("price")),
            "qty": _as_int(data.get("quantity") or data.get("filled_quantity")),
            "order_status": str(data.get("order_status") or data.get("status") or "").lower(),
        }
    except Exception:
        logger.exception("open15 fill-reconcile: order status raised for %s", order_id)
        return None


def recompute_pnl(row: dict, entry_px: float, exit_px: float) -> tuple[float, float | None]:
    """``(gross_pnl, modelled_charges)`` from the two FILL prices.

    Pure. Mirrors exactly what ``flatten`` computes from quotes, so the only
    thing reconciliation changes is *which prices go in* — never the formula.
    An option position is always a premium BUY (the strategy never sells
    options), so its P&L is directional in the premium regardless of the
    underlying signal's side.
    """
    qty = int(row.get("quantity") or 0)
    if row.get("instrument") == "option":
        from services.open15_option_shadow import option_round_trip_charges

        gross = (exit_px - entry_px) * qty
        charges = option_round_trip_charges(entry_px * qty, exit_px * qty)
        return gross, charges

    from services.open15_breakout_service import mis_round_trip_charges

    long_side = row.get("side") == "L"
    gross = ((exit_px - entry_px) if long_side else (entry_px - exit_px)) * qty
    buy_value = (entry_px if long_side else exit_px) * qty
    sell_value = (exit_px if long_side else entry_px) * qty
    return gross, mis_round_trip_charges(buy_value, sell_value)


def _resolve_status(entry: dict | None, exit_: dict | None) -> str:
    """``reconciled`` / ``unavailable`` / ``pending`` for a partly-answered row.

    ``unavailable`` (the broker gave a final, unfilled answer) is kept distinct
    from ``pending`` (we have not got an answer yet) so the retry loop stops
    asking about orders that will never report, and so the UI can say which of
    the two it is instead of showing an open-ended spinner forever.
    """
    legs = [leg for leg in (entry, exit_) if leg is not None]
    if any(leg.get("order_status") in _TERMINAL_UNFILLED for leg in legs):
        return "unavailable"
    return "pending"


def broker_pnl_by_symbol(api_key: str) -> dict[str, float]:
    """Realized P&L per symbol from the strategy's own position book.

    ``mode_key`` is mandatory (the #497 rule): it routes the READ to the same
    book the orders were routed to. Without it a sandbox run reads the empty
    LIVE broker book and every cross-check silently reports zero.

    Returns ``{}`` on any failure — an unreadable book means "no cross-check
    available", which must stay distinguishable from "the broker says zero".
    """
    try:
        from services.positionbook_service import get_positionbook

        ok, resp, _ = get_positionbook(api_key=api_key, mode_key=STRATEGY_NAME)
        if not ok:
            logger.info("open15 fill-reconcile: position book unreadable — no broker cross-check")
            return {}
        out: dict[str, float] = {}
        for pos in (resp or {}).get("data") or []:
            symbol = str(pos.get("symbol") or "").upper()
            pnl = pos.get("pnl", pos.get("realized_pnl"))
            if symbol and pnl is not None:
                try:
                    out[symbol] = float(pnl)
                except (TypeError, ValueError):
                    continue
        return out
    except Exception:
        logger.exception("open15 fill-reconcile: position-book read raised")
        return {}


def reconcile_fills(trade_date: str | None = None, max_rows: int = 20) -> dict:
    """Stamp broker fill prices onto REAL rows and re-derive their P&L.

    Idempotent (a fully-reconciled row is not re-queried) and fail-graceful.
    Only ``fill='real'`` rows are touched: paper and sim rows have no orders to
    reconcile, by definition.
    """
    if not _enabled():
        return {"status": "disabled", "reconciled": 0, "pending": 0}

    from sqlalchemy import or_

    from database.open15_breakout_db import Open15Trade, db_session, update_trade

    try:
        from database.auth_db import get_first_available_api_key

        api_key = get_first_available_api_key()
    except Exception:
        logger.exception("open15 fill-reconcile: api key lookup failed")
        api_key = None
    if not api_key:
        logger.warning("open15 fill-reconcile: no API key / broker session — skipping")
        return {"status": "no_session", "reconciled": 0, "pending": 0}

    try:
        q = db_session.query(Open15Trade).filter(
            Open15Trade.fill == "real",
            Open15Trade.entry_order_id.isnot(None),
            Open15Trade.entry_order_id != "",
            # a row already reconciled, or whose broker answer was final, is done
            or_(
                Open15Trade.fill_reconcile_status.is_(None),
                Open15Trade.fill_reconcile_status == "pending",
            ),
        )
        if trade_date:
            q = q.filter(Open15Trade.trade_date == trade_date)
        rows = q.order_by(Open15Trade.id.desc()).limit(max_rows).all()
        work = [
            {
                "id": r.id,
                "symbol": r.symbol,
                "opt_symbol": r.opt_symbol,
                "side": r.side,
                "instrument": r.instrument or "stock",
                "quantity": r.quantity,
                "entry_order_id": r.entry_order_id,
                "exit_order_id": r.exit_order_id,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("open15 fill-reconcile: journal scan failed")
        return {"status": "error", "reconciled": 0, "pending": 0}
    finally:
        db_session.remove()

    if not work:
        return {"status": "ok", "reconciled": 0, "pending": 0}

    book = broker_pnl_by_symbol(api_key)
    reconciled = pending = 0
    # per-row detail for the caller to write into the decision log — the module
    # itself stays free of the service so it can be run standalone from the CLI
    details: list[dict] = []

    for row in work:
        entry = fetch_fill(row["entry_order_id"], api_key)
        exit_ = fetch_fill(row["exit_order_id"], api_key) if row["exit_order_id"] else None
        fields: dict = {}
        if entry and entry["price"]:
            fields.update(entry_fill_price=entry["price"], entry_fill_qty=entry["qty"])
        if exit_ and exit_["price"]:
            fields.update(exit_fill_price=exit_["price"], exit_fill_qty=exit_["qty"])

        # the broker's own number rides along whether or not both legs reported —
        # it is an independent observation, not a product of ours. The position
        # book keys NFO option positions by the CONTRACT symbol (issue #593), so
        # an option row must look itself up by its opt_symbol, not the underlying.
        lookup = row["opt_symbol"] if row["instrument"] == "option" else row["symbol"]
        symbol_key = (lookup or "").upper()
        if symbol_key in book:
            fields["broker_pnl"] = round(book[symbol_key], 2)

        both = fields.get("entry_fill_price") and fields.get("exit_fill_price")
        if both:
            gross, charges = recompute_pnl(
                row, fields["entry_fill_price"], fields["exit_fill_price"]
            )
            fields.update(
                pnl=round(gross, 2),
                charges_inr=charges,
                pnl_source="fill",
                fill_reconcile_status="reconciled",
            )
            reconciled += 1
        else:
            fields["fill_reconcile_status"] = _resolve_status(entry, exit_)
            pending += 1

        if not update_trade(row["id"], **fields):
            logger.warning("open15 fill-reconcile: update failed for row %s", row["id"])
            continue
        details.append(
            {
                "symbol": row["symbol"],
                "entry_fill_price": fields.get("entry_fill_price"),
                "exit_fill_price": fields.get("exit_fill_price"),
                "broker_pnl": fields.get("broker_pnl"),
                "pnl": fields.get("pnl"),
                "charges": fields.get("charges_inr"),
                "pnl_source": fields.get("pnl_source", "quote"),
                "status": fields["fill_reconcile_status"],
            }
        )
        if both:
            logger.info(
                "open15 fill-reconcile: %s entry %.2f exit %.2f qty %s -> gross %.2f "
                "charges %s (was quote-derived)",
                row["symbol"],
                fields["entry_fill_price"],
                fields["exit_fill_price"],
                row["quantity"],
                fields["pnl"],
                fields["charges_inr"],
            )

    return {"status": "ok", "reconciled": reconciled, "pending": pending, "rows": details}


if __name__ == "__main__":  # pragma: no cover - operator one-shot
    print(reconcile_fills(max_rows=100))
