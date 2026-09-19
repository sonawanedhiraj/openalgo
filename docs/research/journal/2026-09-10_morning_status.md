# Morning status — 2026-09-10 (Thursday)

**Headline, Dheeraj:** The platform is healthy and self-healed everything it hit this
morning, but open15 went 0-for-3 live (≈ **−₹7,524 net**) with all three exits fired by the
**₹2,500 per-trade stop** — and on VBL the stop tripped **3 seconds after entry, on the
bid-ask spread alone**.

> ⚠️ **This report ran late.** It is scheduled for 08:00 IST but executed after 09:40 IST,
> so the open15 window (09:10–09:35) had already completed. Treat it as a post-open
> review, not a pre-market brief.
>
> ⚠️ **The Linux sandbox is DOWN** (`failed to mount … Plan9 share "c" is not mounted`,
> 5 attempts). No `git`, no Python, no `sqlite3`. Everything below was reconstructed from
> `log/openalgo_2026-09-10.log`, `log/errors.jsonl` and raw `.git` refs via file reads.
> **The other two scheduled tasks running right now hit the same wall** — "Fno scan cycle"
> reported *"Bash environment is unavailable"*. This is a Cowork-side outage, not an
> OpenAlgo one.

---

## 🔴 Stuck / Action Required

### 1. The per-trade stop is firing on the spread, not on the move — VBL stopped in 3 seconds

Today's effective risk config (from the 09:10 `armed` event): `stop_loss_inr 2500`,
`profit_target_inr 5500`, `trail_giveback_inr 2000`, `trail_confirm_polls 2`,
`margin_per_slot ₹60,000`, `max_trades 3`, mode **live**, `config_source: ui`.

| Symbol | Entry | Exit | Held | Premium paid | Bid at entry | Instant bid-marked MTM |
|---|---|---|---|---|---|---|
| VBL | 09:19:59 | 09:20:02 | **3 s** | 13.15 | 12.55 | **−₹2,295** vs a ₹2,500 stop |
| CANBK | 09:21:58 | 09:23:05 | 67 s | 2.77 | 2.74 | −₹607 |
| MCX | 09:22:46 | 09:24:27 | 101 s | 98.05 | 98.05 | ₹0 |

VBL was bought into a ~4.8% spread (ask 12.75 / bid 12.55 on 3,825 qty). Per #716 the rule
marks long premium at the **bid**, so the position opened already −₹2,295 underwater against
a ₹2,500 threshold. One 0.05 tick down on the bid finished it. The stop never saw a price
move — it saw the spread.

You already have the plan doc for this: `docs/research/strategy/open15_vol_breakout/2026-09-07_stop_loss_whipsaw_dwell_plan.md`
(currently **untracked**). Candidate fixes to weigh there: a dwell/grace window before the
stop arms, an entry-time max-spread gate, or marking the first N polls at mid.

### 2. The stop has now cost money two days running

| Day | Symbol | Stop saved (net) |
|---|---|---|
| 09-10 | VBL | **+₹8,070** |
| 09-10 | CANBK | **−₹12,092** |
| 09-10 | MCX | **+₹2,239** |
| 09-10 | **net** | **−₹1,782** |
| 09-09 | MCX | held would have been +₹1,283 gross |
| 09-09 | BIOCON | held would have been +₹600 gross |

CANBK is the painful one: realized **−₹3,780 net**, while holding to the 09:30 scheduled exit
would have been **+₹8,910 gross** (mfe +₹11,543 at its peak). Check the **STOP-LOSS SCORECARD**
on `/open15_vol_breakout/logs` — the pre-registered rule (keep the stop only if after 20
events cumulative stop-saved > 0 AND ≥50% of stops were right) is the decision tool here,
and the evidence is trending against the stop.

### 3. `SCANNER_SYMBOLS` is stale — 4 names have no NFO contracts

ERROR at 09:10:01: **DALBHARAT, EXIDEIND, NUVAMA, SAMMAANCAP** dropped from today's
universe. The #647 fail-open filter handled it correctly (universe 211 → 207), so nothing
broke — but this logs as an ERROR every morning until the env list is cleaned.

### 4. One abandoned dispatch session

