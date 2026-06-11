# Unified Strategy Daily Intent — `strategy_daily_intent`

Status: **shipped 2026-06-10** · Author: Cowork (operator-authorized full implementation)
Branch: `feat/sector-rotation-etf`

## Problem statement

OpenAlgo grew **two independent, incompatible strategy control surfaces**, and a
third global one:

1. **Simplified engine** — a legacy `daily_intent` table (`db/openalgo.db`) keyed
   by IST date with a single platform-wide intent (`live` / `sandbox` / `skip`).
   Read only by `services/mode_service.resolve_effective_mode()` and the
   `/mode/status` endpoint; the engine's *own* routing comes from the
   `SIMPLIFIED_ENGINE_MODE` env (`disabled` / `sandbox` / `live`).
2. **Sector-follow** — no persistent control at all. Only an in-memory
   `manual_pause` flag toggled by the `/sector_follow_cap5_vol/api/pause|resume`
   REST endpoints, plus the `SECTOR_FOLLOW_CAP5_VOL_MODE` env
   (`scaffold` / `sandbox` / `live`).
3. **Global** — `settings.analyze_mode` + the legacy `daily_intent` row drive
   `place_order_service` routing for *all* API/webhook orders.

The tension is **two orthogonal axes** that the legacy surfaces conflate:

* **mode** — *how* an order routes: `live` (broker), `sandbox` (sandbox.db),
  `skip` (no order).
* **intent** — *whether* the strategy should act at all today: `run` (normal),
  `pause` (no new entries; exits / MTM / EOD still run), `halt` (skip
  everything, including exits).

There was no per-strategy, persistent, two-axis surface. An operator could not
say "run sector_follow in sandbox today, but pause new entries" without editing
env + restarting, and could not express it per-strategy at all.

## The unified schema

Single additive table in `db/openalgo.db`. It does not modify any existing
table; the legacy `daily_intent` table stays readable and is migrated forward.

```sql
CREATE TABLE strategy_daily_intent (
    strategy_name      TEXT     NOT NULL,
    intent_date        DATE     NOT NULL,
    mode               TEXT     NOT NULL CHECK (mode   IN ('live','sandbox','skip')),
    intent             TEXT     NOT NULL CHECK (intent IN ('run','pause','halt')) DEFAULT 'run',
    daily_capital_cap  REAL     DEFAULT NULL,
    updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by         TEXT     NOT NULL,
    notes              TEXT     DEFAULT NULL,
    PRIMARY KEY (strategy_name, intent_date)
);
```

### Semantics

| Field | Values | Meaning |
| --- | --- | --- |
| `mode` | `live` / `sandbox` / `skip` | HOW orders route. `skip` = place no orders. |
| `intent` | `run` / `pause` / `halt` | WHETHER to act. `pause` = no new entries but exits/MTM/EOD continue; `halt` = skip everything including exits. |
| `daily_capital_cap` | float or NULL | Optional override of the strategy's default daily capital. NULL = use the strategy default. |

`mode` and `intent` are independent. `mode=sandbox, intent=pause` is valid and
means "route to sandbox, but don't open anything new today."

## Read path — `resolve_strategy_mode`

The single per-strategy read path is
`services.mode_service.resolve_strategy_mode(strategy_name, date=None)`, which
returns an `EffectiveDecision` dataclass:

```python
@dataclass(frozen=True)
class EffectiveDecision:
    mode: str               # 'live' | 'sandbox' | 'skip'
    intent: str             # 'run' | 'pause' | 'halt'
    daily_capital_cap: float | None
    source: str             # 'unified' | 'legacy' | 'env' | 'default'
```

### Naming decision (why not `resolve_effective_mode`)

The design brief called this `resolve_effective_mode(strategy_name, date)`, but
that name is already taken by the **load-bearing legacy global resolver**
`resolve_effective_mode(date_str=None) -> EffectiveMode` (an *enum*), which
`place_order_service` and `/mode/status` depend on, and whose first positional
arg is a date string. Overloading it would (a) break `mode_status.py`'s
`resolve_effective_mode(today)` call and (b) change the return type out from
under `place_order_service`'s `if mode is EffectiveMode.SKIP` checks. To keep the
shared live-order path **zero-regression**, the unified resolver is a new,
separately-named function (`resolve_strategy_mode`) returning a new dataclass
(`EffectiveDecision`). The legacy enum + function are untouched.

### Fall-through order (when `STRATEGY_DAILY_INTENT_ENABLED=true`)

1. **unified** — a row exists in `strategy_daily_intent` for
   `(strategy_name, today)` → return it. `source='unified'`.
2. **legacy** — only for `strategy_name == 'simplified_engine'`: a row exists in
   the legacy `daily_intent` table for today → map it
   (`live/sandbox/skip` → mode; `intent='run'`). `source='legacy'`.
3. **env** — the strategy's env mode:
   `SIMPLIFIED_ENGINE_MODE` (`disabled`→`skip`) for `simplified_engine`,
   `SECTOR_FOLLOW_CAP5_VOL_MODE` (`scaffold`→`skip`) for
   `sector_follow_cap5_vol`. `intent='run'`. `source='env'`.
4. **default** — `mode='sandbox', intent='run'`. `source='default'`.

When the flag is **off**, `resolve_strategy_mode` skips step 1 and starts at
legacy/env — i.e. exactly today's behavior.

**Deploy is a no-op** until the operator inserts a row: with no unified row,
every strategy falls through to its existing env/legacy behavior. The operator
opts in per strategy by inserting a row.

## Wiring diagram

