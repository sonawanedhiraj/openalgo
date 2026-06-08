# Sector Rotation ETF — First Sandbox Rebalance Checklist (2026-06-15)

> **SCAFFOLD-ONLY. Operator-manual workflow.** This strategy is `mode:
> scaffold-only`, `deployable: false`. There is **no scheduler, no live mode, no
> automated order placement**. The CLI only computes and prints recommended
> orders. The operator places (or declines) every order **by hand**. This
> checklist does not change that — moving the date earlier gets the operator
> hands-on sooner, it does not remove any safety rail.

**Target date:** Monday **2026-06-15** (moved up from 2026-07-01).
**Capital:** ₹3,00,000 (config `capital_inr`, sandbox-first).
**Universe:** 9 NSE ETFs. **Product:** CNC (delivery). **Broker:** Zerodha.

---

## ⚠️ Read these flags BEFORE the date

1. **Cadence mismatch.** The strategy rule is `monthly_first_trading_day`.
   2026-06-15 is the **third Monday**, not the first trading day of June (that
   was ~June 1). Treat 2026-06-15 as a **mid-cycle seed entry (month-0)**. The
   *next* rebalance should snap back to the native cadence: **first trading day
   of July (~2026-07-01)**, NOT 2026-07-13. (See post-trade step 3.)

2. **Defensive concentration.** Today's signal (2026-06-08 dry-run) puts
   **PHARMABEES + HEALTHIETF in BOTH the momentum and low-vol baskets**, so they
   each get a doubled allocation → **₹2.0L of ₹3.0L (66.7%) sits in
   pharma + healthcare**, two highly correlated sectors. This is by design
   (overlapping legs are summed) and in-character for a weak, defensive tape, but
   it is a *concentrated, not diversified*, book. The operator should accept this
   consciously or cap the overlap manually.

3. **Weak-momentum tape.** Only 3 of 9 ETFs have positive 6-month momentum
   (METAL +28.7%, PHARMA +5.9%, HEALTH +3.1%); the other six are negative
   (ITBEES −21.4% worst). FMCGIETF is bought into the low-vol leg despite
   −12.8% 6M momentum — that is the low-vol sleeve doing its job (calmest, not
   strongest), not a bug.

4. **METALIETF short history.** Launched 2024-08-20 (~22 months of bars as of
   June 2026). Momentum/vol are computable but its history is the shortest in the
   universe. Only ₹46k allocated, so impact is bounded.

---

## 1. Pre-flight — Sunday 2026-06-14

- [ ] **Zerodha session fresh** — token expires ~3:00 AM IST daily; you'll
      re-login Monday regardless, but confirm the broker is reachable today.
- [ ] **OpenAlgo healthy** — `GET http://127.0.0.1:5000/health` returns OK.
      (Do NOT restart OpenAlgo or the bridge during market hours; Sunday is safe.)
- [ ] **DuckDB current** — `db/historify.duckdb` has daily (`interval='D'`) bars
      for all 9 ETFs through the last trading day (Friday **2026-06-12**).
      Verify the dry-run below reports `symbols_missing: []`.
- [ ] **Dry-run for the last close (2026-06-12):**
      ```
      uv run python -m services.sector_rotation_etf_cli --asof 2026-06-12 --current-positions '{}'
      ```
      Confirm the basket is stable vs the 2026-06-08 preview (below). A wholesale
      change in 4 trading days is a red flag — investigate before Monday.
- [ ] **Telegram bot reachable** for status updates (optional but recommended).

## 2. Pre-market — Monday 2026-06-15, 09:00 IST

- [ ] Complete Zerodha login (token refreshed at 3 AM). Confirm session live.
- [ ] **Check India VIX / NIFTY pre-open.** If **India VIX > 25**, HOLD and
      reassess — the strategy has no fast crash gate and a −17% max DD is the
      accepted cost of the alpha; don't seed into a spike.
- [ ] **Dry-run for the last close (2026-06-13 is Saturday → use 2026-06-12):**
      ```
      uv run python -m services.sector_rotation_etf_cli --asof 2026-06-12 --current-positions '{}'
      ```
- [ ] Print the recommended orders, **sleep on them 30 minutes**, re-verify the
      basket and quantities are unchanged.
- [ ] Confirm the ₹3.0L allocation + the 66.7% pharma/health concentration is
      within your risk tolerance for a seed entry.

## 3. Execution — Monday 2026-06-15, **11:00–12:00 IST** (config window)

> Config `execution_window_ist` is **11:00–12:00** — the strategy's backtested
> mid-day fill window, away from the open-auction volatility. (Mission brief said
> 09:30–10:30; the canonical config window of 11:00–12:00 governs.)

- [ ] Operator places ETF BUY orders **MANUALLY in the Zerodha terminal** — NOT
      via OpenAlgo — to keep this entirely off the auto-trading path.