`local_df127dc9` ("Fno scan cycle") is **idle with its last message being a bare tool call**
(`tabs_context_mcp`) — it died mid-work with no summary. Almost certainly a casualty of the
bash outage. Nothing to recover; just be aware today's scan cycle may have produced no output.

---

## 🟢 Complete in last 24h

- **`local_34707f57` + `local_d8fc2d43`** — both "Fno scan cycle" EOD runs for 2026-09-09.
  Verdict **DONE**, consistent with each other: simplified engine (sandbox) 8 trades, 4W/4L,
  gross −₹1,924.90, charges ₹661.85, **net −₹2,586.75**. VBL short held to the 15:20 EOD
  flatten was 80% of the loss; COALINDIA was re-entered and stopped twice (Learning #7).
  Both correctly skipped a duplicate LEARNINGS.md entry.
- Yesterday's schema-drift findings are **resolved**. `open15_config`,
  `strategy_llm_config` and `broker_auto_login_settings` all report "table ready" at boot
  and there are **zero** `no such table` errors today. The open15 risk knobs are reading
  from the UI (`config_source: ui`), not falling back to env.

## ⏳ Running

- **`local_bb0ef1e8`** "Fno scan cycle" — 8 turns, blocked on bash, working around it via browser.
- **`local_bf4d33fa`** "Weekday trading standup" — 8 turns, same constraint.

---

## Git state

- **`dev` is exactly in sync with `origin/dev`** (both `fcd42ef3`). Nothing to push, nothing to FF.
- **Per-branch un-FF'd commit counts could not be computed** — that needs `git`, and bash is
  down. 533 refs exist locally; I'm not going to guess at them.
- **Working tree dirty: 99 entries** (from the boot dirty-check at 08:47:36):
  - **2 tracked modifications** — `.gitignore` (staged) and
    `strategies/simplified_engine/LEARNINGS.md` (unstaged).
  - **97 untracked**, all noise-by-accumulation: ~20 `backtest/options_open15/*` scratch
    scripts, 11 `db/*.bak.*` snapshots, **28 `docs/research/journal/*` files dating back to
    2026-08-21** (including every morning status report since), ~26 `log/restart_*.{err,out}`,
    and `url_1345069591.txt`.
  - Worth a cleanup pass: the journal files are real work product sitting untracked, and the
    `.bak` DBs + restart logs are pure clutter inflating every boot warning.

---

## OpenAlgo health

**Verdict: healthy.** Booted 08:47:35, alive and logging through 09:40+.

| Check | Result |
|---|---|
| Broker session | ✅ Primary auto-login **succeeded 08:48:13**; child `Didi-zerodha` **08:48:53**. Watcher running (300 s, 06:15–23:30 IST). |
| Master contract | ✅ Downloaded (last was 09-09, refreshed today). |
| Tick feed | ✅ Recovered. WS 403s at 08:47:42–08:48:12 were **pre-auto-login** (the normal 06:45–07:30 Kite flush), cleared once the token landed. |
| Scanner 1m / D | ✅ Fresh. 216/216 stale at 09:00–09:01 → straggler tick **09:31:43 healed all 49 remaining, `all_fresh=True`**. |
| Smoke check | ⚠️→✅ **FAILED 09:18:00** (1m + D stale), post-hold ARMED scope=TOTAL, **RELEASED 09:18:38** — 38 seconds. Self-healed, no signals lost. |
| Telegram | ✅ OpenAlgo is sending (09:30, 09:35). Two "Markdown parse failed → sent as plain text" warnings — cosmetic. |
| Errors today | **24 lines** in `errors.jsonl`, all accounted for: 20 = pre-login token/WS (08:47–08:48), 1 = stale SCANNER_SYMBOLS, 1 = smoke check (healed), 1 = a 403 LTP on `CANBK26SEP127.5CE` at 09:22:46 during the pre-login residue, 1 = scanner_dry tripwire. **No unexplained errors.** |

**Two standing niggles:**

- **`scanner_dry` tripwire WARN at 09:30** — last in-house scanner hit was **2026-09-09 15:25**,
  gap 36.8 min vs a 30 min threshold, and Chartink has no rows since the cutoff either. So
  it's a quiet market, not a dead scanner — but it's the second day this has tripped.
- **"Thread count elevated: 50 (threshold 50)"** — health alerts at 09:10, 09:15, 09:18.
  Sitting exactly on the threshold, auto-resolved at boot. Either raise the threshold or find
  the extra thread.

### open15 — today, live

`selected 15 · entered 5 · filled 3 · sim 2 · shadow 0 · unfillable 0 · rolling_added 9 · day done`

| Symbol | Source | Contract | Qty | Entry fill | Exit fill | Gross | Charges | **Net** |
|---|---|---|---|---|---|---|---|---|
| VBL | rolling | VBL29SEP26410CE | 3,825 | 13.30 | 13.27 | −₹114.75 | ₹500.66 | **−₹615.41** |
| CANBK | rolling | CANBK29SEP26127.5CE | 20,250 | 2.81 | 2.65 | −₹3,240.00 | ₹539.60 | **−₹3,779.60** |
| MCX | seed | MCX29SEP263300PE | 450 | 98.30 | 92.30 | −₹2,700.00 | ₹429.12 | **−₹3,129.12** |
| | | | | | | **−₹6,054.75** | **₹1,469.38** | **−₹7,524.13** |

All three reconciled to broker fills (`pnl_source: fill`). All three exited `reason=stop_loss`.
Two more (GVT&D, FORCEMOT) were sim-priced skips at `max_trades_cap`.

*Derivation note:* net is `gross − charges` from the three `fill_reconcile` lines, where
`pnl` is gross per the #552 convention. Confirm against the chip on
`/open15_vol_breakout/logs`, which is the single authority.

**Also worth knowing:** charges were **24% of gross** today, and none of the 6 seed picks
(TORNTPHARM, DIVISLAB, NATIONALUM, MCX-S, IOC, SBICARD) triggered on the seed side — all 15
`no_entry` rows failed the 1.5× volume gate while beyond the level. SBICARD reached a 9.36×
vol ratio but never broke its level.

### futures_follow_cap50 — ⏰ open position, T+1 exit due today

`WARNING 08:47:38`: **rehydrated open sandbox position `NIFTY29SEP26FUT` qty 65 (1 lot)**,
entry stamped 2026-09-09, **T+1 exit due today**. This is sandbox money, not real — but it is
the exact shape of #497, so confirm the **15:25 exit actually fires** (watchdog backstop 15:28).

---

## Today's schedule (Thursday)

| Time IST | Event | Status |
|---|---|---|
| 09:15 | Market open | ✅ done |
| 09:10–09:35 | open15_vol_breakout arm → exit → summary | ✅ done, 3 trades, −₹7,524 net |
| 15:18 | sector_follow_cap5_vol smoke check | pending |
| 15:20 | sector_follow + futures_follow_cap50 entry | pending |
| 15:25 | Exits — **incl. the NIFTY futures T+1 above** | ⏰ **watch this one** |
| 15:30 | EOD summary | pending |
| 15:45 | scanner_comparison_eod | pending |
| 16:30 | sector_follow data freshness check | pending |
| 17:15 | postmarket_review | pending |

---

## ⚠️ Telegram

**Not sent.** The direct Bot API path needs Python/`requests` in the sandbox, and the sandbox
failed to mount. I also can't decrypt `bot_config.bot_token` (Fernet, needs `APP_KEY`) without
a shell. OpenAlgo's own Telegram channel **is working** — it delivered at 09:30 and 09:35 — so
you'll have the app's own open15 summary on your phone; this journal is the part that needs
opening directly in Cowork.

---

## What I'd do first

1. **Decide on the stop.** Two days of negative stop-saved, and today's VBL case shows the
   threshold can be consumed by the spread before any price action. Read the scorecard, then
   either add a dwell window / spread gate per your 09-07 plan doc, or widen `stop_loss_inr`.
2. **Confirm the 15:25 NIFTY futures T+1 exit fires.**
3. **Clean `SCANNER_SYMBOLS`** — drop DALBHARAT, EXIDEIND, NUVAMA, SAMMAANCAP.
4. **Commit or gitignore the 28 untracked journal files**, and sweep the `.bak` DBs and
   `restart_*` logs.
5. **Report the Cowork sandbox outage** if it persists — it silently degrades every scheduled
   task, and today three of them hit it at once.
