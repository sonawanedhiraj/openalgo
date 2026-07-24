"""One-time backfill for the pre-#447 in-house echo pollution (operator-run).

Issue #447 fixed the *forward* path: the ``ScanHitPoster`` now tags its
payloads with ``source='inhouse_scanner'`` and the simplified-engine webhook
audits those invocations as ``cycle_kind='inhouse_echo'``. But every echo row
written BEFORE the fix (poster active since ~2026-07-01) is still stored as
``cycle_kind='chartink'`` — byte-identical to a genuine Chartink cycle — so the
``scanner_comparison`` rows for that window remain self-referential garbage
(in-house SELL hits counted as "Chartink BUY" via the old whitespace-token side
check; in-house BUY echoes matching themselves as fake-perfect intersections).

This script reclassifies those historical rows **heuristically** and recomputes
the affected ``scanner_comparison`` days. A pre-fix echo row is identified as:

* a ``cycle_kind='chartink'`` row whose ``screener_buy`` + ``screener_sell``
  union contains EXACTLY ONE symbol (the poster posts one symbol per event), and
* an in-house ``scan_results`` row (``source='inhouse'``) for the same symbol
  exists within ``--tolerance-seconds`` (default 3) of the cycle's
  ``started_at`` — the poster fires immediately after the scanner persists the
  hit (observed skew on 2026-07-24: ~70-800 ms across all 226 echoes).

A genuine single-symbol Chartink alert landing inside the window AND matching
an identical in-house hit within 3 s would be misclassified — accepted risk:
genuine cycle posts arrive on Chartink's ~15-min cadence at offsets like
:34/:49/:04/:19, so a sub-3-second coincidence is vanishingly rare. Eyeball the
dry-run output before applying.

**Dry-run by default** — prints per-day classification counts and the projected
genuine Chartink unions, writes NOTHING. Re-run with ``--apply`` to (1) flip the
matched rows to ``cycle_kind='inhouse_echo'`` and (2) recompute + persist the
``scanner_comparison`` rows for every day in the range via
``run_comparison_for_date(date, dispatch_telegram=False)`` (no Telegram spam;
the delete-then-insert per (date, side) overwrites the polluted rows).

Idempotent: already-reclassified rows are excluded from the scan, so applying
twice is a no-op. NOT wired into the runtime — operator CLI only.

Examples::

    # Dry run over the polluted window (writes nothing):
    uv run python -m services.scanner_comparison_echo_backfill \
        --from 2026-07-01 --to 2026-07-24

    # After review, reclassify + recompute:
    uv run python -m services.scanner_comparison_echo_backfill \
        --from 2026-07-01 --to 2026-07-24 --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass, field

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = pytz.timezone("Asia/Kolkata")

DEFAULT_TOLERANCE_SECONDS = 3.0


def _parse_ts(raw: str) -> dt.datetime | None:
    """ISO string → aware datetime (naive values are assumed IST)."""
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = _IST.localize(parsed)
    return parsed


def _json_list(blob: str | None) -> list[str]:
    if not blob:
        return []
    try:
        items = json.loads(blob)
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    return [str(s).strip().upper() for s in items if s]


@dataclass
class DayClassification:
    """Classification result for one IST date."""

    date: str
    echo_row_ids: list[int] = field(default_factory=list)
    n_chartink_rows: int = 0
    genuine_buy: set[str] = field(default_factory=set)
    genuine_sell: set[str] = field(default_factory=set)
    echo_symbols: set[str] = field(default_factory=set)


def classify_echoes(
    date: str, tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS
) -> DayClassification:
    """Classify ``cycle_kind='chartink'`` rows for ``date`` into echo/genuine.

    Read-only. Returns the row ids to reclassify plus the projected genuine
    BUY/SELL unions (what the Chartink side of the comparison becomes after
    the apply pass).
    """
    from database import scan_cycle_db as ccdb
    from database import scanner_db as sdb

    result = DayClassification(date=date)

    # In-house hits for the day: [(aware_dt, symbol), ...]
    inhouse: list[tuple[dt.datetime, str]] = []
    sess = sdb.db_session
    try:
        rows = (
            sess.query(sdb.ScanResult)
            .filter(sdb.ScanResult.source == "inhouse")
            .filter(sdb.ScanResult.run_at.like(f"{date}%"))
            .all()
        )
        for row in rows:
            ts = _parse_ts(row.run_at)
            if ts is None:
                continue
            for sym in _json_list(row.symbols):
                inhouse.append((ts, sym))
    finally:
        sess.remove()

    cyc_sess = ccdb.db_session
    try:
        cycles = (
            cyc_sess.query(ccdb.ScanCycle)
            .filter(ccdb.ScanCycle.cycle_kind == "chartink")
            .filter(ccdb.ScanCycle.started_at.like(f"{date}%"))
            .all()
        )
        for cyc in cycles:
            result.n_chartink_rows += 1
            buy = _json_list(cyc.screener_buy)
            sell = _json_list(cyc.screener_sell)
            union = set(buy) | set(sell)
            is_echo = False
            if len(union) == 1:
                sym = next(iter(union))
                started = _parse_ts(cyc.started_at)
                if started is not None:
                    for ts, ih_sym in inhouse:
                        if ih_sym == sym and abs((started - ts).total_seconds()) <= (
                            tolerance_seconds
                        ):
                            is_echo = True
                            break
            if is_echo:
                result.echo_row_ids.append(cyc.id)
                result.echo_symbols.update(union)
            else:
                result.genuine_buy.update(buy)
                result.genuine_sell.update(sell)
    finally:
        cyc_sess.remove()

    return result


def apply_reclassification(row_ids: list[int]) -> int:
    """Flip the given scan_cycle rows to ``cycle_kind='inhouse_echo'``.

    Returns the number of rows actually updated. Only rows still classified
    ``'chartink'`` are touched, so a re-run is a no-op.
    """
    if not row_ids:
        return 0
    from database import scan_cycle_db as ccdb

    sess = ccdb.db_session
    try:
        updated = (
            sess.query(ccdb.ScanCycle)
            .filter(ccdb.ScanCycle.id.in_(row_ids))
            .filter(ccdb.ScanCycle.cycle_kind == "chartink")
            .update({ccdb.ScanCycle.cycle_kind: "inhouse_echo"}, synchronize_session=False)
        )
        sess.commit()
        return int(updated)
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.remove()


def _daterange(start: dt.date, end: dt.date):
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reclassify pre-#447 in-house echo scan_cycle rows and "
        "recompute scanner_comparison (dry-run by default).",
    )
    parser.add_argument("--from", dest="date_from", required=True, help="start date YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", required=True, help="end date YYYY-MM-DD")
    parser.add_argument(
        "--tolerance-seconds",
        type=float,
        default=DEFAULT_TOLERANCE_SECONDS,
        help=f"max |scan_cycle.started_at - scan_results.run_at| to count as an "
        f"echo (default {DEFAULT_TOLERANCE_SECONDS})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the reclassification and recompute scanner_comparison "
        "(default: dry-run, print only)",
    )
    args = parser.parse_args(argv)

    start = dt.date.fromisoformat(args.date_from)
    end = dt.date.fromisoformat(args.date_to)
    if end < start:
        print("--to is before --from", file=sys.stderr)
        return 2

    total_echo = 0
    affected_dates: list[str] = []
    for day in _daterange(start, end):
        date = day.isoformat()
        cls = classify_echoes(date, tolerance_seconds=args.tolerance_seconds)
        if cls.n_chartink_rows == 0:
            continue
        total_echo += len(cls.echo_row_ids)
        affected_dates.append(date)
        print(
            f"{date}: chartink_rows={cls.n_chartink_rows} "
            f"echo={len(cls.echo_row_ids)} "
            f"genuine={cls.n_chartink_rows - len(cls.echo_row_ids)}"
        )
        print(f"  genuine BUY  union: {sorted(cls.genuine_buy)}")
        print(f"  genuine SELL union: {sorted(cls.genuine_sell)}")
        print(f"  echo symbols      : {sorted(cls.echo_symbols)}")

        if args.apply:
            updated = apply_reclassification(cls.echo_row_ids)
            print(f"  APPLIED: {updated} rows -> cycle_kind='inhouse_echo'")

    if not args.apply:
        print(
            f"\nDRY RUN: would reclassify {total_echo} rows across "
            f"{len(affected_dates)} days. Re-run with --apply to write."
        )
        return 0

    # Recompute the comparison for every affected day (idempotent
    # delete-then-insert per (date, side); no Telegram).
    from services.scanner_comparison_eod_service import run_comparison_for_date

    for date in affected_dates:
        result = run_comparison_for_date(date, dispatch_telegram=False)
        buy, sell = result["BUY"], result["SELL"]
        print(
            f"recomputed {date}: "
            f"BUY ih={buy['inhouse_count']} ch={buy['chartink_count']} "
            f"int={buy['intersection_count']} | "
            f"SELL ih={sell['inhouse_count']} ch={sell['chartink_count']} "
            f"int={sell['intersection_count']}"
        )

    print(f"\nDONE: reclassified {total_echo} rows, recomputed {len(affected_dates)} days.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
