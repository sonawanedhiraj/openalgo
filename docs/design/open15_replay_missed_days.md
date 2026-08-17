# open15_vol_breakout — one-click replay of a missed session

**Status:** proposal · **Date:** 2026-08-17 · **Prompted by:** 2026-08-12 (feed
delivered zero ticks) and 2026-08-17 (`skipped_late_boot`) — two of six trading
days lost, both leaving a blank day on `/open15_vol_breakout/logs`.

Prior art: the throwaway harness in `backtest/open15_missed_days/` and its
findings in
[`2026-08-17_missed_sessions_reconstruction.md`](../research/strategy/open15_vol_breakout/2026-08-17_missed_sessions_reconstruction.md).
This promotes that harness into the product.

## 1. Goal and the one rule that shapes everything

**Goal.** On a day the strategy did not run, the operator clicks **↻ replay** on
the day card and the day fills in — selection, watch list, triggers, exits,
summary — rendered by the same timeline as a real day.

**The rule.** A replayed day must be *legible as a real day* and
**impossible to mistake for one**. Those pull in opposite directions, and every
decision below resolves in favour of the second. Concretely:

- replay P&L never enters `total_realized_pnl()`, so it can never compound into
  tomorrow's real position size;
- no replayed number is ever rendered without a badge next to it;
- a day with even one real fill can never be replayed over.

This is the #548/#555/#581 bucket discipline applied to a fifth class. It is
also why the answer to "can we just journal the numbers?" is no: the reconstruction
carries an error band wide enough to flip sign (§5), and a single unlabelled
figure would misrepresent that.

## 2. What replay can and cannot reproduce

Established empirically in the reconstruction doc, and it bounds the whole feature.

