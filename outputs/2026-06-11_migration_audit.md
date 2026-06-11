# 2026-06-11 — Legacy `daily_intent` post-migration audit (plan item #7)

**Author:** Claude (Cowork) · **For:** Dheeraj · **Scope:** read-only audit + cleanup
documentation. No DB rows were mutated and no source behavior was changed by this
item — it only adds the runnable audit script
[`scripts/migration_audit_legacy_daily_intent.py`](../scripts/migration_audit_legacy_daily_intent.py)
and this report.

Reproduce: `uv run python scripts/migration_audit_legacy_daily_intent.py`
(add `--json` for machine output). Safe during market hours (opens
`db/openalgo.db` read-only).

---

## 1. Reader sweep — who else reads the legacy `daily_intent` table?

The migration is "done" only when every reader of the old table is accounted for
(the lesson from Failure 1). The script greps for the legacy API
(`database.daily_intent_db` / `get_daily_intent` / `DailyIntent` /
`resolve_effective_mode`), excludes tests + the table's own module + the unified
`strategy_daily_intent` references, and classifies each hit.

| Verdict | File | Disposition |
| --- | --- | --- |
| **FIXED** | `services/preflight_service.py` | `_check_intent` / `_check_effective_mode` now source from the unified table via `resolve_strategy_mode` (item #2, this batch). The remaining `get_daily_intent` call is the documented legacy back-compat fall-through. |
| **FOLLOW-UP** | `blueprints/mode_status.py` | `GET /mode/status` reads legacy `get_daily_intent` + `resolve_effective_mode` directly and does **not** surface the unified `strategy_daily_intent` table — on a unified-only day it shows `daily_intent: null` and a stale `effective_mode`. **Observability only, no order-gating → not a trading risk**, but it should also consult `resolve_strategy_mode` / `list_intents`. The one genuine open follow-up. |
| **BY-DESIGN** | `app.py` | Imports `daily_intent_db.init_db` to ensure the table exists at boot — schema init, not an intent read. |
| **BY-DESIGN** | `blueprints/chartink.py` | Calls `resolve_effective_mode()` to stamp the scan-cycle **audit** row only; order placement is unchanged by it (per the call-site comment). |
| **BY-DESIGN** | `services/mode_service.py` | `resolve_effective_mode` is the legacy **global** resolver (still load-bearing for `place_order_service` + the audit capture); `resolve_strategy_mode` reads legacy only as its documented fall-through. Intentionally separate — see `docs/design/strategy_daily_intent.md`. |
| **BY-DESIGN-INDIRECT** (~19 files) | `services/place_order_service.py`, `place_smart_order_service.py`, `split_order_service.py`, `basket_order_service.py`, `modify_order_service.py`, `cancel_order_service.py`, `cancel_all_order_service.py`, `place_gtt_order_service.py`, `modify_gtt_order_service.py`, `cancel_gtt_order_service.py`, `gtt_orderbook_service.py`, `close_position_service.py`, `openposition_service.py`, `positionbook_service.py`, `orderbook_service.py`, `orderstatus_service.py`, `tradebook_service.py`, `holdings_service.py`, `funds_service.py`, `signal_review_service.py`, `simplified_stock_engine_service.py` | These call the legacy **global** resolver `resolve_effective_mode` (which reads the table internally); none touch the `daily_intent` table directly. They are the `place_order_service` order family and deliberately stay on the legacy resolver this pass (Phase C below). |
| **REVIEW** | *(none)* | No unclassified direct-table reader remains. |

**Bottom line:** the only order-gating reader that was on the wrong table
(`preflight_service`) is **FIXED**. The single remaining migration debt is the
observability endpoint `GET /mode/status` (**FOLLOW-UP**, no trading impact). The
~19 indirect callers are the order pipeline using the legacy global resolver,
which is intentionally unchanged this pass.

---

## 2. Phantom-row check — `trade_journal`

| Metric | Value |
| --- | --- |
| RELIANCE `101.7`/`97.4` synthetic-signature rows (the pytest-pollution fingerprint) | **28 total, 0 open** |
| Open `trending_equity_intraday` rows | **1** |
| Total open (`exited_at IS NULL`) rows | **1** |

- **The 28 phantom RELIANCE rows are all CLOSED** (`exited_at` set by the mid-day
  remediation `UPDATE`). They are harmless to the engine's open-position rehydrate
  and remain in the DB as closed rows. Deleting them is a separate operator
  decision — this audit does not.
- **The one open row is NOT a phantom — it is a real position.**
  `id=40 ONGC SHORT qty=386 @ 258.7 order_id=26060960448274` (`trending_equity_intraday`,
  placed `2026-06-09T14:44:56+05:30`). The order id is a real 16-digit broker id
  (not a synthetic `OID-`), so this is a genuine 2026-06-09 position whose **exit
  was never journaled** — the same reconciliation gap the EOD-reconciliation fix
  closes going forward, but for 06-09. **It was not deleted or closed by this
  audit** (real data; the plan expected 0 phantom *test* rows, and this is not one
  — surfaced rather than altered).

  > ⚠️ **Restart implication (Phase 2):** this open row is rehydrated as a position
  > on the next engine boot. Confirm the sandbox/broker position is actually flat
  > and reconcile it via
  > `uv run python -m services.engine_eod_reconciliation_backfill --date 2026-06-09 --apply`
  > (operator decision) before/after restart so the engine doesn't manage a stale
  > 06-09 position. (Separately, the 2026-06-10 backfill noted in memory still
  > needs `--apply`.)

---

## 3. Legacy `daily_intent` retirement plan

- **Phase A — done (2026-06-11):** point the order-gating reader (preflight) at the
  unified resolver. `place_order_service` keeps the legacy global resolver
  deliberately — the unified gate lives in the engines, not the shared order path.
- **Phase B — follow-up:** migrate `blueprints/mode_status.py` to also surface
  `strategy_daily_intent` (via `resolve_strategy_mode` / `list_intents`) so every
  observability/decision surface reflects the unified table.
- **Phase C — follow-up:** once the FOLLOW-UP reader is migrated and
  `resolve_effective_mode`'s legacy dependency is the last one, decide whether to
  reimplement `resolve_effective_mode` on top of `resolve_strategy_mode` or keep it
  as the documented legacy global.
- **Drop criteria:** zero non-test direct readers of `database.daily_intent_db`
  remain (the script must report only FIXED/BY-DESIGN, no FOLLOW-UP/REVIEW), **and**
  a full `migrate_legacy_daily_intent` has run so no historical intent is lost.
  Keep the table read-only for at least one trading week after the last reader is
  removed as a rollback cushion.
