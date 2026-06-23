# Tier 3 — Parameter Overrides for Screener Rules: Implementation Plan

**Date:** 2026-06-23
**Parent issue:** #86 (feat(ui): screener UX completion — create + customise screeners)
**Scope:** Tier 3 only — parameterise existing code-backed rules and provide a
"Clone with custom parameters" UI. Tier 4 (full DSL / visual filter builder) is
out of scope here.

---

## Current state (as-read 2026-06-23)

### `scan_definitions` schema (`database/scanner_db.py`)

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `name` | String(128) UNIQUE | |
| `screener_type` | String(8) | `'buy'` or `'sell'` |
| `expression_json` | Text NOT NULL | `'{}'` for code-backed rules |
| `rule_module` | String(256) nullable | points at a registered `@scan_rule` name |
| `enabled` | Integer | 0/1 |
| `created_at` | String(40) | ISO IST |
| `updated_at` | String(40) | ISO IST |

**Missing:** no `parameters_json` column, no `parent_definition_id` FK.

### Rule parameterisation today

Both `fno_intraday_buy_chartink` and `fno_intraday_sell_chartink` already read
**two** parameters at call time from env vars:
- `CHARTINK_RULE_BUY_GAP_PCT` (default `3.0`) — gate 1 gap-up threshold
- `CHARTINK_RULE_SELL_GAP_PCT` (default `3.0`) — gate 1 gap-down threshold

All other constants are **hardcoded inline** (volume SMA periods, ATR threshold,
5m-volume multiplier, Supertrend params, RSI threshold, price range). They
cannot be varied per definition row today.

### How rules are called (`services/scanner_service.py`)

`ScannerService._evaluate_definitions` iterates `get_scan_definitions(enabled_only=True)`,
looks up `rule_fn = get_rule(rule_name)`, and calls:
```python
matched = bool(rule_fn(bars, indicators_dict))
```
where `indicators_dict` is the **same shared dict** for all definitions on a given
symbol/bar tick. Parameter injection for Tier 3 needs a per-definition shallow
copy of that dict with a `"parameters"` key added.

### Existing API surface (`blueprints/scanner_api.py`)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/scanner/api/definitions` | GET | List all definitions |
| `/scanner/api/definitions/<id>/signals` | GET | Signal history |
| `/scanner/api/definitions/<id>/toggle` | POST | Flip enabled |
| `/scanner/api/hits-by-symbol` | GET | Aggregate by symbol |

No read-single, clone, update-params, or delete endpoints yet.

### Existing frontend (`frontend/src/pages/scanner/`)

- `ScannerIndex.tsx` — list of definitions, toggle, hits-by-symbol tab
- `ScannerDetail.tsx` — signal history, hit-density chart, date-range picker

Parameters card on `ScannerDetail` currently shows only `screener_type`,
`status`, `rule_module`, and `updated_at`. No param values displayed, no edit flow.

---

## Parameterisable constants per rule

### `fno_intraday_buy_chartink` (BUY — 12 gates)

| Key | Default | Env fallback | Description |
|-----|---------|--------------|-------------|
| `gap_pct` | `3.0` | `CHARTINK_RULE_BUY_GAP_PCT` | % gap-up in gate 1 |
| `atr_pct` | `5.0` | — | weekly ATR > atr_pct% × close (gate 7) |
| `vol_5m_mult` | `2.0` | — | 5m vol > mult × SMA(5m vol, 10) (gate 13) |
| `rsi_threshold` | `50.0` | — | RSI(14) must exceed this (gate 5) |
| `supertrend_period` | `7` | — | Supertrend ATR period (gates 3+4) |
| `supertrend_mult` | `3.0` | — | Supertrend multiplier (gates 3+4) |
| `price_min` | `100.0` | — | Min daily close (gate 6) |
| `price_max` | `5000.0` | — | Max daily close (gate 12) |
| `vol_sma_short` | `50` | — | Daily vol SMA period, gate 2 |
| `vol_sma_long` | `200` | — | Daily vol SMA period, gate 8 |

### `fno_intraday_sell_chartink` (SELL — 10 gates)

| Key | Default | Env fallback | Description |
|-----|---------|--------------|-------------|
| `gap_pct` | `3.0` | `CHARTINK_RULE_SELL_GAP_PCT` | % gap-down in gate 1 |
| `atr_pct` | `5.0` | — | weekly ATR > atr_pct% × close (gate 7) |
| `rsi_threshold` | `50.0` | — | RSI(14) must be below this (gate 5) |
| `supertrend_period` | `7` | — | Supertrend ATR period (gates 3+4) |
| `supertrend_mult` | `3.0` | — | Supertrend multiplier (gates 3+4) |
| `price_min` | `100.0` | — | Min daily close (gate 6) |
| `price_max` | `5000.0` | — | Max daily close (gate 12) |

