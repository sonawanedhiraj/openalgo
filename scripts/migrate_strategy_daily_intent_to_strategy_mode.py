#!/usr/bin/env python
"""Migrate the latest ``strategy_daily_intent`` row per strategy into the
mode-only ``strategy_mode`` table (Item B1).

The mode-only architecture retires the ``{mode, intent, daily_capital_cap}``
daily control in favour of a single persistent ``mode`` per strategy. This
one-shot migration carries the existing ``mode`` forward:

  * For each ``strategy_name`` in ``strategy_daily_intent``, take the row with
    the latest ``intent_date`` (tie-broken by ``updated_at``) and copy its
    ``mode`` into ``strategy_mode``.
  * The ``intent`` and ``daily_capital_cap`` axes are intentionally dropped —
    automated safety guards now live in ``strategy_runtime_override``.
  * ``mode='skip'`` (no longer a valid mode) maps to ``sandbox`` — the safe
    equivalent of "place no real orders". It never maps to ``live``.

Idempotent: a strategy that already has a ``strategy_mode`` row is left
untouched, so re-running inserts nothing (operator edits win over the backfill).

Usage:  uv run python scripts/migrate_strategy_daily_intent_to_strategy_mode.py
        (add --json for machine-readable output)

Inserts only; never deletes or overwrites an existing ``strategy_mode`` row.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow ``uv run python scripts/<this>.py`` (sys.path[0] is scripts/, not the
# project root) as well as ``-m scripts.<this>`` and pytest import.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.logging import get_logger

logger = get_logger(__name__)

# strategy_daily_intent.mode ∈ {live, sandbox, skip}; strategy_mode ∈ {live,
# sandbox}. 'skip' collapses to the conservative 'sandbox' (virtual orders, no
# real-money risk) — never to 'live'.
_MODE_MAP = {"live": "live", "sandbox": "sandbox", "skip": "sandbox"}


def _latest_intent_per_strategy() -> dict[str, dict]:
    """Return {strategy_name: latest intent row dict} from strategy_daily_intent.

    "Latest" = max ``intent_date`` (YYYY-MM-DD string compare is chronological),
    tie-broken by ``updated_at``.
    """
    from database import strategy_daily_intent_db as sdi

    rows = sdi.list_intents("all")
    latest: dict[str, dict] = {}
    for r in rows:
        name = r["strategy_name"]
        cur = latest.get(name)
        if cur is None:
            latest[name] = r
            continue
        key_new = (r.get("intent_date") or "", r.get("updated_at") or "")
        key_cur = (cur.get("intent_date") or "", cur.get("updated_at") or "")
        if key_new > key_cur:
            latest[name] = r
    return latest


def migrate() -> dict:
    """Backfill ``strategy_mode`` from the latest intent row per strategy.

    Returns ``{'inserted': n, 'skipped_existing': m, 'details': [...]}``. Never
    raises on a per-strategy error — it records the failure in ``details`` and
    continues, so one bad row can't abort the whole migration.
    """
    from database import strategy_mode_db as sm

    latest = _latest_intent_per_strategy()
    inserted = 0
    skipped_existing = 0
    details: list[dict] = []

    for name, row in sorted(latest.items()):
        src_mode = (row.get("mode") or "").strip().lower()
        target_mode = _MODE_MAP.get(src_mode, "sandbox")
        try:
            if sm.get_mode(name) is not None:
                skipped_existing += 1
                details.append({"strategy": name, "action": "skipped_existing"})
                continue
            # One-shot migration is a documented, allowed unchecked caller
            # (no live signals fire during migration; preflight is irrelevant).
            sm._set_mode_unchecked(
                name,
                target_mode,
                updated_by="migration",
                notes=(
                    f"migrated from strategy_daily_intent (date={row.get('intent_date')}, "
                    f"src_mode={src_mode})"
                ),
            )
            inserted += 1
            details.append(
                {"strategy": name, "action": "inserted", "mode": target_mode, "src_mode": src_mode}
            )
        except Exception as e:  # never abort the whole run on one bad row
            logger.exception("strategy_mode migration: failed for %s: %s", name, e)
            details.append({"strategy": name, "action": "error", "error": str(e)})

    logger.info(
        "strategy_mode migration: %d inserted, %d skipped (existing)",
        inserted,
        skipped_existing,
    )
    return {"inserted": inserted, "skipped_existing": skipped_existing, "details": details}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    # Ensure both tables exist before migrating (safe/idempotent).
    from database import strategy_mode_db as sm

    sm.init_db()

    result = migrate()

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    print("=" * 70)
    print("strategy_daily_intent -> strategy_mode migration (Item B1)")
    print("=" * 70)
    print(f"  inserted:         {result['inserted']}")
    print(f"  skipped_existing: {result['skipped_existing']}")
    for d in result["details"]:
        if d["action"] == "inserted":
            print(f"    + {d['strategy']}: {d['src_mode']} -> {d['mode']}")
        elif d["action"] == "skipped_existing":
            print(f"    = {d['strategy']}: already has a strategy_mode row (untouched)")
        elif d["action"] == "error":
            print(f"    ! {d['strategy']}: ERROR {d['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
