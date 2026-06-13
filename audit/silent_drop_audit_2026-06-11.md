# Silent-drop audit — 2026-06-11

> **Resolution status (2026-06-14): all 4 P0/P1 findings RESOLVED in commit
> `5d27bd5d6` ("fix(quality): resolve 4 P0/P1 silent-drop issues from audit",
> 2026-06-11), with regression tests (`test/test_silent_drop_fixes.py`,
> `test/test_basket_order_dispatch.py`, `test/test_trade_journal_service.py`).
> The custom Semgrep ERROR gate (`.semgrep/silent-drops.yml`) now reports
> 0 ERROR findings across `services/ blueprints/ sandbox/ restx_api/`, so the
> CI custom-rule gate is GREEN and required-status-checks can be enabled on
> `main`. Per-finding RESOLVED markers below. The P2 findings remain open
> (observability-only).**

## Summary

- **Total findings: 14** (P0=1, P1=3, P2=4, NOT-A-BUG=6)
- The codebase is **largely well-hardened** against this class. The simplified engine
  entry/exit path, the EOD watchdog, `pending_order_execution_service`, and the
  central `place_order_service` all check the success flag, journal after broker
  acceptance, and escalate failures to `logger.exception` + Telegram. Today's
  `sector_follow` bug is already fixed (commit `6c7f743e`) and now follows the
  correct pattern (journal failed orders, don't create phantom positions, don't
  abort the batch on exception).
- **The remaining risk is concentrated in the multi-order / basket paths**, where a
  top-level `status="success"` is reported even when constituent orders failed, and
  in one sandbox execution rollback gap.

### Top 3 P0/P1 risks
1. **`basket_order_service.py:380`** — basket API response hardcodes top-level
   `status="success"` regardless of whether any constituent order filled. A live
   basket where the broker rejected **every** order returns success to the caller
   (TradingView/ChartInk/strategy). (Class E, P0)
2. **`sandbox/execution_engine.py:386-389`** — trade row + `order_status="complete"`
   are committed, *then* `_update_position` runs; if it raises, the order is flipped
   to `"rejected"` but the committed trade row and filled-quantity persist with **no
   matching position** → orphan fill / inconsistent P&L ledger. (Class C, P1)
3. **`options_multiorder_service.py:327-328`** — a leg reports `status="success"` if
   ≥1 of its split sub-orders succeeded; a leg split 5 ways where only 1 fills is
   reported as a successful leg, masking an under-filled (partial) options position.
   (Class E, P1)

### Methodology + scope
Grepped `services/`, `database/`, `blueprints/`, `bridge/`, `sandbox/`, `restx_api/`
and sampled `broker/zerodha/api/order_api.py` for: bare/Exception swallows
(`except…: pass|continue`), order-placement calls (`place_order`, `record_entry`,
`record_exit`), and unconditional status fields (`status="success"`, `processed=True`).
~200 swallow hits triaged; the bulk are benign (`queue.Empty`, `ValueError` parse
fallbacks, `psutil.NoSuchProcess`, best-effort notification/metric writes). Each
order/journal/state-mutating hit was read with 30-50 lines of context and classified
by production impact. Skipped `test/`, `frontend/`, `docs/`, `backtest/`, `outputs/`.

---

## P0 findings

### Basket order always reports top-level success
- **Status** ✅ RESOLVED in `5d27bd5d6` (2026-06-11). Both live and analyze paths
  now compute the top-level `status` from the per-order results (`success` only on
  all-filled, `partial` on some, `error` on none) and carry counts. The two
  remaining literal per-order `"status": "success"` results carry justified
  `# nosemgrep: hardcoded-success-envelope` suppressions. Regression:
  `test/test_basket_order_dispatch.py`.
- **File:line** `services/basket_order_service.py:380` (live path; analyze path same
  shape at `:304`)
- **Pattern type** E (unconditional success status)
- **What goes wrong** `response_data = {"status": "success", "results": results}` is
  built unconditionally after the order loop. `successful_orders` is counted only for
  the `BasketCompletedEvent` (line 382-389), never to gate the returned top-level
  status. Per-order failures live inside `results[*].status` but the envelope says
  success.
- **Reproduce** Send a basket via webhook/API when the market is closed, margin is
  insufficient, or symbols are wrong. Every `place_order_api` returns non-200 →
  every result is `{"status":"error"}`, but the API responds `{"status":"success",
  "results":[…all errors…]}`. A sender that checks only the envelope (most external
  platforms do) believes the basket executed.
