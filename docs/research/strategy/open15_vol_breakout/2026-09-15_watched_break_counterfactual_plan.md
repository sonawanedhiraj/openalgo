# open15 — "what if we had entered at the 09:15 break?" for the watch-list names that never triggered

**Date:** 2026-09-15 · **Status:** plan, not built · **Owner:** operator + Claude Code
**Scope requested by the operator:** *"stocks did not qualify for entry but I would need what if entry
was taken just based on 9.15 candle high or low break. Just the numbers so that enough data is
collected to find patterns."*

## 1. What is being measured

Every symbol on the day's watch list (09:16 seed picks + rolling adds) that ends the entry window
**without a trigger** currently leaves one line in the log: `no_entry · level_broken · max vol 1.21x <
1.5x`. Nothing says what the trade would have done. This adds ONE counterfactual per such symbol:

> Enter 1 lot of the ATM option the moment price first breaks the 09:15 candle high (long) / low
> (short) — **no volume gate** — and exit at the scheduled exit time. Report gross, charges, net,
> and the worst/best mark over the hold.

Today (2026-09-15) that is 10 of 13 watched names (12 no-trigger, of which 10 broke the level and 2
never did). On 2026-09-11 it is 3 of 7. At ~10 rows/day the sample reaches 200 rows in a month,
which is what the operator asked for: numbers first, patterns later.

**Names that never broke the level are NOT priced.** The rule is "entry at the break"; with no break
there is no entry, and inventing one (09:16 open, cutoff price) would be a different strategy blended
into the same column. They render `no break` and count in the digest as `watched_nobreak`.

