# Per-account realized P&L on the strategy page — plan (v2.1, decisions locked)

Revised 2026-09-05. Status: PROPOSED, no issue yet. Rendered version with the
sample UI: https://claude.ai/code/artifact/ecff3ab9-a438-42dd-8f84-ea112f8461f5

## Objective

On `/strategies/<name>` (first: `open15_vol_breakout`) answer one question
for every account running the strategy: **is it in profit or loss, net of
charges?** One row per account (primary + each child), a strategy total and a
Profit / Loss verdict. No individual trades.

## Locked operator decisions (2026-09-05)

- **Pre-ship child history is IMPORTED** from each child's Zerodha Console
  tradebook export (F&O segment, 2026-08-26 onward). See "Import input" below.
- **Default verdict window = All** (since the strategy went live 2026-07-24).
- **Verdict = sign of the total.** Profit above ₹0, Loss below ₹0, Flat only
  at exactly ₹0 (no closed trades). No tolerance band.

## Broker API check (Kite Connect v3, verified 2026-09-05)

| Endpoint | Gives | Scope | Verdict |
| --- | --- | --- | --- |
| `GET /trades` | per fill `order_id`, `quantity`, `average_price`, `fill_timestamp` — joins to `account_orders.broker_orderid` | today only | **primary source** |
| `POST /charges/orders` | broker's own brokerage/STT/exchange/SEBI/stamp/GST per order; accepts historical/hypothetical orders | any order | **broker-true charges** (not wired today) |
| `GET /portfolio/positions` | per-position `realised`/`unrealised`, whole account incl. manual trades | today only | cross-check only |
| historical P&L / ledger / Console | — | — | **not available** via Kite Connect; Console exports a tradebook file by hand (≤1 year per download) |

The broker answers for TODAY only. Every child figure on the card is a number
OpenAlgo wrote down the same day, or imported once from the Console file.

## What the page has / lacks

- Primary's answer exists: Performance Comparison → Live → Net P&L
  (`_open15_lifetime()` over `open15_trades`, real fills, `net_pnl_of_row`).
  The new card MUST reuse that figure.
- Children have no answer anywhere: `account_orders` records attempts only
  (no fill price, no charges, nothing per day). 88 placed child rows since
  2026-08-26 (all NFO MIS options; 5–6 trading days per child) cannot be
  priced from the broker API — hence the Console import.
- The P&L Curve card is EMPTY for open15 (`pnl_curve()` has no open15
  branch). The daily series the new card needs can fill it, one line per
  account.

## Design

### Data (prerequisite)