- **Proposed fix**
  ```python
  successful_orders = sum(1 for r in results if r.get("status") == "success")
  overall = ("success" if successful_orders == len(results)
             else "partial" if successful_orders else "error")
  response_data = {"status": overall, "successful": successful_orders,
                   "total": len(results), "results": results}
  ```

---

## P1 findings

### Sandbox fill committed before position update; no compensating write on failure
- **Status** ✅ RESOLVED in `5d27bd5d6` (2026-06-11). The premature
  `db_session.commit()` before `_update_position` was removed — the trade row,
  the `order_status="complete"` update, and the position now commit once
  atomically (via `_update_position`'s own commit). If `_update_position` raises,
  the single rollback discards the not-yet-committed fill, so there is no banked
  fill without a matching position. (Invert-order fix, not a suppression.)
  Regression: `test/test_silent_drop_fixes.py`.
- **File:line** `sandbox/execution_engine.py:386-420` (`_execute_order`)
- **Pattern type** C (missing rollback / partial commit)
- **What goes wrong** Line 386 commits the `SandboxTrades` row + `order_status="complete"`
  + `filled_quantity`. Line 389 then calls `_update_position`, which has its own
  commit and **`raise` on failure** (`:678`). The `except` at 409 rolls back (no-op for
  the already-committed trade) and sets `order_status="rejected"` (committed at 418).
  Result: a persisted, "filled" trade row + a "rejected" order + **no position**.
- **Reproduce** Any exception inside `_update_position` (margin lookup, contract-value
  resolution, fund manager error) after the line-386 commit. The fill is banked, the
  position ledger is not — MTM, square-off, and `/positionbook` all diverge from the
  tradebook.
- **Proposed fix** Defer the order-status commit until after `_update_position`
  succeeds: build the trade row, call `_update_position` first (same transaction), and
  only then set `order_status="complete"` and commit once. On failure, the single
  rollback discards both. Alternatively, on the 409 path, also reverse/void the trade
  row rather than only flipping the order to rejected.

### Options multi-order leg masks partial split fills as success
- **Status** ✅ RESOLVED in `5d27bd5d6` (2026-06-11). The split-leg and
  spread-level status now report `partial` when `0 < successful < total` (and log
  at ERROR + alert on a partially-filled leg) instead of `success-if-any`.
  Regression: `test/test_silent_drop_fixes.py`.
- **File:line** `services/options_multiorder_service.py:327-328`
- **Pattern type** E (unconditional-ish success status)
- **What goes wrong** `overall_status = "success" if successful_orders > 0 else "error"`
  — one filled split out of N marks the whole leg successful. Under-filled legs in a
  multi-leg spread produce unbalanced exposure (e.g. a 4-leg iron condor with one
  short leg only half-filled is directionally naked).
- **Reproduce** A split leg (`splitsize < total_quantity`) where rate-limiting,
  partial liquidity, or a mid-batch rejection fails some sub-orders. Caller sees the
  leg as success and assumes full size.
- **Proposed fix** Report `partial` when `0 < successful_orders < len(split_results)`
  and surface `filled_quantity` vs `total_quantity` so the caller (and the spread-level
  aggregator) can react / unwind.

### Trade-journal write failures after a placed order are WARNING-only, no operator alert
- **Status** ✅ RESOLVED in `5d27bd5d6` (2026-06-11). Post-order journal-write
  failures now escalate to `logger.exception` (→ `errors.jsonl`) and alert the
  operator via `_alert_operator` → `notification_service.publish_anomaly`
  (severity `error`), while keeping the fail-safe sentinel return so order flow
  never breaks on an audit miss. Regression: `test/test_trade_journal_service.py`.
- **File:line** `services/trade_journal_service.py:109-115` (`record_entry`), same
  pattern in `record_exit` / `update_*` (`:155`, `:244`, `:280`, `:304`, …)
- **Pattern type** B (downgraded-severity swallow)
- **What goes wrong** On DB failure these return the `0` sentinel and log at
  `logger.warning` — which does **not** route to `errors.jsonl` (ERROR+) or Telegram.
  When called right after a real broker-accepted order (e.g. simplified-engine
  `_journal_record_entry` at `simplified_stock_engine_service.py:616`), the order is
  live but untracked in the journal, and the only trace is a warning log line. The
  simplified engine's in-memory `Position` still manages the exit, so this is P1 (not
  P0) — but the EOD summary, reflection, and operator visibility silently undercount.
- **Reproduce** SQLite lock / disk error during `record_entry` after a successful
  `place_order`. No journal row, no error-log entry, no alert.
- **Proposed fix** Log journal-write failures that follow a confirmed order at
  `logger.exception` (or publish a notification), so a placed-but-unjournaled order is
  loud. Keep the fail-safe return so it never blocks order flow.

