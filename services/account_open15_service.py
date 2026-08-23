"""Child-account open15 verification surface (issue #663).

Backs the "Child trades — open15" card on /accounts: per child account it
reports today's ``open15_vol_breakout`` mirror attempts (the ``account_orders``
journal), whether every traded symbol is flat in the CHILD's own broker book
once the strategy's exit time (default 09:30 IST) has passed, and the child's
broker-sourced day P&L. ``square_off`` is the manual backstop button for a
position still open past the deadline.

Load-bearing rules (from #497/#626/#637 — do not relax):

- **Positions and P&L come from the child's own broker book**, read with the
  child's ``acct:<id>`` token. OpenAlgo does not track child fills, so the
  journal can only say what was ATTEMPTED; the book says what is HELD.
- **Flat and unreadable are different answers.** ``get_open_position`` returns
  0 for both, so this module reads the raw positions payload itself and keeps
  ``None`` ("could not read") distinct from ``0`` ("read fine, flat").
- **Only an affirmative non-zero book justifies a square-off.** ``square_off``
  re-reads the book at call time and REFUSES on a flat or unreadable book —
  the quantity placed is the book's, never the journal's.
- **A placement ACK is not a fill.** The square-off is journaled ``placed``
  like any mirror; the existing ``account_fill_reconcile`` demotes a post-ACK
  rejection. Callers must present success as "placed — verifying".
"""

import os
from datetime import datetime, timedelta
from importlib import import_module

from database import account_orders_db, broker_accounts_db
from database.auth_db import get_auth_token
from utils.logging import get_logger

logger = get_logger(__name__)

STRATEGY_NAME = "open15_vol_breakout"
_EXIT_TIME_DEFAULT = "09:30"


def _parse_hhmm(value) -> int | None:
    """Minutes since midnight, or None for anything malformed."""
    try:
        hh, mm = str(value).strip().split(":")
        hh_i, mm_i = int(hh), int(mm)
        if 0 <= hh_i <= 23 and 0 <= mm_i <= 59:
            return hh_i * 60 + mm_i
    except (ValueError, AttributeError):
        pass
    return None


def effective_exit_time() -> str:
    """The strategy's configured hard-flatten time, HH:MM IST.

    Same resolution order as the strategy itself (issue #451): stored
    ``open15_config.exit_time`` wins, env ``OPEN15_EXIT_TIME`` next, 09:30
    last. A malformed value falls back rather than raising — this is a
    read-only status surface.
    """
    exit_time = None
    try:
        from database.open15_breakout_db import get_config

        cfg = get_config() or {}
        exit_time = cfg.get("exit_time")
    except Exception:
        logger.exception("open15 config read failed — using env/default exit time")
    if not exit_time:
        exit_time = os.getenv("OPEN15_EXIT_TIME", _EXIT_TIME_DEFAULT)
    return exit_time if _parse_hhmm(exit_time) is not None else _EXIT_TIME_DEFAULT


def _now_ist() -> datetime:
    """Naive IST from naive UTC (repo timestamp contract)."""
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _broker_module(broker: str):
    return import_module(f"broker.{broker}.api.order_api")


def _br_symbol(broker: str, symbol: str, exchange: str) -> str:
    """Broker-format symbol for matching against the raw positions payload.

    Fail-open to the OpenAlgo symbol — for NSE equity the two are usually
    identical, and a failed mapping must not take down the status card.
    """
    try:
        mapping = import_module(f"broker.{broker}.mapping.transform_data")
        return mapping.get_br_symbol(symbol, exchange) or symbol
    except Exception:
        logger.exception(f"br-symbol mapping failed for {symbol}:{exchange} ({broker})")
        return symbol


def _read_child_book(broker: str, token: str) -> list | None:
    """The child's raw NET positions, or None when the book is unreadable.

    None is never coerced to an empty list: "could not read" and "read fine,
    no positions" lead to different UI states and different square-off
    verdicts.
    """
    try:
        broker_module = _broker_module(broker)
        payload = broker_module.get_positions(token)
        if not payload or payload.get("status") not in ("success", True):
            return None
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("net"), list):
            return None
        return data["net"]
    except Exception:
        logger.exception(f"child positions read failed (broker={broker})")
        return None


