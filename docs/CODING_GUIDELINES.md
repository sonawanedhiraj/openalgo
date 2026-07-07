# Coding Guidelines

> **Audience: every session that edits code in this repo** — interactive Claude
> Code, bridge-spawned `claude -p`, and any other model or human. Each rule
> below exists because we shipped the mistake at least once. The **Incident**
> line under a rule is not decoration — it is the proof the rule is load-bearing.
> Do not relax a rule without reading its incident first.
>
> Companion: [`TESTING_GUIDELINES.md`](TESTING_GUIDELINES.md). Both are
> enforced at merge time by the PR review step in
> [`.github/PULL_REQUEST_TEMPLATE.md`](../.github/PULL_REQUEST_TEMPLATE.md).

## 1. Environment and tooling

- **Always `uv run`, never global Python.** `uv run app.py`, `uv run pytest`,
  `uv add <pkg>` (never hand-edit only requirements files). Target Python is
  **3.12**.
  - *Incident:* a Python 3.14 venv had no eventlet wheels — Werkzeug's guard
    killed boot and port 5000 never bound. CI pins 3.12 for the same reason.
- **Lint/format with ruff** (`E,F,W,I,B,C4,UP`, line-length 100):
  `uv run ruff check .` / `uv run ruff format .`. Pre-commit pins its own ruff
  (v0.8.6) which differs from the project pin — pre-empt hook surprises with
  `uvx ruff@0.8.6 check <files>` before committing.
- **Semgrep runs via `uvx`**, never added to the lockfile (every semgrep
  version conflicts with an existing pin):
  `uvx semgrep --config .semgrep/silent-drops.yml services/ blueprints/ sandbox/ restx_api/ --severity ERROR`.
- **Never `git commit --no-verify`** except the one documented case (merge
  commits tripping `check-added-large-files` on `frontend/dist`, noted in the
  commit body) and the pre-commit-deadlock recovery below. A bandit finding
  gets an inline `# nosec B<id> — <reason>`, not a bypassed hook.

## 2. Silent-drop / partial-success anti-patterns (the audit class)

The canonical catalog is
[`audit/silent_drop_audit_2026-06-11.md`](../audit/silent_drop_audit_2026-06-11.md);
the ERROR-severity Semgrep rules in `.semgrep/silent-drops.yml` block CI.

- **Never hardcode a success envelope.** Compute top-level `status` from the
  per-item results: `success` only when *all* succeeded, `partial` when some,
  `error` when none — and carry the counts.
  - *Incident (P0):* `basket_order_service.py` returned
    `{"status": "success"}` even when the broker rejected **every**
    constituent order; external senders that check only the envelope believed
    the basket executed.
- **Never report success if ≥1 sub-item succeeded.** A leg split 5 ways with
  1 fill is a *partial*, under-filled position, not a success
  (`options_multiorder_service` P1).
- **Never `commit()` before a mutation that can raise.** Commit-then-mutate
  leaves committed rows with no matching state on failure (sandbox
  `execution_engine` orphan-fill P1). Mutate first, commit last, or wrap both.
- **Post-order journal failures are ERROR-severity, not warnings.** An order
  that executed but wasn't journaled is invisible money
  (`trade_journal_service` P1).
- **No bare `except: pass` on state-mutating paths.** Swallowing is acceptable
  only for genuinely best-effort side work (metrics, notifications, parse
  fallbacks) — and even then log it.

## 3. Fail loud, fail safe

- **A degraded pipeline must never look like a quiet market.** When inputs are
  missing, log the per-symbol reason (which input was `None`), emit a
  completeness metric (`n_live / total`), and alert below thresholds
  (<50% WARNING, <20% CRITICAL).
  - *Incident:* the scanner failed closed silently — every missing input was a
    bare `return False` — so a tick-starved feed and a genuinely quiet market
    produced byte-identical zero-hit logs. Separately, sector_follow emitted
    0 signals with no alert on 2026-06-15 because today's 1m bars were absent
    and every gate failed closed.
