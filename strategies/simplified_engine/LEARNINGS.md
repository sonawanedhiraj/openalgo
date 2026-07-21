# Simplified Engine — Strategy Learnings

**Strategy**: Chartink FnO Intraday Breakout (Long/Short)
**Engine**: `SimplifiedStockEngine` in `services/simplified_stock_engine_core.py`
**First live session**: May 20, 2026 (sandbox mode)

---

## Strategy Overview

Scans Chartink for FnO stocks with >3% intraday moves, arms the engine for 5-minute
candle breakout entries with ATR-based stop-loss, volume confirmation, and RR-based
trailing. All positions flatten at 15:20 IST.

**Screeners**:
- Buy: `https://chartink.com/screener/fno-intraday-buy-20`
- Sell: `https://chartink.com/screener/alert-for-intraday-sell-fno`

**Webhook**: `POST /chartink/simplified-stock-engine/c7d08357-6fe1-4603-bd2a-be4c9f9e06ac`

---

## Current Live Config (as of May 29, 2026)

| Parameter | Value | Notes |
|-----------|-------|-------|
| `atr_sl_mult` | 1.2 | Reverted to default; tighter stops |
| `max_trades_per_day` | 6 | Increased from 4; more opportunities |
| `cooldown_candles` | 3 | 15-min cooldown after a trade closes |
| `capital` | 20,000 | Base capital |
| `leverage` | 5.0 | Intraday leverage |
| `max_risk_per_trade` | 500 | Max loss per position |
| `volume_multiplier` | 2.5 | Entry only on 2.5× average volume |
| `atr_period` | 14 | Wilder's ATR lookback |
| `no_new_entries_after` | 15:10 | No entries in last 20 min |
| `eod_exit_time` | 15:20 | Force exit all positions |
| `mode` | live | Real trading via Zerodha |

---

## Daily Results Log

### May 20, 2026 (First Day — Sandbox)
- **Market regime**: Strong bullish trend, all 8 scanned stocks >3% gainers
- **Backtest result**: 6 trades, 5W/1L, net +₹704.72 (3.52% ROI)
- **Best performer**: POWERINDIA (+₹308.67), SIEMENS (+₹276.00)
- **Only loser**: HINDPETRO (-₹454.23) — stopped out same candle
- **Note**: Backtest used old hardcoded config (atr_sl_mult=1.2, max_trades=6). Not
  directly comparable to live config.

### May 21, 2026 (Second Day — Sandbox, Automated Pipeline)
- **Market regime**: Mixed/choppy after opening rally
- **Live result**: 6 trades, 5W/1L, net **+₹621.55**
- **Backtest result** (old config): 6 trades, 2W/4L, net **-₹1,479.25**
- **Discrepancy**: ₹2,100.80 — caused by config mismatch (see below)
- **Key trades**:
  - SAMMAANCAP: +₹313.95 (held 1h47m, trailing stop locked profit)
  - GRASIM: +₹268.80 (quick 14-min trade)
  - ANGELONE (2nd entry): -₹528.20 (only loser, re-entry failed)
- **Stocks live-only**: ANGELONE, ADANIENSOL (not in backtest stock list)
- **Stocks backtest-only**: APOLLOHOSP (stopped out immediately in backtest)

### May 22, 2026 (Third Day — Sandbox)
- **Market regime**: Mixed — SELL screener produced signals (GLENMARK), BUY side had
  broad universe (DIXON, VBL, ASTRAL, SAMMAANCAP, KAYNES, MFSL)