def _book_entry(net: list, br_symbol: str, exchange: str, product: str) -> dict | None:
    for position in net:
        if (
            position.get("tradingsymbol") == br_symbol
            and position.get("exchange") == exchange
            and position.get("product") == product
        ):
            return position
    return None


def _day_pnl(net: list) -> float | None:
    """Broker-reported day P&L across the child's whole book (requirement 3)."""
    try:
        return round(sum(float(p.get("pnl") or 0) for p in net), 2)
    except (TypeError, ValueError):
        logger.warning("child book has unparseable pnl fields — reporting unknown")
        return None


def _todays_open15_trades(account_id: int, date_utc: str) -> list[dict]:
    rows = account_orders_db.list_orders(date_utc=date_utc, account_id=account_id)
    return [r for r in rows if r["strategy_name"] == STRATEGY_NAME]


def _traded_keys(trades: list[dict]) -> list[tuple[str, str, str]]:
    """Distinct (symbol, exchange, product) the child may actually HOLD.

    Only ``placed`` rows can have opened a position; skips and placement
    rejections never reached the market. (A post-ACK rejection demoted by the
    fill reconcile is no longer ``placed`` — correct on both axes.)
    """
    seen: list[tuple[str, str, str]] = []
    for row in trades:
        if row["status"] != "placed":
            continue
        key = (row["symbol"], row["exchange"], row["product"] or "MIS")
        if key not in seen:
            seen.append(key)
    return seen


def _account_status(account: dict, date_utc: str, after_exit_time: bool) -> dict:
    from services.broker_accounts_service import _is_connected

    account_id = account["id"]
    broker = account.get("broker") or "zerodha"
    trades = _todays_open15_trades(account_id, date_utc)
    result = {
        "account_id": account_id,
        "display_name": account["display_name"],
        "broker": broker,
        "connected": _is_connected(account),
        "trades": trades,
        "positions": [],
        "positions_readable": False,
        "open_after_exit": False,
        "day_pnl": None,
    }

    token = get_auth_token(broker_accounts_db.auth_name(account_id))
    if not token:
        return result

    net = _read_child_book(broker, token)
    if net is None:
        return result
    result["positions_readable"] = True
    result["day_pnl"] = _day_pnl(net)

    positions = []
    for symbol, exchange, product in _traded_keys(trades):
        entry = _book_entry(net, _br_symbol(broker, symbol, exchange), exchange, product)
        try:
            open_qty = int(float(entry.get("quantity") or 0)) if entry else 0
        except (TypeError, ValueError):
            open_qty = 0
        pnl = None
        if entry is not None:
            try:
                pnl = round(float(entry.get("pnl")), 2)
            except (TypeError, ValueError):
                pnl = None
        positions.append(
            {
                "symbol": symbol,
                "exchange": exchange,
                "product": product,
                "open_qty": open_qty,
                "pnl": pnl,
            }
        )
    result["positions"] = positions
    result["open_after_exit"] = after_exit_time and any(p["open_qty"] != 0 for p in positions)
    return result


def open15_status() -> dict:
    """Everything the "Child trades — open15" card needs in one call."""
    exit_time = effective_exit_time()
    now_ist = _now_ist()
    exit_min = _parse_hhmm(exit_time)
    after_exit_time = (now_ist.hour * 60 + now_ist.minute) >= (exit_min or 0)
    date_utc = datetime.utcnow().strftime("%Y-%m-%d")

    accounts = []
    for account in broker_accounts_db.list_accounts():
        try:
            accounts.append(_account_status(account, date_utc, after_exit_time))
        except Exception:
            # One broken child must not blank the card for the other two.
            logger.exception(f"open15 status failed for account {account.get('id')}")
            accounts.append(
                {
                    "account_id": account.get("id"),
                    "display_name": account.get("display_name", "?"),
                    "broker": account.get("broker") or "zerodha",
                    "connected": False,
                    "trades": [],
                    "positions": [],
                    "positions_readable": False,
                    "open_after_exit": False,
                    "day_pnl": None,
                }
            )
    return {
        "strategy": STRATEGY_NAME,
        "exit_time": exit_time,
        "after_exit_time": after_exit_time,
        "now_ist": now_ist.strftime("%H:%M:%S"),
        "date": date_utc,
        "accounts": accounts,
    }