- **Entries fail closed; exits fail open.** Safety gates (staleness, overrides,
  kill switches) may block *new entries* but must **never** block exits, EOD
  flatten, or square-off — a held position is riskier than a skipped entry.
- **Fail-open vs fail-closed on reference data:** fail closed only on a
  *confirmed* divergence; a *missing* cross-check is fail-open with a dedup'd
  WARNING (scanner reference-certificate pattern, issue #305).
- **Best-effort loops are per-item, never all-or-nothing.** One symbol's fetch
  failure is `logger.exception`-logged and skipped; the batch continues.
- **Errors log via `logger.exception()`** — never `logger.error()` + manual
  traceback, never `import traceback`. This routes the traceback to
  `log/errors.jsonl`, which is the first place anyone debugs.
- **Don't downgrade severity in an `except`.** Catching an ERROR-worthy
  failure and logging it at INFO/WARNING is how the journal-failure P1
  happened.

## 4. Runtime constraints (eventlet / Windows / single process)

- **No `asyncio` in server code.** Production runs Gunicorn + eventlet
  (`-w 1`); eventlet monkey-patching breaks `asyncio.run()` / `async/await`.
  Need async? Run it on a real OS thread
  (`telegram_bot_service._render_plotly_png` is the pattern). Code must work
  under BOTH the Windows dev server (real threads) and eventlet (green
  threads).
- **Single worker is load-bearing** (`-w 1`) — SocketIO/WS state is
  in-process.
- **Long-running pollers: one per resource, structurally enforced.** Check
  ownership before starting, return gracefully (no raise) if another owner is
  active.
  - *Incident:* two Telegram pollers on one bot token caused a
    `telegram.error.Conflict: terminated by other getUpdates request` storm
    (2026-06-30, issue #238).
- **Windows path handling:** normalize `\` → `/` before any path-substring
  matching.
  - *Incident:* preflight's test-origin filter matched POSIX paths only, so a
    Windows pytest run bricked the scan cycle.
- **Never restart OpenAlgo during market hours**, and scheduled/background
  tasks must never contend for the live SQLite DBs. Bridge `/restart-app`
  hangs on Windows — start the app directly with `Start-Process` instead.

## 5. Databases

- **SQLite: `NullPool` only, never `StaticPool`** — a shared connection under
  concurrency corrupts cursor state ("bad parameter or other API misuse").
- **SQLAlchemy ORM only, no raw SQL.** Sessions are cleaned up by the 5-layer
  teardown in `app.py` + middleware — don't add code paths that skip it.
- **DuckDB (`historify.duckdb`): all read-only access goes through
  `connect_historify_readonly()`.** The "different configuration" error is an
  *in-process* instance-cache conflict (the app holds it read-write), not a
  cross-process lock — the helper falls back to the shared connection.
- **Transient DuckDB locks are expected** (a CLI backfill holding the file):
  treat as skip-and-retry (`status='skipped_locked'`, quiet INFO), not as an
  alert-worthy failure.
- **Idempotency is the default for any writer that can re-run:** dedup by
  timestamp on replay, delete-then-insert per natural key, upsert with
  ON-CONFLICT-DO-UPDATE. Boot-time convergence checks re-run every restart —
  they must be no-ops when fresh.
- **Incremental convergence cannot fix a wrong value on a fresh date.** If a
  provisional/intraday value can be persisted, you need a forced overwrite
  re-settle pass, not just a staleness check.
  - *Incident:* a provisional intraday close froze into the daily bar; the
    staleness check saw "a bar exists for the day" and skipped it, and the
    stale close manufactured a phantom BUY (DELHIVERY, 42 fires, 2026-07-02).

## 6. Configuration, parameters, and crypto material

- **NEVER touch `API_KEY_PEPPER` or `FERNET_SALT`** on a running install — not
  as a fix, not "just in case". Everything authenticated/encrypted is sealed
  against them; rotation is a destructive reset with no in-tree migration.
  Backup before any `.env` edit: `cp .env .env.bak.<timestamp>`.
- **Every tunable change** (env var, DB config row, threshold default,
  scheduler time) gets a `docs/PARAMETER_LOG.md` entry in the same commit,
  committed **directly to dev** — never batched onto a feature branch.
- **Before parameter-dependent work, read PARAMETER_LOG AND verify `.env`.**
  The doc is intent; the env is reality; a mismatch is a real bug.
- **New behavior ships behind a default-on flag OR with tests that carry the
  safety guarantee — pick one deliberately.** Observability/gating additions
  get a flag (default `true`) so they can be disabled without a deploy.
  Structural safety fixes (WS reinit, WS recovery) ship flagless with an E2E
  suite as the guarantee. Never flagless *and* untested.

## 7. Time and timestamps

- **All market logic is IST**; indirect the clock (`_now_ist()` style) so
  tests can pin it — never call `datetime.now()` inline in gate logic.
- **Know each field's timezone contract before formatting.**
  - *Incident:* the dashboard appends `'Z'` to `last_trade_at`, so the field
    must be naive-UTC; writing tz-aware IST rendered "Invalid Date".
- **T+1 semantics key off the entry row's creation date**, not the exit date —
  ₹0 P&L "today" is correct when the last exit closed a prior day's entry.
- **Gate post-session events.** A straggler/backfill tick after close must not
  fire on a stale bar (the 17× post-close AUROPHARMA SELL class) — check
  market hours at the evaluation choke point.

## 8. Fork hygiene (upstream = marketcalls/openalgo)

- **Keep fork code in fork-only files.** New services/blueprints/strategies go
  in their own modules, not inline in upstream files. When an upstream file
  must be touched, wrap the block in `# FORK-START: <reason>` / `# FORK-END:`,
  bias to additions over modifications, and never reorder/reformat upstream
  code you aren't changing. (See `docs/BRANCHING_AND_CI.md` → Isolation.)
- **Issues/PRs live on `sonawanedhiraj/openalgo`** (origin), not upstream.

## 9. Concurrency of work (agents, worktrees, pre-commit)

- **Two concurrent code-editing tasks NEVER share a checkout.** Pre-commit's
  git-stash collides; the killed stash **silently reverts working-tree
  edits**. Use `git worktree` / `Agent(isolation: "worktree")` /
  `bash scripts/gh/track.sh new … --worktree`.
  - *Recovery if edits vanish:* `.cache/pre-commit/patch*` →
    `git apply --cached` → `uv run ruff` → `git commit --no-verify` → push.
- **Fresh worktrees lack gitignored `.env`** — `cp` it in before importing
  anything that reads `API_KEY_PEPPER`.
- **Rebuilding `frontend/dist` in a worktree needs `npm ci`** (lock-accurate);
  a drifted `node_modules` produces wrong chunk hashes.
- **A fix on a feature branch is not live.** The running engine executes the
  checked-out branch; a fix is live only after it merges into that branch AND
  the app restarts. Verify with `git merge-base --is-ancestor`.

## 10. Process discipline (summary — CLAUDE.md is canonical)

- Every task is a GitHub issue; branch `<type>/<N>-<slug>`; PR body carries
  `Closes #N` and title `[#N]` (use `bash scripts/gh/track.sh`).
- Architectural changes update `docs/SYSTEM_MAP.md` / `CLAUDE.md` **in the
  same commit** — doc drift starts the moment code lands without them.
- Strategy registry and parameter-log entries go **direct to dev**.
- Never merge with CI gates pending or red — wait for the full queue
  (self-hosted runner takes 3–5 min) and check `gh pr checks` before merging.
- Before reverting a "broken" merge, verify it actually broke boot — an empty
  `.err` file plus test-injected errors in errors.jsonl is not breakage.
