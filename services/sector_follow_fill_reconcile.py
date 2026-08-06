"""Reconcile sector_follow_cap5_vol journal rows against the broker's own fills.

Issue #562. ``sector_follow_trades.status`` was written from the broker's
**acknowledgement** and never revisited. A Kite ``place_order`` returns an
``order_id`` the moment the request is accepted; the order can still be rejected
downstream by RMS (insufficient funds is the common one). Seven orders reached
the real broker in 2026-07/08 and were acknowledged::

    2026-07-29 15:20:04  status=200 {"status":"success","data":{"order_id":"260729191071883"}}
    2026-08-05 15:05:02  {"status":"success","orderid":"260805191287166"}
    2026-08-06 15:05:02  {"status":"success","orderid":"260806191325188"}

Only the last one actually filled. The other six were rejected silently: nothing
alerted, nothing retried, and all seven rows still read ``placed`` — so the
journal, the dashboard and the P&L all reported acknowledgements as trades.

``open15_vol_breakout`` already solved this (#555); this module applies the same
discipline to sector_follow. The rules it inherits, each load-bearing:

**The decision price is never overwritten.** ``price`` stays the quote at signal
time and the broker's ``average_price`` lands in ``fill_price``, so
``fill_price - price`` remains measurable as slippage. Reconciliation adds
knowledge; it does not rewrite the decision record.

**Deferred, never on the order path.** ``run_entry`` places orders inside the
15:05 scheduler job; a synchronous broker round-trip per order there would delay
later signals in the same batch. This runs at the EOD summary job and is retried
on the next boot for legs the broker had not yet reported.

**A terminal rejection is loud.** The whole point of the issue is that six
rejections passed unnoticed, so a row transitioning to ``rejected`` logs at
ERROR and raises an operator alert. Silence is the bug.

**Never guess.** An unreadable order status leaves the row exactly as it was and
is retried later — it never downgrades a row to rejected on a failed lookup,
which would be inventing a rejection the broker never reported.
"""

from __future__ import annotations

import os

from utils.logging import get_logger

logger = get_logger(__name__)

STRATEGY_NAME = "sector_follow_cap5_vol"

# Order states in which asking again is pointless — the broker has given its
# final answer and no fill is ever coming.
_TERMINAL_UNFILLED = ("rejected", "cancelled", "canceled")
# States that mean the order transacted.
_FILLED = ("complete", "completed", "filled")


def _enabled() -> bool:
    return os.getenv("SECTOR_FOLLOW_FILL_RECONCILE_ENABLED", "true").lower() == "true"


