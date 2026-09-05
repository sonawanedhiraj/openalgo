"""Child-account realized P&L — capture, pair, persist (issue #700).

Kite Connect answers "what did this account trade" for TODAY only (`GET
/trades`), and has no historical P&L or ledger endpoint. So a child's realized
P&L exists only if OpenAlgo writes it down the same day. This module does that:

1. **capture fills** — read the child's RAW tradebook with the child's own
   ``acct:<id>`` token, group by ``order_id``, volume-weight partial fills, and
   stamp ``fill_price``/``fill_qty``/``fill_at`` on the matching ``placed``
   mirror rows. A row with no trade in a READABLE tradebook is stamped
   ``fill_qty=0`` ("known unfilled"); an unreadable tradebook stamps nothing.
2. **pair** — FIFO per (symbol, exchange, product) inside one strategy's rows,
   over a short lookback so a T+1 exit meets its entry. A round trip is
   attributed to the IST day of its CLOSING fill.
3. **charge** — per leg, the broker's own figure via ``POST /charges/orders``
   when reachable, else the modelled schedule — and the row says which.
4. **persist** — one ``account_daily_pnl`` row per (account, day, strategy),
   idempotent. No row is written while any of the day's placed rows is still
   of unknown fill state: a partial number is worse than a missing one.

Load-bearing rules (#497 / #626 / #637 / #552 — do not relax):

- Fills come from the child's tradebook (or its Console export), keyed by
  ``order_id``. Never from ``sizing_price``, never the parent's fill scaled.
- The child's token, never the primary's API key.
- ``fill_qty`` is read by presence: ``None`` = unknown, ``0`` = unfilled.
- ``book_realised`` (broker positions, whole account) is a cross-check that is
  never summed into the strategy figure.
- Read-only on the broker. Fail-open per account: one dead session never
  blanks the others. Never raises into a scheduler job.
"""

from __future__ import annotations

import os
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from importlib import import_module

from database import account_orders_db, broker_accounts_db
from database.account_orders_db import ist_date_of_utc, ist_day_utc_window
from database.auth_db import get_auth_token
from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timedelta(hours=5, minutes=30)


def _lookback_days() -> int:
    try:
        return max(0, int(os.getenv("ACCOUNT_PNL_PAIRING_LOOKBACK_DAYS", "7")))
    except ValueError:
        return 7


def today_ist() -> date:
    return (datetime.utcnow() + _IST).date()


def _broker_module(broker: str):
    return import_module(f"broker.{broker}.api.order_api")


def _charges_module(broker: str):
    try:
        return import_module(f"broker.{broker}.api.charges")
    except ImportError:
        return None


def _br_symbol(broker: str, symbol: str, exchange: str) -> str:
    """Broker-format symbol; fail-open to the OpenAlgo symbol."""
    try:
        mapping = import_module(f"broker.{broker}.mapping.transform_data")
        return mapping.get_br_symbol(symbol, exchange) or symbol
    except Exception:
        logger.exception(f"br-symbol mapping failed for {symbol}:{exchange} ({broker})")
        return symbol


