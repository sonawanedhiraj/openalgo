<!-- migrated from outputs/2026-06-13_screener_regression_root_cause.md on 2026-06-13 | summary: In-house screener regression â€” root-cause triage -->

# In-house screener regression â€” root-cause triage

**Date:** 2026-06-13
**Author:** Claude Code (analysis-only; no code changed, no commits)
**Scope:** Why in-house screener hits fell from 1944/day (06-09) to 7/day (06-12).
**DB access:** read-only (`mode=ro`). WIP files untouched. OpenAlgo + bridge left running.

---

## TL;DR verdict

The "1944 â†’ 7" headline **conflates two unrelated events**. There is **no rule bug
and no scan_definitions corruption.** Decomposed:

1. **06-10 (BUY = 0):** NOT a regression. It was a genuine market selloff. The
   independent Chartink webhook feed *also* showed BUY=0 / SELL=37 that day, and the
   in-house scanner agreed perfectly (0 BUY, 1503 SELL). The feed was healthy.
2. **06-11 & 06-12 (collapse to 7/day):** **Broker WebSocket tick-feed starvation.**
   The in-house scanner is purely tick-driven â€” rules are evaluated *only* at 5-minute
   bar close, and a bar only closes when ticks arrive. On both days the Zerodha WS feed
   was down/unstable for most of the session (morning token-refresh subscribe race,
   ping/pong timeouts, and app restarts), so almost no bars closed and almost no rules
   ran. The 7 hits each day are the one brief window when ticks actually flowed.

**The real regression is feed starvation, not the screener logic.**

---

## The decisive evidence: independent Chartink feed vs in-house

