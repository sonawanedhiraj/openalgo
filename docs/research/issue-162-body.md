This issue exists because of feedback on #156's plan: *"I should be able to flip sandbox→live by just clicking a button; do not put complex 'wait for X to ship and Y acceptance metric clears' in operator's head."*

The feedback is correct. Operator memory is the wrong place for safety. The system must enforce all pre-conditions.

## Today's failure (the case this fixes)

```
15:18:00  sector_follow 15:18 smoke check PASSED: aggregator_coverage='30/30', historify_ok=True
15:20:00  sector_follow mode override sandbox -> live (strategy_mode row)
15:20:01  [all 8 indices: today_close=None]
15:20:01  0/30 candidates passed gates → 0 order(s) [mode=live]
```

The smoke check only verified the STOCK aggregator. The operator flipped LIVE via raw SQL UPDATE on `strategy_mode` (no UI, no pre-flight check). The strategy emitted 0 orders silently.

**If the operator had a UI toggle gated on a real pre-flight check, the toggle would have refused the flip with:**

> Cannot enable LIVE for sector_follow_cap5_vol:
>   - Index aggregator empty for 8/8 sector indices (NIFTYAUTO, NIFTYFMCG, NIFTYIT, NIFTYMETAL, NIFTYPSUBANK, NIFTYPVTBANK, NIFTY, BANKNIFTY)
>   - Reason: NSE_INDEX symbols are not in the scanner aggregator subscription set (issue #161 pending)
>   - Current mode (sandbox) is unchanged.

The flip would not have happened, and the operator would have an actionable error.

## Current state (read-only dashboard)

`blueprints/strategies_dashboard_api.py` exposes only GET endpoints. Docstring is explicit: **"READ-ONLY. These endpoints never write to any database."** Mode flipping today requires SSH + SQL.

## What this issue ships

### 1. UI toggle on the /strategies page

Per strategy card, a toggle: `[ ] sandbox  ( ) live`. Toggling fires:

`POST /strategies/api/<name>/mode` with `{ "mode": "live" }` or `{ "mode": "sandbox" }`

### 2. Pre-flight checks enforced server-side

Each strategy declares its own gates in `strategies/<name>/preflight.py:check_can_go_live(target_mode)`:

```python
def check_can_go_live(target_mode: str) -> PreflightResult:
    blockers = []
    if target_mode != "live":
        return PreflightResult(can_flip=True, blockers=[], warnings=[], snapshot={})

    # 1. Index aggregator coverage (THIS is what today's flip needed)
    missing = [idx for idx in SECTOR_INDICES if not aggregator.has_today_bars(idx)]
    if missing:
        blockers.append(f"Index aggregator empty for {len(missing)}/8: {missing}")

    # 2. Stock aggregator coverage
    if aggregator.coverage(LOCK_STATIC_30) < 0.9:
        blockers.append(f"Stock aggregator coverage <90%")

    # 3. Broker session live
    if not is_live_broker_session():
        blockers.append("Broker session not live — re-login required")

    # 4. No orphan trades (covers issue #157)
    if count_orphan_trades(strategy="sector_follow_cap5_vol") > 0:
        blockers.append("Orphan trade(s) in journal — reconcile first")

    # 5. Recent error noise — last hour
    if count_recent_errors(regex=r"different configuration|Failed to connect", since_min=60) > 5:
        blockers.append("DuckDB lock errors in last 60 min — system unstable")

    # 6. Master contract ready
    if not master_contract_ready("zerodha"):
        blockers.append("Zerodha master contract still downloading")

    return PreflightResult(
        can_flip=(len(blockers) == 0),
        blockers=blockers,
        warnings=[],
        snapshot={...},
    )
```

**On success**, the handler writes the new `strategy_mode` row, records an audit entry, emits an in-process event so the strategy picks up the new mode immediately, and Telegram-notifies the operator.

**On block**, returns `{"status": "blocked", "blockers": [...]}`. UI displays the blockers list with a "Try Again" button. Telegram notifies the refusal. Audit row written with `accepted=False`.

### 3. Strategy preflight modules

- `strategies/sector_follow_cap5_vol/preflight.py` (this PR — the gates listed above)
- `strategies/futures_follow_cap50/preflight.py` (reuses sector_follow's gates — same evaluator)
- `strategies/simplified_engine/preflight.py` (checks Chartink webhook reachable + no orphans + broker live)
- Default preflight in `services/strategy_preflight.py` for any strategy without a custom one (broker session + no orphans + low recent errors)

### 4. New audit table `strategy_mode_audit`

| column | type |
|---|---|
| id | PRIMARY KEY |
| strategy_name | TEXT |
| target_mode | TEXT |
| previous_mode | TEXT |
| accepted | BOOLEAN |
| blockers_json | TEXT |
| warnings_json | TEXT |
| snapshot_json | TEXT |
| flipped_at | TIMESTAMP |
| flipped_by | TEXT (session user) |

Every flip attempt — successful or blocked — is recorded.

### 5. Hide raw SQL path

`database/strategy_mode_db.set_mode` becomes module-private `_set_mode_unchecked`. The only public path is `services/strategy_mode_service.flip_mode(strategy_name, target_mode)` which always runs the preflight. Direct SQL UPDATE still works (SQLite) but the dashboard + API enforce the gates.

## Tests

- `test/test_strategy_preflight_blocks_on_missing_indices.py`: sector_follow blocker when index aggregator empty
- `test/test_strategy_preflight_blocks_on_orphan_trades.py`: blocker when journal has orphans
- `test/test_strategy_mode_flip_audit_row.py`: audit row on accept AND block
- `test/test_strategy_mode_flip_emits_event.py`: successful flip publishes event picked up by strategy
- E2E (Playwright): UI toggle click → POST → server-side block → UI shows blockers

## Acceptance

- Operator can flip sandbox↔live by clicking a toggle on /strategies
- ANY missing pre-condition blocks the flip with a clear human-readable message
- Audit table records every attempt
- Telegram notification fires on every flip (accepted or blocked)
- Today's exact scenario (sector_follow LIVE with empty index aggregator) is refused with the specific blocker text

## Replaces the operator-memory anti-pattern in #156

The original consolidated plan said *"flip to LIVE only after R2 ships and its 24h acceptance metric clears"*. That's unworkable — nobody can remember a checklist that spans days and 6 PRs.

With this issue's preflight pattern, **the system itself refuses the flip** when conditions aren't met. Each future fix PR (R1 DuckDB singleton, R2 indices in aggregator, R4 orphan reconciliation) adds its check to the preflight module. The check IS the gate.

When all checks pass, the operator clicks the toggle and the flip succeeds — automatically, with no memory required.

## Note on simplified_engine

Same toggle works for the simplified engine. Its preflight differs:
- Chartink webhook received a hit in last N minutes (or webhook configured + reachable)
- No orphan trades
- Broker session live

The flip toggle simply changes mode (`sandbox` routes orders to sandbox.db, `live` routes to broker). Webhook URL configuration is out of scope.

## Order

Build this **in parallel** with the R1-R6 fixes from #156. The preflight module exists from this PR onward. As each subsequent PR ships, it adds its precondition to the preflight (and removes a class of blocker). The toggle works from day one — it just starts very strict (most flips blocked until the system is healthy), then becomes more permissive as fixes land.

Parent: #156. Supersedes the manual operator gating in the original plan.
