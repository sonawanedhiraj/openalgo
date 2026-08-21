# R61 — simplified_engine sandbox-journal replay: what would raise profit?

**Date:** 2026-08-08 · **Issue:** #575 · **Verdict: the strategy is NET NEGATIVE;
two arithmetic changes recover it to +₹8,577, but no *statistical* edge survives.**

## Data

231 closed sandbox trades, 41 trading days, 2026-06-01 → 2026-08-07, replayed
against **real broker 1m bars** from `historify.duckdb` (218 traded symbol-days,
**100% bar coverage**). Entry price is always the journal's **actual fill** —
only the exit is counterfactual, so modelling error is confined to one leg.
Excludes the 28 + 4 `phantom_cleanup*` tombstones.

## Finding 1 — the reported P&L is GROSS; the strategy loses money

`trade_journal.pnl` is `(exit − entry) × qty` on **231/231** rows. There is no
charges column, and the dashboard renders that figure as **"Net P&L"**.

| | |
|---|---|
| Gross P&L (what the dashboard shows) | **+₹8,740** |
| Modelled Zerodha MIS charges | **−₹17,385** (₹75/trade) |
| **True net** | **−₹8,645** |

Charge model cross-checks against the registry: **R56 reported ₹76/trade** on a
comparable notional; this model gives **₹75/trade**.

## Finding 2 — the unit economics are the whole story

| | % of notional |
|---|---|
| Gross edge/trade, live rule | **+0.0444%** |
| Gross edge/trade, hold-to-EOD | **+0.0754%** |
| Cost/trade | **0.0882%** |

The edge is **0.5×–0.85× the cost**. This is not an exit-tuning problem or a
filter problem — the signal does not clear its transaction costs at the current
₹85k average notional.

## Finding 3 — two changes are ARITHMETIC, not curve-fit

**(a) Hold to EOD instead of the ATR-stop + RR-trail: +₹6,112.**
Every trailing variant is far worse (−₹13k to −₹15k). Reproduces R54 (stops
reject on the futures sleeve), R56 (87% of exits are stops) and R57 ("the
engine's ATR-stop+RR-trail DESTROYS it; hold-to-EOD RECOVERS it").

**(b) Skip 11:00–13:00 entries: a further +₹11,110.**
Decomposed at hold-to-EOD:

| window | n | gross/trade | charges | net |
|---|---|---|---|---|
| midday 11:00–13:00 | 106 | **−0.0304%** | −₹8,232 | **−₹11,110** |
| outside midday | 125 | **+0.1730%** | −₹9,156 | **+₹8,577** |

**This does not require believing midday edge is negative** — only that it is
below the 0.0882% cost line. Measured −0.03%, a wide margin. That is why it is
robust where the statistical claims are not.

Combined: **−₹8,645 → +₹8,577** on the same signals.

⚠ This **reverses** the earlier gross-basis reading that the midday window was
"net positive". On gross it contributes +₹1,547; on net it costs **−₹11,110**.
Costs flip the sign — always evaluate this strategy net.

## Finding 4 — position size is structural but magnifies noise

Brokerage is capped at ₹20/leg, so cost/notional **falls** with size
(0.088% → ~0.054%). Hold-to-EOD, all trades: 1x −₹2,533 · 2x **+₹4,936** ·
5x **+₹28,656** · 10x **+₹68,214**.

**Do not deploy this.** H2's gross edge over all trades is **−₹128** — size
multiplies a second-half edge of ~zero. It also assumes **size-invariant fills**
(see `consolidated-10L-deployment-lever-is-position-size`); at 5–10× the
₹85k clip, slippage grows and Finding 5 shows the margin cannot absorb it.

## Finding 5 — the best statistical cell FAILS a placebo test

`skip-midday + SHORT + hold-to-EOD` (n=68) is positive in both halves at every
size, positive in all 3 months, top-1 concentration a healthy 30%. It still dies:

- **Slippage:** net turns negative at **10 bps/leg** (survives ~5 bps).
- **Placebo:** **16.1%** of *random* 68-trade subsets are also both-halves
  positive, and **9.4%** match or beat its net. After ~35 cells tested, ≈3 such
  hits are expected by chance.

**Not distinguishable from luck.** Rejected as an edge claim.

## Recommendation

1. **Fix the labelling** — the dashboard calls a gross number "Net P&L". (#576)
2. **Ship (a) + (b)** — exit-to-EOD and no 11:00–13:00 entries. Justified by cost
   arithmetic, not by a fitted parameter, and both reproduce prior rounds.
3. **Do not** raise size, and do not deploy the SHORT/midday combination.
4. The honest ceiling here is **+₹8,577 over 41 days on ₹20k** with a ~5 bps
   slippage budget. That is thin. R56 already rejected this signal class as an
   intraday system; this replay agrees on live data.
