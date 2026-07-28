"""Order fan-out to child broker accounts — multi-account Phase 2 (issue #474).

Called from ONE seam: the tail of ``place_order_service.place_order_with_auth``,
after a LIVE parent order is ACCEPTED by the broker. Mirrors the order to every
enabled child account whose strategy allow-list contains the order's
``mode_key``, scaled to the child's capital.

Gating (ALL must hold, else silent no-op):
1. ``MULTI_ACCOUNT_ENABLED=true`` (env, default false)
2. the parent resolved LIVE and was accepted (guaranteed by the call site)
3. ``mode_key`` is a known in-repo strategy (``KNOWN_STRATEGIES``)
4. at least one ENABLED child selected that strategy

Sizing (plan §6):
- ``factor = child.capital_inr / PRIMARY_BOOK_CAPITAL`` (env, default 10,00,000)
- opening orders: equity ``floor(parent_qty × factor)``; derivatives floored to
  lot multiples (SymToken.lotsize); 0 after rounding → journaled skip
- **exit asymmetry guard**: an order that REDUCES the child's own broker
  position flattens what the child actually holds (``get_open_position`` with
  the child's token), never a blind scale — a partially-rejected entry still
  exits cleanly.

Isolation: children run on a small daemon thread pool, fire-and-forget — the
parent's return latency is untouched and NOTHING here ever raises into the
parent path. Every attempt is journaled to ``account_orders``; every
non-``placed`` outcome Telegram-warns the operator.

Eventlet note: ThreadPoolExecutor maps to green threads under the production
monkey-patch (same pattern as historify's ``_job_executor``); broker calls go
through the shared httpx client whose sockets are patched.
"""

import copy
import math
import os
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module

from database import account_orders_db, broker_accounts_db
from database.auth_db import get_auth_token
from services.broker_accounts_service import KNOWN_STRATEGIES, is_multi_account_enabled
from utils.logging import get_logger

logger = get_logger(__name__)

# Exchanges whose quantities must round to lot multiples.
DERIVATIVE_EXCHANGES = {"NFO", "BFO", "CDS", "BCD", "MCX", "NCDEX", "NCO"}

_executor: ThreadPoolExecutor | None = None


def _primary_book_capital() -> float:
    """UI-configurable, DB-backed (issue #484); env is only the first-read seed."""
    from services.broker_accounts_service import primary_book_capital

    return primary_book_capital()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="acct_fanout")
    return _executor


def _notify_operator(message: str) -> None:
    """Telegram WARNING — never raises."""
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("multi_account_mirror", message)
    except Exception:
        logger.exception("multi-account mirror: operator notify failed")


def _lookup_lotsize(symbol: str, exchange: str) -> int | None:
    """SymToken lotsize for derivative rounding; None when unknown."""
    try:
        from database.symbol import SymToken, db_session

        try:
            row = (
                db_session.query(SymToken.lotsize)
                .filter(SymToken.symbol == symbol, SymToken.exchange == exchange)
                .first()
            )
            return int(row[0]) if row and row[0] else None
        finally:
            db_session.remove()
    except Exception:
        logger.exception(f"lotsize lookup failed for {symbol}:{exchange}")
        return None


def _child_open_qty(broker_module, symbol: str, exchange: str, product: str, token: str) -> int:
    """Child's own net position (signed). 0 on any failure — fail toward the
    opening-scale path, never toward a phantom flatten."""
    try:
        net = broker_module.get_open_position(symbol, exchange, product, token)
        return int(float(net))
    except Exception:
        logger.exception(f"child position lookup failed for {symbol}:{exchange}")
        return 0


def compute_child_qty(
    parent_qty: int,
    factor: float,
    exchange: str,
    lotsize: int | None,
    action: str,
    child_net_qty: int,
) -> int:
    """Mirror quantity for one child. Pure — unit-tested directly.

    Exit guard: when the order REDUCES the child's existing position (SELL vs a
    long, BUY vs a short), return the child's actual held size. Otherwise scale
    the parent quantity by the capital factor, floored (to lot multiples on
    derivative exchanges).
    """
    action = (action or "").upper()
    if action == "SELL" and child_net_qty > 0:
        return child_net_qty
    if action == "BUY" and child_net_qty < 0:
        return abs(child_net_qty)

    scaled = parent_qty * factor
    if exchange in DERIVATIVE_EXCHANGES:
        if not lotsize or lotsize <= 0:
            return 0  # cannot round safely — caller journals the skip
        return int(math.floor(scaled / lotsize)) * lotsize
    return int(math.floor(scaled))


