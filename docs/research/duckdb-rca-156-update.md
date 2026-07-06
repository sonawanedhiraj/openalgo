# Re-analysis 2026-06-26 PM — broader symptom set discovered, plan expanded

A second pass through today's logs (and the trade_journal + sandbox DBs) surfaced **four additional problem classes** that the original DuckDB analysis didn't cover. They are listed below with hard evidence and folded into a single consolidated remediation plan at the end.

---

## Full symptom inventory after today's restarts

Today (2026-06-26) saw **seven OpenAlgo restarts** (08:53, 09:11, 10:50, 11:00, 11:23, 12:00, 15:05 IST). Aggregate signal:

| # | Class | Count today | Severity | In #156 already? |
|---|---|---|---|---|
| A1 | `ConnectionException: Can't open ... different configuration` (historify) | ~50 | ERROR | yes |
| A2 | `Failed to connect to DuckDB after 3 attempts` | 49 | ERROR | yes |
| A3 | `IO Error: Cannot open file ... being used by another process` | ~10 | ERROR | yes |
| B1 | `pandas_ta_classic verify_series: Series has N rows but indicator requires ≥M` | **25272** | WARNING | yes |
| **C1** | **Orphan trades in `trade_journal`** (exit_reason set, exit_price NULL) | **7 rows** persisting | DATA INTEGRITY | **NO — NEW** |
| **C2** | **Zero trades today** (sandbox.db empty, trade_journal empty for date 2026-06-26) | 0 trades | SILENT FAILURE | **NO — NEW** |
| **D1** | **Scanner WS stale `~119s since last tick — soft recovery`** | 121 | WARNING | **NO — NEW** |
| **D2** | **Scan rule rejects: `bars_daily is None (no daily-D data)`** for indices (NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY/NIFTYNXT50) | 235 × 2 rules | WARNING | **NO — NEW** |
| **D3** | **Scanner backfill: `no api key available` for RELIANCE+SBIN** repeatedly | ~13 | WARNING | partial (auth flow) |
| **D4** | **scanner_dry tripwire CRIT for historical dates** (e.g. `as_of=2026-06-22T11:00:00`, `scanner_subscribed_at=None`) | ~10 | ERROR | partial (PR #147 covers live but a historical backfill of the tripwire is firing) |
| E1 | `backtest_db migration ALTER backtest_trades.scanner_hit_timestamp failed: no such table` | 6 | WARNING | minor — schema migration order bug |
| E2 | Multiple Zerodha funds 401 "Incorrect api_key" + WS 403 handshake | 9 + 6 | ERROR | pre-login class (PR #145 freshness gate addresses, default OFF) |

---

## C1 — orphan trades in `trade_journal` (the "trades not logged" symptom)

### Evidence

```
sqlite> SELECT id, placed_at, symbol, direction, strategy_name, entry_price,
                exit_price, exit_reason, exited_at
        FROM trade_journal WHERE exit_price IS NULL AND exit_reason IS NOT NULL
        ORDER BY placed_at DESC;

(101, '2026-06-24T15:03:55', 'OBEROIRLTY', 'LONG',  ..., 1756.6, NULL, 'eod_watchdog', '2026-06-24T15:14:00')
( 97, '2026-06-19T14:08:53', 'AUROPHARMA', 'LONG',  ..., 1470.0, NULL, 'eod_watchdog', '2026-06-19T15:14:01')
( 94, '2026-06-19T12:08:41', 'TECHM',      'SHORT', ..., 1376.7, NULL, 'eod_watchdog', '2026-06-19T15:14:01')
( 93, '2026-06-19T12:08:35', 'PERSISTENT', 'SHORT', ..., 4709.5, NULL, 'eod_watchdog', '2026-06-19T15:14:01')
( 92, '2026-06-19T11:49:50', 'TCS',        'SHORT', ..., 2070.5, NULL, 'eod_watchdog', '2026-06-19T15:14:02')
( 91, '2026-06-19T11:43:54', 'LTM',        'SHORT', ..., 3772.9, NULL, 'eod_watchdog', '2026-06-19T15:14:02')
( 80, '2026-06-12T14:14:58', 'ASHOKLEY',   'LONG',  ..., 149.87, NULL, 'eod_watchdog', '2026-06-12T15:14:00')
```

Seven trades over 12 days where `exit_reason='eod_watchdog'` is set, `exited_at` is set, but **`exit_price` is NULL** — meaning the EOD watchdog tried to place the exit order but the order never confirmed/filled. The journal row was half-updated.

### Today's specific symptom

```
[2026-06-26 10:50:26] ERROR simplified_stock_engine_service:
  [SIMPLIFIED-ENGINE] No api_key resolvable for TCS exit — order skipped (throttled; ...)
```

The engine is **still trying to exit TCS** — 7 days after the 2026-06-19 entry. Something in the engine's in-memory state was populated for TCS at boot, but `rehydrate_positions_from_journal` reports `positions_restored=0` (its query uses `get_open_trades_today(strategy_name)` which filters by `placed_at=today`).

### Cause

`record_exit` in `services/trade_journal_service.py` sets `exit_reason` and `exited_at` BEFORE the order is confirmed filled. If the broker rejects/silently-drops the order, `exit_price` stays NULL but the row looks closed to `get_open_trades_today` (its filter is `exit_price IS NULL`, so actually it would still be considered open — yet rehydrate reports 0).

There are two separate sub-bugs:
- **C1a**: `record_exit` updates the row optimistically before fill confirmation.
- **C1b**: `get_open_trades_today` returns 0 for these rows even though they have `exit_price IS NULL`. Possibly the date filter excludes old entries — but the engine should still be able to find+close them. Need to inspect `get_open_trades_today`.

### Fix

1. Two-phase exit update: only set `exit_reason` + `exited_at` AFTER `record_exit_fill(exit_price)` confirms.
2. A new `reconcile_orphan_exits` job at boot: for any journal row with `exit_price IS NULL AND exit_reason IS NOT NULL AND age > 1 day`, mark it `exit_reason='ABANDONED_<original>'` so the engine stops attempting the exit on every restart. Telegram-alert the operator with the list.
3. A migration/cleanup script to fix the 7 existing orphan rows today.

---

## C2 — zero trades today despite engine running

### Evidence

```
sqlite> SELECT COUNT(*) FROM trade_journal WHERE date(placed_at)='2026-06-26';
0
sqlite> SELECT COUNT(*) FROM sandbox.sandbox_orders WHERE date(order_timestamp)='2026-06-26';
0
```

The simplified engine, sector_follow, and scanner all initialised correctly. No entries today.

### Likely causes (need confirmation)

- Scanner produced no hits (combined with D2 — indices reject because no daily-D data — and B1 — indicators return None for 100 min after restart, this is the dominant culprit)
- OR: scanner produced hits but the webhook posting failed silently
- OR: signals were generated but the freshness gate / runtime override held them

Confirmation work needed in the implementation plan: read `scan_results` table for today, cross-reference with engine signal-received logs.

---

## D1 — scanner WS stale every 2 minutes (121 warnings)

### Evidence

```
[2026-06-26 11:25:20] WARNING scanner_ws_watchdog:
  Scanner WS stale: 119s since last tick — soft recovery (ws.close → reconnect)
[2026-06-26 11:27:20] WARNING scanner_ws_watchdog:
  Scanner WS stale: 118s since last tick — soft recovery (ws.close → reconnect)
... (pattern repeats every 2 min)
```

The scanner WS watchdog (`services/scanner_ws_watchdog.py`) declares a feed unhealthy after 90s without a tick and does a `close → reconnect`. 121 such events in one day means the broker tick stream is dropping every ~120s.

### Cause

Broker WS instability is upstream — but the **soft recovery itself may be masking the underlying issue** (a Zerodha re-login mid-day, a network blip, ping/pong timeout). The watchdog kicks in too aggressively and reconnects, but reconnects DON'T re-subscribe symbols (the existing subscription state isn't replayed by the recovery path).

### Fix

- Audit soft-recovery path: does it call `register_connect_callback` after reconnect so `scanner_pre_subscribe` re-subscribes? If not, every soft-recovery leaves the scanner with fewer symbols.
- Distinguish "broker dropped ticks" vs "network jitter". Add a 5-min rolling tick-rate metric instead of the 90s hard cutoff. Soft-recover only if the rate drops to 0 for 5+ minutes consecutively.

---

## D2 — scan rules reject indices: `bars_daily is None`

### Evidence

```
[2026-06-26 11:25:22] WARNING fno_intraday_buy_chartink:
  fno_intraday_buy_chartink NIFTY: rejecting — bars_daily is None (no daily-D data)
[2026-06-26 11:25:22] WARNING fno_intraday_sell_chartink:
  fno_intraday_sell_chartink NIFTY: rejecting — bars_daily is None (no daily-D data)
```

For NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50 — both buy and sell rules reject with `bars_daily is None`. 235 warnings per rule.

### Cause

These are **indices** (`NSE_INDEX` exchange). The scanner_universe_backfill keeps daily-D bars only for stocks. The 5 indices are subscribed for live ticks but their daily-D historical is NOT fetched. When the rule needs `bars_daily` to compute a daily gate (gap, ATR-on-daily), it gets None.

### Fix

Two options:
- Extend `scanner_universe_backfill` to also fetch daily-D for the 5 index symbols (small additional load, currently absent).
- OR have the scan rule degrade gracefully for indices (e.g., compute the daily gate from intraday-aggregated bars).

The former is the cleaner architectural fix.

---

## D3 — scanner backfill fails for RELIANCE + SBIN

### Evidence

```
[2026-06-26 09:47:04] WARNING scanner_universe_backfill:
  scanner universe 1m catch-up FAILED for 2 symbol(s) — no api key available — symbols=['RELIANCE', 'SBIN']
```

Repeated 13 times today. Other symbols backfill fine in the same calls.

### Cause

Need investigation — likely the api-key resolution is symbol-keyed somewhere and these two have stale/null entries. Or a partial token-rotation race.

### Fix

Investigate `services/scanner_universe_backfill.py` api-key resolution; should fall back to the global first-available-api-key when symbol-specific lookup fails.

---

## D4 — scanner_dry tripwire CRIT for historical dates

### Evidence

```
[2026-06-26 10:46:27] ERROR scanner_dry_tripwire_service:
  scanner_dry tripwire CRIT: {'as_of': '2026-06-22T11:00:00+05:30',
                              'last_inhouse_at': '2026-06-22T10:25:00+05:30',
                              'scanner_subscribed_at': None,
                              'subscribe_warmup_min': 5,
                              'gap_min': 35.0, ...}
```

Multiple CRIT entries with `as_of` set to dates 2026-06-22 / 2026-06-23 — not "now". Notice `scanner_subscribed_at: None` — the subscribe hook (PR #147) didn't fire because this is a synthetic replay, not a live invocation.

### Cause

Something (a one-shot backfill / replay job) is calling `check_dry_scanner(as_of=<historical>)` and the tripwire's notification path doesn't distinguish "live alert" from "historical evaluation." The Telegram CRIT fires for both.

### Fix

- `check_dry_scanner` should skip the notifier path when `as_of < now - 1 hour` (treat as analysis-mode, return result without alerting).
- OR add a separate `evaluate_dry_scanner_silent` for historical/replay use that returns the verdict without firing the notifier.

---

## E1 — backtest_db migration: `no such table: backtest_trades`

### Evidence

```
[2026-06-26 11:00:31] WARNING backtest_db:
  backtest_db: migration ALTER backtest_trades.scanner_hit_timestamp failed:
  (sqlite3.OperationalError) no such table: backtest_trades
```

The migration tries to `ALTER TABLE backtest_trades ADD COLUMN scanner_hit_timestamp` but the table doesn't exist. Init order bug — the table should be created before the migration runs.

### Fix

Reorder `backtest_db.init_db()` to call `Base.metadata.create_all()` BEFORE the ALTER migrations. Trivial.

---

## Consolidated remediation plan

Five PRs, in this order. Sized to land each independently:

### Phase 1 — DuckDB singleton (the root-cause fix already in #156)

- Module-level shared writeable `_shared_conn` in `database/historify_db.py`
- `get_connection()` returns a cursor; `connect_historify_readonly()` becomes an alias
- Removes 100% of in-process config-mismatch errors AND unblocks Phase 2
- Tests: 50 reader threads + 10 writer threads, zero exceptions
- **Closes A1, A2, A3** (problem class A from #156)

### Phase 2 — Scanner aggregator seeding from historify

- At boot, after broker session live, seed `MultiIntervalAggregator` for every scanner symbol from last ~100 5m bars in historify
- Indicators have history immediately — no more 100-min warmup window
- **Closes B1** (25272 → 0 warnings per restart)

### Phase 3 — trade_journal exit hardening + orphan reconciliation

- Two-phase exit: `record_exit_pending` (sets reason+at, NO price) → `record_exit_fill` (sets price + confirms). Fail-loud if no fill within 30s.
- New boot job `reconcile_orphan_exits`: for any row with `exit_price IS NULL AND age > 1 day`, mark `exit_reason='abandoned_<original>'` and Telegram-alert
- One-time SQL migration to clean up the 7 existing orphans (id=80, 91, 92, 93, 94, 97, 101)
- **Closes C1** + stops the recurring "No api_key resolvable for TCS exit" log spam

### Phase 4 — Scanner reliability bundle

- D2: extend `scanner_universe_backfill` to fetch daily-D for the 5 indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50)
- D3: scanner_universe_backfill api-key resolution falls back to `get_first_available_api_key()` when symbol lookup fails
- D4: `check_dry_scanner(as_of=<historical>)` skips notifier; new `evaluate_dry_scanner_silent` for replay
- D1: scanner_ws_watchdog audit — verify reconnect re-subscribes, switch from 90s hard cutoff to 5-min rolling rate
- **Closes D1, D2, D3, D4**

### Phase 5 — Diagnostic instrumentation for C2 (zero trades today)

- This is the most important diagnostic: instrument the signal → engine → order path so the next "zero trades" day produces an audit trail
- Read today's `scan_results` for source='inhouse' and cross-reference with engine signal-received logs
- Add a Telegram daily 15:35 IST summary: `scanner_hits=N, signals_processed=M, orders_placed=K, orders_filled=J, journal_entries=I`
- If K < M, something is dropping signals between scanner and order placement
- **Diagnoses C2** (zero-trades root cause)

### Phase 6 — E1 (trivial, can go anywhere)

- Reorder `backtest_db.init_db()` so `create_all` runs before ALTER migrations
- Single-line fix

---

## Updated acceptance bar

After Phases 1-5 land + a restart, measured over 24h:

| Metric | Today | Target |
|---|---|---|
| historify DuckDB errors per day | ~100 | **0** |
| pandas_ta `verify_series` warnings per restart | 25272 | **0** |
| Orphan trades in `trade_journal` (exit_price NULL, age > 1 day) | 7 | **0** (cleaned + prevented) |
| `[SIMPLIFIED-ENGINE] No api_key resolvable for ... exit` recurring errors | 1 / restart for TCS | **0** |
| `Scanner WS stale ... soft recovery` warnings per day | 121 | **<20** (only real outages) |
| `bars_daily is None` warnings per day | 470 | **0** |
| `no api key available for [RELIANCE, SBIN]` warnings per day | ~13 | **0** |
| Historical-date scanner_dry CRITs | ~10 | **0** (silent for replay) |
| Daily Telegram summary: `scanner_hits / signals / orders / journal` | absent | **present** (catches future C2-class) |

---

## Issue/PR plan

This re-analysis supersedes the simpler plan in the original #156 body. Each phase becomes its own PR:

1. **PR for #156 Phase 1** — DuckDB singleton (the architectural fix)
2. **PR for #156 Phase 2** — Aggregator seeding
3. **NEW issue #157** — trade_journal exit hardening + orphan reconciliation (C1)
4. **NEW issue #158** — Scanner reliability bundle (D1–D4)
5. **NEW issue #159** — Zero-trades diagnostic instrumentation (C2)
6. **NEW issue #160** — backtest_db migration order (E1)

Existing in-flight #155 (job_id propagation) is kept as defence-in-depth — DuckDB singleton makes its errors disappear regardless, but the propagation is correct hygiene.
