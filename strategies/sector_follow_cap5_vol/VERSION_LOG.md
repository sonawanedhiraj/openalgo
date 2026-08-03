# Sector Follow (Cap-5, Volume-Tiebreaker) — Version Log

## v0.2.0 — 2026-08-02 (issue #512)
**Entry/exit moved earlier for the NSE Closing Auction Session (effective
2026-08-03).** Behavior change — not a tuning.

- From 2026-08-03 NSE ends continuous trading in CAS-eligible cash scrips at
  **15:15 IST** and runs an auction 15:15–15:35; its 15:25–15:30 phase accepts
  **LIMIT orders only**. CAS-eligible = every F&O name = **the entire
  `LOCK_STATIC_30` universe**. F&O derivatives separately extend to 15:40; the
  cash segment for non-F&O names is unchanged at 15:30.
- **This invalidates the v0.1.1 note** ("Verified product = CNC → not subject to
  sandbox's 15:15 MIS square-off; the 15:20 entry time is safe, no timing change
  needed"). Being CNC is exactly what makes it *worse* now: there is no MIS
  auto-square-off underneath, so a T+1 MARKET exit rejected in the limit-only
  phase would silently carry the position to T+2 with nothing catching it — the
  #497 failure shape, on live money.
- New chain, all inside continuous trading: pre-entry refresh **15:02** → smoke
  check **15:03** → entry **15:05** → T+1 exit **15:10**
  (was 15:17 / 15:18 / 15:20 / 15:25).
- Times are now env-tunable (`SECTOR_FOLLOW_ENTRY_TIME`, `_EXIT_TIME`,
  `_SMOKE_CHECK_TIME`, `SECTOR_FOLLOW_PREENTRY_REFRESH_TIME`) but
  **hard-clamped to 15:10** by `resolve_schedule()`, which also enforces
  refresh < smoke < entry ≤ exit and reverts to defaults on violation. An
  operator override cannot push a MARKET order into the auction.
- `seed_strategy` now converges an already-seeded `strategies` row to the
  resolved times, so the dashboard stops reporting the stale 15:20/15:25.
- Tests: `test/test_sector_follow_cas_schedule.py` (22 cases — defaults, env
  overrides, malformed input, the clamp, ordering, registered job times).

⚠️ **The 15:05 entry is a DIFFERENT signal from the backtested 15:20 one** — a
different snapshot of the intraday move and of volume accumulation, and the
volume gate's denominator (20d avg *daily* volume) now includes auction volume
that the 15:05 cumulative volume cannot have captured, biasing the `vol > 1×`
gate toward firing less. **Every R40/R41 result predates this.** Treat sessions
from 2026-08-03 as unvalidated until a re-backtest on the new window lands.

Mode: **live** (since 2026-06-24) · Deployable: true.

## v0.1.1 — 2026-06-11
Order-failure journaling + phantom-position fix (no behavior change to gates/sizing).
- `place_entry`/`place_exit` now wrap placement in try/except so a thrown OR
  error-response order is journaled with a `status` (`placed`/`rejected`/
  `exception`/`scaffold`) + `error_message`, and one symbol's failure no longer
  aborts the rest of the entry/exit batch.
- A rejected/exception entry no longer creates a phantom `paper_book` position or
  `today_entries` row — nothing actually opened.
- Schema: additive `status` + `error_message` columns on `sector_follow_trades`
  (idempotent SQLite `ADD COLUMN` migration in `init_db`).
- Verified product = **CNC** (not MIS) → not subject to sandbox's 15:15 MIS
  square-off rejection; the 15:20 entry time is safe, no timing change needed.
Mode: scaffold-only · Deployable: false (unchanged).

## v0.1.0 — 2026-06-10
Initial scaffold from R40 V_SF_CAP5_VOL.
Mode: scaffold-only · Deployable: false
Operator decisions locked (see PLAN.md "Operator decisions").
Phase 0 starting next.
