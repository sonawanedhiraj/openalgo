# open15 — trade rating in production: live grades on the watch list, grade filter, paper for the rest

**Date:** 2026-09-14 · **Issues:** #726 (build) · R63 report #725 · **Target:** armed session of 2026-09-15 if the PR merges and OpenAlgo restarts before 09:10 IST; otherwise the next session.

## 0. What the operator asked for, and how it is read

1. The rating shows on every stock the moment it appears in the seed selection or as a rolling add.
2. The rating updates in real time — the clock advances, the tape moves, the volume ratio builds.
3. A UI control chooses which grades are traded: any subset of A / B / C (all = today's behaviour).
4. Whatever the choice, the strategy keeps *paper-trading* what it does not trade for real, so every grade
   keeps being measured — the same discipline as the #581 shadow side and the #721 `vol_ratio_cap` sim rows.

**Routing stays where it is.** Whether "traded" means a real Zerodha order or a sandbox-book order is
still the two-control rule of issue #440 (the navbar Analyze toggle + the per-strategy Live/Sandbox toggle on
`/strategies`). This page only *shows* the effective routing. To run the filter as a pure paper experiment
first — recommended for the first week — flip open15 to **Sandbox** on `/strategies`; the grade filter and
every row below behave identically, on the virtual book. No new mode is added.

## 1. The grade (R63 v2 — three checks, frozen at the trigger)

| Check | Rule | Known when |
|---|---|---|
| `early` | trigger at or before **09:22:00** | clock |
| `market_ok` | equal-weight **universe median return** from each symbol's 09:15 open to now **> −0.30 %** | every tick (cheap: ~207 floats) |
| `clean_vol` | volume ratio at the trigger **< 1.55×** (the gate is 1.5×; above 1.55× the surge arrived before the price did) | at the trigger; pre-trigger the running while-beyond ratio is shown |

**C** = `market_ok` false **or** the clock is past **09:24:00**. **A** = not C, `early`, `clean_vol`. **B** = everything else.

Two ratings exist for a watched symbol and the UI names which it is showing:

- **Provisional** (pre-trigger, recomputed every status poll): what the grade *would be if it triggered now*. It
  decays with the clock — every untriggered row drops from A-eligible to B at 09:22:01 and to C at 09:24:01 —
  and flips with the tape when the universe median crosses −0.3 %. The volume check is shown as the running
  while-beyond ratio; it cannot fail a provisional grade (no trigger, no ratio).
- **Final** (at the trigger, on the ZMQ tick thread, frozen): stamped on the journal row and the entry event.
  This is the grade the filter acts on and the grade research reads. A row's chip changes from outlined
  (provisional) to filled (final) the moment it triggers.

## 2. Data flow (all in-process, no broker call on the tick thread — the #626 rule)

```
_handle_raw (ZMQ tick thread)
  ├─ core.on_tick(...)                       → may return an entry action
  ├─ self._last_ltp[symbol] = price          → NEW: 207-float dict, single writer
  └─ _enter(action)
       ├─ grade = grade_trigger(now, univ_median(), vol_ratio)   → NEW, pure, ~1 µs
       ├─ action["rating"] = grade; action["rating_inputs"] = {...}
       ├─ (existing) #721 ceiling → sim
       ├─ (existing) #581 shadow side → shadow
       ├─ NEW  grade not in day_config["trade_grades"] → _journal_shadow(action, reason="rating_excluded")
       ├─ (existing) profit lock → sim ; max_trades cap → sim
       └─ (existing) _enter_option / stock entry → real order

get_status()  (Flask thread, polled every 5 s by /logs during the window)
  └─ NEW "rating": { "univ_median_pct", "clock", "market_ok", "phase": early|late|closed,
                     "provisional": { sym: {"grade","fails":[...],"vol_ratio_now"} },
                     "trade_grades": ["A","B"], "counts": {"A":n,"B":n,"C":n} }
```

- `univ_median()` = median over symbols with both a 09:15 open (the `first_candles` snapshot, already held
  by the core) and a last LTP. Computed on demand from the dict; no lock needed (single writer, one-pass read
  — the same argument as `watch_snapshot`).
