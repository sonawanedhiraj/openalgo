# R78: Dheeraj's audit/handover doc vs the simplified engine's measured equity economics

Issue [#781](https://github.com/sonawanedhiraj/openalgo/issues/781). The updated Word document
is `Trading_Strategy_Audit_and_Handover_updated_2026-11-13.docx` in this folder. Its new section
15 holds every table below. The document's original content is unchanged: the original body
elements are byte-identical.

## 1. What the document actually claims

- **No profitability claim and no charge figure.** It is an investigation framework.
  - Its section 14 says: no backtest was run, no performance figures were invented, and it is
    "not a claim that the strategy is profitable".
  - Its section 4 says to take charges from contract notes rather than a remembered estimate.
- **So there is no claimed result to re-run at the real cost.** Instead, its own section-13
  gross-to-net bridge was filled in with measured data.
- **The strategy it describes is the simplified stock engine:** ChartInk F&O scan, 5-minute candles,
  70% elapsed, ₹500 risk, `apply_simple_rr_trailing`, 15:20 square-off.
- **Where the document diverges from the running engine** (config v1.1):

  | Item | Document | Running engine |
  |---|---|---|
  | Instrument | "F&O bot", futures-lot rounding | Equity MIS |
  | Volume filter | 1.5× | 2.5× |
  | Stop | Red-candle close | ATR 1.5× |
  | Trail start | 50% | 0.6R |
  | Trades per day | 3–5 | Measured 5.1 (cap 6) |
  | Mode | Assumed live | Sandbox only |

## 2. Measured (511 closed trades, 2026-06-01 → 11-13, 100 days; sandbox fills)

**Data:** `trade_journal`, `trending_equity_intraday` / chartink / sandbox, read from a byte-copy of
the database. Excludes 32 `phantom_cleanup` rows and 10 open or unpriced rows.

| | All | Long | Short |
|---|---:|---:|---:|
| Trades | 511 | 240 | 271 |
| Gross | +₹9,752 | −₹301 | +₹10,052 |
| Charges (journal model) | ₹39,231 | ₹18,112 | ₹21,119 |
| Net | **−₹29,480** | −₹18,413 | −₹11,067 |
| Win rate, gross / net | 59.3% / 56.6% | 60.4% / 57.9% | 58.3% / 55.4% |
| Average win / loss (gross) | ₹336 / ₹443 | ₹284 / ₹436 | ₹384 / ₹448 |
| Payoff, gross / net | 0.76 / 0.56 | 0.65 / 0.45 | 0.86 / 0.66 |
| Gross per trade | ₹19 (2.21 bps) | −₹1 (−0.15 bps) | ₹37 (4.17 bps) |
| Charge per trade | ₹77 (8.89 bps) | ₹75 (9.05) | ₹78 (8.76) |
| Charges ÷ gross | **4.0×** | — | 2.1× |
| Mean notional | ₹86,325 | ₹83,405 | ₹88,910 |
| Max drawdown (net) | −₹34,489 | −₹21,692 | −₹18,062 |

- **Every month from June to November is net negative.**
- **Since R65** (345 trades): gross +₹2,218 → +₹9,752, charges ₹26,463 → ₹39,231, net −₹24,245 →
  −₹29,480.
- **Charge check:** Kite's own `POST /charges/orders` calculator (it places no order) re-priced all
  1,022 legs at **₹38,859**, against ₹39,231 in the journal model. They agree to within 1%.
- **Charge components across the 511 trades:**

  | Component | Total |
  |---|---:|
  | Brokerage | ₹19,650 (51%) |
  | STT | ₹11,028 |
  | GST | ₹4,040 |
  | Exchange transaction | ₹2,708 |
  | Stamp duty | ₹1,344 |
  | SEBI fee | ₹88 |

## 3. Real equity-intraday round trip (Kite calculator, current rates)

| Notional | ₹23.8k | ₹49k | ₹65.8k | ₹99.4k | ₹1.99L | ₹5.0L |
|---|---:|---:|---:|---:|---:|---:|
| Round trip ₹ | 25.58 | 51.61 | 69.96 | 82.49 | 117.77 | 224.54 |
| Round trip bps | 10.75 | 10.53 | 10.63 | 8.30 | 5.92 | 4.49 |

- **Structure:** each round trip is a fixed ₹47.2 (₹20 × 2 brokerage, plus GST) plus a proportional
  **3.55 bps** (STT 2.50, exchange 0.61, stamp 0.30, SEBI 0.02, GST 0.11).
- **Floor:** 3.55 bps is the lowest the cost can go at any position size. Brokerage only reaches its
  ₹20 cap at about ₹66.7k per order.

## 4. Where the economics break

- **Planned vs realised:** the document plans a 1:2 trade (₹500 loss, ₹1,000 gain). At ₹77 of cost per
  trade, that plan breaks even at a **38.5%** win rate.
- **The realised wins are a third of plan:**

  | | Planned | Realised |
  |---|---:|---:|
  | Average win | ₹1,000 | ₹336 |
  | Average loss | ₹500 | ₹443 |
  | Payoff | 2.0 | 0.76 |

- **At the realised payoff the break-even win rate is 66.7%**, and the engine wins **59.3%** of trades
  gross.

## 5. What would have to improve

- **Threshold:** a trade must make at least **₹77 gross, about 8.9 bps** of notional at today's
  ₹86k mean position. Today it makes ₹19 (2.2 bps), so the gross edge per trade has to grow **4×**.
- **Bigger average win:** at the current 59.3% win rate and ₹443 average loss, the average win must
  reach **₹433** (+29%), a payoff of 0.98. That needs a different exit. R67's 106 stop/exit variants
  all failed out of sample, so the entry is the binding constraint.
- **Fewer trades:** this only helps if the trades dropped are worse than average.
  - In-sample, the first trade of each day nets **+₹1,516** over 100 trades (₹87 gross per trade).
  - Trades 2–6 net −₹30.5k.
  - This is promising but unvalidated, and filters failed out of sample in R65/R76. Test it as the
    document's experiment E, with the cap fixed before the test.
- **Larger notional:** cost falls from 9.0 bps (₹86k) to 5.9 bps (₹2L) and 4.5 bps (₹5L), but the
  **floor is 3.55 bps, above the 2.21 bps gross edge**. Size alone can never fix it, and larger size
  also breaks the ₹500 risk cap.
- **Combination:** for example, 1–2 selective trades a day at ₹2L each need about **≥ 5.9 bps** gross
  per trade (₹118), about 2.7× today's edge.

## 6. Verdict

The document is right that the strategy is gross-profitable and net-negative, and that charges are
the visible cause. The measured data add two things the document could not know:

- **The cost is mostly fixed brokerage**, at the engine's ~₹86k positions.
- **The real gap is the realised payoff, not the charge rate.** The average win is a third of plan,
  so each trade earns about a quarter of what it costs.
- **Lowering charges will not fix it.** Even zero brokerage leaves ₹46 per trade of statutory cost
  against ₹19 gross.
- **What would fix it:** a 4× improvement in gross edge per trade. The candidates are a selective
  first-trade-only test and a better entry, both validated out of sample.
- **Status:** REJECT, as it runs today.

## Reproduce

`backtest/equity_doc_audit/` (untracked) contains:
- `engine_economics.py` — run with the DB byte-copy path, plus a live broker session for the Kite
  calculator.
- `update_docx.py`
- `results.json`
- `engine_trades.csv`
