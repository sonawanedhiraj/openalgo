# Sector Follow (Cap-5, Volume-Tiebreaker) — Version Log

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