- `grade_trigger` lives in a new pure module `services/open15_rating.py` so the R63 harness, the unit tests
  and the service call the same function (the `resolve_band`/`pick_contract` drift lesson from #669).
- **Excluded grades are paper-traded at full slot size** through the existing `_journal_shadow` seam:
  `fill='shadow'`, `reason='rating_excluded'`, `entry_shadow` event carrying `rating` + `rating_excluded: true`.
  Shadow rows never call `order_placer`, never read the position book, never consume a `max_trades` slot,
  and stay out of real P&L — every guarantee #581 already carries. They share `shadow_max_trades`
  (recommend raising it from 3 to 6 for the measurement period so a C-heavy day is fully priced).
- The grade is decided **before** the #721 ceiling and the shadow-side check so a capped or shadow row still
  gets its grade (the ceiling and `clean_vol` overlap; a 1.7×+ trigger is graded B/C *and* sim-priced).

## 3. Persistence — no new event names (#615/#622)

- `open15_trades`: `rating VARCHAR(1)`, `rating_univ_median_pct FLOAT`, `rating_vol_ratio FLOAT`,
  `rating_provisional_at_add VARCHAR(1)` (what the chip showed when the symbol was first watched — measures
  how often a grade decayed before the trigger).
- Events: `rating` + `rating_inputs` ride the existing `entry`, `entry_shadow`, `entry_skipped`,
  `entry_rejected`, `entry_error`; `selection` and `watchlist_add` gain `rating_provisional`; `armed` records
  `trade_grades` and the three thresholds; `summary` gains `by_grade` (real / paper counts and net).
- `open15_config`: `trade_grades VARCHAR(8)` (e.g. `"ABC"`, NULL = all; env seed `OPEN15_TRADE_GRADES`,
  first boot only). Thresholds are **code constants** for now (pre-registered; a UI knob would invite the
  monthly re-cut R63 warns against). Applied at the next 09:10 arm like every other `open15_config` field.

## 4. UI — `/open15_vol_breakout/logs` (sample: the published mockup)

1. **Header strip**: routing badge (LIVE / SANDBOX — link to `/strategies`), IST clock, universe-median chip
   (green above −0.3 %, red below), the grade-phase pill (`A-eligible until 09:22` → `B until 09:24` → `C`).
2. **Config form** gains a "Trade rating" block: three checkboxes A / B / C, the sentence *"Unchecked grades
   are paper-traded at full slot size and shown as PAPER below"*, and the frozen thresholds in muted text.
   Saved through the existing `/api/config` POST (same clamp-don't-reject treatment as the rolling knobs).
3. **Watch list** (the existing outcome table — seed and rolling rows already have a row each, #559): a new
   **Grade** column with the chip and, on hover, the failing checks; a **Will** column reading REAL / PAPER
   (from `trade_grades`) that becomes the actual outcome once the row triggers. Provisional chips are
   outlined; final chips are filled. The column re-renders on every 5 s status poll and on the 1 s clock tick.
4. **Today's trades**: grade chip on every row (real, paper, sim, shadow), `rating_excluded` rows badged PAPER.
5. **Digest card "By grade"**: per grade, real n / WR / net and paper n / WR / net, with the pre-registered
   rule printed under it so the decision is visible every day.

Both row builders get the chip — the JS in `blueprints/open15_breakout.py` and its Python twin
`open15_log_view` (the page has gone dark twice on an event one builder was not taught).

## 5. Rollout for tomorrow

| When | What |
|---|---|
| tonight | branch `feat/726-open15-trade-rating`, PR with tests (§6), operator merge, restart before 09:10 IST |
| 2026-09-15 09:10 | `armed` records `trade_grades`; chips appear at 09:16 with the seed selection |
| week 1 (recommended) | open15 on **Sandbox**, `trade_grades = ABC` — everything graded, nothing changes, chips validated against the R63 harness on live days |
| week 2+ | operator picks the traded set (R63 suggests `AB`; C is 13 % WR on 48 real fills and 40 % on the holdout); C rows continue as PAPER |
| ~40 new real fills (≈ late Oct) | **pre-registered**: keep C excluded only if the C paper cohort is net-negative AND ≥ 15 pts below A+B on the new fills alone; A/B never gate; if it fails, delete the grade, do not re-tune |

What the filter is expected to do, on the R63 numbers (in-sample, 20 days, correlated): trading A+B only
would have removed 15 of 48 trades and −₹31.6k; on the September half it removes 10 of 24 and −₹14.7k of a
−₹26.7k month. The A grade will not repeat its August 9-for-9; plan on ~60–65 %.

## 6. Tests (all through pytest — never `uv run python` against the live DB)

- `test/test_open15_rating.py`: grade boundaries at 09:22:00 / 09:24:00 / −0.30 % / 1.55×; provisional vs
  final; median with missing opens; the 48 R63 real rows replayed through `grade_trigger` equal the report's
  §7 grades exactly.
- `test/test_open15_breakout_service.py`: excluded grade → `entry_shadow` with `reason='rating_excluded'`,
  no `order_placer` call, no `max_trades` slot, full-slot sizing; included grade → unchanged path; a raising
  grader never blocks an entry (fail OPEN to B, logged).
- `test/test_open15_log_view.py`: both builders render the chip for real / paper / sim / shadow rows.
- Registry tests: no new jobs or threads (nothing to add).

## 7. Out of scope

The 60-second follow-through exit (a different rule family), any news term, UI-editable thresholds.
