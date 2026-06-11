"""One-time backfill for the EOD reconciliation gap (operator-run).

The live job (:func:`services.engine_eod_reconciliation_service.reconcile_engine_journal`
wired into the simplified engine's EOD summary) only fixes *future* days. Days
that already closed before the fix shipped still have incomplete journals — e.g.
2026-06-10, where 3 sandbox square-off exits (OIL, HINDZINC, TATAELXSI) were
never journaled.

This script reconciles a **date range** for operator review. It is **dry-run by
default** — it prints what it *would* write and exits without touching the
journal. Re-run with ``--apply`` only after eyeballing the dry-run output.

Examples::

    # Dry run for the known-incomplete day (writes nothing):
    uv run python -m services.engine_eod_reconciliation_backfill --from 2026-06-10 --to 2026-06-10

    # After review, actually write the exit rows:
    uv run python -m services.engine_eod_reconciliation_backfill --from 2026-06-10 --to 2026-06-10 --apply

Idempotent: the underlying reconciliation only closes ``exited_at IS NULL`` rows,
so applying twice is a no-op. Read-only on ``sandbox.db`` either way.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from services.engine_eod_reconciliation_service import (
    DEFAULT_STRATEGY_NAME,
    reconcile_engine_journal,
)


def _daterange(start: dt.date, end: dt.date):
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", required=True, help="ISO start date YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", required=True, help="ISO end date YYYY-MM-DD")
    parser.add_argument(
        "--strategy", default=DEFAULT_STRATEGY_NAME, help="Journal strategy to scope to"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write exit rows. Without this flag the run is a dry run.",
    )
    args = parser.parse_args(argv)

    start = dt.date.fromisoformat(args.date_from)
    end = dt.date.fromisoformat(args.date_to)
    dry_run = not args.apply

    mode = "DRY RUN (no writes)" if dry_run else "APPLY (writing exit rows)"
    print(f"# EOD reconciliation backfill — {mode}")
    print(f"# range {start} .. {end}  strategy={args.strategy}\n")

    grand_added = 0
    for day in _daterange(start, end):
        result = reconcile_engine_journal(day, strategy_name=args.strategy, dry_run=dry_run)
        grand_added += result.exits_added
        if result.entries_checked == 0 and result.exits_added == 0:
            print(f"{day}: no open entries to reconcile")
            continue
        print(
            f"{day}: checked={result.entries_checked} "
            f"{'would_add' if dry_run else 'added'}={result.exits_added} "
            f"skipped={len(result.skipped)}"
        )
        for d in result.exit_details:
            print(f"    + {d['symbol']:<12} {d['direction']:<5} qty={d['quantity']:<4} "
                  f"exit={d['exit_price']} pnl={d['pnl']} fills={d['fills']} "
                  f"order={d['exit_order_id']}")
        for s in result.skipped:
            print(f"    - {s.get('symbol')}: {s.get('reason')}")
        print(f"    detail_json={json.dumps(result.exit_details)}")

    verb = "would be added" if dry_run else "added"
    print(f"\n# total exit rows {verb}: {grand_added}")
    if dry_run and grand_added:
        print("# review the rows above, then re-run with --apply to write them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
