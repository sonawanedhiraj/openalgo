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
- **residual-cash resize (issue #690, open15 only)**: when the PARENT's open15
  ``residual_sizing_enabled`` flag is ON and the #637 funds check finds the
  child's cash short of the full ``capital_per_trade_inr`` order, the mirror is
  resized to the child's OWN leftover cash (minus the shared
  ``residual_reserve_pct`` headroom) instead of skipped. No per-child knob;
  placed rows carry a ``residual_sized:`` marker in ``error_text``.
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


def _child_book_qty(broker_module, symbol: str, exchange: str, product: str, token: str):
    """Child's own net position (signed int), or **None when unreadable**.

    Deliberately distinct from ``_child_open_qty`` (which collapses failure to
    0 — right for the mirror path, where 0 routes to the safe opening-scale
    branch). The orphan-flatten sweep (issue #659) needs the #626 distinction:
    an affirmative 0 means "nothing held, send nothing", while an unreadable
    book on a believed-filled mirror still squares off.
    """
    try:
        net = broker_module.get_open_position(symbol, exchange, product, token)
        return int(float(net))
    except Exception:
        logger.exception(f"child book read failed for {symbol}:{exchange}")
        return None


def compute_mirror_net(rows: list[dict]) -> dict:
    """Net PLACED mirror quantity per (account_id, symbol, exchange, product).

    Pure (unit-tested directly). BUY adds, SELL subtracts; a non-zero net means
    the child's mirrors did not round-trip — either the exit mirror never fired
    (parent paper-demotion, issue #659 gap A) or it was rejected (gap B). Each
    value carries the latest contributing ``parent_orderid`` so the sweep's own
    journal row groups with the trade it repairs.
    """
    nets: dict = {}
    for r in rows:
        action = (r.get("action") or "").upper()
        if action not in ("BUY", "SELL"):
            continue
        key = (r["account_id"], r["symbol"], r["exchange"], r.get("product") or "MIS")
        entry = nets.setdefault(key, {"net": 0, "parent_orderid": None})
        entry["net"] += (1 if action == "BUY" else -1) * int(r.get("child_qty") or 0)
        entry["parent_orderid"] = r.get("parent_orderid") or entry["parent_orderid"]
    return nets


def flatten_stranded_child_mirrors(
    mode_key: str,
    symbols: list[str] | None = None,
    reason: str = "",
) -> int:
    """Close child positions whose mirrors did not round-trip (issue #659).

    Covers both stranding shapes: the parent entry was demoted to paper so no
    parent exit ever fired (gap A), and the child's exit mirror was rejected
    with nothing retrying it (gap B). For every (account, symbol) whose net
    PLACED mirror quantity today is non-zero, sends a reducing MARKET order:

    - quantity is ``min(|net|, |book|)`` — never more than WE placed (a child's
      unrelated same-symbol position is not ours to close) and never more than
      it holds;
    - an affirmative 0 book sends nothing (the child's own entry never filled);
    - an **unreadable** book still squares off, capped at ``|net|`` — the #626
      believed-filled asymmetry: an unsent exit strands a real position, while
      a redundant MIS order is caught by the broker's own square-off;
    - a book whose sign disagrees with the net is alerted and left alone;
    - master switch OFF → alert-only, no orders (the operator said stop).

    Idempotent: the sweep's own ``placed`` row enters the next computation, so
    a repaired key nets to 0. Synchronous and never raises; callers are
    scheduler threads, NEVER the ZMQ tick thread. ⚠ Do not call this right
    after a flatten that just scheduled exit mirrors — they are fire-and-forget
    on the pool, and racing them double-exits (open15 sweeps at summary time,
    +5 min, for exactly this reason). Returns the number of orders sent.
    """
    try:
        rows = account_orders_db.todays_placed_rows(mode_key)
        if symbols is not None:
            wanted = set(symbols)
            rows = [r for r in rows if r["symbol"] in wanted]
        stranded = {k: v for k, v in compute_mirror_net(rows).items() if v["net"] != 0}
        if not stranded:
            return 0

        if not is_multi_account_enabled():
            _notify_operator(
                f"⚠ Stranded child mirror position(s) found for {mode_key} "
                f"({', '.join(sorted(k[1] for k in stranded))}) but mirroring is "
                f"DISABLED — not touching child accounts. Close manually."
            )
            return 0

        eligible = {a["id"]: a for a in broker_accounts_db.accounts_for_strategy(mode_key)}
        sent = 0
        for (account_id, symbol, exchange, product), info in stranded.items():
            try:
                sent += _flatten_one_stranded_key(
                    account_id=account_id,
                    symbol=symbol,
                    exchange=exchange,
                    product=product,
                    net=info["net"],
                    parent_orderid=info["parent_orderid"],
                    account=eligible.get(account_id),
                    mode_key=mode_key,
                    reason=reason,
                )
            except Exception:
                logger.exception(
                    f"orphan flatten failed for account {account_id} {symbol} — continuing"
                )
        return sent
    except Exception:
        logger.exception("orphan-flatten sweep failed")
        return 0


