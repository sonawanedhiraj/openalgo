"""One-time import of a child's Zerodha Console tradebook export (issue #700).

Kite Connect serves a child's tradebook for TODAY only, so mirror orders placed
before the daily capture shipped (2026-08-26 onward) can no longer be priced
from the API. Console → Reports → Tradebook → Segment F&O → date range →
Download gives the same fills as a file, with ``order_id`` per trade — which is
exactly the join ``account_orders.broker_orderid`` needs.

Operator CLI, dry-run by default::

    uv run python -m services.account_console_import --account <id> --file <csv|xlsx>
    uv run python -m services.account_console_import --account <id> --file <csv|xlsx> --apply

What it does with the file:

- keeps ONLY trades whose ``order_id`` matches a mirror row of THIS account —
  a family member's own trades match nothing and are ignored (reported);
- volume-weights partials into ``fill_price`` / ``fill_qty`` / ``fill_at`` on
  ``placed`` rows; a ``placed`` row dated inside the file's range with no
  trade is stamped ``fill_qty=0`` (known unfilled) so the day can be written;
- a row the #637 reconcile demoted to ``rejected`` but which the file shows as
  TRADED is a conflict: reported loudly, never silently promoted;
- then recomputes each touched IST day through the same
  ``account_pnl_service.capture_account_day`` the daily jobs use, marked
  ``capture_source='console_csv'`` and final. Charges go through the broker's
  calculator if the child is logged in at import time, else modelled+labelled.

Idempotent: re-running the same file reaches the same fills and the same rows.
Read-only on the broker. Never places an order.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from database import account_orders_db, broker_accounts_db
from database.account_orders_db import ist_date_of_utc
from database.auth_db import get_auth_token
from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timedelta(hours=5, minutes=30)

# Console export headers we understand (lower-cased, stripped). Extra columns
# are ignored. Zerodha ships: symbol, isin, trade_date, exchange, segment,
# series, trade_type, auction, quantity, price, trade_id, order_id,
# order_execution_time.
_ORDER_ID = ("order_id", "orderid", "order id")
_QTY = ("quantity", "qty")
_PRICE = ("price", "average_price", "trade_price")
_SIDE = ("trade_type", "transaction_type", "side", "type")
_TS = ("order_execution_time", "fill_timestamp", "exchange_timestamp", "trade_time")
_TRADE_DATE = ("trade_date", "date")
_SYMBOL = ("symbol", "tradingsymbol")


def _norm(header: str) -> str:
    return str(header or "").strip().lower().replace(" ", "_")


def _pick(row: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def read_export(path: Path) -> list[dict]:
    """Rows of the Console export with normalised headers (CSV or XLSX)."""
    if path.suffix.lower() in (".xlsx", ".xls"):
        try:
            import pandas as pd
        except ImportError as e:  # pragma: no cover
            raise SystemExit("XLSX needs pandas/openpyxl — export as CSV instead") from e
        frame = pd.read_excel(path)
        frame.columns = [_norm(c) for c in frame.columns]
        return [
            {k: (None if (isinstance(v, float) and v != v) else v) for k, v in rec.items()}
            for rec in frame.to_dict(orient="records")
        ]
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return [{_norm(k): v for k, v in row.items()} for row in reader]


def _parse_ts(value, trade_date) -> datetime | None:
    """Naive UTC from Console's IST timestamps; falls back to the trade date."""
    from services.account_pnl_service import parse_broker_timestamp

    ts = parse_broker_timestamp(value) if value is not None else None
    if ts is not None:
        return ts
    if trade_date:
        try:
            d = date.fromisoformat(str(trade_date)[:10])
            # 09:20 IST as a neutral intraday stand-in when only the date is known
            return datetime(d.year, d.month, d.day, 9, 20) - _IST
        except ValueError:
            return None
    return None


def normalise_trades(rows: list[dict]) -> tuple[dict[str, list[dict]], list[str]]:
    """``{order_id: [trade, …]}`` in the tradebook shape ``aggregate_fill``
    understands, plus a list of problems (rows that could not be read)."""
    by_order: dict[str, list[dict]] = defaultdict(list)
    problems: list[str] = []
    for i, row in enumerate(rows, start=2):  # header is line 1
        oid = _pick(row, _ORDER_ID)
        qty = _pick(row, _QTY)
        price = _pick(row, _PRICE)
        side = _pick(row, _SIDE)
        if oid is None or qty is None or price is None:
            problems.append(f"line {i}: missing order_id/quantity/price")
            continue
        try:
            qty_i = int(float(qty))
            price_f = float(price)
        except (TypeError, ValueError):
            problems.append(f"line {i}: unreadable quantity/price ({qty!r}, {price!r})")
            continue
        ts = _parse_ts(_pick(row, _TS), _pick(row, _TRADE_DATE))
        by_order[str(oid).strip()].append(
            {
                "order_id": str(oid).strip(),
                "quantity": qty_i,
                "average_price": price_f,
                "transaction_type": str(side or "").upper(),
                "tradingsymbol": _pick(row, _SYMBOL),
                # aggregate_fill re-parses this as IST; hand it back as IST text
                "fill_timestamp": (ts + _IST).strftime("%Y-%m-%d %H:%M:%S") if ts else None,
            }
        )
    return dict(by_order), problems


