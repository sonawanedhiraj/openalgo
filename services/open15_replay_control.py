"""Score the open15 replay engine against days that ACTUALLY traded (issue #600).

A reconstruction is only worth reading if we know how far it is from the real
thing. This replays sessions that have a live decision log and a journal, and
compares — selection, gaps, OI verdicts, triggers, P&L sign.

Why it bypasses eligibility, deliberately
-----------------------------------------
A control day is by definition a day that traded, which is exactly what
:func:`open15_replay.check_eligibility` refuses (``day_was_traded``). So this
module calls the replay STAGES directly and **never** calls ``replay_session``
or ``persist``. Every DB touch here is a SELECT — no insert, no delete, no
day-log write — which is the property ``test_control_harness_never_writes``
pins. The guard it skips protects WRITES; there is nothing here to guard.

What to expect (measured 2026-08-13/14 while building the engine)
-----------------------------------------------------------------
* seed selection — **exact**, and treated as a gate
* OI verdicts — **exact**, and treated as a gate
* trigger overlap — **~60%**, reported and tracked. Bars cannot see a level
  poke that closes back inside the minute, so this will never be 100%; a DROP
  is the regression signal, not the absolute number.
* P&L — same sign and order of magnitude, never equal, because the entry price
  is not resolvable from bars.

Usage::

    uv run python -m services.open15_replay_control --from 2026-07-01 --to 2026-08-14

Needs a broker session and refuses to run during market hours (it makes ~250
historical calls per day replayed, against the live strategy's quota).
"""

from __future__ import annotations

import datetime as dt
import json

from services import open15_replay as R
from utils.logging import get_logger

logger = get_logger(__name__)


def _live_trades(date: str) -> list[dict]:
    """The date's journal rows, read-only — symbol + trigger minute is all we score."""
    from database.open15_breakout_db import Open15Trade, db_session

    try:
        rows = (
            db_session.query(Open15Trade.symbol, Open15Trade.trigger_minute, Open15Trade.fill)
            .filter(Open15Trade.trade_date == date)
            .all()
        )
        return [{"symbol": s, "trigger_minute": t, "fill": f} for s, t, f in rows]
    except Exception:
        logger.exception("open15 control: journal read failed for %s", date)
        return []
    finally:
        db_session.remove()


def _live_day(date: str) -> dict | None:
    """Ground truth for ``date`` from its own decision log + journal."""
    from database.open15_breakout_db import get_day_log

    events = get_day_log(date) or []
    sel = next((e for e in events if e.get("event") == "selection"), None)
    if not sel:
        return None  # a day with no selection event cannot be scored
    return {
        "selection": sel.get("selected") or {},
        "gaps_pct": sel.get("gaps_pct") or {},
        "oi_blocks": sorted(
            {
                (e["symbol"], e["side"], e.get("oi_lots"))
                for e in events
                if e.get("event") == "universe_excluded" and e.get("stage") == 3
            }
        ),
        "trades": _live_trades(date),
    }


def score_day(date: str) -> dict | None:
    """Replay ``date`` and compare against what really happened. Writes nothing."""
    live = _live_day(date)
    if live is None:
        return None

    cfg = R.resolve_replay_config(date)
    bars, prev = R.fetch_session_bars(date, R.universe_symbols())
    if not bars:
        return {"date": date, "error": "no_bars"}

    gaps = {
        s: b["open"] / prev[s] - 1.0
        for s, rows in bars.items()
        if s in prev and prev[s] and (b := rows.get(R._FIRST_MINUTE))
    }
    pos = sorted((s for s in gaps if gaps[s] > 0), key=lambda s: -gaps[s])[:20]
    neg = sorted((s for s in gaps if gaps[s] < 0), key=lambda s: gaps[s])[:20]
    contracts = R.resolve_contracts_and_oi(
        date, [(s, "L") for s in pos] + [(s, "S") for s in neg], bars
    )
    run = R.run_core(date, cfg, bars, prev, contracts)
    missing = [(s, d) for s, d in run["selected"].items() if f"{s}|{d}" not in contracts]
    if missing:
        contracts.update(R.resolve_contracts_and_oi(date, missing, bars))
    rows = R.price_legs(cfg, run, contracts)

    # --- selection: seed picks only; rolling additions are the approximate part
    seed = {s: d for s, d in run["selected"].items() if run["watch_source"].get(s) == "seed"}
    sel_match = seed == live["selection"]
    gap_worst = max(
        (abs(run["gaps"].get(s, 0.0) - g) for s, g in live["gaps_pct"].items()), default=0.0
    )

    # --- OI verdicts, restricted to the seed-reachable set on both sides so a
    # rolling-cadence difference cannot be mistaken for a filter regression
    got_oi = sorted(
        {
            (e["symbol"], e["side"], e.get("oi_lots"))
            for e in run["oi_exclusions"]
            if e.get("watch_source") == "seed"
        }
    )
    live_oi = sorted(
        b
        for b in live["oi_blocks"]
        if b[0] not in run["watch_source"] or run["watch_source"].get(b[0]) == "seed"
    )

    # --- triggers
    live_trig = {(t["symbol"], t.get("trigger_minute")) for t in live["trades"]}
    got_trig = {(r["symbol"], r["trigger_minute"]) for r in rows}
    overlap = len(live_trig & got_trig)

    return {
        "date": date,
        "selection_match": sel_match,
        "selection_live": live["selection"],
        "selection_replay": seed,
        "gap_max_delta_pp": round(gap_worst, 4),
        "oi_match": got_oi == live_oi,
        "oi_live": live_oi,
        "oi_replay": got_oi,
        "triggers_live": len(live_trig),
        "triggers_replay": len(got_trig),
        "triggers_overlap": overlap,
        "missed": sorted(s for s, _ in live_trig - got_trig),
        "extra": sorted(s for s, _ in got_trig - live_trig),
    }