The Chartink side (`scan_cycle`, `cycle_kind='chartink'`) is a **separate data source**
(operator's Chartink screener posted via webhook). It does not depend on our broker tick
feed, so it is the ground-truth market-breadth control.

| Date | Chartink BUY | Chartink SELL | In-house BUY | In-house SELL | Reading |
|---|---|---|---|---|---|
| 06-09 | 6 | 6 | 1343 | 601 | healthy, balanced market |
| 06-10 | **0** | 37 | **0** | 1503 | **genuine down day** â€” both feeds agree BUY=0 |
| 06-11 | 0 | 12 | 0 | **7 (MFSL only)** | starved: Chartink found 12 SELL, in-house caught 1 |
| 06-12 | **35** | 3 | **0** | **7 (MFSL only)** | **starved: UP day, should be ~35 BUY, got 0** |

**06-12 is the smoking gun.** The market was clearly UP (Chartink: 35 BUY vs 3 SELL). A
healthy in-house scanner would have produced many BUY hits. Instead it produced 0 BUY and
7 SELL â€” all the same symbol (MFSL), all within a single 30-minute window (13:05â€“13:35
IST). That output is *backwards* from the market and from the independent control â†’ the
scanner was not seeing the market, i.e. tick-starved.

### In-house hit distribution by hour (source='inhouse')

```
06-09  def1(BUY): 09h=449 10h=125 11h=178 12h=133 13h=90 14h=282 15h=86   (=1343, all day)
06-09  def2(SELL):09h=111 10h=65 11h=82 12h=77 13h=139 14h=117 15h=10     (=601, all day)
06-10  def1(BUY): (none, all hours)                                        (=0)
06-10  def2(SELL):10h=12 11h=70 12h=188 13h=137 14h=412 15h=684           (=1503, ramps into close; starts ~10h, no 09h)
06-11  def2(SELL):12h=7   (MFSL only; nothing else all day)               (=7)
06-12  def2(SELL):13h=7   (MFSL only, 13:05â€“13:35; nothing else all day)  (=7)
```

06-09 fires every hour 09â€“15h on both legs â†’ continuous tick flow.
06-11/06-12 fire in exactly one hour, one symbol â†’ a single brief tick window.

---

## Hypothesis dispositions

### H1 â€” scan_definitions DB update broke the rule binding â€” **RULED OUT**
- `database/scanner_db.py:71` defines `updated_at` as a plain `Column(String(40))` with
  **no `onupdate=`** â€” the timestamp only changes on an explicit UPDATE, not on read.
- Both rows are intact and correctly bound: id=1 â†’ `rule_module='fno_intraday_buy_chartink'`,
  enabled=1; id=2 â†’ `fno_intraday_sell_chartink`, enabled=1.
- BUY's `updated_at='2026-06-09 15:49:48'` is *during* 06-09, yet BUY fired 1343 times
  that day **after** the update â€” so the update did not break it.
- SELL's `updated_at='2026-06-10T17:08:34'` is **after** the 06-10 close, so it could not
  have affected 06-10 trading (SELL fired 1503 times on 06-10, before the update).
- The binding works: the rules clearly fire when ticks are present.

### H2 â€” Historify data gap broke the rule warm-up â€” **RULED OUT as the collapse cause**
- BUY needs `len(bars_daily) >= 200`; SELL needs only `>= 3` (see the rule warm-up
  guards). If the daily/weekly provider cache were broken, **SELL would fail too** â€” but
  SELL fired (MFSL) on both 06-11 and 06-12, proving the provider returns usable
  daily/weekly frames. So the warm-up path is intact.
- (Minor caveat below: BUY's higher 200-row bar makes it the first to go silent if the
  daily cache ever *does* shrink â€” worth a monitoring guard, but it is not what happened
  here.)

### H3 â€” scanner_service silent-fail path â€” **PARTIALLY TRUE (by design), not the trigger**
- `_on_bar_close` wraps everything in `try/except` and logs-and-continues
  (`scanner_service.py:788`), and `_evaluate_definitions` swallows per-rule exceptions
  (`:900`). Both rule modules also catch-all and return `False` on any raise.
- This is intentional resilience, and importantly it is **downstream of the bar close**.
  The scanner went quiet because `_on_bar_close` was **never invoked** (no ticks â†’ no bar
  closes), not because evaluation raised. No matching exceptions appear in `errors.jsonl`.
- So the swallow paths are real but did not cause the regression. (They do make
  starvation *invisible* â€” see Recommendations.)

### H4 â€” Ruff-churn logic regression â€” **RULED OUT**
- Today's ruff commits (`70869a4b8`, `20d0a52cb`) are formatting/safe-fix only, and the
  regression began 06-10/06-11, *before* today's commits. The two chartink rule modules
  have had **no commits** between 2026-06-04 and the 06-13 ruff churn.

### H5 â€” Env / metadata change â€” **RULED OUT as cause (but is a documented drift)**
- `.env: CHARTINK_RULE_BUY_GAP_PCT=1.5` is **looser** than the `.sample.env` default of
  3.0, so it would produce *more* BUY hits, not zero. It cannot explain BUY=0.
- (Drift note: the live 1.5% gap diverges from the canonical 3% Chartink formula. Not the
  bug, but worth reconciling for shadow-comparison fidelity.)

### H6 â€” WS feed broke for most symbols â€” **CONFIRMED (primary cause)**
- `errors.jsonl` shows Zerodha WS `ping/pong timed out` on 06-12 at **13:39:44** and
  **15:31:10** (`zerodha_websocket.py:490`, `zerodha_adapter.py:710`). The 13:39 timeout
  immediately follows the only 06-12 hit window (13:05â€“13:35) and precedes the
  rest-of-day silence.
- The scanner subscribe path itself works: the boot capture
  (`log/_oa_restart_1247.err`) shows all 216 symbols `âœ… Subscribed to NSE:â€¦` at a single
  boot (12:55:30). So symbols *are* subscribed â€” the problem is the **feed behind the
  subscription stops delivering ticks** after timeouts/reconnects.
- Known architectural fragility (project memory): the in-house scanner has **no
  self-subscribe** â€” it passively reads ZMQ and only sees ticks the broker adapter is
  actually publishing; and the boot pre-subscribe **races the morning Zerodha token
  refresh** (polls WS ~30s at boot, never retries), so a "successful" 09:27 subscribe can
  sit on a connection the subsequent token refresh invalidates, yielding no real ticks.

### H7 â€” Restart-induced data loss â€” **CONFIRMED (contributing)**
- Restart capture files confirm repeated restarts: `_openalgo_restart_0611.err` (06-11)
  and `_oa_restart_1247.err` (06-12). `errors.jsonl` truncates to `2026-06-12 12:59:16`,
  consistent with a ~12:47â€“12:55 restart that morning re-truncating the log.
- The in-house scanner's **intraday** state (5m rolling frames in `_bar_history`, 15m
  deques in `_Rolling15mBars`) is in-memory and **wiped on every restart**. After a
  restart the 15m frame needs 15 closed 15m bars (~3.75h) to re-warm RSI(14), and the 5m
  Supertrend needs fresh bars â€” so even when ticks resume post-restart, intraday gates
  stay NaN-rejected for a while. Combined with the feed dying again (13:39 timeout), the
  06-12 tick window (13:05â€“13:35) was too short and too late to produce normal breadth.
- Daily/weekly state survives (disk-backed provider), which is why SELL's low-warm-up leg
  could still catch MFSL while BUY's heavier path produced nothing.

### Why always MFSL, and only SELL, in the brief windows
Not fully determined and not load-bearing for the root cause. Most likely MFSL was among
the first symbols to receive ticks after the reconnect/restart and was one of the few
whose intraday 5m/15m gates aligned for SELL in that short window on a down/uncertain
tape. It is a symptom of the narrow tick window, not a separate cause.

---

## Root causes (ranked)

1. **PRIMARY â€” Broker WebSocket tick-feed starvation (06-11, 06-12).** The Zerodha WS
   feed was down/unstable for most of both sessions (morning token-refresh subscribe
   race, ping/pong timeouts, restarts). Because the in-house scanner evaluates rules
   *only* at 5-minute bar close, no ticks â†’ no bar closes â†’ no evaluations â†’ near-zero
   hits. (H6 + H7.)
2. **SECONDARY â€” No staleness alarm for the in-house scanner.** Unlike `sector_follow`
   (which has a data-freshness gate + auto-pause), the in-house scanner silently emits
   nothing when starved. The collapse was invisible until manually noticed. (H3 swallow
   paths make this worse.)
