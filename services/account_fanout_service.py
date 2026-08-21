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


def resolve_sizing_price(order_data: dict) -> float | None:
    """Price used to size an OPENING child order (issue #496).

    Parent LIMIT/SL price wins (it is already live-safe converted); otherwise
    fetch LTP via the quotes service with the primary's api_key. None on any
    failure — the caller journals ``skipped_no_quote`` (fail-safe: never guess).
    """
    try:
        price = float(order_data.get("price") or 0)
        if order_data.get("pricetype") in ("LIMIT", "SL") and price > 0:
            return price
    except (TypeError, ValueError):
        pass
    try:
        from database.auth_db import get_first_available_api_key
        from services.quotes_service import get_quotes

        api_key = get_first_available_api_key()
        if not api_key:
            return None
        ok, data, _status = get_quotes(
            order_data.get("symbol", ""), order_data.get("exchange", ""), api_key=api_key
        )
        if ok:
            ltp = float((data.get("data") or {}).get("ltp") or 0)
            return ltp if ltp > 0 else None
    except Exception:
        logger.exception(
            f"sizing quote failed for {order_data.get('symbol')}:{order_data.get('exchange')}"
        )
    return None


def compute_opening_qty(
    capital_per_trade: float,
    price: float,
    exchange: str,
    lotsize: int | None,
) -> int:
    """Capital-based OPENING quantity (issue #496). Pure — unit-tested directly.

    The child is a smaller account trading the same strategy: quantity comes
    from ITS capital and the live price, not from scaling the parent's qty.

    - equity: ``floor(capital / price)``
    - derivatives: ``floor(capital / (price * lotsize)) * lotsize`` —
      affordable means at least 1 lot naturally; unaffordable means 0 (honest
      skip); unknown lotsize means 0 (refuse to guess).
    """
    if capital_per_trade <= 0 or price <= 0:
        return 0
    if exchange in DERIVATIVE_EXCHANGES:
        if not lotsize or lotsize <= 0:
            return 0
        lots = int(math.floor(capital_per_trade / (price * lotsize)))
        return lots * lotsize
    return int(math.floor(capital_per_trade / price))


def _funds_check_enabled() -> bool:
    """Per-order affordability check on child mirrors (issue #637)."""
    return os.getenv("MULTI_ACCOUNT_FUNDS_CHECK", "true").lower() == "true"


def read_child_cash(broker: str, token: str) -> float | None:
    """The CHILD's own spendable cash, or None if it cannot be read (issue #637).

    Read with the child's token, never the parent's. A child is a separate
    broker account with a separate balance, and #626 got this exact axis wrong
    once already by routing a funds read through a resolver that answered for a
    different book.

    ``get_funds_with_auth`` is called WITHOUT ``original_data`` deliberately:
    that argument is what makes it consult the analyze overlay, and a child
    mirror only ever fires on a LIVE parent order against a real broker account.

    None means "we do not know" and is never coerced to 0 — a zero would block
    every mirror on a transient funds-API failure.
    """
    try:
        from services.funds_service import get_funds_with_auth

        ok, resp, _ = get_funds_with_auth(token, broker)
        if not ok:
            logger.warning("mirror: funds read failed for %s — not gating: %s", broker, resp)
            return None
        cash = (resp or {}).get("data", {}).get("availablecash")
        return float(cash) if cash is not None else None
    except Exception:
        logger.exception("mirror: funds read raised — not gating")
        return None


