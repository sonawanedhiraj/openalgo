# AI Trading Bot — Architecture & Design

> **Status:** Design / implementation-ready
> **Author:** Operator + Cowork (captured 2026-05-26)
> **Scope:** Evolution of the OpenAlgo + Chartink F&O intraday rig into an AI-supervised
> autonomous trading bot, across five stages, with the Stage 4 autonomous reasoning
> agent and the real-time intelligence ingest layer specified in detail.
> **Audience:** the single operator and the engineers who implement this.

This document is the single source of truth for the redesign. It is dense on purpose.
Schemas are SQL/Python, signatures are real, file paths are repo-relative to the
OpenAlgo root (`C:\workspace\ai-trade-agent\openalgo`).

---

## 1. Goal Statement

This system is a **single-operator F&O intraday trading rig**. It begins as a thin
scheduled-task orchestrator that polls Chartink screeners and forwards matched symbols
to the OpenAlgo simplified stock engine, and it evolves — stage by stage, never in one
leap — into an **AI-supervised autonomous trading bot**. The division of labour is fixed
and deliberate: **Claude provides context and judgment** (regime reading, anomaly
detection, veto/size decisions, reflective learning), **classical rules and ML provide
signal generation** (Chartink screeners today, gradient-boosted success probabilities
later), and **OpenAlgo + Zerodha provide execution** (order routing, broker session,
sandbox isolation). No LLM ever places or closes a real-money order autonomously; the
LLM's authority is bounded to reading state, enriching it, and proposing or vetoing —
with a human as the second key for anything irreversible.

---

## 2. Current Architecture (as of 2026-05-26)

### 2.1 Components

| Component | Location | Role |
|---|---|---|
| **Chartink screener** | `chartink.com/screener/fno-intraday-buy-20` (buy), `.../alert-for-intraday-sell-fno` (sell) | External signal source. Produces symbol lists on screener match. |
| **Scheduled task `fno-scan-cycle`** | `C:\Users\Dheeraj\OneDrive\Documents\Claude\Scheduled\fno-scan-cycle\SKILL.md` | Cowork scheduled skill, runs **every 15 min** during market hours. Scrapes the screeners and POSTs symbols to the engine webhook. |
| **OpenAlgo Flask app** | `app.py`, `http://127.0.0.1:5000` | Main platform. Hosts the engine, order routing, settings DB, analyzer/sandbox. |
| **Simplified stock engine** | `services/simplified_stock_engine_service.py` (integration), `services/simplified_stock_engine_core.py` (broker-agnostic), `services/simplified_stock_engine_ticklog.py` (tick log) | Arms long/short watches from Chartink symbols, fires market orders on 5-min candle breakout with ATR stop loss and RR trailing. Has its **own** mode flag (`SIMPLIFIED_ENGINE_MODE`). |
| **Chartink blueprint** | `blueprints/chartink.py` (`chartink_bp`, `url_prefix="/chartink"`, line 55) | Webhook + status + toggle routes. Engine POST at `:947`, status at `:1016`, toggle at `:1036`. |
| **`place_order_service`** | `services/place_order_service.py` | Final order dispatch. Routes to broker or sandbox based on `analyze_mode`. |
| **`sandbox_service`** | `services/sandbox_service.py` | Virtual ₹1 Cr paper-trading book in `db/sandbox.db`. |
| **Settings DB** | `db/openalgo.db`, model in `database/settings_db.py` | `settings.analyze_mode` (Boolean). `get_analyze_mode()` at `:79`, `set_analyze_mode(bool)` at `:99`, TTL cache at `:19`. |
| **Claude Bridge** | `bridge/server.py`, FastAPI on `http://127.0.0.1:5001` | Lets Cowork invoke Claude Code CLI over HTTP for bug-fix/test/restart. Endpoints: `/fix-bug`, `/run-tests`, `/run`, `/restart-app`, `/status`, `/read-errors`, `/engine-status`. |
| **Zerodha broker integration** | `broker/zerodha/` | OAuth session (expires daily ~3 AM IST), order placement, quotes, funds. |

### 2.2 Data-flow diagram

```
                          (every 15 min)
  ┌──────────────┐      ┌────────────────────────┐
  │  Chartink     │ scrape│  Scheduled task         │
  │  buy / sell   │◀──────│  fno-scan-cycle         │
  │  screeners    │       │  (Cowork SKILL.md)      │
  └──────────────┘       └───────────┬────────────┘
                                      │ POST symbols
                                      ▼
                    http://127.0.0.1:5000/chartink/simplified-stock-engine/<webhook_id>
                                      │
                          ┌───────────▼────────────┐
                          │  blueprints/chartink.py  │
                          │  (chartink_bp :947)      │
                          └───────────┬────────────┘
                                      ▼
                    ┌─────────────────────────────────────┐
                    │ simplified_stock_engine_service.py    │
                    │  arms watch → 5m breakout → fires      │
                    │  FLAG ① SIMPLIFIED_ENGINE_MODE         │  ← .env, read once at startup
                    │        (live | sandbox | disabled)     │     {live ticks vs backtest sense}
                    └─────────────────┬───────────────────┘
                                      ▼
                          ┌───────────────────────┐
                          │ place_order_service.py  │
                          │  FLAG ② settings.        │  ← db/openalgo.db, TTL-cached 1 hr
                          │  analyze_mode (bool)     │     {broker routing}
                          └─────┬──────────────┬────┘
                          live  │              │  analyze
                                ▼              ▼
                        ┌──────────────┐  ┌──────────────┐
                        │  Zerodha      │  │ sandbox_service│
                        │  broker (real)│  │  db/sandbox.db │
                        └──────────────┘  └──────────────┘

  ┌──────────────────────────────────────────────────────────┐
  │  Claude Bridge (bridge/server.py, FastAPI :5001)            │
  │  out-of-band: bug-fix / run-tests / restart / engine-status │
  │  optionally logs scan results for observability             │
  └──────────────────────────────────────────────────────────┘
```

**Where the two mode flags live (critical):**

- **FLAG ① `SIMPLIFIED_ENGINE_MODE`** — string in `.env` (`= 'live'` today). Read **once at
  process startup**. Governs the engine's own routing intent (`live` | `sandbox` |
  `disabled`). Default per CLAUDE.md is `sandbox`; current `.env` is `live`.
