"""One-shot operator repair for duplicated open15 watched-break rows (issue #757).

From 2026-09-17 the live watched-break stamp (#730) ran twice a day. It ran at
the scheduled exit, and again in the +2 min retry flatten. The risk monitor
re-tracked every broken, untriggered name between the two runs, so each name
got a second ``fill='watched'`` row that priced a ~2-minute hold, along with a
second ``watched_counterfactual`` event in the day log. The /logs page renders
the LAST event per symbol, so it showed the wrong number. The digest and
``watched_pnl_by_date`` added both rows together.

For each ``(trade_date, symbol, side)`` this keeps the EARLIEST row (lowest
id: the stamp at the scheduled exit, which is the counterfactual the strategy
defines) and deletes the rest. In the day log it keeps the first
``watched_counterfactual`` event per (symbol, side) and drops the later ones.
The ``summary.watched`` and ``watched_cf`` counts were always per-symbol, so
they are checked against the de-duplicated set and reported, never rewritten.

Dry-run by default. ``--apply`` first copies ``db/openalgo.db`` to a
timestamped backup (SQLite online backup) and then writes. NOT wired into the
runtime.

    uv run python -m services.open15_watched_dup_repair --all
    uv run python -m services.open15_watched_dup_repair --date 2026-10-05 --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import os

from utils.logging import get_logger

logger = get_logger(__name__)

EVENT = "watched_counterfactual"


def find_duplicates(date: str | None) -> list[dict]:
    """Every watched row that is NOT the earliest for its (date, symbol, side)."""
    from database.open15_breakout_db import WATCHED_FILL, Open15Trade, db_session

    try:
        q = db_session.query(Open15Trade).filter(Open15Trade.fill == WATCHED_FILL)
        if date:
            q = q.filter(Open15Trade.trade_date == date)
        seen: dict[tuple, int] = {}
        out: list[dict] = []
        for r in q.order_by(Open15Trade.trade_date, Open15Trade.id).all():
            key = (r.trade_date, r.symbol, r.side)
            if key not in seen:
                seen[key] = r.id
                continue
            out.append(
                {
                    "id": r.id,
                    "kept_id": seen[key],
                    "date": r.trade_date,
                    "symbol": r.symbol,
                    "side": r.side,
                    "cf_source": r.cf_source,
                    "opt_pnl": r.opt_pnl,
                }
            )
        return out
    finally:
        db_session.remove()


def dedupe_events(events: list[dict], pairs: set[tuple]) -> tuple[list[dict], int]:
    """Drop every ``watched_counterfactual`` event after the first, for each
    (symbol, side) in ``pairs``. Pure; returns ``(events, n_dropped)``."""
    first: set[tuple] = set()
    out: list[dict] = []
    dropped = 0
    for ev in events:
        if ev.get("event") == EVENT:
            key = (ev.get("symbol"), ev.get("side"))
            if key in pairs:
                if key in first:
                    dropped += 1
                    continue
                first.add(key)
        out.append(ev)
    return out, dropped


def _check_counts(events: list[dict]) -> str:
    """Compare the per-symbol counts with the de-duplicated event set."""
    priced = {(e.get("symbol"), e.get("side")) for e in events if e.get("event") == EVENT}
    notes = []
    for ev in events:
        if ev.get("event") == "summary" and ev.get("watched") is not None:
            notes.append(f"summary.watched={ev['watched']}")
        if ev.get("event") == "watched_cf":
            notes.append(
                f"watched_cf watched={ev.get('watched')} already={ev.get('already')} "
                f"priced={ev.get('priced')}"
            )
    return f"{len(priced)} priced name(s); " + ", ".join(notes or ["no summary counts"])


def _repair_day_log(date: str, pairs: set[tuple], apply: bool) -> str:
    from database.open15_breakout_db import get_day_log, save_day_log

    events = get_day_log(date)
    if not events:
        return "no day log stored"
    out, dropped = dedupe_events(events, pairs)
    counts = _check_counts(out)
    if not dropped:
        return f"no duplicate events ({counts})"
    if apply and not save_day_log(date, out):
        return "SAVE FAILED"
    return f"{'dropped' if apply else 'would drop'} {dropped} duplicate {EVENT} event(s) ({counts})"


def _backup_db() -> str:
    """Online-backup the main SQLite DB next to itself; returns the path."""
    import sqlite3

    url = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")
    if not url.startswith("sqlite:///"):
        raise RuntimeError(f"not a sqlite DATABASE_URL: {url}")
    src_path = url[len("sqlite:///") :]
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst_path = f"{src_path}.bak.757_{stamp}"
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dst_path


def repair(date: str | None, apply: bool, backup: bool = True) -> dict:
    """Delete the duplicate rows (one date, or all) and de-duplicate day logs."""
    from database.open15_breakout_db import Open15Trade, db_session

    dups = find_duplicates(date)
    result: dict = {"date": date or "all", "apply": apply, "found": dups, "day_logs": {}}
    if not dups:
        result["status"] = "nothing to repair"
        return result
    if apply and backup:
        result["backup"] = _backup_db()
    by_date: dict[str, set[tuple]] = {}
    for d in dups:
        by_date.setdefault(d["date"], set()).add((d["symbol"], d["side"]))
    if apply:
        try:
            n = (
                db_session.query(Open15Trade)
                .filter(Open15Trade.id.in_([d["id"] for d in dups]))
                .delete(synchronize_session=False)
            )
            db_session.commit()
            result["deleted"] = n
        except Exception:
            db_session.rollback()
            logger.exception("open15 watched dup repair: delete FAILED")
            result["status"] = "DELETE FAILED - nothing written"
            return result
        finally:
            db_session.remove()
    for d, pairs in sorted(by_date.items()):
        result["day_logs"][d] = _repair_day_log(d, pairs, apply)
    result["status"] = "repaired" if apply else "dry-run - pass --apply to write"
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="trade date YYYY-MM-DD")
    g.add_argument("--all", action="store_true", help="every date in the journal")
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()
    res = repair(None if args.all else args.date, args.apply)
    print(f"[{res['status']}] {res['date']}")
    if res.get("backup"):
        print(f"  backup: {res['backup']}")
    for r in res["found"]:
        print(
            f"  {r['date']} {r['symbol']:<12} {r['side'] or '-'} id={r['id']:<4} "
            f"(keeps id={r['kept_id']}) {r['cf_source'] or '-':<5} opt_pnl={r['opt_pnl']!s}"
        )
    if "deleted" in res:
        print(f"  deleted {res['deleted']} row(s)")
    for d, msg in res["day_logs"].items():
        print(f"  day log {d}: {msg}")


if __name__ == "__main__":
    main()