def can_afford(order_value: float, available_cash: float | None) -> bool:
    """Whether the child can pay for this order (issue #637).

    Fails OPEN on an unknown balance: the broker still enforces the real limit,
    and with the ACK-vs-fill reconciliation in place a refusal is now recorded
    honestly rather than as a mirror that never happened.
    """
    if available_cash is None:
        return True
    return order_value <= available_cash


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
    # Capital-per-trade sizing (issue #496): the ONE per-(child, strategy)
    # knob, stored on the selection row. None means the mirror is skipped
    # loudly (default deny) — never guessed from ratios.
    capital_per_trade = account.get("capital_per_trade_inr")

    journal = {
        "account_id": account_id,
        "strategy_name": mode_key,
        "symbol": symbol,
        "exchange": exchange,
        "action": action,
        "product": product,
        "parent_qty": parent_qty,
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

        action_upper = (action or "").upper()
        reducing = (action_upper == "SELL" and child_net > 0) or (
            action_upper == "BUY" and child_net < 0
        )
        sizing_price = None
        if reducing:
            # Exits flatten the child's ACTUAL position — no capital or price
            # needed, and a missing capital setting must never block an exit.
            child_qty = abs(child_net)
        else:
            if capital_per_trade is None or float(capital_per_trade) <= 0:
                account_orders_db.record_mirror_attempt(
                    **journal, child_qty=0, status="skipped_no_capital"
                )
                _notify_operator(
                    f"⚠ Mirror skipped — {name}: no per-trade capital set for "
                    f"{mode_key}. Set '₹ per trade' on /accounts → Strategies."
                )
                return
            sizing_price = resolve_sizing_price(order_data)
            if sizing_price is None:
                account_orders_db.record_mirror_attempt(
                    **journal, child_qty=0, status="skipped_no_quote"
                )
                _notify_operator(
                    f"⚠ Mirror skipped — {name}: no price available to size "
                    f"{action} {symbol} (quote failed). Not mirrored."
                )
                return
            child_qty = compute_opening_qty(
                float(capital_per_trade), sizing_price, exchange, lotsize
            )
            if child_qty <= 0:
                account_orders_db.record_mirror_attempt(
                    **journal, child_qty=0, status="skipped_zero_qty", sizing_price=sizing_price
                )
                unit = "lot" if exchange in DERIVATIVE_EXCHANGES else "share"
                _notify_operator(
                    f"⚠ Mirror skipped — {name}: ₹{float(capital_per_trade):,.0f} per trade "
                    f"cannot afford 1 {unit} of {symbol} at ₹{sizing_price:,.2f}."
                )
                return

            # Can the CHILD pay for this? (issue #637 — the #626 defect, one
            # account over.) Sized value, not the raw per-trade cap:
            # compute_opening_qty floors, so the real cost is <= the cap.
            # Deliberately inside the opening branch — an EXIT must never be
            # gated on cash, or a funds blip strands a live child position.
            if _funds_check_enabled():
                cash = read_child_cash(broker, token)
                order_value = child_qty * sizing_price
                if not can_afford(order_value, cash):
                    account_orders_db.record_mirror_attempt(
                        **journal,
                        child_qty=0,
                        status="skipped_insufficient_funds",
                        error_text=(f"needs Rs{order_value:,.0f}, available Rs{cash:,.0f}"),
                        sizing_price=sizing_price,
                    )
                    _notify_operator(
                        f"⚠ Mirror skipped — {name}: {action} {symbol} needs "
                        f"₹{order_value:,.0f} but only ₹{cash:,.0f} is available. "
                        f"No order placed."
                    )
                    return

        child_order = copy.deepcopy(order_data)
        child_order["quantity"] = child_qty

        res, response_data, order_id = broker_module.place_order_api(child_order, token)
        if getattr(res, "status", None) == 200:
            account_orders_db.record_mirror_attempt(
                **journal,
                child_qty=child_qty,
                status="placed",
                broker_orderid=str(order_id),
                sizing_price=sizing_price,
            )
            logger.info(
                f"mirror placed — {name}: {action} {child_qty} {symbol} "
                f"(parent {parent_qty}, price {sizing_price}, orderid {order_id})"
            )
        else:
            message = (
                response_data.get("message", "broker rejected")
                if isinstance(response_data, dict)
                else "broker rejected"
            )
            account_orders_db.record_mirror_attempt(
                **journal,
                child_qty=child_qty,
                status="rejected",
                error_text=message,
                sizing_price=sizing_price,
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