- **FLAG ② `settings.analyze_mode`** — Boolean in `db/openalgo.db`. Read by
  `place_order_service` via `get_analyze_mode()`, **TTL-cached for 1 hour**
  (`database/settings_db.py:19`). Governs whether the *platform* routes orders to broker
  (False = live) or sandbox (True = analyze).

These are two independent controls with overlapping but non-identical meaning. That
overlap is the root of the redesign (see §3).

---

## 3. Known Problems Driving the Redesign

Documented during the 2026-05-26 review:

**(a) Two mode controls with non-identical semantics and different invalidation.**
`.env SIMPLIFIED_ENGINE_MODE` and DB `settings.analyze_mode` can disagree. The engine can
think it is `live` while the platform's `analyze_mode=True` silently diverts to sandbox
(or vice-versa). Worse, their invalidation models differ: `.env` is read **once at
startup** (changing it requires a restart), while `analyze_mode` is **cached for 1 hour**
in-process. So the same toggle takes effect on two different, unpredictable timelines.

**(b) The skill is mode-agnostic — no preflight check.** `fno-scan-cycle` POSTs symbols
with no awareness of what mode the system is actually in. It cannot tell whether it is
arming live or sandbox, whether the broker session is alive, or whether the day was even
meant to trade. It fires blind.

**(c) Scan persistence depends on bridge availability.** Today the only durable record of
a scan cycle is whatever the bridge happens to log. If `bridge/server.py` is down, scans
still post and orders still fire, but **observability is silently lost** — there is no
fallback audit trail.

**(d) Overloaded naming.** `engine_mode` (live ticks vs backtest replay — an in-process
concept) collides conceptually with `mode` (broker routing — a DB/platform concept).
Reading either name in code or logs is ambiguous without context. See the Glossary (§14).

**(e) No persistent scan audit trail.** There is no table recording each scan cycle: what
symbols matched, what was posted, what the engine returned, whether it errored. The
traffic log (`db/logs.db`) records request metadata but **no request bodies**, so the
actual symbol payloads are unrecoverable after the fact.

**(f) LLM-orchestrated chores are fragile.** The deprecated `daily-trading-pipeline`
(a single long-running LLM task that tried to drive the whole day) **died on turn
limits**. Long autonomous LLM sessions that hold state in-context are brittle: they hit
context/turn ceilings, lose state on crash, and cannot be resumed cleanly.

**(g) No record of operator intent separate from system state.** The system has flags
(`analyze_mode`, `SIMPLIFIED_ENGINE_MODE`) but no first-class record of what the **operator
decided to do today** — trade live, paper-trade, or sit out. Intent and mechanism are
conflated, so there is nothing to check the flags *against*.

---

## 4. Five-Stage Roadmap

Each stage has a hard gate. **Do not move on until the gate is met.**

### Stage 0 — Operational Floor
**Scope:** Fix the P0/P1 items from the architectural review (§3). Single source of truth
for mode, durable scan audit trail, preflight gating, heartbeat, atomic mode toggle.
**Dependencies:** none — this is the foundation everything else assumes.
**Effort:** ~1.5–2 weeks.
**Gate:** *Don't move on until* every scan cycle produces a `scan_cycle` row and a
`cycle_heartbeat` trail **regardless of bridge state**, `resolve_effective_mode()` is the
only thing any new code consults, and the skill refuses to post when `/preflight` fails.

### Stage 1 — LLM Context / Veto Layer
**Scope:** Insert a `signal_review_service.py` between engine arming and order dispatch.
Each candidate signal is wrapped in one Claude call that can say `take | skip | size_down`
with reasoning + confidence. Fail-safe to `skip`.
**Dependencies:** Stage 0 (needs `scan_cycle` + effective-mode resolution to know what it
is reviewing and in what mode).
**Effort:** ~2 weeks.
**Gate:** *Don't move on until* a full trading day runs with the veto layer in the path,
every veto decision is logged, and measured added latency stays within the 1–3 s budget
with no missed entries attributable to the LLM call.

### Stage 2 — Reflective Trade Journal
**Scope:** `trade_journal` table capturing every order with full context; a nightly Cowork
reflection task that writes structured retrospectives to `journal_reflection` and opens
rule-change PRs.
**Dependencies:** Stage 1 (LLM reasoning is one of the journaled fields).
**Effort:** ~1.5 weeks.
**Gate:** *Don't move on until* the journal has captured a statistically useful run and the
nightly reflection has produced at least one actionable, reviewed retrospective.

### Stage 3 — Classical ML Signal Augmentation
**Scope:** Feature pipeline over journal + market data → gradient boosting → walk-forward
backtest → `/predict_success_probability` endpoint → ensemble with Chartink score.
**Dependencies:** Stage 2 (needs the journal as labelled training data).
**Effort:** large, deferred — this is future work.
**Gate:** *Don't move on until* **6 months of journal data** exist and the walk-forward
backtest shows out-of-sample lift over the Chartink-only baseline.

### Stage 4 — Autonomous Reasoning Agent
**Scope:** Event-driven supervisor built on the Claude Agent SDK. Reads all state from SQL,
classifies situations (routine/anomaly/emergency), proposes/executes whitelisted
non-monetary actions, escalates irreversible ones through a two-key approval inbox.
Includes the real-time intelligence layer (§10).
**Dependencies:** Stages 0–2 (needs the full SQL state surface and journal). Can begin in
read-only/advisory mode before ML (Stage 3) lands.
**Effort:** large — the bulk of this document (§9).
**Gate:** *Don't move on until* the agent has run advisory-only (no write tools enabled)
for a sustained period with operator-judged-correct classifications, and every guardrail
ring has been exercised.

### Stage 5 — Independent Signal Generation
**Scope:** The bot generates its own signals rather than only filtering Chartink's.
**Dependencies:** Stages 3 + 4 mature.
**Effort:** open-ended, deferred.
**Gate:** *Don't move on until* there is a validated edge under strict walk-forward
discipline **and** a hard capital cap is enforced (see §11).

---

## 5. Stage 0 Detailed Design — Operational Floor

All Stage-0 tables live in `db/openalgo.db` unless noted, with SQLAlchemy models added to
`database/` (suggested new module `database/trading_ops_db.py`).