def plan_import(account_id: int, by_order: dict[str, list[dict]]) -> dict:
    """Decide what the file means for this account's journal. Pure read."""
    from services.account_pnl_service import aggregate_fill

    matched = account_orders_db.rows_by_broker_orderids(account_id, list(by_order))
    fills: list[dict] = []
    conflicts: list[dict] = []
    days: set[date] = set()
    fuzzy = 0
    for oid, trades in by_order.items():
        row = matched.get(oid)
        if row is None:
            continue
        price, qty, at = aggregate_fill(trades)
        if qty <= 0:
            continue
        if row["status"] != "placed":
            conflicts.append({"row": row, "fill_qty": qty, "fill_price": price})
            continue
        fills.append(
            {"row": row, "fill_price": price, "fill_qty": qty, "fill_at": at, "match": "order_id"}
        )
        if at is not None:
            days.add(ist_date_of_utc(at))
        else:
            created = datetime.fromisoformat(row["created_at"])
            days.add(ist_date_of_utc(created))

    # Console's ``order_id`` column is the EXCHANGE order number, not Kite's
    # own order id (verified 2026-09-05: 16-digit ``19000000…`` vs the
    # 15-digit ``260828…`` we journal), so for pre-#700 rows the id join finds
    # nothing. Fall back to a strict attribute match: same IST day, same
    # side, same quantity, same broker symbol, execution within
    # ``_FUZZY_WINDOW_S`` AFTER our placement — each journal row used once,
    # ambiguity (two candidates equally close) refuses rather than guesses.
    still_unmatched = [oid for oid in by_order if oid not in matched]
    if still_unmatched:
        taken = {f["row"]["id"] for f in fills}
        matched_fuzzy = _fuzzy_match(account_id, by_order, still_unmatched, taken, aggregate_fill)
        for oid, (row, price, qty, at) in matched_fuzzy.items():
            matched[oid] = row
            if row["status"] != "placed":
                conflicts.append({"row": row, "fill_qty": qty, "fill_price": price})
                continue
            fills.append(
                {
                    "row": row,
                    "fill_price": price,
                    "fill_qty": qty,
                    "fill_at": at,
                    "match": "attributes",
                }
            )
            fuzzy += 1
            days.add(
                ist_date_of_utc(at)
                if at
                else ist_date_of_utc(datetime.fromisoformat(row["created_at"]))
            )

    unmatched = sorted(set(by_order) - set(matched))

    # Placed rows on the covered days that the file holds no trade for →
    # known unfilled (0), so the day can be captured.
    unfilled: list[dict] = []
    if days:
        lo, hi = min(days), max(days)
        start_utc = account_orders_db.ist_day_utc_window(lo)[0]
        end_utc = account_orders_db.ist_day_utc_window(hi)[1]
        covered = account_orders_db.placed_rows_in_window(account_id, start_utc, end_utc)
        seen = {f["row"]["id"] for f in fills}
        for r in covered:
            if r["id"] in seen:
                continue
            if r.get("broker_orderid") and str(r["broker_orderid"]) in by_order:
                continue
            unfilled.append(r)
    return {
        "matched": len(matched),
        "fuzzy": fuzzy,
        "fills": fills,
        "unfilled": unfilled,
        "conflicts": conflicts,
        "unmatched_order_ids": unmatched,
        "days": sorted(days),
    }


_FUZZY_WINDOW_S = 180