# --------------------------------------------------------------------------- #
# Tradebook parsing
# --------------------------------------------------------------------------- #
def parse_broker_timestamp(value) -> datetime | None:
    """Kite timestamps are naive IST (``2026-09-04 09:17:38`` or ISO ``T``).
    Returned as naive UTC (repo contract)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value - _IST if value.tzinfo is None else value.astimezone(None).replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            return datetime.strptime(text, fmt) - _IST
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None) - _IST
    except ValueError:
        return None


def index_tradebook(payload) -> dict[str, list[dict]] | None:
    """``{order_id: [trade, …]}`` from a raw broker tradebook response.

    ``None`` means UNREADABLE (error envelope / unexpected shape) — the caller
    must then stamp nothing. An empty dict means "read fine, no trades today".
    """
    if isinstance(payload, dict):
        if payload.get("status") not in (None, "success", True):
            return None
        rows = payload.get("data")
    else:
        rows = payload
    if rows is None:
        return None
    if not isinstance(rows, list):
        return None
    out: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            continue
        oid = row.get("order_id") or row.get("orderid")
        if oid is None:
            continue
        out[str(oid)].append(row)
    return dict(out)


def aggregate_fill(trades: list[dict]) -> tuple[float, int, datetime | None]:
    """Volume-weighted fill price, total quantity, latest fill time (#641)."""
    qty_total = 0
    value_total = 0.0
    latest: datetime | None = None
    for t in trades:
        try:
            q = int(float(t.get("quantity") or 0))
            p = float(
                t.get("average_price")
                if t.get("average_price") is not None
                else t.get("price") or 0
            )
        except (TypeError, ValueError):
            continue
        if q <= 0:
            continue
        qty_total += q
        value_total += q * p
        ts = parse_broker_timestamp(
            t.get("fill_timestamp") or t.get("exchange_timestamp") or t.get("order_timestamp")
        )
        if ts and (latest is None or ts > latest):
            latest = ts
    if qty_total == 0:
        return 0.0, 0, latest
    return round(value_total / qty_total, 4), qty_total, latest


# --------------------------------------------------------------------------- #
# Charges
# --------------------------------------------------------------------------- #
def _is_option(symbol: str, exchange: str) -> bool:
    s = (symbol or "").upper()
    return exchange in ("NFO", "BFO", "MCX", "CDS") and (s.endswith("CE") or s.endswith("PE"))


def modelled_leg_charges(exchange: str, symbol: str, action: str, value: float) -> float:
    """Modelled Zerodha charges for ONE leg (Rs). Per-leg twin of the
    round-trip models in ``open15_breakout_service`` / ``open15_option_shadow``
    so an entry on day T and its exit on T+1 are each charged on their own day.
    """
    if not value or value <= 0:
        return 0.0
    sell = (action or "").upper() == "SELL"
    buy = not sell
    if _is_option(symbol, exchange):
        brokerage = 20.0
        exch_txn = 0.003503 * value
        stt = 0.000625 * value if sell else 0.0
    elif exchange in ("NFO", "BFO"):  # futures
        brokerage = min(20.0, 0.0003 * value)
        exch_txn = 0.0000173 * value
        stt = 0.0002 * value if sell else 0.0
    else:  # equity intraday
        brokerage = min(20.0, 0.0003 * value)
        exch_txn = 0.0000297 * value
        stt = 0.00025 * value if sell else 0.0
    sebi = 0.000001 * value
    stamp = 0.00003 * value if buy else 0.0
    gst = 0.18 * (brokerage + exch_txn + sebi)
    return round(brokerage + exch_txn + stt + sebi + stamp + gst, 2)


def broker_leg_charges(
    broker: str, token: str | None, legs: list[dict], br_symbols: dict[int, str]
) -> dict[int, float] | None:
    """``{row_id: charges}`` from the broker's calculator, or ``None``."""
    if not token or not legs:
        return None
    mod = _charges_module(broker)
    if mod is None:
        return None
    try:
        requests = [
            mod.build_charge_request(
                order_id=str(leg["id"]),
                exchange=leg["exchange"],
                tradingsymbol=br_symbols.get(leg["id"], leg["symbol"]),
                transaction_type=leg["action"],
                product=leg.get("product") or "MIS",
                quantity=int(leg["fill_qty"]),
                average_price=float(leg["fill_price"]),
            )
            for leg in legs
        ]
        answer = mod.get_order_charges(requests, token)
    except Exception:
        logger.exception("broker charges call raised — falling back to modelled")
        return None
    if answer is None:
        return None
    try:
        return {int(k): float(v) for k, v in answer.items()}
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# FIFO pairing
# --------------------------------------------------------------------------- #
@dataclass
class Leg:
    row_id: int
    strategy: str
    symbol: str
    exchange: str
    product: str
    action: str  # BUY | SELL
    qty: int
    price: float
    at: datetime  # naive UTC
    day: date  # IST date of the fill


@dataclass
class RoundTrip:
    strategy: str
    symbol: str
    qty: int
    open_price: float
    close_price: float
    open_day: date
    close_day: date
    direction: str  # long | short

    @property
    def gross(self) -> float:
        if self.direction == "long":
            return (self.close_price - self.open_price) * self.qty
        return (self.open_price - self.close_price) * self.qty


def legs_from_rows(rows: list[dict]) -> list[Leg]:
    """Filled rows → legs, oldest fill first. Rows with unknown or zero fill
    contribute nothing."""
    legs: list[Leg] = []
    for r in rows:
        qty = r.get("fill_qty")
        if qty is None or int(qty) <= 0 or r.get("fill_price") is None:
            continue
        at = r.get("fill_at") or r.get("created_at")
        if isinstance(at, str):
            try:
                at = datetime.fromisoformat(at)
            except ValueError:
                continue
        if at is None:
            continue
        legs.append(
            Leg(
                row_id=int(r["id"]),
                strategy=r["strategy_name"],
                symbol=r["symbol"],
                exchange=r["exchange"],
                product=r.get("product") or "MIS",
                action=(r["action"] or "").upper(),
                qty=int(qty),
                price=float(r["fill_price"]),
                at=at,
                day=ist_date_of_utc(at),
            )
        )
    legs.sort(key=lambda leg: (leg.at, leg.row_id))
    return legs


def pair_fifo(legs: list[Leg]) -> tuple[list[RoundTrip], list[Leg]]:
    """FIFO-match legs per (strategy, symbol, exchange, product).

    A BUY closes open shorts first, a SELL closes open longs first; any
    remainder opens a new lot. Returns the round trips and the still-open legs
    (unpaired quantity), which the day row reports as ``n_open_legs``.
    """
    books: dict[tuple, dict[str, deque]] = defaultdict(lambda: {"long": deque(), "short": deque()})
    trips: list[RoundTrip] = []
    for leg in legs:
        key = (leg.strategy, leg.symbol, leg.exchange, leg.product)
        book = books[key]
        remaining = leg.qty
        closing_side = "short" if leg.action == "BUY" else "long"
        opening_side = "long" if leg.action == "BUY" else "short"
        queue = book[closing_side]
        while remaining > 0 and queue:
            open_leg, open_qty = queue[0]
            matched = min(remaining, open_qty)
            trips.append(
                RoundTrip(
                    strategy=leg.strategy,
                    symbol=leg.symbol,
                    qty=matched,
                    open_price=open_leg.price,
                    close_price=leg.price,
                    open_day=open_leg.day,
                    close_day=leg.day,
                    direction=closing_side,
                )
            )
            remaining -= matched
            if matched == open_qty:
                queue.popleft()
            else:
                queue[0] = (open_leg, open_qty - matched)
        if remaining > 0:
            book[opening_side].append((leg, remaining))
    open_legs = [
        Leg(**{**vars(open_leg), "qty": qty})
        for book in books.values()
        for side in ("long", "short")
        for open_leg, qty in book[side]
    ]
    return trips, open_legs


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #
def _read_tradebook(broker: str, token: str) -> dict[str, list[dict]] | None:
    try:
        return index_tradebook(_broker_module(broker).get_trade_book(token))
    except Exception:
        logger.exception(f"child tradebook read failed (broker={broker})")
        return None


def _read_book_realised(broker: str, token: str, keys: set[tuple[str, str, str]]) -> float | None:
    """Sum of the broker's own ``realised`` over the given (brsymbol, exchange,
    product) keys — whole-account, manual trades included. ``None`` = unreadable."""
    if not keys:
        return None
    try:
        payload = _broker_module(broker).get_positions(token)
        if not payload or payload.get("status") not in ("success", True):
            return None
        net = (payload.get("data") or {}).get("net")
        if not isinstance(net, list):
            return None
        total = 0.0
        for p in net:
            k = (p.get("tradingsymbol"), p.get("exchange"), p.get("product"))
            if k in keys:
                total += float(p.get("realised") or 0.0)
        return round(total, 2)
    except Exception:
        logger.exception(f"child positions read failed (broker={broker})")
        return None


def stamp_fills_from_tradebook(
    rows_today: list[dict], index: dict[str, list[dict]]
) -> tuple[int, int]:
    """Write fills for today's placed rows from a READABLE tradebook.

    Returns ``(filled, unfilled)``. A placed row absent from the tradebook is
    stamped ``fill_qty=0`` — the tradebook was read fine and it holds no trade
    for that order, so "unfilled" is the answer, not "unknown".
    """
    filled = unfilled = 0
    for row in rows_today:
        oid = row.get("broker_orderid")
        if not oid:
            continue
        trades = index.get(str(oid)) or []
        price, qty, at = aggregate_fill(trades)
        if qty > 0:
            account_orders_db.set_fill(row["id"], fill_price=price, fill_qty=qty, fill_at=at)
            filled += 1
        else:
            account_orders_db.set_fill(
                row["id"], fill_price=0.0, fill_qty=0, fill_at=row.get("fill_at") or None
            )
            unfilled += 1
    return filled, unfilled


def _ensure_leg_charges(
    broker: str, token: str | None, rows: list[dict]
) -> dict[int, tuple[float, str]]:
    """``{row_id: (charges, source)}`` for filled rows, pricing any that lack
    a stored figure (broker first, modelled fallback) and persisting it."""
    out: dict[int, tuple[float, str]] = {}
    pending: list[dict] = []
    for r in rows:
        if r.get("fill_qty") is None or int(r["fill_qty"]) <= 0:
            continue
        if r.get("charges_inr") is not None:
            out[r["id"]] = (float(r["charges_inr"]), r.get("charges_source") or "modelled")
        else:
            pending.append(r)
    if not pending:
        return out
    br_symbols = {r["id"]: _br_symbol(broker, r["symbol"], r["exchange"]) for r in pending}
    from_broker = broker_leg_charges(broker, token, pending, br_symbols) or {}
    for r in pending:
        if r["id"] in from_broker:
            charges, source = from_broker[r["id"]], "broker"
        else:
            value = float(r["fill_price"]) * int(r["fill_qty"])
            charges, source = (
                modelled_leg_charges(r["exchange"], r["symbol"], r["action"], value),
                "modelled",
            )
        fill_at = r.get("fill_at")
        if isinstance(fill_at, str):
            fill_at = datetime.fromisoformat(fill_at)
        account_orders_db.set_fill(
            r["id"],
            fill_price=float(r["fill_price"]),
            fill_qty=int(r["fill_qty"]),
            fill_at=fill_at,
            charges_inr=charges,
            charges_source=source,
        )
        out[r["id"]] = (charges, source)
    return out


def _combined_source(sources: set[str]) -> str | None:
    if not sources:
        return None
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"


def capture_account_day(
    account: dict,
    day: date,
    *,
    finalize: bool = False,
    use_broker: bool = True,
    capture_source: str = "tradebook",
    charges_token: str | None = None,
) -> dict:
    """Capture + persist one child's realized P&L for one IST day.

    ``use_broker=False`` recomputes purely from stored fills (the Console-import
    path and any historical day — the broker cannot answer for those anyway).
    ``charges_token`` lets that path still price legs through the broker's
    charges calculator (it accepts historical orders) when the child happens
    to be logged in; without it the legs are modelled and labelled so.
    Never raises.
    """
    account_id = account["id"]
    name = account.get("display_name") or f"account {account_id}"
    broker = account.get("broker") or "zerodha"
    result = {
        "account_id": account_id,
        "name": name,
        "date": day.isoformat(),
        "status": "no_rows",
        "strategies": {},
    }
    try:
        start_utc, end_utc = ist_day_utc_window(day)
        rows_today = account_orders_db.placed_rows_in_window(account_id, start_utc, end_utc)
        if not rows_today:
            return result

        token = (
            get_auth_token(broker_accounts_db.auth_name(account_id))
            if use_broker
            else charges_token
        )
        is_today = day == today_ist()
        if use_broker and is_today:
            if not token:
                result["status"] = "no_session"
            else:
                index = _read_tradebook(broker, token)
                if index is None:
                    result["status"] = "tradebook_unreadable"
                else:
                    stamp_fills_from_tradebook(rows_today, index)
                    rows_today = account_orders_db.placed_rows_in_window(
                        account_id, start_utc, end_utc
                    )

        unknown = [r for r in rows_today if r.get("fill_qty") is None]
        if unknown:
            if result["status"] == "no_rows":
                result["status"] = "fills_unknown"
            result["unknown_rows"] = len(unknown)
            logger.info(
                "account P&L: %s %s — %s placed row(s) with unknown fill, no day row written (%s)",
                name,
                day,
                len(unknown),
                result["status"],
            )
            return result

        # Pairing lookback so a T+1 exit meets its entry.
        lb_start = start_utc - timedelta(days=_lookback_days())
        rows_lb = account_orders_db.placed_rows_in_window(account_id, lb_start, end_utc)
        charges_by_row = _ensure_leg_charges(broker, token, rows_lb)
        legs = legs_from_rows(rows_lb)
        trips, open_legs = pair_fifo(legs)

        strategies = sorted({r["strategy_name"] for r in rows_today})
        for strategy in strategies:
            s_trips = [t for t in trips if t.strategy == strategy and t.close_day == day]
            s_legs_today = [leg for leg in legs if leg.strategy == strategy and leg.day == day]
            s_open = [leg for leg in open_legs if leg.strategy == strategy]
            gross = round(sum(t.gross for t in s_trips), 2)
            charges = 0.0
            sources: set[str] = set()
            for leg in s_legs_today:
                c, src = charges_by_row.get(leg.row_id, (0.0, None))
                charges += c
                if src:
                    sources.add(src)
            book_realised = None
            if use_broker and is_today and token:
                keys = {
                    (_br_symbol(broker, leg.symbol, leg.exchange), leg.exchange, leg.product)
                    for leg in s_legs_today
                }
                book_realised = _read_book_realised(broker, token, keys)
            row = account_orders_db.upsert_daily_pnl(
                account_id,
                day,
                strategy,
                realized_gross=gross,
                charges_inr=round(charges, 2),
                charges_source=_combined_source(sources),
                n_round_trips=len(s_trips),
                n_fills=len(s_legs_today),
                n_open_legs=len(s_open),
                book_realised=book_realised,
                capture_source=capture_source,
                finalized=finalize,
            )
            result["strategies"][strategy] = row
        result["status"] = "captured"
        logger.info(
            "account P&L captured — %s %s: %s",
            name,
            day,
            {k: (v or {}).get("realized_net") for k, v in result["strategies"].items()},
        )
        return result
    except Exception:
        logger.exception(f"account P&L capture failed for {name} on {day}")
        result["status"] = "error"
        return result


def capture_all(day: date | None = None, *, finalize: bool = False) -> list[dict]:
    """Capture every child account for ``day`` (default today IST). Never raises."""
    day = day or today_ist()
    results = []
    try:
        accounts = broker_accounts_db.list_accounts()
    except Exception:
        logger.exception("account P&L: could not list accounts")
        return results
    for account in accounts:
        results.append(capture_account_day(account, day, finalize=finalize))
    return results


def recapture_today_if_stale(max_age_s: int = 60) -> list[dict] | None:
    """On-demand capture for today, throttled. Used by the page endpoint so a
    restart between the 09:40 and 15:35 passes still gets today captured the
    moment someone looks."""
    global _last_recapture_at
    now = datetime.utcnow()
    if _last_recapture_at and (now - _last_recapture_at).total_seconds() < max_age_s:
        return None
    _last_recapture_at = now
    return capture_all()


_last_recapture_at: datetime | None = None