### 5.1 `daily_intent` — operator's declared intent (source of truth)

```sql
CREATE TABLE daily_intent (
    trade_date   DATE     PRIMARY KEY,                  -- one row per trading day
    intent       TEXT     NOT NULL CHECK (intent IN ('live','sandbox','skip')),
    set_by       TEXT     NOT NULL,                     -- 'operator' | 'agent' | 'system'
    set_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    locked       BOOLEAN  NOT NULL DEFAULT 0,           -- once locked, only operator may change
    notes        TEXT
);
```

**Monotonicity rule (OPEN QUESTION — confirm with operator, see §13):** intent may only
move *down the risk ladder* automatically: `live → sandbox → skip`. The agent and system
may **downgrade** at any time; only the operator may **upgrade** (`skip → sandbox → live`),
and only while `locked = 0`. Proposed default: once the operator sets `live` and the first
order fires, set `locked = 1` for the rest of the day to prevent mid-session whipsaw.

### 5.2 `scan_cycle` — durable scan audit trail (problem (c), (e))

```sql
CREATE TABLE scan_cycle (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TIMESTAMP NOT NULL,
    completed_at     TIMESTAMP,
    screener         TEXT NOT NULL CHECK (screener IN ('buy','sell')),
    symbols          TEXT,            -- JSON array of matched symbols, verbatim
    post_status      TEXT,            -- 'ok' | 'http_error' | 'engine_rejected' | 'skipped_preflight'
    engine_response  TEXT,            -- JSON: full body returned by the engine webhook
    error            TEXT,            -- JSON: {type, message, traceback} on failure, else NULL
    bridge_logged    BOOLEAN NOT NULL DEFAULT 0   -- did the bridge also log it? (cross-check, not a dependency)
);
```

This row is written by the **skill itself** (or a thin endpoint it calls) at the start and
end of each cycle, independent of the bridge. `bridge_logged` is informational only — its
value being 0 must never block persistence.

### 5.3 `resolve_effective_mode()` — single source of truth (problem (a), (d))

New module **`services/mode_service.py`**:

```python
# services/mode_service.py
from enum import Enum
from dataclasses import dataclass

class EffectiveMode(str, Enum):
    LIVE     = "live"      # real broker orders
    SANDBOX  = "sandbox"   # virtual book only
    DISABLED = "disabled"  # do not arm / do not trade

@dataclass(frozen=True)
class ModeResolution:
    effective: EffectiveMode
    intent: str            # from daily_intent
    engine_flag: str       # SIMPLIFIED_ENGINE_MODE (.env)
    analyze_mode: bool     # settings.analyze_mode (DB)
    reason: str            # human-readable explanation of how this was composed
    conflict: bool         # True if the two flags disagreed with intent

def resolve_effective_mode(trade_date: "date | None" = None) -> ModeResolution:
    """Compose daily_intent + SIMPLIFIED_ENGINE_MODE + settings.analyze_mode into ONE
    authoritative answer. This is the ONLY function new code consults for routing.

    Composition (most-conservative-wins):
      1. If daily_intent.intent == 'skip'           -> DISABLED.
      2. If daily_intent.intent == 'sandbox'         -> SANDBOX (regardless of flags).
      3. If daily_intent.intent == 'live':
           - if analyze_mode is True (platform diverts) -> SANDBOX, conflict=True.
           - if SIMPLIFIED_ENGINE_MODE != 'live'        -> SANDBOX/DISABLED, conflict=True.
           - else                                        -> LIVE.
      4. If no daily_intent row for the date -> DISABLED, conflict=True
         (refuse to trade with no declared intent — fixes problem (g)).
    """
    ...
```

The principle: **operator intent caps the risk; the flags can only make it safer, never
riskier**. Any disagreement resolves to the safer mode and sets `conflict=True` so it
surfaces in logs and to the agent.

### 5.4 Cache TTL reduction for `get_analyze_mode()`

`database/settings_db.py:19` — drop the TTL from `3600` to `30`:

```python
# database/settings_db.py:19
# BEFORE: _settings_cache = TTLCache(maxsize=10, ttl=3600)  # 1 hour TTL
_settings_cache = TTLCache(maxsize=10, ttl=30)  # 30s — mode changes must take effect promptly
```

Rationale: a 1-hour cache means a mode flip can take up to an hour to be honoured by
`place_order_service` — unacceptable for a safety control. 30 s bounds the staleness window
while keeping the per-request DB-query reduction the cache exists for. `set_analyze_mode()`
already invalidates the key on write (`:110`), so this only affects the stale-read ceiling.

### 5.5 `/preflight` route (problem (b))

Added to `chartink_bp` (`blueprints/chartink.py`), e.g.
`GET /chartink/simplified-engine/api/preflight`. The skill calls it **before** posting any
symbols and aborts (writing `post_status='skipped_preflight'` to `scan_cycle`) on failure.

Checks, all must pass:

```python
@chartink_bp.route("/simplified-engine/api/preflight", methods=["GET"])
def simplified_engine_preflight():
    """Returns {'ok': bool, 'checks': {...}, 'effective_mode': str, 'reason': str}."""
    res = resolve_effective_mode()
    checks = {
        # 1. operator intent matches what the system will actually do
        "intent_matches_effective": (res.intent == res.effective.value) and not res.conflict,
        # 2. broker session healthy (only required when effective == live)
        "broker_session_ok":  (res.effective != EffectiveMode.LIVE) or broker_session_healthy(),
        # 3. funds above the configured floor (live only)
        "funds_above_floor":   (res.effective != EffectiveMode.LIVE) or funds_ok(min_floor),
        # 4. trades placed today below the daily max
        "trades_under_max":    trades_today() < max_trades_per_day,
    }
    return {"ok": all(checks.values()), "checks": checks,
            "effective_mode": res.effective.value, "reason": res.reason}
```

### 5.6 `cycle_heartbeat` — missed-run detection

```sql
CREATE TABLE cycle_heartbeat (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id  INTEGER,                 -- FK to scan_cycle.id (NULL for stages before a cycle row exists)
    ts        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    stage     TEXT NOT NULL,           -- 'preflight' | 'scrape' | 'post' | 'engine_ack' | 'complete'
    status    TEXT NOT NULL            -- 'ok' | 'warn' | 'error'
);
```

