# OpenAlgo System Map

Single-source overview of all processes, scheduled tasks, logs, databases, and
inter-component side effects in this deployment. Read at the start of any
session that involves diagnostics, mid-market changes, or unexpected behavior.

> **Golden rule for diagnostics:** when something looks wrong mid-market, read
> the scheduled-task session transcripts **first**, `errors.jsonl` **last**.
> The `fno-scan-cycle` task can run pytest, mutate code, and restart OpenAlgo
> silently — those side effects masquerade as engine faults in `errors.jsonl`.

## Processes

### 1. OpenAlgo Flask app (port 5000)
- **Entry:** `app.py` (`uv run app.py` dev; `gunicorn --worker-class eventlet -w 1 app:app` prod)
- **DBs written:** `db/openalgo.db`, `db/logs.db`, `db/sandbox.db`,
  `db/historify.duckdb`, `db/latency.db`, `db/health.db`
- **Logs:** `log/openalgo_YYYY-MM-DD.log` (text, if `LOG_TO_FILE=True`),
  `log/errors.jsonl` (structured ERROR+, always on)
- **Boot sequence:** imports ~22 `init_db()` functions (`app.py:90-114`) → multi-DB
  table init → master-contract load → scanner-history warm-up thread
  (`app.py:842-851`, gated by `SCANNER_HISTORY_WARMUP_ENABLED`) → WS subscribe →
  "Ready" banner. Boot logs a WARNING if `git status --porcelain` is non-empty
  (`OPENALGO_BOOT_DIRTY_CHECK_ENABLED`, default true).
- **Side effects on restart:** clears in-memory positions/stops/EOD timer; broker
  WS often does not resume cleanly; triggers a ~3-second SQLite "database locked"
  burst (~180 errors) during the multi-DB init.
- **Manage via:** `uv run app.py`, or bridge `POST /restart-app`.

### 2. Bridge FastAPI (port 5001)
- **Entry:** `bridge/server.py` (`uv run python bridge/server.py`)
- **Endpoints + side effects:**

| Endpoint | Method | Side effect |
|---|---|---|
| `/fix-bug` | POST | Spawns Claude Code subprocess → prompt runs `uv run pytest test/ -v` (FULL SUITE — `server.py:427`) → **may mutate any file** |
| `/run-tests` | POST | Spawns Claude Code subprocess → also runs `uv run pytest {test_target} -v` (`server.py:449,456`) |
| `/restart-app` | POST | Kills PID on port 5000 via PowerShell `Stop-Process -Force` → respawns `uv run app.py` (`server.py:494-516`) |
| `/run` | POST | Arbitrary Claude Code prompt — may mutate files |
| `/review-signal`, `/reflect` | POST | LLM calls; review/journal helpers |
| `/status`, `/read-errors`, `/engine-status` | GET | Read-only |

- **Busy lock:** all task endpoints 409 if `state.status == BUSY`. A wedged task
  (e.g. a hung restart on Windows) leaves the bridge permanently busy — see
  memory `bridge-restart-app-hangs-windows`.
- **Logs:** `log/bridge_stderr.log` — **UNRELIABLE** (may show a stale mtime even
  after recent calls; not every invocation reaches it).
- **Pollution risk:** `/fix-bug` + `/run-tests` pytest runs write to the SHARED
  `log/errors.jsonl` and hit localhost (polluting `db/logs.db` traffic) unless
  conftest isolation kicks in. Has caused 300-400 error storms that lock preflight
  45+ min.

### 3. Cowork scheduled tasks (host-side, NOT in OpenAlgo)
- **Configured in:** Cowork app via SKILL.md files at
  `C:\Users\Dheeraj\OneDrive\Documents\Claude\Scheduled\<name>\SKILL.md`
  (tracked snapshots under `docs/skills/`).
- **Inspect via:** `mcp__scheduled-tasks__list_scheduled_tasks` and
  `mcp__session_info__list_sessions` / `read_transcript`.
- **These run read-only on repo code** by policy — they append to
  `audit/proposed_fixes.jsonl` instead of editing source (see `audit/README.md`).
  The exception is `fno-scan-cycle` step 6, which **calls the bridge** (which is
  not bound by that policy).
- **Active tasks** (verify current state — list may drift):

| Task | Cron | Side effects |
|---|---|---|
| `fno-scan-cycle` | `*/15 9-16 * * 1-5` (every 15 min, market hrs) | Scans Chartink → POSTs engine webhook → **step 6 calls bridge `/fix-bug` → can run full pytest + restart OpenAlgo mid-market** |
| `scanner-vs-chartink-daily-comparison` | `45 15 * * 1-5` (15:45 IST) | Read-only comparison; appends to `audit/proposed_fixes.jsonl` |
| `daily-trading-pipeline` | `30 9 * * 1-5` | DISABLED (deprecated) |

## Databases

