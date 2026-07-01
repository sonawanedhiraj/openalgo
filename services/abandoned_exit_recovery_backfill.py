"""One-time backfill for abandoned-exit P&L recovery (operator-run).

The live boot path (:func:`services.abandoned_exit_recovery_service.recover_abandoned_exits`
wired at ``app.py`` boot) recovers abandoned rows going forward. This script
runs the same recovery over the historical ``abandoned_% AND exit_price IS NULL``
rows — the 16 rows behind #262 — for operator review.

**Dry-run by default** — prints what it *would* stamp and exits without touching
the journal. Re-run with ``--apply`` after eyeballing the output.

Examples::

    # Dry run over every abandoned-NULL row (writes nothing):
    uv run python -m services.abandoned_exit_recovery_backfill

    # Restrict to a single day:
    uv run python -m services.abandoned_exit_recovery_backfill --date 2026-06-29

    # After review, actually write the recovered exit rows:
    uv run python -m services.abandoned_exit_recovery_backfill --apply

Idempotent: once a row's ``exit_price`` is stamped it no longer matches, so
applying twice is a no-op. Read-only on ``sandbox.db`` either way.
"""

from __future__ import annotations

import argparse
import json
import sys

from services.abandoned_exit_recovery_service import (
    DEFAULT_STRATEGY_NAME,
    recover_abandoned_exits,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=None,
        help="Restrict to rows placed on this IST day (YYYY-MM-DD). Omit for all.",
    )
    parser.add_argument(
        "--strategy",
        default=DEFAULT_STRATEGY_NAME,
        help="Journal strategy to scope to (pass 'all' for every strategy).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write recovered exit rows. Without this flag the run is a dry run.",
    )
    args = parser.parse_args(argv)

    dry_run = not args.apply
    strategy = None if args.strategy == "all" else args.strategy

    mode = "DRY RUN (no writes)" if dry_run else "APPLY (writing exit rows)"
    scope = args.date or "all days"
    print(f"# Abandoned-exit recovery backfill — {mode}")
    print(f"# scope={scope}  strategy={args.strategy}\n")

    result = recover_abandoned_exits(args.date, strategy_name=strategy, dry_run=dry_run)

    print(
        f"checked={result.rows_checked} "
        f"{'would_recover' if dry_run else 'recovered'}={result.rows_recovered} "
        f"skipped={len(result.skipped)}"
    )
    for d in result.recovered:
        print(
            f"    + {d['symbol']:<12} {d['direction']:<5} qty={d['quantity']:<4} "
            f"entry={d['entry_price']} exit={d['exit_price']} pnl={d['pnl']} "
            f"fills={d['fills']} at={d['exit_time']}"
        )
    for s in result.skipped:
        print(f"    - {s.get('symbol')} (jid={s.get('journal_id')}): {s.get('reason')}")

    verb = "would be reconciled" if dry_run else "reconciled"
    print(f"\n# net P&L {verb}: {result.total_pnl:,.2f}")
    if result.recovered:
        print(f"# detail_json={json.dumps(result.recovered)}")
    if dry_run and result.rows_recovered:
        print("# review the rows above, then re-run with --apply to write them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
