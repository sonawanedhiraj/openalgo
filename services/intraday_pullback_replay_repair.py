"""One-off operator repair for journal rows fabricated by the #624 replay bug.

Before issue #624 a mid-session restart replayed the whole day through the state machine and
re-decided it: the first replayed candle stopped out a position opened hours later (an ``SL``
stamped 09:15 and priced at the *stop*), and the replay then re-reached the original breakout
and opened a duplicate position priced at that stale candle's close.

The orders those decisions sent were REAL — the money (virtual, in sandbox) genuinely moved.
So the rows are not deleted and not zeroed: they are reconciled against the book's own fills,
which is the only source that says what actually happened. What the repair changes is
**which prices and times the row claims**, never the P&L formula (the same
``intraday_pullback_service._charges`` is used, so a repaired row and a live one stay
comparable).

Detection is the signature the bug leaves and nothing else can: ``exit_time < entry_time``.
A row whose legs cannot BOTH be read from the order book is skipped loudly and left exactly as
it is — a half-reconciled row would be a third number nobody can trace.

``note`` is stamped so the incident stays visible in the record rather than being quietly
tidied away. ``session`` is deliberately NOT recompiled: it records which selection window the
SIGNAL came from, which the replay did not change.

Dry-run by default. NOT wired into the runtime — an operator runs it once:

    uv run python -m services.intraday_pullback_replay_repair --date 2026-08-17
    uv run python -m services.intraday_pullback_replay_repair --date 2026-08-17 --apply
"""

from __future__ import annotations

import argparse
from datetime import datetime

from utils.logging import get_logger

logger = get_logger(__name__)

STRATEGY_NAME = "intraday_pullback_top2"
_NOTE = "repaired #624 replay (fills)"


def _as_float(value) -> float | None:
    """Coerce a price field to float; None when absent or unusable.

    A zero ``average_price`` means "not filled / not reported", never "filled at zero" —
    using it would publish a P&L equal to the whole notional as reconciled truth.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def fetch_fill(order_id: str, api_key: str) -> dict | None:
    """``{price, ts}`` for one order leg, or None if unreadable.

    Routing is automatic: ``get_order_status`` resolves the analyze overlay and then
    ``sandbox_order_exists``, so a sandbox order id is answered from ``sandbox.db`` and a
    live one from the broker orderbook without this caller knowing which.
    """
    if not order_id:
        return None
    try:
        from services.orderstatus_service import get_order_status

        ok, resp, _code = get_order_status(
            {"strategy": STRATEGY_NAME, "orderid": str(order_id)}, api_key=api_key
        )
        if not ok:
            logger.warning(
                "repair: order status unavailable for %s — %s",
                order_id,
                (resp or {}).get("message"),
            )
            return None
        data = (resp or {}).get("data") or {}
        return {
            "price": _as_float(data.get("average_price") or data.get("price")),
            "ts": _parse_ts(data.get("timestamp")),
        }
    except Exception:
        logger.exception("repair: order status raised for %s", order_id)
        return None


def recompute(side: str, qty: int, entry_px: float, exit_px: float) -> tuple[float, float, float]:
    """``(gross, charges, net)`` from the two FILL prices, via the service's own formula."""
    from services.intraday_pullback_service import _charges

    long_side = side == "L"
    gross = ((exit_px - entry_px) if long_side else (entry_px - exit_px)) * qty
    buy_value = (entry_px if long_side else exit_px) * qty
    sell_value = (exit_px if long_side else entry_px) * qty
    ch = _charges(buy_value, sell_value)
    return round(gross, 2), round(ch, 2), round(gross - ch, 2)


def _corrupt_query(session, trade_date: str | None):
    """The #624 signature: an exit stamped before its own entry. Nothing else produces it."""
    from database.intraday_pullback_db import IntradayPullbackTrade

    q = session.query(IntradayPullbackTrade).filter(
        IntradayPullbackTrade.entry_time.isnot(None),
        IntradayPullbackTrade.exit_time.isnot(None),
        IntradayPullbackTrade.exit_time < IntradayPullbackTrade.entry_time,
    )
    if trade_date:
        q = q.filter(IntradayPullbackTrade.trade_date == trade_date)
    return q.order_by(IntradayPullbackTrade.id)