def _notify_operator(message: str) -> None:
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("multi_account_mirror", message)
    except Exception:
        logger.exception("open15 square-off: operator notify failed")


def square_off(account_id: int, symbol: str, exchange: str, product: str) -> tuple[bool, dict]:
    """Manually flatten one open child position with a MARKET opposite order.

    Returns ``(ok, payload)``. Refuses — placing NOTHING — unless the child's
    book affirmatively shows a non-zero quantity for this exact (symbol,
    exchange, product) at call time. On refusal the payload's ``reason`` says
    which check failed.
    """
    account = broker_accounts_db.get_account(account_id)
    if not account:
        return False, {"reason": "unknown_account", "message": "Account not found."}
    name = account["display_name"]
    broker = account.get("broker") or "zerodha"

    token = get_auth_token(broker_accounts_db.auth_name(account_id))
    if not token:
        return False, {
            "reason": "no_session",
            "message": f"{name} has no broker session — log in at /accounts first.",
        }

    net = _read_child_book(broker, token)
    if net is None:
        return False, {
            "reason": "book_unreadable",
            "message": f"{name}'s position book could not be read — refusing to place blind.",
        }
    entry = _book_entry(net, _br_symbol(broker, symbol, exchange), exchange, product)
    try:
        open_qty = int(float(entry.get("quantity") or 0)) if entry else 0
    except (TypeError, ValueError):
        open_qty = 0
    if open_qty == 0:
        return False, {
            "reason": "no_position",
            "message": f"{name} holds no open {symbol} {product} position — nothing to square off.",
        }

    action = "SELL" if open_qty > 0 else "BUY"
    child_qty = abs(open_qty)
    order = {
        "symbol": symbol,
        "exchange": exchange,
        "action": action,
        "quantity": child_qty,
        "pricetype": "MARKET",
        "product": product,
        "price": "0",
        "trigger_price": "0",
        "disclosed_quantity": "0",
        "strategy": STRATEGY_NAME,
    }
    journal = {
        "account_id": account_id,
        "strategy_name": STRATEGY_NAME,
        "symbol": symbol,
        "exchange": exchange,
        "action": action,
        "product": product,
        "parent_qty": 0,
        "parent_orderid": "manual_squareoff",
    }
    try:
        broker_module = _broker_module(broker)
        res, response_data, order_id = broker_module.place_order_api(order, token)
    except Exception as e:
        logger.exception(f"manual square-off failed for {name}: {action} {child_qty} {symbol}")
        account_orders_db.record_mirror_attempt(
            **journal, child_qty=child_qty, status="error", error_text=str(e)
        )
        return False, {"reason": "error", "message": f"Square-off failed: {e}"}

    if getattr(res, "status", None) == 200:
        account_orders_db.record_mirror_attempt(
            **journal, child_qty=child_qty, status="placed", broker_orderid=str(order_id)
        )
        _notify_operator(
            f"Manual square-off placed — {name}: {action} {child_qty} {symbol} "
            f"(open15 position open past exit time). ACK only — fill pending verification."
        )
        logger.info(f"manual square-off placed — {name}: {action} {child_qty} {symbol}")
        return True, {
            "broker_orderid": str(order_id),
            "action": action,
            "quantity": child_qty,
            "message": (
                f"{action} {child_qty} {symbol} placed for {name} — broker ACK, verifying fill."
            ),
        }

    message = (
        response_data.get("message", "broker rejected")
        if isinstance(response_data, dict)
        else "broker rejected"
    )
    account_orders_db.record_mirror_attempt(
        **journal, child_qty=child_qty, status="rejected", error_text=message
    )
    _notify_operator(
        f"⚠ Manual square-off REJECTED — {name}: {action} {child_qty} {symbol}: {message}"
    )
    return False, {"reason": "rejected", "message": f"Broker rejected the square-off: {message}"}
