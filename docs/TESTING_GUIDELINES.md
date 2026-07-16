# Testing Guidelines

> **Audience: every session that writes or reviews tests in this repo.** Each
> rule exists because a test gap (or a bad test) let a real bug through — the
> **Incident** lines are the proof. Companion:
> [`CODING_GUIDELINES.md`](CODING_GUIDELINES.md). Both are enforced at merge
> time by the PR review step in
> [`.github/PULL_REQUEST_TEMPLATE.md`](../.github/PULL_REQUEST_TEMPLATE.md).

## 1. Live-DB isolation is sacred — do not weaken `test/conftest.py`

- **pytest can NEVER write to the live databases.** The single load-bearing
  guard is [`test/conftest.py`](../test/conftest.py): (1) unconditional env
  redirect of every DB env var to a temp dir *before* any `database.*` import
  binds its engine (with `utils.config`'s `load_dotenv(override=True)` forced
  to run first — ordering is load-bearing), (2) `init_db()` on the temp DBs,
  (3) a tripwire that `pytest.exit`s if `DATABASE_URL` ever resolves to
  `db/openalgo.db`.
  - *Incident:* per-file opt-in isolation caused two phantom-row pollution
    incidents — the second when an E2E file shipped without the rebind and
    wrote real `trade_journal` rows to the live DB.
- **New engine-path tests need NO isolation boilerplate** — the global guard
  covers them. Subdir conftests may layer `monkeypatch` rebinds on top.
- **Never aim pytest at the live DB "just to check something"** — the
  tripwire will (correctly) abort the run. Use a read-only query instead.
- **Don't run the full suite during market hours on the production box.**
  Even with DB isolation, a full run contends for CPU and has previously
  interacted badly with the scan cycle. Use the CI-safe subset or wait for
  close (`scripts/ci.ps1` market-hours guard does this automatically).

## 2. No wall-clock dependence — pin or inject time

- **Never assert time-of-day-dependent state against the real clock.** Inject
  the clock (`_now_ist()` indirection) or assert the raw underlying state.
  - *Incident:* tests asserted `is_post_hold_active()`, which self-expires at
    15:35 IST — 4 tests passed all morning and failed on every afternoon CI
    run. The fix asserted `get_post_hold()` (raw armed state) instead.
  - *Incident:* a replay harness called `datetime.now()` and behaved
    differently after 15:31 — pin the "now" for replays.
- Same rule for dates: tests that mean "today" must construct the date, not
  inherit it, or they rot on weekends/holidays.

## 3. Test the integration glue, not just the leaf logic

- **The seam where two components meet needs at least one test that exercises
  the real wiring** — mocks injected *below* the seam prove nothing about it.
  - *Incident:* every WS test injected below the connection-pool layer, so
    `ConnectionPool.initialize()`'s return-value predicate was never executed
    by any test; a wrong truthiness check (`not x` vs `is False`) crashed the
    live feed.
- **Run inherited/upstream test suites; don't just check a function exists.**
  - *Incident:* running upstream's tests revealed 3 real fork divergences
    (auth-resume, session-expiry, greeks) that "the function is present"
    checks had missed.
- **Feature-flag both states:** if behavior is gated, test flag-on AND
  flag-off (the off path is production the day you need the kill switch).

## 4. Golden-incident regression tests

- **Every production incident becomes a permanent test** encoding the exact
  bad input → the previously-wrong output, named/commented with the incident
  (e.g. the DELHIVERY stale-close case in
  `test/test_fno_intraday_{buy,sell}_chartink.py`, the TATAELXSI
  SELL-reviewed-as-BUY veto direction bug).
- **Prove the fix fails on the pre-fix tree.** A regression test that passes
  on both trees is decoration. Check out the pre-fix commit (or revert the fix
  in the working tree), run the new test, watch it fail, then re-apply.
  - *Precedent:* the Semgrep silent-drop rules were validated by firing on the
    pre-fix tree; the scanner stale-value fix was proven the same way.

## 5. Hermetic tests cannot see environmental failure — pair them with runtime checks

- **A mocked-data suite is blind to stale feeds, dead tokens, and missing
  backfills.** When a feature depends on external data freshness, ship the
  runtime guard (smoke check, freshness gate, completeness metric) WITH tests
  for the guard — the suite alone is not the safety net.
  - *Incident:* the hermetic E2E suite stayed green while the sector-index 1m
    feed sat **12 days stale** (2026-05-29→06-10).
- **Test the negative/degraded paths, not just the happy path:** rejected
  orders, partial fills, expired tokens, missing bars, locked DB files,
  broker-session-down. The silent-drop audit class lives entirely in these
  paths.

## 6. What every code-changing PR must include

- **Tests added or updated for the changed behavior** — the PR template's
  Definition of Done requires it, and `pr-test-count.yml` tracks the count
  against the baseline.
- Safety-critical changes (order paths, exits, gates, journaling) name the
  test file that carries the guarantee in the PR description (the "no feature
  flag — the E2E suite carries the safety guarantee" pattern).
- Bug-fix PRs answer "what did the previous fix miss?" — and the new test must
  cover exactly that gap.

## 7. Running tests

```bash
uv run pytest test/ -v                      # full suite (off-hours only on prod box)
uv run pytest test/test_foo.py -v           # one file
uv run pytest test/test_foo.py::test_bar -v # one test
uv run pytest test/ --cov                   # coverage
cd frontend && npm test                     # React tests
```

- CI runs on the self-hosted runner (3–5 min); **never merge with checks
  pending or red** — `gh pr checks <N>` / `gh run view` must show all
  pass/success. A red `Quality gate` (GitHub-hosted) on pre-existing ruff debt
  is known noise; the `silent-drops` job and self-hosted `gate`/`backend-test`
  are the real gates.
- When CI is red, diagnose the real layer first: past "ruff debt" reds were
  actually a default `REDIRECT_URL`, an unset `API_KEY_PEPPER`, and a
  dir-less-path crash. Don't pattern-match red → rerun.

## 8. Test hygiene

- Files live in `test/test_*.py`; mark fast tests `@pytest.mark.unit`
  (markers: `unit`, `integration`, `live`).
- Tests must be **order-independent and re-runnable** — no shared mutable
  module state, no dependence on rows a previous test wrote.
- Synthetic frames used by scanner-rule tests are exempt from
  production-column gates by design (e.g. the D-bar date verify keys on the
  production `timestamp` column) — keep that exemption pattern when adding
  gates so unit fixtures stay lightweight.
- A flaky test is a P1 against the suite: fix or quarantine it the day it
  flakes; a suite that cries wolf stops being read.
