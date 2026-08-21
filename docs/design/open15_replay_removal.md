# Removing the open15 replay feature — plan

**Status:** plan, awaiting execution · **Date:** 2026-08-17 · **Operator decision**

Supersedes [`open15_replay_missed_days.md`](open15_replay_missed_days.md).

## Why it is being removed

The feature answered the wrong half of the question, and the half it answered
cannot be fixed.

**1. Replayed P&L is structurally undecidable, not merely imprecise.** The entry
gate fires at a TICK inside a minute; 1m bars cannot say when, so the entry price
is bounded by the minute's open and close. On ATM options those bounds are far
apart — 2026-08-17 came out **−₹13,501 close-entry / +₹13,112 early-entry**, a
₹26.6k span that crosses zero. And the tick data that would resolve it is absent
*by construction*: the only two ways a day gets missed are a dead feed (no ticks
to capture) and a late boot (capture never starts). Both missed days have no tick
log. So this is not a gap that more work closes.

Today's partial equity tick log (from the engine's writer, coverage 09:20:07+)
was checked and does not rescue it: 4 of 6 triggers were rolling additions whose
watch-start must be modelled, 1 fell before coverage, and the single resolvable
case differed from close-entry by 0.13% on the equity leg — while the option
premium, which carries the P&L, remains 1-minute regardless.

**2. It destroyed the evidence it was meant to explain.** `save_day_log`
replaces, so replaying a day overwrites its decision log. Both incidents lost
their record:

| date | lost |
| --- | --- |
| 2026-08-12 | `no_ticks_received`, `first_candles covered:0` — the zero-tick feed failure |
| 2026-08-17 | `skipped_late_boot` + 6x `late_boot_restart preserved_events:N` |

That is the #597 clobber pattern — already fixed once in this codebase —
reintroduced by design. No DB backup covers those dates.

**3. It cost eight defects in one day** (#602, #606, #608 x2, #613, #615, #617,
plus the log clobber), six of them in seams rather than in the engine. The
selection reconstruction *is* exact and validated, but "what would it have
watched" was never the question worth this much surface area.

## Order matters: data before code

Once the code is gone so is `delete_replay_rows`, and the 12 replay rows would
sit in the journal with `NON_REAL_FILLS` as the only thing keeping them out of
real P&L.

### Phase 0 — data (operator-run, no code change)

1. **Restore the two clobbered day logs.** The only surviving copies are the
   fragments captured in this session's transcript — 2026-08-17 verbatim,
   2026-08-12 near-complete. Write them back with a marker saying they are
   reconstructed from a transcript capture, not recovered from a backup.
   *Alternative considered:* delete the two rows so the days read "no log". The
   restore is preferred because `no_ticks_received` is the incident evidence and
   its absence is worse than a labelled reconstruction — but the operator should
   choose, because a reconstructed log is exactly the sort of thing this
   codebase otherwise refuses to write.
2. **Delete the 12 `fill='replay'` rows** (6 on 2026-08-12, 6 on 2026-08-17).
3. **Assert `total_realized_pnl() == 2567.86`** before and after. It never
   counted them; this proves it.

### Phase 1 — code removal (one issue, one PR)

Delete outright — nothing else imports them:

- `services/open15_replay.py`
- `services/open15_replay_control.py`
- `test/test_open15_replay.py`, `test/test_open15_replay_api.py`
- `test/fixtures/open15_replay_2026-08-14.json`

Strip from shared files:

| file | remove |
| --- | --- |
| `blueprints/open15_breakout.py` | 3 routes, `_REPLAY_LOCK`/state/worker, the day-card button + progress, `renderReplayBanner`, band chips, `.b-replay`/`.rbtn`/`.rprog` CSS, `entry_replay`/`exit_replay` render branches, `replay_pnl` wiring |
| `services/open15_log_view.py` | `replay`/`replay_pnl` in `summarize_day`, the two `selection_outcomes` branches |
| `database/open15_breakout_db.py` | `replay_pnl_by_date`, `early_entry_net_pnl`, `delete_replay_rows`, `real_fill_dates`, `has_real_fill` |
| `test/test_open15_log_view.py` | the replay-day tests only |

## Deliberately KEPT, each for a reason

- **`'replay'` stays in `NON_REAL_FILLS` — permanently.** A latch, not a
  leftover: if a replay row ever survives, is restored from a backup, or the
  feature is ever re-added, removing it from that tuple silently reclassifies it
  as REAL and compounds it into live position sizing. It costs one string.
- **`opt_entry_premium_early` column stays.** Nullable and unused; dropping a
  column from a live SQLite table is the riskier operation. Documented as
  vestigial.
- **The #612 clobbered-log fix stays.** It repairs 2026-08-13 / 2026-08-04,
  which have nothing to do with replay, and its tests stay with it.
- **`csrfToken()` stays.** The config form now uses it (#613).
- **The pre-ship checklist in `CLAUDE.md` stays.** Earned independently of this
  feature.
- **The research doc and STRATEGY_REGISTRY round 62 stay.** The repo keeps
  rejected work permanently; round 62 gains the removal outcome so the next
  person does not rebuild this.

## Docs

- This file supersedes `open15_replay_missed_days.md`; mark that one **REMOVED**
  with a pointer here.
- Append the outcome to `strategies/STRATEGY_REGISTRY.md` round 62.
- `docs/PARAMETER_LOG.md` and `docs/SYSTEM_MAP.md` mention "replay" for the
  WS-reconnect historical replay and the backtester — **verify before touching;
  they are unrelated.**

## Verification

1. `uv run pytest test/test_open15*.py -q` green on the reduced set.
2. `node --check` on both inline `<script>` blocks of `_LOGS_PAGE`.
3. Browser walkthrough from `/strategies` → open15 → logs: 2026-08-12 and
   2026-08-17 show **no** replay button, banner or band chips; **2026-08-13
   still shows its #612 banner and its 8 trades**; 2026-08-14 unchanged.
4. `total_realized_pnl() == 2567.86`.
5. Boot-import check: nothing new in `sys.modules`, no scheduler job, no thread.

## Risk

Low. Nothing replay-related runs at boot, there is no scheduler job and no live
order path. The only shared-surface risks are the `summarize_day` digest shape
(pinned by tests) and the JS↔Python row-builder parity test — both mechanical.