def _as_float(value) -> float | None:
    """Coerce a broker price field to float; None when absent or unusable.

    A zero ``average_price`` means "not filled / not reported", never "filled at
    zero" — treating it as a price would publish a P&L equal to the whole
    notional as if it were reconciled truth.
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
    """``{price, qty, order_status}`` for one order, or None if unreadable.

    Routing is automatic for both books: ``get_order_status`` resolves the
    analyze overlay and, failing that, checks ``sandbox_order_exists`` — so a
    sandbox order id is answered from ``sandbox.db`` and a live one from the
    broker orderbook, without this caller having to know which.

    None means "could not read", which is NOT "rejected" — the caller must not
    conflate them.
    """
    try:
        from services.orderstatus_service import get_order_status

        ok, resp, _code = get_order_status(
            {"strategy": STRATEGY_NAME, "orderid": str(order_id)}, api_key=api_key
        )
        if not ok:
            logger.info(
                "sector_follow fill-reconcile: order status unavailable for %s — %s",
                order_id,
                (resp or {}).get("message"),
            )
            return None
        data = (resp or {}).get("data") or {}
        return {
            "price": _as_float(data.get("average_price") or data.get("price")),
            "qty": _as_int(data.get("quantity") or data.get("filled_quantity")),
            "order_status": str(data.get("order_status") or data.get("status") or "").lower(),
            "message": str(data.get("message") or data.get("status_message") or "")[:255] or None,
        }
    except Exception:
        logger.exception("sector_follow fill-reconcile: order status raised for %s", order_id)
        return None


def _resolve_api_key() -> str | None:
    try:
        from database.auth_db import get_first_available_api_key

        return get_first_available_api_key()
    except Exception:
        logger.exception("sector_follow fill-reconcile: api key lookup failed")
        return None


def _notify(message: str) -> None:
    """Operator alert. Best-effort — never raises into the caller."""
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("sector_follow_fill_reconcile", message)
    except Exception:
        logger.exception("sector_follow fill-reconcile: alert failed")


def reconcile_unreconciled(limit: int = 200, api_key: str | None = None) -> dict:
    """Ask the broker what really happened to every acknowledged-but-unverified row.

    Considers rows with ``status='placed'``, a non-null ``order_id`` and no
    terminal ``fill_reconcile_status`` yet. Each is resolved to one of:

    * **reconciled** — the broker reports a fill; ``fill_price``/``fill_qty`` set.
    * **rejected**   — the broker gave a terminal unfilled answer. ``status`` is
      corrected to ``rejected``, the broker's message is stored, and the
      operator is alerted (loudly — six of these passed unnoticed).
    * **pending**    — no answer yet, or the status read failed. The row is left
      untouched and retried next run. Never downgraded on a failed lookup.

    Returns a counts dict. Never raises.
    """
    if not _enabled():
        return {"skipped": "disabled"}

    try:
        from database.sector_follow_db import SectorFollowTrade, db_session
    except Exception:
        logger.exception("sector_follow fill-reconcile: db import failed")
        return {"error": "db_import"}

    key = api_key or _resolve_api_key()
    if not key:
        logger.warning("sector_follow fill-reconcile: no api key available; skipping")
        return {"skipped": "no_api_key"}

    counts = {"checked": 0, "reconciled": 0, "rejected": 0, "pending": 0}
    newly_rejected: list[str] = []
    try:
        rows = (
            db_session.query(SectorFollowTrade)
            .filter(SectorFollowTrade.status == "placed")
            .filter(SectorFollowTrade.order_id.isnot(None))
            .filter(
                (SectorFollowTrade.fill_reconcile_status.is_(None))
                | (SectorFollowTrade.fill_reconcile_status == "pending")
            )
            .order_by(SectorFollowTrade.id.desc())
            .limit(limit)
            .all()
        )
        for row in rows:
            counts["checked"] += 1
            fill = fetch_fill(row.order_id, key)
            if fill is None:
                # Unreadable != rejected. Leave the row alone and retry.
                row.fill_reconcile_status = "pending"
                counts["pending"] += 1
                continue
            state = fill.get("order_status") or ""
            if state in _TERMINAL_UNFILLED:
                row.status = "rejected"
                row.fill_reconcile_status = "unavailable"
                row.error_message = (
                    fill.get("message") or f"broker reported {state} after acknowledgement"
                )[:255]
                counts["rejected"] += 1
                newly_rejected.append(f"{row.symbol} {row.side} qty={row.quantity} ({state})")
                logger.error(
                    "sector_follow fill-reconcile: order %s for %s was ACKNOWLEDGED but "
                    "the broker reports %s — journal corrected to rejected",
                    row.order_id,
                    row.symbol,
                    state,
                )
                continue
            price = fill.get("price")
            if state in _FILLED and price is not None:
                row.fill_price = price
                row.fill_qty = fill.get("qty")
                row.fill_reconcile_status = "reconciled"
                counts["reconciled"] += 1
                slip = price - float(row.price or 0.0)
                logger.info(
                    "sector_follow fill-reconcile: %s %s filled @ %.2f (decision %.2f, "
                    "slippage %+.2f)",
                    row.symbol,
                    row.side,
                    price,
                    float(row.price or 0.0),
                    slip,
                )
                continue
            row.fill_reconcile_status = "pending"
            counts["pending"] += 1
        db_session.commit()
    except Exception:
        db_session.rollback()
        logger.exception("sector_follow fill-reconcile: run failed")
        return {"error": "run_failed", **counts}
    finally:
        db_session.remove()

    if newly_rejected:
        _notify(
            "🚫 sector_follow_cap5_vol: "
            f"{len(newly_rejected)} order(s) were acknowledged by the broker but never "
            "filled:\n  " + "\n  ".join(newly_rejected)
        )
    if counts["checked"]:
        logger.info("sector_follow fill-reconcile: %s", counts)
    return counts


def _cli_main(argv: list[str] | None = None) -> int:
    """``uv run python -m services.sector_follow_fill_reconcile [--limit N]``.

    Operator entry point for reconciling the historical backlog — the 7 live
    orders from 2026-07-29..08-06 that have never been checked.
    """
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        prog="python -m services.sector_follow_fill_reconcile",
        description="Reconcile sector_follow journal rows against broker fills.",
    )
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv)
    print(_json.dumps(reconcile_unreconciled(limit=args.limit), indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_cli_main())