`account_orders` += `fill_price` (volume-weighted across partials, #641),
`fill_qty` (read by presence, #626), `fill_at`, `charges_inr`,
`charges_source ∈ {broker, modelled}`.

New `account_daily_pnl`, UNIQUE `(account_id, trade_date, strategy_name)`:
`realized_gross`, `charges_inr`, `charges_source`, `realized_net`,
`n_round_trips`, `book_realised` (whole-account cross-check, never merged),
`capture_source ∈ {tradebook, positions_only, console_csv}`, `finalized`,
`captured_at`. Additive boot migrations (`_migrate_add_sizing_price` style).

`services/account_pnl_service.py`: RAW `get_trade_book(token)` with the
CHILD's `acct:<id>` token → group by `order_id`, volume-weight partials, write
fills onto matching `placed` rows only → FIFO pairing per (symbol, exchange,
product) within the strategy's rows → charges via `POST /charges/orders`
(`broker`) with `option_round_trip_charges`/`mis_round_trip_charges` fallback
(`modelled`) → positions cross-check → idempotent upsert. Runs inside the
existing `multi_account_fill_reconcile` (09:40) and `multi_account_eod_summary`
(15:35, sets `finalized`) jobs + boot catch-up for today. No new thread or job
id (else catalog it in `scheduler_registry.CATALOG`, same commit).

### Console tradebook import (Phase 4, decided)

`services/account_console_import.py` — operator CLI, dry-run default:

```
uv run python -m services.account_console_import --account <id> --file <csv|xlsx> [--apply]
```

Reads `order_id`, `trade_id`, `trade_date`, `quantity`, `price`, side
(`trade_type`) from the Console export; extra columns ignored. Keeps ONLY rows
whose `order_id` matches an `account_orders.broker_orderid` for that account,
so the family member's own trades never count. Volume-weights partials into
`fill_price`/`fill_qty`, pairs FIFO into per-day realized, prices charges via
`POST /charges/orders` if the child is logged in at import time (else
modelled, labelled), writes `account_daily_pnl` rows with
`capture_source='console_csv'`, `finalized=1`. Prints unmatched order ids;
never guesses. Idempotent. Files live under a gitignored `imports/console/`.

**Import input required from the operator (per child, once):**
1. Log in to console.zerodha.com as the child (holder's login + TOTP).
2. Reports → Tradebook → Segment F&O → 2026-08-26 to the day before import →
   Download CSV/XLSX.
3. Save as `imports/console/<display_name>_tradebook_<from>_<to>.csv`.
4. Run the CLI dry-run, then `--apply`. Repeat for all three children.
No P&L figures, capital or dates are typed by hand; the file is the only input.

### Strategy-level aggregation

`services/strategy_accounts_pnl.py` → payload for any strategy:

- Primary row from the strategy's own journal via the SAME helper the
  Performance table uses (`_open15_lifetime()['live']`).
- Child rows from `account_daily_pnl` by `strategy_name`, one per child that
  ever selected the strategy in `account_strategies`.
- Per row per window (1d/1w/1m/all; default all): `net_inr`, `days_traded`,
  `win_days_pct`, `max_dd_inr` (on daily cumulative), `capital_basis_inr`
  (child `capital_per_trade_inr × max_trades`; primary slot budget),
  `return_pct` flagged notional, `charges_source`, `capture ∈ {journal, final,
  provisional, missing, console_csv}`, `days_missing`, `daily[]`.
- Total row = sum of account nets. Verdict = sign (`profit`/`loss`/`flat`).
  A total excluding uncaptured days says so.

### API

```
GET /strategies/api/<name>/accounts-pnl?window=1d|1w|1m|all   (default all)
→ { window, since, verdict, total: {net_inr, days_traded, days_missing},
    accounts: [ {account_id|null, name, role: primary|child, net_inr,
                 days_traded, win_days_pct, max_dd_inr, capital_basis_inr,
                 return_pct, charges_source, capture, days_missing, daily} ] }
GET /strategies/api/<name>/pnl-curve   — add the open15 branch; one series per account
```

### Rules (load-bearing)

1. The primary figure IS the Performance table's figure (same helper, same
   real-fill filter, same `net_pnl_of_row`). One definition (#552).
2. Child realized comes from the child's fills (live tradebook or Console
   export), keyed by `order_id` — never `sizing_price`, never the parent's fill
   scaled, never the primary's API key (#497/#637).
3. Missing is a red chip + a count, never ₹0; the total says what it excludes.
4. Whole-account P&L never enters the table (manual trades); cross-check only.
5. Charges labelled per row (broker vs modelled).
6. Read-only on the broker; fail-open per account.

### UI (`/strategies/open15_vol_breakout`)

New card "Account P&L — is it making money?" under the Performance /
P&L-curve grid, above Trades & LLM Decisions. Header: window segment
1D/1W/1M/All (All selected) + Refresh. Verdict bar: PROFIT/LOSS pill, big total
net, subline (accounts, since-date, exclusions), Primary / Children / Days
traded. Table: Account · Net P&L · Today · Days · Win days · Max DD · On
capital · Last-10-days bar strip · Status chip; Strategy total row. Also: open15
P&L-curve branch (one line per account) and the strategy total on the
`/strategies` list tile.

## Phases

1. Prerequisite: capture + persist child fills, job hooks (~1 day). Ship
   first — each day without it is lost.
2. `accounts-pnl` endpoint + card + curve branch + list-tile total; commit
   dist (~1 day).
3. Broker charges module with labelled fallback (~0.5 day).
4. Console tradebook import CLI + postmarket expectation "every enabled child
   with placed rows has a finalized day row" (~0.5 day; needs the three
   files above).

## Validation (post to the issue before close)

- From `/strategies` → Open15 Vol Breakout: card shows Primary + 3 children +
  verdict; screenshot.
- Primary row net == Performance table Live Net P&L to the rupee.
- Imported child days: per-child total matches the child's Console P&L
  report for the same range within charges tolerance.
- Live day: one child's final net matches its Console P&L for the date.
- Window switch changes every row and the verdict consistently; default is All.
- Child disconnected at both capture points → "not captured"; total excludes
  it and says so.
- Restart after 15:35: history unchanged, today still final.
- `uv run pytest test/test_account_pnl_service.py
  test/test_account_console_import.py test/test_strategy_accounts_pnl.py
  test/test_scheduler_registry.py` green.

Urgent regardless: start capturing every child's tradebook from the next
trading day.