def _flatten_one_stranded_key(
    *,
    account_id: int,
    symbol: str,
    exchange: str,
    product: str,
    net: int,
    parent_orderid: str | None,
    account: dict | None,
    mode_key: str,
    reason: str,
) -> int:
    """Flatten one stranded (account, symbol). Returns 1 if an order was sent."""
    if account is None:
        # Disabled or deselected since the mirror placed — still stranded, but
        # an account the operator pulled out of mirroring is not ours to trade.
        _notify_operator(
            f"⚠ Stranded child position — account {account_id} holds a {mode_key} "
            f"mirror of {symbol} (net {net}) but is no longer enabled for the "
            f"strategy. Close manually."
        )
        return 0
    name = account["display_name"]
    marker = f"orphan_flatten: {reason}" if reason else "orphan_flatten"
    journal = {
        "account_id": account_id,
        "strategy_name": mode_key,
        "symbol": symbol,
        "exchange": exchange,
        "action": "SELL" if net > 0 else "BUY",
        "product": product,
        "parent_qty": 0,
        "parent_orderid": parent_orderid,
    }

    token = get_auth_token(broker_accounts_db.auth_name(account_id))
    if not token:
        account_orders_db.record_mirror_attempt(
            **journal, child_qty=0, status="skipped_no_session", error_text=marker
        )
        _notify_operator(
            f"⚠ Stranded child position — {name}: {symbol} (net {net}) cannot be "
            f"closed, no broker session. Log in at /accounts or close manually."
        )
        return 0

    broker_module = import_module(f"broker.{account['broker']}.api.order_api")
    book = _child_book_qty(broker_module, symbol, exchange, product, token)
    if book == 0:
        # Affirmatively flat: the child's own entry never filled, or something
        # already closed it. Nothing to repair, nothing to journal.
        logger.info(f"orphan flatten — {name}: {symbol} book is flat, nothing to do")
        return 0
    if book is not None and (book > 0) != (net > 0):
        _notify_operator(
            f"⚠ Orphan flatten SKIPPED — {name}: {symbol} book ({book}) disagrees "
            f"in direction with the mirror net ({net}). Not trading against an "
            f"inconsistent read; check the child account."
        )
        return 0
    qty = abs(net) if book is None else min(abs(net), abs(book))

    child_order = {
        "symbol": symbol,
        "exchange": exchange,
        "action": journal["action"],
        "product": product,
        "pricetype": "MARKET",
        "price": "0",
        "quantity": qty,
        "strategy": mode_key,
    }
    res, response_data, order_id = broker_module.place_order_api(child_order, token)
    if getattr(res, "status", None) == 200:
        account_orders_db.record_mirror_attempt(
            **journal,
            child_qty=qty,
            status="placed",
            broker_orderid=str(order_id),
            error_text=marker,
        )
        _notify_operator(
            f"🧹 Orphan flatten — {name}: {journal['action']} {qty} {symbol} sent "
            f"({reason or 'stranded mirror'}). The parent-side exit for this child "
            f"never happened — investigate why."
        )
        return 1
    message = (
        response_data.get("message", "broker rejected")
        if isinstance(response_data, dict)
        else "broker rejected"
    )
    account_orders_db.record_mirror_attempt(
        **journal,
        child_qty=qty,
        status="rejected",
        error_text=f"{marker}: {message}",
    )
    _notify_operator(
        f"⚠ Orphan flatten REJECTED — {name}: {journal['action']} {qty} {symbol}: "
        f"{message}. Position remains until the broker square-off; close manually."
    )
    return 1


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


# The one strategy whose parent-side residual-sizing flag extends to children.
_RESIDUAL_STRATEGY = "open15_vol_breakout"


