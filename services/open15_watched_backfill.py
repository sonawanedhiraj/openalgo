"""One-off / catch-up backfill for the open15 watched-break counterfactual (#728).

    uv run python -m services.open15_watched_backfill [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--apply]

Dry-run by default; ``--apply`` writes. NOT wired into the runtime — the live
path is the summary job + the next 09:10 arm's catch-up.

What it does, per stored day in the range (default: the current calendar
month, because Kite drops an option contract from the master contract at
expiry and a contract that no longer exists cannot be priced):

1. Reads the day's decision log and lists every ``no_entry`` symbol.
2. For a symbol that broke its level but whose ``no_entry`` event predates the
   tick-thread break capture (no ``first_break_at``), **rebuilds the break
   from the tick capture** (``tick_logs/open15/ticks-YYYYMMDD-*.jsonl``, the
   whole-universe capture of issue #528): the 09:15 candle from the 09:15-minute
   ticks, then the first tick at/after 09:16:00 and inside the entry window
   that is beyond the level. Labelled ``break_source='ticks'``.
3. With ``--apply``: journals + prices those rows through the SAME
   ``enrich_watched`` the live path uses (so repair and live behaviour cannot
   drift), then runs ``enrich_watched_pending`` for anything the broker could
   not serve bars for on the first pass. Idempotent: a re-run is a no-op.

Limits, stated rather than smoothed over: the tick-built 09:15 candle can
differ by a tick from the broker snapshot the live gate used on days armed
with ``first_candle_source='quotes'``; a symbol with no ticks in the capture is
reported as ``no_ticks`` and skipped; a contract already expired is
``no_contract``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sys

from utils.logging import get_logger

logger = get_logger(__name__)

TICK_DIR = os.path.join("tick_logs", "open15")
_FIRST_MIN = 9 * 60 + 15
_ENTRY_FROM = _FIRST_MIN + 1


def _hhmm_to_min(hhmm: str | None, default: int) -> int:
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
        return h * 60 + m
    except (ValueError, AttributeError, TypeError):
        return default


def load_ticks(trade_date: str, symbols: set[str], tick_dir: str = TICK_DIR) -> dict[str, list]:
    """``{symbol: [(ts, ltp), ...]}`` sorted by time, for the symbols asked for.

    Reads every capture file of the date (one per process that ran that day)
    and merges them. Malformed lines are skipped.
    """
    ymd = trade_date.replace("-", "")
    out: dict[str, list] = {s: [] for s in symbols}
    for path in sorted(glob.glob(os.path.join(tick_dir, f"ticks-{ymd}-*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    sym = rec.get("symbol")
                    if sym not in out:
                        continue
                    try:
                        ts = dt.datetime.fromisoformat(rec["ts"])
                        px = float(rec["ltp"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    out[sym].append((ts, px))
        except OSError:
            logger.exception("open15 watched backfill: cannot read %s", path)
    for sym in out:
        out[sym].sort(key=lambda t: t[0])
    return out


def first_break_from_ticks(
    ticks: list[tuple[dt.datetime, float]], side: str, entry_to_min: int
) -> dict | None:
    """The first tick beyond the tick-built 09:15 candle level, or ``None``.

    Mirrors ``Open15Core.on_tick``: the 09:15 minute only builds the candle;
    the gate starts at 09:16:00 and stops updating past the entry cutoff.
    Returns ``{"break_at", "break_price", "level", "candle_ticks"}``.
    """
    hi = lo = None
    n_first = 0
    for ts, px in ticks:
        minute = ts.hour * 60 + ts.minute
        if minute == _FIRST_MIN:
            hi = px if hi is None else max(hi, px)
            lo = px if lo is None else min(lo, px)
            n_first += 1
    if hi is None or lo is None:
        return None
    level = hi if side == "L" else lo
    for ts, px in ticks:
        minute = ts.hour * 60 + ts.minute
        if minute < _ENTRY_FROM or minute > entry_to_min:
            continue
        beyond = px > level if side == "L" else px < level
        if beyond:
            return {
                "break_at": ts.strftime("%H:%M:%S"),
                "break_price": px,
                "level": level,
                "candle_ticks": n_first,
            }
    return None


def reconstruct_breaks(trade_date: str, events: list[dict], tick_dir: str = TICK_DIR) -> dict:
    """``{symbol: {break_at, break_price, level, source}}`` for the level-broken
    ``no_entry`` symbols of the day that lack ``first_break_at``.

    Symbols the log already carries a break for are returned as-is (source
    ``log``) so the caller has one map to hand to ``enrich_watched``.
    """
    from services.open15_option_shadow import watched_candidates

    armed = next((e for e in events if e.get("event") == "armed"), {})
    entry_to = _hhmm_to_min(armed.get("no_entry_after"), 9 * 60 + 29)
    cands = [c for c in watched_candidates(events) if c["level_broken"]]
    need = {c["symbol"]: c["side"] for c in cands if not c.get("break_at")}
    breaks: dict[str, dict] = {}
    for c in cands:
        if c.get("break_at"):
            breaks[c["symbol"]] = {
                "break_at": c["break_at"],
                "break_price": c.get("break_price"),
                "source": "log",
            }
    if not need:
        return breaks
    ticks = load_ticks(trade_date, set(need), tick_dir)
    for sym, side in need.items():
        if not ticks.get(sym):
            breaks[sym] = {"break_at": None, "break_price": None, "source": "no_ticks"}
            continue
        fb = first_break_from_ticks(ticks[sym], side, entry_to)
        if fb is None:
            # the log said broken but the capture shows no beyond-tick inside
            # the window — say so rather than invent a moment
            breaks[sym] = {"break_at": None, "break_price": None, "source": "no_break_in_ticks"}
            continue
        breaks[sym] = {**fb, "source": "ticks"}
    return breaks


def _dates(d_from: str, d_to: str) -> list[str]:
    a = dt.date.fromisoformat(d_from)
    b = dt.date.fromisoformat(d_to)
    out = []
    while a <= b:
        out.append(a.isoformat())
        a += dt.timedelta(days=1)
    return out


def run(d_from: str, d_to: str, apply: bool, tick_dir: str = TICK_DIR) -> dict:
    """Backfill the range. Returns a per-date report; writes only with ``apply``."""
    from database.open15_breakout_db import get_day_log, init_db
    from services.open15_option_shadow import (
        enrich_watched,
        enrich_watched_pending,
        resolve_atm_option,
    )

    # the app adds post-ship columns at boot (`_ensure_columns`); a CLI run
    # against a DB the branch code has never booted on must do the same, or
    # every ORM read of `break_at` fails and the day reports zero rows
    init_db()
    report: dict[str, dict] = {}
    for date in _dates(d_from, d_to):
        events = get_day_log(date)
        if not events:
            continue
        breaks = reconstruct_breaks(date, events, tick_dir)
        usable = {k: v for k, v in breaks.items() if v.get("break_at")}
        day = {
            "breaks": breaks,
            "usable": len(usable),
            "skipped": {k: v["source"] for k, v in breaks.items() if not v.get("break_at")},
        }
        if not apply:
            # show what WOULD be priced, resolving the contract read-only
            plan = {}
            armed = next((e for e in events if e.get("event") == "armed"), {})
            sides = {
                e["symbol"]: e.get("side")
                for e in events
                if e.get("event") == "no_entry" and e.get("symbol")
            }
            for sym, b in usable.items():
                c = resolve_atm_option(sym, sides.get(sym), float(b["break_price"]), date)
                plan[sym] = {
                    "break_at": b["break_at"],
                    "break_price": b["break_price"],
                    "source": b["source"],
                    "contract": c["symbol"] if c else None,
                    "lot": int(c["lotsize"]) if c else None,
                    "exit_time": armed.get("exit_time") or "09:30",
                }
            day["plan"] = plan
        else:
            day["result"] = enrich_watched(date, events, breaks=usable)
        report[date] = day
    if apply:
        report["_pending_pass"] = enrich_watched_pending()
    return report


def main(argv: list[str] | None = None) -> int:
    today = dt.date.today()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--from", dest="d_from", default=today.replace(day=1).isoformat())
    ap.add_argument("--to", dest="d_to", default=today.isoformat())
    ap.add_argument("--apply", action="store_true", help="write journal rows (default: dry-run)")
    ap.add_argument("--tick-dir", default=TICK_DIR)
    args = ap.parse_args(argv)
    rep = run(args.d_from, args.d_to, args.apply, args.tick_dir)
    print(json.dumps(rep, indent=2, default=str))
    if not args.apply:
        print(
            "\nDRY RUN — nothing written. Re-run with --apply to journal + price.", file=sys.stderr
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
