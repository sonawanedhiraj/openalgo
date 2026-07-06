# Process audit: why 11 merged PRs didn't fix the issue (and how to fix the plan)

The user's question is fair: *we have shipped a lot of code; nothing helped in production*. Before adding **another** PR, this comment audits the pattern so the next round doesn't repeat it.

## The 11 merged PRs that targeted this problem class

| Merge | PR | What it claimed | What it actually changed | Why production was unchanged |
|---|---|---|---|---|
| 06-21 | #92 (#85) | "Serialize DuckDB writes" | RLock on **one** `duckdb.connect()` call | 16+ other connect sites bypass |
| 06-23 | #109 (#110) | "Avoid lock contention" | First attempt at fallback helper | Reverted same day for breaking other paths |
| 06-24 | #118 (#110) | "Fallback helper retry" | Catches `ConnectionException("different configuration")` on 3 read functions | Doesn't catch `IOException` / `BinderException`; doesn't touch write paths |
| 06-24 | #125 (#124) | "Serialize ALL writes" | `@_synchronized_write` on 16 write functions | Serialises writes vs writes; **does not** prevent reads vs writes config mismatch — the actual error source |
| 06-24 | #130 (#129 P1) | "Scanner empty DataFrame" | Lazy-load guard | Unrelated to lock errors |
| 06-24..25 | #131-136 (#129 P2-4) | "Comprehensive E2E tests" | BootHarness + 4 integration batches + Playwright UI smoke | Tests pass in mocked environments; reproduce neither the concurrency pattern nor the WS-tick driven workload of production |
| 06-26 | #142 (#126) | "Broaden fallback exceptions" | Catches 3 transient classes + retry | Architecturally identical to #118 — still per-op connect/close, still mixed configs |
| 06-26 | #143 (#139) | "Boot orphan-PID guard" | Probes file at boot, exits if held by external pid | Only at boot; runtime contention untouched. Today (06-26) the boot probe passed cleanly THREE times, errors fired anyway |
| 06-26 | #144 (#140) | "Serialise boot backfill burst" | Lock between two boot workers | Held lock for ~1.5s while downloads ran ~25s — sibling started before they finished |
| 06-26 | #145 (#141) | "Broker freshness gate" | Filter stale tokens | **Shipped with `BROKER_FRESHNESS_GATE_ENABLED=false`** — never activated in production |
| 06-26 | #147 (#146) | "Subscribe-aware tripwire" | Module-level subscribe marker | Fixed false CRIT for live runs; today's logs show CRITs still firing for historical-replay calls (`scanner_subscribed_at: None`) |
| 06-26 | #152 (#151) | "Wait for jobs inside lock" | Polls `get_job_status` until terminal | Received empty job_ids because backfill helpers don't propagate `job_id` (open as #154/#155) |

## The pattern — six failure modes shared by every PR

### 1. **Tests verified the patch, not the outcome**

Every PR shipped with a passing unit test. Every test was structured: *"if I call the new function with arguments X, it returns Y"*. None were structured: *"after running the app for N minutes under production load, errors of class C = 0"*.

