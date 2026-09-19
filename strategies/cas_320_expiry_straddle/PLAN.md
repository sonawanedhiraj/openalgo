# CAS 3:20 Expiry-Day ATM Straddle — Implementation Plan

- **Issue:** [#740](https://github.com/sonawanedhiraj/openalgo/issues/740)
- **Status:** SHIPPED 2026-09-19 (PR for #740) — trading the sandbox book from the first expiry day; R69 open (INSUFFICIENT until ≥ 30 sessions).
- **Strategy key / `mode_key` / payload `strategy` label:** `cas_320_expiry_straddle` (one string for all three — `basket_order_service` and `options_multiorder_service` route on the payload label, `place_order` on `mode_key`; keeping them identical removes a routing class of bug).
- **Source:** operator-supplied transcript of a YouTube video ("CAS 3:20 PM strategy", recorded 2026-09-18). The video's own numbers are NOT evidence (§2).

## 0. What the video says

| Rule | Video |
|---|---|
| Days | Weekly expiry days only: NIFTY Tuesday, SENSEX Thursday |
| Strike | Mark ATM from the spot at **15:15** (spot "pauses" at 15:15) |
| Entry | Buy ATM CE **and** ATM PE, same strike, same quantity, both at **15:20** |
| Exit | When combined premium reaches **2× cost** ("1:2"), else **before 15:30** |
| Thesis | Since CAS (2026-08-03) a big index candle prints in the last minutes; one leg goes to ~0, the other "explodes" |
| Sample | SENSEX 27-Aug +329 pts, 3-Sep +303, 10-Sep +222, 17-Sep **−186** (3 of 4), priced at the 15:20 candle open and the 15:20–15:30 **high** |
| Own caveats | "hero or zero", "risk only what you can lose", "not a holy grail" |

## 1. Verified facts that change the spec (checked 2026-09-18; memory `cas-expiry-day-index-options-facts`)

1. **F&O trades until 15:40** on both NFO and BFO, expiry day included ([Zerodha Z-Connect CAS](https://zerodha.com/z-connect/general/everything-you-need-to-know-about-closing-auction-session-cas), [Anand Rathi](https://anandrathi.com/blog/nse-extends-trading-hours)). "Exit before 15:30" is a choice, not a constraint. Expiry-day settlement = the index close built from the CAS equilibrium prices of constituents, known ~15:30–15:35.
2. **After 15:15 the only live "spot" is the Indicative Index Value (IIV)** computed from the auction book's indicative equilibrium prices — nothing trades at it ([Quintal Mind](https://www.quintalmind.com/nifty-indicative-price)). First print ~15:20:00, 1/s updates, order entry closes at a random moment 15:28–15:30, official close by ~15:30. The video's "big last candle" is the IIV path. It is thin-book noise: 2026-09-15 NIFTY first print 23,342 → low 22,852 (15:20:57) → close 23,118 (range 490); 09-08 23,806 → 23,354 → 23,635. Options are priced off this level minute to minute, then settle on the real close.
3. **Zerodha MIS auto square-off: F&O 15:26, CAS stocks 15:12** ([support article](https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/intraday-auto-square-off-timings)). Both legs must be **NRML** (long options = premium only; no extra margin).
4. **Lot sizes since Jan 2026: NIFTY 65, SENSEX 20**, BANKNIFTY 30 ([Sahi](https://www.sahi.com/blogs/nifty-lot-size-2026-bank-nifty-sensex)); the live master contract agrees (`NIFTY…CE lotsize=65`, `SENSEX01OCT2674300CE lotsize=20`). Always read `SymToken.lotsize`.
5. **Expiry days come from the master contract, not `weekday()`.** NIFTY expiries are Tuesdays but shift to **Monday** around holidays (2026-10-19, 2026-11-23 in the live `symtoken`); SENSEX are Thursdays (2026-09-24, 10-01, …).
6. **Regulatory risk is live.** SEBI's consultation paper (2026-09-03/04, comments to **2026-10-03**) proposes (a) blended-VWAP or CTS-VWAP expiry settlement instead of the CAS close and (b) **discontinuing the IIV display** ([Upstox](https://upstox.com/news/market-news/financial-regulations/sebi-proposes-changes-to-expiry-day-cas-settlement-price-methodology-key-factors-that-investors-need-to-know/article-200228/), [Value Research](https://www.valueresearchonline.com/stories/229632/sebi-closing-auction-expiry-day-options-proposal/)). Cited: expiring index-option premium ₹190 cr/min in 15:20–15:30; on 2026-08-13 two entities moved the SENSEX close 240 pts for ₹57 L in cash and made ₹3.68 cr on options. Either change removes most of what this strategy trades.
7. **No historical data exists for a backtest** (audit, §4): Kite serves option bars only for contracts still listed (expired weeklies are gone); `index_options_eod` is NIFTY/BANKNIFTY **monthly** to 2026-06-04; `fo_bhavcopy_eod` is stock options only; historify NIFTY 1m is **padded flat from 15:15 and only the 15:29 bar jumps to the close** (verified for 2026-09-02..18) — the IIV path is not in it; there are **zero SENSEX / BSE_INDEX rows** anywhere. Quintal Mind publishes per-session IIV summaries (first/low/high/close) from 2026-09-04 only, third-party.

## 2. Honest assessment before building

- **The mechanism is real but it is not "momentum".** A long ATM straddle bought at 15:20 on expiry day pays if |settlement − K| (or the peak IIV excursion, if you can sell into it) exceeds the premium paid plus spread. Since CAS, the IIV whipsaws early (thin auction book) and the close can land far from the 15:15 print, so the *distribution* of |move| in that window is wider than pre-CAS. Whether the straddle is priced to reflect that is the whole question — sellers have had seven weeks to raise 15:20 premiums.
- **The video's backtest is look-ahead** (registry cross-cutting finding 2026-07-19): exit at the window **high**, entry at a candle open, 4 samples, one underlying. Its "3 of 4" is uninformative. Our R58/R66 lessons apply: price at the bid you can actually sell at, at a time you can actually know.
- **This is a bet against sellers in the single most crowded 10 minutes in Indian options** (₹190 cr/min). Expect spreads to blow out exactly when the 2× target is near.
- **The edge has a regulator-imposed shelf life.** If the IIV goes dark, premiums during 15:20–15:30 will price off nothing until the close prints; if settlement moves to VWAP, the close no longer carries the auction jump. Pre-registered kill criterion (§8).
- **Prior rounds are not against this.** R29v2/R55/R66 rejected option *buying* as a vehicle for **directional** stock/sector signals where theta and spread ate the edge over hours. This is a 10-minute, non-directional, expiry-hour bet — a different question, so it gets a round (R69), but it inherits their cost discipline.
- **Cost of finding out is small**: 1 lot each ≈ ₹5–10k of premium per expiry (max loss), and the sandbox book pays nothing.

## 3. Strategy spec v0.1 (what gets built; every number is a default in `config_snapshot.json`)

| Item | v0.1 | Note |
|---|---|---|
| Underlyings | `NIFTY` (spot `NSE_INDEX`, options `NFO`), `SENSEX` (spot `BSE_INDEX`, options `BFO`) | **Which of the two to TRADE is a UI toggle per underlying** (`trade_nifty`, `trade_sensex`, see §3a); the recorder always covers both |
| Expiry-day test | `expiry_service.get_expiry_dates(u, NFO|BFO, "options")["data"][0]` parsed with `futures_follow_service._parse_expiry` **== today (IST)** | Handles holiday-shifted expiries; no weekday logic |
| ATM basis | Spot LTP captured at **15:15:00** (last continuous print) → `find_atm_strike_from_actual` over `get_available_strikes(u, expiry, CE, exch)` | Variant B (ATM at the 15:20 first IIV print) is **measured** from the recorder, not traded |
| Entry | **15:20:00 IST** + `entry_delay_s` (default 0), two BUY legs, `product=NRML`, exchange NFO/BFO, `quantity = lots × lotsize` | Placed per leg via `place_order(payload, api_key, mode_key=STRATEGY_NAME)`; sandbox = MARKET; live = marketable LIMIT at `ask + limit_ticks × tick_size` (index options bypass `ensure_live_safe_pricetype`) |
| Fill truth | `get_order_status` → `average_price`, `filled_quantity` **by presence** (ACK ≠ fill, #626) | A rejected leg → `fill='paper'`; a one-legged fill is journaled as such and exited by the same rules (it is a naked long option, max loss = its premium) |
| Target exit | Combined **bid** mark of both legs × qty ≥ `target_mult` (2.0) × combined entry cost, **net of modelled charges**, held for `confirm_polls` (2) consecutive polls → SELL both | Bid marks per `open15_pnl_curve.mark_for_row`; #716 lesson: never a single LTP print |
| Hard exit | `hard_exit_time` default **15:28:00** (before the random 15:28–15:30 book close), SELL any leg with bid > 0 | A leg with bid 0 / no bid is left to **expire worthless** (cash-settled, costs nothing; selling at 0.05 costs ₹20+) and journaled `expired_worthless` |
| Fallback flatten | **15:38:00**, PROTECTED: SELL any leg still open per an **affirmative** position book (`get_positionbook(mode_key=…)`) | Catches a rejected exit; an unreadable book still sends (believed-filled asymmetry, #626) |
| Settlement counterfactual | For every traded leg record `|settle − K|` intrinsic at the official close, STT-on-exercise modelled (0.125% of intrinsic), as `settlement_cf_pnl` — numbers only | Answers "was selling at 15:28 better than holding to settlement?" |
| Sizing | **`lots_nifty` / `lots_sensex` are UI fields** (default 1 each); `quantity = lots × SymToken.lotsize` (the exchange multiple, shown read-only beside the field); hard cap `max_premium_inr` per underlying per expiry (default 15,000, UI) | A save that would exceed the cap is refused at the arm with an alert, not silently trimmed |
| Kill switch | Strategy pauses (`strategy_runtime_override`) after `max_consecutive_full_losses` (3) or weekly net loss > `max_weekly_loss_inr` | Plus the §8 regulatory kill, operator-actioned |
| Charges | `option_round_trip_charges` for NFO; a **BFO twin** with BSE's transaction rate (to be verified against Zerodha's charges page before coding) | Exercise STT only in the counterfactual |
| Mode | **Exactly like open15: `sandbox` or `live`, nothing else.** No env flag, no observe/scaffold state, no code switch. The service boots on every start, places both legs into `sandbox.db` from the first expiry day, and goes LIVE the moment the operator flips the `/strategies` Live/Sandbox toggle (`strategy_mode_service.flip_mode` → `strategy_mode` row); the navbar Analyze toggle still forces sandbox (#440 platform kill switch) | `resolve_order_mode('cas_320_expiry_straddle')` default-denies to sandbox with no row |

### 3a. UI-editable config (operator requirement, 2026-09-19)

Pattern = open15's `open15_config` (issue #425 follow-up): a **single-row table `cas_straddle_config`** (`database/cas_straddle_db.py`), every column nullable, **NULL = the code default constant in `cas_straddle_service.py` (no env seeds — the UI row is the only knob)**, served by `GET/POST /cas_320_expiry_straddle/api/config` (`{env_defaults, override, effective_today}` — the open15 shape at `blueprints/open15_breakout.py:62`). A save applies at the **next 15:12 arm** (today's, if saved before 15:12 IST); the arm snapshots an effective day-config and stamps it into `cas_straddle_sessions` and the `armed` decision-log row, so a later restart or edit can never make the day's record ambiguous (#645).

| Field | Type / bounds | Code default (NULL row) | Meaning |
|---|---|---|---|
| `trade_nifty` | bool | true | Place NIFTY straddles on NSE/NFO |
| `trade_sensex` | bool | true | Place SENSEX straddles on BSE/BFO |
| `lots_nifty` | int 1–10 | 1 | Lots per leg |
| `lots_sensex` | int 1–10 | 1 | Lots per leg |
| `max_premium_inr` | int | 15000 | Per-underlying per-expiry premium cap (refuses, never trims) |
| `target_mult` | float 1.1–5.0 | 2.0 | Combined-bid exit multiple |
| `hard_exit_time` | `HH:MM:SS`, clamped ≤ 15:37:00 | 15:28:00 | Timed exit; the 15:38 fallback flatten is not editable |
| `poll_interval_s` | int 2–60 | 2 | Monitor cadence (applies within one cycle, like open15) |

The exchange lot multiple (65 / 20 today) is **read from `SymToken.lotsize` at the arm and displayed read-only** next to each lots field with the resulting quantity and an estimated premium outlay from the last session's straddle cost — it is exchange-set and must not be overridable (a non-multiple quantity is a guaranteed rejection). Live-vs-sandbox stays the existing `/strategies` toggle (`flip_mode`); the config form never routes orders.

**Where it lives:** a "Settings" card on `/strategies/cas_320_expiry_straddle` (React `StrategyDetail.tsx`, strategy-conditional like the futures_follow `EntryEvaluationCard` at L2230), reachable from `/strategies` → card, so the walkthrough validation from the navigation entry point holds (#430/#425 rule). The blueprint route uses the futures_follow `_authed` pattern, not `check_session_validity` on any broker-page path. Validation: server-side bounds + the 15:12 arm re-validates against the live master contract (an underlying whose contracts are missing is skipped loudly, not traded at the stale value).

## 4. Reuse map (audit 2026-09-18) — build nothing that exists

| Need | Existing code | Gap |
|---|---|---|
| ATM CE/PE for an INDEX on NFO/BFO | `services/option_symbol_service.get_option_symbol(underlying, exchange, expiry_date, strike_int=None, offset="ATM", option_type, api_key, underlying_ltp)` (L529; SENSEX/BANKEX → BSE_INDEX at L576-591), `get_available_strikes` L279, `find_atm_strike_from_actual` L385, `get_option_exchange` L496 | Returns ONE leg per call — compose ×2 sharing `underlying_ltp` and the expiry string. **Do not** use `open15_option_shadow.resolve_atm_option` (NFO-only, one side) or `is_expiry_blocked` (stock physical-delivery rule; would roll off the expiry-day contract) |
| Weekly expiry list | `services/expiry_service.get_expiry_dates` L12 (non-expired, sorted, `DD-MMM-YY`); `futures_follow_service._parse_expiry` L361 | No "is today an expiry day" helper anywhere — write `is_expiry_day(underlying, exch, today)` from these two |
| Quotes, one batch | `services/quotes_service.get_multiquotes(symbols=[{symbol, exchange}], api_key)` L327; Zerodha batches 500; mixed NSE_INDEX/BSE_INDEX/NFO/BFO rows OK; `open15_pnl_curve._batched_quotes` L504 as the shape | None |
| Bid/ask marks, net rule | `open15_pnl_curve.mark_for_row` L570, `exit_charges_estimate` L592, `clamp_live_poll_interval` L57 (2–60 s) | Generalise mark helper for a 2-leg combined mark |
| Order placement + routing | `place_order_service.place_order(order_data, api_key, mode_key=)` L324; `mode_service.resolve_order_mode` L181 | Use per-leg `place_order`; not `options_multiorder_service` (no `mode_key`, `emit_event=False`) and not `close_position_service` (closes everything) |
| Fill / book readback | `orderstatus_service.get_order_status` L307 (sandbox-aware via `sandbox_order_exists`), `positionbook_service.get_positionbook(mode_key=)` L167 | None |
| Service template | `services/futures_follow_service.py`: `production_*` DI seams, `_entry_held_by_override` L1358, kill switch L892-925, `register_jobs` L2599 (shared historify scheduler, `CronTrigger(... timezone="Asia/Kolkata")`, `replace_existing=True`), module-level `_*_job` wrappers, `init_futures_follow_service` L2753; `sector_follow_service.resolve_schedule` L111 for HH:MM env times | Clone the shape, not the file |
| Poll loop thread | `open15_breakout_service._ensure_risk_thread` L3412 / `_risk_loop` L3431 (beat → done → re-resolve interval → tick in try/except → sleep) | Own thread; open15's `_exit_open_row` hardcodes `exchange="NFO"` (L3208) — do not share it |
| Journal DB pattern | `database/futures_follow_db.py` (`_ensure_columns` L90, `record_trade` L127, `db_session.remove()` in `finally`); `database/open15_breakout_db.net_pnl_expr/net_pnl_of_row` (#552 one-definition rule) | New module `database/cas_straddle_db.py` |
| Control API | `blueprints/futures_follow.py` (`_authed`, `_service_or_503`, status/positions/pause/resume/close_all) | New blueprint, same routes + `/polls`, `/sessions` |
| Dashboard | Directory discovery in `blueprints/strategies_dashboard_api.py` L108; the **four** if/elif chains at L1394-1403, L1545-1630, L1635-1660, L1876-1885 | Add the fifth branch in all four |
| Registries | `services/scheduler_registry.py` `JobSpec` L71, CATALOG L114 (group const `_CAS = "strategy:cas_320_expiry_straddle"`); `services/thread_registry.py` `ThreadSpec` L90, `open15-risk-monitor` entry L338 as the model; enforced by `test/test_scheduler_registry.py::test_every_add_job_id_in_the_source_is_catalogued` and the thread anti-rot test | Add every `add_job(id=)` and `Thread(name=)` in the same commit |
| Telegram | `services/notification_service.NotificationService.notify(event_type, message)` L285 — **unknown event types are dropped** | Register `cas_straddle` in `per_event` + `NOTIFY_CAS_STRADDLE` |
| Trading day / session | `data_freshness_service.is_trading_day` L115; `market_calendar_db.get_effective_session_window` L824 (shortened / muhurat sessions) | None |
| Mirror to child accounts | `place_order_with_auth` LIVE seam fans out automatically (#468/#496) | Nothing to build; a child needs `capital_per_trade_inr` for this strategy or it is `skipped_no_capital` |
| Charges | `open15_option_shadow.option_round_trip_charges` L43 (NFO rates, ₹20 flat) | BFO rate twin |

## 5. The poll recorder inside the service (part of the one PR)

Goal: from the first expiry day we own every 2-second print of the ATM straddle ladder across the auction, for both underlyings, from the live feed — and the same polls are the marks the exit rule trades on. This is the strategy's own monitor thread, not a separate observe-only component. **Data collection is unconditional** (it runs for both underlyings whether or not each is toggled to trade — collection is a fact, trading is a choice; the #647/#651 split).

- **Job `cas_straddle_arm` 15:12 mon-fri** (shared scheduler): for each enabled underlying → `is_trading_day`, `is_expiry_day`, broker session live, spot quote OK, strikes resolvable. Non-expiry day → one INFO line, exit.
- **Thread `cas-straddle-monitor`** (daemon, catalogued, `cadence_sec` at the clamp max, window `15:14–15:41 IST`, `done()` on completion — #709). Every `poll_interval_s` (default 2, clamped 2–60): ONE `get_multiquotes` over, per underlying, the spot + **ATM ±2 strikes, both sides** (10 contracts; strikes fixed from the 15:15:00 spot; at 15:20:00 the first-print ATM is added if different). Persists a `cas_straddle_polls` row per contract per poll (`ts, underlying, symbol, strike, side, ltp, bid, ask, volume, oi`) plus a spot row. ~800 polls × 11 symbols ≈ 9k rows per underlying-day — SQLite is fine.
- **The spot row answers a question we cannot answer offline:** whether Kite's live quote for `NIFTY@NSE_INDEX` / `SENSEX@BSE_INDEX` carries the IIV after 15:15 (charting apps show it; the historical API pads it flat). If it does not, IIV is reconstructed from put–call parity on the ±2 ladder (`S ≈ K + C − P`), which the ladder makes possible.
- **`cas_straddle_sessions`** — one row per underlying-day, written at 15:41: `close_1515, atm_strike, straddle_ask_1520, straddle_bid_1520, first_iiv_print/at, iiv_low/high/at, max_combined_bid/at, t_first_2x (or NULL), combined_bid_1525/1528/1530/1535, settle, settle_intrinsic, spread_pct_1520, spread_pct_at_peak`. This row IS the backtest input.
- **Telegram** at 15:41: the session row in one message per underlying. **Weekly** (Friday 16:00 job or folded into the EOD): running tally of sessions and the video-faithful sim P&L.
- **SENSEX/BSE_INDEX 1m feed**: add `SENSEX` to the historify universe backfilled by the existing convergence check (needs the `NSE_INDEX`-style routing extended to `BSE_INDEX` in `scanner_presubscribe.resolve_exchange_for_symbol` or a dedicated symbol list) so spot context exists for SENSEX days.
- **No flags.** No `CAS_STRADDLE_*` env vars, no master enable, no observe mode. The service is constructed and registers its jobs on every boot (`init_cas_straddle_service(app=app)` in `app.py`, unconditional, like the fill-reconcile and risk guards after #651). The only controls are: the `/strategies` Live/Sandbox toggle, the Settings card (§3a), and `pause`/`resume` (`strategy_runtime_override`). Telegram routes through `notify('cas_straddle', …)` — the per-event key is registered and defaults ON; the platform's existing `NOTIFY_TELEGRAM_ENABLED` master is the only switch above it, and that is not this strategy's flag.
- Docs in the same PR: `docs/SYSTEM_MAP.md` (job, thread, tables), `docs/PARAMETER_LOG.md`, CLAUDE.md strategy paragraph, registry "Currently Testing" row.

## 6. Phase 1 — Round 69 backtest (registry entry direct to dev when it lands)

- **Input:** `cas_straddle_sessions` + `cas_straddle_polls` only. No synthetic premiums (BS is optimistic for index option buying — R36-real).
- **Primary (pre-registered, video-faithful):** ATM from the 15:15 spot; buy both legs at the **ask** of the first poll at/after 15:20:00; mark at the **bid**; exit when combined bid ≥ 2× combined ask-cost net of charges for 2 consecutive polls, else at the 15:28:00 poll; charges per exchange; 1 lot.
- **Measured variants (numbers only, no scorecards until asked):** strike from the 15:20 first print; entry 15:20:30 / 15:21 / 15:22 (after the first-print shock); target 1.5× / 3× / none; hard exit 15:25 / 15:29:30 / hold-to-settlement (intrinsic − exercise STT); ±1 strike (OTM strangle at the same cost); NIFTY-only / SENSEX-only.
- **Decision rule (pre-registered):** verdict is **INSUFFICIENT** until ≥ 30 expiry-sessions (≈ 15 weeks on both underlyings); an interim read at 8 sessions is a status note, nothing more. The strategy is already trading in sandbox while this accumulates (sandbox fills ARE the primary variant's data, plus the polls for the variants). The R69 verdict advises the operator's **live** flip: recommend live only if the primary variant is net-positive, stays ≥ 0 with its best session removed, and its 2× hit-rate × payoff clears 1.0 after charges. The flip itself is the operator's, via the toggle, at any time — the report informs it, nothing in code gates it. Compare every variant with the do-nothing baseline of `E|settle − K| − cost`.
- **Shortcut worth one afternoon:** the 10 Quintal Mind sessions (IIV first/low/high/close) + the video's four SENSEX straddle costs give a rough prior on whether the IIV range even reaches 2× a plausible straddle cost. Report it as a prior, not a result.

## 7. The service (one PR; boots in sandbox, live by toggle)

Files (all new unless marked):
- `strategies/cas_320_expiry_straddle/{README.md, LEARNINGS.md, VERSION_LOG.md, config_snapshot.json}` — the dashboard discovers the directory; `config_snapshot.json` carries `mode: sandbox`, `deployable: true` (the dashboard toggle is enabled from day one, as for open15 and futures_follow; a `false` here would grey out the very switch the operator asked for), and an `llm_context` block if the Stage-1 veto is ever wired (not in v0.1 — R65 showed the veto adds nothing measurable).
- `services/cas_straddle_service.py` — `CasStraddleService` with `production_*` seams: `expiry_resolver`, `contract_resolver` (both legs), `quote_batch`, `order_placer` (`place_order(..., mode_key=STRATEGY_NAME)`), `fill_reader`, `book_reader`, `session_checker`, `telegram_notifier`, `clock`. Jobs: `cas_straddle_daily_reset` 09:00, `cas_straddle_arm` 15:12, `cas_straddle_entry` 15:20, `cas_straddle_hard_exit` at `hard_exit_time`, `cas_straddle_fallback_flatten` 15:38 (**PROTECTED**, `safety_note`: a long option left open past 15:40 is cash-settled — harmless for OTM, but an ITM leg the operator meant to sell becomes an exercise with STT), `cas_straddle_eod_summary` 15:45. The monitor thread records the ladder polls AND evaluates the target rule on the same batch.
- `database/cas_straddle_db.py` — `cas_straddle_config` (single row, §3a, `get_config`/`save_config`), `cas_straddle_trades` (per leg; `fill ∈ {real, paper}`, `exit_reason ∈ {target, hard_exit, fallback, expired_worthless, error}`, `settlement_cf_pnl`), `cas_straddle_polls`, `cas_straddle_sessions`; `net_pnl_of_row` / `net_pnl_expr` as the single P&L definition; `_ensure_columns`.
- `blueprints/cas_straddle.py` — `/cas_320_expiry_straddle/api/{status,config,positions,polls,sessions,pause,resume,close_all}`; registered in `app.py` next to `futures_follow_bp`.
- `frontend/src/pages/strategies-dashboard/StrategyDetail.tsx` + `frontend/src/api/strategies-dashboard.ts` (edits) — the Settings card (§3a) and the session table; rebuild + commit `frontend/dist`.
- `blueprints/strategies_dashboard_api.py` (edit) — the four chains. Card shows: today's expiry-day verdict, resolved contracts, combined cost / mark / 2× target line, session table.
- `services/scheduler_registry.py`, `services/thread_registry.py`, `services/notification_service.py` (edits).
- `app.py` (edit) — `init_cas_straddle_service(app=app)`, unconditional, after the job-run audit listener.
- `docs/PARAMETER_LOG.md` (the UI fields and their code defaults — they are tunables even without env vars), `docs/SYSTEM_MAP.md`, `CLAUDE.md` (edits, same PR). Nothing in `.sample.env`.
- Tests `test/test_cas_straddle_service.py`: config resolution (NULL → code default, bounds, an underlying toggled off is still recorded but never ordered, quantity = lots × master lotsize, cap refuses rather than trims, hard-exit clamp); expiry-day detection incl. a holiday-shifted Monday NIFTY expiry and a SENSEX Thursday; ATM from actual strikes on both exchanges; entry payloads carry `NRML` + `mode_key` (routing tested, not a mocked `place_order` — #497: no `strategy_mode` row → sandbox, row `live` + Analyze off → LIVE, row `live` + Analyze on → sandbox); 2× rule on bid marks net of charges with confirm polls; hard exit leaves a zero-bid leg to expire; fallback flatten only on an affirmative book; rejected entry → paper, rejected exit → error + alert (asymmetric, #626); one-leg fill handled; thread `done()` on completion; BFO charges rate. Registry/thread tests enforce cataloguing automatically.

## 8. Risk controls and kill criteria

- Max loss per underlying per expiry = premium paid (bounded by `max_premium_inr`); no averaging, no re-entry, no second attempt on a rejected leg.
- `strategy_runtime_override` pause honoured at entry only; exits and the fallback flatten are never gated.
- **Regulatory kill (operator decision, pre-registered):** the day SEBI/NSE announce IIV discontinuation or a VWAP-based expiry settlement, the operator flips the toggle back to sandbox (or pauses) and R69's window is closed at whatever sample exists. The sandbox book and the polls keep running — the post-change data is worth having.
- Consecutive-full-loss pause (3) and weekly loss cap; both Telegram once.
- Live entries are marketable LIMIT at ask + ticks, never MARKET, given the 15:20 spread blow-out risk; sandbox uses MARKET. When to flip live is the operator's call — the plan recommends waiting for the R69 read, but nothing in code enforces it.

## 9. Decisions needed from the operator (defaults applied if silent)

1. ~~Trade both underlyings or NIFTY first?~~ — now a UI toggle per underlying (§3a); defaults both ON.
2. Hard exit 15:28:00 (video-faithful) vs hold-to-settlement — default **15:28:00 traded, settlement measured**.
3. ~~`max_premium_inr`~~ and lots — now UI fields (§3a); defaults 1 lot each, ₹15,000 cap.
4. Record the ±2-strike ladder (10 contracts) or ATM only — default **ladder** (needed for the IIV reconstruction and the strangle variant; costs nothing).
5. Should the Stage-1 LLM veto review the entry — default **no** (R65).

## 10. Sequence

| Step | Deliverable | Gate |
|---|---|---|
| The PR | Full service (jobs, monitor + polls, journal, config API + Settings card, dashboard, registries, docs, tests), trading in **sandbox** from boot | Merge + restart before **Tue 2026-09-22 15:12 IST** so the first NIFTY expiry is traded in sandbox and recorded; SENSEX Thu 09-24 |
| Any time | Operator flips Live on `/strategies` | Operator decision; the plan recommends after the R69 read |
| Weekly | EOD/weekly Telegram tally; LEARNINGS.md entries per expiry | — |
| ~2026-10-16 | Interim read at ≥ 8 sessions; SEBI outcome (comments closed 10-03) folded in | Status note |
| ≥ 30 sessions (~Jan 2027) or earlier on a regulatory change | R69 verdict → registry (direct to dev) | Advises the live flip; never gates it |
