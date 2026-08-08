"""One-off operator backfill: close futures_follow_cap50's stranded lots at the
T+1 exits the strategy WOULD have taken, had #507 not suppressed them.

Not wired into the runtime. Dry-run by default; ``--apply`` writes.

What it does
------------
For every ``futures_follow_trades`` BUY row with no matching exit, it derives the
T+1 exit date (next trading day) and journals the SELL row the strategy would
have written — using the strategy's OWN ``compute_futures_charges`` so the
backfilled rows are directly comparable with the real ones. It then squares off
the sandbox book (``sandbox_positions`` + an audit ``sandbox_orders`` /
``sandbox_trades`` pair).

Two rules, both load-bearing:

1. **It cannot invent a price.** Every exit date must be supplied via
   ``--prices``. A missing date aborts the whole run — no interpolation, no
   nearest-neighbour, no carrying the previous close forward. A performance
   number built on a guessed price is worse than no number, because it looks
   like a measurement.
2. **Prices are real, not modelled.** Exit marks come from the broker's own 1m
   history (``history_service.get_history``), taking the OPEN of the 15:25 bar —
   the first trade of the minute the MARKET order fires in. Calibrated against
   the four real 15:20 entry fills, that field tracks an actual fill to within
   ~4 index points (~₹260/lot).

Labelling (operator decision, 2026-08-08): backfilled rows are written
IDENTICALLY to real strategy exits — ``note='t+1_exit'``, strategy tag
``futures_follow_cap50``, same order-id shape. They are therefore NOT
distinguishable in the journal; the reconstruction is recorded in
``strategies/futures_follow_cap50/VERSION_LOG.md`` instead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[0]
MAIN_DB = "db/openalgo.db"
SANDBOX_DB = "db/sandbox.db"
STRATEGY = "futures_follow_cap50"
SYMBOL = "NIFTY25AUG26FUT"
USER = "dheeraj.sonawane"


def compute_futures_charges(buy_notional: float, sell_notional: float) -> float:
    """Imported from services.futures_follow_service at runtime — see main()."""
    raise NotImplementedError


def _load_charge_model(repo: Path):
    """Import the strategy's own charge function. Never reimplement it: the
    backfilled rows have to be comparable with the real ones."""
    sys.path.insert(0, str(repo))
    from services.futures_follow_service import compute_futures_charges as fn

    return fn


def next_trading_day(d: dt.date, holidays: set[dt.date]) -> dt.date:
    nxt = d + dt.timedelta(days=1)
    while nxt.weekday() >= 5 or nxt in holidays:
        nxt += dt.timedelta(days=1)
    return nxt


def load_holidays(main_db: Path) -> set[dt.date]:
    con = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
    try:
        rows = con.execute("select holiday_date from market_holidays").fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()
    out = set()
    for (raw,) in rows:
        try:
            out.add(dt.date.fromisoformat(str(raw)[:10]))
        except ValueError:
            continue
    return out


def open_lots(main_db: Path) -> list[dict]:
    """BUY rows not yet matched by an exit, FIFO against total SELL qty."""
    con = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
    buys = con.execute(
        "select id, entry_date, lots, quantity, entry_price, signal_id, vol_ratio, margin_inr, "
        "exchange, product, nifty_symbol from futures_follow_trades "
        "where side='BUY' order by id"
    ).fetchall()
    sold = con.execute(
        "select coalesce(sum(quantity),0) from futures_follow_trades where side='SELL'"
    ).fetchone()[0]
    con.close()

    remaining = sold
    out = []
    for r in buys:
        qty = r[3]
        if remaining >= qty:
            remaining -= qty
            continue
        if remaining > 0:  # partially covered — not expected here, but be exact
            qty -= remaining
            remaining = 0
        out.append(
            {
                "buy_id": r[0],
                "entry_date": dt.date.fromisoformat(r[1]),
                "lots": r[2],
                "quantity": qty,
                "journal_entry_price": r[4],
                "signal_id": r[5],
                "vol_ratio": r[6],
                "margin_inr": r[7],
                "exchange": r[8],
                "product": r[9],
                "symbol": r[10],
            }
        )
    return out


def sandbox_fill_prices(sandbox_db: Path) -> dict[str, list[float]]:
    """Actual sandbox BUY fills per date — the true position entry prices
    (the journal's entry_price is the pre-fill quote and differs slightly)."""
    con = sqlite3.connect(f"file:{sandbox_db}?mode=ro", uri=True)
    rows = con.execute(
        "select order_timestamp, average_price from sandbox_orders "
        "where symbol=? and action='BUY' and order_status='complete' order by order_timestamp",
        (SYMBOL,),
    ).fetchall()
    con.close()
    by_date: dict[str, list[float]] = {}
    for ts, px in rows:
        by_date.setdefault(str(ts)[:10], []).append(float(px))
    return by_date


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="C:/workspace/ai-trade-agent/openalgo")
    ap.add_argument(
        "--prices",
        default="",
        help="Comma-separated YYYY-MM-DD=PRICE, the 15:25 IST NIFTY futures mark per exit date.",
    )
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    repo = Path(args.repo)
    main_db, sandbox_db = repo / MAIN_DB, repo / SANDBOX_DB
    charges_fn = _load_charge_model(repo)

    price_map: dict[dt.date, float] = {}
    for tok in filter(None, (t.strip() for t in args.prices.split(","))):
        d, _, p = tok.partition("=")
        price_map[dt.date.fromisoformat(d)] = float(p)

    holidays = load_holidays(main_db)
    lots = open_lots(main_db)
    fills = sandbox_fill_prices(sandbox_db)

    if not lots:
        print("No open lots. Nothing to do.")
        return 0

    print(f"\n{len(lots)} open lot-row(s), {sum(x['quantity'] for x in lots)} qty\n")
    print(
        f"{'buy_id':>6} {'entry':11} {'T+1 exit':11} {'qty':>4} {'entry_px':>9} {'exit_px':>9} "
        f"{'gross':>11} {'chg':>7} {'net':>11}"
    )

    planned, missing = [], []
    for lot in lots:
        exit_date = next_trading_day(lot["entry_date"], holidays)
        # Entry price = the actual sandbox fill for that date, not the journal quote.
        day_fills = fills.get(lot["entry_date"].isoformat(), [])
        entry_px = day_fills[0] if day_fills else lot["journal_entry_price"]
        exit_px = price_map.get(exit_date)
        if exit_px is None:
            missing.append(exit_date)
            print(
                f"{lot['buy_id']:>6} {lot['entry_date']!s:11} {exit_date!s:11} "
                f"{lot['quantity']:>4} {entry_px:>9.2f} {'MISSING':>9} "
                f"{'—':>11} {'—':>7} {'—':>11}"
            )
            continue
        qty = lot["quantity"]
        gross = (exit_px - entry_px) * qty
        chg = charges_fn(entry_px * qty, exit_px * qty)
        net = gross - chg
        planned.append(
            {
                **lot,
                "exit_date": exit_date,
                "entry_px": entry_px,
                "exit_px": exit_px,
                "gross": gross,
                "charges": chg,
                "net": net,
            }
        )
        print(
            f"{lot['buy_id']:>6} {lot['entry_date']!s:11} {exit_date!s:11} {qty:>4} "
            f"{entry_px:>9.2f} {exit_px:>9.2f} {gross:>11.2f} {chg:>7.2f} {net:>11.2f}"
        )

    if missing:
        print(
            f"\nABORT — no 15:25 price supplied for: {', '.join(sorted({str(d) for d in missing}))}"
        )
        print("Supply every exit date via --prices. This tool will not interpolate,")
        print("carry a close forward, or substitute a nearby timestamp: a fabricated")
        print("price produces a performance number that looks measured but isn't.")
        return 2

    # Same-symbol positions are MERGED by the overnight rehydrate, so the strategy
    # writes ONE SELL per (exit_date, symbol) — see the real qty=130 rows 5 and 16.
    # This is not cosmetic: brokerage is a flat 40/round-trip, so 7 separate exits
    # would over-charge the book by ~142 vs the 4 the strategy would really place.
    merged: dict[tuple, dict] = {}
    for p in planned:
        key = (p["exit_date"], p["symbol"])
        m = merged.get(key)
        if m is None:
            merged[key] = {**p, "buy_ids": [p["buy_id"]]}
            continue
        tot = m["quantity"] + p["quantity"]
        m["entry_px"] = (m["entry_px"] * m["quantity"] + p["entry_px"] * p["quantity"]) / tot
        m["quantity"] = tot
        m["lots"] += p["lots"]
        m["margin_inr"] = (m["margin_inr"] or 0) + (p["margin_inr"] or 0)
        m["buy_ids"].append(p["buy_id"])

    planned = []
    for m in merged.values():
        m["gross"] = (m["exit_px"] - m["entry_px"]) * m["quantity"]
        m["charges"] = charges_fn(m["entry_px"] * m["quantity"], m["exit_px"] * m["quantity"])
        m["net"] = m["gross"] - m["charges"]
        planned.append(m)

    print(f"\nMerged into {len(planned)} exit(s), as the rehydrate would have:")
    print(
        f"{'buy_ids':>12} {'entry':11} {'T+1 exit':11} {'qty':>4} {'entry_px':>9} {'exit_px':>9} "
        f"{'gross':>11} {'chg':>7} {'net':>11}"
    )
    for p in sorted(planned, key=lambda x: x["exit_date"]):
        ids = ",".join(str(i) for i in p["buy_ids"])
        print(
            f"{ids:>12} {p['entry_date']!s:11} {p['exit_date']!s:11} {p['quantity']:>4} "
            f"{p['entry_px']:>9.2f} {p['exit_px']:>9.2f} {p['gross']:>11.2f} "
            f"{p['charges']:>7.2f} {p['net']:>11.2f}"
        )

    tg = sum(p["gross"] for p in planned)
    tc = sum(p["charges"] for p in planned)
    tn = sum(p["net"] for p in planned)
    print("-" * 96)
    print(
        f"{'BACKFILL TOTAL':36} {sum(p['quantity'] for p in planned):>4} "
        f"{'':>9} {'':>9} {tg:>11.2f} {tc:>7.2f} {tn:>11.2f}"
    )

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return 0

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    for db in (main_db, sandbox_db):
        shutil.copy2(db, db.with_suffix(db.suffix + f".bak.{stamp}"))
        print(f"backed up {db.name} -> {db.name}.bak.{stamp}")

    # Operator decision (2026-08-08): backfilled exits are labelled IDENTICALLY
    # to real strategy fills — same note, same strategy tag, same order-id shape.
    # The reconstruction is recorded in strategies/futures_follow_cap50/VERSION_LOG.md
    # instead, since the rows themselves carry no marker.
    tag = STRATEGY
    mcon = sqlite3.connect(main_db)
    scon = sqlite3.connect(sandbox_db)
    try:
        for p in planned:
            # Same 14-digit shape as sandbox-generated ids (YYMMDD + 8 digits),
            # derived from the source row so re-runs cannot collide.
            oid = f"{p['exit_date']:%y%m%d}{min(p['buy_ids']):08d}"
            mcon.execute(
                "insert into futures_follow_trades (strategy_id, mode, side, nifty_symbol, "
                "exchange, product, lots, quantity, entry_price, exit_price, entry_date, "
                "signal_id, vol_ratio, margin_inr, gross_pnl, charges_inr, net_pnl, order_id, "
                "status, note, created_at) values (2,'sandbox','SELL',?,?,?,?,?,?,?,?,NULL,?,?,"
                "?,?,?,?,'placed','t+1_exit',?)",
                (
                    p["symbol"],
                    p["exchange"],
                    p["product"],
                    p["lots"],
                    p["quantity"],
                    p["entry_px"],
                    p["exit_px"],
                    p["entry_date"].isoformat(),
                    p["vol_ratio"],
                    p["margin_inr"],
                    p["gross"],
                    p["charges"],
                    p["net"],
                    oid,
                    # created_at is naive UTC in this journal (09:55Z == 15:25 IST),
                    # matching every real exit row.
                    f"{p['exit_date']} 09:55:00.000000",
                ),
            )
            ts = f"{p['exit_date']} 15:25:00.000000"
            scon.execute(
                "insert into sandbox_orders (orderid, user_id, strategy, symbol, exchange, "
                "action, quantity, price, price_type, product, order_status, average_price, "
                "filled_quantity, pending_quantity, order_timestamp, update_timestamp) "
                "values (?,?,?,?,?,'SELL',?,?, 'MARKET', ?, 'complete', ?, ?, 0, ?, ?)",
                (
                    oid,
                    USER,
                    tag,
                    p["symbol"],
                    p["exchange"],
                    p["quantity"],
                    p["exit_px"],
                    p["product"],
                    p["exit_px"],
                    p["quantity"],
                    ts,
                    ts,
                ),
            )
            scon.execute(
                "insert into sandbox_trades (tradeid, orderid, user_id, symbol, exchange, "
                "action, quantity, price, product, strategy, trade_timestamp) "
                "values (?,?,?,?,?,'SELL',?,?,?,?,?)",
                (
                    f"TRADE-BF-{oid}",
                    oid,
                    USER,
                    p["symbol"],
                    p["exchange"],
                    p["quantity"],
                    p["exit_px"],
                    p["product"],
                    tag,
                    ts,
                ),
            )
        scon.execute(
            "update sandbox_positions set quantity=0, pnl=?, accumulated_realized_pnl=?, "
            "margin_blocked=0, updated_at=? where symbol=? and user_id=?",
            (tn, tn, dt.datetime.now().isoformat(sep=" "), SYMBOL, USER),
        )
        mcon.commit()
        scon.commit()
        print(f"\nAPPLIED — {len(planned)} exit row(s); sandbox position flat; net {tn:,.2f}")
    except Exception:
        mcon.rollback()
        scon.rollback()
        raise
    finally:
        mcon.close()
        scon.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
