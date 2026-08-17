"""One-shot Phase-0 cleanup for the open15 replay removal (issue #620).

Dry-run by default; ``--apply`` writes. NOT wired into the runtime — this is an
operator tool that runs once and is deleted with the rest of the feature.

Two jobs, in this order:

1. **Back up** every row it is about to touch, to a timestamped JSON file, so
   the whole step is reversible.
2. **Delete the ``fill='replay'`` journal rows** and **restore the two decision
   logs the replay overwrote**.

Why the logs need restoring: ``save_day_log`` REPLACES, so replaying a day
destroys its record. 2026-08-12 lost ``no_ticks_received`` (the zero-tick feed
failure) and 2026-08-17 lost ``skipped_late_boot`` + six ``late_boot_restart``
entries. No DB backup covers those dates.

⚠ The restored content comes from a TRANSCRIPT CAPTURE taken earlier the same
day, not from a backup. 2026-08-17 is verbatim. 2026-08-12 is complete except
its ``atm_lot_cost`` event, whose ~200-name ``costs`` array was not captured —
that event is OMITTED rather than partially fabricated, and the omission is
recorded in the marker below. Every restored log carries a ``log_restored``
event as its first entry so the provenance is visible on the page, not just in
this file.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime

_MARKER = "log_restored"

# --- 2026-08-17: captured verbatim ----------------------------------------- #
LOG_0817 = [
    {
        "ts": "09:17:09.079",
        "event": "skipped_late_boot",
        "armed_at": "09:17:09",
        "fix": "boot OpenAlgo before 09:15 IST",
    },
    {
        "ts": "11:41:13.650",
        "event": "late_boot_restart",
        "armed_at": "11:41:13",
        "preserved_events": 1,
    },
    {
        "ts": "11:55:03.334",
        "event": "late_boot_restart",
        "armed_at": "11:55:03",
        "preserved_events": 2,
    },
    {
        "ts": "12:40:54.844",
        "event": "late_boot_restart",
        "armed_at": "12:40:54",
        "preserved_events": 3,
    },
    {
        "ts": "13:15:05.417",
        "event": "late_boot_restart",
        "armed_at": "13:15:05",
        "preserved_events": 4,
    },
    {
        "ts": "13:40:05.790",
        "event": "late_boot_restart",
        "armed_at": "13:40:05",
        "preserved_events": 5,
    },
    {
        "ts": "14:10:17.208",
        "event": "late_boot_restart",
        "armed_at": "14:10:17",
        "preserved_events": 6,
    },
]

# --- 2026-08-12: the diagnostic spine, atm_lot_cost omitted ---------------- #
LOG_0812 = [
    {
        "ts": "09:10:01.861",
        "event": "armed",
        "universe": 192,
        "prev_closes": 211,
        "vol_mult": 1.5,
        "top_n": 3,
        "mode": "live",
        "no_entry_after": "09:29",
        "exit_time": "09:30",
        "trade_side": "long_only",
        "instrument": "atm_option",
        "rolling_watchlist_enabled": True,
        "rolling_cadence_s": 30,
        "rolling_top_n": 3,
        "shadow_excluded_side": True,
        "shadow_side": "S",
        "shadow_max_trades": 3,
        "sizing_mode": "fixed",
        "margin_per_slot": 60000.0,
        "margin_effective": 60000.0,
        "notional": 300000.0,
        "cum_realized_pnl": 0.0,
        "config_source": "ui",
        "tick_capture": True,
        "tick_capture_universe": True,
        "first_candle_source": "quotes",
        "baseline_includes_first_minute": False,
        "option_liquidity_gate_enabled": True,
        "option_liquidity_min_pctile": 15.0,
        "option_liquidity_backfill_rank": True,
        "option_liquidity_universe_after": 192,
        "option_liquidity_excluded": [
            "ALKEM",
            "BAJAJHLDNG",
            "CONCOR",
            "EXIDEIND",
            "GODREJCP",
            "ICICIPRULI",
            "IREDA",
            "LICI",
            "MFSL",
            "MOTILALOFS",
            "NAM-INDIA",
            "NUVAMA",
            "PAGEIND",
            "PETRONET",
            "PHOENIXLTD",
            "SAMMAANCAP",
            "SHREECEM",
            "TIINDIA",
            "VMM",
        ],
    },
    {
        "ts": "09:16:00.683",
        "event": "first_candles",
        "source": "ticks",
        "covered": 0,
        "universe": 192,
    },
    {
        "ts": "09:30:00.059",
        "event": "no_ticks_received",
        "hint": (
            "ZMQ feed delivered ZERO ticks in the window — check WS proxy (8765), "
            "broker session, scanner presubscribe"
        ),
    },
    {
        "ts": "09:35:00.104",
        "event": "summary",
        "selected": 0,
        "entered": 0,
        "filled": 0,
        "paper": 0,
        "sim": 0,
        "shadow": 0,
        "rolling_added": 0,
        "day": "done",
        "captured_drift": [],
    },
    {
        "ts": "09:35:00.185",
        "event": "fill_reconcile",
        "status": "ok",
        "reconciled": 0,
        "pending": 0,
    },
    {"ts": "09:35:00.262", "event": "opt_shadow", "status": "ok", "priced": 0, "skipped": 0},
    {
        "ts": "09:35:03.110",
        "event": "opt_liquidity_path",
        "status": "ok",
        "priced": 0,
        "skipped": 9,
    },
    {"ts": "09:35:03.110", "event": "tick_capture_flushed", "records": 0},
]

RESTORE = {
    "2026-08-12": (
        LOG_0812,
        "transcript capture, 2026-08-17; complete except the atm_lot_cost event, "
        "whose ~200-name costs array was not captured and is OMITTED rather than "
        "partially reconstructed",
    ),
    "2026-08-17": (LOG_0817, "transcript capture, 2026-08-17; verbatim"),
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write; omit for a dry run")
    args = ap.parse_args()

    from database.open15_breakout_db import (
        Open15Trade,
        db_session,
        get_day_log,
        save_day_log,
        total_realized_pnl,
    )

    before_pnl = total_realized_pnl()
    print(f"total_realized_pnl BEFORE: {before_pnl}")

    rows = (
        db_session.query(Open15Trade)
        .filter(Open15Trade.fill == "replay")
        .order_by(Open15Trade.trade_date, Open15Trade.id)
        .all()
    )
    print(f"replay journal rows found: {len(rows)}")

    # ---- 1. back up EVERYTHING we are about to touch ---------------------- #
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = {
        "created": stamp,
        "issue": 620,
        "replay_rows": [
            {c.name: getattr(r, c.name) for c in Open15Trade.__table__.columns} for r in rows
        ],
        "day_logs_being_replaced": {d: get_day_log(d) for d in RESTORE},
        "total_realized_pnl_before": before_pnl,
    }
    path = pathlib.Path(f"audit/open15_replay_removal_backup_{stamp}.json")
    if args.apply:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(backup, indent=1, default=str), encoding="utf-8")
        print(f"backup written: {path}")
    else:
        print(f"backup WOULD be written: {path} ({len(backup['replay_rows'])} rows)")

    # ---- 2. delete the replay rows ---------------------------------------- #
    for r in rows:
        print(f"  delete {r.trade_date} {r.symbol:12} {r.side} fill={r.fill} pnl={r.pnl}")
    if args.apply:
        n = (
            db_session.query(Open15Trade)
            .filter(Open15Trade.fill == "replay")  # scoped EXPLICITLY, never by date alone
            .delete(synchronize_session=False)
        )
        db_session.commit()
        print(f"deleted {n} replay rows")

    # ---- 3. restore the clobbered decision logs --------------------------- #
    for date, (events, provenance) in RESTORE.items():
        marker = {
            "ts": "00:00:00.000",
            "event": _MARKER,
            "reason": "this day's decision log was overwritten by an open15 replay (#620)",
            "provenance": provenance,
            "restored_at": stamp,
        }
        payload = [marker, *events]
        print(f"  restore {date}: {len(payload)} events (marker + {len(events)})")
        if args.apply and not save_day_log(date, payload):
            print(f"  !! save_day_log FAILED for {date}")

    after = total_realized_pnl()
    print(f"total_realized_pnl AFTER : {after}")
    # An explicit raise, not an assert: this guards a production-data invariant
    # and `python -O` strips asserts. If real P&L moved, a replay row was being
    # counted as real and the deletion has changed compound sizing — stop loudly.
    if after != before_pnl:
        raise SystemExit(
            f"ABORT: total_realized_pnl moved {before_pnl} -> {after}. Replay rows were "
            f"being counted as REAL; restore from the backup above and investigate."
        )
    print("OK - real P&L unchanged" if args.apply else "DRY RUN - nothing written")
    db_session.remove()


if __name__ == "__main__":
    main()
