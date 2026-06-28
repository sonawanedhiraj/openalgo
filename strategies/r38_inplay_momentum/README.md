# R38 — NSE In-Play Intraday Momentum (IPM)

## Overview

Direct port of Pradeep Bonde's "in-play" detection to the NSE F&O universe. The
strategy decomposes in-play into two orthogonal signals — **participation** (RVOL +
traded value, porting Bonde's 9M-volume criterion) and **velocity** (new-intraday-high
burst count or ATR-normalized thrust, porting his 60-new-highs-in-<3-min criterion).
A name passing both gates simultaneously, aligned with the intraday VWAP trend, is
taken long on continuation. This is an ignition/continuation play, not mean-reversion.
The F&O universe is used (not cash) because NSE cash circuit bands truncate exactly the
30–40% vertical moves Bonde targets.

## Status

**SCAFFOLD ONLY — not deployable, not wired into any scheduler.**

Defaults in `harness.py` Config are **placeholders** requiring calibration against the
actual in-play distribution before any result can be trusted. See Section 9 of `SPEC.md`
for the mandatory calibration protocol.

## Mode flag

Would be `R38_INPLAY_MODE` if/when deployable. Placeholder only — the mode flag does not
exist in the codebase yet.

## Benchmark gate

Must clear **V_BLD_B**: Sharpe ≳ 1.41 · green months ≳ 70% · payoff ≳ 1.67.
Anti-R37 guard: reject if qualifying trades < `MIN_TRADES` (default 30).

## Universe

NSE F&O stock constituents (record the as-of date — F&O list changes are a survivorship
source). Optional second sleeve: NIFTY / BANKNIFTY futures. As-of date: **TBD** (to be
recorded when the Phase 1 historify adapter pulls the live list).

## Files in this folder

| File | Purpose |
|---|---|
| `SPEC.md` | Full strategy specification (source: `R38_inplay_momentum_spec.pdf`) |
| `harness.py` | Python backtest harness — placeholder thresholds, calibration required |
| `config_snapshot.json` | Current config values (all placeholders, `deployable: false`) |
| `LEARNINGS.md` | Cumulative learnings — empty until Phase 1 |
| `VERSION_LOG.md` | Parameter / logic change history |

## Next phases

- **Phase 1**: write a small adapter to read 1m bars from `historify.duckdb` instead of
  per-symbol parquet → smoke-run (`python harness.py --smoke` already works for plumbing)
- **Phase 2**: real backtest with recorded F&O constituent list as-of date + 20-session
  RVOL warm-up
- **Phase 3**: calibration sweep (`RVOL_MIN` / `NHB_MIN` / `THRUST_K` / `ATR_MULT`) —
  walk-forward / OOS split
- **Phase 4**: decision vs V_BLD_B benchmark