def _mirror_to_account(
    account: dict,
    order_data: dict,
    mode_key: str,
    broker: str,
    parent_orderid: str,
) -> None:
    """Place one child's mirror order. Runs on the pool; never raises."""
    symbol = order_data.get("symbol", "")
    exchange = order_data.get("exchange", "")
    action = order_data.get("action", "")
    product = order_data.get("product", "")
    parent_qty = int(order_data.get("quantity", 0))
    account_id = account["id"]
    name = account["display_name"]
    # Per-strategy capital override (issue #486): stored on the selection row,
    # so it exists only while the strategy is selected; None → base capital.
    sizing_capital = account.get("capital_override_inr") or float(account["capital_inr"])
    factor = float(sizing_capital) / _primary_book_capital()

    journal = {
        "account_id": account_id,
        "strategy_name": mode_key,
        "symbol": symbol,
        "exchange": exchange,
        "action": action,
        "product": product,
        "parent_qty": parent_qty,
        "factor": round(factor, 4),
        "parent_orderid": parent_orderid,
    }

    try:
        token = get_auth_token(broker_accounts_db.auth_name(account_id))
        if not token:
            account_orders_db.record_mirror_attempt(
                **journal, child_qty=0, status="skipped_no_session"
            )
            _notify_operator(
                f"⚠ Mirror skipped — {name}: no broker session. "
                f"{mode_key} {action} {symbol} not mirrored. Log in at /accounts."
            )
            return

        broker_module = import_module(f"broker.{broker}.api.order_api")

        lotsize = _lookup_lotsize(symbol, exchange) if exchange in DERIVATIVE_EXCHANGES else None
        child_net = _child_open_qty(broker_module, symbol, exchange, product, token)

        # Rejected-entry exit guard (issue #478): the child is FLAT but the
        # journal shows its opposite-side entry was recently attempted and did
        # NOT place — this parent order is an exit of a position the child
        # never got. Scaling it would open a fresh naked position; skip it.
        # A flat child with NO opposite-attempt history is a genuine opening
        # order (e.g. a short entry) and scales normally below.
        if child_net == 0:
            prior = account_orders_db.last_opposite_attempt_status(
                account_id, symbol, exchange, mode_key, action
            )
            if prior is not None and prior != "placed":
                account_orders_db.record_mirror_attempt(
                    **journal, child_qty=0, status="skipped_no_position"
                )
                _notify_operator(
                    f"⚠ Mirror skipped — {name}: {action} {symbol} has nothing to exit "
                    f"(entry attempt was '{prior}'). No position opened."
                )
                return

        child_qty = compute_child_qty(parent_qty, factor, exchange, lotsize, action, child_net)

        if child_qty <= 0:
            account_orders_db.record_mirror_attempt(
                **journal, child_qty=0, status="skipped_zero_qty"
            )
            _notify_operator(
                f"⚠ Mirror skipped — {name}: {mode_key} {action} {symbol} "
                f"scales to 0 (factor {factor:.2f}). Increase capital or deselect."
            )
            return

        child_order = copy.deepcopy(order_data)
        child_order["quantity"] = child_qty

        res, response_data, order_id = broker_module.place_order_api(child_order, token)
        if getattr(res, "status", None) == 200:
            account_orders_db.record_mirror_attempt(
                **journal, child_qty=child_qty, status="placed", broker_orderid=str(order_id)
            )
            logger.info(
                f"mirror placed — {name}: {action} {child_qty} {symbol} "
                f"(parent {parent_qty}, factor {factor:.2f}, orderid {order_id})"
            )
        else:
            message = (
                response_data.get("message", "broker rejected")
                if isinstance(response_data, dict)
                else "broker rejected"
            )
            account_orders_db.record_mirror_attempt(
                **journal, child_qty=child_qty, status="rejected", error_text=message
            )
            _notify_operator(
                f"⚠ Mirror REJECTED — {name}: {action} {child_qty} {symbol}: {message}"
            )
    except Exception as e:
        logger.exception(f"mirror attempt failed for account {account_id} ({symbol} {action})")
        account_orders_db.record_mirror_attempt(
            **journal, child_qty=0, status="error", error_text=str(e)
        )
        _notify_operator(f"⚠ Mirror ERROR — {name}: {action} {symbol}: {e}")


def maybe_fan_out(
    order_data: dict,
    mode_key: str | None,
    broker: str,
    parent_orderid: str,
) -> int:
    """Fan a live accepted parent order out to eligible children.

    Fire-and-forget: returns the number of children scheduled (0 on any
    gate miss) and NEVER raises. Called only from the LIVE-accepted branch of
    ``place_order_with_auth``.
    """
    try:
        if not is_multi_account_enabled():
            return 0
        if not mode_key or mode_key not in KNOWN_STRATEGIES:
            return 0
        accounts = broker_accounts_db.accounts_for_strategy(mode_key)
        if not accounts:
            return 0

        pool = _get_executor()
        for account in accounts:
            pool.submit(_mirror_to_account, account, order_data, mode_key, broker, parent_orderid)
        logger.info(
            f"fan-out scheduled: {len(accounts)} child account(s) for "
            f"{mode_key} {order_data.get('action')} {order_data.get('symbol')}"
        )
        return len(accounts)
    except Exception:
        logger.exception("fan-out scheduling failed (parent order unaffected)")
        return 0
