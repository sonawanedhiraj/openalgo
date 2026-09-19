# cas_320_expiry_straddle — Learnings

Cumulative knowledge for the expiry-day ATM straddle. Append one dated entry per
expiry session (the 15:45 Telegram digest is the raw material). Verified
exchange/broker facts live in `PLAN.md` §1 and the memory note
`cas-expiry-day-index-options-facts`.

## Cumulative

- **Mechanism, not momentum.** After 15:15 the "spot" is the Indicative Index
  Value of the auction book; the option premiums price off it until the close
  prints (~15:30). The straddle pays when |close − K| or the peak IIV
  excursion exceeds the premium plus spread.
- **Regulatory shelf life.** SEBI's consultation paper (comments closed
  2026-10-03) proposes hiding the IIV and/or a VWAP expiry settlement. Either
  change removes most of the edge — the pre-registered kill is to flip the
  strategy to sandbox and keep recording.
- **The video's evidence is look-ahead** (exit priced at the window high on 4
  samples). Every number here must come from the strategy's own polls.
- **No historical data exists**: Kite drops expired weeklies; historify's index
  1m bars are padded flat from 15:15. The `cas_straddle_polls` /
  `cas_straddle_sessions` tables ARE the dataset. Verdict is INSUFFICIENT
  until ≥ 30 expiry sessions.

## Dated entries

### 2026-09-19 — shipped (sandbox from boot)

- Built in one PR (#740): service, journal, control API, Settings card,
  dashboard branches, registries, tests. First expiry sessions: NIFTY Tue
  2026-09-22, SENSEX Thu 2026-09-24. Nothing traded yet.
