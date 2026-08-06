# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Strategy Registry — Read First When User Mentions "Trading Strategy"

The canonical record of every strategy tested in this project lives at
[`strategies/STRATEGY_REGISTRY.md`](strategies/STRATEGY_REGISTRY.md). When the user
references trading strategies — past, current, or proposed — Claude should READ
THAT FILE FIRST. It contains the active deployable shortlist, every rejected
strategy with WHY, in-flight experiments, the untested backlog, and the standard
testing protocol that makes rounds comparable.

The registry is a LIVING document. After every backtest round, Claude should add
a new entry. After every live trading session, Claude should update the relevant
`strategies/<name>/LEARNINGS.md`. The registry's `Active` and `In-Flight` rows
should reflect current reality.

## Cowork Objective (Read First)

**Cowork is the brain of this project.** It does real-time market research, selects
and tunes strategies, monitors execution, and drives continuous improvement through
a daily learn→backtest→improve loop. For the full objective, role definition, and
daily workflow, see [`COWORK_OBJECTIVE.md`](COWORK_OBJECTIVE.md).

**Strategy files live in `strategies/<name>/`** — each strategy has its own
`LEARNINGS.md`, `VERSION_LOG.md`, and `config_snapshot.json`. Always read the
active strategy's learnings before making decisions. See
[`strategies/simplified_engine/`](strategies/simplified_engine/) for the current
active strategy.

## Operational awareness — START HERE for diagnostics

Before investigating any unexpected mid-market behavior (preflight aborts,
stray pytest runs, mystery restarts, dirty working trees), check the
scheduled-task session inventory FIRST — not errors.jsonl. The fno-scan-cycle
task can mutate code, run pytest, and restart OpenAlgo silently via its
SKILL step 6 auto-fix flow. See [`docs/SYSTEM_MAP.md`](docs/SYSTEM_MAP.md)
for the full process/log/DB map.

Quick checks:
1. `mcp__scheduled-tasks__list_scheduled_tasks`
2. `mcp__session_info__list_sessions` + `read_transcript` on today's "Fno scan cycle" sessions
3. Then errors.jsonl (filter pytest noise per memory)