The skill writes one row at **each stage** of every run. Missed runs become trivially
queryable: a gap in `ts` ordered by stage, or an absence of a `complete` row for an
expected 15-min slot, means the scheduler or skill silently failed.

### 5.7 Atomic mode-toggle endpoint

A single endpoint flips `daily_intent` **and** the underlying flags together, so they can
never drift (problem (a)). Suggested: `POST /chartink/simplified-engine/api/set-mode`
`{intent: 'live'|'sandbox'|'skip', set_by, reason}`.

```python
def set_trading_mode(intent: str, set_by: str, reason: str) -> ModeResolution:
    """Atomically: write daily_intent, set settings.analyze_mode, and (where the running
    process allows) the engine's in-memory mode — in ONE transaction. Then return the
    freshly resolved effective mode so the caller sees exactly what took effect.

      intent='live'    -> daily_intent='live',    set_analyze_mode(False), engine->'live'
      intent='sandbox' -> daily_intent='sandbox', set_analyze_mode(True),  engine->'sandbox'
      intent='skip'    -> daily_intent='skip',                              engine->'disabled'

    NOTE: SIMPLIFIED_ENGINE_MODE in .env is read once at startup; this endpoint updates the
    engine's in-process state and records the desired .env value, but a process restart is
    still required for the .env literal to match. resolve_effective_mode() reflects the live
    in-process value, not the stale .env literal, so behaviour is correct without a restart.
    """
    ...
```

---

## 6. Stage 1 Detailed Design — LLM Veto Layer

New module **`services/signal_review_service.py`**. Wraps each candidate signal in one
Claude call **between engine arming and order dispatch** — i.e. after the engine decides a
breakout fired and built the order, but before `place_order_service` sends it.

### 6.1 Input schema

```jsonc
{
  "signal": {
    "symbol": "CONCOR",
    "direction": "BUY",                 // BUY | SELL
    "source": "chartink:fno-intraday-buy-20",
    "breakout_price": 812.4,
    "atr_stop": 798.1,
    "rr_target": 840.0,
    "chartink_rank": 3,
    "fired_at": "2026-05-26T10:31:00+05:30"
  },
  "positions": [                         // current open positions (from get_positions)
    {"symbol": "TATASTEEL", "direction": "BUY", "qty": 400, "unrealized_pnl": 1250.0}
  ],
  "market_context": {                    // today's regime snapshot
    "nifty_regime": "range_bound",
    "india_vix": 14.2,
    "trades_today": 2,
    "max_trades": 6,
    "effective_mode": "live"
  }
}
```

### 6.2 Output schema

```jsonc
{
  "decision": "take",                    // take | skip | size_down
  "size_multiplier": 1.0,                // 1.0 for take; e.g. 0.5 for size_down; ignored for skip
  "reasoning": "Range-bound regime, VIX benign, 2/6 trades used, no correlated exposure. Standard entry.",
  "confidence": 0.78                     // 0.0–1.0
}
```

### 6.3 Placement, budget, and failure handling

- **Placement:** inserted in `place_order_service` path (or a wrapper the engine calls
  just before it), gated on `effective_mode != disabled`. On `skip`, the order is not sent
  and the reason is written to `scan_cycle`/journal. On `size_down`, qty is multiplied by
  `size_multiplier` (rounded to lot size).
- **Latency / cost budget:** **1–3 s** added latency, **₹2–5 per call**. Use a fast model
  with a tight, cached system prompt (the rules and schema are static — cache them).
- **On LLM failure (timeout, error, malformed output): default to `skip` (fail-safe).**
  Rationale: a missed trade costs an opportunity; a wrongly-taken trade costs real money. In
  an intraday F&O context with a human-bounded risk appetite, never-trade-on-doubt is the
  correct asymmetry. Pass-through (`take`) would defeat the entire purpose of the layer the
  moment the LLM is flaky. Every fail-safe skip is logged distinctly (`reason="llm_unavailable"`)
  so flakiness is visible and not mistaken for a genuine veto.

---

## 7. Stage 2 Detailed Design — Reflective Journal

### 7.1 `trade_journal`

```sql
CREATE TABLE trade_journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT,               -- broker/sandbox order id
    trade_date      DATE NOT NULL,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,      -- BUY | SELL
    signal_source   TEXT NOT NULL,      -- 'chartink:...' | 'ml:...' | 'agent:...'
    market_regime   TEXT,               -- snapshot label at entry (e.g. 'range_bound', 'high_vol')
    llm_reasoning   TEXT,               -- Stage-1 veto reasoning if any, else NULL
    llm_decision    TEXT,               -- take | size_down | (NULL if no review)
    entry_price     REAL,
    entry_at        TIMESTAMP,
    exit_price      REAL,
    exit_at         TIMESTAMP,
    qty             INTEGER,
    pnl             REAL,
    pnl_pct         REAL,
    effective_mode  TEXT,               -- live | sandbox at time of trade
    tags            TEXT                -- JSON array: ['gap_up','sl_hit','trailed','reversal']
);
```

### 7.2 Reflection pipeline

```
nightly Cowork task (after square-off)
   │
   ├─ read trade_journal for trade_date
   ├─ read journal_reflection history (prior retrospectives, for trend)
   ├─ Claude: structured retrospective
   │     - what worked / what didn't, by regime and tag
   │     - hypotheses for rule changes (e.g. "skip BUY entries when VIX > 17")
   ▼
   ├─ write journal_reflection row (structured)
   └─ if a rule change is warranted -> open a PR via the bridge (feature branch, never main)
```

```sql
CREATE TABLE journal_reflection (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date       DATE NOT NULL,
    generated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    summary          TEXT,              -- prose retrospective
    metrics          TEXT,              -- JSON: win_rate, avg_pnl, by_regime{}, by_tag{}
    proposed_changes TEXT,              -- JSON array of {rule, rationale, evidence}
    pr_url           TEXT               -- if a PR was opened, else NULL
);
```

---

## 8. Stage 3 Detailed Design — ML Augmentation (sketch)

Deferred until the Stage 2 gate (**6 months of journal data**). Sketch only:

- **Feature pipeline:** pull from `trade_journal` (outcomes as labels) + market data
  (regime, VIX, breadth, time-of-day, sector, ATR percentile). Engineer per-signal feature
  vectors. Land in a feature store table or parquet.
