# cas_auction_reversal — plan and pre-registered gate

## Schedule (IST, mon-fri, trading days)

| Time | Job | Action |
|---|---|---|
| 09:00 | `cas_ar_daily_reset` | Clear the day state |
| 09:16 | `cas_ar_exit` (PROTECTED) | CNC SELL for every earlier-day entry. Quantity from own holdings + positions (mode_key). A CNC sell cannot short, so it always sends |
| 09:20 | `cas_ar_exit_retry` (PROTECTED) | Same, for anything still open |
| 15:14:30 | `cas_ar_arm` | Universe + monitor thread: one batched quote every 15 s to 15:33, journaled to `cas_ar_polls` |
| 15:23:30 | `cas_ar_decide` | IEP <= ref × (1 − 0.5%) → top 10 by dislocation. LIVE: CNC LIMIT into the auction now. SANDBOX: queued |
| 15:32 | `cas_ar_fill` | SANDBOX: CNC LIMIT at the post-auction LTP (= the auction close). Both modes: fill verification (#626) |
| 15:45 | `cas_ar_eod_summary` | Final auction close per candidate (the measurement) + Telegram |

## Pre-registered gate (from R71b, before any live flip)

After **20 sessions** of `cas_ar_polls` / `cas_ar_candidates`, re-run the R71b
table with the **15:23:30 IEP** as the signal and the **final auction close** as
the fill.

Promote to live only if **both** hold on IEP-signalled names at >= 0.5%:
- they still make **>= +0.40% gross** to the T+1 09:15 bar close;
- they win **>= 70%** of the time.

Otherwise drop or redesign. Also required before live: the Zerodha holdings
mapper must carry `t1_quantity`.

## Kill criteria

- **Regulatory:** SEBI/NSE stops disseminating the indicative close, or
  materially changes the CAS mechanism. The strategy goes back to sandbox and
  the evidence is re-measured.
- **Decay:** 3 consecutive calendar months with net-negative sandbox P&L. Pause
  the strategy and run a research round.
