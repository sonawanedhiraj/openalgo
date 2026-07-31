"""One-time operator backfill: reconstruct the T+1 exits futures_follow_cap50
never fired while issue #497 was live (2026-07-27 .. 2026-07-30).

WHY THIS IS NOT ORDINARY RECONCILIATION
---------------------------------------
``engine_eod_reconciliation_backfill`` stamps exits that DID happen in sandbox
but were never journaled — it records a truth the journal missed. This is the
opposite case: the exits never happened at all, because #497 left the engine
reading an empty position book. Writing them is therefore a RECONSTRUCTION of
what the strategy would have done, not a record of what it did.

That distinction is load-bearing, so every row written here is marked
``note='t+1_exit_reconstructed_#497'`` and can never be mistaken for a real
fill. Do not relax that marker.

WHAT IT DOES
------------
For each BUY cohort (grouped by ``entry_date``) with no corresponding SELL row:

1. Resolve the T+1 trading day (weekend/NSE-holiday aware).
2. Skip the cohort entirely if that T+1 day is today or later — those lots are
   legitimately still open and belong to the live exit job, not to this script.
3. Fetch the broker's 1m bar at the configured exit time (15:25 IST) on that
   T+1 day and use its close as the MARKET fill.
4. Compute gross / charges / net with the strategy's own ``compute_futures_charges``.
5. Write one SELL row per cohort, ``created_at`` stamped at the T+1 exit moment.

SCOPE — it touches ONLY ``futures_follow_trades`` in ``db/openalgo.db``. It does
NOT modify ``sandbox.db``: the sandbox book holds a real netted position that
can only be flattened at a live price, so journal and sandbox cash will differ
by the drift the missed exits accumulated. That divergence is inherent and is
reported at the end rather than papered over.

Dry-run by default. ``--apply`` writes. NOT wired into the runtime.

    uv run python -m services.futures_follow_missed_exit_backfill
    uv run python -m services.futures_follow_missed_exit_backfill --apply
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
STRATEGY_NAME = "futures_follow_cap50"
RECONSTRUCTED_NOTE = "t+1_exit_reconstructed_#497"
_MAX_T1_LOOKAHEAD_DAYS = 7


def _next_trading_day(d: date) -> date | None:
    """First trading day strictly after ``d`` (weekend + NSE-holiday aware)."""
    from services.data_freshness_service import is_trading_day

    probe = d
    for _ in range(_MAX_T1_LOOKAHEAD_DAYS):
        probe = probe + timedelta(days=1)
        if is_trading_day(probe):
            return probe
    return None


def _load_open_cohorts() -> dict[str, list[dict]]:
    """BUY rows grouped by entry_date that have no SELL row for that date."""
    from database.futures_follow_db import FuturesFollowTrade, db_session

    rows = db_session.query(FuturesFollowTrade).order_by(FuturesFollowTrade.id).all()
    buys: dict[str, list[dict]] = defaultdict(list)
    exited_dates: set[str] = set()
    for r in rows:
        if r.side == "SELL":
            exited_dates.add(r.entry_date)
        elif r.side == "BUY" and r.status == "placed":
            buys[r.entry_date].append(
                {
                    "id": r.id,
                    "strategy_id": r.strategy_id,
                    "mode": r.mode,
                    "nifty_symbol": r.nifty_symbol,
                    "exchange": r.exchange,
                    "product": r.product,
                    "lots": r.lots or 0,
                    "quantity": r.quantity or 0,
                    "entry_price": r.entry_price,
                    "margin_inr": r.margin_inr,
                }
            )
    return {d: v for d, v in buys.items() if d not in exited_dates}


def _exit_price_from_broker(symbol: str, exchange: str, on: date, exit_time: time) -> float | None:
    """Close of the 1m bar at ``exit_time`` IST on ``on`` (broker historical API)."""
    from database.auth_db import get_auth_token, get_feed_token
    from services.history_service import get_history_with_auth

    user = _resolve_user()
    if not user:
        logger.error("backfill: no auth user resolvable")
        return None
    auth_token = get_auth_token(user)
    if not auth_token:
        logger.error("backfill: no broker auth token for %s", user)
        return None
    try:
        feed_token = get_feed_token(user)
    except Exception:
        feed_token = None

    ok, resp, _status = get_history_with_auth(
        auth_token,
        feed_token,
        "zerodha",
        symbol,
        exchange,
        "1m",
        on.isoformat(),
        on.isoformat(),
    )
    if not ok:
        logger.error("backfill: history fetch failed for %s on %s: %r", symbol, on, resp)
        return None

    target = datetime.combine(on, exit_time, tzinfo=_IST)
    for bar in resp.get("data") or []:
        ts = bar.get("timestamp")
        if ts is None:
            continue
        if datetime.fromtimestamp(int(ts), _IST) == target:
            return float(bar["close"])
    logger.error("backfill: no %s bar for %s on %s", exit_time.strftime("%H:%M"), symbol, on)
    return None


def _resolve_user() -> str | None:
    from database.auth_db import Auth, db_session

    row = (
        db_session.query(Auth)
        .filter(Auth.is_revoked == False, ~Auth.name.startswith("acct:"))  # noqa: E712
        .order_by(Auth.id)
        .first()
    )
    return row.name if row else None


def build_plan(today: date, exit_time: time) -> list[dict]:
    """Reconstruction plan. Cohorts whose T+1 has not passed are excluded."""
    from services.futures_follow_service import compute_futures_charges

    plan: list[dict] = []
    for entry_date, legs in sorted(_load_open_cohorts().items()):
        d = date.fromisoformat(entry_date)
        t1 = _next_trading_day(d)
        if t1 is None:
            continue
        if t1 >= today:
            plan.append(
                {
                    "entry_date": entry_date,
                    "t1": t1,
                    "skip": "not yet due — belongs to the live exit job",
                    "lots": sum(x["lots"] for x in legs),
                }
            )
            continue

        lots = sum(x["lots"] for x in legs)
        qty = sum(x["quantity"] for x in legs)
        prices = [x["entry_price"] for x in legs if x["entry_price"]]
        avg_entry = sum(prices) / len(prices) if prices else None
        symbol = legs[0]["nifty_symbol"]
        exit_price = _exit_price_from_broker(symbol, legs[0]["exchange"], t1, exit_time)
        if avg_entry is None or exit_price is None:
            plan.append({"entry_date": entry_date, "t1": t1, "skip": "no price", "lots": lots})
            continue

        gross = (exit_price - avg_entry) * qty
        charges = compute_futures_charges(avg_entry * qty, exit_price * qty)
        plan.append(
            {
                "entry_date": entry_date,
                "t1": t1,
                "skip": None,
                "legs": legs,
                "symbol": symbol,
                "lots": lots,
                "quantity": qty,
                "entry_price": avg_entry,
                "exit_price": exit_price,
                "gross_pnl": gross,
                "charges_inr": charges,
                "net_pnl": gross - charges,
                "created_at": datetime.combine(t1, exit_time, tzinfo=_IST),
            }
        )
    return plan


def apply_plan(plan: list[dict]) -> int:
    from database.futures_follow_db import record_trade

    written = 0
    for item in plan:
        if item.get("skip"):
            continue
        legs = item["legs"]
        rid = record_trade(
            strategy_id=legs[0]["strategy_id"],
            mode=legs[0]["mode"],
            side="SELL",
            nifty_symbol=item["symbol"],
            exchange=legs[0]["exchange"],
            product=legs[0]["product"],
            lots=item["lots"],
            quantity=item["quantity"],
            entry_date=item["entry_date"],
            entry_price=item["entry_price"],
            exit_price=item["exit_price"],
            margin_inr=sum(x["margin_inr"] or 0 for x in legs),
            gross_pnl=item["gross_pnl"],
            charges_inr=item["charges_inr"],
            net_pnl=item["net_pnl"],
            status="placed",
            note=RECONSTRUCTED_NOTE,
        )
        if rid:
            written += 1
            logger.warning(
                "backfill: reconstructed T+1 exit id=%s entry_date=%s lots=%d "
                "exit=%.2f on %s net=%.0f",
                rid,
                item["entry_date"],
                item["lots"],
                item["exit_price"],
                item["t1"],
                item["net_pnl"],
            )
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write rows (default: dry run)")
    ap.add_argument("--exit-time", default="15:25", help="IST exit time (default 15:25)")
    args = ap.parse_args()

    hh, mm = (int(x) for x in args.exit_time.split(":"))
    exit_time = time(hh, mm)
    today = datetime.now(_IST).date()

    plan = build_plan(today, exit_time)
    if not plan:
        print("nothing to reconstruct — no unexited BUY cohorts")
        return

    print(
        f"\n{'entry_date':12} {'lots':>4} {'T+1 exit':12} {'entry':>10} {'exit':>10} "
        f"{'gross':>10} {'chg':>8} {'net':>10}"
    )
    print("-" * 82)
    tot = 0.0
    for i in plan:
        if i.get("skip"):
            print(f"{i['entry_date']:12} {i['lots']:>4} {str(i['t1']):12} -- SKIPPED: {i['skip']}")
            continue
        tot += i["net_pnl"]
        print(
            f"{i['entry_date']:12} {i['lots']:>4} {str(i['t1']):12} "
            f"{i['entry_price']:>10.2f} {i['exit_price']:>10.2f} "
            f"{i['gross_pnl']:>10,.0f} {i['charges_inr']:>8,.0f} {i['net_pnl']:>10,.0f}"
        )
    print("-" * 82)
    n = sum(1 for i in plan if not i.get("skip"))
    print(f"{n} cohort(s) reconstructible, net {tot:,.0f}\n")

    if not args.apply:
        print("DRY RUN — no rows written. Re-run with --apply to write.")
        return

    written = apply_plan(plan)
    print(
        f"APPLIED — {written} reconstructed exit row(s) written, "
        f"marked note='{RECONSTRUCTED_NOTE}'."
    )
    print(
        "NOTE: sandbox.db is untouched. Its netted position must still be "
        "flattened at a live price; journal and sandbox cash will differ by the "
        "drift the missed exits accumulated."
    )


if __name__ == "__main__":
    main()