---

## Chunk breakdown

Five independently-mergeable PRs. Each is tagged S/M/L (≈4h/6h/8h effort).

---

### Chunk A — Backend: `scan_definitions` schema migration

**Effort:** S (~4h)
**Blocks:** B, C (both need the new columns)

#### Scope

File: `database/scanner_db.py`

1. Add two columns to the `ScanDefinition` SQLAlchemy model:
   - `parameters_json = Column(Text, nullable=True)` — JSON dict of override values; `NULL` = use rule defaults
   - `parent_definition_id = Column(Integer, nullable=True)` — FK reference (unenforced at SQLite layer) to the `scan_definitions` row this was cloned from; `NULL` = code-backed / built-in
2. `init_db()`: run idempotent `ALTER TABLE scan_definitions ADD COLUMN …` for each new column. SQLite does not support `ADD COLUMN IF NOT EXISTS`, so catch `OperationalError` with message containing `"duplicate column name"`. Run inside a `with engine.connect() as conn` block.
3. Update `_definition_to_dict` to include both new fields in the returned dict.
4. Update `create_scan_definition(...)` to accept optional `parameters_json: str | dict | None = None` and `parent_definition_id: int | None = None` kwargs.

#### Acceptance criteria

- Fresh `init_db()` creates the table with both columns.
- Calling `init_db()` on a DB that already has the columns does not error.
- `_definition_to_dict` output always includes `parameters_json` (str or None) and `parent_definition_id` (int or None).
- `create_scan_definition` with no new kwargs stores `NULL` for both — backwards-compatible.
- `create_scan_definition(parameters_json={"gap_pct": 1.5}, parent_definition_id=1)` round-trips correctly.

#### Test plan (`test/test_scanner_db.py`)

- `test_schema_has_new_columns` — SQLAlchemy `inspect(engine).get_columns` confirms both columns.
- `test_create_definition_without_params` — round-trip via `create_scan_definition` + `get_scan_definitions`, fields present and None.
- `test_create_definition_with_params` — dict and str both serialise/round-trip.
- `test_init_db_idempotent_with_existing_columns` — call `init_db()` twice on a DB that already has the columns, no error.

#### Risk

Low. `ALTER TABLE` on SQLite is append-only and cannot break existing rows. The try/except guard is idempotent. No rule behavior changes.

---

### Chunk B — Backend: Clone, update-params, delete, and get-single API

**Effort:** M (~6h)
**Requires:** A merged
**Blocks:** D (frontend needs these endpoints)

#### Scope

Files: `blueprints/scanner_api.py`, `database/scanner_db.py` (add helper functions)

New DB helpers in `scanner_db.py`:
- `clone_definition(source_id, new_name, parameters_json) -> int` — creates a child row with same `rule_module` + `screener_type`, sets `parent_definition_id = source_id`. Raises `ValueError` if source does not exist; raises `IntegrityError` on duplicate name.
- `update_definition_params(definition_id, parameters_json) -> None` — update `parameters_json` + `updated_at`; raises `ValueError` if the row has `parent_definition_id IS NULL` (code-backed rows are immutable by policy).
- `delete_definition(definition_id) -> None` — hard delete; raises `ValueError` if `parent_definition_id IS NULL` (code-backed rows cannot be deleted via the UI).

New API endpoints in `scanner_api.py`:

| Endpoint | Method | Response | Notes |
|----------|--------|----------|-------|
| `/scanner/api/definitions/<id>` | GET | Full definition dict | For clone form pre-fill |
| `/scanner/api/definitions/<id>/clone` | POST | `{id, name}` 201 | Body: `{name, parameters_json?}` |
| `/scanner/api/definitions/<id>/params` | PUT | `{id, parameters_json}` | Clone-only; 403 if code-backed |
| `/scanner/api/definitions/<id>` | DELETE | `{id}` 200 | Clone-only; 403 if code-backed |

All endpoints follow the existing auth pattern (`check_session_validity` decorator + `session.get("user")` guard).

The `DELETE /scanner/api/definitions/<id>` endpoint disallows deleting a definition that has children (where other rows have `parent_definition_id = <id>`) — return `409 Conflict` in that case. This prevents orphan signals pointing at a deleted parent.