**Silent tick-flow death (Flask alive, feed dead — the 2026-07-07 libzmq 10055
incident):** two in-process daemons now watch for this (issue #376). The
**tick-liveness watchdog** (`services/tick_liveness_watchdog.py`) alerts CRIT
(`tick_liveness`) when NO live scanner bar closes for
`SCANNER_LIVENESS_MAX_SILENT_MIN` (default 10) min in market hours, then runs an
auto-heal ladder (re-subscribe → adapter reconnect → WS-proxy restart →
terminal CRIT) and logs an hourly handle/TCP resource-trend line. The
**WS-proxy supervisor** (`services/ws_proxy_supervisor.py`) auto-restarts the
`websocket_proxy` subprocess on unexpected exit (`ws_proxy_died` alert, capped
`WS_PROXY_MAX_RESTARTS_PER_DAY`/day). See `docs/SYSTEM_MAP.md` Processes §6.

## Task Tracking — Every Task Is a GitHub Issue

Every unit of work — feature, bug fix, documentation, **or backtest round** —
is tracked as a labelled GitHub issue and closed when its work merges. This
applies to **all** sessions: interactive Claude Code, bridge-spawned `claude -p`
(it inherits this file via `cwd=PROJECT_ROOT`), and Cowork dispatch (read-only —
see the carve-out below). Capability reference:
[`docs/TASK_CAPABILITIES.md`](docs/TASK_CAPABILITIES.md).

**The lifecycle (do this for every task):**

1. **Open or attach.** Before starting, `gh issue list --search "<keywords>"`.
   Reuse an existing issue or create one:
   `gh issue create --title "..." --label "type:<bug|enhancement|docs|infra|incident|strategy>" --label "area:<…>" --label "session:claude-code"`.
   Capture the issue number `N`.
2. **Branch by number.** `git checkout -b <type>/<N>-<slug>` (e.g.
   `feat/42-add-foo`). For parallel work, use a **git worktree** (below).
3. **Link the PR.** The PR description **MUST** contain `Closes #N` AND the PR
   title **MUST** be prefixed `[#N]`. Both are written for you by `bash
   scripts/gh/track.sh link <N>`. The body's `Closes #N` is a **required check**
   (`link-guard`): a code-changing PR with no issue link cannot merge. The title
   prefix is a convention (no CI gate) that surfaces the linked issue in any
   PR-list scan without having to click through. Docs-only PRs are exempt from
   the link-guard block but should still link a `type:docs` issue and carry the
   `[#N]` prefix.
4. **Validate before close (operator rule, 2026-07-21).** Every NEW issue MUST
   carry a `## Validation` section: concrete acceptance checks that prove the
   requirement is met, not just that code merged. For UI work the check is a
   **walkthrough from the user's navigation entry point** (e.g. /strategies →
   click through to the feature) with screenshot evidence — a feature reachable
   only by a hand-typed URL is NOT done, and the missing link IS part of the
   requirement (the open15 decision-log miss, issues #430/#425). **Evidence is
   posted to the ISSUE before it closes — strictly in that order.** When a
   check needs the running app, validate from the checked-out BRANCH (restart
   loads branch code) so evidence lands pre-merge; only then merge (auto-close
   fires with evidence already on the issue). If an issue ever auto-closes
   without evidence, REOPEN it until the evidence is posted.
5. **Close on done.** Merging the PR into `dev`/`main` auto-closes the issue
   (the `issue-autoclose.yml` Action parses `Closes #N` — GitHub's native
   keyword close only fires on the default branch, so we do it ourselves).
   **Auto-close certifies "merged", not "validated"** — step 4's evidence must
   exist either way. No-PR/manual work (a doc edit, a closed-as-wontfix) →
   close it yourself with a result comment.

**Labels** (filterable; bootstrap via `bash scripts/gh/bootstrap_labels.sh`):
`type:*` (kind), `status:*` (lifecycle), `session:*` (which session opened it),
`area:*` (subsystem), `P0/P1/P2` (priority), `strategy:backtest-round` (each
backtest round gets its own issue linked to the registry entry).

**Parallel tasks — use git worktrees (load-bearing).** Concurrent code-editing
tasks must run in their own `git worktree` (`Agent(isolation: "worktree")` or
`git worktree add ../wt-<N> <branch>`). Two agents committing in the *same*
checkout deadlock `pre-commit` (git-stash collision) and the killed stash
**silently reverts working-tree edits**. A worktree gives each task its own
index. Recovery if edits vanish: `.cache/pre-commit/patch*` → `git apply
--cached` → `uv run ruff` → `git commit --no-verify` → push. Fresh worktrees
lack the gitignored `.env` — `cp` it in before importing anything that reads
`API_KEY_PEPPER`.

**Cowork read-only carve-out.** Cowork dispatch tasks are read-only on code
(see "Scheduled Tasks Audit-Trail Policy") — they **open** a `session:cowork`
issue (or append to `audit/proposed_fixes.jsonl`) describing the problem, then
exit. A Claude Code/bridge session does the code change, links `Closes #N`, and
closes it. The create→work→close lifecycle is **asymmetric**: Cowork opens, an
editing session closes.

## Documentation discipline — change docs WITH code, not after

When any architectural change ships, the matching documentation update ships in the SAME commit. Not in a follow-up. Doc drift starts the moment code lands.

**Architectural changes that REQUIRE doc updates:**
- New/removed/renamed database → update `docs/SYSTEM_MAP.md` databases section
- New/removed/changed scheduled task → update `docs/SYSTEM_MAP.md` scheduled tasks table + this file's "Scheduled Tasks" section
- New/removed bridge endpoint or significant side-effect change → update `docs/SYSTEM_MAP.md` bridge section
- New process (daemon, port, sidecar) → update `docs/SYSTEM_MAP.md` processes section
- Log structure change (paths, formats, retention) → update `docs/SYSTEM_MAP.md` logs table
- Auth/security/mode-resolution change → update relevant sections of this file + `docs/SYSTEM_MAP.md`
- New strategy under `strategies/<name>/` → mention in this file + create LEARNINGS.md scaffold
- Request pipeline change (middleware order, CSRF, session) → update this file's "Request Processing Pipeline" section

**Changes that do NOT need doc updates:**
- Bug fixes that don't change behavior
- Small feature additions within an existing module
- Test additions, linting, formatting
- Dependency version bumps that don't change the public API
- Strategy parameter tuning (covered by `strategies/<name>/VERSION_LOG.md`, not architecture docs)

**Before merging an architectural change, verify:**
1. Does the diff make any line in `docs/SYSTEM_MAP.md` stale? If yes, update it in the same commit.
2. Does the diff make any line in `CLAUDE.md` stale? If yes, update it in the same commit.
3. Does the diff touch an active strategy? If yes, the strategy's `LEARNINGS.md` or `VERSION_LOG.md` may also need an entry.

**When spawning child tasks for architectural work, include in the brief:**
"Update `docs/SYSTEM_MAP.md` and `CLAUDE.md` if affected. Same commit. Verify by reading both before pushing."

**Canonically tracked files** (the ones most likely to need updates):
- `CLAUDE.md` — operational + coding guidance
- `docs/SYSTEM_MAP.md` — process/log/DB map
- `docs/architecture/AI_TRADING_BOT_DESIGN.md` — strategic roadmap
- `strategies/<active>/LEARNINGS.md` — strategy-specific cumulative learning
- `strategies/<active>/VERSION_LOG.md` — strategy parameter/logic history
- `COWORK_OBJECTIVE.md` — strategic objective
- `audit/README.md` — audit policy

**What changed today (2026-06-13):** Research persistence moved from gitignored
`outputs/` to tracked `docs/research/` as of 2026-06-13. See
[`docs/research/README.md`](docs/research/README.md) for the index, naming
conventions, and how to add new docs.

## Parameter changes ALWAYS go to dev directly

Any change to a tunable parameter (env var, DB config row, threshold default in
code, scheduler interval, etc.) MUST:

1. Add an entry to [`docs/PARAMETER_LOG.md`](docs/PARAMETER_LOG.md) in the same commit
2. Commit directly to `dev` — no feature branch, no PR, no batching with other work
3. Pair the doc update with the actual change (`.env` edit, SQL UPDATE, code default change)

**Why direct to dev:** every feature branch off dev inherits the latest log
automatically. Parameter changes stuck on feature branches create silent drift
where the live system disagrees with the documented intent. Direct-to-dev
guarantees alignment.

**When a feature branch adds a NEW tunable:** the PR description must propose
the PARAMETER_LOG entry. The entry is added to dev as part of the merge or as
an immediate follow-up direct commit.

**Before any parameter-dependent work** (spawning a backtest, deploying a
strategy, evaluating a rule): read PARAMETER_LOG AND verify against `.env`. The
doc captures intent; the env captures reality. Mismatches are real bugs.

### NEVER rotate `API_KEY_PEPPER` or `FERNET_SALT` on a running install

These two values are a categorical exception to the "edit `.env`, log it,
restart" pattern above. They are not tunables — they are **load-bearing
crypto material** that everything authenticated and encrypted on this install
is sealed against. Rotating either is a destructive reset.

What breaks the moment a running install starts up against a rotated value:

- **Login.** The user password is hashed as `Argon2(password + API_KEY_PEPPER)`
  (`database/user_db.py:120-136`). With a new pepper the existing
  `password_hash` row never verifies; `/login` returns a generic 401 "Invalid
  credentials" with no extra error log — the operator looks locked out for
  no apparent reason.
- **Every encrypted secret in `db/openalgo.db`.** Broker tokens, stored API
  keys, TOTP secrets, SMTP password are all Fernet-encrypted with a key
  derived from `API_KEY_PEPPER` + `FERNET_SALT`. A rotation makes every
  ciphertext unreadable. Re-login to Zerodha won't help once the row is
  written under the new pepper — the *next* restart can't decrypt it either.

There is **no in-tree migration** that re-hashes the password and
re-encrypts every secret on the way through a rotation. So a rotation is in
practice a destructive reset.

**Rules:**

1. Do not touch `API_KEY_PEPPER` or `FERNET_SALT` in `.env` after the
   first-run rotation has happened (i.e. once the placeholders have been
   replaced and a user has been created). Not as part of a fix, not as part
   of a "let me try a fresh value", not "just in case".
2. Before editing `.env` for anything, make a backup: `cp .env
   .env.bak.<timestamp>`. `.env.bak*` is gitignored.
3. If you think you must rotate them (compromise, install transfer), plan
   the full re-setup first: user-password reset, every broker re-link, every
   stored API key re-issued, every TOTP secret re-enrolled, SMTP password
   re-entered. There is no halfway.
4. If a rotation has already happened and the install is locked out, follow
   [`docs/runbooks/api_key_pepper_rotation_recovery.md`](docs/runbooks/api_key_pepper_rotation_recovery.md).

## Strategy registry updates ALWAYS go to dev directly

Same pattern as the parameter log: `strategies/STRATEGY_REGISTRY.md` is reference
documentation that informs every backtest, deployment, and "what should I try
next" decision. Spawned tasks read it via CLAUDE.md's instruction "READ FIRST
when user mentions strategy / backtest / round N".

Every new backtest round MUST:
1. Add an entry to [`strategies/STRATEGY_REGISTRY.md`](strategies/STRATEGY_REGISTRY.md)
   in the same commit that closes the round
2. Commit directly to `dev` — no feature branch, no PR, no batching with other work
3. Cross-cutting structural findings (e.g. "BS pricing is systematically optimistic
   for option buying", or "g13 5m-volume gate is the dominant killer in the
   chartink mirror rule") get an entry in the "Cross-Cutting Findings" section,
   same direct-commit pattern

**Why direct to dev:** every feature branch off dev inherits the latest registry
automatically. Registry entries stuck on feature branches create silent drift
where the canonical "what was tested and why" record disagrees with the work
that actually happened. Direct-to-dev guarantees alignment.

**When a feature branch creates new strategy files** (e.g. `strategies/<name>/`):
the PR description must propose the registry entry. The entry lands on dev as
part of the merge or as an immediate follow-up direct commit.

**Updating an active strategy's Status / Latest Note** after a live trading
session: direct to dev too. The registry's "Status" column is intended to be
fresh.

## Overview

OpenAlgo is a production-ready algorithmic trading platform built with Flask (backend) and React 19 (frontend). It is **four products in one self-hosted instance**, all sharing a single broker session and WebSocket feed:

| Surface | Route | Purpose |
| --- | --- | --- |
| **Unified Broker API** | `/api/v1/` | External platforms (TradingView, Amibroker, ChartInk, Excel, Python, MCP) |
| **Python Strategy Host** | `/python` | In-browser CodeMirror editor — paste scripts, schedule on IST times, run parallel strategies with process isolation and live logs |
| **Flow (No-Code Builder)** | `/flow` | Drag-and-drop nodes: market data → indicators → conditions → order execution; JSON import/export |
| **Options Trading Suite** | `/tools` | 12 analytical tools: Strategy Builder, Option Chain, IV Smile, Max Pain, Vol Surface, GEX, OI Tracker, Straddle Chart, etc. |

All surfaces share the Sandbox engine (₹1 Crore sandbox capital, exchange-aligned auto square-off) and support Telegram alerts.

**Repository**: https://github.com/marketcalls/openalgo
**Documentation**: https://docs.openalgo.in

## Security and Deployment Model

- **Single user per deployment** — no multi-user, no privilege escalation. One user, one broker session per instance.
- **Self-hosted on user's own server** — server access = full control. No SaaS component.
- All official install scripts (`install.sh`, `install-docker.sh`, `install-multi.sh`, `docker-run.sh`, `docker-run.bat`, `start.sh`) auto-generate unique `APP_KEY` and `API_KEY_PEPPER` via `secrets.token_hex(32)`.
- **SEBI static IP mandate** (effective April 1, 2026): All transactional API orders require broker-side static IP whitelisting. Delta Exchange (crypto) also enforces this. Stolen broker credentials CANNOT be used from an attacker's machine — the broker rejects requests from non-registered IPs. However, attacks routed THROUGH the OpenAlgo server (which has the registered IP) are still viable.
- External platforms (TradingView, GoCharting, Chartink) send API keys in JSON body or URL query params — they cannot set custom HTTP headers. This is an accepted architectural trade-off.
- The MCP server (`mcp/mcpserver.py`) is local-only, communicates via stdio with Claude Desktop/Cursor/Windsurf. It is NOT remotely exposed.
- Indian broker tokens expire daily at ~3:00 AM IST. Session management is aligned to this schedule.

## Development Environment Setup

### Prerequisites
- Python 3.12+ (required per pyproject.toml)
- Node.js 20/22/24 for React frontend development
- **uv package manager (required)** - Never use global Python

### Initial Setup

```bash
# Install uv package manager (required)
pip install uv

# Configure environment
cp .sample.env .env

# Generate new APP_KEY and API_KEY_PEPPER:
uv run python -c "import secrets; print(secrets.token_hex(32))"

# Run application (uv automatically handles virtual env and dependencies).
# frontend/dist is committed to the repo, so a fresh clone already has it
# ready to serve. You only need to install Node and build locally if you
# are actively editing React code.
uv run app.py
```

### Important: Always Use UV

**Never use global Python or manually manage virtual environments.** Always prefix Python commands with `uv run`:

```bash
# Running the app
uv run app.py

# Running any Python script
uv run python script.py

# Installing a new package (adds to pyproject.toml)
uv add package_name

# Syncing dependencies after pulling changes
uv sync
```

### React Frontend Development

```bash
cd frontend

# Install dependencies
npm install

# Development server (hot reload)
npm run dev

# Production build
npm run build

# Run tests
npm test

# Run end-to-end tests
npm run e2e

# Linting and formatting
npm run lint
npm run format
```

## Application Architecture

### Frontend

**React 19 Frontend** (`/frontend/`): Modern SPA with TypeScript, Vite, shadcn/ui, TanStack Query. Built and served from `/frontend/dist/` by Flask via `blueprints/react_app.py`.

### Backend Structure

- `app.py` - Main Flask application entry point
- `blueprints/` - Flask route handlers (UI and webhooks)
- `restx_api/` - REST API endpoints (`/api/v1/`)
- `broker/` - Broker integrations (30+ brokers), each with `api/`, `database/`, `mapping/`, `streaming/`, `plugin.json`
- `services/` - Business logic layer
- `database/` - SQLAlchemy models and database utilities
- `utils/` - Shared utilities and helpers
- `websocket_proxy/` - Unified WebSocket server (port 8765)

### Database Architecture

OpenAlgo uses **6 separate databases** for isolation:

- `db/openalgo.db` - Main database (users, orders, positions, settings)
- `db/logs.db` - Traffic and API logs
- `db/latency.db` - Latency monitoring data
- `db/health.db` - Health monitoring data
- `db/sandbox.db` - Sandbox trading mode (isolated from live trading)
- `db/historify.duckdb` - Historical market data (DuckDB)

Each database has its own initialization function in `/database/`.

#### SQLite Connection Pooling (NullPool)

All SQLite databases use `NullPool` — each operation gets a fresh connection, closed immediately after use. **Do NOT use `StaticPool`** (single shared connection) — it causes `"bad parameter or other API misuse"` and `"cannot commit - SQL statements in progress"` errors because concurrent requests corrupt the shared connection's cursor state. This applies to all platforms (Windows, Mac, Linux).

FD leak prevention is handled by 5 layers of session cleanup:
- `app.py` `teardown_appcontext` removes all scoped sessions after every request
- `traffic_logger.py` explicit `logs_session.remove()` in finally block
- `security_middleware.py` explicit cleanup for banned-IP WSGI path
- `blueprints/traffic.py` and `blueprints/security.py` teardown handlers

#### HTTP Client Pooling

Broker API calls use `httpx` with HTTP/2 connection pooling (`utils/httpx_client.py`). A single shared client instance per broker session maintains persistent connections to the broker's API servers, avoiding TCP/TLS handshake overhead on every order or data request.

### Broker Integration Pattern

All 30+ brokers follow a standardized structure in `broker/{broker_name}/`:

1. `api/auth_api.py` - OAuth2 or API key based authentication
2. `api/order_api.py` - Place, modify, cancel orders
3. `api/data.py` - Quotes, depth, historical data
4. `api/funds.py` - Account balance and margins
5. `mapping/` - Transform OpenAlgo format ↔ broker format
6. `streaming/` - WebSocket adapter for real-time data
7. `database/master_contract_db.py` - Symbol mapping
8. `plugin.json` - Broker metadata

Reference implementations: `/broker/zerodha/`, `/broker/dhan/`, `/broker/angel/`

### WebSocket Architecture

Real-time market data flows through a three-layer pipeline:

1. **Broker WebSocket Adapters** (`broker/*/streaming/`): Each broker has a WebSocket adapter that connects to the broker's proprietary feed and normalizes data into OpenAlgo's internal format. Connection pooling is per-broker: `MAX_SYMBOLS_PER_WEBSOCKET` (default: 1000) x `MAX_WEBSOCKET_CONNECTIONS` (default: 3) = 3000 symbols max.

2. **ZeroMQ Message Bus** (port 5555): Broker adapters publish normalized tick data to a ZeroMQ PUB socket. This decouples the broker feed from client delivery — the broker adapter runs independently and never blocks on slow clients.

3. **Unified WebSocket Proxy Server** (`websocket_proxy/server.py`, port 8765): Subscribes to ZeroMQ, manages client WebSocket connections, handles symbol subscriptions/unsubscriptions, and delivers filtered ticks to each connected client. Includes per-symbol throttling to prevent flooding slow clients.

**Broker re-login → WS reinit (event-driven, no restart, no flag).** Indian broker tokens expire daily ~3 AM IST. The WS proxy runs as a **separate subprocess** from the Flask app, so a Flask SocketIO event cannot reach it — the cross-process signal is the ZMQ `CACHE_INVALIDATE` event that `database.auth_db.upsert_auth()` publishes on every re-login. `WebSocketProxy._handle_cache_invalidation` consumes it and **unconditionally** calls `_reconnect_broker_adapter(user_id)`: it snapshots the held symbol subscriptions, disconnects, re-reads the fresh token via `adapter.initialize()`, reconnects, and re-subscribes — so the feed resumes **without an OpenAlgo restart**. It is failure-graceful (a rejected token logs `logger.exception`, retains the subscription snapshot in `_last_known_subscriptions`, and drops the dead adapter for the next client auth to rebuild) and idempotent (repeated events reuse the one adapter; disconnect always precedes reconnect). There is **no feature flag** — the E2E suite (`test/test_broker_session_auto_reconnect.py`) carries the safety guarantee. The login completion (`utils/auth_utils.handle_auth_success` → `notify_broker_session_refreshed`) also emits a `broker_session_refreshed` SocketIO event as a **UI/observability notification** (not the reconnect trigger — the subprocess is reached only via ZMQ).

**WS-reconnect historical replay (Fix B-prime, `services/ws_recovery_service.py`).** The WS reinit above resumes the *live* feed, but the in-process bar aggregators that drive the in-house scanner still have a **gap**: every 1m/5m bar that closed while the socket was down was never seen, so after a hiccup the scanner silently warms up from scratch (the 2026-06-11/12 tick-starvation collapse). The recovery service closes that gap. `notify_broker_session_refreshed` publishes an in-process `BrokerSessionRefreshedEvent` on the event bus (additive to the browser-only SocketIO emit); `WSRecoveryService` subscribes and, for every tracked symbol (scanner `SCANNER_SYMBOLS` universe with indices routed to `NSE_INDEX`, plus the sector_follow locked-static-30 stocks and mapped sector indices), fetches the last `WS_RECOVERY_LOOKBACK_MIN` (default 20) minutes of 1m bars via `history_service.get_history` and folds them into the live scanner aggregator through the new `MultiIntervalAggregator.replay_bars(symbol, bars)` — replaying the missed bar closes so rolling 5m/15m state is immediately current. **Guarantees:** idempotent (each `BarBuilder` dedups replayed bars by timestamp, so overlapping/repeat runs never double-count); best-effort and non-blocking (a per-symbol fetch failure is `logger.exception`-logged and skipped — never all-or-nothing — and the bus callback never raises back into login); observable (a structured Telegram alert summarizes symbols re-synced / elapsed / gap / bars replayed, escalated to a warning if >20% of symbols fail). **Limitation:** Zerodha's current-day historical API lags ~5-15 min, so a reconnect inside that window retrieves what is available and reports the still-missing minutes (they catch up on the next refresh). `get_history` enforces the broker's 3 req/sec limit internally, so ~250 symbols take ~85s. **No feature flag** — the service always registers at boot (`app.py` → `init_ws_recovery_service(app)`); `test/test_ws_recovery_service.py` carries the safety guarantee. It goes live on the next OpenAlgo restart.

### Request Processing Pipeline

WSGI middleware wraps in reverse order — last registered is outermost. The request flows:

```
Incoming Request
  → TrafficLoggerMiddleware (logs method, path, duration, status code)
    → SecurityMiddleware (checks IP ban list, blocks banned IPs with 403)
      → CSP Middleware (sets Content-Security-Policy headers)
        → Flask app (routing, blueprints, CSRF, session)
          → API key auth (for /api/v1/ endpoints)
            → Service layer → Broker API
```

Registered in `app.py:319-323`: security middleware first, then traffic logging (so traffic wraps outside security). Session cleanup happens in `teardown_appcontext` after the response is sent.

## Runtime Constraints

### Eventlet + Gunicorn (Production)

Production deployments (Ubuntu direct and Docker) run under **Gunicorn with eventlet worker** (`--worker-class eventlet -w 1`). This has critical implications:

- **No `asyncio`**: eventlet monkey-patches the stdlib and is incompatible with `asyncio.run()`, `async/await`, and `asyncio.get_event_loop()`. Any code that needs async behavior must use eventlet green threads or run async work on a separate real OS thread (see `telegram_bot_service.py:_render_plotly_png` for the pattern).
- **Single worker (`-w 1`)**: Required for WebSocket and SocketIO compatibility. Flask-SocketIO state is in-process and cannot be shared across workers.
- **`threading.local()` maps to green threads**: eventlet monkey-patches `threading.local()` so each green thread gets its own session. This is why `scoped_session` works correctly under eventlet.

### Windows / Mac Development

The Flask development server (`uv run app.py`) uses standard threading, not eventlet. Code must work in both environments. Key differences:
- No monkey-patching — standard `threading` and `socket` modules
- `asyncio` works normally on dev server but will break under eventlet in production
- SQLite concurrency behavior differs (Windows is more restrictive with file locking)

## Common Development Tasks

### Running the Application

```bash
# Development mode (auto-reloads on code changes)
uv run app.py

# Production mode with Gunicorn (Linux only)
uv run gunicorn --worker-class eventlet -w 1 app:app

# IMPORTANT: Use -w 1 (one worker) for WebSocket compatibility
```

Access points:
- Main app: http://127.0.0.1:5000
- API docs: http://127.0.0.1:5000/api/docs
- React frontend: http://127.0.0.1:5000/react

### Testing

```bash
# Run all tests
uv run pytest test/ -v

# Run specific test file
uv run pytest test/test_broker.py -v

# Run single test function
uv run pytest test/test_broker.py::test_function_name -v

# Run tests with coverage
uv run pytest test/ --cov

# React frontend tests
cd frontend
npm test                    # Run all tests
npm run test:coverage      # With coverage
npm run e2e                # End-to-end tests
```

Most testing is currently manual via:
- Web UI: http://127.0.0.1:5000
- Swagger API: http://127.0.0.1:5000/api/docs
- API Analyzer: http://127.0.0.1:5000/analyzer

### Building for Production

You typically do **not** need to build the frontend yourself for production deploys — see the CI/CD section below. Build only when actively editing React code:

```bash
# Build React frontend (only needed if editing React code)
cd frontend
npm run build

# The React build artifacts go to frontend/dist/
# These are served by Flask via blueprints/react_app.py
```

### Important: Frontend Build (CI/CD)

frontend/dist is committed (upstream convention as of v2.0.1.1 merge);
local rebuilds may produce dirty working trees — commit dist when shipping.

Practical implications:

- **Production servers** (clients running OpenAlgo on Ubuntu/Docker/EC2) **do not need Node.js or npm.** A plain `git pull` already brings the latest committed UI artifacts. This is the canonical upgrade path documented at https://docs.openalgo.in/installation-guidelines/getting-started/upgrade.
- **Backend-only local devs** (editing Python only, not React) also typically don't need to build — the committed dist serves the UI fine.
- **React developers** run `cd frontend && npm install && npm run build` (or `npm run dev` for hot reload) to test their own changes; commit the rebuilt dist when shipping UI changes.

## Key Architectural Concepts

### Plugin System for Brokers

Brokers are dynamically loaded from `broker/*/plugin.json`. The plugin loader (`utils/plugin_loader.py`) discovers and loads broker modules at runtime. To add a new broker:

1. Create directory: `broker/new_broker/`
2. Implement required modules: `api/`, `mapping/`, `database/`, `streaming/`
3. Add `plugin.json` with metadata
4. Add broker to `VALID_BROKERS` in `.env`

### REST API Layer (Flask-RESTX)

The `/api/v1/` endpoints are defined in `restx_api/`:
- Automatic Swagger documentation at `/api/docs`
- Uses Flask-RESTX for request/response validation
- All endpoints require API key authentication
- Rate limiting configured per endpoint type

### Action Center (Order Approval System)

Orders can flow through two modes:
- **Auto Mode**: Direct execution (personal trading)
- **Semi-Auto Mode**: Manual approval required (managed accounts)

Approval workflow in `database/action_center_db.py` and `services/action_center_service.py`

### Sandbox Trading Mode

Separate database (`sandbox.db`) with ₹1 Crore sandbox capital:
- Realistic margin system with leverage
- Auto square-off at exchange timings
- Complete isolation from live trading
- Sandbox controls (capital, leverage, reset schedule) live at `/sandbox` (`blueprints/sandbox.py`); request/response inspection is at `/analyzer` (`blueprints/analyzer.py`)

### Python Strategy Host

In-browser Python editor (`blueprints/python_strategy.py`) powered by **APScheduler** (`services/historify_scheduler_service.py` and `services/flow_scheduler_service.py` share the same scheduler instance). Each strategy runs in a subprocess for process isolation. Logs stream to the UI via SocketIO. Strategy metadata is persisted in `openalgo.db` via `database/strategy_db.py`.

### Flow (No-Code Builder)

Node-based visual strategy builder (`blueprints/flow.py`). Flow definitions are stored as JSON in `database/flow_db.py`. At runtime, `services/flow_executor_service.py` interprets the node graph, `services/flow_price_monitor_service.py` watches live prices, and `services/flow_scheduler_service.py` manages scheduled triggers via APScheduler.

### MCP Integration

Two MCP endpoints exist: `blueprints/mcp_http.py` (streamable HTTP transport for MCP) and `blueprints/mcp_oauth.py` (OAuth2 authorization for remote MCP clients). OAuth state is stored in `database/oauth_db.py`. The stdio MCP server (`mcp/mcpserver.py`) remains local-only.

### Real-Time Communication (Event-Driven Architecture)

OpenAlgo uses an event-driven architecture where state changes are broadcast to the UI in real-time:

1. **Flask-SocketIO events**: Order placement, modification, cancellation, position updates, and analyzer results all emit SocketIO events (e.g., `order_update`, `analyzer_update`, `cache_loaded`). The React frontend subscribes to these events for live dashboard updates without polling.

2. **WebSocket Proxy**: Unified market data streaming (port 8765) — see WebSocket Architecture above.

3. **ZeroMQ PUB/SUB**: Internal message bus between broker adapters and WebSocket proxy (port 5555). Also used for cache invalidation events across modules.

Key event flows:
- **Order placed** → `order_router_service.py` → broker API → `socketio.emit("order_update")` → UI updates
- **Market data tick** → broker WebSocket adapter → ZeroMQ PUB → WebSocket proxy → client browser
- **Master contract loaded** → `master_contract_cache_hook.py` → `socketio.emit("cache_loaded")` → UI notified
- **Analyzer trade** → `sandbox_service.py` → `socketio.emit("analyzer_update")` → sandbox UI updates

## Important Configuration

### Environment Variables (.env)

Critical variables to configure:
- `APP_KEY`: Flask secret key (generate with secrets.token_hex(32))
- `API_KEY_PEPPER`: Encryption pepper (generate with secrets.token_hex(32))
- `BROKER_API_KEY` / `BROKER_API_SECRET`: Broker credentials
- `VALID_BROKERS`: Comma-separated list of enabled brokers
- `DATABASE_URL`: Main database path
- `WEBSOCKET_HOST` / `WEBSOCKET_PORT`: WebSocket server config
- `MAX_SYMBOLS_PER_WEBSOCKET`: Symbol limit per connection
- `FLASK_DEBUG`: Enable debug mode (development only)

## Version Bumping

There are **two independent versions** in this repo. Do not confuse them.

### 1. Platform version (e.g. `2.0.1.0`)

This is the OpenAlgo platform itself. Source of truth: `utils/version.py`. Bumping touches **two files** and regenerates the lockfile — **never** the requirements files.

1. `utils/version.py` — `VERSION = "x.y.z.w"` (runtime source of truth, read by `get_version()`)
2. `pyproject.toml` — `version = "x.y.z.w"` (line 4, package metadata)
3. Run `uv sync` to regenerate `uv.lock` with the new version

```bash
# Example: bumping platform 2.0.1.0 → 2.0.1.1
# 1. Edit utils/version.py     → VERSION = "2.0.1.1"
# 2. Edit pyproject.toml line 4 → version = "2.0.1.1"
# 3. Sync the lockfile
uv sync

# 4. Verify
uv run python -c "from utils.version import get_version; print(get_version())"
# → 2.0.1.1
```

The platform version surfaces in:
- The UI footer / about page (via `get_version()`)
- API responses that include version metadata
- Docker image tags built by CI

### 2. OpenAlgo Python SDK pin (e.g. `openalgo==1.0.49`)

This is a **separate** client library published on PyPI ([`openalgo`](https://pypi.org/project/openalgo/)) that the platform uses internally. It has its own release cycle. Bumping the SDK pin touches the dependency lists, **not** `utils/version.py`:

1. `pyproject.toml` — update `openalgo==X.Y.Z` in the `dependencies` list
2. `requirements.txt` — update the `openalgo==X.Y.Z` line
3. `requirements-nginx.txt` — update the `openalgo==X.Y.Z` line
4. Run `uv sync` to regenerate `uv.lock`

```bash
# Example: bumping SDK 1.0.49 → 1.0.50
# Edit the three files above, then:
uv sync
```

**Rule of thumb:** if you are releasing OpenAlgo, bump #1. If a new SDK is on PyPI with a fix you need, bump #2. They are unrelated.

## Code Style and Conventions

### Python

The project uses **Ruff** for linting and formatting (configured in `pyproject.toml`):

```bash
uv run ruff check .          # lint (errors + warnings)
uv run ruff check . --fix    # auto-fix safe issues
uv run ruff format .         # format (replaces Black)
```

Ruff rules enabled: `E`, `F`, `W` (pycodestyle/pyflakes), `I` (isort), `B` (bugbear), `C4` (comprehensions), `UP` (pyupgrade). Line-length 100, target Python 3.12. Directories excluded: `.venv`, `frontend`, `db`, `log`, `strategies`.

- Use 4 spaces for indentation
- Use Google-style docstrings
- Imports: Standard library → Third-party → Local

Dev security tooling (in `dev` dependency group):

```bash
uv run --group dev bandit -r . -x .venv,frontend   # security scan
uv run --group dev pip-audit                        # CVE check on deps
uv run --group dev detect-secrets scan              # secret leak scan
```

### React/TypeScript
- Follow Biome.js linting rules (`frontend/biome.json`)
- Use functional components with hooks
- Component files use PascalCase: `MyComponent.tsx`

### Git Commit Messages (Conventional Commits)
- `feat:` New features
- `fix:` Bug fixes
- `docs:` Documentation changes
- `refactor:` Code refactoring

## Code-quality gates

Three static-analysis tools form the mandatory pre-commit / CI gate. Their
canonical rule catalog for the project-specific rules is
[`audit/silent_drop_audit_2026-06-11.md`](audit/silent_drop_audit_2026-06-11.md)
(14 findings: P0=1, P1=3, P2=4, NOT-A-BUG=6) — every custom Semgrep rule cites the
finding it encodes.

| Tool | Scope | Blocking? | How to run |
| --- | --- | --- | --- |
| **ruff** | lint + format (`E,F,W,I,B,C4,UP`, line-length 100) | yes | `uv run ruff check .` / `uv run ruff format .` |
| **bandit** | security scan | non-blocking initially | `uv run --group dev bandit -r . -x .venv,frontend,test` |
| **Semgrep (custom)** | silent-drop / partial-success anti-patterns | ERROR rules block; WARNING rules informational | `uvx semgrep --config .semgrep/silent-drops.yml services/ blueprints/ sandbox/ restx_api/` |

**Why Semgrep runs via `uvx`, not the uv lockfile:** semgrep cannot be added to
`pyproject.toml` — every version conflicts with an existing pin (semgrep <1.146
needs `tomli<2.1` vs `pip-audit`'s `tomli>=2.2.1`; semgrep ≥1.146 needs
`mcp==1.23.3` vs the project's `mcp==1.27.0`). `uvx semgrep` runs it in an
isolated ephemeral tool environment, and the pre-commit hook manages its own env
— both sidestep the lockfile entirely. The local dev env is Python 3.14; CI pins
3.12 (eventlet has no 3.14 wheels — see the boot-fail memory).

**Custom rules** live in [`.semgrep/silent-drops.yml`](.semgrep/silent-drops.yml)
(6 rules — 3 ERROR, 3 WARNING). The 4 confirmed P0/P1 findings map to 4 rules
(3 ERROR + the sandbox `commit-then-mutate` heuristic kept WARNING, per the
audit's own classification):
- `hardcoded-success-envelope` (**ERROR**) — literal `{"status": "success", ...}`
  in basket/multiorder responses (P0-1, `basket_order_service.py:380`).
- `success-if-any-aggregation` (**ERROR**) — `"success" if n > 0 else ...` masks
  partial fills (P1-3, `options_multiorder_service.py:328`).
- `journal-failure-warning-only` (**ERROR**) — post-order journal failure at
  `logger.warning` (P1-4, `trade_journal_service.py:110`).
- `commit-then-mutate` (**WARNING**) — `commit()` before a mutation that can
  raise (P1-2, `execution_engine.py:386`).

The 3 **WARNING** rules (`bare-except-swallow`, `severity-downgrade-in-except`,
and `commit-then-mutate`) are the cross-cutting heuristics the audit flagged as
"warn, not block — these have legitimate uses". A literal-`"success"` rule does
**not** fire on the safe variable-status pattern (`"status": status` computed
from results) — the simplified-engine arm response is intentionally not flagged.

**Pre-commit** ([`.pre-commit-config.yaml`](.pre-commit-config.yaml)): ruff +
bandit + semgrep (ERROR-only) + detect-secrets + biome run on staged files.
Enable locally: `uv pip install pre-commit && pre-commit install`.

**CI** ([`.github/workflows/quality-gate.yml`](.github/workflows/quality-gate.yml)):
runs on PRs to `dev`/`main` and pushes to `dev`. As of 2026-06-14 the workflow
is split into **two jobs** because GitHub gates required status checks at the
*job* level, not the step level:

- **`silent-drops`** — the lone job intended to be a **required check on `main`**
  today. Minimal by design (checkout + uv + `uvx semgrep`) so it stays green and
  fast: it runs *only* the custom ERROR rules (`uvx semgrep --config
  .semgrep/silent-drops.yml --severity ERROR --error services/ blueprints/
  sandbox/ restx_api/`) and blocks on any finding.
- **`quality`** — everything else (ruff, bandit, the WARNING heuristics, the
  public `--config=auto` rulesets), currently **informational**. Ruff still
  carries pre-existing debt from the 1535-error backlog, so this job is red on
  ruff; it will be **promoted to a required check on `main` once the ruff debt
  clears** and the job is reliably green. Within it, bandit and the public
  Semgrep rulesets remain best-effort (`|| true`).

The split was needed precisely because the ruff debt kept the single combined
job red, which would have made the otherwise-green silent-drops check
un-requireable. **The custom-rule gate is GREEN as of 2026-06-11
(commit `5d27bd5d6`)** — all 4 P0/P1 findings are fixed (the rules' firing on
the pre-fix tree was the proof they work; see
[`audit/silent_drop_audit_2026-06-11.md`](audit/silent_drop_audit_2026-06-11.md),
each finding marked RESOLVED). `uvx semgrep --config .semgrep/silent-drops.yml
services/ blueprints/ sandbox/ restx_api/ --severity ERROR` returns 0 findings,
so the `silent-drops` required-status-check can be enabled on `main`'s branch
protection. Branch protection on `dev`/`main` is configured via the GitHub UI
(cannot be automated from the CLI).

GitHub Actions guard `code-direct-push-guard.yml` alerts on direct-to-dev code
pushes — alert-only, no block. See `.github/workflows/README.md`.

## Test DB isolation — pytest can NEVER write to the live databases

Every `database/*.py` module binds its SQLAlchemy `engine` to
`os.getenv("DATABASE_URL")` (and `LOGS_/LATENCY_/HEALTH_/SANDBOX_DATABASE_URL`,
`HISTORIFY_DATABASE_PATH`) **at import time**, and `.env` points `DATABASE_URL` at
the live `db/openalgo.db`. The single load-bearing guard that stops pytest from
ever touching those live DBs is **[`test/conftest.py`](test/conftest.py)**. Do not
weaken it.

It is the structural replacement for the old *per-file opt-in* isolation (each
test had to copy a `_rebind` fixture). That was the root cause of two separate
phantom-row pollution incidents — the second when `test/e2e/test_fno_flows.py`
shipped without the rebind and wrote real `trade_journal` rows to the live DB.
Full write-up: [`outputs/2026-06-11_retrospective_and_plan.md`](outputs/2026-06-11_retrospective_and_plan.md)
(Section 4).

Three layers, all in `test/conftest.py`:
1. **Unconditional env redirect** at module top — every DB env var is repointed to
   a throwaway per-process `tempfile.mkdtemp()` dir *before* any `database.*`
   import binds its engine. **Ordering is load-bearing:** `utils.config`'s
   import-time `load_dotenv(override=True)` is forced to run *first* (via
   `importlib.import_module`) so it cannot later clobber the redirect back to the
   live path; the caller's pre-dotenv `DATABASE_URL` is captured *before* that for
   the tripwire.
2. **`init_db()` on the temp DBs** (`_isolate_databases`, session-autouse) — creates
   every table in the redirected DBs. This is the `settings_db` "tables don't
   exist" breakage that kept isolation per-file last time; fixing it here is what
   lets the redirect be global.
3. **Tripwire** (`pytest_configure`) — aborts collection with a loud `pytest.exit`
   (returncode 2) if `DATABASE_URL` ever resolves to the live `db/openalgo.db`, or
   if the caller explicitly aimed pytest at it (`DATABASE_URL=sqlite:///db/openalgo.db
   pytest`). A run can no longer *start* against the live DB even if layer 1 regresses.

Subdir conftests (e.g. `test/e2e/conftest.py`) may still `monkeypatch`-rebind
individual modules to per-test temp DBs; that layers cleanly on top and is
reverted per test. **Adding a new engine-path test no longer requires any
isolation boilerplate** — the global guard covers it.

## Common Patterns and Utilities

### API Authentication

All `/api/v1/` endpoints require API key:
```python
# In request body (recommended):
{"apikey": "YOUR_API_KEY", "symbol": "SBIN", ...}  # pragma: allowlist secret

# Or in headers:
X-API-KEY: YOUR_API_KEY
```

API keys are generated at `/apikey` and hashed with pepper before storage.

### Symbol Format

OpenAlgo uses a standardized symbol format across all 30+ brokers. Broker-specific symbols are mapped via `broker/*/mapping/` modules and stored in the `SymToken` table.

**Equity:** Just the base symbol — `INFY`, `SBIN`, `TATAMOTORS`

**Futures:** `[BaseSymbol][ExpiryDate]FUT` — `BANKNIFTY24APR24FUT`, `CRUDEOILM20MAY24FUT`

**Options:** `[BaseSymbol][ExpiryDate][Strike][CE/PE]` — `NIFTY28MAR2420800CE`, `VEDL25APR24292.5CE`

**Exchange codes:** `NSE` (equity), `BSE` (equity), `NFO` (NSE F&O), `BFO` (BSE F&O), `CDS` (NSE currency), `BCD` (BSE currency), `MCX` (commodity), `NCDEX` (commodity), `NCO` (NSE commodities — Zerodha only), `NSE_INDEX` (indices), `BSE_INDEX` (indices), `GLOBAL_INDEX` (global indices — Zerodha only, quote-only; includes US30/JAPAN225/HANGSENG and `GIFTNIFTY` from NSE IFSC)

**Order constants:**
- **Product:** `CNC` (cash & carry / delivery), `NRML` (futures & options carry), `MIS` (intraday square-off)
- **Price type:** `MARKET`, `LIMIT`, `SL` (stop-loss limit), `SL-M` (stop-loss market)
- **Action:** `BUY`, `SELL`

**Database schema (`SymToken`):** `symbol` (OpenAlgo format), `brsymbol` (broker format), `exchange`, `brexchange`, `token` (broker instrument token), `expiry`, `strike`, `lotsize`, `instrumenttype`, `tick_size`

### Database Queries

Always use SQLAlchemy ORM (never raw SQL):
```python
from database.auth_db import User

# Good
user = User.query.filter_by(username='admin').first()
```

### Error Handling

Return consistent JSON responses and use `logger.exception()` for error logging:
```python
from utils.logging import get_logger
logger = get_logger(__name__)

try:
    result = broker_module.place_order(data, token)
    return {'status': 'success', 'data': result}
except Exception as e:
    logger.exception(f"Error placing order: {e}")  # auto-captures traceback
    return {'status': 'error', 'message': str(e)}
```

### React API Calls

Use TanStack Query for server state:
```typescript
import { useQuery } from '@tanstack/react-query';

const { data, isLoading, error } = useQuery({
  queryKey: ['positions'],
  queryFn: () => api.getPositions()
});
```

## Logging Architecture

### Centralized Logging (`utils/logging.py`)

All logging flows through Python's standard `logging` module, configured in `setup_logging()` at import time. Every module uses `logger = get_logger(__name__)`.

**Three output handlers (all share the same `SensitiveDataFilter` to redact API keys/tokens):**

1. **Console** (always active): Colored output via `ColoredFormatter`, level controlled by `LOG_LEVEL` env var.
2. **File** (if `LOG_TO_FILE=True`): Daily-rotated text logs in `log/openalgo_YYYY-MM-DD.log`, retained for `LOG_RETENTION` days.
3. **JSON error log** (always active): `log/errors.jsonl` — structured JSON Lines, ERROR+ only.

### Error Log for Debugging

When debugging issues, **read `log/errors.jsonl` first**. Each line is a JSON object with: timestamp, logger name, module, source file:line, error message, full exception traceback (if any), and Flask request context (method, path, IP) when available. Auto-truncated to the last 1000 entries on app startup.

### Error Handling Convention

All error logging uses `logger.exception()` (not `logger.error()` + manual traceback). This automatically captures the full traceback and routes it to the JSON error handler. Do NOT use `import traceback` / `traceback.print_exc()` / `traceback.format_exc()` — these bypass centralized logging.

## Troubleshooting Common Issues

### WebSocket Connection Issues
1. Ensure WebSocket server is running (starts with app.py)
2. Check `WEBSOCKET_HOST` and `WEBSOCKET_PORT` in `.env`
3. For Gunicorn: Use `-w 1` (single worker only)
4. Check firewall settings for port 8765

### Database Locked Errors
1. SQLite doesn't handle high concurrency well
2. Close all connections and restart app
3. For production, consider PostgreSQL

### Broker Integration Not Loading
1. Check broker name in `VALID_BROKERS` (.env)
2. Verify `plugin.json` exists in broker directory
3. Check broker module structure matches pattern
4. Restart application to reload plugins

### React Frontend Build Errors
1. Ensure Node.js version matches `frontend/package.json` engines
2. Delete `frontend/node_modules` and run `npm install`
3. Check for TypeScript errors: `npm run build`

## Strategy Architecture

Strategies are versioned independently under `strategies/<name>/`. Each has:
- `LEARNINGS.md` — cumulative knowledge (most important file for decision-making)
- `VERSION_LOG.md` — parameter/logic changes with dates, rationale, backtest evidence
- `config_snapshot.json` — current live config values
- `README.md` — strategy overview and usage

**Active strategy**: [`strategies/simplified_engine/`](strategies/simplified_engine/)

**Scaffold strategy**: [`strategies/sector_rotation_etf/`](strategies/sector_rotation_etf/)
— a monthly long-only ETF rebalance that rotates across Indian sector ETFs on a
momentum sleeve (top-3 by 6M return) + low-vol sleeve (bottom-3 by 60d vol) with
risk-parity inverse-vol weighting between legs. Backtest-validated (Sharpe 1.17,
CAGR 14.8%, +34.8pp NIFTY alpha; see
[`BACKTEST_ROUND26_ETF_COMBINED_REPORT_2026-06-06.md`](BACKTEST_ROUND26_ETF_COMBINED_REPORT_2026-06-06.md)).
**SCAFFOLD ONLY — not live.** Signal computation lives in
`services/sector_rotation_etf_service.py` (pure, read-only on `historify.duckdb`,
emits recommended-orders JSON — never places orders). Entry point is the CLI
`uv run python -m services.sector_rotation_etf_cli --asof YYYY-MM-DD --current-positions '{...}'`.
**Not yet wired**: no scheduler job, no live mode (`mode: scaffold-only`,
`deployable: false`), no order placement — the operator reviews orders manually.
First sandbox rebalance planned for 2026-06-15 (moved up from 2026-07-01).
Operator-manual workflow — see
[`DEPLOYMENT_CHECKLIST_2026-06-15.md`](strategies/sector_rotation_etf/DEPLOYMENT_CHECKLIST_2026-06-15.md).
Still `mode: scaffold-only`, `deployable: false`; date moved earlier only to get
the operator hands-on sooner — no safety rails removed.

**Scaffold strategy**: [`strategies/sector_follow_cap5_vol/`](strategies/sector_follow_cap5_vol/)
— an intraday sector-follow strategy (spawned from R40 winner `V_SF_CAP5_VOL`):
at **15:05 IST** it buys ≤5 names whose mapped sector index is up >1% intraday AND the
stock is up >0.5% AND volume >1× its 20d average (vol-ratio tiebreaker), holds to a
T+1 **15:10** MARKET exit, with a 3%-of-capital daily kill switch. Universe is the
Phase-0.5 `LOCK_STATIC_30` set; ₹2.5L capital, ₹50k/position.
**Timing changed 2026-08-03 (issue #512 — NSE Closing Auction Session).** It was
entry 15:20 / exit 15:25 until then. From 2026-08-03 NSE ends continuous trading in
CAS-eligible cash scrips — **every F&O name, i.e. the entire `LOCK_STATIC_30`
universe** — at **15:15 IST**, then runs the auction 15:15–15:35 whose 15:25–15:30
phase accepts **LIMIT orders only**. This strategy places **CNC MARKET** orders and,
being CNC, has **no MIS auto-square-off backstop**, so a rejected T+1 exit would
silently carry to T+2 (the #497 failure shape). The whole chain therefore moved
inside continuous trading — refresh 15:02 → smoke 15:03 → entry 15:05 → exit 15:10 —
and the times are env-tunable (`SECTOR_FOLLOW_{ENTRY,EXIT,SMOKE_CHECK}_TIME`,
`SECTOR_FOLLOW_PREENTRY_REFRESH_TIME`) but **hard-clamped to 15:10** by
`resolve_schedule()`: an operator override can never push a MARKET order into the
auction. ⚠️ **A 15:05 entry is a DIFFERENT signal from the backtested 15:20 one** (a
different snapshot of the intraday move and of volume accumulation) — every R40/R41
result predates the change, so post-2026-08-03 sessions are **unvalidated** until a
re-backtest on the new window lands.
**LIVE** — the `strategy_mode` row has said `live` since 2026-06-24 (operator flip
via the harness). This paragraph previously read "SCAFFOLD ONLY — not live", which
was stale and dangerous for a real-money sleeve; the env default
`SECTOR_FOLLOW_CAP5_VOL_MODE=scaffold` is only the first-boot seed and is
superseded by the DB row (per the #440 per-strategy dispatch rules). Unlike
sector_rotation_etf, this strategy IS wired into the runtime: `SectorFollowService`
(`services/sector_follow_service.py`) is built at boot and registers 4 APScheduler
jobs (entry 15:05 / exit 15:10 / daily-reset 09:00 / EOD-summary 15:30 IST), but the
default `SECTOR_FOLLOW_CAP5_VOL_MODE=scaffold` places **no orders** — it computes
signals, logs, and writes the `sector_follow_trades` journal only. Flip to
`sandbox` / `live` is operator-only. Key files:
`services/sector_follow_service.py` (evaluator + scheduler glue),
`blueprints/sector_follow.py` (control API at `/sector_follow_cap5_vol/api/*` —
status/positions/pause/resume/close_all),
`database/sector_follow_db.py` (`sector_follow_trades` journal),
`services/sector_follow_index_backfill.py` + `services/sector_follow_stock_backfill.py`
(sector-index + universe-stock 1m feed; refreshed by the boot+periodic
state-convergence check below, not a cron). Plan + locked operator
decisions: [`strategies/sector_follow_cap5_vol/PLAN.md`](strategies/sector_follow_cap5_vol/PLAN.md).
Daily EOD report mirror written to `strategies/sector_follow_cap5_vol/eod_reports/YYYY-MM-DD.md`
at 15:30 IST (same content as the Telegram summary; git-ignored, observational).

**Active sandbox strategy**: [`strategies/futures_follow_cap50/`](strategies/futures_follow_cap50/)
— a **leveraged broad-market-beta** sleeve built on the sector_follow signal set
(spawned from the 2026-06-14 NIFTY-only CAP50 leverage research). At 15:20 IST it
**reuses** the `sector_follow_cap5_vol` C1×W2+E4 evaluator (does NOT reimplement the
gates) to find today's ≤5 stock signals, and for each — greedily in vol-ratio order
— buys **one NIFTY near-month index future lot** (NIFTY futures are MONTHLY; the
resolver picks the front-month from the master contract — there is no weekly NIFTY
future; the resolver **skips any contract expiring within 1 day** (today or
tomorrow) so the T+1 overnight hold is always viable — on a monthly-expiry day
the current-month contract is skipped and the next-month future is picked
automatically, never buying a contract that expires before the T+1 exit. NIFTY
monthly expiry is the **last Tuesday** of the month (verified 2026-06-15 against
the master contract: 30-JUN-26 / 28-JUL-26 / 25-AUG-26, all Tuesdays — NSE moved
NIFTY expiry off Thursday)),
HARD-CAPPED at **50% of capital as overnight SPAN margin** (₹10L book ⇒ ~2
lots; late signals beyond the cap are skipped). Product **NRML**, exchange **NFO**,
MARKET orders. Held to a **T+1 15:25 IST** MARKET sell. As of 2026-07-04 (#332)
the 15:20 decision snapshot (today's close+volume per symbol) comes from **one
batched broker quote call** (`get_multiquotes`) behind
`FUTURES_FOLLOW_INTRADAY_SOURCE` (default `quotes`; `aggregator` restores the
WS-fed scanner-aggregator path) — fallback chain quotes→aggregator→historify with
a WARNING per hop, plus a 15:18 dry-run quote probe in the smoke check. **No stop loss** (Phase-1
proved hard stops net-negative on this signal class); the **15:28 IST EOD watchdog**
(post-primary-exit retry backstop, #334 — after the 15:25 exit, before the 15:30
NFO close; a rejected 15:25 SELL stays in the book for it to retry) is the only
backstop. 3%-of-capital daily kill switch; modelled ~₹530/lot
(0.03% notional) round-trip charges.
**Big-loss news-context alert (issue #399):** the kill switch is same-day and
blind to T+1 overnight losses (the 2026-07-07 war-day gap read ₹0), so on a big
*realized* T+1 loss (`FUTURES_FOLLOW_BIG_LOSS_ALERT_PCT`, default 2% of capital;
`_maybe_alert_big_loss` in `run_exit`) — and on a kill-switch fire — the operator
Telegram alert is enriched with recent market headlines (`news_context_service`
reads `market_intel(kind='news')` already ingested by `news_ingest_service`), so
a large loss is explainable (war/macro/broad sell-off) at a glance. **Strictly
informational + human-in-the-loop — it NEVER places or cancels an order** (R54
stops and R55 put hedges both proved reacting is net-negative on this
leveraged-beta sleeve; headlines are treated as untrusted DATA, only embedded in
the alert text). Read-only, fail-open, once/day; flags
`NEWS_CONTEXT_ON_ALERTS_ENABLED` (default true), `NEWS_CONTEXT_LOOKBACK_MIN`,
`NEWS_CONTEXT_MAX_ITEMS`, `NEWS_CONTEXT_HIGHLIGHT_TERMS`.
**ACTIVELY TRADING IN SANDBOX** (`mode: sandbox`, `deployable: true`) — there is **no
scaffold / observe-only state**; the mode flag is only `sandbox` or `live`.
`FuturesFollowService` (`services/futures_follow_service.py`) is built at boot and
registers 6 APScheduler jobs (reset 09:00 / smoke-check 15:18 / entry 15:20 /
exit 15:25 / watchdog 15:28 / EOD-summary 15:30 IST).
**Entry timing is flag-gated (`FUTURES_FOLLOW_ENTRY_MODE`, default `legacy`, issue #406):**
`legacy` is the job map above (entry 15:20, T+1 exit 15:25). `same_minute` (OPTION_C)
instead makes 15:20 a *signal snapshot* and 15:25 an *exit-then-entry* job — the T+1
exit runs first so the new entry sizes against a fresh 50% cap (closes the #405 carry
under-sizing) and margin never overlaps. Backtest: +1.19pp CAGR / +0.07 Sharpe vs
legacy, peak margin 49.8% (see
`docs/research/strategy/futures_follow_cap50/2026-07-14_entry_cap_carry_sizing.md`).
Selection stays at 15:20 (backtest-faithful); execution moves to 15:25. Live needs the
exit fill confirmed before the entry places (sandbox ₹1Cr book is unaffected).
The default `FUTURES_FOLLOW_MODE=sandbox` means it **places
real orders into `sandbox.db` (the virtual ₹1Cr book) from boot** — the first sandbox
cycle is **Monday 2026-06-15 15:20 IST** (the session's first sector_follow signal →
a NIFTY-futures BUY in sandbox.db). Flip to `live` is operator-only (env or a
`strategy_mode` row); operator can pause active trading via
`POST /futures_follow_cap50/api/pause` (durable `strategy_runtime_override`) without
changing mode.
**Stage-1 LLM veto (strategy-aware, issue #318):** `run_entry` reviews every
in-cap signal via `signal_review_service.review_signal(strategy_name=
'futures_follow_cap50')` before `place_entry`. The prompt is the strategy's OWN
framing (STRATEGY CONTEXT block from `config_snapshot.json`'s `llm_context` key,
code fallback in `signal_review_service.py`): the veto's job is
**overnight-regime fit for a leveraged long NIFTY carry**, not per-name stock
analysis; it reviews BOTH the source stock signal and the resolved NIFTY-future
book state (from `get_status()`) combined. Enforcement resolves per-strategy
(`strategy_llm_config` row → `VETO_LAYER_MODE` env → B4 mode-aware default,
**sandbox = active/enforcing**). An enforcing `skip` drops that lot without
consuming the 50% margin cap and journals `status='veto_skip'`; cumulative
review time is budgeted at 180s/batch (beyond it remaining signals place
UNREVIEWED); every reviewer failure fails OPEN. The simplified engine's veto
path (`strategy_name=None`) is unchanged.
**Honest caveat (load-bearing — do not lose):** the backtest clears 12% (CAGR
14.44%, Sharpe 1.27, MaxDD −8.0% on ₹10L, 2024-01..2026-06) but the signal does
**NOT** predict NIFTY direction (hit-rate 53.4% < 55%, corr 0.295). The return is
leveraged broad-market drift on bullish signal-days — **leveraged beta, not the
sector_follow stock-selection alpha** — so it will struggle in a sustained flat/bear
NIFTY regime, where it has no edge to fall back on. Sector-matched routing
(banking→BANKNIFTY) was tested and **rejected** (costs 0.74pp CAGR, no correlation
gain — NIFTY-only is the vehicle). Keep `sector_follow_cap5_vol` (CNC T+1 equity) as
the alpha primary; run this as a separate, leverage-bounded beta sleeve. Key files:
`services/futures_follow_service.py` (evaluator reuse + sizing + scheduler glue),
`blueprints/futures_follow.py` (control API at `/futures_follow_cap50/api/*` —
status/positions/pause/resume/close_all/data_health/entry_breakdown/entry_breakdown-history),
`database/futures_follow_db.py` (`futures_follow_trades` journal),
`database/futures_follow_eval_db.py` (`futures_follow_eval_snapshots` — the
per-day 15:20 entry-evaluation breakdown, issue #352: `run_entry` persists the
sector_follow evaluator's per-symbol gate inputs/outcomes after placement
decisions, fail-graceful; read via `GET /futures_follow_cap50/api/entry_breakdown`
— API-key OR logged-in-session auth, read-only — and rendered as the
"15:20 Evaluation" card on `/strategies/futures_follow_cap50`, so a zero-signal
day is explainable without reading logs. **History (issue #395):** `GET
/futures_follow_cap50/api/entry_breakdown/history?limit=&before=` lists per-day
digests (`services/futures_follow_eval_view.summarize_payload` — ~300 B/day, not
the ~9 KB payload; `limit` clamped to `MAX_HISTORY_LIMIT`=90, `before` pages
backwards) plus a `today` object (`is_trading_day` via
`data_freshness_service.is_trading_day`, `snapshot_exists`) that tells the UI
whether to show the pending row — the snapshot only lands at 15:20 IST, and on a
weekend/NSE holiday none is coming at all. The card is one table: today is the
newest row, the newest row WITH a snapshot is expanded by default, and each row
drills into the same per-symbol table via the `?date=` endpoint. Snapshots start
2026-07-07 (when #352 shipped the writer); earlier days are not reconstructable
because the live path reads today's close/volume from broker `quotes` while a
replay could only read historify). Plan + locked
decisions: [`strategies/futures_follow_cap50/PLAN.md`](strategies/futures_follow_cap50/PLAN.md).
Backtest reports:
[`docs/research/strategy/sector_follow_cap5_vol/2026-06-14_sector_matched_futures_10L.md`](docs/research/strategy/sector_follow_cap5_vol/2026-06-14_sector_matched_futures_10L.md)
(NIFTY-only CAP50 control) and `2026-06-14_futures_10L.md`. Daily EOD report mirror
written to `strategies/futures_follow_cap50/eod_reports/YYYY-MM-DD.md` at 15:30 IST
(git-ignored, observational).

**Sandbox measurement strategy**: [`strategies/open15_vol_breakout/`](strategies/open15_vol_breakout/)
— opening 15-min **mid-bar volume-surge breakout** (issue #425). Top-3 pre-open
gainers long / losers short; tick-driven entry the moment cumvol-in-minute ≥1.5×
the running-avg minute volume AND price breaks the 09:15 candle level; hard
flatten at the configured exit time. **Deployed as a measurement, not a
validated edge**: Round 58
proved every honest bar-level variant of this signal loses (the published edge
was entry-level look-ahead); the journal's level/trigger-second/trigger-price/
minute-close columns measure how much of the ~0.54% intra-bar burst a legal
real-time entry captures (decision rule in SPEC §4). `Open15BreakoutService`
(`services/open15_breakout_service.py`) has its own additive ZMQ tick SUB and 4
APScheduler jobs (arm 09:10 / exit / retry +2 / summary +5). The entry cutoff
and exit time are **UI-configurable** on `/open15_vol_breakout/logs` (issue
#451: `open15_config.no_entry_after`/`exit_time`, env defaults
`OPEN15_NO_ENTRY_AFTER` 09:29 / `OPEN15_EXIT_TIME` 09:30 = the R58-measured
window; exit capped 15:10 so the retry precedes the 15:15 MIS square-off) —
applied at the next 09:10 arm, with the effective window recorded in the day's
`armed` decision-log event.
**Rolling additive watch list (issue #529, default OFF):** when enabled from the
same UI form, the universe is re-ranked on live LTP every `rolling_cadence_s`
(default 30 s, clamped 10–300, **UI-editable** — the operator-facing knob) inside
the entry window and the top-N movers per side are **appended** to the watch
list. Strictly additive (the 09:16 seed picks are never dropped), the entry gate
and `max_trades` cap are untouched, and every addition emits a `watchlist_add`
decision-log event rendered in the "Rolling watch-list" panel on
`/open15_vol_breakout/logs`. Trades carry `open15_trades.watch_source ∈ {seed,
rolling}` so the two cohorts can be scored apart. ⚠ **Measurement, not a
validated edge** — the 2026-08-03 replay showed the 09:16 ranking misses the
day's biggest movers but could NOT show the added names pay (3 incremental
trades on 4 usable days); a promotion decision waits on the #528 sample.
**Broker-rejected entries become PAPER fills (issue #548).** On 2026-08-05
Zerodha rejected three live entries with a static-IP 403; the message was
discarded, `flatten` only ever looked at `status='open'` so the rows sat at
`status='error'` forever with no exit, the rejections ate the whole `max_trades`
cap, and nothing alerted. Now a rejected entry is journaled
`status='rejected'` + **`fill='paper'`** with the broker text in
`error_message`, and at the exit time it is priced exactly as a sandbox run
would have been (exit price, gross P&L, modelled charges) — **no order is ever
placed for it**. Four rules, each load-bearing:
- **`mode` still records what the run genuinely was** (`live`). A paper row is
  never disguised as a sandbox run; `fill` is the axis that says "this did not
  happen".
- **Paper P&L never merges into real P&L.** `total_realized_pnl()` (which drives
  compound sizing) and `trades_pnl_by_date()` exclude `fill='paper'`;
  `paper_pnl_by_date()` reports it separately and the UI badges every paper
  number. Blending them would compound tomorrow's real position size off money
  that was never made.
- **Every reported P&L is NET, and is derived in exactly ONE place** (issue
  #552). `open15_trades.pnl` is gross with modelled charges in `charges_inr`
  (#433), so "the P&L" is a *derived* value — and before #552 four consumers
  derived it independently with three different answers, putting a **gross**
  +₹2109 in the logs-page chip above rows totalling **net** +₹1383.81 (charges
  were 34% of gross), and flipping 2026-07-23's sign (+₹82 gross vs −₹17.82
  net). `net_pnl_expr()` (SQL) and `net_pnl_of_row()` (Python) in
  `database/open15_breakout_db.py` are now the only definition; the `exit` and
  `exit_paper` decision-log events emit the identical `gross`/`charges`/`pnl`
  (net) triple so the page never has to know which kind of exit it is reading.
  **Never write `sum(pnl)` against this journal again** — route through the
  helpers. The regression that catches this class asserts the day digest equals
  the sum of the rows the page renders, which is the invariant nothing
  expressed before (a pre-#552 test pinned the digest at 150.0 three lines
  above the row it renders at 120.0).
- **One date key per day.** The journal's `trade_date` comes from
  `Open15BreakoutService._trade_date()` — the arm-time `_log_date` that
  `save_day_log` also uses — not from a fresh clock read (issue #553). Deriving
  it twice filed a row and its own day log under different dates, which left
  two #548 tests green only on the day they were written.
- **Only an AFFIRMATIVE non-zero position book justifies a square-off.** A 403 is
  unambiguous but a timeout is not, so `flatten` verifies via
  `get_positionbook(mode_key='open15_vol_breakout')` (the #497 rule). A non-zero
  quantity promotes the row back to a real `open` and squares it off; an
  **unreadable** book papers it with a loud warning — sending an unjustified
  square-off would open a naked position, whereas a genuinely-filled lot is
  still caught by the 15:15 MIS auto-square-off.
- **A rejection frees its `max_trades` slot** (it is not a trade), while paper
  fills carry their own cap of `max_trades` so a persistently-rejecting broker
  cannot simulate an unbounded day.
**Reported P&L is reconciled against the broker's own fills, and there are
THREE buckets (issue #555).** Every price the strategy journals is a *decision*
observation — `trigger_price` is the tick that fired the volume gate,
`opt_entry_premium` the option quote at that instant — so before #555 every
published P&L was a quote-derived estimate that had never been checked, and
slippage was unmeasurable. `services/open15_fill_reconcile.py` reads the broker's
`average_price` per leg (via `get_order_status`, which routes to the sandbox or
live book automatically through `sandbox_order_exists`) into
`entry_fill_price`/`exit_fill_price` and **re-derives `pnl`/`charges_inr` in
place**, stamping `pnl_source='fill'` — it does NOT add a second number, so the
#552 one-convention rule survives untouched. It runs at the summary job (exit+5)
and is retried by the next 09:10 arm; **never** in `_enter`/`flatten`, because
entries are placed from the ZMQ tick callback where a synchronous broker call
would stall every other symbol's ticks. Charges stay **modelled** — no broker
exposes per-order charges via API — but on the actual fill turnover, and the UI
labels them as modelled. The quote columns are deliberately NOT overwritten:
`fill − quote` is the slippage this deployment exists to measure. The three
P&L buckets are never summed into one another:
- **real** (`fill='real'`) — money. The only bucket that compounds.
- **paper** (`fill='paper'`) — the broker REJECTED the entry (#548). An order was
  attempted and refused.
- **sim** (`fill='sim'`) — no order was ever attempted (`unaffordable` /
  `max_trades_cap`), priced at **1 lot** so a capped or under-funded day can say
  whether the slot capital or the signal is what limited it.
A sim row places nothing and — unlike a rejection — **never reads the position
book**: nothing was sent, so the book could only surface an unrelated same-symbol
position and promote a trade we never placed into a live square-off. `_REAL_FILL`
is an explicit exclusion **list** (`NON_REAL_FILLS`), not `!= 'paper'`: the old
form silently classified every future class as REAL, which would have compounded
tomorrow's real position size off simulated money. Flags
`OPEN15_FILL_RECONCILE_ENABLED`, `OPEN15_SIM_SKIPPED_ENABLED`,
`OPEN15_PAPER_SIM_MAX`.
**Option liquidity: read every count in LOTS, and the spread is the cost
(issue #555).** Lot sizes across this universe differ ~30× (HAL 150 vs SAIL
4700), so #488's raw `opt_*_volume`/`opt_*_oi` contract counts were never
comparable between contracts — measured 2026-08-06, raw counts made SAIL look
**26× more liquid** than HAL when in lots it is the *smaller* book. That
inversion is the simplest explanation for #488's own note that "every ex-ante
metric ranked the two live trades backwards". `services/open15_liquidity.py`
owns every derivation and never reports a bare contract count. The strategy
sends **MARKET** orders, so it crosses the book twice: `opt_*_bid`/`opt_*_ask` +
`opt_tick_size` are now captured at both decision moments at **zero broker
cost** (the pair always rode in the quote response `_option_liquidity` already
fetched and was discarded). Spread % is of the **MID**, never the LTP — the LTP
is whichever side last traded, so quoting against it moves the metric without
the market moving. ⚠ **Spread cost is reported, never deducted from `pnl`**:
sim/paper rows price both legs at the quote LTP so their net is optimistic by
roughly `spread_cost_inr`, while real rows use fills where the spread is already
inside the price — deducting it would break the #552 single convention and make
every older row incomparable. `opt_liquidity_path` stores per-minute `{m,v,oi}`
over the hold from the 1m bars the option-shadow already fetches (per-bar `v` is
incremental and may be summed; the quote's `volume` is cumulative and must not
be), answering *building vs unwinding* — which endpoint snapshots cannot.
**Nothing gates on any of it.** Flag `OPEN15_LIQUIDITY_PATH_ENABLED`. Kite also
returns `average_price`, `last_trade_time`, whole-book `buy_quantity`/
`sell_quantity` (OpenAlgo's `totalbuyqty` is only the 5 visible levels — 750 vs
105,150 for HAL at one instant), `oi_day_high/low`, circuit limits, the
`low/high_limit_price_protection` band (NSE rejects MARKET orders outside it)
and per-level `orders`; all are dropped by the shared broker mappers, so
capturing them means changing a mapper every broker uses. The WS feed is not a
source — open15 subscribes `NSE_<sym>_LTP` only, which carries neither depth
nor OI.
Alert: `logger.error` + Telegram `open15_breakout`, deduped once per day. The
digest/summary report `filled` and `paper` separately — `core.entered` counts
TRIGGERS and read `5` on a day with zero fills. One-off repair for pre-#548 rows
(journal AND the day log, which is what the `/logs` page renders):
`uv run python -m services.open15_rejection_backfill --date YYYY-MM-DD [--apply]`
(dry-run default, NOT wired into the runtime).

**Ops: boot OpenAlgo before 09:15 IST on trading days** — a late boot skips the
day loudly. Flags `OPEN15_*` (default mode `sandbox`; `observe` = journal-only);
`NOTIFY_OPEN15_BREAKOUT` gates the rejection alert.

## Data freshness validation (sector_follow_cap5_vol)

A durable guard against the class of failure that produced the 2026-05-29→06-10
incident: the sector-index 1m feed sat **12 days stale** because the daily
backfill job did not exist yet, and the hermetic (mocked-data) E2E suite never
noticed an *environmental* regression. The validation layer makes feed staleness
fail loud and fail safe.

**Fix 1b — aggregator-source for today's data + loud failure (2026-06-15).** A
*different* silent-failure class surfaced on the first sandbox cycle: at 15:20 IST
historify had **no stock 1m bars for today** (the backfill convergence only runs
15:30+), so `_series_metrics` returned `stock_ret=None`, every gate failed closed,
and the strategy emitted **0 signals with no alert** — while the WS feed was
ticking the whole `LOCK_STATIC_30` universe into the in-process scanner aggregator
the entire time. The fix splits market data by its natural source:

- **TODAY's intraday close+volume** now come from the **in-process scanner
  aggregator** (`services/sector_follow_service.py` `production_intraday_provider`
  → `ScannerService.get_today_ohlcv(symbol, date)`). All 30 universe stocks are
  already in `SCANNER_SYMBOLS`, so their live bars are in memory. The **20-day
  lookback** (prior close, avg daily volume) stays on historify (the correct
  source for *historical* days). 6 of 8 sector indices are **not** in the scanner
  universe, so their `sector_ret` still comes from historify via the per-symbol
  fallback below.
- **Per-symbol historify fallback** for today's data is kept but logs a **WARNING**
  ("aggregator had no today bars — falling back to historify"). Every metric
  carries `intraday_source ∈ {aggregator, historify, none}`.
- **Loud failure:** `evaluate_candidates` logs per-symbol **PASS/FAIL** with the
  exact gate/None reason, then emits a **decision-input completeness** metric
  (`n_symbols_on_live_intraday / total`): **<50% → Telegram WARNING, <20% →
  CRITICAL**. A silently-degraded pipeline can no longer look like a genuine
  zero-signal day.
- **15:18 IST pre-entry smoke check** (`assert_data_pipeline_healthy`, APScheduler
  job `sector_follow_smoke_check`): (1) aggregator has today's data for
  ≥`SECTOR_FOLLOW_SMOKE_MIN_COVERAGE` (default 0.5) of the universe, (2) the
  historify lookback returns prior-day data for a sample symbol, (3) a broker
  session is live. On failure it writes a same-day `pause` `strategy_runtime_override`
  (holds the 15:20 entries via the engine's `_entry_held_by_override` gate;
  expires 15:30 IST so it self-clears) and Telegram-alerts. Gated by
  `SECTOR_FOLLOW_SMOKE_CHECK_ENABLED` (default `true`). Tests:
  `test/test_sector_follow_service.py` (aggregator read, historify fallback,
  all-empty loud logging, smoke-check abort/alert).

- **Pure service** `services/data_freshness_service.py` — read-only on
  `historify.duckdb`. `check_strategy_data_ready(strategy, date,
  max_staleness_business_days=1)` returns `(ok, per-symbol details)`;
  trading-day aware (weekend gap ≠ stale; NSE holidays are now modelled too,
  issue #253 — the module's own `is_trading_day()` consults
  `database.market_calendar_db.is_market_holiday()`, weekday AND not a market
  holiday; fails open to weekday-only behavior with a once-per-year WARNING
  if the calendar has no rows for a given year, e.g. a future year before its
  yearly seed lands. `scanner_aggregator_seeder`, `scanner_backfill_scheduler`,
  and `sector_follow_backfill_scheduler` all delegate their own
  `_is_trading_day` to this same predicate). For sector_follow it checks the
  8 mapped indices + 30 universe stocks. All
  read-only DuckDB reads go through **`connect_historify_readonly()`**, which
  tries `read_only=True` first but falls back to a config-matching connect when
  the live app already holds `historify.duckdb` open read-write **in the same
  process** — DuckDB's instance cache otherwise rejects the mismatched config
  ("Can't open a connection … with a different configuration"). This was the
  recurring post-close (15:30+) lock-warning spam; the fallback reuses the shared
  in-process connection so the read just succeeds. The same helper backs the
  live 15:20 sector_follow evaluator read (`sector_follow_service`).
- **Table** `data_health_check` in `db/openalgo.db`
  (`database/data_health_db.py`) — one row per check: `check_at`, `overall_ok`,
  `stale_symbols` (JSON), `details_json`, `alert_sent`.
- **Daily 16:30 IST job** `sector_follow_data_health` (runs inside the post-close
  backfill-convergence window). On stale data: Telegram-alerts the operator AND auto-pauses
  *tomorrow's* entries by writing a **`strategy_runtime_override`** row
  (mode-only, B6: `override_type='pause'`, `expires_at=` tomorrow 15:30 IST,
  `reason='stale_feed: …'`, `set_by='sector_follow'`). The engine's job-entry
  gate (`_entry_held_by_override`) enforces it; the persistent `strategy_mode` is
  untouched and **only entries** are held (exits/EOD run). Self-expiring, so a
  one-off stale day never silently disables the strategy beyond tomorrow. (Was:
  `strategy_daily_intent` `intent='pause'` — the intent axis is retired.)
- **Pre-entry gate** in `run_entry` (after the intent gate): aborts entries +
  alerts on a stale index OR stock feed. `run_exit` only *warns* on stale index
  data — exits are never blocked (a held T+1 position is riskier).
- **HTTP** `GET /sector_follow_cap5_vol/api/data_health` — live per-symbol
  freshness (read-only; never writes the row).
- **Feature flag** `DATA_FRESHNESS_VALIDATION_ENABLED` (default `true`) +
  threshold `MAX_STALENESS_BUSINESS_DAYS` (default `1`). See
  `docs/PARAMETER_LOG.md`. The gate/job are no-ops when the flag is off; the
  HTTP endpoint always works.

Two independent universes are kept fresh by **boot-time + periodic
state-convergence checks**: (1) `sector_follow_cap5_vol`'s index + 30-stock 1m
feeds (`services/sector_follow_backfill_scheduler.py`, wired via
`init_sector_follow_backfill`), and (2) the **in-house scanner's
`SCANNER_SYMBOLS` F&O universe in BOTH `1m` AND daily (`D`)**
(`services/scanner_backfill_scheduler.py`, wired via
`init_scanner_backfill_scheduler` — see the dedicated subsection below). The
sector_follow check **supersedes the 16:05/16:10 IST cron jobs** from commit
`5c2a06eff` and earlier — those were removed from `historify_scheduler_service.py`.
Per the directive: *"start once OpenAlgo starts every time and start the task
based on the last backfill timestamp only if required, for index and stocks both,
instead of dependency on a scheduler."*

How it works:
- Each backfill service exposes **`check_and_refresh_if_stale(today)`** — reads
  `MAX(timestamp)` per symbol from `historify.duckdb` (via
  `data_freshness_service.compute_stale_symbols`), and fetches **only the symbols
  behind today's expected 15:30 IST close** through the same incremental historify
  pipeline. Idempotent (fresh → no-op) and fail-graceful (a dead-token fetch is
  `logger.exception`-logged into `errors`, never raised; an anomaly alert fires on
  any error). A **transient DuckDB lock** on the freshness read
  (`is_transient_lock_error` — e.g. a separate CLI backfill holds the file) is
  downgraded to a quiet `logger.info` skip (`status='skipped_locked'`, no Telegram
  anomaly); the arm is treated as *not fresh* so the periodic loop retries rather
  than backing off, and the boot/next-tick convergence catches up.
- **Boot hook**: on every OpenAlgo start, a daemon thread waits for a broker
  session to appear, then runs the index + stock convergence check once (never
  blocks boot). So a restart after the daily ~3 AM Zerodha re-login auto-catches
  up whatever went stale overnight — the self-healing replacement for the missed
  16:10 catch-up that held all entries on 2026-06-12.
- **Periodic loop**: a daemon thread re-checks every
  `SECTOR_FOLLOW_PERIODIC_INTERVAL_MIN` minutes (default 30) inside the
  `15:30`..`SECTOR_FOLLOW_PERIODIC_END_TIME` (default `17:00`) IST window on
  trading days, backing off until the next day once both universes report fresh.
  Gated by `SECTOR_FOLLOW_PERIODIC_CHECK_ENABLED` (default `true`). This closes
  the after-close gap on a day OpenAlgo was already running.

**Manual catch-up** for a deep historical gap (both feeds route through the same
historify pipeline, and both need an active broker session — historical fetch
fails on an expired daily Zerodha token) is still available via the CLIs: index
1m `uv run python -m services.sector_follow_index_backfill --from YYYY-MM-DD --to
YYYY-MM-DD`; stock 1m `uv run python -m services.sector_follow_stock_backfill
--from YYYY-MM-DD --to YYYY-MM-DD`. With the convergence check, the CLI is now
only needed to backfill a multi-day outage beyond the small lookback window — the
boot+periodic path handles routine staleness automatically.

### Scanner-universe feed convergence (`scanner_backfill_scheduler`)

The scanner-side sibling of the sector_follow convergence above. It is the
durable fix for the two **data-supply** bugs the 2026-06-13 Friday-screener
replay surfaced (see
`docs/research/strategy/screener/2026-06-13_friday_replay_with_backfilled_data.md`),
which the sector_follow service does NOT cover because the two universes are
disjoint:

- **Bug A — the scanner universe was never backfilled.** The sector_follow
  convergence refreshes only its locked-static-30 stocks + 8 indices; the
  in-house screener mirrors Chartink across the full `SCANNER_SYMBOLS` F&O list
  (~200 names). Friday 1m existed for only 38/238 historify symbols, so a replay
  could reconstruct only 1 of Chartink's 8 Friday hits.
- **Bug B — the stored daily (`D`) interval was universally stale** (ending
  2026-06-04 for 229 symbols). `ScannerHistoryProvider` reads stored `D` via
  `historify_db.get_ohlcv(interval='D')` for its daily gap/volume gates, so the
  live scanner evaluated against ~6-trading-day-old daily bars on any day,
  independent of tick health.

`services/scanner_backfill_scheduler.py` (+ `services/scanner_universe_backfill.py`)
closes both, mirroring the sector_follow pattern exactly but over **both storage
intervals** (`1m` AND `D`):

- **Symbol set** is derived live from the `SCANNER_SYMBOLS` env var (the same
  source `ScannerHistoryProvider` / `scanner_presubscribe` read), so it tracks
  the scanner config automatically. Each symbol routes to `NSE` or `NSE_INDEX`
  via `scanner_presubscribe.resolve_exchange_for_symbol` (the universe interleaves
  a few indices), and the download goes through the same incremental
  `historify_service.create_and_start_job` pipeline.
- **`check_and_refresh_if_stale(today, interval=...)`** reads `MAX(timestamp)` per
  symbol for the interval from `historify.duckdb` (via
  `data_freshness_service.compute_stale_symbols`, which already accepts an
  `interval` argument) and fetches **only the symbols behind today's close**.
  On the `1m` arm the predicate is additionally **session-coverage-aware**
  (issue #380, flag `SCANNER_BACKFILL_SESSION_COVERAGE_ENABLED` default `true`):
  a symbol whose last 1m bar is dated today but stops short of the elapsed
  session (minus a 15-min broker-lag grace, mirroring the seeder's
  `_has_sufficient_today_coverage`) is stale too — pre-#380 a lone 09:16 bar
  counted the symbol fresh all day, the post-close loop never backfilled the
  session, and every `scanner_aggregator_seeder` run fell back to ~225
  per-symbol broker calls. The `D` arm and the sector_follow backfills keep the
  date-granular semantics.
  Idempotent (fresh → no-op), fail-graceful (a dead-token fetch is logged into
  `errors`, never raised), empty-universe-safe (no-op when `SCANNER_SYMBOLS` is
  unset).
- **Boot hook + periodic loop** identical in shape to sector_follow: a daemon
  thread waits for a broker session, runs the 1m then `D` convergence once
  (never blocks boot), then a periodic daemon re-checks every
  `SCANNER_BACKFILL_PERIODIC_INTERVAL_MIN` minutes (default 30) inside
  `15:30`..`SCANNER_BACKFILL_PERIODIC_END_TIME` (default `17:00`) IST on trading
  days, backing off until tomorrow once both intervals report fresh.
- **Health rows**: each interval check writes a `data_health_check` row
  (`strategy_name='scanner_universe_1m'` / `'scanner_universe_D'`) via the
  existing `database/data_health_db.insert_check` — no schema change, so
  `get_latest_check` / the existing query + alerting paths keep working and
  scanner coverage is directly queryable.
- **Flags**: `SCANNER_BACKFILL_ENABLED` (master, default `true`),
  `SCANNER_BACKFILL_PERIODIC_CHECK_ENABLED` (default `true`),
  `SCANNER_BACKFILL_PERIODIC_INTERVAL_MIN` (default `30`),
  `SCANNER_BACKFILL_PERIODIC_END_TIME` (default `17:00`),
  `SCANNER_BACKFILL_INTERVALS` (default `1m,D` — drop one arm to reduce broker
  load), `SCANNER_BACKFILL_SESSION_COVERAGE_ENABLED` (default `true` — the #380
  rollback switch). See `docs/PARAMETER_LOG.md`.
- **Manual catch-up** for a deep historical gap — notably the one-time initial
  deep 1m backfill for the ~200 never-fetched scanner symbols, which is beyond
  the small lookback window — uses the CLI (needs an active broker session):
  `uv run python -m services.scanner_universe_backfill --from YYYY-MM-DD --to
  YYYY-MM-DD --interval 1m` (or `--interval D`).
- **Daily-D approach (Approach 1):** the `D` arm fetches fresh daily bars
  directly through the historify pipeline. A future task may migrate to deriving
  daily by resampling the continuous stored 1m series (Approach 2, single source
  of truth) — that only becomes viable once the full-universe 1m deep backfill
  has landed.
- **Daily-D re-settle (issue #299, 2026-07-02):** the incremental convergence
  above can *never* correct a daily bar that was written **intraday** as a
  provisional/running close (the #277 09:45-freeze class) — `compute_stale_symbols`
  sees a bar for the day already present and the incremental download SKIPS it, so
  the provisional close persists into the scanner's `yest_d` gate and manufactures
  phantom gap signals (the 2026-07-02 DELHIVERY false BUY: stored 07-01 close
  475.4 vs broker-settled 507.7). `resettle_recent_daily` fixes this: once per
  process per date (before the stale-check, at boot + post-close), it forces a
  **non-incremental** overwrite re-fetch of the last `SCANNER_DAILY_RESETTLE_DAYS`
  (default 2) settled trading days for the whole universe — the broker's daily API
  returns the settled close post-close, and the `upsert_market_data`
  ON-CONFLICT-DO-UPDATE write overwrites the provisional bar in place — then calls
  `ScannerHistoryProvider.refresh()` so the corrected close reaches the live
  scanner **without** a restart (the provider caches daily-D at boot and never
  re-reads during the session). Gated by `SCANNER_DAILY_RESETTLE_ENABLED` (default
  `true`); wired via `scanner_backfill_scheduler._maybe_resettle_daily`. Manual
  one-shot: `uv run python -m services.scanner_universe_backfill --resettle`.
  Additive, idempotent, fail-graceful. A follow-up (not yet shipped) adds a
  defense-in-depth rule-level guard that rejects when the derived `yest_d` bar is
  not the actual previous trading day, for the case where the re-settle can't run
  (broker session down).

The learning loop: Morning scan → Arm engine → Monitor trades → EOD results →
Compare vs backtest → Record in LEARNINGS.md → Improve strategy → Repeat.

### In-house screener observability — Tier-1 hardening (2026-06-15)

The in-house scanner (`services/scanner_service.py`) is purely event-driven and
historically **failed closed, silently** — every missing input became a bare
`return False`/`(None, None)` with no log, so "tick-starved feed" and "genuinely
quiet market" produced byte-identical zero-hit logs. Tier-1 of the Phase-B plan
(`docs/research/strategy/screener/2026-06-15_inhouse_deep_analysis.md`) applies
sector_follow's Fix 1b disciplines (loud failure + completeness metric +
market-hours gate) to the scanner. **All three are additive — they change *what
is observed/skipped*, never *which signals fire***:

- **Market-hours gate** (`_evaluate_definitions`): skips evaluation with an INFO
  log outside `[09:15, 15:30]` IST, so a straggler/backfill tick that closes a
  bar after the session cannot fire a stale-bar signal (the 2026-06-15 17×
  post-close AUROPHARMA SELL class, FM-6). Flag `SCANNER_POSTCLOSE_GATE_ENABLED`
  (default `true`). `_now_ist()` is indirected for testability.
- **D-bar-date verify** (both `services/scan_rules/fno_intraday_*_chartink.py`):
  post-settle (`today_idx == -1`), the rule aborts with a WARNING when its latest
  daily-D bar is dated *before* today — the stale-D condition that let a prior-day
  bar masquerade as "today's settled bar." Flag `SCANNER_DBAR_DATE_VERIFY_ENABLED`
  (default `true`); gated on the production `timestamp` column so synthetic test
  frames are exempt. Paired with the market-hours gate so a future change to
  either one cannot silently re-open the post-close path.
- **Loud per-symbol PASS/FAIL + missing-input logging**: `_evaluate_definitions`
  logs `scanner PASS <sym>` at INFO (rare) / `scanner FAIL <sym>` at DEBUG (per-bar
  firehose); `get_today_ohlcv` logs a reason instead of a silent `(None, None)`;
  the rules log a WARNING naming the symbol + which input is `None` (data missing)
  vs DEBUG for a short-but-present warm-up frame; `scanner_universe_backfill`'s
  `check_and_refresh_if_stale` WARNs with the affected symbols + reason on a failed
  catch-up (FM-11: an expired token could fail every symbol with only a quiet error
  key as the trace).
- **Per-cycle decision-input completeness metric** (`_record_completeness` /
  `_emit_completeness`): the scanner accumulates the set of symbols that produced a
  live bar within a rolling `SCANNER_COMPLETENESS_WINDOW_MIN` (default 5) minute
  window and, when the window rolls, emits `n_live / total_subscribed` — **<50%
  WARNING, <20% CRITICAL** via Telegram (`notification_service.notify`,
  `scanner_completeness` event, per-severity once-a-day dedup). This is the single
  change that makes "0 hits because no data" visually distinct from "0 hits because
  quiet market." Flags `SCANNER_COMPLETENESS_ENABLED` (default `true`),
  `SCANNER_COMPLETENESS_WARN_PCT` (50), `SCANNER_COMPLETENESS_CRIT_PCT` (20),
  `SCANNER_COMPLETENESS_WINDOW_MIN` (5). **Limitation:** a *total* feed outage
  produces no bar closes at all, so this path never fires — that case is the 15:18
  smoke check's job (Tier 2, not yet shipped). The metric catches *partial*
  degradation and reports coverage. See `docs/PARAMETER_LOG.md` for all flags.

**Tier-2 — reference-data contract (issue #305, 2026-07-02).** Tier-1 made
failures *visible*; Tier-2 makes the reference data *enforceable* — the durable
fix for the 2026-07-02 DELHIVERY incident (BUY fired 42× while DOWN because the
rule's `yest_d.close` came from a stale historify-D slot diverging 6.8% from the
broker-known prior close, with every existing guard alert-only). Three parts:

- **Broker prev-close registry** (`services/scanner_reference_data.py`): the
  boot `aggregator_seeder` already fetches broker 1m bars per symbol (broker
  fallback arm) — it now also records each symbol's **T-1 settled close** (the
  last broker bar dated before today) into an in-process, day-scoped registry
  (only same-IST-day recordings are served; no new broker API load).
- **Reference certificate at the choke point**:
  `ScannerService._evaluate_definitions` computes — once per (symbol, bar
  close), centrally, so the rules stay pure — the settled reference the rules
  will use (shared `derive_today_and_yest` helper) and cross-checks it against
  the registry. The verdict rides the indicators dict (`reference_certified`,
  `reference_divergence_pct`, value keys). Both Chartink rules gate on it early:
  an **explicit `False`** (divergence > `SCANNER_REFERENCE_DIVERGENCE_MAX_PCT`,
  default 1.0) rejects the symbol with a dedup'd per-(symbol, day) WARNING + a
  CRIT Telegram via `source_divergence_alerts.check_and_alert`
  (service=`scanner_reference`, shared BUY/SELL so one alert per symbol/day). A
  **missing key is certified** (backward compat) and a missing broker prev-close
  is **fail-open** (certified + dedup'd WARNING) — fail-closed only on a
  *confirmed* divergence. Flag `SCANNER_REFERENCE_CHECK_ENABLED` (default `true`).
- **Smoke-fail post-hold**: a FAILED 09:18 smoke check
  (`scanner_smoke_check_service`) now arms an in-process, day-scoped hold —
  the scanner keeps evaluating and logs `scanner PASS`, but the hit is NOT
  written to `scan_results` nor posted to the engine (`scanner HELD <sym>
  reason=smoke_check_failed` WARNING, dedup'd per symbol/day). The hold lifts
  when a re-check passes (`re_check_and_release`, invoked by the
  `scanner_backfill_scheduler` periodic convergence tick after it refreshes the
  stored 1m/D data) or self-expires at 15:35 IST. Exits/other strategies are
  unaffected — only scanner hit posting is gated. Flag
  `SCANNER_SMOKE_BLOCK_ENABLED` (default `true`, consult-time so a runtime flip
  takes effect immediately). Tests: `test/test_scanner_reference_data.py`,
  `test/test_scanner_smoke_check.py`, and the golden-incident cases in
  `test/test_fno_intraday_{buy,sell}_chartink.py`.

  **Per-symbol hold + mid-session straggler heal (issue #390, 2026-07-10).**
  The post-hold used to be all-or-nothing: on 2026-07-08 just **3 of 216**
  1m symbols stayed stale after the 09:16 pre-entry refresh (broker
  current-day API lag tail), the 09:18 smoke check FAILED, and the hold
  darked the **entire** scanner all session — 0 `scan_results`, 68 HELD
  events, released only at 15:36 (after close) when the 15:30-17:00 periodic
  loop finally re-checked. Two fixes:
  - **A — the hold is now PER-SYMBOL.** On a FAIL the smoke check reads the
    stale symbol SET from the stored-freshness rows'
    (`scanner_universe_1m`/`_D`) own `stale_symbols` lists (the #338
    post-refresh still-stale sets) and stores it on the hold.
    `scanner_service._hit_held_by_smoke(symbol, …)` now holds a hit **only if
    that symbol is in the stale set** — the fresh 213 post normally.
    **Total-hold is preserved** for a genuine dead-feed morning: aggregator
    coverage gate (gate 1) fail, broker session down (gate 4), a freshness
    gate that names no symbols, or a stale fraction above
    `SCANNER_SMOKE_TOTAL_HOLD_PCT` (default 0.5) all hold EVERYTHING (stale
    set = `None` sentinel). A legacy symbol-less `set_post_hold` / a
    symbol-less `is_post_hold_active` = total hold (backward-compatible).
  - **B — mid-session straggler tick.** The convergence periodic loop only
    runs 15:30-17:00, so morning stragglers couldn't self-heal intraday. A
    new **daemon loop** (`_straggler_tick`/`_straggler_loop` in
    `scanner_backfill_scheduler.py`) runs every `SCANNER_STRAGGLER_RECHECK_MIN`
    (default 15) min inside **09:20-15:30 IST** on trading days (holiday-aware,
    broker-session-gated), reuses the existing convergence machinery
    (`run_backfill_checks(resettle=False)` → `check_and_refresh_if_stale`,
    which no-ops cheaply when nothing is stale), persists the verified health
    rows, then invokes `re_check_and_release()` — so a 3-straggler morning
    heals within one tick (~15 min) and the hold narrows/clears intraday
    instead of at 15:30. Gated by `SCANNER_STRAGGLER_RECHECK_ENABLED`
    (default `true`). New flags: `SCANNER_SMOKE_TOTAL_HOLD_PCT`,
    `SCANNER_STRAGGLER_RECHECK_ENABLED`, `SCANNER_STRAGGLER_RECHECK_MIN`
    (see `docs/PARAMETER_LOG.md`). Tests:
    `test/test_scanner_smoke_check.py` (partial vs total),
    `test/test_scanner_service.py` (per-symbol enforcement),
    `test/test_scanner_watchdog_and_backfill_gate.py` (straggler tick).

## Scheduler + daemon-thread registry (`/admin/schedulers`, issue #539)

The single live answer to "what is scheduled, and is it actually running?"
Phase 1 is **read-only observability**; controls are Phase 2.

**Why a catalog and not just introspection.** Scheduling is spread across
**seven** APScheduler instances (shared historify, EOD watchdog, sandbox
square-off, Flow, python_strategy, chartink, strategy) plus ~32 long-lived
daemon threads, several of which are cron jobs in all but name. Enumerating
live jobs misses the row that matters most: a job whose registration was
skipped by an env flag is simply **absent**, which is exactly the case an
operator is trying to explain ("why did nothing happen at 16:00?").

- **`services/scheduler_registry.py`** — declarative `CATALOG` of `JobSpec`s
  merged at read time with live `get_jobs()` across all seven instances and the
  last fire from `job_run`. States: `registered` / `not_registered` (catalogued
  but absent) / `unregistered` (live but uncatalogued — user-defined historify,
  python, flow and chartink schedules land here without needing entries).
  Resolution reads `sys.modules` and **never imports** — importing
  `blueprints.python_strategy` runs a strategy cleanup, so an observability call
  that imported it would have real side effects.
- **`services/thread_registry.py`** — same shape for daemon threads, by class
  (`loop` / `transport` / `poller` / `boot`), plus an **in-memory heartbeat**
  (`beat()`) stamped at the top of each recurring loop's tick. The heartbeat is
  the point: `Thread.is_alive()` stays `True` forever for a thread wedged on a
  socket read, so liveness alone proves nothing. States: `running` / `stale` /
  `dead` / `not_started` / `completed`.
- **Alerting is deliberately narrow** and rides the existing `ThreadWatchdog`
  30 s loop (no new thread to watch threads): only a thread that **beat at least
  once and then went silent or vanished** alerts. `not_started` never alerts,
  because it is the normal state for most catalog entries on a normal install
  (no broker session, outside the window, flag off, bot not configured) — daily
  noise is how an alert channel gets ignored. Flags `THREAD_REGISTRY_ENABLED`,
  `THREAD_HEARTBEAT_STALE_MULTIPLIER` (3.0), `THREAD_REGISTRY_ALERT_DEDUP_MIN`
  (30), `NOTIFY_THREAD_REGISTRY`. See `docs/PARAMETER_LOG.md`.
- **This closes a real gap.** `thread_watchdog_service` alerts on a thread
  *leak* (count climbing); with no expected-set it structurally cannot detect
  the opposite — a thread that died or wedged, the 2026-07-07 silent tick-flow
  death shape.
- **Tiers** are recorded now, enforced in Phase 2: `protected` (exits, EOD
  flattens, square-offs, daily resets, market-hours enforcer — never disableable
  from the UI; each carries a `safety_note` saying why, because the reason is
  what stops a future maintainer from downgrading it), `guarded` (typed
  confirmation + mandatory expiry), `free`.
- **API + UI:** `GET /admin/api/schedulers` (`@check_session_validity`,
  read-only, each half degrades independently into `sources_failed`) rendered by
  `frontend/src/pages/admin/Schedulers.tsx`, reachable from the Admin dashboard
  card at `/admin`.

**⚠ When you add a scheduled job or a long-lived thread, add it to the matching
catalog in the same commit.** This is enforced, not advisory:
`test/test_scheduler_registry.py` parses every `add_job(..., id="X")` literal in
`services/`, `blueprints/` and `sandbox/` and fails on anything uncatalogued;
`test/test_thread_registry.py` does the same for `Thread(name="X")` literals.
A catalog that falls behind the code answers "what runs?" *wrongly*, which is
worse than not answering — so the tests treat drift as a build failure. Long-
lived threads must also be **named** (`Thread(..., name="X")`); the name is the
only join key against a live process.

**Not yet built** (Phase 2+, separate issues): durable `scheduler_job_control`
table, fire-time guard wrapper, per-tick loop gate, tiered toggles, run-now,
per-job fire history, and post-market expectation contracts that read the
control table so an operator-disabled job is not reported as a failure.

## Daily post-market review (`postmarket_review`) + job-run audit

An in-process APScheduler job (**17:15 IST mon-fri, trading days only**) that
records what the day actually did, and the `job_run` audit table it stands on.
Issue #511, Phase 1 of a phased build.

**Why it exists.** Two failures motivated it. (1) `futures_follow_cap50` T+1
exits were silently dead for **four trading days** (2026-07-27..30, issue #497)
while open NRML lots climbed to 110% of book — every individual layer looked
healthy. (2) `journal_reflection`, the 16:00 IST nightly LLM job, has run **three
times in two months** against a daily mon-fri schedule: it posts to the Cowork
bridge on :5001, which is normally down, and fails silently. Nobody noticed
because **nothing recorded whether a scheduled job fired**.

**The load-bearing rule for every later phase: Python detects, the LLM triages.**
Invariants are asserted in code and are either true or false; the model only
ranks severity, correlates, and drafts prose. It never decides whether something
is broken. That split is what stops the report from inventing problems on a quiet
day — and it is why Phase 1 ships with **no verdicts and no LLM call at all**.

- **`job_run` audit** (`database/job_run_db.py` + `services/job_run_audit.py`) —
  one row per APScheduler fire (`ok` / `error` / `missed`, with `duration_ms`,
  `scheduled_at`, exception text). The listener attaches to the shared Historify
  scheduler in `app.py` **immediately after `init_historify_scheduler` and before
  any service registers jobs**, so a boot-time fire is still captured. Two
  non-obvious details, both regression-tested: the SUBMITTED event spells it
  `scheduled_run_times` (a *list*) while EXECUTED uses `scheduled_run_time`
  (singular) — keying only the singular form silently produced `duration_ms=None`
  for every job; and job names are cached on `EVENT_JOB_ADDED` because a one-shot
  (`date`-trigger) job is removed from the jobstore *before* it is submitted, so
  a later `get_job` lookup returns None. Pruned to `JOB_RUN_RETENTION_DAYS`
  (default 90) by the review job. Flag `JOB_RUN_AUDIT_ENABLED` (default `true`).
- **Day digest** (`services/postmarket_day_digest.build_day_digest`) — read-only
  aggregation over `job_run`, `trade_journal` (per strategy/direction, incl.
  open-at-EOD and #350-style unpriced exits), `signal_decision`, `scan_results` +
  `scan_cycle`, the four per-strategy journals, `data_health_check`,
  `scanner_comparison`, `account_orders`, `sandbox_orders`, and the compacted
  logs. **Every section is independently wrapped**: a dead source degrades to
  `None` and is named in `sources_failed` rather than killing the run.
  The **futures carry walk** is the #497 detector: exits are *separate SELL rows*
  (`exit_price` is never back-filled onto the BUY leg), so carry is a FIFO lot
  balance — replaying 2026-07-24..30 yields open lots `1→2→4→6→8` with
  `exits_today=0` every day and `carry_age_days` reaching 13 on a T+1 strategy.
- **Log compaction** (`services/postmarket_log_digest`) — the daily text log is
  **~5 MB** and `errors.jsonl` is **capped at 1000 lines and truncated on every
  app startup**, so raw logs can never go into a digest (or, later, a prompt) and
  error history is otherwise lost on restart. Errors bucket by
  `(logger, normalized_message)`; the text log is streamed for level counts and
  known structured markers only — **raw log text never enters the digest**. A
  durable `log/error_digest_<date>.json` snapshot is merged **max-wise** on each
  run, which both survives the truncation and keeps re-runs idempotent (summing
  would double-count everything already captured).
- **Persistence + report** — one `postmarket_review` row per date (idempotent
  delete-then-insert) plus a Telegram summary via `notify("postmarket_review")`.
  A **non-trading day returns early and persists nothing** — an empty row for a
  Saturday is noise, not a record.
- **Replay CLI** — `uv run python -m services.postmarket_review_service --date
  YYYY-MM-DD --dry-run`. Use it to develop against known-bad days before
  anything runs live.
- **Flags:** `POSTMARKET_REVIEW_ENABLED` (default `true`, per-fire),
  `POSTMARKET_REVIEW_TIME` (default `17:15`), `NOTIFY_POSTMARKET_REVIEW`,
  `JOB_RUN_AUDIT_ENABLED`, `JOB_RUN_RETENTION_DAYS`. See `docs/PARAMETER_LOG.md`.
  **17:15 is not arbitrary** — the scanner backfill convergence loop runs until
  17:00 (`SCANNER_BACKFILL_PERIODIC_END_TIME`); reviewing earlier reads
  half-written data. Move one and you must move the other.

**Phase 2 — expectation contracts (issue #532, `services/strategy_expectations.py`).**
This is what turns the digest from a report into a **verdict**. `EXPECTATIONS` is
a registry of declarative `Expect(contract_id, strategy, predicate, severity,
requires, shape, ...)` entries; `evaluate_expectations(digest)` returns
violations, which lead the Telegram summary and persist to
`postmarket_review.violations_json` / `n_violations` / `contracts_json`.

Four rules, each load-bearing — break one and the report stops being trusted:

- **Contracts read the digest ONLY, never the DB.** That is what makes every
  contract replayable against any past day through the CLI, and testable without
  fixtures for ten tables.
- **Missing input is `unknown`, NEVER `fail`.** A degraded digest section means
  *our collection* broke, not that a strategy misbehaved. Contracts declare
  `requires` paths and short-circuit to `unknown`. Related: a contract that
  depends on `job_run` checks `audit_earliest_date` first, so replaying a day
  from before the audit shipped reports `unknown` instead of a phantom "nothing
  fired". Accusing a strategy of a collection gap is how a report earns being
  ignored.
- **Fingerprints exclude observed values.** `strategy|contract_id|shape` only.
  The #497 outage failed the same contract on four consecutive days with
  different lot counts (2/4/6/8); Phase 4 must see one recurring problem, not
  four. Renaming a `contract_id` re-files its issue — treat ids as an API.
- **A raising predicate is contained as `unknown`.** One broken rule must not
  take down the report it is part of.

Evidence it works, from replaying real history: **2026-07-17** (a healthy day
with a real exit) produces **zero** violations, while **2026-07-27** — day *one*
of the #497 window — fires `futures_follow_cap50/t1_exit_for_carry` as **P0**,
and all four broken days share the identical fingerprint. The bug that took four
trading days to notice is caught on the first.

Flags: `POSTMARKET_CONTRACTS_ENABLED` (default `true`) and
`POSTMARKET_CONTRACTS_DISABLED` (comma-separated `contract_id` or
`strategy:contract_id`) — the latter silences one contract surgically so a rule
that starts crying wolf can be muted same-day without a code change. A muted
contract should get an issue to fix or delete it: a permanently disabled contract
is worse than no contract, because the report still *looks* complete.

**Phase 3 — LLM triage (issue #534, `services/postmarket_triage.py`).** Adds
judgment on top of the verdict: a `claude -p` pass over the Phase 2 violations
returning a day assessment, per-violation likely-cause / recurrence / recommended
action, and draft issue title+body. It uses the **same in-process seam as the
Stage-1 veto** (`llm_review_client.invoke_claude_review` — a blocking subprocess
on a real, unpatched OS thread, because the app runs under eventlet). Persisted
to `triage_json` / `llm_status` / `llm_latency_ms`.

- **The model cannot create findings — enforced structurally, not by prompt.**
  Every triage entry must carry a `fingerprint` that was in the input; entries
  with an unrecognised fingerprint are dropped and logged. A model that invents
  a problem produces nothing, whether it hallucinated or was steered there by
  text injected into the logs it was shown. The model's `severity_assessment` is
  **advisory** — the contract's own severity stays authoritative.
- **Untrusted input.** Violation summaries and error templates originate in logs
  containing arbitrary third-party text. Mitigations in order of real weight:
  (1) the output is used only as text and ranking — never a command, path, shell
  argument or filing decision (Phase 4 builds its `gh` argv in Python from a
  fixed template regardless of what comes back); (2) log content rides inside a
  delimited `<untrusted_log_data>` block; (3) the fingerprint allow-list above.
  Prompt wording is not treated as a security boundary.
- **Failure is LOUD.** Unlike the veto — which fails *open* to "take" on purpose
  so a dead LLM never blocks a trade — a failed triage must be visible.
  `llm_status ∈ ok | skipped_clean_day | skipped_disabled | not_logged_in |
  cli_missing | unreachable | timeout | parse_failed | error`, and a non-skip
  failure is stated in the Telegram summary. A silent no-op is exactly how
  `journal_reflection` hid a dead schedule for two months.
- **Clean days do not call the LLM** by default
  (`POSTMARKET_TRIAGE_ON_CLEAN_DAYS=false`): with nothing proven broken the
  output is speculative and Phase 4 would not file it.
- **Operator prerequisite:** the `claude` CLI must be **logged in on the host**
  (subscription auth via the CLI — there is no API key in this codebase).

**Two logged-out-CLI bugs fixed in `llm_review_client` while building this
(#534)** — both affected the live Stage-1 veto too. The CLI reports "not logged
in" in two shapes and neither reached the caller as an error: (1) **exit 0 with
`is_error: true`** in the envelope, whose `result` ("Not logged in · Please run
/login") was returned as if it were the model's answer; and (2) **exit 1 with an
EMPTY stderr**, the diagnostic being in *stdout*, which produced the useless
message `"claude review exited 1: "` and cost `_AUTH_MARKERS` its only evidence
— so the operator was told "error" instead of the one-command fix. `_parse_envelope`
now raises on `is_error`, and a non-zero exit falls back to the stdout envelope's
`result`. Regression tests in `test/test_llm_review_client.py`.

**Phase 3b/4 — investigating agent + issue filing (issue #536).** The agent now
gets **read-only access to the source tree and the logs** and does two jobs:
**verify** each deterministic violation against the actual code (`confirmed` /
`refuted` / `unverified`, citing `file:line`), and **propose** problems the
contracts do not cover. Confirmed findings are filed as GitHub issues.

- **Seam:** `services/llm_agent_client.py` — same unpatched-OS-thread subprocess
  pattern as the bare client, but with `--allowedTools Read Grep Glob`,
  `--disallowedTools Bash Write Edit MultiEdit NotebookEdit WebFetch WebSearch
  Task`, and CLI-level `--settings` deny rules on `.env*`, `db/`, `.git/`,
  `*.key`, `*.pem`. **The deny-list is load-bearing, not tidy:** `.env` holds
  `API_KEY_PEPPER`/`FERNET_SALT`, every encrypted secret in `openalgo.db` is
  sealed against them, and the pipeline's whole purpose is to *publish* what the
  agent writes. Read access plus a publish path is an exfiltration channel.
- **Evidence replaces the fingerprint allow-list.** Phase 3 kept the model honest
  by only letting it speak about violations Python had proven; that has to relax
  once it can find new things. Instead: every finding must cite a repo path (or a
  log template present in today's digest), and **cited paths are checked against
  the filesystem in Python** — a confident citation of a file that isn't there is
  the cheapest hallucination tell there is. Uncited findings are dropped.
- **`services/publish_guard.py`** — `detect-secrets` plus an explicit pattern set
  for this install's crown jewels, run over any model-authored text before it
  leaves the machine. **Fails closed**: if the scanner cannot run, publishing is
  refused. A scanner that degrades to "looks fine" is worse than none, because it
  is trusted.
- **`services/github_cli_client.py`** — the only path to GitHub. An **allow-list
  of verbs** (`list/view/create/comment/edit/reopen/close`), argv built from fixed
  templates, every body guarded. `pr merge`, `push`, `workflow run`, `release` are
  structurally unreachable, so **model output is only ever a value inside a fixed
  template, never part of the command shape**.
- **Filing:** only `confirmed` + `worth_filing` findings become issues — an issue
  asserts something *is* wrong, so refuted/unverified findings are reported to the
  operator and never filed. Recurrences comment on the existing issue (matched via
  a `<!-- postmarket-fingerprint: … -->` body marker) rather than opening a
  second. Rate-capped by `POSTMARKET_MAX_ISSUES_PER_DAY` with overflow appended to
  `audit/proposed_fixes.jsonl` — the cap bounds noise, never the record.
  **`POSTMARKET_FILING_MODE` defaults to `dry_run`.**
- **Read-only on code, always.** The agent never edits, branches, or commits —
  the same carve-out the Cowork scheduled tasks run under. A human owns every fix,
  and each filed issue says so.

**Not yet built:** the closed-issue validation sweep (#537 — the agent runs the
acceptance checks it can, posts evidence, and reopens on failure; it must never
tick a box it did not actually verify). Tests:
`test/test_job_run_audit.py`, `test/test_postmarket_review.py`,
`test/test_strategy_expectations.py`, `test/test_postmarket_triage.py`,
`test/test_postmarket_investigation.py`.

## Scanner-vs-Chartink EOD comparison (`scanner_comparison_eod`)

A daily in-process APScheduler job (**15:45 IST mon-fri**) that scores how the
in-house scanner's BUY/SELL hits matched the Chartink lists posted via webhook.
It is the durable replacement for the retired Cowork-side
`scanner-vs-chartink-daily-comparison` scheduled task, which ran read-only but
silently failed in the sandbox (no repo/folder access). Moving it inside OpenAlgo
— where both sides' data already live — means the result is written AND
Telegrammed every trading day.

- **Service** `services/scanner_comparison_eod_service.py` — read-only on every
  DB except its own table. `compute_comparison(date)` unions the Chartink side
  (`scan_cycle`, `cycle_kind='chartink'` → `screener_buy`/`screener_sell`) and the
  in-house side (`scan_results`, `source='inhouse'`, grouped by the joined
  `scan_definition.screener_type`), then computes per-side counts, intersection,
  Jaccard, recall ratio, top diff names, and a one-line tuning verdict.
  `run_comparison_for_date(date)` persists + Telegrams.
- **Table** `scanner_comparison` in `db/openalgo.db`
  (`database/scanner_comparison_db.py`) — one row per `(date, screener_side)`;
  idempotent delete-then-insert per date+side, so re-running the day overwrites.
- **Telegram** routes through `notification_service.notify("scanner_comparison", …)`
  so the Phase 6 inbound-bot fallback delivers; toggle `NOTIFY_SCANNER_COMPARISON`
  (default `true`).
- **Flags** `SCANNER_COMPARISON_EOD_ENABLED` (default `true`, per-fire gate) +
  `SCANNER_COMPARISON_EOD_TIME` (default `15:45`). See `docs/PARAMETER_LOG.md`.
- **Caveat:** the in-house side reflects the *live tick-driven* scanner, which only
  sees ticks the engine subscribed (the "in-house scanner starved" learning) — a
  fully-disjoint result usually means tick starvation, not a threshold mismatch.
  The tuning verdict calls this out.
- **Echo exclusion (issue #447):** the in-house `ScanHitPoster` posts scanner
  hits into the *same* simplified-engine webhook Chartink uses. Those payloads
  carry `source='inhouse_scanner'` and the webhook audits them as
  `cycle_kind='inhouse_echo'`, so the comparison's Chartink side (and the #321
  miss-debug set in `scanner_service._today_chartink_symbols`) sees only genuine
  Chartink cycles. Before 2026-07-24 the echoes were recorded as `'chartink'`
  with SELL hits misfiled into `screener_buy` (the webhook's old
  whitespace-token side check vs the poster's single-token
  `fno_intraday_sell_20` scan name) — every `scanner_comparison` row from
  ~2026-07-01 to 2026-07-24 was self-referential. The side
  check now tokenizes on non-alphabetic chars (SELL/SHORT/COVER), mirroring the
  engine's `_infer_direction`, so audit and armed direction cannot diverge.
  **The polluted window was repaired on 2026-07-24** (issue #449): the one-time
  operator CLI `services/scanner_comparison_echo_backfill.py` (dry-run default,
  `--apply` to write) reclassified the 5,412 pre-fix echo rows heuristically
  (single-symbol `chartink` row matching an in-house `scan_results` hit within
  ±3 s) and recomputed the 18 affected `scanner_comparison` days without
  Telegram. NOT wired into the runtime.

Registered at boot in `app.py` next to `init_sector_follow_service`. One-shot
backfill / re-run for a past day: `run_comparison_for_date(date='YYYY-MM-DD')`.

## Simplified Stock Engine

This project hosts the simplified stock engine — a Chartink-driven intraday
strategy that arms long/short watches and fires market orders via 5-minute
candle breakouts with ATR-based stop loss and RR trailing. The engine has its
own mode flag (`SIMPLIFIED_ENGINE_MODE`) and is independent of the global
`analyze_mode` toggle.

For deep context — the integration plan, every design decision, env var
reference, test status, and the work queue for picking it up fresh — see
[docs/SIMPLIFIED_ENGINE_HANDOFF.md](docs/SIMPLIFIED_ENGINE_HANDOFF.md).

For strategy-specific learnings, parameter history, and daily results, see
[`strategies/simplified_engine/LEARNINGS.md`](strategies/simplified_engine/LEARNINGS.md).

Key files:
- `services/simplified_stock_engine_core.py` — broker-agnostic engine.
- `services/simplified_stock_engine_service.py` — openalgo integration.
- `services/simplified_stock_engine_ticklog.py` — async tick log writer.
- `services/engine_eod_reconciliation_service.py` — EOD reconciliation (below).
- `blueprints/chartink.py:947+` — webhook, status, direction-toggle routes.
- `test/test_simplified_stock_engine_*.py` — 68 tests.

The default mode is `sandbox` — orders flow into `sandbox.db` (virtual ₹1Cr
capital) regardless of the global analyze_mode flag. Flip to `live` only
after running the test suite and the smoke-boot checklist in the hand-off doc.

**EOD journal reconciliation.** The engine only writes a `trade_journal` exit row
when *it* fires an exit (stop/target/trailing/its own EOD flatten). In sandbox
mode, positions still open at the close are flattened by sandbox's own MIS
auto-square-off, which the engine never journaled — so the Telegram EOD summary
under-counted trades and P&L (the 2026-06-10 bug: +₹352 shown vs +₹8,327 real).
`services/engine_eod_reconciliation_service.reconcile_engine_journal()` closes
that gap: before the Telegram EOD summary fires, `_maybe_log_eod_summary` calls
`_maybe_reconcile_eod_journal(today)` (reconcile → summarize), which reads
`sandbox.db` **read-only** and stamps the missing exit rows
(`exit_reason='sandbox_eod_squareoff'`, gross P&L). A second pass (issue #350)
prices **watchdog-stamped exits**: the EOD watchdog closes journal rows with
`exit_price=None` (it doesn't wait for the fill), which made their P&L read ₹0
on the strategies dashboard while `/positions` had the real number — the pass
backfills `exit_price`+`pnl` from the sandbox fill matching the stamped
`exit_order_id` (order-id matching, so a same-symbol earlier stop-loss fill is
never blended in). Idempotent, mid-day safe,
sandbox-only, gated by `ENGINE_EOD_RECONCILIATION_ENABLED` (default true). A
past-date operator backfill lives in
`services/engine_eod_reconciliation_backfill.py` (dry-run by default; `--apply`
to write) and is **not** wired into the runtime.

**EOD flatten has three layers of defense.** (1) tick-driven `_maybe_flatten_eod`
(primary — fires intra-tick after `eod_exit_time`, but can't run if the broker
tick stream dies before close); (2) the APScheduler **EOD watchdog**
(`services/eod_watchdog_service.py`) — a tick-independent backstop that flattens
open `trade_journal` rows via `place_order`, gated by
`SIMPLIFIED_ENGINE_EOD_WATCHDOG_ENABLED` (default true); (3) the 15:30
reconciliation above, which catches anything the first two missed. The watchdog
fires at **15:14 IST** (`min(strategy.eod_exit_time, SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME)`,
default cap 15:14) — deliberately **before** the 15:15 sandbox/broker MIS
auto-square-off. This is load-bearing: sandbox *rejects* MIS orders placed at/after
15:15, so a watchdog at the old declared 15:20 was always too late (the 2026-06-10
OIL/HINDZINC/TATAELXSI orphans). Do not move the cap to ≥15:15.

## Multi-account mirror trading (issues #468/#474/#476/#478)

One install can drive multiple **family** Zerodha accounts: the PRIMARY account
keeps everything it always did (feed, historical data, scanner, signals,
sandbox); CHILD accounts exist only to **mirror LIVE strategy orders**, scaled
to their capital. Full design: [`docs/design/multi_account_plan.md`](docs/design/multi_account_plan.md).

- **Setup/login** (`/accounts` page, `blueprints/broker_accounts.py`,
  `services/broker_accounts_service.py`): child rows in `broker_accounts`
  (Kite app credentials encrypted, per-account TOTP, capital, `is_enabled`
  default false) + `account_strategies` allow-list. Each child's daily token
  lives in the `auth` table under `name='acct:<id>'`; the Zerodha callback
  routes to the child path ONLY when `?account_id=<N>` (each child Kite app's
  redirect URL carries it — all apps under ONE developer profile because the
  static-IP whitelist is profile-level; SEBI family-only IP sharing).
  Child login performs NONE of the primary-login side effects.
- **Fan-out** (`services/account_fanout_service.py`): fires from ONE seam at
  the tail of `place_order_with_auth`'s LIVE-accepted branch (covers entries,
  exits, watchdog flattens, kill-switch closes uniformly). Gating: env
  the UI master switch (`multi_account_settings.enabled`, default **false**;
  issue #484 — env vars are only the first-boot seed) + LIVE parent +
  broker-accepted + known strategy + enabled child selecting it.
  **Sizing is capital-per-trade (issue #496, supersedes the ratio model):**
  each (child, strategy) row carries `capital_per_trade_inr` — OPENING
  quantity = `floor(capital ÷ price)` (equity) or `floor(capital ÷
  (premium × lotsize)) × lotsize` (derivatives), priced from the parent's
  LIMIT price or a live quote at mirror time (`resolve_sizing_price`;
  journaled as `account_orders.sizing_price`). Unset capital →
  `skipped_no_capital`, quote failure → `skipped_no_quote`, unaffordable →
  `skipped_zero_qty` — all loud, never guessed. EXITS flatten the child's
  own broker position (never blocked by capital/quote availability); a flat
  child whose entry attempt recently FAILED gets `skipped_no_position`
  (never a naked position — issue #478 guard).
  Fire-and-forget per-child isolation; every attempt journaled to
  `account_orders`; every non-placed outcome Telegrams
  (`multi_account_mirror`).
- **Observability** (`services/account_mirror_summary_service.py`): orderbook
  "Mirror Orders" card + `/accounts` today chips; login reminders 09:00/15:00
  IST and a 15:35 IST EOD mirror summary — all fire-time gated on the master
  flag + trading day (silent for single-account installs).
- **Ops**: every child account needs its own daily Kite login (Connect button
  + per-account TOTP code on `/accounts`); child fills/P&L live in the child's
  own broker book — OpenAlgo does not track child positions.

## Unified strategy daily intent (`strategy_daily_intent`)

> **Mode-only migration in progress (2026-06-12).** A new `strategy_mode` table
> (`database/strategy_mode_db.py`) is becoming the single *persistent* per-strategy
> operator control — `mode ∈ {live, sandbox}`, default `sandbox`. It supersedes the
> `{mode, intent, daily_capital_cap}` model below: the `intent` (run/pause/halt) axis
> is moving to a separate self-expiring `strategy_runtime_override` table written only
> by automated safety guards (data-health auto-pause, daily kill-switch, sector_follow
> `/api/pause`), and `resolve_mode` replaces `resolve_strategy_mode`. The migration
> script `scripts/migrate_strategy_daily_intent_to_strategy_mode.py` carries the latest
> mode forward (drops intent/cap; `skip` → `sandbox`). The sections below describe the
> table being retired; they are rewritten progressively as the refactor lands.
>
> **Per-strategy UI-driven order dispatch (issue #440, 2026-07-23 — supersedes
> B2):** live-vs-sandbox routing is now decided **per order, per strategy**, by
> exactly two visible controls and nothing else: the navbar **Analyze/Live
> toggle** (`analyze_mode`, the platform kill switch — Analyze ON forces
> sandbox for everything) and the per-strategy **Live/Sandbox toggle** on
> `/strategies` (a `strategy_mode` row written via the preflight-gated
> `strategy_mode_service.flip_mode`). `place_order` (and basket/split/smart/
> GTT-place/close_position) dispatch on
> `services.mode_service.resolve_order_mode(mode_key)`: LIVE **only** when
> Analyze is off AND that strategy's row says `live`; no row / unknown label /
> env-only resolution → SANDBOX (**default deny**). In-repo engines pass their
> canonical `mode_key` explicitly (the simplified engine's payload `strategy`
> is a webhook label, so it passes `mode_key='simplified_engine'`). The hidden
> `strategy_mode['__global__']` gate and the legacy `daily_intent` fall-through
> are **retired from dispatch** (a leftover `__global__` row is purged at
> boot), and env mode flags (`SIMPLIFIED_ENGINE_MODE` etc.) are **capped at
> sandbox** — an env var can never escalate to live. The residual
> `resolve_effective_mode()` is now the analyze overlay only (SANDBOX iff
> Analyze ON, else LIVE) and is used ONLY by read decorations (which book the
> UI shows) and order management (cancel/modify route per-order via a
> sandbox-book orderid lookup, `sandbox_order_exists`; cancel-all sweeps both
> books when Analyze is off) — it must never gate a new order live.
> Observability: `/mode/status` lists every strategy row with its
> `effective_routing`, and the strategies page badges show routing truth
> (e.g. "Live (held: Analyze on)").
>
> **A strategy reading back its OWN positions is NOT a read decoration
> (issue #497).** `get_positionbook(mode_key=...)` /
> `get_positionbook_with_auth(mode_key=...)` resolve the book with
> `resolve_order_mode(mode_key)` — the *same* resolver that routed the order —
> so the write and the read can never land on different books. Omitting
> `mode_key` keeps the analyze-overlay behavior for UI reads. This is
> load-bearing: with Analyze OFF the overlay returns LIVE, so a `sandbox`
> strategy wrote into `sandbox.db` and read the empty LIVE broker book —
> `futures_follow_cap50` rehydrated 0 positions and fired **no T+1 exits for
> four trading days** (2026-07-27..30, 7 NIFTY lots stranded at 110% of book
> against a 50% cap). It surfaced there first because it trades **NRML**, which
> has no MIS auto-square-off underneath it. In-repo callers pass their canonical
> key (`futures_follow_cap50`, `simplified_engine`). **When adding a position
> read to a strategy, pass `mode_key` — and test the routing, not a mocked
> `get_positionbook`** (mocking that call is what hid #497 from 5 existing
> rehydrate tests). See `test/test_positionbook_mode_routing.py`.
>
> **Engines + preflight wired to runtime_override (B3, 2026-06-12):** both engines
> now consult `strategy_runtime_override.is_entry_blocked(strategy)` at job-entry
> instead of the retired `intent` axis. An active `pause`/`kill_switch` override
> holds **new entries only**; **exits and EOD are never gated** (a held position
> must always be allowed to square off — the simplified engine's `_place_exit_order`
> and sector_follow's `run_exit` no longer have an intent gate). The simplified
> engine's `_intent_blocks` is replaced by `_entry_held_by_override`; sector_follow's
> `run_entry` calls `_entry_held_by_override`; `_apply_mode_override` now fires on a
> persistent `strategy_mode` row (`source='strategy_mode'`) rather than a `unified`
> intent row. Preflight's `intent`/`effective_mode` checks are now **informational
> and never abort** (mode-only has no skip/halt; the "refuse with no declared
> intent" floor is removed — unconfigured resolves to sandbox).
>
> **LLM veto enforces in sandbox by default (B4, 2026-06-12):** `VETO_LAYER_MODE`
> is now mode-aware in `services/signal_review_service.get_veto_layer_mode(effective_mode)`.
> With the env var unset, a `sandbox` strategy defaults to `active` (the Stage-1
> veto *enforces* — a `skip` verdict blocks the entry on the virtual book), while
> `live` is unchanged (`shadow`, observe-only). An explicit `VETO_LAYER_MODE` wins
> everywhere and is the single emergency disable (`=off`). See `docs/PARAMETER_LOG.md`.

The single per-strategy control surface for both the simplified engine and
sector_follow is the `strategy_daily_intent` table in `db/openalgo.db`. It
replaces the legacy simplified-engine `daily_intent` table and sector_follow's
in-memory pause flag as the canonical *pre-market* control. One row per
`(strategy_name, intent_date)`:

- **`mode`** ∈ `live` / `sandbox` / `skip` — HOW orders route (broker /
  sandbox.db / no orders).
- **`intent`** ∈ `run` / `pause` / `halt` — WHETHER to act. `pause` blocks new
  entries but lets exits / MTM / EOD continue; `halt` skips everything including
  exits.
- **`daily_capital_cap`** — optional override of the strategy's default daily
  capital (caps the position-slot count).

The single read path is
`services.mode_service.resolve_strategy_mode(strategy_name, date=None)`, which
returns an `EffectiveDecision(mode, intent, daily_capital_cap, source)`. It is a
**separate** function from the order-dispatch gate — since issue #440 that gate
is the per-strategy `resolve_order_mode(mode_key)` (see the #440 block above);
`resolve_effective_mode()` survives only as the analyze overlay for read paths
and order management — see `docs/design/strategy_daily_intent.md` for history.

**Fall-through (flag on):** unified row → legacy `daily_intent` (simplified
only) → env mode flag (`SIMPLIFIED_ENGINE_MODE` / `SECTOR_FOLLOW_CAP5_VOL_MODE`)
→ `sandbox/run` default. **Deploy is a no-op** until the operator inserts a row;
each strategy stays on its existing env/legacy behavior until then.

**Feature flag:** `STRATEGY_DAILY_INTENT_ENABLED` (env, default `true`). Set
`false` for pure legacy behavior. **Migration:** legacy `daily_intent` rows are
backfilled into the unified table once at boot (idempotent, `updated_by=
'migration'`, `intent='run'`).

**Where the gate lives:** in the engines (at job-entry / order-dispatch), NOT in
`place_order_service` — the simplified engine's sandbox path bypasses
`place_order_service`, and entry-vs-exit is only knowable in the engine. The
shared `place_order_service` global resolver is unchanged. The
`/sector_follow_cap5_vol/api/pause|resume` REST endpoints remain as **runtime
emergency overrides** (in-memory `manual_pause`); pre-market planning uses the
table.

To opt a strategy in: `set_intent(strategy_name, date, mode, intent, cap,
updated_by, notes)` in `database/strategy_daily_intent_db.py` (SQL or the
Telegram inbound bot below). To roll one back: delete its row → instant env
fall-through.

## Telegram daily intent control (Phase 6)

> **RETIRED by the mode-only architecture (B5, 2026-06-12).** There is no per-day
> intent to set from the phone anymore — strategies run continuously in their
> persistent `strategy_mode`. In `services/telegram_inbound_service.py` the
> `/intent`, `/pause`, `/resume`, `/halt`, capital-cap, free-text intent forms, and
> the inline morning-keyboard buttons all now return a single deprecation notice
> (mode flips stay laptop-only; emergency pause is the sector_follow `/api/pause`
> REST endpoint over WireGuard/SSH). **The 08:45 IST `telegram_inbound_morning_prompt`
> APScheduler job is removed** (`register_jobs` is a no-op that also clears any
> stale instance). Only `/status` remains — it now reports each strategy's current
> mode (and any active `strategy_runtime_override`). The section below describes the
> retired control surface, kept for historical context.

The unified `strategy_daily_intent` table can be set **from the phone** via the
inbound Telegram bot (`services/telegram_inbound_service.py`), the INBOUND
counterpart to the send-only outbound `telegram_bot_service`. Full design:
[`docs/design/telegram_inbound.md`](docs/design/telegram_inbound.md).

**Feature-flagged off by default** (`TELEGRAM_INBOUND_ENABLED`, default `false`):
deploying the module starts no poller. When enabled, it polls Telegram on a real
OS thread (eventlet-safe, like the outbound bot), registers an **08:45 IST**
morning-prompt APScheduler job, and writes the intent table.

**Commands:** `/status`, `/intent <strategy> <run|pause|halt>`,
`/intent <strategy> cap <amount>`, `/intent <strategy> clear`,
`/pause`/`/resume`/`/halt <strategy>`, `/morning`. Free-text replies
(`pause sector_follow`) and inline morning-keyboard buttons work too. Strategy
aliases accepted (`simplified`, `sector`/`sf`).

**Safety rails (load-bearing — do not relax):**
- **Mode flips are NOT exposed.** Only the *intent* axis (run/pause/halt) and the
  capital cap are settable from Telegram; `live`/`sandbox`/`skip` (HOW orders
  route) replies *"Mode changes require laptop access for safety."* Setting an
  intent **preserves** the row's existing mode.
- **chat_id allowlist** in `bot_config.telegram_chat_ids` (comma-separated);
  unauthorized chats are silently ignored.
- **Halt always two-step:** a halt-triggering input arms a 30-second "reply YES"
  confirmation before the row is written.
- **Audit:** every change writes `updated_by=telegram:<chat_id>:<message_id>`.
- **One poller per bot token (structurally enforced — issue #238):** the inbound
  service refuses to start its `getUpdates` poller whenever the UI-toggled
  interactive bot (`bot_config.is_active`, `telegram_bot_service`) owns the token.
  `telegram_inbound_service.start()` checks `_ui_bot_active()` first and returns
  `(False, …)` with an INFO log (no raise) if the UI bot is active — so even with
  `TELEGRAM_INBOUND_ENABLED=true` the UI bot wins and two pollers on one token can
  never both run (the 2026-06-30 `telegram.error.Conflict: terminated by other
  getUpdates request` storm). The outbound `notify()` fallback is unaffected: when
  the UI bot is DOWN, `_ui_bot_active()` is false, the inbound poller starts, and
  `send_message_to_all` works as before. Test: `test/test_telegram_single_poller.py`.

**Operator activation:** `UPDATE bot_config SET telegram_chat_ids='<chat_id>'
WHERE id=1;` (or `database.telegram_db.add_authorized_chat_id`), set
`TELEGRAM_INBOUND_ENABLED=true`, restart.

**Outbound alerts route through the inbound poller when the legacy bot is
inactive.** Activating Phase 6 frees the Telegram token to the inbound poller,
which means `bot_config.is_active=0` and the legacy `telegram_bot_service` stops
running — so one-way operator alerts would otherwise be silently dropped.
`services/notification_service.notify()` now falls through to
`telegram_inbound_service.send_message_to_all()` (same `telegram_chat_ids`
allowlist) when the legacy bot is down. Legacy stays primary when it's running
(purely additive); the inbound fallback escalates any send failure via
`logger.exception` rather than dropping silently.

## Claude Code Instructions

### Frontend Build Process
When actively editing React code, run `cd frontend && npm run build` (build only,
no tests — tests run in CI; not required for local iteration). A fresh clone
already has `frontend/dist/` committed, so backend-only work needs no Node/npm.

frontend/dist is committed (upstream convention as of v2.0.1.1 merge);
local rebuilds may produce dirty working trees — commit dist when shipping.

## Scheduled Tasks Audit-Trail Policy

Cowork's scheduled tasks (e.g. `fno-scan-cycle`, `scanner-vs-chartink-daily-comparison`)
run **read-only on this repo's code**. They must never edit source files, run `git add`,
or commit. When a scheduled task observes a probable bug during its normal work, it
appends a single structured JSON line to [`audit/proposed_fixes.jsonl`](audit/proposed_fixes.jsonl)
and exits; the operator reviews that log and decides whether to fix. The schema and rules
live in [`audit/README.md`](audit/README.md). Tracked snapshots of the live (OneDrive)
SKILL.md files are kept under [`docs/skills/`](docs/skills/) and carry the same
"code is read-only" section. Relatedly, `app.py` logs a WARNING at boot if
`git status --porcelain` is non-empty (gated by `OPENALGO_BOOT_DIRTY_CHECK_ENABLED`,
default `True`) so uncommitted code edits surface on the next restart.

## Cowork / AI Agent Operational Learnings

For detailed session learnings including login flows, Zerodha quirks, API endpoints,
webhook IDs, monitoring procedures, and daily workflow checklists, see:
[docs/COWORK_SESSION_LEARNINGS.md](docs/COWORK_SESSION_LEARNINGS.md)

**Key quick-reference from that doc:**
- **Webhook ID**: `c7d08357-6fe1-4603-bd2a-be4c9f9e06ac` (strategy: `chartink_FnO_intraday_buy`)
- **Simplified Engine POST**: `http://127.0.0.1:5000/chartink/simplified-stock-engine/<webhook_id>`
- **Engine Status GET**: `http://127.0.0.1:5000/chartink/simplified-engine/api/status`
- **Chartink Buy Screener**: `https://chartink.com/screener/fno-intraday-buy-20`
- **Chartink Sell Screener**: `https://chartink.com/screener/alert-for-intraday-sell-fno`
- Chrome extension cannot navigate to `kite.zerodha.com` — user must complete Zerodha login manually
- Bash shell is sandboxed Linux — use `javascript_tool` in browser for localhost API calls
- Zerodha tokens expire daily at 3 AM IST — re-login required each trading day
- **Backtester**: `uv run python backtest/run_backtest.py --date YYYY-MM-DD --from-engine` — replays 5-min candles (or tick data with `--tick-data tick_logs`) through the engine in `disabled` mode (no DB writes). **Always use `--from-engine`** to fetch live config (atr_sl_mult, max_trades, cooldown) from the running engine API — without it, config may diverge from live and produce misleading results. Requires OpenAlgo running + active broker session. See Sections 11-12 of the learnings doc for full details, comparison analysis, and tick-data replay.

## Cowork ↔ Claude Code Bridge Server

A FastAPI bridge at `bridge/server.py` allows Cowork to invoke Claude Code CLI
over HTTP for automated bug fixing, testing, and app restart.

**Start**: `uv run python bridge/server.py` (runs on port 5001 alongside OpenAlgo on 5000)

**Endpoints** (all at `http://127.0.0.1:5001`):
- `POST /fix-bug` — Send error details, Claude Code fixes the code and runs tests
- `POST /run-tests` — Run pytest, optionally auto-fix failures
- `POST /run` — Run any custom prompt via Claude Code
- `POST /restart-app` — Kill and restart OpenAlgo
- `GET /status` — Check if bridge is idle/busy
- `GET /read-errors` — Read last N entries from errors.jsonl
- `GET /engine-status` — Proxy simplified engine status

**How Cowork calls it** (via browser JS on any OpenAlgo tab):
```javascript
fetch('http://127.0.0.1:5001/fix-bug', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    error_message: 'WebSocket connection failed',
    traceback: '...',
    file_path: 'services/simplified_stock_engine_service.py'
  })
}).then(r => r.json()).then(d => { window.__bridge_result = d; });
```