def _fuzzy_match(
    account_id: int,
    by_order: dict[str, list[dict]],
    order_ids: list[str],
    taken: set[int],
    aggregate_fill,
) -> dict[str, tuple[dict, float, int, datetime | None]]:
    """``{console_order_id: (row, price, qty, at)}`` by strict attributes.

    Candidate rows: this account's ``placed`` mirrors on the trade's IST day
    with the same action, the same total quantity, and the same broker
    symbol (Console reports Zerodha's tradingsymbol; the journal stores the
    OpenAlgo symbol, mapped through the broker's own ``get_br_symbol``).
    Among candidates, the nearest placement at or before the execution time
    within the window wins; a tie refuses the match.
    """
    from services.account_pnl_service import _br_symbol

    out: dict[str, tuple[dict, float, int, datetime | None]] = {}
    account = broker_accounts_db.get_account(account_id) or {}
    broker = account.get("broker") or "zerodha"
    # Load once: every placed row across the file's span.
    ats = []
    prepared = []
    for oid in order_ids:
        trades = by_order[oid]
        price, qty, at = aggregate_fill(trades)
        if qty <= 0 or at is None:
            continue
        side = (trades[0].get("transaction_type") or "").upper()
        symbol = (trades[0].get("tradingsymbol") or "").upper()
        prepared.append((oid, price, qty, at, side, symbol))
        ats.append(at)
    if not prepared:
        return out
    lo, hi = ist_date_of_utc(min(ats)), ist_date_of_utc(max(ats))
    start_utc = account_orders_db.ist_day_utc_window(lo)[0]
    end_utc = account_orders_db.ist_day_utc_window(hi)[1]
    rows = [
        r
        for r in account_orders_db.placed_rows_in_window(account_id, start_utc, end_utc)
        if r["id"] not in taken
    ]
    br_cache: dict[tuple[str, str], str] = {}

    def br(r):
        key = (r["symbol"], r["exchange"])
        if key not in br_cache:
            br_cache[key] = (_br_symbol(broker, r["symbol"], r["exchange"]) or "").upper()
        return br_cache[key]

    used: set[int] = set()
    for oid, price, qty, at, side, symbol in sorted(prepared, key=lambda p: p[3]):
        day = ist_date_of_utc(at)
        cands = []
        for r in rows:
            if r["id"] in used:
                continue
            created = datetime.fromisoformat(r["created_at"])
            if ist_date_of_utc(created) != day:
                continue
            if (r["action"] or "").upper() != side or int(r["child_qty"]) != qty:
                continue
            if symbol and br(r) != symbol:
                continue
            delta = (at - created).total_seconds()
            if -30 <= delta <= _FUZZY_WINDOW_S:
                cands.append((abs(delta), r))
        if not cands:
            continue
        cands.sort(key=lambda c: c[0])
        if len(cands) > 1 and cands[0][0] == cands[1][0]:
            continue  # ambiguous — refuse rather than guess
        row = cands[0][1]
        used.add(row["id"])
        out[oid] = (row, price, qty, at)
    return out


def apply_import(account: dict, plan: dict) -> list[dict]:
    """Write the fills and recompute each touched day. Returns per-day results."""
    from services.account_pnl_service import capture_account_day

    for f in plan["fills"]:
        account_orders_db.set_fill(
            f["row"]["id"],
            fill_price=f["fill_price"],
            fill_qty=f["fill_qty"],
            fill_at=f["fill_at"],
        )
    for r in plan["unfilled"]:
        account_orders_db.set_fill(r["id"], fill_price=0.0, fill_qty=0, fill_at=None)
    token = get_auth_token(broker_accounts_db.auth_name(account["id"]))
    results = []
    for day in plan["days"]:
        results.append(
            capture_account_day(
                account,
                day,
                finalize=True,
                use_broker=False,
                capture_source="console_csv",
                charges_token=token,
            )
        )
    return results


def _print_plan(account: dict, plan: dict, problems: list[str]) -> None:
    name = account["display_name"]
    print(f"Account {account['id']} ({name})")
    print(f"  orders in file matching this account's mirrors : {plan['matched']}")
    print(
        f"  placed rows that will receive a fill           : {len(plan['fills'])}"
        f" ({plan.get('fuzzy', 0)} matched by day/side/qty/symbol — Console's order_id is the exchange id)"
    )
    print(f"  placed rows on covered days with NO trade (->0) : {len(plan['unfilled'])}")
    print(f"  order ids in file NOT ours (ignored)           : {len(plan['unmatched_order_ids'])}")
    print(
        f"  IST days to (re)compute                        : {', '.join(d.isoformat() for d in plan['days']) or '—'}"
    )
    if plan["conflicts"]:
        print("  !! CONFLICTS — journal says rejected/other, file shows trades (NOT imported):")
        for c in plan["conflicts"]:
            r = c["row"]
            print(
                f"     row {r['id']} {r['symbol']} {r['action']} status={r['status']} "
                f"file fill {c['fill_qty']} @ {c['fill_price']}"
            )
    if problems:
        print(f"  unreadable lines: {len(problems)} (first: {problems[0]})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--account", type=int, required=True, help="child account id (see /accounts)"
    )
    parser.add_argument("--file", required=True, help="Console tradebook export (.csv or .xlsx)")
    parser.add_argument(
        "--apply", action="store_true", help="write fills + day rows (default: dry-run)"
    )
    args = parser.parse_args(argv)

    account = broker_accounts_db.get_account(args.account)
    if not account:
        print(f"no child account with id {args.account}", file=sys.stderr)
        return 2
    path = Path(args.file)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 2

    rows = read_export(path)
    by_order, problems = normalise_trades(rows)
    plan = plan_import(account["id"], by_order)
    _print_plan(account, plan, problems)
    if not args.apply:
        print("dry-run — re-run with --apply to write.")
        return 0
    results = apply_import(account, plan)
    for res in results:
        nets = {k: (v or {}).get("realized_net") for k, v in (res.get("strategies") or {}).items()}
        print(f"  {res['date']}: {res['status']} {nets}")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator CLI
    sys.exit(main())