def run_control(date_from: str, date_to: str) -> dict:
    from services.data_freshness_service import is_trading_day

    d, end = dt.date.fromisoformat(date_from), dt.date.fromisoformat(date_to)
    days = []
    while d <= end:
        if is_trading_day(d):
            try:
                s = score_day(d.isoformat())
            except Exception:
                logger.exception("open15 control: scoring failed for %s", d)
                s = {"date": d.isoformat(), "error": "exception"}
            if s:
                days.append(s)
                _print_day(s)
        d += dt.timedelta(days=1)

    scored = [x for x in days if "error" not in x]
    tl = sum(x["triggers_live"] for x in scored)
    ov = sum(x["triggers_overlap"] for x in scored)
    return {
        "days_scored": len(scored),
        "selection_exact": sum(1 for x in scored if x["selection_match"]),
        "oi_exact": sum(1 for x in scored if x["oi_match"]),
        "gap_max_delta_pp": max((x["gap_max_delta_pp"] for x in scored), default=0.0),
        "triggers_live": tl,
        "triggers_overlap": ov,
        "trigger_overlap_pct": round(100.0 * ov / tl, 1) if tl else None,
        "days": days,
    }


def _print_day(s: dict) -> None:
    if "error" in s:
        print(f"{s['date']}  ERROR {s['error']}")
        return
    print(
        f"{s['date']}  selection {'OK ' if s['selection_match'] else 'MISMATCH'}"
        f"  oi {'OK ' if s['oi_match'] else 'MISMATCH'}"
        f"  gapΔ {s['gap_max_delta_pp']:.3f}pp"
        f"  triggers {s['triggers_overlap']}/{s['triggers_live']} matched"
        f" (+{len(s['extra'])} extra)"
    )
    if not s["selection_match"]:
        print(f"    live   {s['selection_live']}")
        print(f"    replay {s['selection_replay']}")
    if not s["oi_match"]:
        print(f"    live   {s['oi_live']}")
        print(f"    replay {s['oi_replay']}")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Score replay against days that traded.")
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    now = R._now_ist()
    from services.data_freshness_service import is_trading_day

    if R._MARKET_OPEN <= now.time() <= R._MARKET_CLOSE and is_trading_day(now.date()):
        raise SystemExit("refusing to run during market hours — it competes with the live feed")

    out = run_control(args.date_from, args.date_to)
    if args.json:
        print(json.dumps(out, indent=1, default=str))
        return
    print("\n=== control summary ===")
    print(f"days scored          {out['days_scored']}")
    print(
        f"selection exact      {out['selection_exact']}/{out['days_scored']}   (GATE: must be all)"
    )
    print(f"OI verdicts exact    {out['oi_exact']}/{out['days_scored']}   (GATE: must be all)")
    print(f"worst gap delta      {out['gap_max_delta_pp']:.4f}pp   (GATE: <= 0.01)")
    print(
        f"trigger overlap      {out['triggers_overlap']}/{out['triggers_live']}"
        f" = {out['trigger_overlap_pct']}%   (expect ~60%; a DROP is the regression)"
    )


if __name__ == "__main__":
    main()