```
  Operator (SQL today; Telegram bot in Phase 6)
        │  set_intent(strategy, date, mode, intent, cap, by, notes)
        ▼
  ┌─────────────────────────────┐
  │ strategy_daily_intent table │  (db/openalgo.db)
  └─────────────────────────────┘
        ▲ migrate_legacy_daily_intent() at boot (idempotent)
        │
        │  resolve_strategy_mode(name) ── fall-through ──► EffectiveDecision
        │                                  (unified→legacy→env→default)
        ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ Engines consult the resolver at job-entry (the intent GATE):  │
  │                                                                │
  │  sector_follow_service.run_entry()  intent in {pause,halt}→skip│
  │  sector_follow_service.run_exit()   intent == halt       →skip │
  │     + apply daily_capital_cap to the position selector         │
  │     + mode override (unified mode → service routing mode)      │
  │                                                                │
  │  simplified_engine._place_entry_order()  pause/halt → no order │
  │  simplified_engine._place_exit_order()   halt        → no order│
  └──────────────────────────────────────────────────────────────┘
        │ (orders that ARE placed flow on as before)
        ▼
  place_order_service / sandbox_service / broker   (UNCHANGED)
        ▲
        └── still governed by the legacy global resolve_effective_mode()
            for non-strategy API/webhook orders.
```

### Where the intent gate lives — and why NOT in `place_order_service`

The brief allowed the gate to live in the engines if entry-vs-exit can't be
cleanly distinguished at `place_order_service`. We put it in the **engines**, and
leave `place_order_service` **unchanged**, for three reasons:

1. **The simplified engine bypasses `place_order_service`'s mode resolution.**
   Its `_dispatch_order` routes by `self.mode`: sandbox → `sandbox_place_order`
   directly; only `live` calls `place_order`. A gate in `place_order_service`
   would never see sandbox simplified orders.
2. **Entry-vs-exit is only knowable in the engine.** Both engines already split
   entry (`run_entry` / `_place_entry_order`) from exit (`run_exit` /
   `_place_exit_order`). `pause` (block entries, allow exits) is trivial there
   and impossible at the shared executor.
3. **`place_order_service` is the shared executor for every order source**
   (chartink webhooks, REST API, manual, both engines' live path). Making it
   strategy-aware risks regressing unrelated flows. Its existing global
   `resolve_effective_mode()` remains the platform-wide floor.

The unified `mode` axis still controls each engine's routing: at job entry the
engine maps the resolved unified `mode` onto its native routing mode
(`skip`→`scaffold`/`disabled` = no order; `sandbox`; `live`), overriding the env
default only when `source == 'unified'`.

## Migration

`migrate_legacy_daily_intent()` runs once at boot, after both tables exist. For
every row in the legacy `daily_intent` table it upserts a
`strategy_daily_intent` row with `strategy_name='simplified_engine'`,
`mode=<legacy intent>`, `intent='run'`, `updated_by='migration'`. Idempotent: an
existing `(simplified_engine, date)` row is left untouched (the operator's
explicit edits win over the migration backfill).

## Back-compat strategy

* Flag default **true** (ships hot), but with no rows the resolver falls through
  to env/legacy — identical to today.
* Legacy `resolve_effective_mode()` enum + `daily_intent` table untouched;
  `place_order_service` and `/mode/status` unaffected.
* `SECTOR_FOLLOW_CAP5_VOL_MODE=sandbox` and `SIMPLIFIED_ENGINE_MODE=live` (the
  live `.env` values) remain the source of truth until rows are inserted.
* Tomorrow's 2026-06-11 15:20 IST sandbox sector_follow fire: no unified row →
  `source='env'` → `mode='sandbox', intent='run'` → unchanged behavior.

## Feature flag + rollout

`STRATEGY_DAILY_INTENT_ENABLED` (env, default `true`). Set `false` to disable the
unified read path entirely (pure legacy behavior) if anything misbehaves.

Rollout: deploy hot (no-op), then the operator opts a strategy in by inserting a
row via SQL (or the future Telegram bot). To roll back a single strategy: delete
its row → instant fall-through to env.

## Test plan

* DB layer: init idempotency, `get_intent` None when no row, set+get roundtrip,
  list, migration backfill + idempotency.
* Resolver: unified hit, legacy fall-through (simplified only), env fall-through,
  default, flag-off path.
* sector_follow: `intent=pause` → `run_entry` places nothing, `run_exit` still
  exits; `intent=halt` → both return immediately; `daily_capital_cap` override
  applied; no-row back-compat matches env.
* simplified: `intent=pause` → no new arms; `intent=halt` → no scan/arm; no-row
  back-compat uses env path.

## Risk register

| Risk | Mitigation |
| --- | --- |
| Regress tomorrow's sandbox sector_follow fire | No unified row → env fall-through → unchanged. Verified in smoke test. |
| Break the shared `place_order_service` path | Left fully unchanged; gate lives in engines. |
| Migration corrupts data on double-run | Idempotent upsert; existing rows skipped. |
| Resolver raises and kills a job | All resolver calls in engines are wrapped fail-open to the env default. |
| Flag misread → unexpected routing | Flag parsed with the same `str2bool` helper used elsewhere; default true documented in PARAMETER_LOG. |
| Enum/dataclass name confusion | New name `resolve_strategy_mode`/`EffectiveDecision`; legacy untouched. |

## Future work (Phase 6)

* Telegram inbound commands (`/intent sector_follow sandbox pause`) write to this
  table via `set_intent`, giving the operator a phone-based pre-market control.
* A `/strategy_intent` status endpoint listing today's rows for every strategy.
* Migrate the `/sector_follow_cap5_vol/api/pause|resume` endpoints to write
  `intent` rows (kept as runtime emergency overrides for now).
