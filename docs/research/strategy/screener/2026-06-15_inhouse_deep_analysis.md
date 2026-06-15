> **Tier 1 status: SHIPPED (2026-06-15).** Branch `variant/inhouse-screener-tier1`,
> 3 commits: Fix #1 market-hours gate + D-bar-date verify (`33ce471a6`), Fix #2
> loud per-symbol PASS/FAIL + missing-input logging (`e7a1897c2`), Fix #3 per-cycle
> completeness metric + Telegram + docs (this commit). All additive, default-on,
> behind flags (`SCANNER_POSTCLOSE_GATE_ENABLED`, `SCANNER_DBAR_DATE_VERIFY_ENABLED`,
> `SCANNER_COMPLETENESS_*`). See `CLAUDE.md` → "In-house screener observability —
> Tier-1 hardening" and `docs/PARAMETER_LOG.md`. Tier 2 (P1 unified data-access,
> P3 15:18 smoke check, P6 partial-today staleness) and Tier 3 (wall-clock bar
> flush, ZMQ-side watchdog, time-aligned comparator) remain open.

# In-House Screener — Phase A Deep Analysis (READ-ONLY)

**Date:** 2026-06-15
**Author:** Claude (Phase A, read-only)
**Requested by:** Dheeraj — *"the in-house screener has had a new failure mode almost
every day this week. Catalog every failure, trace every data path, surface the
cross-cutting design problems, and propose a unified Phase-B architecture."*
**Mode:** READ-ONLY on every DB and file. No edits, no commits, no pytest, no
restarts. Every claim below is backed by a `file:line` citation or a measured
artefact.
**Companion (today's measurement):**
[`2026-06-15_inhouse_vs_chartink_timealigned.md`](2026-06-15_inhouse_vs_chartink_timealigned.md)
**Reference template ("what good looks like"):** sector_follow Fix 1b
(`services/sector_follow_service.py`, 2026-06-15).

---

## Executive summary (read this first, Dheeraj)

The in-house screener has not had "a new bug every day." It has had **one
architectural shape** — *purely event-driven, fail-closed-silently, with no
data-readiness gate and no completeness telemetry* — that **manifests as a
different symptom every day** depending on which input happened to be missing. The
six reported failures collapse into **two root design defects plus the data-supply
gaps you already started fixing**:

1. **The scanner is blind to its own health.** It evaluates rules *only* when a
   tick closes a bar (`bar_aggregator.py:238-239`), has **no wall-clock flush**, **no
   liveness telemetry inside `ScannerService`**, and its only watchdog monitors a
   **different feed** (proxy WS :8765) than the one the scanner actually consumes
   (ZMQ :5555) — `scanner_ws_watchdog.py:144-156` vs `scanner_service.py:458,770-774`.
   So "tick-starved" and "genuinely quiet market" produce **byte-identical logs**:
   zero hits, no warning. This is the BUY-side collapse on 06-09→12 and again today.

2. **Every missing input fails closed, silently.** The buy/sell rules `return False`
   on missing daily-D, short history, or NaN indicators with **no log**
   (`scan_rules/fno_intraday_buy_chartink.py:74-81`); `get_today_ohlcv` returns
   `(None, None)` with **no log** (`scanner_service.py:757-758`); the backfill
   reports `success` even when an expired token failed every symbol
   (`scanner_universe_backfill.py:118-120`). There is **no per-symbol PASS/FAIL log,
   no "N of M symbols had data" metric, and no pre-decision smoke check** — the exact
   three things Fix 1b added to sector_follow and the exact three things the scanner
   still lacks.

The good news: **the fix already exists, one module over.** sector_follow's Fix 1b
(`sector_follow_service.py`) is a working, in-repo template for every gap below.
Phase B is largely "apply Fix 1b's discipline to `ScannerService`," plus a
market-hours gate and a documented single-source-of-truth for "what's live."

**Counts:** **14 failure modes** cataloged (6 reported + 8 discovered), **7
cross-cutting design problems**, **9 proposed fixes** ranked into 3 tiers.

---

## Section 1 — System map

### 1.0 The single most important structural fact

There are **two parallel, independent tick consumers that share no state**:

- **`ScannerService`** (the in-house screener) subscribes **directly to the broker
  adapter's ZeroMQ PUB socket on :5555** — `scanner_service.py:458`
  (`_DEFAULT_ZMQ_ENDPOINT`), `:770-774` (creates SUB, connects). It never touches
  the WebSocket proxy (:8765) or `WebSocketClient`.
- **The pre-subscribe + watchdog machinery** (`scanner_presubscribe.py`,
  `scanner_ws_watchdog.py`, `websocket_client.py`) operate on the **proxy WS at
  :8765**. Their job is to (a) tell the broker adapter *which symbols to stream onto
  ZMQ* and (b) detect/recover a dead feed. The watchdog's liveness signal comes from
  the proxy WS `market_data` callback (`scanner_ws_watchdog.py:152-153`) — **not**
  from the scanner's own ZMQ socket.

This split is load-bearing: a ZMQ-side stall, or a missing-today condition, produces
a clean silent zero-signal result that the watchdog (watching :8765) cannot see.

### 1.1 Architecture diagram (each box = one file, each arrow = one read/write)

```
                        ┌─────────────────────────────────────────────┐
  Broker WS feed ─────► │ broker/*/streaming  (broker adapter)         │
                        │ websocket_proxy/base_adapter.py              │
                        │   :189 bind ZMQ PUB  :416-432 publish tick   │
                        └───────────────┬──────────────────┬──────────┘
                                        │ ZMQ PUB :5555     │ (also feeds proxy)
                       ┌────────────────┘                   └───────────────┐
                       ▼                                                     ▼
   ┌───────────────────────────────────────┐         ┌──────────────────────────────────┐
   │ PLANE 3: IN-HOUSE SCANNER              │         │ Unified WS Proxy :8765            │
   │ services/scanner_service.py            │         │ websocket_proxy/server.py         │
   │  :770-802 ZMQ SUB loop                 │         └──────────────┬───────────────────┘
   │  :825 aggregator.on_tick               │                        │ market_data cb
   │  bar_aggregator.py:238-239 bar close   │           ┌────────────┴───────────────┐
   │  :832 _on_bar_close                    │           ▼                            ▼
   │  :926 _evaluate_definitions            │   ┌──────────────────┐   ┌──────────────────────┐
   │  scan_rules/fno_intraday_*_chartink.py │   │ scanner_presub-  │   │ scanner_ws_watchdog  │
   │   reads: 5m/15m from aggregator        │   │ scribe.py        │   │  :79-111 check()     │
   │          daily-D/W from historify ◄────┼─┐ │ (tells adapter   │   │  monitors :8765 ONLY │
   │  :968 record_scan_result(inhouse)      │ │ │  what to stream) │   │  :85-87 no_ticks=NOP │
   │  :982 publish ScanHitEvent             │ │ └──────────────────┘   └──────────────────────┘
   └───────────────┬───────────────────────┘ │
                   │ scan_hit bus              │ historify-D read
                   ▼                           │ scanner_history_provider.py:159
   ┌──────────────────────────────┐           │  get_ohlcv(interval='D')
   │ scan_hit_poster.py           │           │
   │  DEFAULT shadow (:38) → log  │           ▼
   │  active → POST to engine      │   ┌──────────────────────────────────────────┐
   └──────────────────────────────┘   │ historify.duckdb                          │
                                       └──────▲──────────────────▲─────────────────┘
                                              │ writes 1m + D     │ reads D
   ┌──────────────────────────────────────────┴───┐              │
   │ PLANE 2: BACKFILL                             │              │
   │ services/scanner_universe_backfill.py         │              │
   │  :74 STORAGE_INTERVALS=("1m","D")             │              │
   │  :91 universe = SCANNER_SYMBOLS (~216)        │              │
   │ services/scanner_backfill_scheduler.py        │              │
   │  boot hook + periodic loop 15:30–17:00 ONLY   │              │
   └───────────────────────────────────────────────┘              │
                                                                   │
   ┌───────────────────────────────────────────────────────────────────────────────┐
   │ PLANE 1: CHARTINK WEBHOOK + SIMPLIFIED ENGINE (independent of in-house scanner) │
   │ blueprints/chartink.py:954-1115  simplified_stock_engine_webhook               │
   │  :970 start_cycle("chartink") → scan_cycle (cycle_kind='chartink')             │
   │  :1076 service.process_chartink_webhook → arms engine watches                  │
   │ services/simplified_stock_engine_service.py                                    │
   │  :346-370 own broker-WS ticks → OWN bar aggregator (NOT scanner's)             │
   │  :1423 history_service.get_history 5m to seed ATR                              │
   └───────────────────────────────────────────────────────────────────────────────┘

   ┌───────────────────────────────────────────────────────────────────────────────┐
   │ CONSUMER: scanner_comparison_eod_service.py   (15:45 IST mon-fri)              │
   │  :116-145 in-house side: scan_results(source='inhouse') JOIN scan_definitions  │
   │  :92-113  chartink side: scan_cycle(cycle_kind='chartink')                     │
   │  DAY-UNION by date-prefix [:10], NOT time-aligned                              │
   │  :166-171 verdict explicitly names "in-house tick starvation"                  │
   └───────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 The three data planes — exactly who reads what, when

| Plane / Consumer | Reads (source) | When | Citations |
|---|---|---|---|
| **In-house scanner** (producer) | TODAY's 5m/15m from **own ZMQ-fed aggregator**; daily-D & weekly from **historify-D** | every bar close (tick-driven) | `scanner_service.py:825,832,910-911`; `scanner_history_provider.py:64-66,159-162` |
| **Chartink webhook** | Chartink POST payload → writes `scan_cycle` (`cycle_kind='chartink'`) | on webhook POST | `chartink.py:1051-1053,1076-1080,1097-1104` |
| **Simplified engine** | (a) Chartink payload to arm; (b) **own** broker-WS ticks → **own** aggregator for triggers; (c) `history_service.get_history` 5m to seed ATR | continuous, market hours | `simplified_stock_engine_service.py:258,299-302,346-370,360,1423-1433` |
| **scanner_comparison_eod** | in-house: `scan_results(source='inhouse')` JOIN `scan_definitions`; chartink: `scan_cycle(cycle_kind='chartink')`; **day-union** | 15:45 IST | `scanner_comparison_eod_service.py:92-113,116-145` |
| **scan_hit_poster** (default **shadow**, no-op) | `scan_hit` event bus → POST to engine webhook only in `active` mode | real-time (active only) | `scan_hit_poster.py:38,159-187,195-244` |
| **Backfill** (writer) | Zerodha history API → `historify.duckdb` (1m + D), universe = `SCANNER_SYMBOLS` | boot + 15:30–17:00 | `scanner_universe_backfill.py:74,91`; `scanner_backfill_scheduler.py:50-51,117-119` |

**Live config confirmed:** `.env:405 SCANNER_ENABLED=true` (the scanner IS running),
`.env:407 SCAN_HIT_POSTER_MODE=shadow` (in-house hits log but do **not** reach the
engine — the engine trades exclusively off the Chartink webhook + its own feed). So
in-house scanner failures today are **observability/comparison failures, not
order-routing failures** — but `scan_hit_poster` flipping to `active` (the documented
intent) would make every silent defect below directly affect orders.

---

## Section 2 — Failure-mode catalog

### Reported failures

**FM-1 — BUY-side collapse / tick starvation (2026-06-09→12, again 2026-06-15: 0 of 25 Chartink BUY names)**
- **Root cause:** bars close **only** on a tick-driven bucket transition
  (`bar_aggregator.py:238-239`); there is **no wall-clock flush**. When ticks stop,
  no later-bucket tick arrives → the current bar never closes → `_on_bar_close`
  (`scanner_service.py:832`) never fires → rules never evaluate. Zero hits, no error.
- **Why tests missed it:** the E2E suite is hermetic (mocked ticks), so it never
  reproduces an *environmental* tick outage; and there is no unit test that asserts
  "starved feed must raise an alert" because there is no alert to assert.
- **Frequency / blast radius:** observed 4+ days this week; affects the **entire
  universe** at once (it's a feed condition, not per-symbol).
- **Severity:** **silent-data-loss** (today, shadow mode) → **production-blocker** if
  `scan_hit_poster` is `active`. Measured today: micro-recall 0.0%, 0/217 cycle-hits
  (`2026-06-15_inhouse_vs_chartink_timealigned.md` §1).

**FM-2 — Backfill universe mismatch: only 38/238 symbols had 1m data (2026-06-12 Friday)**
- **Root cause:** before `scanner_universe_backfill.py` existed, the only 1m backfill
  was sector_follow's, which covers **LOCK_STATIC_30** (+8 indices), **not** the ~216
  `SCANNER_SYMBOLS`. The two universes are near-disjoint
  (`scanner_universe_backfill.py:91` vs `sector_follow_backfill_scheduler` /
  `config_snapshot.json` universe). **Largely fixed** by the new scanner backfill,
  but see FM-9/FM-11.
- **Why tests missed it:** no test asserts coverage of the *full* `SCANNER_SYMBOLS`
  list in historify; the replay harness only had the 38 symbols to work with.
- **Blast radius:** ~84% of the scanner universe un-replayable on Friday.
- **Severity:** **silent-data-loss** (replay/comparison only).

**FM-3 — Day-union precision blindness (2026-06-13: union precision 44% "looked bad")**
- **Root cause:** `scanner_comparison_eod_service.py` matches on the **date prefix
  `[:10]` only** and unions all symbols across the whole day (`:108-110,136-142`).
  It cannot tell an in-session match from a post-close phantom (see FM-6). Today this
  inflated the SELL side to a phantom 0.5 Jaccard (`timealigned.md` §2,§7).
- **Why tests missed it:** the comparator's own unit tests check union arithmetic,
  not time-sensitivity (there is no time-aligned mode to test).
- **Blast radius:** every EOD comparison row; misleads tuning decisions.
- **Severity:** **partial-degradation** (metric quality, not data).

**FM-4 — Stored daily-D universally stale (2026-06-13: D ending 2026-06-04 for 229 symbols)**
- **Root cause:** the in-house rules read daily-D from **stored historify**
  (`scanner_service.py:910` → `scanner_history_provider.py:159-162`
  `get_ohlcv(interval='D')`), and before the backfill covered the `D` interval, those
  bars were ~6 trading days old → every daily gap/volume gate ran on stale data.
  **Largely fixed** by `STORAGE_INTERVALS=("1m","D")` (`scanner_universe_backfill.py:74`)
  with a 15-day D lookback (`:80`), but see FM-9.
- **Why tests missed it:** no test asserts the freshness of stored-D at evaluation
  time; the rules trust whatever `get_ohlcv` returns.
- **Blast radius:** every daily-gated evaluation, every day, independent of ticks.
- **Severity:** **silent-data-loss** (wrong inputs, no error).

**FM-5 — 0 in-house BUY vs Chartink's 25 (2026-06-15 today)**
- **Root cause:** same as FM-1 (tick starvation). Confirmed by the built-in 15:45 job's
  own SELL verdict ("most likely in-house tick starvation … check WS subscription
  coverage") and by the time-aligned analysis (`timealigned.md` §6,§7,§8).
- **Severity:** **silent-data-loss**; persistence is the tell — LTF/CHOLAFIN screened
  on Chartink for 18–20 consecutive cycles and in-house caught **zero**.

**FM-6 — 17 AUROPHARMA SELL fires 16:10–17:30 IST (post-close, 2026-06-15)**
- **Root cause:** **no market-hours gate anywhere in the evaluation path**
  (`scanner_service.py:832,926` have zero clock checks). The rule's `_SETTLE_CUTOFF =
  dtime(15,31)` flips the daily index to `today_idx=-1` purely by wall clock
  (`fno_intraday_sell_chartink.py:56,99-103`), then reads `bars_daily.iloc[-1]` as
  "today's settled bar." If a straggler/backfill tick closes a bar after 15:31 and
  today's D row is stale/absent, a **prior** day's D bar is treated as today →
  spurious post-close SELL. The rule **never verifies `bars_daily.iloc[-1].ts ==
  today`** (`fno_intraday_sell_chartink.py:99-107`).
- **Why tests missed it:** no test runs the rule at a post-close timestamp with a
  stale-D fixture and asserts no-fire.
- **Blast radius:** any symbol that still gets ticks after close (illiquid late
  prints, backfill replays); pollutes every day-union comparison.
- **Severity:** **partial-degradation** today (no orders in shadow) → **production-blocker**
  if `active` (post-close SELL orders on stale bars).

### Discovered failures (from code reading)

**FM-7 — No wall-clock bar-close flush (the mechanism under FM-1/FM-5)**
- `bar_aggregator.py:238-239` is the *only* close trigger. `close_current_bar` exists
  (`:268-282,458-465`) but `ScannerService` never calls it on a timer. A bar that
  should have closed at 11:35 stays open indefinitely if no 11:35+ tick arrives.
- **Severity:** silent-data-loss (root enabler of starvation invisibility).

**FM-8 — `get_today_ohlcv` returns `(None, None)` silently (cross-cutting; also broke sector_follow)**
- `scanner_service.py:757-758` returns `(None,None)` on no-bars with **no log**. The
  caller cannot distinguish "scanner doesn't track this symbol" from "feed
  tick-starved." This is the exact mechanism behind the 2026-06-15 sector_follow
  0-signal incident (MEMORY: `sector-follow-0signals-data-not-logic`), and it is a
  *shared* dependency — the scanner's silence propagates into sector_follow.
- **Severity:** silent-data-loss (multi-consumer).

**FM-9 — Staleness detection is date-granular, cannot see a *partial* today**
- `compute_stale_symbols` (`data_freshness_service.py:216-217`) compares
  `business_days_between(last_date, ref_business_day)`; `business_days_between` returns
  0 once `last_date == ref_business_day` (`:70-84`). So **one bar dated today → "fresh"**,
  even if only the 09:15 bar landed and the whole afternoon is missing. The strategy
  gate at threshold 1 (`:271,303`) is looser still — a *fully* missing today reads
  `ok` (1 business day ≤ 1). Neither detects "morning present, afternoon missing."
- **Severity:** silent-data-loss (the freshness layer gives false assurance).

**FM-10 — Watchdog monitors the wrong feed (:8765, not the scanner's ZMQ :5555)**
- `scanner_ws_watchdog.py:144-156` stamps liveness from the **proxy WS** callback; the
  scanner consumes **ZMQ :5555** (`scanner_service.py:770-774`). A ZMQ-side stall
  while the proxy still gets ticks → watchdog sees "fresh," never recovers, scanner
  starves silently. Also `:85-87` `no_ticks` (last==None) returns with **no log, no
  recovery**.
- **Severity:** silent-data-loss (the one health check has a coverage blind spot).

**FM-11 — Expired-token mass failure reports `success` in backfill**
- Per-symbol fetch failures are absorbed *inside* `create_and_start_job` and never
  surface (`scanner_universe_backfill.py:118-120`); the convergence returns
  `status="success"` even if zero bars landed (`:178-181`). An expired Zerodha token
  can fail all 216 symbols and the data_health row reads healthy.
- **Severity:** silent-data-loss.

**FM-12 — Stale-but-no-error never alerts via Telegram**
- `scanner_backfill_scheduler.py:213-221` fires a Telegram anomaly **only** when
  `res["errors"]` is non-empty (a fetch *exception*), **not** when symbols are merely
  stale. A chronically-behind feed with no exception is invisible except in the
  `data_health_check` row (`alert_sent=0`).
- **Severity:** silent-data-loss.

**FM-13 — No pre-15:30 intraday convergence (today's data absent at scan time)**
- The periodic backfill window is `[15:30, 17:00]` (`scanner_backfill_scheduler.py:50-51,117-119`).
  There is **no intraday top-up before 15:30**. On a normal day (app up since ~3 AM,
  no restart) historify gets no intraday refresh until after close, so any
  before-15:30 historify read runs against yesterday's close. (The scanner's *5m/15m*
  signal comes from the live aggregator, but its *daily-D gates* and any historify
  read are stale intraday.)
- **Severity:** silent-data-loss.

**FM-14 — `scan_hit_poster` active mode would re-POST post-close/stale hits to the engine (latent)**
- Default `shadow` neutralizes it today (`scan_hit_poster.py:38`), but in `active`
  mode (`:195-244`) every in-house hit — including the FM-6 post-close AUROPHARMA
  SELLs — would POST to the simplified-engine webhook. The webhook *does* reject
  arming after `squareoff_time`/`end_time` (`chartink.py:1018-1031`), which partially
  saves it, but this coupling means the scanner's silent defects are one env flag away
  from being order-affecting.
- **Severity:** latent production-blocker.

**Catalog totals: 6 reported + 8 discovered = 14 failure modes.**

---

## Section 3 — Cross-cutting design problems

Each problem is the *single cause* behind multiple failure modes.

### DP-1 — No internal liveness/coverage telemetry; the one watchdog watches the wrong feed
The scanner is purely event-driven on tick arrival with **no wall-clock flush**
(`bar_aggregator.py:238-239`, FM-7) and **no in-process liveness check**. Its only
health monitor watches the proxy WS (:8765), not the scanner's ZMQ feed (:5555)
(`scanner_ws_watchdog.py:144-156`, FM-10). → **drives FM-1, FM-5, FM-7, FM-10.**

### DP-2 — Fail-closed-silently is the universal default
Every missing input becomes `return False` / `(None,None)` / `success` with no log
that names *why*: rules (`fno_intraday_buy_chartink.py:74-81`), `get_today_ohlcv`
(`scanner_service.py:757-758`), backfill (`scanner_universe_backfill.py:118-120`).
→ **drives FM-1, FM-5, FM-8, FM-11.** Contrast Fix 1b's WARNING-on-fallback and
per-symbol PASS/FAIL (`sector_follow_service.py:328-337,716-760`).

### DP-3 — No pre-decision data-readiness check
There is no scanner analog of sector_follow's 15:18 smoke check
(`sector_follow_service.py:1398-1470`). `_evaluate_definitions` (`scanner_service.py:926`)
runs immediately and discovers missing data only inside each rule. → **drives FM-4,
FM-5, FM-13** (evaluation proceeds regardless of whether inputs exist).

### DP-4 — No decision-inputs completeness metric ("0 hits" == "no data" in the logs)
Nothing aggregates "N of M symbols had live data this cycle." A fully-starved scan
is indistinguishable from a genuinely quiet market — exactly what made FM-1/FM-5 look
like normal days. The EOD comparator (`scanner_comparison_eod_service.py`) measures
*output overlap* after the fact, not *input completeness* per cycle. → **drives FM-1,
FM-3, FM-5.** Contrast Fix 1b's `n_symbols_on_live_intraday/total` with WARNING/CRITICAL
thresholds (`sector_follow_service.py:762-790`).

### DP-5 — No market-hours gate on evaluation
`_on_bar_close`/`_evaluate_definitions` have zero clock checks (`scanner_service.py:832,926`),
and the rules' `_SETTLE_CUTOFF` flip trusts the wall clock without verifying the D
bar's own date (`fno_intraday_sell_chartink.py:99-107`). → **drives FM-6, FM-14.**

### DP-6 — Staleness/health layer gives false assurance
Date-granular staleness can't see a partial today (`data_freshness_service.py:70-84,216-217`,
FM-9); stale-but-no-error never alerts (`scanner_backfill_scheduler.py:213`, FM-12);
the pre-15:30 window leaves intraday stale at scan time (FM-13). → **drives FM-4,
FM-9, FM-12, FM-13.**

### DP-7 — Four state stores for "what symbols are alive," no single source of truth
"Which symbols are live right now" is answered differently by four stores, easy to
mismatch:
1. **Master contract** (`SymToken`) — what *can* be subscribed.
2. **Subscription state** — `scanner_presubscribe._subscribed` (`scanner_presubscribe.py:117`),
   the broker→ZMQ stream membership.
3. **Aggregator state** — `ScannerService._bar_history` / `MultiIntervalAggregator`
   (`scanner_service.py:642`), in-memory, lost on restart.
4. **historify.duckdb** — persisted 1m/D, refreshed on a *different* schedule by a
   *different* universe-derivation (`scanner_universe_backfill.py:91` vs sector_follow's 30).
The 06-12 mismatch (FM-2) and the disjoint-universe backfill gap are direct
consequences of (4) diverging from (2). → **drives FM-2, FM-13.**

---

## Section 4 — Test-coverage gap analysis

For each design problem, the scenarios that *should* have a test and don't. (Names +
assertions only — Phase B writes them.)

**DP-1 (liveness):**
- `test_scanner_starvation_emits_alert` — feed ticks for 5 symbols then stop for 10
  min of simulated time; assert a coverage WARNING is emitted (today: nothing is).
- `test_wallclock_flush_closes_stale_bar` — open a bar at 11:30, no further ticks;
  assert a timer flush closes it by 11:36 so the rule can evaluate.
- `test_watchdog_detects_zmq_stall_not_just_proxy` — proxy WS fresh, ZMQ silent;
  assert the scanner-side liveness check fires.

**DP-2 (loud failure):**
- `test_rule_logs_reason_on_missing_daily_d` — daily-D absent; assert a per-symbol
  "data not available: daily-D" log, not a silent `False`.
- `test_get_today_ohlcv_logs_when_no_bars` — untracked symbol; assert a structured
  "no bars for symbol today" reason rather than bare `(None,None)`.
- `test_backfill_reports_failure_on_expired_token` — all per-symbol fetches fail;
  assert status≠"success" and an alert.

**DP-3 (readiness):**
- `test_scanner_smoke_check_pauses_on_low_coverage` — aggregator covers <50% of
  universe at 15:18; assert a smoke-check WARNING/abort (mirrors sector_follow).
- `test_scan_aborts_when_inputs_absent` — no daily-D for the universe; assert the
  cycle logs "inputs not ready" instead of silently producing zero hits.

**DP-4 (completeness):**
- `test_cycle_emits_completeness_metric` — 30 of 216 symbols have live bars; assert
  `n_live/total ≈ 0.14` is emitted and trips CRITICAL.
- `test_zero_hits_with_full_coverage_is_quiet` — full coverage, no rule matches;
  assert completeness=100% and **no** alert (distinguish quiet day from starvation).

**DP-5 (market hours):**
- `test_no_evaluation_after_1530` — feed a post-close tick; assert
  `_evaluate_definitions` is skipped with an INFO log.
- `test_settle_cutoff_verifies_d_bar_date` — post-15:31, stale-D fixture; assert the
  rule does NOT treat a prior-day D bar as today (no AUROPHARMA-style fire).

**DP-6 (staleness):**
- `test_partial_today_flagged_stale` — only morning bars present; assert the freshness
  check reports "partial: last bar 09:15, expected 15:25", not "fresh".
- `test_stale_without_error_alerts` — symbols stale, no fetch exception; assert a
  Telegram stale alert fires.
- `test_intraday_present_before_scan_window` — assert today's intraday is available
  to the scanner *before* 15:30 (currently it is not).

**DP-7 (single source of truth):**
- `test_backfill_universe_equals_subscription_universe` — assert
  `scanner_universe_symbols()` ⊇ the presubscribe set (would have caught FM-2).
- `test_aggregator_restart_replays_from_historify` — restart mid-day; assert the
  aggregator is rehydrated rather than warming from scratch.

---

## Section 5 — Proposed unified architecture (Phase B)

Each proposal cites the failure modes it eliminates and a rough scope. **The template
is Fix 1b** — sector_follow already proves each pattern in-repo.

### P1 — Unified data-access layer for the scanner (aggregator → historify fallback → loud)
Adopt sector_follow's `production_intraday_provider` pattern
(`sector_follow_service.py:259-275,317-338`) inside `ScannerService`: today's
intraday from the aggregator, historical lookback from historify, **WARNING on
fallback**, and an `intraday_source ∈ {aggregator,historify,none}` tag on every read.
Replace the silent `(None,None)` at `scanner_service.py:757-758` with a reason-carrying
return. **Eliminates FM-8; foundation for FM-1/FM-5 visibility.**
*Scope:* `scanner_service.py` (~+80 LOC, modify `get_today_ohlcv` + add a provider),
new `test_scanner_data_access.py`. ~120 LOC.

### P2 — Loud-failure discipline applied universally
Every rule read and every backfill outcome returns either data or a structured
"missing because X" that is logged at WARNING. Add per-symbol PASS/FAIL logging in
`_evaluate_definitions` (mirror `sector_follow_service.py:716-760` + `_gate_fail_reason`
`:424-439`); make the rules log the specific missing input instead of bare `return
False` (`fno_intraday_*_chartink.py:74-93`); make backfill report real failure
(`scanner_universe_backfill.py:118-120,178-181`). **Eliminates FM-2-visibility, FM-8,
FM-11; makes FM-1/FM-5 self-announcing.**
*Scope:* `scanner_service.py`, both rule files, `scanner_universe_backfill.py`,
`scanner_backfill_scheduler.py`. ~150 LOC across 4 files.

### P3 — Pre-decision smoke check at 15:18 IST (and a per-cycle readiness guard)
Register a `scanner_smoke_check` APScheduler job mirroring
`sector_follow_service.py:1398-1470,1848-1854`: probe aggregator coverage ≥ threshold,
historify lookback returns prior-day, broker session live; on failure log loudly +
Telegram (the scanner has no orders to pause, so it's alert-only, not an override).
**Eliminates the blind spot behind FM-4, FM-5, FM-13** (you learn at 15:18 that the
feed is dead, instead of inferring it from a 15:45 zero-hit comparison).
*Scope:* `scanner_service.py` (or a small `scanner_smoke_check_service.py`), wired in
`app.py`. New flag `SCANNER_SMOKE_CHECK_ENABLED`. ~100 LOC + `test_scanner_smoke_check.py`.

### P4 — Market-hours gate on all evaluation
Add a single guard at `_on_bar_close`/`_evaluate_definitions` (`scanner_service.py:832,926`):
skip with an INFO log when `now_ist >= market_close` (configurable, default 15:30).
Separately, harden the rule's `_SETTLE_CUTOFF` flip to verify `bars_daily.iloc[-1].ts
== today` before treating it as settled (`fno_intraday_sell_chartink.py:99-107`).
**Eliminates FM-6; defuses FM-14.**
*Scope:* `scanner_service.py` (~10 LOC), both rule files (~10 LOC each). ~30 LOC +
`test_post_close_evaluation.py`.

### P5 — Decision-inputs completeness metric per cycle
Emit `n_symbols_with_live_data / total` on every evaluation cycle (or a rolling
window), mirroring `sector_follow_service.py:762-790`: <50% WARNING, <20% CRITICAL via
Telegram, counting only aggregator-sourced symbols. This is the single change that
makes "0 hits because no data" visually distinct from "0 hits because quiet market."
**Eliminates FM-1/FM-5 ambiguity and FM-3's misleading tuning hint.**
*Scope:* `scanner_service.py` (a per-cycle aggregation — note the scanner currently
evaluates per-symbol-per-bar, so this needs a lightweight cycle/window accumulator).
~120 LOC + `test_completeness_metric.py`.

### P6 — Single source of truth for the live universe + staleness that sees partial-today
(a) Document and enforce one canonical resolution order for "live universe":
`SCANNER_SYMBOLS` → presubscribe set → aggregator membership, with a boot assertion
that the backfill universe ⊇ presubscribe universe (would have caught FM-2). (b) Make
staleness *bar-count / last-bar-time aware*, not date-granular
(`data_freshness_service.py:70-84,216-217`), so "morning bars only" reports stale. (c)
Add a pre-15:30 intraday convergence pass (or rehydrate the aggregator from historify
on restart via the existing `replay_bars`, `bar_aggregator.py:433-452`) so today's
data exists at scan time (FM-13). (d) Make stale-but-no-error alert
(`scanner_backfill_scheduler.py:213`). **Eliminates FM-2, FM-4, FM-9, FM-12, FM-13.**
*Scope:* `data_freshness_service.py`, `scanner_backfill_scheduler.py`,
`scanner_universe_backfill.py`, a short `docs/` note on canonical resolution. ~180 LOC
— the largest/riskiest item.

### Time-alignment for the comparator (folds into P5's intent)
The day-union comparator (`scanner_comparison_eod_service.py:92-145`) should gain a
time-aligned mode (the `2026-06-15_inhouse_vs_chartink_timealigned.md` harness is the
spec). Lower priority because P3/P5 fix the *detection* of starvation directly; the
comparator is a downstream scorecard. *Scope:* ~80 LOC, additive.

---

## Section 6 — Phase B execution plan (ordered by impact ÷ risk)

### Tier 1 — Quick wins (low risk, high impact) — do first
| # | Fix | Files | Tests to add | Effort |
|---|---|---|---|---|
| 1 | **P4 market-hours gate** + D-bar-date verify | `scanner_service.py`, both rule files | `test_post_close_evaluation`, `test_settle_cutoff_verifies_d_bar_date` | ~0.5 day |
| 2 | **P2 loud failure** (per-symbol PASS/FAIL + rule reasons + backfill real-failure) | `scanner_service.py`, rule files, `scanner_universe_backfill.py` | `test_rule_logs_reason`, `test_backfill_reports_failure` | ~1 day |
| 3 | **P5 completeness metric** | `scanner_service.py` | `test_cycle_emits_completeness_metric`, `test_zero_hits_with_full_coverage_is_quiet` | ~1 day |

Rationale: items 1–3 are *additive* (new logs, a guard, a metric) — they don't change
*which* signals fire, only what is observed/skipped. Item 3 is the single highest-value
change: it ends the "0 hits == no data" ambiguity that caused most of the week's
confusion. Doc updates: `docs/SYSTEM_MAP.md` (scanner health row), `docs/PARAMETER_LOG.md`
(new flags), `CLAUDE.md` scanner section.

### Tier 2 — Structural (higher risk, biggest long-term payoff)
| # | Fix | Files | Tests | Effort |
|---|---|---|---|---|
| 4 | **P1 unified data-access layer** | `scanner_service.py` | `test_scanner_data_access` | ~1 day |
| 5 | **P3 15:18 smoke check** | `scanner_service.py` / new service, `app.py` | `test_scanner_smoke_check` | ~1 day |
| 6 | **P6a/b staleness sees partial-today + single-source-of-truth assertion** | `data_freshness_service.py`, `scanner_backfill_scheduler.py` | `test_partial_today_flagged_stale`, `test_backfill_universe_equals_subscription` | ~1.5 days |

Rationale: P1 underpins P3/P5 cleanly but touches the hot `get_today_ohlcv` path
(shared with sector_follow — regression risk), so it lands after Tier 1's tests exist.
P6 changes freshness semantics — highest blast radius — so it goes last in Tier 2 with
the most test coverage. Doc updates: `docs/SYSTEM_MAP.md` databases/health, a new
"canonical live-universe resolution" note.

### Tier 3 — Nice-to-haves (defer)
| # | Fix | Why deferred |
|---|---|---|
| 7 | **P6c** wall-clock bar flush / aggregator rehydrate-on-restart | Real fix for FM-7/FM-13 but invasive in `bar_aggregator.py`; Tier-1 completeness metric already *surfaces* the problem. |
| 8 | **P6d + FM-10** scanner-side ZMQ liveness watchdog | Closes the :8765-vs-:5555 blind spot; valuable but the 15:18 smoke check (item 5) already gives a daily liveness signal. |
| 9 | **Time-aligned comparator mode** | Downstream scorecard; P3/P5 detect starvation upstream regardless. |

---

## Section 7 — Recommendation

**Dheeraj — Phase B should do three things first, in this order, and they are small:
(1) add a market-hours gate plus a D-bar-date check to kill the post-close AUROPHARMA
SELLs; (2) add loud per-symbol PASS/FAIL + missing-input logging to the rules,
backfill, and `get_today_ohlcv`; and (3) emit a per-cycle decision-inputs completeness
metric (`n_live/total`, <50% WARNING / <20% CRITICAL) — copying sector_follow's Fix 1b
verbatim.** Those three are additive, low-risk, touch no signal logic, and together
they convert "a different mysterious failure every day" into "the scanner tells you,
loudly, exactly which input was missing and how much of the universe was live." Only
*after* those land (and their tests exist) should you take on the structural tier —
the unified aggregator→historify data-access layer (P1), the 15:18 smoke check (P3),
and partial-today-aware staleness with a single-source-of-truth universe assertion
(P6) — because those touch the hot `get_today_ohlcv` path and freshness semantics
shared with sector_follow. The wall-clock bar flush, a scanner-side ZMQ watchdog, and
a time-aligned comparator are real but deferrable: the completeness metric already
*surfaces* starvation, so the rest is hardening, not triage. Net: ~3 days for Tier 1
(the triage), ~3.5 days for Tier 2 (the cure), with the whole effort being "apply the
discipline that already works in `sector_follow_service.py` to `scanner_service.py`."

---

## Appendix — Things that surprised me (every one cited)

1. **The scanner and its watchdog watch different feeds.** The scanner reads ZMQ :5555
   (`scanner_service.py:770-774`); the watchdog reads proxy WS :8765
   (`scanner_ws_watchdog.py:144-156`). The one health check has a structural blind
   spot for the exact failure it exists to catch.
2. **There is no wall-clock bar close at all.** A bar closes *only* when a later-bucket
   tick arrives (`bar_aggregator.py:238-239`). Starvation isn't "the scanner errors" —
   it's "the scanner goes quiet and that looks identical to a calm market."
3. **The boot-race is already fixed** (it surprised me given the MEMORY note): the
   presubscribe retry now waits up to 2 h and the connect-callback stays armed
   permanently (`app.py:854-884`) — the old 30-s-then-give-up race is gone. The
   remaining starvation is mid-day ZMQ/feed loss, not the boot race.
4. **The simplified engine is NOT a consumer of in-house scanner output.** It trades
   off the Chartink webhook + its *own* aggregator (`simplified_stock_engine_service.py:346-370`);
   `scan_hit_poster` defaults to `shadow` (`scan_hit_poster.py:38`). So this week's
   failures were observability failures, not order failures — *but* one env flag
   (`SCAN_HIT_POSTER_MODE=active`) would make every silent defect order-affecting
   (FM-14).
5. **The freshness gate that sector_follow relies on can't see a half-missing day**
   (`data_freshness_service.py:70-84,216-217`) — "Friday present" reads fresh even when
   today's afternoon tape never arrived. The layer meant to prevent the incident has
   the same date-granular blind spot that caused it.
6. **The post-close SELL bug is a rule-logic bug, not a scheduler bug.** Nothing
   *schedules* a 16:10 evaluation; a straggler tick closes a bar and the rule's
   `_SETTLE_CUTOFF=15:31` flip (`fno_intraday_sell_chartink.py:99-103`) trusts the wall
   clock without checking the D bar's actual date. Remove the post-close evaluation
   *and* harden the date check — either alone is insufficient.