---

## P2 findings

- `services/simplified_stock_engine_service.py:319` — arm response `status="success" if
  processed`. A symbol whose `_seed_history` / `_subscribe_quote` failed still lands in
  `processed` (results recorded but not gated), so arming "succeeds" with no data.
  Mitigated downstream by the data-freshness pre-entry gate; observability-only.
- `services/flow_executor_service.py:227-231` — `execute_place_order` logs an `error`
  level flow-log line on non-success and returns the result, but writes no failure
  journal row beyond the flow log. Acceptable (flow log is the record) but not
  operator-visible outside the flow UI.
- `sandbox/execution_engine.py:201` — multiquote fetch failure logged at `logger.debug`;
  a persistently failing quote feed stalls fills silently. Consider WARNING.
- `services/sector_follow_service.py:345-349` — MTM live-quote fetch swallows to a
  fallback; status-surface only, no trade impact.

---

## NOT-A-BUG — reviewed and dismissed

- `sandbox/position_manager.py:184` — `except: pass` around contract-value lookup,
  falls back to `Decimal("1.0")`; correct default for equities.
- `sandbox/execution_thread.py:315` — `except: pass` around an optional
  websocket-engine import probe in `is_execution_engine_running`; pure read.
- `services/flow_executor_service.py:1146` — `json.JSONDecodeError: pass` keeps the raw
  string as the variable value; intended fallback.
- `broker/zerodha/api/order_api.py:207-223` — `place_order_api` reads the body, extracts
  `orderid` defensively, sets `response.status`, and the caller (`place_order_service.py:248`)
  gates success on `res.status == 200`. Failure propagates correctly.
- `services/eod_watchdog_service.py:273-301` — broad `except` is deliberate (must not
  crash the APScheduler thread) and escalates to `logger.exception` + Telegram. Correct
  fail-safe.
- `services/pending_order_execution_service.py:142-179` — checks `success`, reconciles
  broker status, falls back to `"open"` and marks `"rejected"` on the failure branches.
  Sound.

---

## Cross-cutting patterns

1. **"Success-if-any" aggregation** is the dominant remaining anti-pattern (basket,
   options multi-order). Multi-order envelopes should report `success` only on
   **all** filled, `partial` on some, `error` on none — and always carry counts.
2. **Commit-then-mutate** ordering creates rollback gaps: the sandbox engine commits
   the fill *before* updating the position. State-mutating sequences should commit
   **once, last**, after every dependent write succeeds.
3. **Severity downgrade on the order path**: `logger.warning` (and `logger.debug`)
   for failures that occur *after* a real order is placed hides them from
   `errors.jsonl` and Telegram. Post-order failures deserve `logger.exception`.
4. **Well-modeled reference**: the simplified-engine entry path
   (`simplified_stock_engine_service.py:594-642`) is the template — check `success`,
   log ERROR + notify on failure, journal only after acceptance, wrap the journal
   write in a fail-safe that never blocks the order. New order paths should mirror it.

## Recommended pre-commit hook checks

Heuristic regex flags (warn, not block — these have legitimate uses):

1. **Hardcoded success envelopes** — flag a literal `"status": "success"` /
   `status="success"` that is **not** on a line containing a conditional
   (`if`/`else`/ternary). Catches `basket_order_service:380`-style envelopes.
   `rg -n '["\']status["\']\s*[:=]\s*["\']success["\']' services/ | rg -v 'if |else| == | if else'`
2. **Success-if-any** — flag `if\s+\w*success\w*\s*>\s*0` and
   `success.*if.*>\s*0` in `*order*service.py` for human review (partial-fill masking).
3. **place_order not followed by a success check** — AST rule: a call to
   `place_order` / `place_order_api` / `place_single_split_order*` whose result is not
   referenced in a subsequent `if`/`.get("status")`/unpacked `success` within N lines.
4. **Bare/Exception swallow on the order path** — flag `except(\s+Exception)?\s*:\s*(pass|continue)`
   in files matching `*order*`, `*execution*`, `*sandbox*`, `*journal*` (allowlist
   `queue.Empty`, `psutil.NoSuchProcess`, `json.JSONDecodeError`, `ProcessLookupError`).
5. **Downgraded severity after order placement** — flag `logger\.(warning|debug|info)`
   inside an `except` block in the same function as a `place_order*` call; suggest
   `logger.exception`.
6. **Commit-then-call** — flag a `db_session.commit()` followed within the same `try`
   by a method call that can `raise` before the function returns (heuristic; manual
   review). Targets the sandbox rollback-gap class.