#### Acceptance criteria

- `GET /scanner/api/definitions/1` returns the full dict including new fields.
- `POST .../1/clone` with `{name: "my-custom-buy", parameters_json: {"gap_pct": 1.5}}` creates a new row with `parent_definition_id = 1`, returns 201.
- `PUT .../2/params` on a cloned row updates `parameters_json`, returns 200.
- `PUT .../1/params` on a code-backed row returns 403.
- `DELETE .../2` on a cloned row removes it, returns 200.
- `DELETE .../1` on a code-backed row returns 403.
- `DELETE .../1` when row 2 has `parent_definition_id = 1` returns 409.
- All endpoints return 401 when unauthenticated.

#### Test plan (`test/test_scanner_api.py`)

- `test_get_single_definition` — 200 with correct shape.
- `test_clone_definition` — 201; new row has correct `parent_definition_id`.
- `test_clone_duplicate_name` — 409 (or 400) on duplicate name.
- `test_update_params_on_clone` — 200 round-trip.
- `test_update_params_on_code_backed_returns_403`.
- `test_delete_clone` — 200; row gone from list.
- `test_delete_code_backed_returns_403`.
- `test_delete_with_children_returns_409`.
- `test_all_endpoints_require_auth` — 401 when no session.

#### Risk

Low–medium. New endpoints; does not touch existing routes. The 403 guard on
code-backed rows is the critical safety rail — tested explicitly.

---

### Chunk C — Backend: Rule parameterisation (scanner service + rules)

**Effort:** M (~6h)
**Requires:** A merged (needs `parameters_json` in definition dicts)
**Can be developed in parallel with B**

#### Scope

Files: `services/scanner_service.py`, `services/scan_rules/fno_intraday_buy_chartink.py`, `services/scan_rules/fno_intraday_sell_chartink.py`

**`scanner_service.py` — `_evaluate_definitions`:**
After the `rule_fn = get_rule(rule_name)` lookup, before calling the rule,
inject the definition's `parameters_json` into a shallow copy of `indicators_dict`:

```python
raw_params = definition.get("parameters_json")
if raw_params:
    try:
        params = json.loads(raw_params) if isinstance(raw_params, str) else raw_params
    except (ValueError, TypeError):
        params = {}
else:
    params = {}
eff_indicators = {**indicators_dict, "parameters": params} if params else indicators_dict
matched = bool(rule_fn(bars, eff_indicators))
```

This is a no-mutation, no-overhead path: `eff_indicators == indicators_dict` (same object) when `params` is empty.

**`fno_intraday_buy_chartink.py` — `_evaluate`:**
Replace all hardcoded constants with reads from `indicators.get("parameters", {})`,
with env-var fallback, then hardcoded default. Example pattern:

```python
p = indicators.get("parameters", {})
gap_pct     = float(p.get("gap_pct",     os.environ.get("CHARTINK_RULE_BUY_GAP_PCT", "3.0")))
atr_pct     = float(p.get("atr_pct",     "5.0")) / 100.0
vol_5m_mult = float(p.get("vol_5m_mult", "2.0"))
rsi_thresh  = float(p.get("rsi_threshold", "50.0"))
st_period   = int(p.get("supertrend_period", "7"))
st_mult     = float(p.get("supertrend_mult", "3.0"))
price_min   = float(p.get("price_min", "100.0"))
price_max   = float(p.get("price_max", "5000.0"))
vol_sma_s   = int(p.get("vol_sma_short", "50"))
vol_sma_l   = int(p.get("vol_sma_long", "200"))
```

These replace the 10 hardcoded literals in the evaluate function body. The warm-up
guard for `len(bars_daily) < 200` also becomes `len(bars_daily) < vol_sma_l`.

