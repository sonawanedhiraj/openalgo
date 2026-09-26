"""Grade pre-#726 open15 triggers from the #528 tick capture (issue #748).

One-off operator CLI, NOT wired into the runtime. The service has frozen an
A/B/C grade on every trigger since 2026-09-15 (#726); every earlier row is
ungraded, which leaves the grade scorecard with ~10 real fills per grade. The
three inputs the grader needs are all recoverable after the fact:

- trigger time — ``trigger_minute`` + ``trigger_second`` on the row;
- volume ratio — ``cum_vol_at_trigger / baseline_vol``, the SAME expression
  ``Open15BreakoutService._rate_action`` evaluates live;
- universe median — each symbol's 09:15 OPEN against its last captured tick
  at or before the trigger, through the SAME :func:`universe_median_pct`. The
  open is the broker 1m 09:15 bar's open from ``historify.duckdb`` — the same
  broker candle the live ``first_candle`` reads. The capture's first tick is
  only a fallback per symbol: it lags the exchange open (median 0.155 %,
  p90 0.72 % on 172 checked opens), which is too loose against a -0.30 %
  threshold. How many opens came from each source is reported per day.

and they go through the SAME :func:`grade_trigger`, so a backfilled grade and a
live grade cannot be two different rules. Rows are stamped
``rating_source='backfill'``: they are the sample R63 was FITTED on and the
scorecard labels them as such.

Only rows with ``rating IS NULL`` are ever touched. A day whose capture holds
fewer than :data:`MIN_UNIVERSE` symbols (the tick log only carried the watch
list before 2026-08-04) is skipped: a median over a handful of gappers is not
the tape. Dry-run by default::

    uv run python -m services.open15_rating_backfill            # dry run, all days
    uv run python -m services.open15_rating_backfill --date 2026-08-18 --apply
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import os
from collections import defaultdict

from utils.logging import get_logger

logger = get_logger(__name__)

#: below this many captured symbols the "universe median" is a watch-list
#: median — skip rather than grade on it (a normal day captures ~207)
MIN_UNIVERSE = 50
DEFAULT_TICK_DIR = os.path.join("tick_logs", "open15")
OPEN_FROM = "09:15:00"
#: fill classes with no trigger of their own (watched never triggered;
#: replay rows are synthetic reconstructions)
SKIP_FILLS = ("watched", "replay")


def tick_file(tick_dir: str, trade_date: str) -> str | None:
    """The day's capture file (largest if a restart split it)."""
    fs = glob.glob(os.path.join(tick_dir, f"ticks-{trade_date.replace('-', '')}-*.jsonl"))
    return max(fs, key=os.path.getsize) if fs else None


def load_day(path: str) -> tuple[dict[str, float], dict[str, tuple[list[str], list[float]]]]:
    """``(opens, series)``: 09:15 open per symbol, and per-symbol time-sorted
    ``(times "HH:MM:SS.ffffff", ltps)`` for bisecting "last tick at or before".
    """
    per: dict[str, list[tuple[str, float]]] = defaultdict(list)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                t = json.loads(line)
                ts = str(t["ts"])[11:]
                ltp = float(t["ltp"])
            except (ValueError, KeyError, TypeError):
                continue  # a malformed capture line is skipped, not fatal
            if ltp > 0 and ts >= OPEN_FROM:
                per[t["symbol"]].append((ts, ltp))
    opens: dict[str, float] = {}
    series: dict[str, tuple[list[str], list[float]]] = {}
    for sym, ticks in per.items():
        ticks.sort()
        opens[sym] = ticks[0][1]
        series[sym] = ([x[0] for x in ticks], [x[1] for x in ticks])
    return opens, series


def historify_opens(trade_date: str, path: str | None = None) -> dict[str, float]:
    """Broker 09:15 1m-bar open per NSE symbol from historify (read-only).

    Fail-open to ``{}`` (then every symbol falls back to its first tick).
    """
    try:
        import datetime as dt

        import duckdb
        import pytz

        path = (
            path or os.getenv("HISTORIFY_DATABASE_PATH") or os.path.join("db", "historify.duckdb")
        )
        t0 = pytz.timezone("Asia/Kolkata").localize(
            dt.datetime.strptime(trade_date + " 09:15", "%Y-%m-%d %H:%M")
        )
        ts = int(t0.timestamp())
        con = duckdb.connect(path, read_only=True)
        try:
            rows = con.execute(
                "SELECT symbol, open FROM market_data WHERE interval = '1m' "
                "AND exchange = 'NSE' AND timestamp = ?",
                [ts],
            ).fetchall()
        finally:
            con.close()
        return {s: float(o) for s, o in rows if o and o > 0}
    except Exception:
        logger.exception("open15 rating backfill: historify opens unavailable for %s", trade_date)
        return {}


def merge_opens(
    tick_opens: dict[str, float], broker_opens: dict[str, float]
) -> tuple[dict[str, float], int]:
    """Broker open wherever historify has one, else the first captured tick.
    Returns ``(opens, n_broker)``; only captured symbols are kept (they are
    the ones with an LTP to compare against)."""
    out, n = {}, 0
    for sym, o in tick_opens.items():
        b = broker_opens.get(sym)
        if b:
            out[sym], n = b, n + 1
        else:
            out[sym] = o
    return out, n