- **Live result**: 4 trades (max hit), 3W/1L, net **+₹365.30**, win rate **75%**
- **Trade breakdown**:
  - DIXON (LONG): BUY 7 @ ₹11,822 → SELL @ ₹11,748 | **-₹518.00** | Only loser, large-cap SL hit
  - GLENMARK (SHORT #1): SELL 32 @ ₹2,293.10 → BUY @ ₹2,287.20 | **+₹188.80** | 44-min hold
  - GLENMARK (SHORT #2): SELL 38 @ ₹2,281.00 → BUY @ ₹2,270.80 | **+₹387.60** | ~1h51m hold, best trade
  - VBL (LONG): BUY 186 @ ₹537.25 → SELL @ ₹538.90 | **+₹306.90** | Quick 12-min scalp
- **SELL direction worked**: GLENMARK was the first productive SHORT trade — two wins
  from the sell screener accounting for ₹576.40 combined (157% of net P&L)
- **Tick logging active**: 80,468 ticks / 6.77 MB written, no drops (final EOD)
- **Cooldown**: VBL and GLENMARK both entered cooldown after exits
- **Armed at close**: BUY — DIXON, VBL, ASTRAL, SAMMAANCAP, KAYNES, MFSL; SELL — GLENMARK
- **Errors**: WebSocket DNS failures (`getaddrinfo failed`) around 14:17 — transient,
  auto-recovered. No impact on trading.

### May 26, 2026 (Fourth Trading Day — Live/Analyze Mode)
- **Market regime**: Mixed — both BUY and SELL screeners active. BUY side had PREMIERENE,
  ADANIPOWER, TMPV, JSWENERGY. SELL side had CONCOR, RVNL. Shorts outperformed longs.
- **Live result**: 4 trades (max hit), 2W/2L, net **+₹164.15**, win rate **50%**
- **Trade breakdown**:
  - CONCOR (SHORT): SELL 124 @ ₹484.65 → BUY @ ₹476.50 | **+₹1,010.60** | 5h21m hold, best trade — held nearly all day, exited at 15:15 (likely EOD flatten)
  - RVNL (SHORT): SELL 379 @ ₹263.75 → BUY @ ₹263.30 | **+₹170.55** | 51-min hold, small scalp
  - PREMIERENE (LONG): BUY 85 @ ₹1,018.00 → SELL @ ₹1,012.00 | **-₹510.00** | 1h13m hold, stopped out
  - ADANIPOWER (LONG): BUY 300 @ ₹245.25 → SELL @ ₹243.56 | **-₹507.00** | 18-min hold, stopped out
- **SHORT direction dominated**: Both SHORT trades were winners (+₹1,181.15 combined), both
  LONG trades were losers (-₹1,017.00 combined). Net +₹164.15 entirely from shorts.
- **CONCOR was a standout**: Held for 5+ hours with only ₹4.01 risk/share, captured ₹8.15/share
  (>2R profit). This is the type of all-day runner that Learning #4 identifies as the strategy's edge.
- **Tick logging active**: 83,951 ticks / 7.13 MB written, 0 drops
- **Armed at close**: BUY — PREMIERENE, ADANIPOWER, TMPV, JSWENERGY; SELL — CONCOR, RVNL
- **Errors**:
  - Pre-login auth error at 08:33 (benign, before Zerodha session started).
  - **EOD flatten failure at 15:20**: Engine tried to exit CONCOR SHORT at `eod_exit_time=15:20`
    but got rejected: "MIS orders cannot be placed after square-off time (15:15 IST)." The
    position was already closed at 15:15:01 (broker auto-square-off), so no financial impact.
    However, this reveals a config bug: `eod_exit_time` (15:20) is *after* the broker's MIS
    cutoff (15:15). Should be changed to 15:10 or 15:12 to ensure the engine exits before
    the broker forces a market-price square-off.
  - **Action needed**: Update `eod_exit_time` from 15:20 → 15:10 in engine config.

### May 27, 2026 (Fifth Trading Day — Live Mode)
- **Market regime**: BUY-dominated — 5 stocks on buy screener (ADANIENSOL, CGPOWER,
  JSWENERGY, ADANIPOWER, SWIGGY), only 1 on sell (COALINDIA). No SHORT trades fired
  despite COALINDIA being armed.
- **Live result**: 3 trades (of 4 max), 2W/1L, net **-₹158.43**, win rate **66.7%**
- **Trade breakdown**:
  - ADANIENSOL (LONG): BUY 31 @ ₹1,533.70 → SELL @ ₹1,541.00 | **+₹226.30** | 3h33m hold, best trade — patient hold rewarded
  - SWIGGY (LONG): BUY 368 @ ₹271.55 → SELL @ ₹270.10 | **-₹533.60** | 10-min hold, SL hit — late entry (14:58) on a fading move
  - ADANIPOWER (LONG): BUY 402 @ avg ₹248.68 → SELL @ ₹249.05 | **+₹148.87** | 3-min hold, quick scalp at 15:09 (just before no_new_entries_after cutoff)
- **Late entries underperformed**: Both SWIGGY (14:58) and ADANIPOWER (15:09) entered
  in the last ~15 minutes before the entry cutoff. SWIGGY was a clear loser; ADANIPOWER
  barely scraped a profit. ADANIENSOL, entered at 11:09, was the only meaningful winner.
- **No SHORT trades**: COALINDIA was armed for SELL but never triggered. All 3 trades
  were LONG. This is the first day with zero SHORT trades since SELL direction was enabled.
- **Tick logging active**: 122,158 ticks / 10.59 MB written, 0 drops
- **Armed at close**: BUY — ADANIENSOL, CGPOWER, JSWENERGY, ADANIPOWER, SWIGGY; SELL — COALINDIA
- **Symbols in cooldown at close**: SWIGGY, ADANIPOWER
- **Funds**: Available cash ₹22,392.70 (floor ₹20,000)
- **Errors**: Pre-login WebSocket 403s at 06:53–06:56 IST (benign, before Zerodha session).
  No trading-hour errors.

### May 29, 2026 (Day 3 — First Live Trading Day)
- **Market regime**: BUY-dominant; only BUY screener produced signals (GMRAIRPORT, NBCC). SELL screener empty.
- **Engine mode**: `live` (first real-money day). Config changed from sandbox: atr_sl_mult 1.5→1.2, max_trades 4→6.
- **Live result**: 3 trades, 1W/2L, net **-₹784.80**
- **Win rate**: 33.3%
- **Trade breakdown**:
  - GMRAIRPORT LONG: BUY 500@102.96 (11:23) → SELL 500@103.50 (11:34), **+₹270**, 11min hold. Quick winner, trailing stop locked profit.
  - NBCC LONG #1: BUY 500@102.13 (12:59) → SELL 500@101.10 (13:43), **-₹514.80**, 44min hold. Stopped out on pullback.
  - NBCC LONG #2 (re-entry): BUY 500@101.50 (14:34) → SELL 500@100.42 (15:03), **-₹540**, 29min hold. Re-entry also stopped out; NBCC fading all afternoon.
- **Notable observations**:
  - Re-entry on NBCC (Trade 3) repeated the Learning #7 pattern — re-entering after an exit on a fading stock lost more. Both NBCC trades were losers.
  - atr_sl_mult reverted to 1.2 (from 1.5) — the tighter stop may have contributed to the NBCC losses. Learning #1 warned that 1.2 produces more whipsaws.
  - NBCC was in cooldown at close, suggesting the engine correctly applied cooldown after Trade 2 before allowing Trade 3.
  - GMRAIRPORT remained armed at close with no re-entry — only 1 trade on the winner vs 2 on the loser.
- **Tick log**: 35,185 ticks written, 2.92 MB, 0 drops (final EOD).
- **Armed watches at close**: GMRAIRPORT (BUY), NBCC (BUY)
- **Funds**: ₹22,081 available (floor ₹20,000)
- **Errors**: 0 in last hour

### June 1, 2026 (Day 4 — Live, Monday)
- **Market regime**: Mixed. BUY screener produced signals (NBCC seen in scans). SELL screener quiet. Engine ran in live mode with atr_sl_mult=1.5.
- **Engine result**: **0 engine-managed trades**. Engine shows trades_today=0 at EOD — engine never subscribed to any symbols (tick log: 0 ticks, 0 bytes, 0 drops).
- **Broker tradebook** (non-engine fills, likely from signal_review or manual):
  - TCS LONG: BUY 42 @ ~2331.8 (12:09) → SELL 42 @ 2322.0 (13:24), **-₹411**, ~1h15m hold
  - NBCC LONG: BUY 500 @ 104.94 (14:38) → SELL 500 @ 104.43 (15:25), **-₹255**, ~47m hold
- **Net P&L (tradebook)**: **-₹666** (0W/2L, 0% win rate)
- **Notable observations**:
  - Engine had 0 trades despite being in live mode — scanned symbols didn't meet entry criteria (volume/breakout conditions), or scan cycles didn't successfully arm the engine.
  - TCS and NBCC trades came from outside the engine (signal_review_service or manual). Both losers.
  - NBCC continues its losing streak from Day 3 — now 3 consecutive losing trades across 2 days. Strong signal to avoid re-entering NBCC in current regime.
  - `signal_review_service` repeatedly failed to persist decisions due to missing `signal_decision` table (DB migration gap, known since last session).
  - `get_funds` API raised an error — engine failed open (continued without funds check).
  - atr_sl_mult confirmed at 1.5 (matching Learning #8).
- **Armed watches at close**: None (buy_symbols=[], sell_symbols=[])
- **Tick log**: 0 ticks written, 0 bytes, 0 drops
- **Errors**:
  - `signal_decision` table missing (DB migration needed) — recurring, many entries today
  - `get_funds` raised error in simplified_stock_engine_service — needs investigation
  - Telegram bot placeholder token errors (known config issue)

### July 1, 2026 (Day 5 — Sandbox; first logged entry since June 1)
- **Note on gap**: No daily entries were recorded between June 1 and today. Interim
  sessions are not captured here — this entry resumes the log and does not imply only
  one trading day passed.
- **Market regime**: Mixed, two-sided. BUY-side fills across broad F&O names
  (PHOENIXLTD, ETERNAL, COLPAL, PRESTIGE); SELL/SHORT side active on TATAELXSI,
  JINDALSTEL, KPITTECH. Armed watches at close were all SELL (HCLTECH, JINDALSTEL,
  KPITTECH), so the late-session bias was bearish.
- **Engine mode**: `sandbox`. Config: atr_sl_mult=1.5, max_trades_per_day=6,
  cooldown_candles=3. Both BUY and SELL directions enabled.
- **Engine self-report vs sandbox reality**: The engine reported
  `completed_trades_today=0`, `positions={}`, `trades_today=0` at EOD — yet the
  sandbox tradebook shows **18 fills / 9 round-trips**. This is the known
  engine-vs-sandbox reconciliation gap (positions squared off by sandbox MIS
  auto-square-off at ~15:14 that the engine never journaled). P&L below is
  reconstructed from the tradebook by pairing BUY/SELL fills per symbol, so
  entry/exit matching is approximate for symbols with multiple round-trips.
- **Reconstructed result**: **9 trades, 5W/4L (1 flat), net +₹368.8, win rate 56%**.
- **Trade breakdown** (reconstructed):
  - PHOENIXLTD (LONG): 2007.9 → 2017.5 × 49 | **+₹470.4** | best long, ~1h10m hold
  - ETERNAL (LONG): 278.2 → 279.95 × 359 | **+₹628.3** | largest winner (high qty)
  - COLPAL (LONG): 2070.7 → 2070.7 × 48 | **₹0** | flat, exited at entry price
  - PRESTIGE (LONG): 1624.3 → 1626.9 × 61 | **+₹158.6** | ~10m scalp near close
  - TATAELXSI (SHORT #1): 3591.5 → 3584.9 × 27 | **+₹178.2**
  - TATAELXSI (SHORT #2): 3630.8 → 3619.9 × 27 | **+₹294.3** | best short
  - JINDALSTEL (SHORT): 1025.7 → 1029.3 × 97 | **−₹349.2** | short went against, covered higher
  - KPITTECH (SHORT #1): 557.75 → 560.75 × 171 | **−₹513.0** | worst trade
  - KPITTECH (SHORT #2): 562.4 → 566.7 × 116 | **−₹498.8** | re-entry also lost
- **Notable observations**:
  - **SHORT results were split by symbol, not uniformly good** — TATAELXSI shorts both
    won (+₹472.5 combined) while JINDALSTEL and KPITTECH shorts all lost (−₹1,361.0
    combined). Contrasts with the strong SHORT-side runs of May 22/26; a persistent
    downtrend is required, and these names reversed up intraday.
    (Qualifies Learning #10 — SHORT edge is name-specific, not a blanket rule.)
    Notably these three (HCLTECH/JINDALSTEL/KPITTECH) remained SELL-armed at close,
    yet the KPITTECH/JINDALSTEL shorts had already lost — armed ≠ profitable.
  - **KPITTECH re-entry lost twice** (−₹513.0 then −₹498.8) — SHORT re-entry on a
    stock that keeps bouncing repeated the re-entry-risk pattern (Learning #7/#9).
    Reinforces that SHORT re-entry only tolerates *persistent* losers.
  - **LONG side carried the day** — all four longs were non-losers (+₹1,257.3
    combined incl. one flat); the two-sided book netted positive only because longs
    offset the JINDALSTEL/KPITTECH short losses.
- **Tick log**: 22,039 ticks written, ~1.78 MB (1,867,399 bytes), 0 drops.
- **Armed watches at close**: SELL — HCLTECH, JINDALSTEL, KPITTECH. BUY — none.
- **Symbols in cooldown at close**: none.
- **Errors**:
  - `broker.zerodha.api.data` "Unsupported timeframe: W" at 14:45 IST (2×) — a weekly
    history request the Zerodha adapter can't serve. Already logged to
    `audit/proposed_fixes.jsonl` by earlier sessions today; no trading impact.
  - `telegram_bot_service` RetryAfter "Flood control exceeded" at 14:00 (benign
    rate-limit; already logged earlier today).
  - `sector_follow` 15:18 smoke check: index coverage 8/10, missing NIFTYCONSRDURBL
    and NIFTYOILANDGAS. Above the 50% abort threshold (entries not held). Newly
    logged to `audit/proposed_fixes.jsonl` this cycle. Unrelated to the simplified
    engine but noted for the operator.

### July 8, 2026 (Sandbox; next logged session after July 1)
- **Market regime**: Strongly bearish/one-sided. Armed watches at close were
  **32 SELL vs 1 BUY** (33 active symbols) — the sell screener dominated the
  universe all day (AMBUJACEM, ANGELONE, ASHOKLEY, ASIANPAINT, BANKBARODA, BEL,
  BPCL, CONCOR, HINDPETRO, IEX, IOC, RECLTD, RVNL, SRF, UPL, and many more). Only
  KALYANKJIL was BUY-armed. This is the most one-sided (bearish) tape logged so far.
- **Engine mode**: `sandbox`. Config (live status): atr_sl_mult=1.5,
  max_trades_per_day=6, cooldown_candles=3. Both BUY and SELL enabled.
- **Engine self-report vs sandbox reality**: engine reported
  `completed_trades_today=4`, `positions={}` at 15:17 — but the sandbox tradebook
  shows **12 fills / 6 round-trips**. The 2 uncounted trades (CONCOR, UPL) both
  exited at **15:14:00** via sandbox MIS auto-square-off, which the engine's
  completed-trade counter doesn't include — the known engine-vs-sandbox
  reconciliation gap. P&L below is reconstructed by pairing BUY/SELL fills per
  symbol (all 12 fills paired cleanly; book flat at close).
- **Reconstructed result**: **6 trades, 5W/1L, net +₹5,246.35, win rate 83.3%**.
  Best day logged to date.
- **Trade breakdown** (reconstructed):
  - CONCOR (SHORT): 464.75 → 450.90 × 215 | **+₹2,977.75** | entry 12:05, exit 15:14 (EOD square-off), ~3h09m all-day runner — the standout
  - UPL (SHORT): 588.65 → 581.30 × 169 | **+₹1,242.15** | entry 13:28, exit 15:14 (EOD square-off), ~1h45m hold
  - IEX (SHORT): 119.62 → 117.88 × 500 | **+₹870.00** | entry 13:08, exit 13:49, ~40m hold
  - KALYANKJIL (LONG): 381.20 → 382.65 × 186 | **+₹269.70** | entry 13:19, exit 13:26, ~7m scalp — the only long
  - HINDPETRO (SHORT): 391.40 → 390.55 × 255 | **+₹216.75** | entry 10:59, exit 11:06, ~7m hold
  - BPCL (SHORT): 302.20 → 303.20 × 330 | **−₹330.00** | entry 14:15, exit 14:17 — only loser, 2-min stop-out on an immediate bounce
- **Notable observations**:
  - **SHORT side carried the day, consistent with a bearish tape.** 5 shorts netted
    **+₹4,976.65** (4W/1L); the lone long (KALYANKJIL) added +₹269.70. This is the
    directional-asymmetry pattern of Learning #10, now confirmed on a strongly
    bearish, sell-screener-dominated day.
  - **Learning #4 reaffirmed — the biggest winners were the all-day runners.** CONCOR
    (+₹2,977.75) and UPL (+₹1,242.15), the two positions held to the 15:14 EOD
    square-off, together produced **80.4%** of net P&L. The quick scalps (IEX,
    KALYANKJIL, HINDPETRO) captured modest gains; the strategy's edge was again in
    letting the trend-day shorts ride.
  - **The only loser was fast and cheap.** BPCL SHORT stopped out in 2 minutes for
    −₹330 — the ATR stop did its job (small, contained loss) on an entry that
    immediately reversed. No re-entry on it.
  - **No re-entries today** — 6 distinct symbols, one trade each. Cooldown/selection
    kept the book diversified (Learning #2).
- **Tick log**: **2,643,269 ticks / ~212.8 MB (223,176,201 bytes), 0 drops.** By far
  the highest tick volume logged (prior days were 22k–122k ticks) — the engine was
  subscribed to a large, active universe (33 armed names) on a high-volatility day.
  Queue never backed up (queued=0), so the async writer kept pace.
- **Armed watches at close**: SELL — 32 names (AMBUJACEM, ANGELONE, ASHOKLEY,
  ASIANPAINT, AUBANK, AUROPHARMA, BAJAJFINSV, BANKBARODA, BEL, BPCL, CANBK, CDSL,
  CONCOR, FORTIS, GODFRYPHLP, HINDPETRO, HYUNDAI, IEX, IOC, IREDA, KAYNES, LICHSGFIN,
  LTF, M&M, NAM-INDIA, RECLTD, RVNL, SONACOMS, SRF, TMPV, TVSMOTOR, UPL). BUY —
  KALYANKJIL.
- **Symbols in cooldown at close**: none reported.
- **Errors**: Only pre-market/early-session noise — `funds` margin-auth errors and
  `websocket` connect failures at 08:51–08:52 IST (before the Zerodha session was
  live), plus one transient `history_service` JSON-parse error at 09:16. **Zero
  trading-hour errors (09:30–15:17)** and no actionable issues — nothing proposed to
  `audit/proposed_fixes.jsonl` this cycle.

### July 10, 2026 (Sandbox)
- **Market regime**: Predominantly **bullish**, the mirror image of July 8. Armed
  watches at close were **30 BUY vs 1 SELL** (31 active symbols) — the buy screener
  dominated the universe (360ONE, BANDHANBNK, BANKINDIA, CANBK, CDSL, CONCOR, DLF,
  GODREJPROP, HDFCLIFE, ICICIPRULI, INDIANB, JIOFIN, KALYANKJIL, KFINTECH, KPITTECH,
  MPHASIS, NYKAA, PAYTM, PERSISTENT, PNB, RBLBANK, and more). AUROPHARMA was the lone
  SELL-armed name. But unlike July 8, the tape was **choppy, not trending** — long
  breakouts mostly faded.
- **Engine mode**: `sandbox`. Config (live status): atr_sl_mult=1.5,
  max_trades_per_day=6, cooldown_candles=3. Both BUY and SELL enabled.
- **Engine self-report vs sandbox reality**: engine reported
  `completed_trades_today=6`, `positions={}` at 15:17, matching the tradebook's
  **12 fills / 6 round-trips**. **No engine-vs-sandbox reconciliation gap today** —
  all 6 exited on the engine's own stops/targets (last exit 13:19:45, book flat well
  before the 15:14 EOD square-off), so nothing was left for sandbox MIS auto-square-off
  to close. Contrast July 8, where 2 all-day runners hit the EOD gap.
- **Result**: **6 trades, 2W/4L, net −₹1,465.37, win rate 33.3%.** All 6 trades were
  **LONG** — no shorts fired despite AUROPHARMA being armed SELL.
- **Trade breakdown** (all LONG, chronological by entry):
  - KALYANKJIL: 474.40 → 476.30 × 72 | **+₹136.80** | entry 10:48:54, exit 11:48:03, ~59m
  - DLF: 691.55 → 686.00 × 92 | **−₹510.60** | entry 10:48:54, exit 10:57:30, ~9m stop-out
  - CDSL: 1424.00 → 1427.20 × 62 | **+₹198.40** | entry 11:59:26, exit 12:14:37, ~15m
  - 360ONE: 1124.00 → 1120.10 × 88 | **−₹343.20** | entry 12:13:49, exit 12:57:26, ~44m
  - BANDHANBNK: 210.54 → 209.45 × 468 | **−₹510.12** | entry 12:18:49, exit 12:37:41, ~19m
  - CONCOR: 468.35 → 466.30 × 213 | **−₹436.65** | entry 12:38:51, exit 13:19:45, ~41m
- **Notable observations**:
  - **Signal direction matched the tape, but the edge did not.** July 8's bearish tape
    rewarded shorts (+₹5,246); today's bullish tape produced 6 correctly-directional
    longs that **still net −₹1,465**. The difference was regime *quality*: July 8 trended,
    today chopped — long breakouts triggered then reversed into the ATR stop. Direction
    alignment (Learning #10) is necessary but not sufficient; trend persistence is the
    real driver (Learning #4).
  - **Losers larger than winners — classic chop signature.** The two winners were small
    scalps (+₹136, +₹198); all four losers were −₹343 to −₹510, i.e. near the full ATR
    stop. Whipsaw days invert the payoff of a breakout strategy.
  - **Max-trades cap (6) was consumed by ~12:39 IST.** The last entry (CONCOR) filled at
    12:38:51 and the daily cap was hit — the engine took **no afternoon entries at all**.
    On a bullish day this is a double-edged constraint: it capped the morning bleed, but
    also meant zero participation if the tape had cleaned up and trended after ~13:00.
    Worth watching whether the cap is systematically spent on choppy morning signals.
  - **No re-entries today** — 6 distinct symbols, one trade each; diversified book
    (Learning #2).
- **Tick log**: **2,781,516 ticks / ~223.8 MB (234,697,575 bytes), 0 drops.** Slightly
  above July 8's 2.64M — another large, active universe (31 armed names) on a busy day.
  Queue never backed up (queued=0); async writer kept pace.
- **Armed watches at close**: BUY — 30 names (360ONE, BANDHANBNK, BANKINDIA, CANBK, CDSL,
  CONCOR, DLF, GODREJPROP, HDFCLIFE, ICICIPRULI, INDIANB, JIOFIN, KALYANKJIL, KFINTECH,
  KPITTECH, LTM, MOTILALOFS, MPHASIS, NYKAA, OBEROIRLTY, PAYTM, PERSISTENT, PHOENIXLTD,
  PNB, PREMIERENE, PRESTIGE, RBLBANK, TATAELXSI, TMPV, UNIONBANK). SELL — AUROPHARMA.
- **Symbols in cooldown at close**: BANDHANBNK, 360ONE, CONCOR.
- **Errors**: Bridge server (port 5001) was unreachable this cycle, so `read-errors`
  could not be queried — no error scan performed and nothing proposed to
  `audit/proposed_fixes.jsonl`. No trading-hour anomalies observed in the engine status
  or tradebook.

### July 13, 2026 (Sandbox, Monday)
- **Market regime**: Two-sided but **SELL-tilted and choppy**. Armed watches at
  close were **10 SELL vs 2 BUY** (12 active symbols) — sell screener dominated
  (ZYDUSLIFE, ICICIGI, DMART, BDL, JINDALSTEL, TATASTEEL, MUTHOOTFIN, GODREJCP,
  INDHOTEL, MANAPPURAM); BUY side was thin (LTM, VOLTAS). Unlike the strongly
  bearish, trending July 8 tape, today was **directionless** — most moves faded and
  no position could run.
- **Engine mode**: `sandbox`. Config (live status): atr_sl_mult=1.5,
  max_trades_per_day=6, cooldown_candles=3. Both BUY and SELL enabled.
- **Engine self-report vs sandbox reality**: engine reported
  `completed_trades_today=6`, `positions={}` at 15:17, matching the tradebook's
  **12 fills / 6 round-trips**. **No engine-vs-sandbox reconciliation gap today** —
  all 6 exited on the engine's own stops/targets (last exit ICICIGI 13:50:02), book
  flat well before the 15:14 EOD square-off, so nothing was left for sandbox MIS
  auto-square-off. Same clean-exit pattern as July 10 (contrast July 8's 2 EOD-runner
  gaps).
- **Result**: **6 trades, 4W/2L, net −₹257.00, win rate 66.7%.** Directional mix:
  **5 SHORT + 1 LONG**. The max-trades cap (6/6) was hit by the 12:23 entry.
- **Trade breakdown** (chronological by entry):
  - ZYDUSLIFE (SHORT): 1137.5 → 1135.7 × 87 | **+₹156.60** | entry 10:49:12, exit 11:08:17, ~19m
  - DMART (SHORT): 3986.9 → 3974.9 × 23 | **+₹276.00** | entry 11:14:24, exit 11:35:48, ~21m
  - BDL (SHORT): 1302.2 → 1308.2 × 76 | **−₹456.00** | entry 11:50:07, exit 12:38:37, ~48m — short covered higher
  - LTM (LONG): 4128.3 → 4142.5 × 16 | **+₹227.20** | entry 11:53:55, exit 12:05:42, ~12m scalp — the only long, a winner
  - ICICIGI (SHORT): 1773.6 → 1772.9 × 56 | **+₹39.20** | entry 12:20:18, exit 13:50:02, ~1h30m — longest hold, tiny edge captured
  - TATASTEEL (SHORT): 186.67 → 187.67 × 500 | **−₹500.00** | entry 12:23:48, exit 13:44:59, ~1h21m — worst trade, ground against the short
- **Notable observations**:
  - **No all-day runner = capped upside (Learning #4, inverted).** Every position was
    out by 13:50; nothing was held into the afternoon. On the profitable days (May 26
    CONCOR, July 8 CONCOR/UPL) the bulk of net P&L came from trend-day runners held to
    the EOD square-off. A choppy tape offered no such runner, so the best any trade did
    was +₹276 and the book netted slightly negative.
  - **Today the longer holds were the losers, not the winners.** The two losers (BDL
    −₹456 at ~48m, TATASTEEL −₹500 at ~1h21m) were the longer-duration shorts that
    ground against the position; the winners were mostly quick scalps (ZYDUSLIFE ~19m,
    DMART ~21m, LTM ~12m). This is the mirror of the strategy's usual edge — in a
    non-trending tape, holding longer just accumulated adverse drift rather than
    riding a move.
  - **SHORT results were name-split again (qualifies Learning #10).** 5 shorts went
    3W/2L but **−₹484.20 net across the short book** (the lone LONG, LTM +₹227.20,
    cut the total loss to −₹257.00); the sell-screener tilt did NOT translate into a
    short-side edge because the tape wasn't trending down. Consistent with July 1
    (TATAELXSI shorts won, JINDALSTEL/KPITTECH shorts lost) — SHORT edge needs a
    *persistent* downtrend, not merely a sell-screener-heavy universe.
  - **ICICIGI: winner in name only.** Held ~1h30m for +₹39.20 — the smallest gain of
    the day. A near-scratch that tied up a trade slot for 90 minutes; the max-trades
    cap makes such low-yield holds an opportunity cost.
  - **No re-entries** — 6 distinct symbols, one trade each. Cooldown/selection kept the
    book diversified (Learning #2).
- **Tick log**: **2,650,306 ticks / ~213.3 MB (223,638,880 bytes), 0 drops, queue 0.**
  Comparable to July 8/10 — a large, active universe on a busy session; async writer
  kept pace (no backlog).
- **Armed watches at close**: SELL — ZYDUSLIFE, ICICIGI, DMART, BDL, JINDALSTEL,
  TATASTEEL, MUTHOOTFIN, GODREJCP, INDHOTEL, MANAPPURAM (10). BUY — LTM, VOLTAS (2).
- **Symbols in cooldown at close**: BDL, TATASTEEL, ICICIGI.
- **Errors**: Bridge server (port 5001) unreachable this cycle, so `read-errors` could
  not be queried — no error scan performed and nothing proposed to
  `audit/proposed_fixes.jsonl`. Same bridge-down condition as July 10. No trading-hour
  anomalies observed in the engine status or tradebook.

---

### July 14, 2026 (Sandbox, Tuesday)
- **Market regime**: **SELL-tilted universe, but two-sided and choppy on execution.**
  Armed watches at close were **24 SELL vs 5 BUY** (29 active symbols) — the sell
  screener dominated the universe again (as on July 13), but the trades that actually
  fired were an even **3 SHORT + 3 LONG** split. No sustained trend either way; every
  position was out by 12:31, so nothing could run into the afternoon.
- **Engine mode**: `sandbox`. Config (live status): atr_sl_mult=1.5,
  max_trades_per_day=6, cooldown_candles=3. Both BUY and SELL enabled.
- **Engine self-report vs sandbox reality**: engine reported
  `completed_trades_today=6`, `positions={}` at 15:17, matching the tradebook's
  **12 fills / 6 round-trips**. **No engine-vs-sandbox reconciliation gap today** —
  all 6 exited on the engine's own stops/targets (last exit UNOMINDA 12:31:02), book
  flat by early afternoon, nothing left for the 15:14 EOD square-off. Same clean-exit
  pattern as July 10 / July 13.
- **Result**: **6 trades, 4W/2L, net −₹34.35, win rate 66.7%.** Directional mix:
  **3 SHORT + 3 LONG**. The max-trades cap (6/6) was hit by the 10:49 CONCOR entry —
  the whole day's action was compressed into 09:43–12:31.
- **Trade breakdown** (chronological by entry):
  - VMM (SHORT): 115.61 → 115.0 × 500 | **+₹305.00** | entry 09:43:51, exit 10:15:34, ~32m
  - UNOMINDA (SHORT): 1130.0 → 1125.5 × 72 | **+₹324.00** | entry 09:55:08, exit 12:31:02, ~2h36m — longest hold, best trade
  - BIOCON (LONG): 441.4 → 436.4 × 123 | **−₹615.00** | entry 10:14:09, exit 10:22:03, ~8m — quick stop-out, worst trade
  - WAAREEENER (SHORT): 2818.3 → 2829.5 × 35 | **−₹392.00** | entry 10:29:40, exit 10:59:55, ~30m — short covered higher
  - KALYANKJIL (LONG): 529.4 → 532.0 × 63 | **+₹163.80** | entry 10:34:20, exit 10:40:51, ~6.5m scalp
  - CONCOR (LONG): 485.5 → 487.15 × 109 | **+₹179.85** | entry 10:49:58, exit 10:53:49, ~4m scalp
- **Notable observations**:
  - **66.7% win rate, still net-negative — the same shape as July 13.** Four winners
    (VMM, UNOMINDA, KALYANKJIL, CONCOR) totalled +₹972.65 but the two losers (BIOCON
    −₹615, WAAREEENER −₹392 = −₹1,007) outran them. A choppy tape with no runner caps
    winners near +₹300 while stops still take the full −₹400/−₹600 hit — small
    unfavourable expectancy despite a healthy hit-rate.
  - **SHORT book beat the LONG book today (re-qualifies Learning #10).** 3 shorts went
    2W/1L for **+₹237.00 net** (VMM +305, UNOMINDA +324, WAAREEENER −392); 3 longs went
    2W/1L for **−₹271.35 net** (BIOCON −615, KALYANKJIL +164, CONCOR +180). This is the
    inverse of July 13, where the SHORT book was the drag. Read together: on a
    non-trending tape neither direction has a durable edge, and the sign of the net flips
    day-to-day on which side takes the single big stop-out (BIOCON long today, TATASTEEL
    short yesterday).
  - **The big loser was a fast stop, not a slow grind (contrast July 13).** BIOCON −615
    was an 8-minute stop-out; WAAREEENER −392 a 30-min cover. Unlike July 13 (losers were
    the longest holds), today's damage came from quick adverse breaks right after entry.
    The only long hold that paid was UNOMINDA (SHORT, 2h36m, +324) — the day's lone
    example of a position given room to work.
  - **No re-entries** — 6 distinct symbols, one trade each (Learning #2 holds).
  - **BIOCON, CONCOR, KALYANKJIL fired LONG while also armed on the BUY screener at
    close** — the buy-side names that did trade were the small scalp winners; the buy
    screener's other names (BLUESTARCO, OIL) never triggered a breakout.
- **Tick log**: **3,148,967 ticks / ~253.4 MB (265,732,812 bytes), 0 drops, queue 0.**
  The busiest session logged so far (vs July 13's 2.65M / 213 MB) — a 29-symbol
  subscribed universe on an active day; async writer kept pace (no backlog).
- **Armed watches at close**: SELL (24) — ASHOKLEY, AUBANK, BAJAJFINSV, BAJFINANCE,
  CAMS, COCHINSHIP, HDFCLIFE, HEROMOTOCO, HINDPETRO, IEX, IREDA, LICI, LT, M&M, MAZDOCK,
  PIIND, SAMMAANCAP, SBIN, SHRIRAMFIN, TVSMOTOR, UNOMINDA, VBL, VMM, WAAREEENER. BUY (5)
  — BIOCON, BLUESTARCO, CONCOR, KALYANKJIL, OIL.
- **Symbols in cooldown at close**: KALYANKJIL, CONCOR, WAAREEENER, UNOMINDA.
- **Errors**: Bridge server (port 5001) unreachable this cycle, so `read-errors` could
  not be queried — no error scan performed and nothing proposed to
  `audit/proposed_fixes.jsonl`. Same bridge-down condition as July 10 / July 13. No
  trading-hour anomalies observed in the engine status or tradebook.

### July 15, 2026 (Sandbox, Wednesday)
- **Market regime**: **BUY-tilted universe — the inverse of July 13/14.** Armed
  watches at close were **9 BUY vs 2 SELL** (11 active symbols). Yet unlike the two
  prior choppy sell-tilted days, today produced a real intraday hold (TATAELXSI SHORT,
  ~3h) and the first clearly net-positive session in this July window since July 8.
- **Engine mode**: `sandbox`. Config (live status): atr_sl_mult=1.5,
  max_trades_per_day=6, cooldown_candles=3. Both BUY and SELL enabled.
- **Engine self-report vs sandbox reality**: engine reported
  `completed_trades_today=5`, `positions={}` at 15:17, matching the tradebook's
  **10 fills / 5 round-trips**. **No engine-vs-sandbox reconciliation gap** — all 5
  exited on the engine's own stops/targets (last exit MANKIND 14:02:20), book flat by
  early afternoon, nothing left for the 15:14 EOD square-off. Same clean-exit pattern
  as July 10 / July 13 / July 14. The 6-trade cap was **not** hit (5 of 6).
- **Result**: **5 trades, 4W/1L, net +₹755.95, win rate 80%.** Directional mix:
  **2 SHORT + 3 LONG**.
- **Trade breakdown** (chronological by entry):
  - TATAELXSI (SHORT): 3517.3 → 3497.7 × 17 | **+₹333.20** | entry 09:59:29, exit 13:05:51, ~3h06m — longest hold, the day's anchor
  - PATANJALI (SHORT): 376.3 → 372.75 × 95 | **+₹337.25** | entry 10:15:01, exit 10:24:00, ~9m — quick winning cover, best trade
  - KALYANKJIL (LONG): 539.25 → 542.4 × 109 | **+₹343.35** | entry 11:14:38, exit 11:57:47, ~43m
  - SBICARD (LONG): 651.25 → 651.8 × 153 | **+₹84.15** | entry 11:54:59, exit 12:05:27, ~10.5m scalp
  - MANKIND (LONG): 2584.5 → 2575.5 × 38 | **−₹342.00** | entry 13:39:17, exit 14:02:20, ~23m — only loser, stopped out
- **Notable observations**:
  - **A runner returned, and the day went net-positive (re-confirms Learning #4).**
    July 13/14 had zero afternoon holds and both netted negative despite ~66% hit
    rates; today the lone multi-hour position (TATAELXSI SHORT, ~3h06m) held while the
    rest scalped, and the session cleared +₹755.95 at an 80% hit rate. Trend
    persistence — not hit-rate — again separates the good day from the choppy ones.
  - **SHORT book won again on a BUY-tilted universe (re-qualifies Learning #10).**
    2 shorts went **2W/0L for +₹670.45**; 3 longs went **2W/1L for +₹85.50** (KALYANKJIL
    +343.35, SBICARD +84.15, MANKIND −342). The shorts fired from just 2 SELL-screener
    names in an otherwise BUY-dominated universe and delivered 89% of net P&L. This is
    the mirror of the July 13/14 finding: universe tilt does not predict which book
    wins — the SHORT names simply had cleaner follow-through today.
  - **No re-entries** — 5 distinct symbols, one trade each (Learning #2 holds).
  - **KALYANKJIL and SBICARD fired LONG while armed on the BUY screener at close**; the
    other BUY names (BANDHANBNK, BHEL, DALBHARAT, HYUNDAI, IOC, UNIONBANK, MANKIND
    after exit) either scalped once or never broke out.
- **Tick log**: **2,844,269 ticks / ~228.97 MB (240,083,388 bytes), 0 drops, queue 0.**
  Slightly below July 14's 3.15M / 253 MB — a smaller 11-symbol subscribed universe;
  async writer kept pace (no backlog).
- **Armed watches at close**: BUY (9) — BANDHANBNK, BHEL, DALBHARAT, HYUNDAI, IOC,
  KALYANKJIL, MANKIND, SBICARD, UNIONBANK. SELL (2) — PATANJALI, TATAELXSI.
- **Symbols in cooldown at close**: none.
- **Errors**: Bridge server (port 5001) unreachable this cycle, so `read-errors` could
  not be queried — no error scan performed and nothing proposed to
  `audit/proposed_fixes.jsonl`. Same bridge-down condition as July 10 / July 13 / July
  14. No trading-hour anomalies observed in the engine status or tradebook.

### July 16, 2026 (Sandbox, Thursday)
- **Market regime**: **Mixed, two-sided.** Armed watches at close were **8 BUY vs 5
  SELL** (13 active symbols) — a genuinely balanced universe (unlike the sell-tilted
  July 13/14 or buy-tilted July 15). Both directions traded (2 SHORT + 4 LONG). The
  tape was **choppy with no sustained trend** — the longest hold (HDFCAMC SHORT) ran
  ~1h20m for a modest +₹262; nothing became an all-day runner.
- **Engine mode**: `sandbox`. Config (live status): atr_sl_mult=1.5,
  max_trades_per_day=6, cooldown_candles=3. Both BUY and SELL enabled.
- **Engine self-report vs sandbox reality**: engine reported
  `completed_trades_today=6`, `positions={}` at 15:17, matching the tradebook's
  **12 fills / 6 round-trips**. **No engine-vs-sandbox reconciliation gap** — all 6
  exited on the engine's own stops/targets (last exit HDFCAMC 14:15:06), book flat by
  early afternoon, nothing left for the 15:14 EOD square-off. Same clean-exit pattern
  as July 10 / 13 / 14 / 15.
- **Result**: **6 trades, 4W/2L, net +₹122.88, win rate 66.7%.** Directional mix:
  **2 SHORT + 4 LONG**. The max-trades cap (6/6) was hit by the 12:54 HDFCAMC entry —
  all action compressed into 09:34–14:15, no afternoon entries.
- **Trade breakdown** (chronological by entry):
  - ICICIGI (SHORT): 1587.20 → 1571.20 × 24 | **+₹384.00** | entry 09:34:41, exit 09:35:52, ~1m — fastest trade, best P&L, immediate cover on a sharp drop
  - SWIGGY (LONG): 282.95 → 280.18 × 221 | **−₹612.17** | entry 10:59:47, exit 11:15:21, ~16m — worst trade, stop-out on a fade
  - SIEMENS (LONG): 3676.70 → 3704.40 × 8 | **+₹221.60** | entry 11:28:48, exit 11:50:23, ~22m
  - SRF (LONG): 2864.00 → 2873.00 × 34 | **+₹306.00** | entry 12:13:53, exit 12:57:46, ~44m
  - BIOCON (LONG): 443.20 → 441.25 × 225 | **−₹438.75** | entry 12:34:40, exit 13:31:32, ~57m — second loser, stop-out
  - HDFCAMC (SHORT): 2602.50 → 2595.60 × 38 | **+₹262.20** | entry 12:54:34, exit 14:15:06, ~1h20m — longest hold
- **Notable observations**:
  - **SHORT book won again — the fourth straight July session where it did.** 2 shorts
    went **2W/0L for +₹646.20** (ICICIGI +384, HDFCAMC +262.20); 4 longs went **2W/2L
    for −₹523.32** (SIEMENS +221.60, SRF +306, SWIGGY −612.17, BIOCON −438.75). The two
    shorts fired from the 5-name SELL-screener minority on a balanced-to-buy universe —
    re-confirming (Learning #10) that universe tilt does not predict which book wins;
    the SHORT names simply had cleaner follow-through. Rolling July SHORT-book net:
    July 14 +₹237, July 15 +₹670.45, July 16 +₹646.20.
  - **66.7% hit-rate, barely positive — the chop signature persists (Learning #4).**
    Like July 13/14, there was no runner, so winners capped modest (best non-scalp
    +₹306) while the two long losers took near-full ATR stops (−₹612, −₹438). Unlike
    July 13/14 the day eked *positive* only because both shorts won cleanly and the
    ICICIGI 1-minute scalp banked +₹384 — the sign flipped positive on which side
    avoided the big stop-out, exactly the day-to-day coin-flip noted July 14.
  - **The winners were quick, the big loser was quick too.** ICICIGI (+384, ~1m) and
    the modest scalps carried; SWIGGY (−612, ~16m) broke down fast right after entry.
    The only meaningful hold, HDFCAMC SHORT (~1h20m), earned a middling +₹262 — no
    position was given room to compound into a trend-day runner.
  - **No re-entries** — 6 distinct symbols, one trade each (Learning #2 holds).
- **Tick log**: **2,896,330 ticks / ~232.8 MB (244,128,701 bytes), 0 drops, queue 0.**
  The busiest session logged to date (edging July 14's 3.15M was close; this is 2.90M
  on a 13-name armed universe but heavy per-symbol tick flow); async writer kept pace
  (no backlog).
- **Armed watches at close**: BUY (8) — BHEL, BIOCON, HCLTECH, IEX, KAYNES, SIEMENS,
  SRF, SWIGGY. SELL (5) — CAMS, HDFCAMC, ICICIGI, PHOENIXLTD, TVSMOTOR.
- **Symbols in cooldown at close**: SRF, BIOCON, HDFCAMC.
- **Errors**: Bridge server (port 5001) unreachable this cycle, so `read-errors` could
  not be queried — no error scan performed and nothing proposed to
  `audit/proposed_fixes.jsonl`. Same bridge-down condition as July 10 / 13 / 14 / 15.
  No trading-hour anomalies observed in the engine status or tradebook.

### July 17, 2026 (Sandbox, Friday)
- **Market regime**: **Balanced-to-buy, but LONG-heavy execution.** Armed watches at
  close were **8 BUY vs 7 SELL** (15 active symbols) — a near-balanced universe, mildly
  buy-tilted. Execution skewed strongly LONG (**5 LONG + 1 SHORT**). The tape was
  **choppy with no sustained trend** — no position became an all-day runner; the longest
  holds (ZYDUSLIFE SHORT ~1h, 360ONE LONG ~51m, BAJFINANCE LONG ~47m) all closed modest
  or at a stop. Same chop signature as July 13/14/16.
- **Engine mode**: `sandbox`. Config (live status): atr_sl_mult=1.5,
  max_trades_per_day=6, cooldown_candles=3. Both BUY and SELL enabled.
- **Engine self-report vs sandbox reality**: engine reported
  `completed_trades_today=5`, `positions={}` at 15:17, but the tradebook shows **12
  fills / 6 round-trips** — a **1-trade reconciliation gap**. The most likely uncounted
  round-trip is **POLICYBZR** (LONG, entry 13:40:04 → exit 13:55:02): it is the only
  traded symbol **not present in the armed-watch list at close**, suggesting it was
  armed/exited earlier and dropped from the engine's live watch (and possibly its
  counter) while still landing both fills in sandbox.db. Book was **flat by 13:55**
  (last exit POLICYBZR 13:55:02), nothing left for the 15:14 EOD square-off.
- **Result**: **6 trades, 2W/4L, net −₹1,328.20, win rate 33.3%.** Directional mix:
  **5 LONG + 1 SHORT**. All action compressed into **10:55–13:55** — no early-morning
  and no late-afternoon entries; the max-trades cap (6) was reached mid-afternoon.
- **Trade breakdown** (chronological by entry):
  - 360ONE (LONG): 1115.80 → 1106.60 × 56 | **−₹515.20** | entry 10:55:04, exit 11:46:10, ~51m — stop-out on a fade, second-worst
  - TECHM (LONG): 1536.00 → 1540.00 × 57 | **+₹228.00** | entry 11:24:07, exit 11:39:06, ~15m — quick winner
  - SONACOMS (LONG): 705.35 → 701.50 × 141 | **−₹542.90** | entry 11:53:54, exit 12:02:49, ~9m — worst trade, fast stop-out right after entry
  - ZYDUSLIFE (SHORT): 1137.70 → 1140.40 × 87 | **−₹234.90** | entry 12:29:37, exit 13:31:19, ~1h — lone short, ground out a loss on a drift-up
  - BAJFINANCE (LONG): 1054.80 → 1051.40 × 94 | **−₹319.60** | entry 12:53:49, exit 13:40:57, ~47m — stop-out
  - POLICYBZR (LONG): 1580.50 → 1581.10 × 94 | **+₹56.40** | entry 13:40:04, exit 13:55:02, ~15m — scalp; the likely engine-uncounted round-trip
- **Notable observations**:
  - **The SHORT-book winning streak broke — but on n=1.** After four straight July
    sessions where the SHORT book won (July 13–16), today's single SHORT (ZYDUSLIFE)
    **lost −₹234.90 (0W/1L)** while the 5 LONGs netted **−₹1,093.30 (2W/3L)**. Both
    books negative, so nothing was "won" — but the durable point is the sample: with
    only one short taken, this is not evidence against the short-side follow-through
    thesis, just a LONG-heavy day where longs dominated the book and dragged it down.
  - **33.3% hit-rate, clearly negative — chop signature (Learning #4).** No runner:
    the two winners were quick scalps (TECHM +228 ~15m, POLICYBZR +56 ~15m) capped
    small, while three of four losers took near-full ATR stops on fades (SONACOMS −543
    ~9m, 360ONE −515 ~51m, BAJFINANCE −320). Winners quick and small, losers paying the
    full stop — the exact P&L asymmetry that makes hit-rate a poor proxy in a
    range-bound tape.
  - **LONG-heavy execution on a balanced universe underperformed.** 5 of 6 trades were
    LONG despite a near-even 8/7 armed split; the longs went 2W/3L and carried the day's
    loss. Re-confirms (Learning #10) that universe tilt / which side you trade does not
    itself predict P&L — trend persistence does, and there was none.
  - **No re-entries** — 6 distinct symbols, one trade each (Learning #2 holds). Cooldown
    list empty at close.
- **Tick log**: **3,174,133 ticks / ~255.0 MB (267,384,005 bytes), 0 drops, queue 0.**
  **Busiest session logged to date** (edges past July 14's ~3.15M and July 16's ~2.90M);
  async writer kept pace (queue 0, no backlog) on a 15-name armed universe.
- **Armed watches at close**: BUY (8) — 360ONE, AXISBANK, BAJFINANCE, HCLTECH, JIOFIN,
  SONACOMS, TCS, TECHM. SELL (7) — BLUESTARCO, HINDALCO, JUBLFOOD, KEI, NATIONALUM,
  WIPRO, ZYDUSLIFE.
- **Symbols in cooldown at close**: none.
- **Errors**: Bridge server (port 5001) unreachable this cycle, so `read-errors` could
  not be queried — no error scan performed and nothing proposed to
  `audit/proposed_fixes.jsonl`. Same bridge-down condition as July 10 / 13 / 14 / 15 /
  16. One data note (not an error): the engine's `completed_trades_today` (5) undercounts
  the sandbox tradebook (6 round-trips) — worth watching whether the counter reliably
  misses symbols dropped from the armed watch mid-session.

### July 20, 2026 (Sandbox, Monday)
- **Market regime**: **Financials-dominated, near-balanced universe, chop again.** Armed
  watches at close were **7 BUY vs 8 SELL** (15 active symbols), and the universe was
  overwhelmingly **banking/NBFC** — BUY: PNB, UNIONBANK, CANBK, BANKBARODA, MANAPPURAM
  (+OIL, NTPC); SELL: AXISBANK, HDFCBANK, KOTAKBANK, RBLBANK, LTF (+VOLTAS, WAAREEENER,
  DELHIVERY). A PSU-bank-long / private-bank-short split. Tape was **range-bound with no
  follow-through** — every round-trip closed inside ~4–35 minutes with a small P&L; no
  runner. Fifth consecutive chop session (July 13/14/16/17/20).
- **Engine mode**: `sandbox`. Config (live status): atr_sl_mult=1.5,
  max_trades_per_day=6, cooldown_candles=3. Both BUY and SELL enabled.
- **Engine self-report vs sandbox reality**: engine reported
  `completed_trades_today=5`, `positions={}` at 15:17; tradebook shows **12 fills / 6
  round-trips** — a **1-trade gap, the second session running** (same signature as July
  17). The uncounted round-trip is **YESBANK** (SHORT, 15:00:01 → 15:15:00), the only
  traded symbol absent from the armed-watch list at close. Caveat: YESBANK's notional
  (₹150k, 6553 sh) is ~1.5–2.7× every other position today (₹55k–₹100k), so it may not
  be a simplified-engine fill at all — it could belong to another sandbox strategy. Not
  resolvable from the tradebook alone; both readings are recorded below.
- **Result (5 engine-attributed trades)**: **3W/2L, net −₹608.05, win rate 60.0%.**
  Including the ambiguous YESBANK short: **6 trades, 3W/3L, net −₹804.64, 50.0%.**
  Directional mix (engine 5): **2 LONG + 3 SHORT.** Action ran 10:04–13:32, then flat
  for ~1h40m until the (possibly foreign) 15:00 YESBANK short. Max-trades cap (6) not
  reached by the engine's own count.
- **Trade breakdown** (chronological by entry):
  - VOLTAS (SHORT): 1338.70 → 1347.10 × 67 | **−₹562.80** | entry 10:04:30, exit 10:49:40, ~45m — worst trade, stopped out on a lift
  - PNB (LONG): 110.72 → 109.61 × 500 | **−₹555.00** | entry 10:23:49, exit 10:38:33, ~15m — fast stop-out, near-identical damage to VOLTAS
  - AXISBANK (SHORT): 1259.40 → 1257.10 × 79 | **+₹181.70** | entry 11:19:52, exit 11:40:56, ~21m — best trade
  - LTF (SHORT): 303.75 → 303.30 × 329 | **+₹148.05** | entry 12:23:58, exit 12:41:33, ~18m — scalp
  - CANBK (LONG): 128.09 → 128.45 × 500 | **+₹180.00** | entry 13:28:51, exit 13:32:35, **~4m** — shortest hold in the log to date
  - *(ambiguous)* YESBANK (SHORT): 22.91 → 22.94 × 6553 | **−₹196.59** | entry 15:00:01, exit **15:15:00** — closed exactly at the sandbox MIS auto-square-off, i.e. held to the bell rather than exited by the engine
- **Notable observations**:
  - **60% hit-rate, still negative — the clearest P&L-asymmetry day yet.** Three winners
    averaged **+₹169.92**; two losers averaged **−₹558.90** — losers were **3.3× the size
    of winners**. Winning 3 of 5 and still losing ₹608 is Learning #4's chop signature in
    its purest form: winners get clipped at scalp size, losers pay a near-full 1.5×ATR
    stop. Hit-rate remains a poor proxy for edge here.
  - **The SHORT book won again — 2W/1L, +₹−232.95 net incl. the −₹562.80 VOLTAS stop**,
    i.e. shorts were 2/3 and produced both of the day's top two winners (AXISBANK, LTF).
    LONGs went 1W/1L (−₹375.00). Across July 13–20 the short side has now outperformed
    the long side in **five of six** sessions (July 17 the lone exception, n=1). This is
    accumulating into real evidence for the short-side follow-through thesis rather than
    noise — worth a dedicated backtest split by side.
  - **Private-bank SHORT / PSU-bank LONG was the day's structural bet, and the shorts
    were right.** AXISBANK (private, SHORT) and LTF (NBFC, SHORT) both paid; PNB (PSU,
    LONG) took the second-largest loss while CANBK (PSU, LONG) only scratched +₹180 in
    4 minutes. Small sample (n=4 financials) — flagged, not concluded.
  - **Holds are getting shorter.** Mean engine hold today ~**20.6 minutes** (4m–45m),
    versus ~31m on July 17. Nothing was given room; consistent with a tape that offers no
    trend to trail into.
  - **No re-entries** — 5 (or 6) distinct symbols, one trade each (Learning #2 holds).
    Cooldown list **empty at close**, which means the 3-candle cooldown never even bound
    today.
- **Tick log**: **3,043,777 ticks / ~244.6 MB (256,527,768 bytes), 0 drops, queue 0.**
  Third-busiest session logged (behind July 17's 3.17M and July 14's ~3.15M); async
  writer kept pace with no backlog on a 15-name armed universe. (Final post-close read at
  15:32: **3,132,814 ticks / 264,062,283 bytes, 0 drops** — the 15:17 figure above was
  mid-flush.)
- **Armed watches at close**: BUY (7) — BANKBARODA, CANBK, MANAPPURAM, NTPC, OIL, PNB,
  UNIONBANK. SELL (8) — AXISBANK, DELHIVERY, HDFCBANK, KOTAKBANK, LTF, RBLBANK, VOLTAS,
  WAAREEENER.
- **Symbols in cooldown at close**: none.
- **Errors**: Bridge server (port 5001) unreachable this cycle (`/read-errors` returned a
  browser error page). **Same bridge-down condition as July 10 / 13 / 14 / 15 /
  16 / 17 — seven consecutive sessions.** This is no longer transient; the bridge should
  be treated as effectively offline until an operator restarts it.
- **Errors (amended 15:40 by the post-close cycle)**: the bridge being down does **not**
  mean the error scan is impossible — `log/errors.jsonl` was read directly and shows
  **121 errors today**, all clustered in the pre-market boot window (~08:23–08:29) and
  none during trading hours. Breakdown: 36× `Could not find instrument token`
  (`broker/zerodha/api/data.py:457`, whole scanner/sector_follow universe — master
  contract not yet loaded when the boot convergence check ran), 28× Zerodha margin/auth
  failures (pre-re-login token expiry, expected), 2× `Failed to initialize Open15
  breakout service: cannot pickle '_thread.lock' object` (the #425 service never started
  today), 3× Telegram broadcast eventlet `RuntimeError`, plus WebSocket 403 handshake
  failures at 08:23 (pre-re-login). **Nothing new proposed to
  `audit/proposed_fixes.jsonl`** — the instrument-token cluster and the Open15 init
  failure were both already logged today (09:36 and 13:22 entries); re-proposing would
  be duplicate noise. The file now carries 6 proposals for 2026-07-20.
  **Correction to the standing assumption**: prior sessions recorded "bridge down → no
  error scan performed." That is a tooling gap, not a data gap — the log is readable
  without the bridge, and the week of "dead error-scanning" was recoverable all along.
- **Amendment (15:47 cycle) — YESBANK ambiguity RESOLVED, it is NOT ours.** The earlier
  entries above could not attribute the YESBANK round-trip from the tradebook alone.
  `trade_journal` settles it: exactly **5 rows** carry `date(placed_at)='2026-07-20'`,
  all `strategy_name='trending_equity_intraday'` — VOLTAS, PNB, AXISBANK, LTF, CANBK.
  **YESBANK has no journal row**, so it belongs to another sandbox strategy and must be
  excluded. The engine's `completed_trades_today=5` was **correct all along** — there is
  **no 1-trade gap**, and by extension the identical "gap" flagged on July 17 should be
  re-checked against `trade_journal` before it is treated as a real discrepancy.
  **Canonical result for today: 5 trades, 3W/2L, net −₹608.05, 60.0% win rate.** The
  6-trade / −₹804.64 / 50.0% reading is retired. **Method note for future cycles: the
  tradebook is a shared surface across all sandbox strategies — attribute via
  `trade_journal.strategy_name`, never by pairing tradebook fills.**
- **Amendment — every exit today was `exit_reason='stop_loss'`, winners included.** All
  5 journal rows exit on the stop, i.e. the three winners (AXISBANK, LTF, CANBK) were
  closed by the **RR trailing stop ratcheting into profit**, not by a target. Combined
  with the P&L asymmetry noted above (winners avg +₹169.92 vs losers avg −₹558.90), the
  mechanism is now explicit: in chop the trail locks in a scalp the moment price ticks
  favourably, while losers still pay the full 1.5×ATR initial stop. **Not one trade all
  day exited on a target.** This is the sharpest evidence yet that the exit logic — not
  entry selection — is what caps the upside on chop days, and it argues for testing a
  slower/wider trail activation rather than more entry filtering.
- **Errors (amended 15:47)**: the Telegram broadcast failures grew from 3 to **4** —
  15:20:03, 15:25:03, 15:30:01, 15:30:07, all `RuntimeError('Event loop is closed')` to
  chat 1345069591. These land on the **15:20 entry / 15:25 exit / 15:30 EOD-summary**
  alert path, so **today's operator EOD alerts were not delivered**. This is a
  **fourth recurrence** of the class already proposed on 2026-07-01, 07-03 and 07-15;
  logged again to `audit/proposed_fixes.jsonl` because the failure is now hitting every
  EOD alert slot rather than one-off. No errors during trading hours otherwise.

---

## Key Learnings

### 1. ATR Multiplier is the Most Impactful Parameter
- **1.2× (old default)**: Tight stops. Catches quick reversals but whipsawed out of
  many trades that later recovered. Produced 33% win rate on May 21 backtest.
- **1.5× (current live)**: Wider stops. Survives normal pullbacks within a trending
  candle. Produced 83% win rate on May 21 live.
- **Observation**: The 0.3 difference in ATR multiplier flipped 3 trades from loss to
  profit on the same day. This is the single most sensitive parameter.
- **TODO**: Test 1.3, 1.4, 1.6, 1.8 across a week of data to find the sweet spot.

### 2. Cooldown Prevents Over-Trading on Whipsaw Stocks
- Without cooldown (backtest): SAMMAANCAP was traded 3 times, burning 50% of the
  trade budget on one choppy stock.
- With 3-candle cooldown (live): Only 1 SAMMAANCAP trade, allowing budget for
  ANGELONE and ADANIENSOL entries.
- **Observation**: Cooldown improves diversification across stocks.

### 3. Max Trades Per Day: 4 vs 6
- 6 trades (backtest): Filled budget by early afternoon, including re-entries on losers.
- 4 trades (live): More selective. Forced the engine to skip marginal setups.
- **Observation**: Fewer max trades → higher quality entries, but may miss late-day
  opportunities. Need more data.

### 4. EOD Trades Can Be the Biggest Winners
- On strong trend days (May 20), stocks that ran all day (POWERINDIA, SIEMENS, ABB)
  generated 90%+ of P&L through EOD exit at 15:20.
- Trailing stop trades exited with modest 1-3 point gains per share.
- **Implication**: Don't optimize for quick exits. The strategy's edge may be in
  catching all-day runners. Consider widening trailing stop or delaying its activation.

### 5. Volume Filter is Effective
- Prevented entries on HINDALCO, HINDPETRO (May 20), SAMMAANCAP partial (May 21)
  where breakouts weren't confirmed by volume.
- 2.5× multiplier seems about right — not so high that it misses real breakouts,
  not so low that it lets through noise.

### 6. Chartink Screener Timing Matters
- Stock universe shifts as the market moves intraday.
- Early scans (9:30-9:45) catch opening momentum stocks.
- Later scans may find different stocks that only crossed 3% threshold later.
- **Implication**: Multiple scan cycles improve coverage. The `fno-scan-cycle`
  scheduled task (every 15 min) handles this well.

### 7. Re-Entry Risk
- ANGELONE 2nd entry on May 21 was the only loser (-₹528.20).
- Re-entering a stock after it was already exited carries higher risk — the first
  exit often signals the trend is weakening.
- Cooldown helps but doesn't fully prevent re-entry on the same stock.
- **TODO**: Consider a per-symbol daily trade limit (max 1 or 2 entries per symbol).

### 9. SHORT Re-Entry Can Work (Unlike LONG Re-Entry)
- May 21: ANGELONE LONG re-entry was the only loser (-₹528.20) — re-entering a
  weakening uptrend was risky.
- May 22: GLENMARK SHORT re-entry worked — both entries were winners (+₹188.80,
  +₹387.60). A stock that keeps falling often has sustained selling pressure.
- **Observation**: Re-entry risk may be directional. Shorts on persistent losers
  may tolerate re-entry better than longs on fading momentum stocks. Sample size
  is tiny (n=2) — need more data before making a rule.

### 10. SHORT Trades Are Consistently Profitable (Early Signal)
- May 22: GLENMARK SHORT — 2 trades, 2W, +₹576.40
- May 26: CONCOR SHORT +₹1,010.60, RVNL SHORT +₹170.55 — 2 trades, 2W, +₹1,181.15
- May 27: No SHORT trades (COALINDIA armed but never triggered)
- **Cumulative SHORT record**: 4 trades, 4W/0L, +₹1,757.55 (100% win rate)
- **Cumulative LONG record** (May 22–27): DIXON -₹518, VBL +₹306.90, PREMIERENE -₹510,
  ADANIPOWER(26) -₹507, ADANIENSOL(27) +₹226.30, SWIGGY -₹533.60, ADANIPOWER(27) +₹148.87
  = 7 trades, 3W/4L, -₹1,386.53 (43% win rate)
- **Observation**: Small sample (n=11 total), but the directional asymmetry persists.
  SHORT entries from the sell screener may have a stronger edge because stocks falling
  >3% intraday often have sustained selling pressure (institutional unwinding, stop
  cascades), while stocks rising >3% may face profit-taking resistance.
- **Caution**: This could be regime-dependent — a strong bull market may flip the pattern.
  Continue tracking per-direction stats before adjusting max_trades allocation.
- **Update (July 10, 2026)**: The July 8 (bearish) vs July 10 (bullish) pair sharpens the
  caveat. On both days the dominant screener matched the tape direction, but the outcomes
  diverged entirely: July 8 shorts on a *trending* bearish tape netted +₹5,246 (5W/1L);
  July 10 longs on a *choppy* bullish tape netted −₹1,465 (2W/4L), with all four losers
  near the full ATR stop. **Direction alignment is necessary but not sufficient — trend
  persistence (Learning #4), not direction, is the real edge.** The short-side asymmetry
  may partly be that bearish days in this window happened to trend; don't over-attribute
  to SHORT vs LONG until we see a *trending* bullish day.
- **Update (July 14, 2026)**: The July 13/14 pair confirms the "necessary but not
  sufficient" reading and adds a second-order point: **on a choppy tape the sign of the
  short-vs-long book flips day-to-day.** July 13 SHORT book was net −₹484 (LONG saved
  the day); July 14 SHORT book was net +₹237 (LONG dragged, BIOCON −₹615). Both days a
  SELL-tilted universe, both directionless — neither side held an edge, and which book
  ended positive was decided by whichever direction took the single big stop-out. **A
  sell-screener-heavy universe is not itself a short signal** (re-confirms July 1 / July
  13). Also note both days posted a **66.7% win rate yet netted negative** — in chop the
  winners cap near +₹300 (no runner, Learning #4) while stops still pay the full
  −₹400/−₹600, so hit-rate is a poor P&L proxy without trend persistence.
- **Update (July 15, 2026)**: A clean counter-example to the two prior choppy days.
  Universe was **BUY-tilted (9 BUY / 2 SELL)** — the inverse of July 13/14 — yet the
  **SHORT book again won**: 2 shorts 2W/0L +₹670.45 vs 3 longs 2W/1L +₹85.50, net
  **+₹755.95 at 80% hit-rate**. Two reinforcing points: (1) universe tilt still does
  not predict which book wins (short names won on a buy-heavy day, mirror of July
  13/14); (2) the difference from the choppy days was **a runner** — the lone ~3h hold
  (TATAELXSI SHORT) anchored the day while the rest scalped. When a position is given
  room and the tape cooperates, the 80% hit-rate actually converts to P&L, unlike the
  66%-but-negative July 13/14 sessions. Cumulative read stands: SHORT names have shown
  cleaner follow-through across this July window, but the durable edge is trend
  persistence (Learning #4), not direction.
- **Update (July 16, 2026)**: Fourth straight July session where the SHORT book won —
  and this time on a genuinely **balanced universe (8 BUY / 5 SELL)**, not a tilt in
  either direction. Shorts went 2W/0L +₹646.20 (ICICIGI, HDFCAMC) while the 4 longs
  netted −₹523.32; the day cleared +₹122.88 at 66.7% hit-rate. Rolling SHORT-book net
  over the choppy July window: July 14 +₹237, July 15 +₹670.45, July 16 +₹646.20 — a
  persistent short-side edge across three sessions **independent of universe tilt**.
  Caveat unchanged: still no trending day to isolate whether this is a durable SHORT
  edge or just cleaner short-name follow-through in a range-bound tape; the two long
  losers again took near-full ATR stops on fades (SWIGGY −612, BIOCON −438), the chop
  signature (Learning #4).
- **Update (July 17, 2026)**: The four-session SHORT-book winning streak (July 13–16)
  **ended** — but on a sample too small to weigh against the thesis. On a balanced 8/7
  universe the engine took **5 LONG + only 1 SHORT**; the lone short (ZYDUSLIFE) lost
  −₹234.90 and the LONG book lost −₹1,093.30, so the day netted −₹1,328.20 at 33.3%.
  With n=1 short this is not a counter-signal to short-side follow-through; it is a
  LONG-heavy chop day where longs dominated the book. The reading is unchanged: **which
  side you trade does not predict P&L — trend persistence (Learning #4) does, and there
  was none.** Winners were quick scalps (+228, +56) while three of four losers paid
  near-full ATR stops — the same chop P&L asymmetry as July 13/14/16.
- **Update (July 20, 2026)**: SHORT book won again — **2W/1L on 3 shorts**, producing both
  of the day's top winners (AXISBANK +₹181.70, LTF +₹148.05), while the 2 LONGs went 1W/1L
  for −₹375.00. Across **July 13–20 the short side has now outperformed the long side in
  five of six sessions**, on buy-tilted, sell-tilted, and balanced universes alike — the
  tilt-independence noted on July 15/16 continues to hold. That is enough sessions to
  justify a **dedicated backtest split by side** rather than more anecdotal tracking; the
  open question is still whether this is a durable SHORT edge or an artifact of a
  range-bound July with no trending bullish day in the sample (Learning #4 caveat
  unchanged). Today also gave the **cleanest P&L-asymmetry illustration in the log**:
  3W/2L (60%) yet −₹608.05, because the three winners averaged +₹169.92 against losers
  averaging −₹558.90 — a **3.3× loser/winner size ratio**. Hit-rate remains a poor proxy
  for edge absent trend persistence.

### 11. Late Entries (After 14:30) Underperform
- May 27: SWIGGY entered at 14:58 → lost ₹533.60 (SL hit in 10 min). ADANIPOWER
  entered at 15:09 → scraped +₹148.87 in 3 min. Only ADANIENSOL (entered 11:09,
  held 3.5h) was a meaningful winner.
- **Observation**: Entries in the last hour face compressed time for trends to develop,
  and proximity to EOD flatten reduces the strategy's edge of catching all-day runners
  (Learning #4). The `no_new_entries_after=15:10` cutoff may be too late — consider
  tightening to 14:30 or adding a separate late-entry risk multiplier.
- **Sample size**: Only 1 day of data — need to track late vs early entry performance
  over more sessions before changing config.

### 12. Reverting atr_sl_mult to 1.2 on Live Hurts (May 29 Evidence)
- First live day used atr_sl_mult=1.2 instead of the sandbox-proven 1.5.
- Result: 33% win rate, -₹784.80 net. Both NBCC trades stopped out on pullbacks.
- This mirrors the May 21 backtest finding (Learning #1): 1.2 produced 33% win rate vs 83% at 1.5.
- **Strong signal across 2 data points**: atr_sl_mult=1.2 consistently underperforms 1.5 on this strategy.
- **Action**: Consider reverting to 1.5 before next live session.
- **Update (May 29 EOD)**: Reverted live engine to `atr_sl_mult=1.5` for Monday's
  open. `.env`, `.sample.env`, and the Python default in
  `SimplifiedEngineConfig` now agree on 1.5. Evidence basis: May 21 backtest
  (1.2 → 33% win rate vs 1.5 → 83%) + May 29 live (1.2 → 33% / -₹784.80).
  Regression-guarded by `test_default_atr_sl_mult_is_1_5` in
  `test/test_simplified_stock_engine_core.py` so future config edits cannot
  silently revert to 1.2 without a failing test.

### 13. Config Mismatch is Dangerous
- May 21 comparison proved that the backtester and live engine MUST use identical
  config. A 0.3 difference in one parameter caused a ₹2,100 P&L swing.
- **Rule**: Always use `--from-engine` when backtesting. Never rely on defaults.
- Fixed in backtester: `config_from_engine_api()` fetches live config from the
  engine's status endpoint.

---

## Backtest Limitations (Known)

1. **Candle vs tick**: Finalized 5-min candles miss intra-candle price action.
   Tick-level replay is now supported (`--tick-data`) but tick logging must be
   enabled first: `SIMPLIFIED_ENGINE_TICK_LOG_ENABLED=true`.
2. **No slippage**: MARKET orders in live trading fill at varying prices.
3. **No partial fills**: Assumes full quantity fills instantly.
4. **Selection bias**: Testing on stocks already known to be >3% gainers guarantees
   a bullish sample on BUY direction days.
5. **Intra-candle SL**: Uses candle low/high — actual SL hit time is unknown.

---

## Cost model corrections (June 7, 2026)

### Brokerage cap was per round trip, should be per order (Fixed)
`compute_zerodha_intraday_charges` (`services/simplified_stock_engine_core.py`)
capped brokerage at ₹20 for the whole round trip — but Zerodha caps ₹20 **per
order**. NBCC reconciliation exposed it: model ₹20.00 vs Kite actual ₹32.15
(37.8% under-reported). Each leg is now charged `min(₹20, 0.03% of that leg)`,
rounded to paise the way Kite's `/charges/orders` reports each order, and summed
(16.15 buy + 16.00 sell = 32.15, matching Kite exactly; model total ₹57.37 vs
Kite ₹57.27, within ₹0.5 — residual is the exchange/SEBI rate approximations,
not the cap). Regression test: `test/test_simplified_stock_engine_charges.py`.

### Capturing LTP at signal time for slippage validation (New)
Added `ltp_at_signal` (nullable REAL) to `trade_journal`. The engine writes the
decision-time reference price at entry (`_journal_record_entry`), pinned even
after `update_entry_fill` overwrites `entry_price` with the real fill. This lets
the nightly loop compute `realized_slippage = (fill_price − ltp_at_signal) /
ltp_at_signal` once live fills accumulate — directly addressing "No slippage"
under Backtest Limitations above. Column evolves at boot via guarded
`ALTER TABLE` in `trade_journal_db.init_db`.

## Bug Fixes & Improvements (May 22, 2026)

### Tick Log Loader Format Mismatch (Fixed)
The backtester's `load_tick_data()` expected filenames `ticks_YYYY-MM-DD.jsonl`
and a `"price"` field, but the actual `TickLogWriter` produces:
- **Filename**: `ticks-YYYYMMDD-<pid>.jsonl` (dashes, compact date, PID suffix)
- **Field**: `"ltp"` not `"price"`

The loader would have silently found zero files and fallen back to candle mode
every time, even with tick logging enabled. Fixed to:
- Scan for both writer format (`ticks-YYYYMMDD-*.jsonl`) and legacy format
- Read `"ltp"` with fallback to `"price"`
- Merge multiple PID files for the same date (e.g. after app restart)

### Exact Day Replay (New)
Added `--replay-symbols` and `--from-results` flags for reproducing a trading
day exactly:

```bash
# Full exact replay: live config + live stock list + tick data
uv run python backtest/run_backtest.py \
    --date 2026-05-22 --from-engine --replay-symbols --tick-data tick_logs

# Replay a past day using stocks from its results file
uv run python backtest/run_backtest.py \
    --date 2026-05-21 --from-engine --from-results backtest/results_2026-05-21.json
```

Symbol sources (priority): `--replay-symbols` > `--from-results` > `--symbols` > defaults.

---

## Open Questions / Future Research

- [ ] What ATR multiplier optimizes across 20+ trading days? (Test 1.2 to 2.0)
- [ ] Should trailing stop activation be delayed (e.g., only after 2R profit)?
- [x] ~~Does the SELL direction (shorting top losers) work at all?~~ **Yes** — May 22
      produced GLENMARK shorts: 2 trades, both winners, +₹576.40 combined. Sell screener
      needs a bearish stock, not a bearish market. Keep SELL enabled.
- [ ] Is there a market regime detector that could switch parameters dynamically?
- [ ] Per-symbol daily trade limit — would it improve or hurt?
- [x] ~~Enable tick logging and compare tick-replay vs candle-replay results.~~
      Tick replay is implemented. Enable with `SIMPLIFIED_ENGINE_TICK_LOG_ENABLED=true`.
      Filename/field mismatch fixed May 22.
- [ ] Test with `mode=live` after 2 weeks of profitable sandbox results.
- [ ] Save the engine's armed stock list to a daily log file so past days can be
      replayed exactly even after the engine resets.

---

## 2026-06-10 — Pre-market intent now resolves via unified table

Prior: pre-market intent for simplified_engine was a date-keyed mode-of-execution row
in `daily_intent` table (live/sandbox/skip), surfaced only via `/mode/status` and not
wired into the order path. Engine ran on `SIMPLIFIED_ENGINE_MODE` env var.

Now: `services/mode_service.resolve_strategy_mode('simplified_engine', today)` is the
single read path. Fall-through order: unified `strategy_daily_intent` row → legacy
`daily_intent` row (auto-migrated at boot) → `SIMPLIFIED_ENGINE_MODE` env → default
`sandbox/run`.

Operator can set today's intent via SQL (`set_intent(...)`) or — once
`TELEGRAM_INBOUND_ENABLED=true` — by texting the bot (`/intent simplified pause`,
inline keyboard at 08:45 IST, etc.). Mode flips (sandbox/live) stay laptop-only.

See `docs/design/strategy_daily_intent.md` and `docs/design/telegram_inbound.md` for
full architecture.