- [ ] Use **LIMIT orders at/near LTP**, not market orders (thin ETFs —
      HEALTHIETF, PSUBNKBEES — can have wide touch; budget 15–25 bps slippage).
- [ ] Place the 4 buys (today's preview — re-confirm against Monday's dry-run):

      BUY  PHARMABEES   ~4016 qty   ~₹1,00,000   [both]
      BUY  HEALTHIETF    ~631 qty   ~₹1,00,000   [both]
      BUY  FMCGIETF     ~1039 qty    ~₹53,800    [lowvol]
      BUY  METALIETF    ~3469 qty    ~₹46,200    [momentum]

- [ ] Capture **fill prices, slippage vs LTP, and realized cost per leg.**

## 4. Post-trade — Monday 2026-06-15, 16:00 IST

- [ ] Update `LEARNINGS.md` "Live Learnings" with: fill prices, realized
      slippage vs the 0.15%/side assumption, total entry cost, and any
      execution friction on the thin ETFs.
- [ ] Update `VERSION_LOG.md` with the live seed-entry date and actual fills.
- [ ] **Set the next rebalance reminder for the strategy-native first trading
      day of July (~2026-07-01)** — NOT 2026-07-13. This re-aligns the seed
      entry to the `monthly_first_trading_day` cadence.
- [ ] **Do NOT flip `mode` to live or `deployable` to true.** Keep the
      operator-manual review workflow for at least the next **3 rebalances**.

## 5. Stop conditions — pause the scaffold and review if ANY trip

- [ ] **Monthly drawdown > 8%** → review (note: −17% is the accepted *max* DD,
      but an 8% monthly bleed warrants a look).
- [ ] **Sector ETF tracking error > 50 bps over 5 days** vs its underlying index
      → review (ETF NAV decoupling / illiquidity).
- [ ] **Any held ETF's volume drops > 50% vs its 30-day average** → review
      (liquidity drying up — especially the thinner HEALTHIETF / PSUBNKBEES).

---

## Appendix — 2026-06-08 dry-run preview (full-capital, fresh start)

Command:
```
uv run python -m services.sector_rotation_etf_cli --asof 2026-06-08 --current-positions '{}'
```

| Basket | ETFs |
|---|---|
| Momentum (top-3 6M return) | METALIETF (+28.7%), PHARMABEES (+5.9%), HEALTHIETF (+3.1%) |
| Low-vol (bottom-3 60d vol) | PHARMABEES (17.3%), HEALTHIETF (17.7%), FMCGIETF (17.8%) |

Leg weights (inverse-vol): momentum **46.18%** / low-vol **53.82%**.
Capital deployed: **₹2,99,827 of ₹3,00,000 (99.9%)**. `symbols_missing: []`.

| Order | Qty | Notional | Reason |
|---|---:|---:|---|
| BUY PHARMABEES | 4016 | ₹99,998 | both |
| BUY HEALTHIETF | 631 | ₹99,868 | both |
| BUY FMCGIETF | 1039 | ₹53,789 | lowvol |
| BUY METALIETF | 3469 | ₹46,172 | momentum |

### Cost preview (₹3.0L, all-buy entry → eventual round-trip)

Zerodha CNC (delivery), 4 legs, total buy notional ≈ ₹2,99,827.

**Entry (buy) friction:**
| Component | Rate | Amount |
|---|---|---:|
| Brokerage | ₹0 (delivery free) | ₹0 |
| Stamp duty | 0.015% on buy | ₹45 |
| Exchange txn (NSE) + SEBI | ~0.00307% | ₹9 |
| GST (18% on txn+SEBI) | — | ₹2 |
| Slippage | 1.88 bps × 4 legs | ₹56 |
| STT on buy | ETF delivery (≈0)¹ | ₹0 |
| **Entry total** | **≈ 3.7 bps** | **≈ ₹112** |

¹ If equity-delivery STT (0.1% buy) were applied, add ~₹300 (entry → ~14 bps).
ETF units are treated as ≈0 STT on buy; STT bites on the **sell** leg.

**Eventual exit / next-rebalance sell (~₹3.0L):** STT 0.1% sell ≈ ₹300 +
DP charges ₹15.93 × 4 scrips ≈ ₹64 + txn/GST ~₹11 + slippage ~₹56 ≈ **₹431
(~14 bps).**

**Round-trip ≈ ₹540 (~18 bps)** — well inside the backtest's modeled friction
(total cost 0.0502 over 47 rebalances ≈ **0.107%/round-trip**), which the
~1.0–1.5%/yr ETF dividend capture offsets almost exactly (see LEARNINGS.md
"dividend-offset insight"). **Cost is not a blocker.**
</content>
</invoke>