3. **NON-CAUSE but worth fixing â€” 06-10 BUY=0 was correct market behavior**, not a bug.
   The headline metric ("hits/day") is not a health signal â€” it tracks the market, not
   the scanner. A down day legitimately yields ~0 BUY.

---

## Recommended fixes (NOT applied â€” operator decides)

### Fix A â€” Make in-house tick starvation fail loud (highest value, lowest risk)
Add a freshness/heartbeat check for the in-house scanner, mirroring the existing
`sector_follow` data-freshness pattern: track last-bar-close time per symbol (or an
aggregate "bars closed in last N minutes" counter) and alert via Telegram when it drops
to ~0 during market hours. Optionally surface it on the scanner status endpoint.
- **Risk:** very low â€” additive observability, no order-path change.
- **Why:** turns a silent multi-day outage into a same-session alert.

### Fix B â€” Harden the scanner's tick subscription (addresses the primary cause)
Give the scanner its own re-subscribe retry that does not depend on the boot pre-subscribe
window, and re-subscribe on WS reconnect / token refresh (the new event-driven WS reinit
on `CACHE_INVALIDATE`, dev `c5f88a8cf`, is the natural hook). Confirm the scanner's 216
symbols are re-subscribed after every adapter reconnect, not just at boot.
- **Risk:** medium â€” touches subscription lifecycle; needs E2E coverage for the
  reconnectâ†’re-subscribe path. Test against the morning token-refresh race specifically.
- **Why:** removes the boot-race / post-timeout starvation at the source.

### Fix C â€” Persist or re-warm intraday bar state across restarts
Either snapshot the 5m/15m rolling frames to disk, or seed them from historify 1m on boot
so RSI(14)/Supertrend gates are not NaN-rejected for hours after every restart.
- **Risk:** medium â€” adds state management; seeding from 1m must match the live bar
  builder's aggregation exactly (see "historify 5m is computed, not stored" memory).
- **Why:** restarts mid-session currently blind the scanner for hours.

### Fix D â€” Reconcile the BUY gap threshold (hygiene, not the bug)
Decide whether `CHARTINK_RULE_BUY_GAP_PCT=1.5` (and SELL 1.5) should match the canonical
3.0 Chartink formula, and log the decision in `docs/PARAMETER_LOG.md`.
- **Risk:** low â€” single env value; affects hit volume only.
- **Why:** keeps the shadow-comparison apples-to-apples.

### Fix E â€” Change the health metric
Stop treating raw hits/day as a regression signal; compare in-house vs the Chartink feed
(recall ratio) â€” which is exactly what `scanner_comparison_eod` already computes. A
down-day BUY=0 is then correctly read as "market", and a 35-vs-0 divergence (06-12) flags
starvation immediately.
- **Risk:** none â€” uses an existing job.

---

## Is tomorrow's trading at risk if unfixed?

**Yes, the in-house scanner's reliability is at risk â€” but live order safety is not
directly implicated by this finding.**
- The starvation is a *broker-feed/session* problem. If the Zerodha WS feed is stable
  tomorrow (clean morning login, no ping/pong timeouts, no restarts), the scanner will
  produce normal breadth again â€” nothing in the scanner logic is broken.
- If the feed is unstable again (or there's a mid-session restart), the in-house scanner
  will silently under-produce again, with **no alert** (Fix A not yet in place). The
  operator would not know until manually checking.
- `SCAN_HIT_POSTER_MODE=shadow` means in-house hits are **not posted to the engine**, so
  this did not place or suppress live orders. The live engine is driven by the Chartink
  webhook path, which was healthy throughout (35 BUY on 06-12).
- **Action for tomorrow morning:** verify a clean Zerodha login and a stable WS feed
  (watch for `ping/pong timed out` in `errors.jsonl`), and sanity-check in-house breadth
  against the Chartink feed mid-morning. That manual check substitutes for Fix A until it
  ships.

---

## Appendix â€” files & lines examined
- `services/scan_rules/fno_intraday_buy_chartink.py` â€” 12-gate BUY; warm-up needs dailyâ‰¥200.
- `services/scan_rules/fno_intraday_sell_chartink.py` â€” 10-gate SELL; warm-up needs dailyâ‰¥3.
- `services/scanner_service.py` â€” tick-driven; rules run only in `_on_bar_close` (:777),
  swallow paths at :788 and :900.
- `services/scanner_history_provider.py` â€” daily/weekly cache, boot warm-up + 16:00 refresh.
- `database/scanner_db.py:71` â€” `updated_at` has no `onupdate` (H1 disproof).
- `db/openalgo.db` (ro): `scan_definitions`, `scan_results` (inhouse), `scan_cycle` (chartink).
- `log/errors.jsonl` â€” WS ping/pong timeouts 06-12 13:39:44 & 15:31:10.
- `log/_oa_restart_1247.err`, `log/_openalgo_restart_0611.err` â€” restart captures; 216-symbol subscribe at 12:55:30.
