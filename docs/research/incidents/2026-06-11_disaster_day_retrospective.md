<!-- migrated from outputs/2026-06-11_retrospective_and_plan.md on 2026-06-13 | summary: 2026-06-11 â€” Retrospective & Plan -->

# 2026-06-11 â€” Retrospective & Plan

**Author:** Claude (Cowork) Â· **For:** Dheeraj Â· **Status:** research-only draft (no code/config touched)

> Four distinct failures hit a single trading day. One of them â€” phantom RELIANCE
> rows from pytest writing to the live DB â€” is the **second** time this exact class
> has bitten us. This document is direct about why, including where I made the day
> worse rather than better.

---

## 1. What Happened Today (factual timeline)

All times IST. Reconstructed from `git log` (2026-06-11 commits), the memory record,
and direct code inspection. Where a timestamp is inferred rather than logged, it is
marked *(approx)*.

| Time | Event |
| --- | --- |
| 23:40, 23:41 (06-10) â†’ 00:02, 00:38 | Local `pytest` runs (4 of them) executed the **new e2e suite** added earlier that evening. Full buy/sell cycle tests wrote `trade_journal` entry rows to the **live** `db/openalgo.db`. |
| 00:17 (`2edeafa9`) | Veto-direction + EOD-relabel fix committed. |
| 00:51 (`34731aa9`) | EOD reconciliation fix committed. |
| 07:56â€“07:59 (`a346ad43`, `f261cb48`, `e4d9a404`) | Watchdog 15:14 cap + PARAMETER_LOG + merge of `feat/sector-rotation-etf` into dev. |
| 08:30 (`6c7f743e`) | sector_follow: journal failed orders + drop phantom positions. |
| ~09:15 | Market open. Live simplified engine boots / is running with **28 phantom RELIANCE rows** (`exited_at IS NULL`, entry_price 97.4 / 101.7) loaded as "open positions". |
| 09:32 â†’ 13:32 | **Every scan cycle aborts** `aborted_preflight`. Two compounding causes: (a) the engine retries exits on the phantom RELIANCE positions every tick â†’ ~1.2 errors/sec into `errors.jsonl`; (b) the legacy `daily_intent` preflight gate found no row because today's intent was set in the **new unified** `strategy_daily_intent` table. |
| 10:05 (`0518ddfd`) | Semgrep silent-drop rules + CI gate committed (unrelated to the incident, but part of the day's churn). |
| 11:09 (`5d27bd5d`) | 4 P0/P1 silent-drop fixes committed. |
| ~mid-day | **Failure 1 fix:** a legacy `daily_intent` row for today was written by hand â†’ preflight stops aborting on the intent check. |
| ~mid-day | **Failure 3 remediation:** SQL `UPDATE` marked the 28 phantom rows `exited_at`. DB layer was clean, but the **running engine held stale in-memory position state** â†’ still retried exits â†’ still erroring. |
| ~mid-day | **Full OpenAlgo restart during market hours** to clear in-memory state. High-risk; only tolerable because both strategies were in sandbox today. |
| 12:53 (`60a466f8`) | **Failure 2 fix:** `notify()` now falls through to the Phase 6 inbound bot when the legacy bot is inactive. Shipped to dev. |
| ~13:05 | Preflight recovers as the error burst ages out of the 30-min window (Failure 4's mechanism). |

### Failure 1 â€” Preflight reads the *legacy* `daily_intent`, intent was in the *unified* table
The pre-market control surface was migrated to `strategy_daily_intent`
(strategy+date+mode+intent+cap). Today's intent was set there. But the preflight
gate still queries the **old** `daily_intent` (date+mode). No legacy row â†’ gate
treats the day as "not armed" â†’ every cycle 09:32â€“13:32 aborts. Fixed mid-day by
back-writing a legacy row.

### Failure 2 â€” Outbound Telegram silently dropped
Phase 6 activation set `bot_config.is_active=0` to free the bot token for the
inbound poller. `services/notification_service.notify()` gated every send on
`telegram_bot_service.is_running`. With the legacy bot inactive, **every alert was
dropped with no error** â€” including the preflight-abort alerts that would have told
us Failure 1 was happening. Fixed at 12:53 (`60a466f8`): `notify()` now falls
through to `telegram_inbound_service.send_message_to_all()` and escalates send
failures via `logger.exception` instead of dropping.

### Failure 3 â€” Phantom RELIANCE rows from pytest â†’ live DB (SECOND OCCURRENCE)
Four overnight `pytest` runs executed `test/e2e/test_fno_flows.py` (the e2e suite
added 2026-06-11, commit `965587dd`). Its full-cycle tests call
`svc._place_entry_order(...)` with `_wait_for_fill` stubbed to **101.7** â€” exactly
the phantom entry price seen in `trade_journal`; the SELL variant uses
reference_price 97.5 â†’ **97.4**. The file **never rebinds `trade_journal_db.engine /
db_session`** to a temp DB, and `DATABASE_URL='sqlite:///db/openalgo.db'`, so each
run wrote real journal rows to the **live** database. 28 accumulated. The live
engine loaded them on boot as open positions and retried exits every tick.

### Failure 4 â€” Windows path-marker bug bricks the preflight error gate
`services/preflight_service.py`'s `recent_errors` gate excludes test-origin errors
via `_TEST_TRACEBACK_MARKERS = ('test/', 'unittest/mock', 'pytest')` â€” **forward
slashes only**. On Windows tracebacks are `...\test\test_x.py`, so the filter never
matches and pytest noise is counted toward the abort threshold (default 10). Any
pytest run bricks the live gate for up to 30 min until the burst ages out.
Identified today, **not fixed** (it is your WIP file).

---

## 2. Root Causes (the flaw, not the symptom)

### Failure 1 â€” *Incomplete migration: two control tables, one reader left behind*
**First-time** issue. The migration from `daily_intent` â†’ `strategy_daily_intent`
updated the *write* and *engine read* paths but left the **preflight gate** reading
the legacy table. This is migration debt: a dual-write/dual-read window with no
test asserting the gate reads the new source. The symptom was "preflight aborts";
the flaw is "no single source of truth for pre-market arming, and no test pinning
which table preflight consults."

### Failure 2 â€” *A liveness check used as a delivery gate, with a silent failure mode*
**First-time** issue (new, introduced by Phase 6). `notify()` treated "legacy bot
running" as a precondition for *any* delivery. When Phase 6 deliberately stopped the
legacy bot, the precondition turned a routing change into a **silent drop**. The
deeper flaw: a notification path whose failure mode is silence, and which had no
fallback when the only architectural change of the week (freeing the token)
invalidated its assumption. This is exactly the silent-drop anti-pattern the
Semgrep rules added the same day are meant to catch â€” and it slipped through.

### Failure 3 â€” *DB isolation is per-file opt-in, so every new test file is born polluting* (HIGHEST PRIORITY)
**Second-time** issue. The prior mitigation
(`fix/test-live-db-isolation` + `fix/test-isolation-engine-journal-services`)
rebound `trade_journal_db` to in-memory **inside the two then-known offending test
files**. It deliberately did **not** set a global isolation (that broke
`settings_db`). So the protection is opt-in per file.

**Why the previous mitigation didn't hold:** it was never structural. The moment a
*new* test file (`test_fno_flows.py`, written today) drives the engine entry path
without copying the rebind boilerplate, it writes to live `db/openalgo.db` again â€”
because `trade_journal_db.py` binds its engine to `os.getenv("DATABASE_URL")` at
import, and `.env` points that at the live DB. There is **no `test/conftest.py`**,
no `PYTEST_RUNNING` switch, no global `DATABASE_URL` override. Tellingly, the
*sibling* file added in the same commit (`test_engine_eod_reconciliation.py`) *does*
have a `_rebind` fixture â€” so the author knew the pattern, and the next file still
shipped without it. **Opt-in isolation is the root cause. A fix that depends on every
future test author remembering to opt in is not a fix.**

### Failure 4 â€” *Cross-platform path assumption in a safety gate*
**First-time** as a distinct bug, but a **repeat pattern**: POSIX-only string
matching on a Windows box. The flaw is that a *safety* gate's correctness depends on
a path separator, and the test-noise heuristic is fragile anyway (no-traceback test
`logger.error()` calls evade it even when fixed). The gate should not be defeatable
by, nor dependent on, the contents of `errors.jsonl` that tests can write.

**Common thread across all four:** every failure is a **silent or invisible**
breakage â€” aborts with no alert (1), drops with no error (2), pollution with no
guardrail (3), a gate mis-firing on its own noise (4). The system fails quietly, and
quiet failures on a trading day are the most expensive kind.

---

## 3. Docker Test Runner â€” Where We Are

**Verdict: built and deployed, but DOWN and structurally incapable of being the
default path. It has never run the test suite, and by design it cannot stop local
pytest from polluting the DB.**

### What exists (the infrastructure is real)
- **Templates** in `ci/runner/`: `docker-compose.yml`, `README.md`,
  `.env.runner.example`, `.gitignore`. Hardening contract is well-designed â€” no host
  bind-mounts, no Docker socket, no host networking, 1 CPU / 2 GB, the container is
  blind to `db/`, `.env`, `log/`, `bridge/`.
- **Deploy script** `scripts/install-runner.ps1` copies templates to
  `C:\actions-runner\`.
- **Runtime deployed:** `C:\actions-runner\docker-compose.yml` exists and
  `C:\actions-runner\.env.runner` has an `ACCESS_TOKEN` set (PAT supplied).
- **Workflow** `.github/workflows/ci-self-hosted.yml` targets labels
  `[self-hosted, linux, docker, openalgo-laptop]`; `ci.yml` was trimmed to manual
  frontend-only. Design doc `docs/BRANCHING_AND_CI.md` (32 KB) describes the L3 plan.

### What does NOT work today
1. **Docker Desktop is not running.** `docker version` â†’
   *"failed to connect to the docker API â€¦ daemon."* No daemon â‡’ no runner container
   â‡’ GitHub jobs queue forever (or never trigger). The runner is **offline right now.**
2. **The unit-test job collects zero tests.** `backend-test` runs `pytest -m unit`,
   but **no `markers` are defined in `pyproject.toml`** and no test is tagged `unit`.
   `-m unit` matches nothing. The workflow's own comment admits this and says keep it
   out of required checks "until the markers land." So even with the runner up, the
   only job that runs the suite runs **nothing**. `gate` runs `make gate`
   (`lint smoke test-engine`) â€” engine tests only, not the full `test/` tree.
3. **It only triggers on PR/push to main/dev.** It is a *CI-on-push* mechanism. It has
   **no relationship to a developer or an agent typing `uv run pytest test/` locally** â€”
   which is exactly how today's pollution happened (overnight local runs). The runner,
   even fully working, would not have prevented Failure 3.
4. **`EPHEMERAL: "false"`** in the deployed compose contradicts the README's
   "fresh container per job" claim â€” a persistent runner reuses its workdir, which
   weakens the isolation story the doc sells.
5. **No evidence it has ever executed a workflow successfully.** No green run is
   referenced anywhere; the markers gap alone means a green `backend-test` would be
   green-because-empty.

### Gap between "almost working" and "live" â€” punch list
1. Start Docker Desktop (WSL2 backend) and `docker compose -f C:\actions-runner\docker-compose.yml up -d`; confirm **openalgo-laptop = Idle** in GitHub â†’ Settings â†’ Actions â†’ Runners.
2. **Codify pytest markers** in `pyproject.toml` (`markers = ["unit","integration","live"]`) and tag the fast deterministic tests `unit`. Until this lands, `backend-test` is a no-op.
3. Decide `EPHEMERAL` intentionally (`true` for clean-per-job isolation matching the README, or document why `false`).
4. Make the runner **auto-start** (Docker Desktop "start on login" + `restart: unless-stopped` is already set, but the daemon being down today proves login-start isn't configured).
5. Add a one-time **smoke PR** that touches a backend file and confirm the self-hosted jobs go green end-to-end. Record the run URL in `docs/BRANCHING_AND_CI.md`.
6. **Recognize its scope:** the runner protects *the shared branches*, not *the local DB*. It is necessary but not sufficient. Failure 3 needs Section 4, not the runner.

---

## 4. Test DB Isolation â€” Where We Are

**Verdict: isolation is per-file opt-in. `trade_journal` (and any table reachable via
`DATABASE_URL`) writes to live `db/openalgo.db` for any test file that forgets to
rebind. This is the direct cause of Failure 3 and it will recur until isolation is
global.**

### Current state
- `database/trade_journal_db.py:28` â€” `DATABASE_URL = os.getenv("DATABASE_URL")`, bound
  at **import time**. `.env:43` â†’ `DATABASE_URL = 'sqlite:///db/openalgo.db'`.
- **No `test/conftest.py`.** Root `conftest.py` only pins the `sandbox` package and
  offers a lazy `_restx_loaded` fixture â€” **it sets no env, no DB override, no
  `PYTEST_RUNNING`.**
- Prior fixes (`fix/test-live-db-isolation`, `fix/test-isolation-engine-journal-services`)
  rebound `trade_journal_db.engine/db_session` **inside specific files only**, and
  intentionally avoided a global `DATABASE_URL=:memory:` because it broke `settings_db`.
- **Still writing to live DB:** `test/e2e/test_fno_flows.py` â€” its `_service()` builder
  and full-cycle tests call `_place_entry_order` with no `trade_journal_db` rebind. Its
  sibling `test/e2e/test_engine_eod_reconciliation.py` has a proper `_rebind` fixture;
  the FnO file does not. **This is today's polluter, confirmed by the 101.7 / 97.4
  entry prices.** Any future engine-path test added without the rebind joins it.

### Fix proposal â€” make local pytest *structurally incapable* of touching `db/openalgo.db`
A single global guard in a **new `test/conftest.py`**, autouse + session-scoped,
that runs before any module that reads `DATABASE_URL` is imported:

1. **Redirect every DB to a throwaway location for the whole pytest process.**
   Set `DATABASE_URL` (and the other DB path env vars â€” `LOGS`, `LATENCY`, `HEALTH`,
   `SANDBOX`) to files under a per-session `tmp_path` **at the very top of
   `test/conftest.py`**, before `import sandbox` and before any `database.*` import.
   Because `trade_journal_db` binds at import, the env must be set first â€” a
   `conftest.py` at `test/` is imported before test modules, satisfying that order.
2. **Solve the `settings_db` regression that blocked the global approach last time:**
   add a session-autouse fixture that calls each DB's `init_db()` against the temp
   files so the `settings`/`bot_config` tables exist in the throwaway DBs (this is the
   specific breakage the earlier surgical fix dodged â€” fixing it properly removes the
   reason isolation was kept per-file).
3. **Belt-and-braces tripwire:** a session-autouse fixture asserts the resolved
   `DATABASE_URL` does **not** contain `db/openalgo.db`; if it does, **fail collection
   immediately** with a loud message. A test run can no longer *start* against the live DB.
4. **Defense in depth in the gate (separate from this fix):** make Failure 4's
   `recent_errors` filter separator-agnostic AND add a positive check that ignores any
   `errors.jsonl` burst whose entries lack a production logger name â€” so a polluted log
   can't brick preflight even if isolation ever regresses.

This removes the dependency on every author remembering the rebind. The per-file
rebinds can stay (harmless) or be deleted once #1â€“#3 land.

---

## 5. The Plan (ranked, sequenced, concrete)

> Ranked by *risk-reduction per hour*. Items 1â€“2 stop the bleeding; 3â€“4 close the
> structural gaps; 5â€“7 harden.

1. **Global test DB isolation via `test/conftest.py`** *(WHAT: Section 4 #1â€“#3 â€” env
   redirect + temp-DB `init_db` + live-DB tripwire. WHY: kills the repeat polluter at
   the root; no future test can touch the live DB. WHO: me (subagent), you review.
   ETA: 0.5 day. DEPENDS: none â€” do this first.)* Deliverable: new `test/conftest.py`
   + PR; verify by running the e2e suite and confirming **zero** new `trade_journal`
   rows in `db/openalgo.db`.

2. **Fix Failure 1 â€” point preflight at the unified intent table** *(WHAT: change the
   preflight gate to read `strategy_daily_intent` via
   `mode_service.resolve_strategy_mode`, with legacy `daily_intent` as documented
   fall-through; add a test pinning the source. WHY: a silent all-day abort with no
   alert must never recur. WHO: me, you review â€” but coordinate, `preflight_service.py`
   is your WIP. ETA: 0.5 day. DEPENDS: a 2-min sync with you on WIP state.)*

3. **Codify pytest markers + bring the Docker runner online** *(WHAT: add
   `markers = ["unit","integration","live"]` to `pyproject.toml`, tag fast tests
   `unit`, start Docker Desktop, confirm `openalgo-laptop` Idle, land a green smoke PR.
   WHY: makes `backend-test` actually run something and gives shared branches a real
   gate. WHO: me for markers/PR; you for Docker Desktop start-on-login + PAT. ETA: 1
   day. DEPENDS: #1 ideally first so CI runs can't pollute either.)*

4. **Fix Failure 4 â€” separator-agnostic + logger-name-aware preflight noise filter**
   *(WHAT: normalize `tb_text.replace('\\','/')` before marker match AND ignore bursts
   lacking a production logger name. WHY: a pytest run must never brick the live gate.
   WHO: you (it's your WIP file) or me with your handoff. ETA: 0.25 day. DEPENDS:
   coordinate with #2 since both touch `preflight_service.py`.)*

5. **Add a Semgrep/lint rule for the silent-notification anti-pattern** *(WHAT: a rule
   that flags a delivery function gated on a single liveness boolean with no fallback /
   no `logger.exception` on the drop path â€” the Failure 2 shape. WHY: the day's own
   Semgrep work targets silent drops; this *was* one and slipped. WHO: me. ETA: 0.5
   day. DEPENDS: none.)*

6. **Refuse `/fix-bug` and `/restart-app` during market hours; scope bridge pytest**
   *(WHAT: implement the long-planned counter-measures â€” market-hours guard
   `09:15 â‰¤ now_ist â‰¤ 15:30`, replace full-suite pytest in `/fix-bug` with scoped
   pytest, add `log/bridge_access.jsonl`. WHY: the bridge auto-fix flow is the other
   silent pytest-on-live trigger; #1 makes its pollution harmless but the restart
   risk remains. WHO: me. ETA: 1 day. DEPENDS: #1.)*

7. **One-time cleanup + post-migration audit** *(WHAT: confirm no phantom rows remain
   (`trade_journal WHERE exited_at IS NULL AND source='chartink_FnO_intraday_buy'` with
   sub-second/synthetic signatures), and grep for any *other* reader still on legacy
   `daily_intent`. WHY: close the migration debt that caused #2 of the plan. WHO: me.
   ETA: 0.25 day. DEPENDS: #1, #2.)*

---

## 6. What I Would Have Done Differently Today

Direct accountability, where I added to the disaster vs. limited it.

**Where I made it worse / failed to prevent it:**
- **I let a second pollution event happen on my watch.** The class was in memory
  (`pytest-pollutes-live-db-and-preflight`). When the e2e suite was authored, I should
  have treated "new test file that calls `_place_entry_order`" as a tripwire and
  demanded the `trade_journal_db` rebind *in the same PR* â€” the sibling file had it; I
  didn't insist the FnO file match. I trusted a per-file convention I already knew was
  fragile.
- **I treated the SQL `UPDATE` as the fix for the phantom rows.** Cleaning the DB while
  the engine held stale in-memory state was a half-fix that forced the higher-risk
  market-hours restart. I know restarts wipe in-memory positions/stops/EOD timers
  (it's in memory, `feedback_no_restart_during_market_hours`). I should have predicted
  the in-memory/DB divergence *before* touching the DB and planned the sequence.
- **Migrations shipped without a "who else reads the old table?" sweep.** Failure 1 was
  a grep away. A migration is not done when the new writer works; it's done when every
  reader is accounted for.

**Where I limited the damage:**
- Failure 2's fix (`60a466f8`) was correct and additive â€” fallback + `logger.exception`
  instead of a silent drop. Good shape.
- Failure 4 was diagnosed correctly and I did **not** edit your WIP file â€” right call.
- I correctly read these as silent/test-pollution failures rather than chasing a
  phantom "trading is broken" outage (the misdiagnosis trap from
  `feedback_filter_pytest_noise_from_error_analysis`).

**Behaviors to change going forward:**
1. **No `pytest` on the live host during market hours â€” ever.** And no full-suite
   `pytest test/` locally until Plan #1 lands; until then, isolation is not guaranteed.
2. **New test file touching an engine entry/exit path â‡’ block on DB-isolation check**
   in review. Make it structural (Plan #1) so this stops depending on my vigilance.
3. **Migrations get a reader-sweep** (`grep` the old table name across the repo) before
   the old writer is considered retired.
4. **DB-then-memory ordering:** when remediating live state, reconcile in-memory state
   first or plan the restart explicitly; never assume a DB `UPDATE` propagates to a
   running engine.
5. **A silent failure mode is a bug even when "working as written."** Any delivery /
   gate path must fail loud (`logger.exception`) and have a fallback.