def find_corrupt_rows(trade_date: str | None = None) -> list[dict]:
    """Read-only listing of the corrupted rows, as plain dicts.

    Dicts, not ORM instances: this closes its own session, and a detached instance handed
    back to a caller either raises on attribute access or — worse — accepts mutations that
    are silently never written.
    """
    from database.intraday_pullback_db import db_session as session

    try:
        return [
            {
                "id": r.id,
                "trade_date": r.trade_date,
                "symbol": r.symbol,
                "side": r.side,
                "quantity": r.quantity,
                "entry_time": str(r.entry_time),
                "entry_price": r.entry_price,
                "exit_time": str(r.exit_time),
                "exit_price": r.exit_price,
                "net_pnl": r.net_pnl,
            }
            for r in _corrupt_query(session, trade_date).all()
        ]
    finally:
        session.remove()


def repair(trade_date: str | None = None, apply: bool = False) -> dict:
    """Reconcile every #624-corrupted row against its fills. Returns a summary dict."""
    from database.auth_db import get_first_available_api_key
    from database.intraday_pullback_db import db_session as session

    api_key = get_first_available_api_key()
    if not api_key:
        logger.error("repair: no API key available — cannot read the order book")
        return {"status": "error", "message": "no api key", "rows": []}

    report: list[dict] = []
    repaired = skipped = 0
    try:
        # Query INSIDE this session scope: rows fetched by find_corrupt_rows() would be
        # detached by its own session.remove(), and mutating a detached instance commits
        # nothing at all — silently.
        for r in _corrupt_query(session, trade_date).all():
            entry = fetch_fill(r.entry_order_id, api_key)
            exit_ = fetch_fill(r.exit_order_id, api_key)
            before = {
                "id": r.id,
                "trade_date": r.trade_date,
                "symbol": r.symbol,
                "side": r.side,
                "entry_time": str(r.entry_time),
                "entry_price": r.entry_price,
                "exit_time": str(r.exit_time),
                "exit_price": r.exit_price,
                "net_pnl": r.net_pnl,
            }
            if not (entry and entry["price"] and exit_ and exit_["price"]):
                # Never half-repair: a row with one reconciled leg is a third number.
                logger.warning(
                    "repair: row %s SKIPPED — fills unreadable (entry=%s exit=%s)",
                    r.id,
                    bool(entry and entry["price"]),
                    bool(exit_ and exit_["price"]),
                )
                report.append({**before, "action": "skipped_unreadable_fills"})
                skipped += 1
                continue

            gross, ch, net = recompute(r.side, int(r.quantity or 0), entry["price"], exit_["price"])
            after = {
                "entry_time": str(entry["ts"] or r.entry_time),
                "entry_price": entry["price"],
                "exit_time": str(exit_["ts"] or r.exit_time),
                "exit_price": exit_["price"],
                "gross_pnl": gross,
                "charges_inr": ch,
                "net_pnl": net,
            }
            report.append({**before, "action": "repair", "after": after})
            repaired += 1
            if apply:
                if entry["ts"]:
                    r.entry_time = entry["ts"]
                if exit_["ts"]:
                    r.exit_time = exit_["ts"]
                r.entry_price = entry["price"]
                r.exit_price = exit_["price"]
                r.gross_pnl = gross
                r.charges_inr = ch
                r.net_pnl = net
                r.note = _NOTE
        if apply:
            session.commit()
    except Exception:
        logger.exception("repair failed")
        session.rollback()
        raise
    finally:
        session.remove()

    return {
        "status": "success",
        "applied": apply,
        "n_repaired": repaired,
        "n_skipped": skipped,
        "rows": report,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="restrict to one trade_date (YYYY-MM-DD)")
    ap.add_argument("--apply", action="store_true", help="write the repair (default: dry run)")
    args = ap.parse_args()

    out = repair(trade_date=args.date, apply=args.apply)
    if out["status"] != "success":
        print(f"ERROR: {out.get('message')}")
        return
    mode = "APPLIED" if out["applied"] else "DRY RUN (nothing written)"
    print(f"\n#624 replay repair — {mode}")
    print(
        f"corrupted rows found: {len(out['rows'])} "
        f"(repairable {out['n_repaired']}, skipped {out['n_skipped']})\n"
    )
    for r in out["rows"]:
        print(f"  id={r['id']} {r['trade_date']} {r['symbol']} {r['side']} -> {r['action']}")
        print(
            f"    before: entry {r['entry_time']} @ {r['entry_price']} | "
            f"exit {r['exit_time']} @ {r['exit_price']} | net {r['net_pnl']}"
        )
        a = r.get("after")
        if a:
            print(
                f"    after : entry {a['entry_time']} @ {a['entry_price']} | "
                f"exit {a['exit_time']} @ {a['exit_price']} | net {a['net_pnl']}"
            )
    if not out["applied"] and out["n_repaired"]:
        print("\nRe-run with --apply to write.")


if __name__ == "__main__":
    main()
