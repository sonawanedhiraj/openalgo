<!-- migrated from outputs/2026-06-13_sector_rotation_yesterday_2026-06-12.md on 2026-06-13 | summary: Sector Strategy Verification â€” Yesterday (Fri 2026-06-12) -->

# Sector Strategy Verification â€” Yesterday (Fri 2026-06-12)

**Analysis date:** 2026-06-13 Â· **Scope:** read-only Â· **For:** Dheeraj

## Verdict

**sector_follow_cap5_vol â€” DID NOT TRADE; fail-safe held entries (working as designed).**
All scheduled jobs fired on time, but **all 30 universe stocks had a 2-business-day-stale
1m feed** (last bar = 2026-06-10), so the pre-entry data-freshness gate correctly aborted
entries. This is a **data gap, not a strategy bug** â€” the 8 sector indices were fresh.

**sector_rotation_etf â€” not run** (monthly, not scheduled; no new output files; next rebalance still planned 2026-06-15).

---

## sector_follow_cap5_vol â€” job-by-job

| Job | Fired 06-12? | Evidence (next_run = next weekday) |
| --- | --- | --- |
| daily_reset 09:00 | âœ… | next 2026-06-15 09:00 |
| entry 15:20 | âœ… | next 2026-06-15 15:20 |
| exit 15:25 | âœ… | next 2026-06-15 15:25 |
| eod_summary 15:30 | âœ… | EOD report generated 2026-06-12T15:30:00 |
| index_backfill 16:05 | âœ… | next 2026-06-13 16:05; all 8 indices fresh |
| data_health 16:30 | âœ… (also 11:00 run) | next 2026-06-15 16:30 |

- **Mode:** `sandbox` (strategy_daily_intent row, `updated_by=cli`, "pre-market sandbox 2026-06-12"; intent=run).
- **15:20 entry:** 0 signals fired / 0 positions opened. Cause: stock 1m feed stale â†’ freshness pre-entry gate aborted.
- **15:25 exit:** nothing to exit (no open positions).
- **15:30 EOD:** report written (Telegram summary path ran); â‚¹0 deployed, kill-switch inactive.
- **sector_follow_trades:** **empty for 06-12** â€” gate-aborted entries write no journal row (expected).
- **errors.jsonl:** 0 sector_follow exceptions on 06-12.

## Data health (the root cause)

`data_health_check` id=2 @ 2026-06-12 11:00, `overall_ok=0`, `alert_sent=1`:
- **8 sector indices:** all fresh (last_date 2026-06-12, staleness 0) â€” 16:05 backfill working.
- **All 30 universe stocks:** stale (last_date **2026-06-10**, staleness **2 business days** > threshold 1).

Cause matches the known pattern: **stock 1m backfill is not automated** (only the index 1m
has a 16:05 job), so the 30 stocks sat stale since 06-10. See memory
`sector-follow-data-freshness-gate`.

## âš  Gap to verify (not blocking)

`strategy_runtime_override` is **empty**. The data-health auto-pause is supposed to write a
self-expiring `pause` override for the next session on stale data â€” yet despite `alert_sent=1`,
no override row exists. Either the auto-pause path didn't fire (the only health row is the
11:00 run, not the 16:30 job) or it expired. **The pre-entry gate is the real protection and
it worked**, so entries were still held â€” but the auto-pause belt-and-suspenders should be
confirmed before relying on it. **Remediation: backfill the 30 stocks' 1m feed before 06-15 open.**

## Bottom line

Strategy infrastructure is healthy â€” every job fired, in sandbox, no exceptions. Zero trades is
the **correct fail-safe response to a stale stock feed**, not a malfunction. Fix the data
(automate/run stock 1m backfill) and confirm the auto-pause override actually writes.