- **Model:** gradient boosting (XGBoost / LightGBM) predicting P(trade succeeds | features).
- **Validation:** **walk-forward** backtest (train on rolling window, test forward, never
  peek). Reuse `backtest/run_backtest.py` harness conventions.
- **Deployment:** `POST /predict_success_probability` endpoint returning a calibrated score
  for a candidate signal.
- **Ensemble:** combine Chartink rank + ML probability (e.g. weighted, or ML as a second
  veto gate after Stage 1's LLM). Tune the blend on out-of-sample data only.

---

## 9. Stage 4 Detailed Design — Autonomous Reasoning Agent

This is the core of the design. The agent is an **AI supervisor**, not a trader: it reads
state, reasons about it, classifies the situation, and acts only within a tightly
whitelisted, non-monetary set of tools — escalating anything irreversible to the operator.

### 9.1 Invocation model

**Event-driven supervisor.** The agent is not a long-running loop (that failure mode killed
the old `daily-trading-pipeline`, problem (f)). Instead, **each invocation is a complete,
self-contained Claude Agent SDK session with its own fresh transcript**, started by an
event, and torn down when it returns a decision. Three trigger types:

| Trigger type | Source | Example |
|---|---|---|
| **scheduled** | APScheduler inside OpenAlgo | pre-open check, mid-session review, EOD wrap |
| **threshold** | OpenAlgo emits an event when a metric crosses a bound | VIX jump, drawdown limit, consecutive SL hits, broker session drop |
| **explicit** | operator or another service requests it | "review this position", "should I flip to sandbox?" |

Because no state lives in the LLM between invocations, sessions are cheap, crash-safe, and
independently resumable — a dead session loses nothing.

### 9.2 State: zero LLM in-memory persistence — everything in SQL

The agent holds **no** durable state in-context. Every fact it reasons over is read fresh
from SQL at invocation time. Tables it reads:

- `daily_intent` — operator's declared intent (§5.1)
- `scan_cycle` — recent scan history (§5.2)
- `trade_journal` — today's and historical trades (§7.1)
- `agent_decision` — its own prior decisions (§9.3)
- `cycle_heartbeat` — run health (§5.6)
- `market_intel` — real-time intelligence (§10)

Plus a markdown directory **`agent_memory/`** (repo-relative, gitignored or versioned per
operator choice) holding **cross-session learnings** — durable lessons distilled from many
sessions (e.g. "CONCOR gaps hard on rail-budget days"). These are prose, not rows, and are
loaded into the system prompt at invocation.

### 9.3 `agent_decision`

```sql
CREATE TABLE agent_decision (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    invoked_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    trigger_id          TEXT,                 -- correlates to the emitting event
    trigger_type        TEXT NOT NULL CHECK (trigger_type IN ('scheduled','threshold','explicit')),
    inputs_snapshot     TEXT,                 -- JSON: the exact state the agent read (for replay/audit)
    classification      TEXT NOT NULL CHECK (classification IN ('routine','anomaly','emergency')),
    reasoning           TEXT,                 -- the agent's chain of reasoning (prose)
    actions             TEXT,                 -- JSON array of actions taken / proposed (see §9.8)
    confidence          REAL,                 -- 0.0–1.0
    requires_human      BOOLEAN NOT NULL DEFAULT 0,
    message_to_operator TEXT,                 -- the Telegram ping text, if any
    executed_at         TIMESTAMP,            -- when low/medium-risk actions were carried out
    outcome             TEXT                  -- JSON: result of the actions, filled in after execution
);
```

### 9.4 `approval_inbox` (two-key rule)

```sql
CREATE TABLE approval_inbox (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    agent_decision_id   INTEGER NOT NULL,     -- FK -> agent_decision.id
    action_type         TEXT NOT NULL,        -- e.g. 'propose_code_change', 'pause_trading_until'
    action_params       TEXT,                 -- JSON payload describing exactly what would happen
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','approved','denied','expired')),
    decided_at          TIMESTAMP,
    decided_by          TEXT,                 -- 'operator'
    executor_completed_at TIMESTAMP           -- when the approved action actually finished executing
);
```

Anything irreversible lands here as `pending`. A separate executor process only acts on
rows the operator flips to `approved`. `expired` rows (operator never responded within a
TTL) are never executed — silence is denial.

### 9.5 Tool catalog (five groups)

Tools are exposed to the Agent SDK via MCP servers, one server per group, so the whitelist
is enforced at the protocol boundary (§9.6 ring 1).

#### Group A — Read tools (no side effects)

```python
def get_market_state() -> dict:
    """Return {'nifty': float, 'nifty_pct': float, 'india_vix': float,
               'regime': str, 'advances': int, 'declines': int,
               'session': 'pre_open'|'open'|'closed', 'as_of': iso8601}."""

def get_positions() -> list[dict]:
    """Return open positions:
       [{'symbol': str, 'direction': 'BUY'|'SELL', 'qty': int,
         'avg_price': float, 'ltp': float, 'unrealized_pnl': float,
         'stop_loss': float|None, 'effective_mode': str}, ...]."""

def get_today_journal() -> list[dict]:
    """Return today's trade_journal rows (schema §7.1)."""

def get_recent_scans(minutes: int = 60) -> list[dict]:
    """Return scan_cycle rows started within the last `minutes`
       (schema §5.2). Default 60."""

def get_broker_session_health() -> dict:
    """Return {'connected': bool, 'broker': str, 'token_valid': bool,
               'token_expires_at': iso8601, 'last_order_ok': bool}."""
```

#### Group B — Analysis tools (read + external lookup)

```python
def get_market_news(symbols: list[str], hours: int = 6) -> list[dict]:
    """Read market_intel (§10) for rows touching `symbols` in the last `hours`.
       Return [{'headline','summary','sentiment','source_name','fetched_at','confidence'}]."""

def get_nifty_regime() -> dict:
    """Return {'regime': 'trending_up'|'trending_down'|'range_bound'|'high_vol',
               'vix': float, 'vix_change_pct': float, 'rationale': str}."""

def compare_to_historical_pattern(symbol: str, pattern_type: str) -> dict:
    """Look up trade_journal history for `symbol` matching `pattern_type`
       (e.g. 'gap_up', 'vol_spike', 'sl_hit'). Return base rates:
       {'n': int, 'win_rate': float, 'avg_pnl': float, 'notes': str}."""
```

#### Group C — Low-risk write tools (reversible, non-monetary)

```python
def set_daily_intent(intent: str, reason: str) -> dict:
    """Change daily_intent. HARD CONSTRAINT: the agent can only DOWNGRADE
       risk — to 'sandbox' or 'skip'. It can NEVER escalate to 'live'.
       Attempting intent='live' is rejected at the tool boundary.
       Returns the new ModeResolution."""

def add_journal_note(symbol: str, note: str) -> dict:
    """Append a freeform note (tag) to today's trade_journal row(s) for `symbol`."""

def request_operator_confirmation(action: str, reason: str) -> dict:
    """Send a Telegram ping asking the operator to confirm something.
       Does NOT itself execute anything. Returns {'sent': bool, 'message_id': str}."""
```

#### Group D — Medium-risk tools (bounded, directional-only)

```python
def pause_trading_until(timestamp: str, reason: str) -> dict:
    """Stop arming new entries until `timestamp` (today only). Does NOT touch
       open positions. Reversible by the operator. Returns {'paused_until': iso8601}."""

def tighten_stop_loss(symbol: str, new_sl: float, reason: str) -> dict:
    """Move a stop loss to `new_sl`. HARD CONSTRAINT: new_sl must be TIGHTER
       (closer to LTP, smaller risk) than the current SL — never wider.
       Rejected at the tool boundary if it would loosen risk. Returns the modified SL."""
```

#### Group E — Code-modifying tool (via bridge, human-gated)

```python
def propose_code_change(file: str, diff: str, reason: str) -> dict:
    """Open Claude Code CLI (via bridge/server.py) on a NEW feature branch and apply
       `diff` to `file`. NEVER writes to main. Lands as a PR / pending approval, never
       auto-merged. Subject to the bridge denied_paths config (§9.7).
       Returns {'branch': str, 'pr_url': str|None, 'approval_inbox_id': int}."""
```

#### Explicitly NOT in the toolset

> **There is no tool — and there will never be a tool — that places or closes a
> real-money order autonomously.** Entry and exit of live capital is exclusively the
> engine's mechanical job (Chartink/ML signal → breakout → order) and the operator's
> manual prerogative. The agent can tighten a stop, pause new entries, downgrade to
> sandbox, and ping the human — but it cannot buy or sell real-money positions on its own.

### 9.6 Three guardrail rings

```
   ┌──────────────────────────────────────────────────────────┐
   │ Ring 3: two-key rule (approval_inbox)                       │
   │   irreversible actions need operator approval to execute    │
   │  ┌──────────────────────────────────────────────────────┐ │
   │  │ Ring 2: host-process invariant guard                    │ │
   │  │   sidecar tails agent_decision; kills/quarantines any   │ │
   │  │   decision violating invariants (e.g. live escalation,  │ │
   │  │   SL-loosening, order placement) before it can execute  │ │
   │  │  ┌────────────────────────────────────────────────────┐│ │
   │  │  │ Ring 1: tool-level whitelisting (MCP)               ││ │
   │  │  │   agent can only call tools the MCP servers expose; ││ │
   │  │  │   monetary order tools simply do not exist          ││ │
   │  │  └────────────────────────────────────────────────────┘│ │
   │  └──────────────────────────────────────────────────────┘ │
   └──────────────────────────────────────────────────────────┘
```

1. **Tool-level whitelisting via MCP** — the agent literally cannot call what is not
   exposed. The order-placement tools are absent from every MCP server.
2. **Host-process invariant guard** — a separate process **tails `agent_decision`** and
   enforces invariants independent of the LLM: no live escalation, no SL widening, no
   order placement, no write to denied paths. A violating decision is quarantined and the
   operator alerted; the executor never runs it.
3. **Two-key rule via `approval_inbox`** — irreversible actions (`propose_code_change`,
   and any future destructive action) require explicit operator approval before the
   executor acts. Silence expires to denial.

### 9.7 Bridge expansion

`bridge/server.py` gains a `denied_paths` config so the agent's `propose_code_change` can
never touch safety-critical code:

```python
# bridge config — paths the agent may NEVER modify
DENIED_PATHS = [
    "services/place_order_service.py",   # order dispatch
    "database/settings_db.py",           # mode flag storage
    "services/mode_service.py",          # effective-mode resolution
    "broker/**",                         # broker integration / credentials
    ".env", ".sample.env",
]
# All agent-driven writes go to a NEW feature branch. NEVER main. NEVER force-push.
```

### 9.8 Decision schema (agent structured output)

Every invocation returns exactly this JSON, which maps 1:1 onto an `agent_decision` row:

```jsonc
{
  "classification": "anomaly",                 // routine | anomaly | emergency
  "confidence": 0.82,                          // 0.0–1.0
  "reasoning": "VIX spiked 14→17.5 in 20 min; ...",
  "inputs_snapshot": {                         // what the agent read (for replay)
    "market_state": { "...": "..." },
    "positions": [ "..." ],
    "recent_scans": [ "..." ],
    "market_intel": [ "..." ]
  },
  "actions": [                                 // ordered list of tool calls performed/proposed
    { "tool": "pause_trading_until",
      "params": {"timestamp": "2026-05-26T11:15:00+05:30", "reason": "vol spike"},
      "status": "executed" },
    { "tool": "tighten_stop_loss",
      "params": {"symbol": "CONCOR", "new_sl": 805.0, "reason": "protect short on vol spike"},
      "status": "executed" },
    { "tool": "set_daily_intent",
      "params": {"intent": "sandbox", "reason": "ride out volatility in paper mode"},
      "status": "proposed_to_operator" }
  ],
  "requires_human": true,
  "message_to_operator": "VIX 14→17.5. Paused new BUY entries to 11:15, tightened CONCOR short SL to 805. Recommend flipping to sandbox — approve?"
}
```

### 9.9 Worked example — the volatility-spike scenario

**Trigger (threshold):** OpenAlgo's metric watcher fires when India VIX jumps **14 → 17.5**
inside 20 minutes. It emits an event; the FastAPI sidecar starts a fresh agent session.

**Read tools called (in order):**
1. `get_market_state()` → confirms VIX 17.5, Nifty −0.6%, regime flipping `range_bound →
   high_vol`.
2. `get_positions()` → one open position: **CONCOR SHORT**, unrealized +₹900, SL currently
   wide at 818.
3. `get_recent_scans(minutes=30)` → two fresh **BUY** candidates just armed by the engine.
4. `get_market_news(['CONCOR'], hours=3)` / reads `market_intel` → nothing material on
   CONCOR; the VIX move looks macro (index-wide), not single-name.
5. `get_nifty_regime()` → `high_vol`, vix_change_pct +25%, rationale "broad risk-off."

**Reasoning:** Macro volatility spike, not a stock-specific event. The held **CONCOR SHORT
benefits** from risk-off and should be protected, not closed. The fresh **BUY candidates
are dangerous** to arm into a rising-VIX tape. No single-name news justifies an emergency.

**Classification:** `anomaly` (not `emergency` — no position is bleeding, no session
failure).

**Action whitelist routing:**
- `pause_trading_until('11:15', 'vol spike — hold new BUY entries')` → **medium-risk, executed.**
- `tighten_stop_loss('CONCOR', 805.0, 'lock gains on short into vol spike')` → **medium-risk,
  executed** (805 is tighter than 818, so it passes the never-wider constraint).
- `set_daily_intent('sandbox', 'ride out volatility in paper mode')` → the agent *wants*
  this, but downgrading the whole day is consequential, so it routes it as
  `request_operator_confirmation` / `proposed_to_operator` → Telegram ping. The operator
  is the second key.
- **No order is placed or closed.** The CONCOR short stays open under a tighter stop; the
  BUY candidates are simply not armed.

The decision row, the Telegram message, and the pending intent change are all written to
SQL; if the session crashed mid-way, nothing is lost and the next trigger re-reads state.

### 9.10 Cost estimate

- **Per invocation:** ~5–15 K input tokens (state snapshot + system prompt + memory) +
  1–3 K output tokens.
- **Frequency:** ~10 invocations/day (scheduled checks + threshold events).
- **Daily API cost:** ~**₹40–80/day**. With prompt caching on the static system prompt and
  tool schemas, the marginal per-call input cost drops substantially.

### 9.11 Stack

| Concern | Choice |
|---|---|
| Agent runtime | **Claude Agent SDK (Python)** — one session per invocation |
| Event source | **APScheduler inside OpenAlgo** emitting scheduled + threshold events |
| Invocation surface | **FastAPI sidecar** exposing the agent-invocation endpoint + the approval inbox API |
| Tools | **MCP servers, one per tool group** (A–E), enforcing Ring-1 whitelisting |
| State store | **SQLite** (`db/openalgo.db` + new tables); migrate to **Postgres** as volume grows |
| Operator notifications | **Telegram bot** (see §9.12) |
| Dashboard | **Streamlit** (fast) or **Next.js** (richer), reading `agent_decision` + `approval_inbox` |

### 9.12 Telegram bot setup

- Create the bot via **@BotFather**, obtain the bot token.
- Store the token in **`.env`** as `TELEGRAM_BOT_TOKEN` (and `TELEGRAM_OPERATOR_CHAT_ID`).
- Set the bot **webhook to the FastAPI sidecar** (e.g. `POST /telegram/webhook`) so operator
  replies (approve/deny on `approval_inbox` rows) flow back in real time.
- The sidecar verifies the chat id matches the single operator and ignores everything else.

---

## 10. Stage 4 Real-Time Intelligence Layer

### 10.1 Two-layer pattern

The agent must not browse the open web on every tick — that is slow, costly, and noisy.
Instead, **two layers**:

1. **Continuous ingest sidecar** — a background service that polls a whitelist of news
   sources on a schedule and writes normalized rows to **`market_intel`**. Always running,
   cheap, no LLM.
2. **Narrow `browse_for_anomaly` tool** — the agent calls this **only on triggers**, and
   only when `market_intel` is empty for the symbols in question. It is a last-resort
   drill-down, rate-limited and tagged low-confidence.

### 10.2 `market_intel`

```sql
CREATE TABLE market_intel (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source            TEXT NOT NULL CHECK (source IN ('RSS','api','agent_browse')),
    source_url        TEXT,
    source_name       TEXT,             -- 'Moneycontrol' | 'ET Markets' | ...
    symbols_affected  TEXT,             -- JSON array of OpenAlgo symbols
    headline          TEXT,
    summary           TEXT,
    sentiment         TEXT,             -- 'positive' | 'negative' | 'neutral'
    confidence        TEXT,             -- 'high' | 'medium' | 'low' (agent_browse => always 'low')
    raw_payload       TEXT              -- JSON: original item for audit / re-parse
);
```

### 10.3 Ingest sidecar design

- **Language:** Python service using **`httpx`** (API/HTTP) + **`feedparser`** (RSS).
- **Source whitelist:** Moneycontrol, ET Markets, LiveMint, BloombergQuint, **BSE/NSE
  corporate announcements**, and **NewsAPI**. No source outside this list is polled.
- **Dedup logic:** hash `(source_name, headline)` (or a normalized title + publish date);
  skip inserts whose hash already exists within a rolling window. Corporate announcements
  also dedup on the exchange filing id.
- **Symbol tagging:** map headline entities to OpenAlgo symbols (watchlist match + alias
  table); rows that match no tracked symbol may still be stored but flagged.
- **Error handling:** per-source try/except with `logger.exception`; one source failing
  never blocks the others; backoff on repeated failure; never crashes the loop.
- **Refresh interval:** **configurable, 5 min default** per source (corporate announcements
  can poll faster during market hours, slower after).

### 10.4 `browse_for_anomaly` tool

```python
def browse_for_anomaly(query: str, urls_hint: list[str] | None = None,
                       max_pages: int = 3) -> list[dict]:
    """Targeted, on-trigger web drill-down when market_intel has nothing for the
       symbols under investigation. Fetches at most `max_pages` pages (optionally
       starting from `urls_hint`), summarizes findings, and writes them back to
       market_intel with source='agent_browse' and confidence='low'.

       RATE LIMIT: at most 3 calls per agent invocation. Exceeding the limit raises
       at the tool boundary. Returns the rows written."""
```

### 10.5 Hot-news drill-down pattern

```
threshold trigger (e.g. single-name -8% move, or VIX spike)
   │
   ├─ agent wakes (fresh session)
   ├─ get_market_news(symbols, hours) -> reads market_intel
   │     ├─ if rows found  -> reason over them, decide
   │     └─ if EMPTY       -> browse_for_anomaly(query, urls_hint, max_pages<=3)
   │                              -> writes low-confidence rows to market_intel
   │                              -> reason over them, decide
   ▼
   make decision (classify, act within whitelist, escalate if needed)
```

### 10.6 Explicit non-goals

> **The agent does NOT browse to generate signals.** Browsing exists *only* to enrich the
> response to an already-detected anomaly. Intelligence informs judgment about existing
> positions and pending entries; it never originates a trade idea. Signal generation stays
> with Chartink (today) and ML (Stage 3).

---

## 11. Stage 5 Sketch — Independent Signal Generation (deferred)

Eventually the bot could generate its own entry signals rather than only filtering
Chartink's, fusing the Stage 3 ML model, regime context, and agent judgment into original
ideas. **Deferred indefinitely.** Caveat: independent generation is where **overfitting risk
is highest** — a model that invents its own setups can fit noise catastrophically and
will look brilliant in-sample. Any Stage 5 work is gated behind a **hard capital cap** (a
small fixed fraction of the book, enforced mechanically, never overridable by the agent)
and strict walk-forward, out-of-sample validation before a single rupee of additional
size.

---

## 12. Implementation Sequencing (Stage 0 + Stage 1)

These two are immediate. Roughly two sprints each; gates are hard.

### Sprint 1 — Stage 0a: state foundation
- Add `database/trading_ops_db.py` with `daily_intent`, `scan_cycle`, `cycle_heartbeat`
  models + init.
- Implement `services/mode_service.py` → `resolve_effective_mode()` + `set_trading_mode()`.
- Reduce `get_analyze_mode()` cache TTL to 30 s (`database/settings_db.py:19`).
- Unit tests: every composition branch of `resolve_effective_mode()`; conflict detection.
- **Gate:** `resolve_effective_mode()` is the only mode authority new code calls; all
  branches covered by tests.

### Sprint 2 — Stage 0b: gating + audit + heartbeat
- Add `/chartink/simplified-engine/api/preflight` and the atomic
  `/chartink/simplified-engine/api/set-mode` routes.
- Update the `fno-scan-cycle` SKILL to: call `/preflight` → abort on fail (write
  `skipped_preflight`) → on pass, post symbols → write `scan_cycle` start/end + heartbeat at
  each stage, **all independent of the bridge**.
- **Gate (Stage 0 exit):** a full day runs where every cycle has a `scan_cycle` row + full
  `cycle_heartbeat` trail with the bridge **intentionally down**, and a preflight failure
  demonstrably blocks a post.

### Sprint 3 — Stage 1a: veto service skeleton
- Implement `services/signal_review_service.py` with the input/output schemas (§6),
  prompt-cached system prompt, and the fail-safe-to-`skip` failure path.
- Wire it into the dispatch path behind a feature flag (default off), logging decisions to
  `scan_cycle`/journal without yet blocking orders (shadow mode).
- **Gate:** shadow-mode decisions logged for a full day; latency measured within 1–3 s.

### Sprint 4 — Stage 1b: enforce + measure
- Flip the veto layer to enforcing (skip/size_down actually affect dispatch).
- Add distinct `llm_unavailable` accounting so fail-safe skips are separable from real vetoes.
- **Gate (Stage 1 exit):** a full enforcing day with no missed entries attributable to the
  LLM call, and every decision auditable.

---

## 13. Open Questions

From the 2026-05-26 architectural review:
1. **`daily_intent` monotonicity** — confirm the downgrade-only rule and the auto-lock
   semantics (§5.1). Specifically: should `live` auto-lock after the first fired order, and
   may the agent ever propose a re-upgrade for operator approval, or never?
2. **`direction_enabled` persistence** — where should the per-day long/short enable flags
   live and how should they survive a restart? (Currently in-engine, in-memory.) Should
   they move into `daily_intent` or a sibling table so `resolve_effective_mode()` /
   preflight can see them?
3. **Double EOD summary intent** — there appear to be two end-of-day summary paths with
   overlapping intent; confirm which is canonical and retire the other.

Surfaced while drafting:
4. **Telegram vs alternative** for operator notifications — Telegram is specified (§9.12),
   but confirm it over alternatives (Signal, ntfy, email, dashboard-only) given the
   single-operator, IP-sensitive (SEBI static-IP) deployment.
5. **Pre-market agent run** — should the agent run during the pre-open session to
   *recommend* the day's `daily_intent` (advisory only, operator confirms), or stay
   silent until market open?
6. **`agent_decision` retention policy** — how long to keep decision rows (audit value vs
   table growth), and when to migrate from SQLite to Postgres (§9.11). Proposed: keep
   indefinitely until row count forces the Postgres move; never auto-purge audit rows
   without an export.

---

## 14. Glossary

Overloaded terms, defined once:

- **`mode`** — the **broker-routing flag** stored in `db/openalgo.db`
  (`settings.analyze_mode`, Boolean). `False` = orders go to the real broker; `True` = orders
  divert to sandbox. Read via `get_analyze_mode()` (`database/settings_db.py:79`).
- **`engine_mode`** — the **simplified engine's own routing/run flag**, `SIMPLIFIED_ENGINE_MODE`
  in `.env` (`live` | `sandbox` | `disabled`). Conceptually about *live ticks vs backtest/paper*
  for the engine itself; read **once at process startup**. Independent of `analyze_mode`.
- **`daily_intent`** — the **operator's declared intent for the day** (`live` | `sandbox` |
  `skip`), stored in the new `daily_intent` table. **This is the source of truth**; the two
  flags above can only make the day *safer* than the intent, never riskier.
- **`effective_mode`** — the **single composed answer** returned by
  `resolve_effective_mode()` (`services/mode_service.py`): the most-conservative resolution
  of `daily_intent` + `engine_mode` + `analyze_mode`. **All new code consults this, not the
  raw flags.**
- **Analyze Mode** — the **UI label** (and analyzer/sandbox feature) for sandbox routing,
  i.e. the human-facing name for `analyze_mode = True`. Same concept as the DB flag, shown
  in the React frontend.