| | fidelity | evidence |
| --- | --- | --- |
| Universe, prev-closes, first candle | **exact** | 09:15 1m bar O/H/L *is* what the live 09:16 quote snapshot reads (SPEC §3) |
| Seed selection + gaps | **exact** | 08-14 control reproduces all 6 picks and their gaps |
| OI filter (#595) verdicts | **exact** | reproduced MFSL 418 / JUBLFOOD 346 / CHOLAFIN 482 lots |
| Volume gate, `top_n`, `trade_side`, shadow, caps | **exact** | the real `Open15Core` is driven, not reimplemented |
| Rolling watch list | **approximate** | live re-ranks 2×/min on LTP; bars support 1×/min |
| Which symbols trigger | **~60%** | 6 of 10 live triggers reproduced at the same minute across two controls |
| Entry PRICE | **not resolvable** | the gate fires mid-minute; bars only close the minute |

The last row is the whole reason this is a measurement feature and not a
backfill. So replay reports a **band**, never a point:

- **close-entry** — the R58-honest convention, entry at the trigger minute's
  close. Pessimistic.
- **early-entry** — entry at the trigger minute's option open. Optimistic.

The live fill sits between them. On both missed days the band spans zero.

## 3. Eligibility — when the button appears

`replay_eligibility(date)` returns `eligible | ineligible(reason)`:

| condition | verdict |
| --- | --- |
| not an NSE trading day (`data_freshness_service.is_trading_day`) | ineligible — `not_a_trading_day` |
| any `open15_trades` row for the date with `fill='real'` | **ineligible — `day_was_traded`** |
| day log has `skipped_late_boot` / `skipped_no_prev_closes` / `no_ticks_received` | eligible |
| no day log at all (app was down) | eligible |
| day log exists, armed cleanly, and simply had no triggers | ineligible — `day_ran_normally` |
| date is today and now < 09:45 IST | ineligible — `too_early` (broker current-day history lags 5–15 min) |
| market hours (09:00–15:40 IST) | **eligible, with a `warning`** (operator decision 2026-08-17) |

The last two are load-bearing operationally: replay makes ~250 historical calls,
and running it during the session competes with the live strategy for the
broker's 3 req/s budget.

**`day_was_traded` is checked again inside the writer**, immediately before any
write, not only at button-render time — the same defence #597 added after a
late-boot arm clobbered a traded day's persisted `day_log`.

## 4. Architecture

### 4.1 `services/open15_replay.py` — the engine

Promoted from `backtest/open15_missed_days/`, with the harness's four stages
kept as functions so each is testable:

1. `fetch_session_bars(date, universe)` — equity 1m 09:15–09:31 + daily D for
   prev closes, via `history_service.get_history`.
2. `resolve_contracts_and_oi(date, candidates)` — ATM contracts through the live
   `open15_option_shadow.resolve_atm_option`; **OI read off the 09:15 bar**, which
   is stamped ~09:16:00 and is what the live quote sees. ⚠ Reading the 09:16 bar
   instead put NMDC through the 500-lot floor on 08-17 and manufactured a phantom
   +₹17,924 on a ₹1.67 put. This is the single most breakable detail in the
   feature and gets its own regression test.
3. `run_core(date, cfg, bars, oi)` — drives the real `Open15Core`, one synthetic
   tick per minute, two passes per minute so the re-rank ranks a consistent price
   set. Returns actions + the core's own `selected` / `rolling_adds` /
   `watch_stats` / `liquidity_exclusions`.
4. `price_legs(...)` — option legs via the live `option_shadow` premium
   convention and `option_round_trip_charges`.

**Day config resolution** (`resolve_replay_config(date)`) prefers, in order:
the date's own persisted `armed` event → the `open15_config` row → env defaults.
The chosen source is recorded on the replay event, because a config that drifted
since the missed day silently changes the answer.

Nothing here imports Flask; the CLI and the endpoint are both thin callers.

### 4.2 Emit the SAME events, not new ones

The page renders a day by walking its decision-log events. So the replay writes
the identical event vocabulary — `armed`, `selection`, `universe_excluded`,
`watchlist_add`, `entry`, `exit`, `watch_stats`, `no_entry`, `summary` — and the
whole existing UI works with no per-event rendering changes. Each event gains
`replay: true`.

Two events are new:

- **`replay_meta`** — first event of the day. Provenance: run timestamp, engine
  version, config source, bar counts, symbols that failed to fetch, and the
  eligibility reason the day qualified under. This is what the banner renders.
- **`exit_replay`** — mirrors `exit` but carries the **band**
  (`pnl`/`charges`/`net` on the close-entry convention plus `net_early`), under a
  distinct name so the digest cannot sum it into `exit` (the #581 lesson: folding
  a measurement bucket into a traded one destroys exactly the comparison it exists
  for).

### 4.3 Journal rows

`fill='replay'`, `status='closed'`, `mode` = the mode the day would have run in.

- **`'replay'` is appended to `NON_REAL_FILLS`.** Without this one line, replay
  P&L flows into `total_realized_pnl()` and compounds real position sizes off
  money that never existed.
- `pnl` / `charges_inr` carry the **close-entry** convention only, and
  `replay_pnl_by_date()` aggregates exactly that — the #552 single-definition
  rule survives untouched.
- **The early-entry band is persisted as a PRICE, not as a second P&L.** A new
  nullable `opt_entry_premium_early` column stores the trigger minute's option
  open; the optimistic net is then *derived* by running the same
  `net_pnl_of_row` machinery against that entry price. One definition, one
  derivation, two inputs. Storing a `replay_pnl_early` figure instead would
  create a parallel P&L convention with its own charges — precisely the shape
  #552 was written to stop, and it would rot the moment the charge model
  changed. Deriving also means the band stays queryable across days without
  parsing day logs.
- `watch_source` (`seed`/`rolling`) is journaled as usual so replayed days can
  still be split by cohort.
- Rows whose trigger minute is the last entry minute (09:29 by default) get
  `reason='replay_degenerate_hold'`: a 09:29:59 synthetic trigger fills at the
  09:30 open, which is also the exit, so the row reduces to charges. Three of
  08-12's six triggers were this. The UI greys them.

### 4.4 API

| endpoint | notes |
| --- | --- |
| `GET /api/replay/eligibility?date=` | drives button visibility + tooltip |
| `POST /api/replay` `{date, force?}` | starts a run; 409 if one is in flight, 403 on ineligible |
| `GET /api/replay/status?date=` | `queued\|running\|done\|failed`, progress `n/total`, error text |

A run is ~250 broker calls ≈ 90–150 s at the 3 req/s limit, so it goes on a real
OS thread (never eventlet-blocking) with one run at a time process-wide. All
three are `@check_session_validity`.

`force` exists only to re-run an *already replayed* day (config changed, engine
fixed). It cannot override `day_was_traded`.

## 5. UI

Sidebar day card, missed day:

```
2026-08-17            [skipped]
skipped_late_boot            ↻
```

The `↻` appears only when eligible; hover gives the ineligibility reason
otherwise. Click → inline progress on the card (`replaying 84/211…`) → refresh.

After replay the card reads:

```
2026-08-17     [replay] −₹13,608
3 sel · 3 replayed · 3 sim
```

The amount is **never bare** — it sits behind the `replay` badge, the same
protection `paper`/`sim`/`shadow` already have, and it shows the close-entry
number because that is the conservative end.

Main pane gains a provenance banner above the timeline (reusing `.rejbanner`
styling in a neutral colour), stating: this is a reconstruction, the band, what
is exact vs approximate, the data source, and when it ran. Below it the timeline
renders as normal with a `replay` tint, and a band chip pair replaces the single
net chip.

New CSS only: `.b-replay` badge and `.ev-replay_meta` / `.ev-exit_replay` event
colours, in the existing palette (violet-grey — deliberately not green, amber or
teal, which already mean real / paper / shadow).

## 6. Validation — the acceptance gate

Explicit operator requirement: **validate replay against days that did trade,
starting when implementation starts, not after.**

`services/open15_replay_control.py` + `uv run python -m services.open15_replay_control
--from 2026-07-01 --to <yesterday>` replays every day that HAS a journal and scores:

| metric | gate |
| --- | --- |
| seed selection — symbols and sides identical to the day's `selection` event | **100%** on days whose log records one |
| gap values | within 0.01pp |
| OI verdicts vs the day's `universe_excluded` stage-3 events | **100%** |
| trigger overlap (symbol + minute) | report; expected ~60%, **regression if it drops** |
| per-trade P&L sign vs journal | report |
| real-bucket totals | must be **unchanged** — the control run writes nothing |

Two of these become pytest regressions with committed fixtures (no broker
needed): the 2026-08-14 selection match (the OI-source trap) and
`NON_REAL_FILLS` containing `replay` so `total_realized_pnl()` excludes it.

**Phase 1 is not done until the control report is green on selection and OI and
its output is posted to the issue** — per the operator validation rule, evidence
before close.

## 7. Phasing

| phase | scope | issue |
| --- | --- | --- |
| **P1** | `open15_replay.py`, `fill='replay'` bucket, `replay_pnl_by_date`, CLI, control-validation harness + report | code + tests, no UI |
| **P2** | eligibility, the three endpoints, threaded runner | |
| **P3** | sidebar button, `replay` badge, provenance banner, band chips | validated by clicking from `/strategies` → open15 card → logs, with screenshots on the issue |
| **P4** *(optional)* | `postmarket_review` raises a contract violation on a missed session and names the replay URL | |

Each phase is its own issue and PR off `dev`. P1 and P2 are backend-only and
independently mergeable; P3 is the only one that touches `_LOGS_PAGE`.

## 8. Production isolation — what guarantees it, and what tests it

The live strategy trades **real money** (since 2026-07-24). Replay must be
incapable of touching it. Stated intent is not enough, so each guarantee below
names the mechanism *and* the test that fails if it regresses.

| # | Guarantee | Mechanism | Test |
| --- | --- | --- | --- |
| G1 | Replay can never place an order | `open15_replay.py` never imports `order_placer` / `place_order` / `production_order_placer` — it consumes `Open15Core` as a pure library and prices from bars | assert the module's import graph contains no order path |
| G2 | Replay P&L never compounds | `'replay'` in `NON_REAL_FILLS` | `total_realized_pnl()` unchanged with replay rows present |
| G3 | Nothing new runs at boot | no import of `open15_replay` from `app.py` or any booted service; no APScheduler job, no thread, no catalog entry in P1 | boot-import test asserts the module is absent from `sys.modules` after app import |
| G4 | A traded day is never overwritten | eligibility check **plus** a re-check inside the writer, inside the same transaction | test: replaying a day with a `fill='real'` row raises and writes nothing |
| G5 | The live service is untouched | P1 changes **zero** lines of `open15_breakout_service.py` | the PR diff is the evidence |
| G6 | Existing rows keep their meaning | the new column is nullable and added by the existing `_ensure_columns()` ALTER path; `_REAL_FILL` stays NULL-tolerant | test: pre-existing rows still classify as real and their net P&L is byte-identical |
| G7 | No contention with the live feed | replay reads the broker **historical** API through `history_service` (already 3 req/s limited); it never subscribes ZMQ, never opens `historify.duckdb` read-write | eligibility blocks market hours; single run process-wide |

**The `NON_REAL_FILLS` edit is additive-only.** Adding `'replay'` widens an
exclusion list, so for all existing data — where no row has `fill='replay'` —
every aggregate returns exactly what it returned before. That is the property
G2 and G6 pin, and it is why this one-word change is safe to ship ahead of the
feature that produces such rows.

**Rollout order is itself a safety property:** P1 ships the bucket, the column
and the engine with no way to invoke them from the UI. So the first merge is
inert in production by construction — the only observable change is a nullable
column and a longer exclusion tuple. P2 adds an endpoint that refuses to run
during market hours. Only P3 makes it clickable.

## 9. Risks

| risk | mitigation |
| --- | --- |
| Replay P&L read as real | `NON_REAL_FILLS`, badge-always, distinct events, banner — four independent layers |
| A traded day clobbered | eligibility check **plus** a re-check inside the writer (#597) |
| Broker session dead / token expired | eligibility surfaces it; the run fails loudly with the fix ("re-login to Zerodha") |
| Current-day history lag | `too_early` before 09:45 IST |
| Competing with the live strategy for broker quota | blocked during market hours; single run process-wide |
| OI source silently regressing to the 09:16 bar | dedicated regression test on the 08-14 fixture |
| Rolling-list drift making replay look worse over time | control report tracks trigger overlap as a monitored metric |
