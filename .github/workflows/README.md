# GitHub Actions workflows

This directory holds the project's CI/automation workflows. Most are
code-quality gates (`quality-gate.yml`, `ci.yml`, `ci-self-hosted.yml`,
`security.yml`); see `docs/SYSTEM_MAP.md` → "CI / code-quality gate" for the gate
catalog.

## `quality-gate.yml` — two-job split (silent-drops + quality)

Runs on PRs to `dev`/`main` and direct pushes to `dev`. As of 2026-06-14 it is
split into **two jobs** because GitHub gates required status checks at the *job*
level, not the step level:

- **`silent-drops`** — the only job intended to be a **required check on `main`**
  today. Deliberately minimal (checkout + uv + `uvx semgrep`) so it stays green
  and fast; runs only the custom ERROR rules
  (`.semgrep/silent-drops.yml --severity ERROR --error`) and blocks on any
  finding. These are the 4 confirmed P0/P1 silent-drop findings.
- **`quality`** — everything else (ruff, bandit, the WARNING heuristics, the
  public `--config=auto` rulesets), currently **informational**. Ruff still
  carries pre-existing debt, so this job is red on ruff; it will be **promoted
  to required on `main` once the ruff debt clears** and the job is reliably
  green. bandit and the public rulesets stay best-effort (`|| true`).

The split was needed because the ruff debt kept the original single combined
job red, which would have made the otherwise-green silent-drops check
un-requireable. Both jobs fire on the same triggers — the split is purely about
job granularity for branch protection, not about when the workflow runs.

## `code-direct-push-guard.yml` — direct-to-dev alert

`dev` intentionally accepts direct pushes (parameter-log updates, the strategy
registry, hotfixes — see `docs/PARAMETER_LOG.md`'s direct-to-dev policy), while
`main` is PR-protected. This guard watches every **direct** (non-PR-merge) push
to `dev` and, if the diff touches a runtime **code path** (`services/`,
`broker/`, `restx_api/`, `database/`, `blueprints/`, `utils/`, `mcp/`,
`websocket_proxy/`, `sandbox/`, `frontend/src/`, top-level `app.py`, `bridge/`),
Telegram-pings the operator with the SHA, author, message, and up to 8 touched
files. It is **alert-only — it never fails the job or blocks the push**.
PR-merge commits (message contains `Merge pull request #` / `Merge branch`, or
the commit has two parents) and exempt-only pushes are skipped silently. Exempt
paths (docs, `*.md`, `test/`, `*.yml`/`*.yaml`, `audit/`, `outputs/`,
`.github/`, `.semgrep/`, `pyproject.toml`, `uv.lock`, etc.) never alert; exempt
wins over code. **To add an exempt path, edit the `EXEMPT_PATTERNS` array in the
"Classify diff and alert" bash step** (globs, where `*` also matches `/`).

### Required repo secrets

The alert is a no-op (prints to the Actions log with a `::warning::` instead of
sending) until both secrets are set under
**Settings → Secrets and variables → Actions**:

- `TELEGRAM_BOT_TOKEN` — the bot token used for operator alerts.
- `TELEGRAM_CHAT_ID` — the operator chat id (single id).

### Manual test plan

To dry-run the alert path end-to-end, push a no-op edit under `services/` on a
personal test branch retargeted to `dev` (or temporarily point the workflow's
`branches:` at a throwaway branch), then watch the run under the **Actions** tab
and confirm the Telegram message arrives. A docs-only or `test/`-only push
should produce a "No code paths touched — no alert." log line and send nothing.

## `ci-cd.yml` — smoke-stack gate (issue #229)

Tier 1 of the test-infrastructure plan landed in-process integration tests via
`BootHarness` — fast, but blind to anything at a **process boundary**: Flask ↔
WS-proxy subprocess, ZMQ socket lifecycle, eventlet monkey-patching under
Gunicorn, real HTTP through middleware, ConnectionPool predicates. The 2026-06-22
WS-proxy-down cascading 0-signal day and the ConnectionPool predicate bug were
both this class.

`ci-cd.yml` carries the **smoke-stack** gate that closes that gap. It runs on
every PR to `dev`/`main` and every push to those branches. Layout:

- **`cd-build`** — builds `openalgo:latest` once, uploads it as an artifact so
  every downstream stack-boot consumes the same image (no rebuild thrash).
- **`cd-pytest-e2e`** — `docker compose up --wait` on `docker-compose.yml`
  (main stack), runs `test/e2e` against it.
- **`cd-playwright-smoke`** — same main stack, runs `smoke.spec.ts` +
  `scanner_clone.spec.ts` against the real Flask + WS-proxy. Both specs are
  broker-independent (smoke is route-reachability; scanner_clone uses
  Playwright `page.route` mocks).
- **`cd-playwright-broker`** — `docker compose -f docker-compose.test.yml -p
  mock_test up --wait` on the **mock-broker stack** (`docker-compose.test.yml`),
  runs `broker_happy_path.spec.ts` end-to-end through the FastAPI mock broker
  (`test/fixtures/mock_broker`). Exercises the full setup → login → broker auth
  → funds path with no real Kite OAuth.

All three downstream jobs run in parallel on the `cd-build` artifact. Each has
its own healthcheck-gated boot (`--wait`), a `failure()` diagnostics step that
dumps the last 300 lines of container logs + the healthcheck state, and an
`if: always()` teardown that runs `docker compose down -v --remove-orphans`
even on cancellation.

**Total wall clock per PR is ~5 minutes** (build ~80s on a cache miss / ~15-25s
on hit, then three ~2-3 min parallel test jobs).

**Branch protection (operator-only — GitHub UI step):** the smoke-stack gate is
not enforced on `main` until the operator marks the six job names listed at
the bottom of `ci-cd.yml` as required status checks under **Settings →
Branches → Branch protection rules**. The workflow itself is already triggered
on the right events; only the "required check" gating lives in the UI.