def median_at(
    opens: dict[str, float], series: dict[str, tuple[list[str], list[float]]], at: str
) -> tuple[float | None, int]:
    """Universe median (%) from the 09:15 open to the last tick at/before
    ``at`` ("HH:MM:SS") — the tape exactly as the live grader saw it."""
    from services.open15_rating import universe_median_pct

    bound = at + ".999999"
    ltps = {}
    for sym, (times, px) in series.items():
        i = bisect.bisect_right(times, bound)
        if i:
            ltps[sym] = px[i - 1]
    return universe_median_pct(opens, ltps)


def grade_row(row, opens, series) -> dict | None:
    """The four rating columns for one row, or None when an input is missing."""
    from services.open15_rating import RATING_SOURCE_BACKFILL, grade_trigger, sec_of_day

    tm, tsec = row.trigger_minute, row.trigger_second
    sec = sec_of_day(tm, tsec)
    if sec is None or not row.baseline_vol or row.cum_vol_at_trigger is None:
        return None
    at = f"{tm}:{int(tsec or 0):02d}"
    med, n = median_at(opens, series, at)
    if med is None or n < MIN_UNIVERSE:
        return None
    ratio = float(row.cum_vol_at_trigger) / max(float(row.baseline_vol), 1.0)
    g = grade_trigger(sec, med, ratio)
    return {
        "rating": g["grade"],
        "rating_univ_median_pct": med,
        "rating_vol_ratio": g["inputs"]["vol_ratio"],
        "rating_source": RATING_SOURCE_BACKFILL,
        "_univ_n": n,
    }


def run(
    date: str | None = None,
    apply: bool = False,
    tick_dir: str = DEFAULT_TICK_DIR,
    opens_provider=historify_opens,
) -> dict:
    from database.open15_breakout_db import Open15Trade, db_session, init_db, update_trade

    init_db()  # a branch-added column is absent from the live DB until this runs
    try:
        q = db_session.query(Open15Trade).filter(
            Open15Trade.rating.is_(None),
            Open15Trade.trigger_minute.isnot(None),
            Open15Trade.trigger_price.isnot(None),
            (Open15Trade.fill.is_(None)) | (Open15Trade.fill.notin_(SKIP_FILLS)),
        )
        if date:
            q = q.filter(Open15Trade.trade_date == date)
        rows = q.order_by(Open15Trade.trade_date, Open15Trade.id).all()
        db_session.expunge_all()
    finally:
        db_session.remove()

    by_day: dict[str, list] = defaultdict(list)
    for r in rows:
        by_day[r.trade_date].append(r)
    report = {
        "graded": 0,
        "written": 0,
        "skipped": defaultdict(int),
        "by_grade": defaultdict(int),
        "open_source": {},
    }
    for day in sorted(by_day):
        path = tick_file(tick_dir, day)
        if not path:
            report["skipped"]["no_capture"] += len(by_day[day])
            print(f"{day}: no tick capture — {len(by_day[day])} row(s) left ungraded")
            continue
        tick_opens, series = load_day(path)
        opens, n_broker = merge_opens(tick_opens, opens_provider(day) or {})
        report["open_source"][day] = {"broker": n_broker, "first_tick": len(opens) - n_broker}
        if len(opens) < MIN_UNIVERSE:
            report["skipped"]["thin_universe"] += len(by_day[day])
            print(
                f"{day}: capture holds {len(opens)} symbols (< {MIN_UNIVERSE}) — "
                f"{len(by_day[day])} row(s) left ungraded"
            )
            continue
        for r in by_day[day]:
            kw = grade_row(r, opens, series)
            if kw is None:
                report["skipped"]["missing_input"] += 1
                continue
            report["graded"] += 1
            report["by_grade"][kw["rating"]] += 1
            n = kw.pop("_univ_n")
            print(
                f"{day} {r.symbol:<12} {r.side} {r.fill or 'real':<6} trig {r.trigger_minute}:"
                f"{int(r.trigger_second or 0):02d} tape {kw['rating_univ_median_pct']:+.3f}% "
                f"(n={n}) vol {kw['rating_vol_ratio']}x -> {kw['rating']}"
            )
            if apply and update_trade(r.id, **kw):
                report["written"] += 1
    report["skipped"] = dict(report["skipped"])
    report["by_grade"] = dict(report["by_grade"])
    return report


def _cli() -> int:
    ap = argparse.ArgumentParser(
        description="Grade ungraded open15 triggers from the tick capture (issue #748). "
        "Dry-run by default."
    )
    ap.add_argument("--date", help="YYYY-MM-DD; default = every ungraded row")
    ap.add_argument("--apply", action="store_true", help="write the journal (default: dry run)")
    ap.add_argument("--tick-dir", default=DEFAULT_TICK_DIR)
    a = ap.parse_args()
    rep = run(a.date, a.apply, a.tick_dir)
    print(json.dumps(rep, indent=2))
    if not a.apply:
        print("dry run — nothing written (re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
