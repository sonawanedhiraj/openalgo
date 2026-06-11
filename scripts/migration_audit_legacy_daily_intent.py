#!/usr/bin/env python
"""Post-migration audit for the legacy ``daily_intent`` table (plan item #7).

Read-only. Two checks, then a printed migration-retirement plan:

  1. READER SWEEP — every non-test reader of the legacy ``daily_intent`` table
     (the ``database.daily_intent_db`` API: ``get_daily_intent`` / ``DailyIntent``
     / ``resolve_effective_mode``), classified as FIXED / BY-DESIGN /
     FOLLOW-UP. This is the "who else reads the old table?" sweep the 2026-06-11
     retrospective says a migration is not done without.

  2. PHANTOM-ROW CHECK — open ``trade_journal`` rows (``exited_at IS NULL``), the
     class that the pytest-pollution incident produced, plus the RELIANCE
     101.7/97.4 synthetic signature. Distinguishes genuine open positions
     (real broker order ids) from synthetic test rows.

Usage:  uv run python scripts/migration_audit_legacy_daily_intent.py
        (add --json for machine-readable output)

Writes nothing. Opens ``db/openalgo.db`` read-only (``mode=ro``). Safe to run
during market hours.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "db" / "openalgo.db"

# Synthetic entry prices from test/e2e/test_fno_flows.py — the 2026-06-11
# pollution signature (BUY fill stub 101.7, SELL reference_price 97.5 -> 97.4).
_PHANTOM_RELIANCE_PRICES = (101.7, 97.4)
# Real broker order ids are bare digit strings (~16 digits, e.g. 26060960448274);
# synthetic test ids carry an ``OID-`` prefix.
_REAL_ORDER_ID_RE = re.compile(r"^\d{8,}$")


# Static classification of each known legacy-daily_intent reader. Keyed by
# "path:symbol". Kept here (rather than inferred) so the audit states an
# explicit verdict the operator can trust.
READER_VERDICTS = {
    "services/preflight_service.py": (
        "FIXED",
        "_check_intent / _check_effective_mode now source from the unified "
        "table via resolve_strategy_mode (commit on 2026-06-11, plan item #2). "
        "The remaining get_daily_intent call is the documented legacy "
        "back-compat fall-through.",
    ),
    "services/mode_service.py": (
        "BY-DESIGN",
        "resolve_effective_mode is the legacy GLOBAL resolver (returns "
        "EffectiveMode), still load-bearing for place_order_service and the "
        "audit-mode capture; resolve_strategy_mode reads legacy only as its "
        "documented fall-through. Intentionally separate — see "
        "docs/design/strategy_daily_intent.md.",
    ),
    "blueprints/chartink.py": (
        "BY-DESIGN",
        "Calls resolve_effective_mode() to stamp the scan-cycle audit row only; "
        "order placement is unchanged by it (comment at the call site).",
    ),
    "app.py": (
        "BY-DESIGN",
        "Imports daily_intent_db.init_db to ensure the table exists at boot — "
        "schema init, not an intent read.",
    ),
    "blueprints/mode_status.py": (
        "FOLLOW-UP",
        "GET /mode/status reads legacy get_daily_intent + resolve_effective_mode "
        "directly and does NOT surface the unified strategy_daily_intent table — "
        "so on a unified-only day the status page shows daily_intent=null and a "
        "stale effective_mode. Observability only (no order-gating), so not a "
        "trading risk, but it should also consult resolve_strategy_mode / "
        "list_intents in a follow-up.",
    ),
}


def _grep_readers() -> list[tuple[str, int, str]]:
    """ripgrep/grep for legacy-table readers, excluding tests + the table's own
    module + the unified table + the venv. Returns (path, lineno, text)."""
    pattern = (
        r"from database\.daily_intent_db import|daily_intent_db\.|"
        r"get_daily_intent\(|DailyIntent\b|resolve_effective_mode"
    )
    # Prefer ripgrep; fall back to git grep.
    for cmd in (
        ["rg", "-n", "--no-heading", pattern, "--glob", "*.py"],
        ["git", "grep", "-nE", pattern, "--", "*.py"],
    ):
        try:
            out = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if out.returncode in (0, 1):  # 1 = no matches (still a clean run)
            break
    else:
        return []

    hits: list[tuple[str, int, str]] = []
    for line in out.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path, lineno, text = parts[0], parts[1], parts[2]
        norm = path.replace("\\", "/")
        if (
            norm.startswith("test/")
            or "/test_" in norm
            or norm.endswith("/daily_intent_db.py")
            or "strategy_daily_intent" in norm
            or ".venv/" in norm
        ):
            continue
        # Skip false positives where ``DailyIntent`` is the unified
        # ``StrategyDailyIntent`` class / ``strategy_daily_intent_db`` module
        # rather than the legacy table.
        if "strategy_daily_intent" in text.lower() or "StrategyDailyIntent" in text:
            continue
        try:
            hits.append((norm, int(lineno), text.strip()))
        except ValueError:
            continue
    return hits


# A "direct" reader touches the daily_intent TABLE API; an "indirect" caller
# only invokes resolve_effective_mode (the legacy GLOBAL resolver), which reads
# the table internally. The latter are the place_order_service-family order
# services and are all BY-DESIGN this pass (Phase C of the retirement plan).
_DIRECT_TABLE_RE = re.compile(
    r"from database\.daily_intent_db import|daily_intent_db\.|get_daily_intent\(|DailyIntent\b"
)


def _verdict_for(path: str, texts: list[str]) -> tuple[str, str]:
    for key, verdict in READER_VERDICTS.items():
        if path == key:
            return verdict
    is_direct = any(_DIRECT_TABLE_RE.search(t) for t in texts)
    if is_direct:
        return (
            "REVIEW",
            "Direct daily_intent-table reader not in the known "
            "classification — inspect manually.",
        )
    return (
        "BY-DESIGN-INDIRECT",
        "Calls the legacy GLOBAL resolver "
        "resolve_effective_mode (which reads the table internally), not the "
        "daily_intent table directly. Part of the place_order_service order "
        "family — stays on the legacy resolver this pass (Phase C).",
    )


def _phantom_check() -> dict:
    if not DB_PATH.exists():
        return {"error": f"{DB_PATH} not found"}
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        open_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, placed_at, symbol, direction, quantity, strategy_name, "
                "signal_source, entry_price, entry_order_id, exited_at "
                "FROM trade_journal WHERE exited_at IS NULL ORDER BY id"
            ).fetchall()
        ]
        reliance_phantom_total = conn.execute(
            "SELECT COUNT(*) FROM trade_journal WHERE symbol='RELIANCE' "
            "AND entry_price IN (?, ?)",
            _PHANTOM_RELIANCE_PRICES,
        ).fetchone()[0]
        reliance_phantom_open = conn.execute(
            "SELECT COUNT(*) FROM trade_journal WHERE symbol='RELIANCE' "
            "AND entry_price IN (?, ?) AND exited_at IS NULL",
            _PHANTOM_RELIANCE_PRICES,
        ).fetchone()[0]
        trending_open = conn.execute(
            "SELECT COUNT(*) FROM trade_journal WHERE exited_at IS NULL "
            "AND strategy_name='trending_equity_intraday'"
        ).fetchone()[0]
    finally:
        conn.close()

    for r in open_rows:
        oid = (r.get("entry_order_id") or "").strip()
        r["looks_real_order_id"] = bool(_REAL_ORDER_ID_RE.match(oid))
    return {
        "open_rows": open_rows,
        "open_count": len(open_rows),
        "reliance_phantom_total": reliance_phantom_total,
        "reliance_phantom_open": reliance_phantom_open,
        "trending_equity_intraday_open": trending_open,
    }


MIGRATION_PLAN = """\
Legacy `daily_intent` retirement plan
-------------------------------------
The unified `strategy_daily_intent` table is now the single pre-market control
surface, and the boot migration (database.strategy_daily_intent_db.
migrate_legacy_daily_intent) backfills legacy rows forward. To retire the legacy
table safely:

  Phase A (done 2026-06-11): point the ORDER-GATING reader (preflight) at the
    unified resolver. place_order_service keeps the legacy global resolver
    (resolve_effective_mode) deliberately — the unified gate lives in the engines.

  Phase B (follow-up): migrate the remaining FOLLOW-UP reader
    (blueprints/mode_status.py) to also surface strategy_daily_intent, so every
    observability/decision surface reflects the unified table.

  Phase C (follow-up): once mode_status + any other FOLLOW-UP readers are off the
    legacy API and resolve_effective_mode's legacy dependency is the only one
    left, decide whether to (a) reimplement resolve_effective_mode on top of
    resolve_strategy_mode, or (b) keep it as the documented legacy global. Only
    after that is the legacy `daily_intent` table a candidate for DROP.

  Drop criteria: zero non-test readers of database.daily_intent_db remain (this
    script reports FIXED/BY-DESIGN/FOLLOW-UP — DROP requires all rows FIXED or
    removed), AND a full migrate_legacy_daily_intent has run so no historical
    intent is lost. Keep the table (read-only) for at least one trading week
    after the last reader is removed as a rollback cushion.
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    readers = _grep_readers()
    texts_by_path: dict[str, list[str]] = {}
    for path, _lineno, text in readers:
        texts_by_path.setdefault(path, []).append(text)
    classified = []
    for path, lineno, text in readers:
        verdict, note = _verdict_for(path, texts_by_path[path])
        classified.append(
            {"path": path, "line": lineno, "text": text, "verdict": verdict, "note": note}
        )
    phantom = _phantom_check()

    if args.json:
        print(json.dumps({"readers": classified, "phantom": phantom}, indent=2, default=str))
        return 0

    print("=" * 78)
    print("LEGACY daily_intent — POST-MIGRATION AUDIT (plan item #7)")
    print("=" * 78)
    print("\n1. READER SWEEP — non-test readers of the legacy daily_intent API\n")
    by_verdict: dict[str, list[dict]] = {}
    for c in classified:
        by_verdict.setdefault(c["verdict"], []).append(c)
    for verdict in ("FOLLOW-UP", "REVIEW", "FIXED", "BY-DESIGN", "BY-DESIGN-INDIRECT"):
        rows = by_verdict.get(verdict, [])
        if not rows:
            continue
        # One note per path (the grep yields several lines per file).
        seen_paths = set()
        print(f"  [{verdict}]")
        for c in rows:
            if c["path"] in seen_paths:
                continue
            seen_paths.add(c["path"])
            print(f"    - {c['path']}: {c['note']}")
        print()

    print("2. PHANTOM-ROW CHECK — trade_journal\n")
    if "error" in phantom:
        print(f"    ERROR: {phantom['error']}")
    else:
        print(
            f"    RELIANCE 101.7/97.4 synthetic rows: {phantom['reliance_phantom_total']} "
            f"total, {phantom['reliance_phantom_open']} still open"
        )
        print(
            f"    open trending_equity_intraday rows: "
            f"{phantom['trending_equity_intraday_open']}"
        )
        print(f"    total open (exited_at IS NULL) rows: {phantom['open_count']}")
        for r in phantom["open_rows"]:
            kind = "REAL position" if r["looks_real_order_id"] else "SYNTHETIC/test"
            print(
                f"      • id={r['id']} {r['symbol']} {r['direction']} "
                f"qty={r['quantity']} @ {r['entry_price']} "
                f"order_id={r['entry_order_id']} ({kind}) "
                f"strategy={r['strategy_name']} placed={r['placed_at']}"
            )
        if phantom["reliance_phantom_open"] == 0 and phantom["reliance_phantom_total"]:
            print(
                "\n    OK: all RELIANCE pollution rows are CLOSED (exited_at set) — "
                "harmless to the open-position rehydrate. They remain in the DB as "
                "closed rows; deleting them is a separate operator decision."
            )
        if phantom["open_count"]:
            print(
                "\n    ACTION: open rows above are rehydrated as positions on the "
                "next engine boot. Confirm each REAL row is reconciled (its sandbox/"
                "broker position is actually flat) via "
                "services/engine_eod_reconciliation_backfill.py --apply before "
                "restart; SYNTHETIC rows should be closed/removed by the operator."
            )

    print("\n" + "=" * 78)
    print(MIGRATION_PLAN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
