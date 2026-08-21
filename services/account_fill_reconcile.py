"""Reconcile child mirror orders against what the broker actually did (#637).

`account_fanout_service` journals `status='placed'` when the child broker
returns HTTP 200. That is the **acknowledgement**, not a fill. Zerodha returns
200 with an order id and its RMS can reject the order afterwards — the exact
sequence that published a fabricated +Rs7,680 trade on the parent path in #626.

Before this module there was **no fill reconciliation for `account_orders` at
all**: a post-ACK rejection on a child stayed `placed` for ever, the EOD mirror
summary reported a trade that never happened, and nothing alerted.

Three properties, each load-bearing:

**It reads the RAW broker orderbook, not the mapper.** `transform_order_data`
drops `status_message` and (for most brokers) `filled_quantity`, so the mapped
view cannot tell a rejection from a fill — `price` and `quantity` are what we
ASKED for and read identically either way (#626). `get_order_book(token)`
returns the broker's own payload with the reason intact.

**It uses each CHILD's token.** A child is a separate broker account; there is
no OpenAlgo API key for it, so `orderstatus_service` (which resolves by API key
to the PRIMARY user's session) is the wrong door — it would answer for the
parent's book. Same #497 rule the funds check follows.

**It only ever corrects a row DOWNWARDS.** A `placed` row the broker refused
becomes `rejected` with the broker's reason. A confirmed fill is left alone —
promoting rows on our own authority is how an ACK became a fill in the first
place. Idempotent by construction: re-running re-reads the same orderbook and
reaches the same verdict, so no marker column is needed.

Deferred, never on the order path: the fan-out is fire-and-forget on a thread
pool, and a synchronous orderbook call there would serialise every child.
"""

from __future__ import annotations

import os
from importlib import import_module

from utils.logging import get_logger

logger = get_logger(__name__)

# Broker states meaning "this order is finished and nothing was bought or sold".
_TERMINAL_UNFILLED = ("rejected", "cancelled", "canceled")


def _enabled() -> bool:
    return os.getenv("MULTI_ACCOUNT_FILL_RECONCILE_ENABLED", "true").lower() == "true"


def is_terminal_unfilled(status: str | None) -> bool:
    return str(status or "").strip().lower() in _TERMINAL_UNFILLED


def _first_present(row: dict, *keys: str):
    """First key present with a non-None value — never truthiness (#626).

    On a rejected order every meaningful field is 0, so `a or b` reads the
    wrong one exactly when it matters.
    """
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def index_orderbook(payload) -> dict[str, dict]:
    """`{order_id: raw_order}` from a broker orderbook response.

    Accepts the two shapes brokers return — a bare list, or `{"data": [...]}`.
    Anything else yields `{}`, which the caller treats as "unreadable" and so
    corrects nothing.
    """
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        oid = _first_present(row, "order_id", "orderid", "orderId")
        if oid is not None:
            out[str(oid)] = row
    return out


def verdict_for(order: dict) -> tuple[str, str | None]:
    """`(verdict, reason)` where verdict is `rejected` / `ok`.

    `rejected` is returned ONLY on an explicit terminal-unfilled broker status.
    Everything else — filled, still open, or a status we do not recognise — is
    `ok`, i.e. leave the row alone. Guessing in the other direction would let a
    parsing gap silently erase a real trade from the record.
    """
    status = _first_present(order, "status", "order_status")
    if not is_terminal_unfilled(status):
        return "ok", None
    reason = _first_present(order, "status_message", "status_message_raw", "message")
    return "rejected", (str(reason).strip() if reason else None)


def _notify(message: str) -> None:
    logger.error("%s", message)
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("multi_account_mirror", f"⚠ {message}")
    except Exception:
        logger.exception("mirror fill-reconcile: alert failed")


def reconcile_account_fills(date_utc: str | None = None) -> dict:
    """Correct `placed` rows the broker actually refused. Never raises."""
    if not _enabled():
        return {"status": "disabled", "checked": 0, "corrected": 0}

    from database import account_orders_db, broker_accounts_db
    from database.auth_db import get_auth_token

    try:
        rows = [
            r for r in account_orders_db.list_orders(date_utc=date_utc) if r["status"] == "placed"
        ]
    except Exception:
        logger.exception("mirror fill-reconcile: journal scan failed")
        return {"status": "error", "checked": 0, "corrected": 0}

    rows = [r for r in rows if r.get("broker_orderid")]
    if not rows:
        return {"status": "ok", "checked": 0, "corrected": 0}

    # one orderbook call per child, not per row
    by_account: dict[int, list[dict]] = {}
    for row in rows:
        by_account.setdefault(row["account_id"], []).append(row)

    checked = corrected = 0
    for account_id, account_rows in by_account.items():
        try:
            account = broker_accounts_db.get_account(account_id)
            if not account:
                continue
            token = get_auth_token(broker_accounts_db.auth_name(account_id))
            if not token:
                logger.info(
                    "mirror fill-reconcile: no session for account %s — %s row(s) unchecked",
                    account_id,
                    len(account_rows),
                )
                continue
            broker_module = import_module(f"broker.{account['broker']}.api.order_api")
            book = index_orderbook(broker_module.get_order_book(token))
            if not book:
                logger.warning(
                    "mirror fill-reconcile: unreadable orderbook for account %s — "
                    "%s row(s) left as placed",
                    account_id,
                    len(account_rows),
                )
                continue

            name = account.get("display_name") or f"account {account_id}"
            for row in account_rows:
                order = book.get(str(row["broker_orderid"]))
                if order is None:
                    # not in today's book (older date, or a broker that prunes)
                    continue
                checked += 1
                verdict, reason = verdict_for(order)
                if verdict != "rejected":
                    continue
                text = reason or "broker rejected the order after acknowledging it"
                if account_orders_db.update_status(
                    row["id"], status="rejected", error_text=text[:255]
                ):
                    corrected += 1
                    _notify(
                        f"Mirror REJECTED after acknowledgement — {name}: "
                        f"{row['action']} {row['child_qty']} {row['symbol']}. "
                        f"It was reported as placed and did NOT happen.\n{text}"
                    )
        except Exception:
            logger.exception("mirror fill-reconcile: account %s failed", account_id)

    if corrected:
        logger.warning(
            "mirror fill-reconcile: corrected %s of %s checked mirror order(s)",
            corrected,
            checked,
        )
    return {"status": "ok", "checked": checked, "corrected": corrected}


if __name__ == "__main__":  # pragma: no cover - operator one-shot
    print(reconcile_account_fills())
