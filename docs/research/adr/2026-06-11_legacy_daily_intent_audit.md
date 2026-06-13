<!-- migrated from outputs/2026-06-11_migration_audit.md on 2026-06-13 | summary: 2026-06-11 â€” Legacy `daily_intent` post-migration audit (plan item #7) -->

# 2026-06-11 â€” Legacy `daily_intent` post-migration audit (plan item #7)

**Author:** Claude (Cowork) Â· **For:** Dheeraj Â· **Scope:** read-only audit + cleanup
documentation. No DB rows were mutated and no source behavior was changed by this
item â€” it only adds the runnable audit script
[`scripts/migration_audit_legacy_daily_intent.py`](../scripts/migration_audit_legacy_daily_intent.py)
and this report.

Reproduce: `uv run python scripts/migration_audit_legacy_daily_intent.py`
(add `--json` for machine output). Safe during market hours (opens
`db/openalgo.db` read-only).

---

## 1. Reader sweep â€” who else reads the legacy `daily_intent` table?

The migration is "done" only when every reader of the old table is accounted for
(the lesson from Failure 1). The script greps for the legacy API
(`database.daily_intent_db` / `get_daily_intent` / `DailyIntent` /
`resolve_effective_mode`), excludes tests + the table's own module + the unified
`strategy_daily_intent` references, and classifies each hit.

| Verdict | File | Disposition |
| --- | --- | --- |
| **FIXED** | `services/preflight_service.py` | `_check_intent` / `_check_effective_mode` now source from the unified table via `resolve_strategy_mode` (item #2, this batch). The remaining `get_daily_intent` call is the documented legacy back-compat fall-through. |
| **FIXED** | `blueprints/mode_status.py` | `GET /mode/status` now sources `effective_mode` from the unified `resolve_strategy_mode('simplified_engine')` (Phase B, this batch) and exposes `source`/`intent`/`daily_capital_cap`. The legacy `daily_intent` row is still surfaced under `daily_intent` for observability/back-compat but no longer drives the effective mode. Was the last surviving legacy reader. |
| **BY-DESIGN** | `app.py` | Imports `daily_intent_db.init_db` to ensure the table exists at boot â€” schema init, not an intent read. |
| **BY-DESIGN** | `blueprints/chartink.py` | Calls `resolve_effective_mode()` to stamp the scan-cycle **audit** row only; order placement is unchanged by it (per the call-site comment). |
| **BY-DESIGN** | `services/mode_service.py` | `resolve_effective_mode` is the legacy **global** resolver (still load-bearing for `place_order_service` + the audit capture); `resolve_strategy_mode` reads legacy only as its documented fall-through. Intentionally separate â€” see `docs/design/strategy_daily_intent.md`. |
| **BY-DESIGN-INDIRECT** (~19 files) | `services/place_order_service.py`, `place_smart_order_service.py`, `split_order_service.py`, `basket_order_service.py`, `modify_order_service.py`, `cancel_order_service.py`, `cancel_all_order_service.py`, `place_gtt_order_service.py`, `modify_gtt_order_service.py`, `cancel_gtt_order_service.py`, `gtt_orderbook_service.py`, `close_position_service.py`, `openposition_service.py`, `positionbook_service.py`, `orderbook_service.py`, `orderstatus_service.py`, `tradebook_service.py`, `holdings_service.py`, `funds_service.py`, `signal_review_service.py`, `simplified_stock_engine_service.py` | These call the legacy **global** resolver `resolve_effective_mode` (which reads the table internally); none touch the `daily_intent` table directly. They are the `place_order_service` order family and deliberately stay on the legacy resolver this pass (Phase C below). |
| **REVIEW** | *(none)* | No unclassified direct-table reader remains. |

**Bottom line:** the only order-gating reader that was on the wrong table
(`preflight_service`) is **FIXED**. The observability endpoint `GET /mode/status`
is now **FIXED** too (Phase B, 2026-06-11) â€” it sources the effective mode from
`resolve_strategy_mode`. No non-test direct reader of `daily_intent` remains
outside the documented legacy fall-through. The ~19 indirect callers are the
order pipeline using the legacy global resolver, which is intentionally unchanged
this pass (Phase C).

---

## 2. Phantom-row check â€” `trade_journal`

| Metric | Value |
| --- | --- |
| RELIANCE `101.7`/`97.4` synthetic-signature rows (the pytest-pollution fingerprint) | **28 total, 0 open** |
| Open `trending_equity_intraday` rows | **1 at audit time â†’ 0 after the 06-09 ONGC reconciliation** |
| Total open (`exited_at IS NULL`) rows | **1 at audit time â†’ 0 after reconciliation** |

- **The 28 phantom RELIANCE rows are all CLOSED** (`exited_at` set by the mid-day
  remediation `UPDATE`). They are harmless to the engine's open-position rehydrate
  and remain in the DB as closed rows. Deleting them is a separate operator
  decision â€” this audit does not.
- **The one open row is NOT a phantom â€” it is a real position.**
  `id=40 ONGC SHORT qty=386 @ 258.7 order_id=26060960448274` (`trending_equity_intraday`,
  placed `2026-06-09T14:44:56+05:30`). The order id is a real 16-digit broker id
  (not a synthetic `OID-`), so this is a genuine 2026-06-09 position whose **exit
  was never journaled** â€” the same reconciliation gap the EOD-reconciliation fix
  closes going forward, but for 06-09. **It was not deleted or closed by this
  audit** (real data; the plan expected 0 phantom *test* rows, and this is not one
  â€” surfaced rather than altered).

  > âœ… **Reconciled (2026-06-11):** the open ONGC `id=40` row was closed via
  > `uv run python -m services.engine_eod_reconciliation_backfill --from 2026-06-09 --to 2026-06-09 --apply`
  > â€” sandbox covering BUY fill priced the exit at `259.0`
  > (`exit_reason='sandbox_eod_squareoff'`, gross P&L âˆ’â‚¹115.80, order
  > `26060950846436`). It no longer rehydrates as a stale position on engine boot.
  > The 2026-06-10 backfill was already applied (OIL/HINDZINC/TATAELXSI carry
  > `sandbox_eod_squareoff`); a fresh dry-run for 06-10 is now a no-op, confirming
  > idempotency.

---

## 3. Legacy `daily_intent` retirement plan

- **Phase A â€” done (2026-06-11):** point the order-gating reader (preflight) at the
  unified resolver. `place_order_service` keeps the legacy global resolver
  deliberately â€” the unified gate lives in the engines, not the shared order path.
- **Phase B â€” done (2026-06-11):** migrated `blueprints/mode_status.py` to source
  the effective mode from `resolve_strategy_mode('simplified_engine')`, exposing
  `source`/`intent`/`daily_capital_cap` while preserving the legacy response keys.
  Covered by `test/test_mode_status_endpoint.py` (unified-hit + legacy
  fall-through).
- **Phase C â€” follow-up:** once the FOLLOW-UP reader is migrated and
  `resolve_effective_mode`'s legacy dependency is the last one, decide whether to
  reimplement `resolve_effective_mode` on top of `resolve_strategy_mode` or keep it
  as the documented legacy global.
- **Drop criteria:** zero non-test direct readers of `database.daily_intent_db`
  remain (the script must report only FIXED/BY-DESIGN, no FOLLOW-UP/REVIEW), **and**
  a full `migrate_legacy_daily_intent` has run so no historical intent is lost.
  Keep the table read-only for at least one trading week after the last reader is
  removed as a rollback cushion.