**`fno_intraday_sell_chartink.py` — `_evaluate`:**
Same pattern for its 7 tunables (no vol_sma_short/long — SELL's volume gate is simpler).

#### Acceptance criteria

- A definition row with `parameters_json = '{"gap_pct": 1.5}'` and `rule_module = "fno_intraday_buy_chartink"` evaluates with a 1.5% gap threshold instead of 3.0%.
- A definition row with `parameters_json = NULL` or `'{}'` behaves identically to before (defaults unchanged).
- Env vars `CHARTINK_RULE_BUY_GAP_PCT` / `CHARTINK_RULE_SELL_GAP_PCT` still work as second-level fallback when no `parameters_json` override is set.
- No behaviour change to the **existing** code-backed definition (its `parameters_json` is NULL).

#### Test plan

`test/test_scanner_service.py` (extend existing):
- `test_evaluate_definitions_injects_params` — mock definition dict with `parameters_json = '{"gap_pct": 1.5}'`; confirm rule is called with `indicators["parameters"] == {"gap_pct": 1.5}`.
- `test_evaluate_definitions_no_params_no_copy` — confirm `eff_indicators is indicators_dict` when `parameters_json` is NULL (same object reference).

New `test/test_scan_rules_parameterised.py` (unit tests on `_evaluate` directly):
- `test_buy_rule_custom_gap_pct` — frame with 1.6% gap passes with `gap_pct=1.5`, fails with `gap_pct=2.0`.
- `test_buy_rule_custom_vol_5m_mult` — frame with 1.8× 5m vol passes with `vol_5m_mult=1.5`, fails with `vol_5m_mult=2.0`.
- `test_buy_rule_custom_supertrend_params` — different period/mult produces different Supertrend line; confirm gate 3/4 changes.
- `test_sell_rule_custom_gap_pct` — analogous for SELL.
- `test_default_fallback_behaviour` — `parameters={}` gives same result as no-parameters baseline.

#### Risk

Medium. This is the core behaviour change. The guard `if params else indicators_dict`
ensures zero overhead and zero mutation for the base (code-backed) definitions.
The three-tier fallback (params dict → env var → hardcoded) means existing
deployments see no change. The new test file exercises the critical "params override
the constant" path explicitly.

One subtlety: `vol_sma_long` controls the warm-up guard too (`len(bars_daily) < vol_sma_l`).
If someone sets `vol_sma_long=500`, warm-up is slower. Document in the frontend
form's range hint.

---

### Chunk D — Frontend: Clone dialog + parameter form

**Effort:** L (~8h)
**Requires:** B merged (clone, update-params, get-single endpoints)
**Note:** C does not need to be merged for D to work — the UI can be developed
and demoed with the API before the rule parameterisation lands.

#### Scope

Files:
- `frontend/src/api/scanner.ts` — new API methods + updated types
- `frontend/src/pages/scanner/ScannerIndex.tsx` — "Clone" button
- `frontend/src/pages/scanner/ScannerDetail.tsx` — param display + "Edit" button
- `frontend/src/pages/scanner/CloneDefinitionDialog.tsx` (new)
- `frontend/src/pages/scanner/ParamForm.tsx` (new)

**`scanner.ts` type updates:**

```typescript
export interface ScanDefinitionSummary {
  // … existing fields …
  parameters_json: string | null     // added
  parent_definition_id: number | null // added
}

export interface ScanDefinitionDetail {
  // … existing fields …
  parameters_json: string | null
  parent_definition_id: number | null
}
```

New API methods:
```typescript
scannerApi.getDefinition(id)             // GET /scanner/api/definitions/<id>
scannerApi.cloneDefinition(id, {name, parameters_json?})  // POST …/clone
scannerApi.updateParams(id, parameters_json)              // PUT …/params
scannerApi.deleteDefinition(id)          // DELETE /scanner/api/definitions/<id>
```

**`ParamForm.tsx`** — shared controlled component:
- Static param schema per `rule_module` (hardcoded map; only two rule modules exist today)
- Renders number inputs with label, step, min, max, and a "reset to default" link per field
- Validates: all fields numeric, `price_min < price_max`, `vol_sma_short < vol_sma_long`
- Props: `ruleModule: string`, `value: Record<string, number>`, `onChange`

**`CloneDefinitionDialog.tsx`**:
- shadcn `Dialog`, triggered by "Clone" button on `ScannerIndex` rows (code-backed rows only, i.e. `parent_definition_id === null`)
- Fields: name input + `ParamForm` for the source rule's params pre-filled with defaults
- On submit: calls `cloneDefinition`; on success closes dialog + invalidates `scanner-definitions` query

**`ScannerIndex.tsx`** changes:
- "Clone" icon button (Copy icon from lucide-react) on each row where `parent_definition_id === null && rule_module !== null`
- Cloned rows show a "custom" badge alongside existing badges
- Cloned rows show the "Clone" button targeting themselves (to create a second variant)

**`ScannerDetail.tsx`** changes:
- Parse `parameters_json` and display current override values in the Parameters card — only show params that differ from defaults; show "using defaults" when `parameters_json` is null/empty
- "Edit parameters" button (only when `parent_definition_id !== null`): opens a Dialog with `ParamForm` pre-filled from current `parameters_json`; on save calls `updateParams`

#### Acceptance criteria

- Code-backed definition rows in `ScannerIndex` show a "Clone" button.
- Clicking "Clone" opens a dialog where the user names the new definition and optionally adjusts params.
- Saving creates a new row visible in the list with "custom" badge.
- Navigating to a custom definition's detail page shows the override params.
- "Edit parameters" button opens a form; saving updates the row.
- No UI change for code-backed rows (no Clone button on already-custom rows is acceptable — they can still clone from the detail page if needed).

#### Test plan

Vitest unit tests:
- `ParamForm.test.tsx` — renders correct fields per `ruleModule`; validation errors shown; reset-to-default restores value.

Manual integration smoke (before marking the PR ready):
1. Clone `fno-intraday-buy-chartink` with `gap_pct = 1.5`; confirm it appears in list.
2. Navigate to the clone's detail page; confirm `gap_pct: 1.5` displayed.
3. Edit params to `gap_pct = 2.0`; confirm update persists after page reload.
4. Code-backed row has no "Edit parameters" button.

#### Risk

Medium. UI work is the most open-ended chunk; param form requires a static map of
known params per rule_module. This map is in the frontend — when a new rule module
is added in the backend, the frontend static map must also be updated. Document this
in a comment in `ParamForm.tsx`.

---

### Chunk E — Frontend: Delete + list polish

**Effort:** S (~3h)
**Requires:** B (delete endpoint), D (renders the cloned rows)

#### Scope

Files: `frontend/src/pages/scanner/ScannerIndex.tsx`, `frontend/src/pages/scanner/ScannerDetail.tsx`

**`ScannerIndex.tsx`:**
- Trash icon button (`Trash2` from lucide-react) on rows where `parent_definition_id !== null`
- On click: shadcn `AlertDialog` confirm: "Delete 'my-custom-buy'? This cannot be undone."
- On confirm: calls `deleteDefinition(id)`, invalidates `scanner-definitions` query
- "Clone" button column now exists; "Delete" button in the same column (different rows)

**`ScannerDetail.tsx`:**
- Footer section: "Delete this definition" button (only when `parent_definition_id !== null`)
- Same `AlertDialog` confirm flow; on success navigates `router.navigate('/scanner')`

#### Acceptance criteria

- Trash icon visible on custom rows only in `ScannerIndex`.
- Confirmation dialog prevents accidental deletes.
- After delete, row disappears without page reload.
- `ScannerDetail` of a custom definition has a delete button; code-backed detail does not.

#### Test plan

No new Vitest tests (delete path is already covered by Chunk B's API tests). Manual
smoke:
1. Delete a custom definition from the list.
2. Delete a custom definition from its detail page; confirm redirect to `/scanner`.
3. Verify no delete affordance appears on code-backed rows.

#### Risk

Low. Purely additive UI. The backend 403 guard is the safety net if a frontend
bug tries to delete a code-backed row.

---

## Dependency graph

```
A (schema)
├── B (API endpoints)  ──┐
│                        ├── D (frontend clone+edit) ─── E (frontend delete+polish)
└── C (rule params)  ────┘
```

**Recommended start order:**
1. **A** — merge first (unlocks B and C in parallel)
2. **B** and **C** in parallel (independent)
3. **D** after B merges (C does not need to land for D to be usable)
4. **E** after D merges

B and C can be developed concurrently if two sessions are available; B is a
pre-requisite for D but C is not (the frontend can be demoed/reviewed against
the API before the rule actually changes behaviour).

---

## Total effort estimate

| Chunk | Effort | Hours |
|-------|--------|-------|
| A — Schema migration | S | ~4h |
| B — API endpoints | M | ~6h |
| C — Rule parameterisation | M | ~6h |
| D — Frontend clone + edit | L | ~8h |
| E — Frontend delete + polish | S | ~3h |
| **Total** | | **~27h (~3.4 days)** |

Range: **3–4 days** of focused work. Fits within the #86 estimate of 3–5 days.

---

## Out of scope (Tier 3 only)

- Tier 4: visual filter builder / DSL — tracked in #86 as a separate sub-task
- Scheduling / per-definition scan frequency — a future enhancement
- Importing/exporting parameter sets as JSON files
- Per-user definitions (single-user deployment; no multi-user concern)
- Operator-approval flow before a custom definition goes live (toggle is sufficient)