def open15_residual_params() -> tuple[bool, float]:
    """The PARENT's open15 residual-sizing flag + reserve pct (issue #690).

    Enabling residual sizing on the parent enables it for every child mirror —
    there is deliberately NO per-child knob. Resolution goes through the same
    ``resolve_residual_params`` the parent's own arm uses (stored
    ``open15_config`` row, ``None`` → env seed, clamps applied), so the two
    sides can never disagree about what the flag says. Lazy imports because
    ``open15_breakout_service`` imports this module (inside functions) for the
    #659 orphan-flatten sweep.

    Any failure → ``(False, 0.0)`` — fail toward the pre-#690 skip behavior:
    a broken config read must not start resizing child orders.
    """
    try:
        from database.open15_breakout_db import get_config
        from services.open15_breakout_service import resolve_residual_params

        enabled, reserve_pct, _min_lots = resolve_residual_params(get_config())
        return enabled, reserve_pct
    except Exception:
        logger.exception("open15 residual params read failed — child residual sizing off")
        return False, 0.0


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

        action_upper = (action or "").upper()

        if child_net == 0:
            # Duplicate-exit echo guard (issue #659): the child is FLAT and
            # today's placed mirrors show the position already round-tripped on
            # the child side (net 0 with the closing leg last — an earlier exit
            # mirror or an orphan-flatten sweep), OR a sweep row with this same
            # action exists (the partial-fill case, where the sweep capped at
            # the book and the net never reaches 0). Either way this parent
            # exit is an echo with nothing left to reduce; without the guard it
            # falls into the OPENING branch below and sizes a fresh naked
            # position from capital. A same-direction repeat while the net is
            # still open (a second entry) passes through, as does a fresh entry
            # after today's T+1 exit (net != 0, no sweep row).
            today_rows = [
                r
                for r in account_orders_db.todays_placed_rows(
                    mode_key, account_id=account_id, symbol=symbol
                )
                if r["exchange"] == exchange
            ]
            net_today = sum(
                (1 if (r.get("action") or "").upper() == "BUY" else -1)
                * int(r.get("child_qty") or 0)
                for r in today_rows
            )
            closed_round_trip = (
                today_rows
                and net_today == 0
                and (today_rows[-1].get("action") or "").upper() == action_upper
            )
            swept_same_action = any(
                (r.get("action") or "").upper() == action_upper
                and (r.get("error_text") or "").startswith("orphan_flatten")
                for r in today_rows
            )
            if closed_round_trip or swept_same_action:
                account_orders_db.record_mirror_attempt(
                    **journal, child_qty=0, status="skipped_no_position"
                )
                _notify_operator(
                    f"⚠ Mirror skipped — {name}: {action} {symbol} already sent today "
                    f"and the child is flat. Duplicate exit echo — nothing to do."
                )
                return

            # Rejected-entry exit guard (issue #478): the journal shows the
            # opposite-side entry was recently attempted and did NOT place —
            # this parent order is an exit of a position the child never got.
            # Scaling it would open a fresh naked position; skip it. A flat
            # child with NO opposite-attempt history is a genuine opening
            # order (e.g. a short entry) and scales normally below.
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

        reducing = (action_upper == "SELL" and child_net > 0) or (
            action_upper == "BUY" and child_net < 0
        )
        sizing_price = None
        # Set only when the order was resized to the child's leftover cash
        # (issue #690) — journaled so a smaller row is never read as full-size,
        # the #643 comparability-by-labelling rule.
        residual_note = None
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
                    # Residual-cash sizing (issue #690): with the PARENT's open15
                    # flag ON, resize to this child's OWN leftover cash (minus
                    # the same charges-headroom reserve) instead of skipping —
                    # the child-side twin of the parent's #643 behavior. cash is
                    # a real float here: can_afford only fails on a known
                    # balance. No ledger/lock: the fresh per-order read is the
                    # budget, and the rare same-second double-spend is the
                    # broker RMS's to refuse (journaled as rejected, corrected
                    # by the 09:40 reconcile) — the same fail-open stance
                    # can_afford itself documents.
                    residual_qty = 0
                    budget = 0.0
                    enabled, reserve_pct = (
                        open15_residual_params() if mode_key == _RESIDUAL_STRATEGY else (False, 0.0)
                    )
                    if enabled:
                        budget = max(cash, 0.0) * (1.0 - reserve_pct / 100.0)
                        residual_qty = compute_opening_qty(budget, sizing_price, exchange, lotsize)
                    if residual_qty <= 0:
                        unit = "lot" if exchange in DERIVATIVE_EXCHANGES else "share"
                        detail = f"needs Rs{order_value:,.0f}, available Rs{cash:,.0f}"
                        if enabled:
                            detail += f"; residual Rs{budget:,.0f} cannot afford 1 {unit}"
                        account_orders_db.record_mirror_attempt(
                            **journal,
                            child_qty=0,
                            status="skipped_insufficient_funds",
                            error_text=detail,
                            sizing_price=sizing_price,
                        )
                        _notify_operator(
                            f"⚠ Mirror skipped — {name}: {action} {symbol} needs "
                            f"₹{order_value:,.0f} but only ₹{cash:,.0f} is available. "
                            f"No order placed."
                        )
                        return
                    child_qty = residual_qty
                    residual_note = (
                        f"residual_sized: budget Rs{budget:,.0f} "
                        f"of Rs{float(capital_per_trade):,.0f}"
                    )

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
                error_text=residual_note,
            )
            logger.info(
                f"mirror placed — {name}: {action} {child_qty} {symbol} "
                f"(parent {parent_qty}, price {sizing_price}, orderid {order_id}"
                f"{', ' + residual_note if residual_note else ''})"
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
                error_text=f"{residual_note}; {message}" if residual_note else message,
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