**No decision rule is pre-registered yet** — the operator asked for data collection. The obvious
eventual question is *does the volume gate earn its keep?* (compare the triggered cohorts' per-lot
net against this cohort's over the same days); write that rule down when the sample is ~100 rows,
not now, so it is not fitted to the first fortnight.

## 2. The convention (locked — code constants, not knobs)

| item | rule | why this and not something else |
| --- | --- | --- |
| entry moment | first tick with price > 09:15 high (L) / < 09:15 low (S), inside the entry window (09:16 → `no_entry_after`) | exactly the level half of the real gate; `on_tick` already stops updating watch stats past the cutoff, so the window bound is inherited |
| contract | `resolve_atm_option(symbol, side, break_price, date)` — ATM at the break, #669 expiry roll applies | the same resolver a real trigger at that price would have used |
| entry premium | OPEN of the 1m bar AFTER the break minute; fallback break-minute close | the #435 option-shadow convention: a market order at second :xx fills on the next prints |
| exit premium | OPEN of the day's `exit_time` bar (09:30 today); fallback last bar at/before it | same as #435; `_EXIT_MINUTE` becomes a parameter instead of a constant |
| size | **1 lot** | the `sim` convention (#555): "would this have paid?" at the minimum tradeable unit, comparable across lot sizes that differ 30× |
| net | gross − `option_round_trip_charges` for 1 lot | the one charges model every option row already uses |
| MAE / MFE | min / max of bar lows / highs between entry and exit, vs the entry premium, 1 lot | free from the same bars; reuses the existing `cf_mae` / `cf_mfe` columns (#704) |
| price source | broker 1m bars, stamped `cf_source='bars'` | bars carry no bid/ask, so this net is optimistic by ~the round-trip spread — label it, never compare it unqualified against quote- or fill-priced rows |
| instrument | follows the day's `instrument` config (`atm_option` today; in `stock` mode price the stock at the break from NSE bars, MIS charges) | the counterfactual must be the strategy AS CONFIGURED minus one gate |

## 3. Where the numbers live

**Tick thread (free, no broker call — the #626 rule):** `Open15Core.on_tick` records
`first_break_ts` and `first_break_price` in `watch_stats[symbol]` the first time `beyond` is true.
The `no_entry` and `watch_stats` events carry them (`first_break_at`, `first_break_price`), so the
page can say *"broke 09:19:42 @ 1616.40"* the same day even before the option is priced.

**Journal (`open15_trades`) — one row per priced or price-attempted name:**
`fill='watched'`, `status='skipped'`, `reason='no_trigger'`, `sim_quantity=lot`, `quantity=0`,
`trigger_price=NULL` (there was no trigger — the break is a different thing and gets its own
columns). New columns: **`break_at`** (HH:MM:SS), **`break_price`**. Everything else REUSES the
#435 option-shadow columns, whose semantics are identical (1-lot ATM, bars, next-minute open →
exit open): `opt_symbol`, `opt_lot_size`, `opt_entry_premium`, `opt_exit_premium`,
`opt_charges_inr`, `opt_pnl` (NET, 1 lot), plus `cf_mae`, `cf_mfe`, `cf_source` from #704.
**`pnl` and `charges_inr` stay NULL** so no existing sum can ever pick the row up, and `'watched'`
is added to `NON_REAL_FILLS` as the second lock (the explicit-list rule from #555). One derived
reader, `watched_net_of_row(row) -> row.opt_pnl`, in `open15_breakout_db` (the #552 shape: one
place, never `sum(opt_pnl)` inline).

**Day log — one new event, `watched_counterfactual`**, per row, emitted by the pricing pass:
`symbol, side, watch_source, break_at, break_price, contract, lot_size, entry_premium,
exit_premium, gross, charges, pnl, mae, mfe, source, status ∈ {priced, no_contract,
bars_pending}`. Both row builders learn it in the same commit — the JS `renderSel` in
`blueprints/open15_breakout.py` AND `open15_log_view.selection_outcomes` — with a test on each
(the page has gone dark twice on an event nobody taught it, #615/#622).

**Summary / digest:** `summary` event and `summarize_day` gain `watched`, `watched_priced`,
`watched_nobreak`, `watched_pnl` (sum of 1-lot nets, labelled). `open15_breakout_db.watched_pnl_by_date()`
mirrors `sim_pnl_by_date()`.

**CSV export** (`CSV_COLUMNS`): `break_at, break_price, wcf_contract, wcf_entry, wcf_exit,
wcf_net, wcf_mae, wcf_mfe, wcf_status`. This is the pattern-finding surface the operator asked for.

## 4. When it runs

`enrich_watched(trade_date)` in `services/open15_option_shadow.py` — sibling of `enrich_missing`:

1. **Summary job (exit + 5 min).** For every `no_entry` row with a break: resolve the contract,
   fetch its 1m bars, price, insert the journal row, emit the event. Zerodha's same-day 1m history
   lags 5–15 min, so some rows land as `bars_pending`.
2. **Next 09:10 arm** via the existing `_opt_shadow_catchup` seam — prices whatever was pending.
   Idempotent: a row whose `opt_pnl` is set is skipped.
3. Never on the tick thread; ~10 bar fetches/day at the broker's 3 req/s ≈ 4 s, off-thread.

**Backfill CLI** `uv run python -m services.open15_watched_backfill --from --to [--apply]`
(dry-run default, not wired into the runtime — the #704 shape). Past days have `level_broken` but
not the break TIME, so the CLI rebuilds it from the tick capture (`tick_logs/open15/ticks-YYYYMMDD-*.jsonl`,
whole-universe since #528): the 09:15 candle from the 09:15-minute ticks, then the first beyond-tick.
**Honest limit:** Kite drops an instrument from the master contract at expiry, so a contract can be
priced only while it is alive — the September rows (expiry 29 Sep) are recoverable now, August is
gone. Ship before 29 Sep to keep the September sample.

## 5. UI on `/open15_vol_breakout/logs` — numbers only, no new cards

- **Outcome table**, no-trigger rows: `entry` = break time + stock price with a `break` tag;
  `exit` = exit premium; `qty` = `1 lot (700)`; `net P&L` = the 1-lot net in the watched colour with
  a `watched` badge (never green/red money colours — it is a counterfactual); `outcome` keeps the
  gate reason and adds the contract + premiums. Never-broke rows read `no break`; unpriced rows read
  `bars pending` / `no contract`.
- **Detail drawer** gains a `WATCHED COUNTERFACTUAL` box: break, contract, entry/exit premium,
  gross/charges/net, MAE/MFE, source, and the caveat line.
- **Day digest chips** gain one chip: `watched · 10 priced / 2 no break · Σ 1-lot net`.
- **Config form**: one checkbox `price the untriggered watch list after exit` (`watched_cf_enabled`,
  env seed `OPEN15_WATCHED_CF`, default ON) — a measurement knob like `OPEN15_SIM_SKIPPED_ENABLED`,
  recorded in the `armed` event.
- **Sidebar day list unchanged** — it is money only.
- **Colour**: a new `.b-watched` badge (slate-blue, the `#1b2b3a` / `#89b4fa` family) so it can
  never be read as sim (mauve), shadow (teal) or paper (amber).

Sample: see the published mockup (illustrative premiums on the real 2026-09-15 watch list).

## 6. Rules carried (each load-bearing)

1. **Never money.** `pnl` NULL + `NON_REAL_FILLS` + never registered in `self.positions` (so
   `flatten`, the risk monitor and `_count_fills` cannot see it). `total_realized_pnl()` is asserted
   unchanged by a test that inserts watched rows.
2. **Never a broker call on the tick thread** — the break is recorded from the tick already in hand;
   pricing is a scheduler-thread job.
3. **One convention, labelled** — bars-priced, 1 lot, next-minute open. `cf_source='bars'` on
   every row; the page prints "bars · 1 lot" beside every number.
4. **No new event goes in without both row builders and a test** (#615/#622).
5. **Not a strategy change.** Nothing gates on it; `vol_mult`, `max_trades`, sizing and every real
   path are byte-identical. The only new scheduler work is the pricing pass.

## 7. Files, tests, effort

| file | change |
| --- | --- |
| `services/open15_breakout_service.py` | `on_tick` break capture; `no_entry`/`watch_stats`/`summary` fields; call `enrich_watched` at summary + arm catch-up; config plumbing |
| `services/open15_option_shadow.py` | `enrich_watched`, `premiums_from_bars(exit_minute=...)`, MAE/MFE from bars |
| `services/open15_watched_backfill.py` | new CLI (dry-run default) |
| `database/open15_breakout_db.py` | `break_at`, `break_price` columns; `NON_REAL_FILLS`; `watched_net_of_row`; `watched_pnl_by_date`; `CSV_COLUMNS` |
| `services/open15_log_view.py` | `watched_counterfactual` in `selection_outcomes` + `summarize_day` |
| `blueprints/open15_breakout.py` | JS row builder, drawer box, chip, config checkbox, `/api/config` field |
| `strategies/open15_vol_breakout/SPEC.md`, `docs/PARAMETER_LOG.md` | §4 gains the fifth non-money class; the env seed |

Tests: `test/test_open15_watched_counterfactual.py` — break capture in `Open15Core` (first beyond-tick
only, window-bounded); bars pricer conventions incl. fallbacks and MAE/MFE; journal row shape
(`pnl` NULL, fill `watched`); `total_realized_pnl` unchanged; digest counts; both row builders
render the event; catch-up idempotent; CLI dry-run writes nothing. Effort ≈ 1 day; one PR.

## 8. Validation (goes on the issue before close)

1. From `/strategies` → open15 → `logs`, today's row for a level-broken no-trigger name shows the
   break time in `entry`, a `watched`-badged 1-lot net, and the drawer box; a never-broke name reads
   `no break`. Screenshot.
2. CSV export carries the new columns for the same day.
3. Day chip shows `watched · n priced / m no break · Σ`.
4. `total_realized_pnl()` and the strategies dashboard Live column are identical before/after the
   rows exist (SQL read-only check).
5. Backfill CLI dry-run on 2026-09-11 and 2026-09-15 lists the expected 3 + 10 rows; `--apply`
   populates them; re-run is a no-op.