Example: `test_wait_for_jobs.py` (PR #152) has 9 tests, all green. They verify the helper's behaviour in isolation. They do not verify that **when the helper is wired into the boot path, the lock is actually held until downloads complete**. The wiring bug (job_id not propagated — issue #154) was outside the test's contract.

### 2. **Each PR fixed one manifestation, not the cause**

The error class — `Can't open a connection to same database file with a different configuration` — has been reported in essentially the same words since June 22. Each PR fixed a different *layer* (read fallback / write retry / boot serialisation / exception broadening). None fixed the architectural condition under which the error can occur (per-op connect with mixed configs in one process).

### 3. **No post-merge production verification**

After each merge we restarted, observed new errors, opened a new issue. There was no required step: *deploy, observe 24h, attach the post-deploy `errors.jsonl` delta to the closed issue*. Without a feedback loop, "fix" became unfalsifiable.

### 4. **Tests passed in isolation but not under production concurrency**

The BootHarness from #129 simulates boot but does **not** simulate:
- Live WS ticks at production rate (~100/sec across 224 symbols)
- The 5-worker `ThreadPoolExecutor` doing concurrent writes
- A second OpenAlgo process running concurrently (orphan case)
- Mid-day Zerodha re-login while downloads are in flight

A stress test that replicated even ONE of these would have caught #144's lock-too-narrow design before merge.

### 5. **Operator-flippable defaults masked unshipped fixes**

PR #145 added the freshness gate but defaulted `BROKER_FRESHNESS_GATE_ENABLED=false`. The merge was reported as success but **the production behaviour never changed**. The gate exists in code, untested in the actual environment, behind a flag nobody flipped.

### 6. **Issue scopes were too narrow to force architectural thinking**

Each issue focused on the most recent symptom: *"tripwire fires CRIT after restart"*, *"boot serialisation lock too narrow"*, *"job_id not propagated"*. None asked *"what would make this entire class of errors impossible?"*. The shape of the question constrained the shape of the answer.

---

## Process changes to prevent recurrence

### A. PR template addition — outcome-based acceptance

Every PR for this class of issue must include:

```
## Acceptance (outcome, not implementation)

Pre-fix metric: <run this query, attach output>
Post-fix target: <number, with verification command>
24h observation window: how long do we wait before declaring fixed?
What counts as regression: <specific error message classes>
```

Example for the DuckDB singleton PR (Phase 1 below):

```
Pre-fix: grep -c "different configuration" log/errors.jsonl  → ~50/day today
Target:  0 over 24h
Command: scripts/verify_duckdb_quiet.sh (asserts 0 matches in 24h window)
Regression: ANY occurrence of:
  - "Can't open a connection to same database file with a different configuration"
  - "Failed to connect to DuckDB after 3 attempts"
  in production logs over the verification window
```

### B. Stress test mirroring production concurrency

Before any PR claiming to fix DuckDB contention can merge, it must include:

```python
# test/test_duckdb_production_load.py
def test_50_readers_10_writers_zero_lock_errors():
    """Production-shape stress: 50 threads reading, 10 writing, 30 seconds."""
    # Spawn 50 reader threads doing get_ohlcv across random symbols
    # Spawn 10 writer threads doing upsert_market_data
    # Sample errors.jsonl every second
    # Assert: ZERO ConnectionException, ZERO IOException after 30 seconds
```

If the test fails, the PR can't merge. This single test would have prevented #118, #142, #144, #152 from being labelled "fixed".

### C. Required post-merge verification before closing the issue

Issue close is gated on the operator attaching:

```
## Production verification (24h)

Deployed: <commit-sha> at <timestamp>
Restarts in window: <count>
Pre-deploy error count (matching regex): <N>
Post-deploy error count (matching regex): <M>
Verdict: PASS / FAIL
```

If `M >= N * 0.5`, the issue stays open and a new PR is needed.

### D. No flag-default-off for fixes

Safety-fix PRs ship `default=true`. The flag is for emergency disable, not for "shipping the code without testing the behaviour". This was the structural reason PR #145 contributed nothing — the feature wasn't on.

If a fix is risky enough that it must be off-by-default for a day, the PR is staged: PR #145a ships the code with flag-off, PR #145b flips the flag default ON after the verification window. Two PRs, two acceptance gates.

### E. Issue framing: ask the right question

Issue titles for this class must follow the template:

> What architectural condition makes <error class> mathematically possible? Remove that condition.

Not:

> Fix the <error message that fired most recently>.

This forces the diff to address causation, not correlation.

---

## Consolidated open-issue list (12 issues, 4 are duplicates of the same scope)

After today's audit, four issues overlap heavily — they describe the same problem at different times:

- **#106** (P0 incident, 2026-06-22) — "Operational resilience — never lose another trading day to silent failure"
- **#129** (P0 infra, 2026-06-23) — "Comprehensive trading-day reliability: DuckDB locking + scanner cache invalidation + full E2E test harness"
- **#138** (P0 enhancement, 2026-06-26 morning) — "Tracking: broker freshness gap + boot DuckDB contention — 3-phase remediation"
- **#156** (P0 bug, 2026-06-26 evening) — "Root cause analysis: 261 DuckDB lock errors + 2160 indicator warmup warnings — architectural fix"

**Action: close #106, #129, #138 as superseded; #156 carries forward as the single umbrella.** The proliferation of umbrellas is itself a symptom of the process gap.

### Active actionable issues (ordered by dependency)

| # | Title | Why this order | Acceptance metric |
|---|---|---|---|
| **#156 Phase 1** | DuckDB singleton (shared writeable connection + cursors) | Unblocks everything below; closes 100% of historify lock errors | `grep -c "different configuration\|Failed to connect to DuckDB" logs` over 24h = 0 |
| **#161** | Add NSE_INDEX to scanner aggregator + harden sector_follow smoke | sector_follow produced 0 trades in LIVE today; highest operational impact | sector_follow at 15:20: `today_close` non-None for all 8 indices; smoke check FAILS if indices missing |
| **#156 Phase 2** | Scanner aggregator seeding from historify at boot | Closes the 25,272/restart `verify_series` warnings; depends on Phase 1 for the reads | `grep -c "verify_series" logs` per restart = 0 |
| **#157** | trade_journal two-phase exit + orphan reconciliation | Closes the recurring "No api_key resolvable for TCS exit"; one-time cleanup of 7 orphan rows | `SELECT COUNT(*) FROM trade_journal WHERE exit_price IS NULL AND exit_reason IS NOT NULL` over 24h = 0 |
| **#158** | Scanner reliability bundle (WS watchdog, daily-D for indices, api-key fallback, tripwire-replay silence) | D2 subsumed by #161; remaining D1/D3/D4 are smaller cleanups | Counts: WS staleness <20/day, RELIANCE+SBIN warnings = 0, historical-date CRITs = 0 |
| **#159** | Trading-day funnel instrumentation (Telegram 15:35 IST summary) | Diagnoses any future "zero trades" day at 15:35 instead of next-morning forensics | Telegram daily summary present every trading day |
| **#160** | backtest_db init order | Trivial; can be bundled with any of the above | `grep -c "no such table: backtest_trades" logs` per restart = 0 |
| #154 / PR #155 | job_id propagation | Keep merging as hygiene; becomes obsolete once #156 Phase 1 ships | covered by Phase 1's acceptance |

---

## The robust plan (six PRs, dependency-ordered, each with falsifiable acceptance)

Critically — **before any PR in this list merges**, it must include the items from sections A and B above. No exceptions.

### PR R1 — DuckDB singleton (#156 Phase 1)
- **What**: module-level `_shared_conn = duckdb.connect(path)` in `database/historify_db.py`; `get_connection()` returns its cursor; `connect_historify_readonly()` becomes an alias; remove all `read_only=True` opens in production code.
- **Stress test (gate)**: `test/test_duckdb_production_load.py` — 50 readers + 10 writers, 30s, zero lock errors.
- **Acceptance**: 24h post-deploy, count of `different configuration` + `Failed to connect to DuckDB` errors = **0**.
- **Eliminates**: 100% of in-process historify lock errors.

### PR R2 — Indices in scanner aggregator (#161)
- **What**: extend `MultiIntervalAggregator` to accept NSE_INDEX symbols; add the 8 sector indices + 5 scanner-universe indices to the subscribe set; harden `sector_follow_service.smoke_check` to verify BOTH stock AND index aggregator coverage; CRITICAL Telegram alert when all sector indices return None at entry.
- **Stress test (gate)**: simulate 1 hour of WS ticks for NIFTYAUTO at 1/sec; assert aggregator has non-empty 5m bar at minute 5+.
- **Acceptance**: 24h post-deploy, sector_follow 15:20 entry log shows `today_close` non-None for all 8 indices; smoke check at 15:18 fails-safe if indices empty.
- **Eliminates**: today's 0-trades-in-LIVE for sector_follow; scanner's 470 `bars_daily is None` warnings (the index subset; #158 D2 covers the daily-D historical backfill).

### PR R3 — Scanner aggregator seeding (#156 Phase 2)
- **What**: at boot, after broker session live, read last ~100 5m bars per scanner symbol from historify (via R1's singleton) and seed the aggregator.
- **Stress test (gate)**: boot the app, immediately call `aggregator.get_5m_bars("RELIANCE")` — must return >=100 bars before first WS tick arrives.
- **Acceptance**: 24h post-deploy, count of `pandas_ta_classic verify_series` warnings per restart = **0**.
- **Eliminates**: 25,272 warnings per restart; the "first 100 min scanner produces no signals" silent failure.

### PR R4 — Two-phase trade journal + orphan reconciliation (#157)
- **What**: `record_exit_pending` (sets reason only) → `record_exit_fill` (sets price + exited_at). Boot job `reconcile_orphan_exits` reclassifies rows with `exit_price IS NULL AND age > 1 day` to `exit_reason='abandoned_<original>'` + Telegram. One-time cleanup script for the 7 existing orphans.
- **Stress test (gate)**: simulate an exit attempt where broker rejects; assert row stays in pending state, no half-update.
- **Acceptance**: 24h post-deploy, recurring `[SIMPLIFIED-ENGINE] No api_key resolvable for X exit` errors = **0**; orphan row count = **0**.

### PR R5 — Scanner reliability + funnel diagnostics (#158 + #159 combined)
- **What**: WS watchdog audit (verify re-subscribe on reconnect); daily-D backfill for indices (covered partly by R2 already); api-key fallback for RELIANCE/SBIN; tripwire silence for historical replay; daily 15:35 IST funnel Telegram (scanner_hits → signals → orders → fills → journal).
- **Acceptance**: WS staleness <20/day, no `bars_daily is None`, daily funnel summary present every trading day.

### PR R6 — backtest_db init order (#160)
- **What**: trivial reorder.
- **Acceptance**: 24h post-deploy, count of `no such table: backtest_trades` warnings = **0**.

---

## What changes immediately, regardless of PR work

1. **Close redundant umbrellas**: #106, #129, #138 are superseded by #156. The proliferation itself is a symptom.
2. **Open PR template update**: section A acceptance gates become a checklist requirement.
3. **Recommend operator action**: revert `strategy_mode` for sector_follow to `sandbox` until #161 ships. Today's LIVE flip emitted 0 orders silently — same configuration tomorrow at 15:20 will do the same.
4. **For each open PR (#153, #155)**: add the acceptance section retroactively. If they can't produce a measurable acceptance, they should be re-scoped or closed.

## The honest summary

**The reason 11 PRs didn't fix the production issue is that none of them was structured to fix the production issue.** They were structured to make their tests pass. The tests passed because they tested the implementation, not the outcome.

The robust plan above changes the gating: a PR can merge only when there's a falsifiable post-deploy acceptance metric, a stress test that mirrors production concurrency, and (for fixes) a default-on configuration. Without these gates, the next round will produce the same result.