| DB | Holds | Notes |
|---|---|---|
| `db/openalgo.db` | users, orders, positions, settings, **scan_cycle** (canonical Chartink fire history), strategies, **trade_journal** (one row per round trip; `ltp_at_signal` REAL holds the decision-time LTP for slippage analysis, added 2026-06-07 via boot-time `ALTER TABLE` in `trade_journal_db.init_db`) | Main DB. Pooling: `NullPool` |
| `db/logs.db` | `traffic_logs` (HTTP request log) | Polluted by pytest hitting localhost |
| `db/latency.db` | latency monitoring | `NullPool` |
| `db/health.db` | health monitoring | `NullPool` |
| `db/sandbox.db` | sandbox trading (₹1 Cr virtual capital) | Engine default target; isolated from live. Auto square-off at exchange close |
| `db/historify.duckdb` | historical OHLC market data (`market_data`); **`fo_bhavcopy_eod`** = expired-contract F&O option EOD recovered from NSE bhavcopy | DuckDB, not SQLite |

`fo_bhavcopy_eod` (cols: trade_date, symbol, expiry, strike, option_type, OHLC,
settle, volume, oi, lot_size, source) is a **research/backtest artifact**, not
written by the Flask app. Backfilled offline from NSE bhavcopy (UDiFF ≥2024-07-06,
legacy before) by `outputs/r29v2_options_hybrid_2026-06-07/phase1_backfill.py` to
recover daily prices for expired stock options that Kite's master cache purges
(~4.7M rows: 30-symbol R29 universe over 2024-01→2025-11 + 2026-01→05, plus
all-symbol coverage on R8's 55 swing dates). Used to replay equity signals as
options (see `outputs/r29v2_options_hybrid_2026-06-07/`).
Read-only for the app; short-lived
DuckDB RW connections from the backfill coexist with the running app.

All SQLite DBs use `NullPool` (fresh connection per op) — never `StaticPool`.
Indian broker tokens expire ~03:00 IST daily; sandbox reset schedule is
configurable at `/sandbox`.

## Logs — where to look

| File | What's in it | Reliability |
|---|---|---|
| `log/errors.jsonl` | structured ERROR+ (truncated to last 1000 on boot) | **Polluted by pytest** unless isolated — filter test noise first |
| `log/openalgo_YYYY-MM-DD.log` | full text log | Only if `LOG_TO_FILE=True` |
| `log/bridge_stderr.log` | bridge stderr | **UNRELIABLE** (may show stale mtime even after recent calls) |
| `db/openalgo.db` → `scan_cycle` | canonical Chartink fire history | **Trustworthy** — start here for trading-action audits |
| `db/logs.db` → `traffic_logs` | HTTP request log | Polluted by pytest hitting localhost |
| scheduled-task session transcripts | what each Cowork task actually did | **MOST reliable** for "what fired" — `mcp__session_info__read_transcript` |

## Investigation order when something looks wrong mid-market

1. `mcp__scheduled-tasks__list_scheduled_tasks` — what's enabled, `lastRunAt`.
2. `mcp__session_info__list_sessions` — find today's "Fno scan cycle" sessions.
3. `mcp__session_info__read_transcript` — read what each cycle actually did
   (auto-fix? restart? pytest?).
4. `scan_cycle` table (`db/openalgo.db`) — the canonical Chartink fire record.
5. `/preflight` endpoint — current gate state.
6. `errors.jsonl` (last — and only AFTER filtering pytest noise per memory
   `pytest-pollutes-live-db-and-preflight`).

## Symbol format + API auth conventions

See `CLAUDE.md` → "Symbol Format" and "API Authentication" sections. Not
duplicated here. Quick reminder: API key goes in JSON body (`apikey`) or
`X-API-KEY` header; equity symbols are the bare base symbol.

## Known recurring patterns

- **Morning Zerodha token rollover** ~02:00–03:00 IST → WS reconnect burst
  ~02:10–08:55 (pre-market noise, filtered by preflight). A morning
  "Invalid openalgo apikey" 401 is the expired broker session, **not** a bad
  API key — fix by re-login, don't regenerate the key
  (memory `morning-401-broker-session-not-key`).
- **Restart during market hours** → SQLite database-locked burst ~3 sec,
  ~180 errors during multi-DB init.
- **Bridge `/fix-bug` call** → full pytest suite + restart → ~300-400 error
  storm; can lock preflight 45+ min.
- **Bridge `/restart-app` on Windows can hang** → wedges bridge into permanent
  BUSY (409 on all task endpoints). Start OpenAlgo directly with `Start-Process`
  instead (memory `bridge-restart-app-hangs-windows`).
- **Scanner late-start / tick gaps** are usually tick starvation downstream of
  Chartink (scanner passively reads ZMQ), not scanner bugs
  (memory `inhouse-scanner-starved-no-self-subscribe`).

## Cross-references

- `CLAUDE.md` — coding conventions, deployment specifics, version bumping
- `COWORK_OBJECTIVE.md` — strategic objective
- `strategies/simplified_engine/LEARNINGS.md` — strategy-specific daily learnings
- `docs/SIMPLIFIED_ENGINE_HANDOFF.md` — engine integration context
- `docs/COWORK_SESSION_LEARNINGS.md` — Cowork-specific learnings, webhook IDs
- `audit/README.md` — read-only scheduled-task policy + `proposed_fixes.jsonl` schema
