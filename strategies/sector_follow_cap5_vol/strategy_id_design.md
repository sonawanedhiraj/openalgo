# strategy_id Schema Design — sector_follow_cap5_vol (Phase 0 Deliverable C)

**Proposal only. No code changes in this phase.**

## Current state
`database/strategy_db.py` defines `Strategy` (`strategies` table): auto-increment
`id` (PK), `name`, `webhook_id` (UUID, unique), `user_id`, `platform`, `trading_mode`,
time windows. `StrategySymbolMapping.strategy_id` (Integer FK → `strategies.id`,
`strategy_db.py:68`) is the existing per-strategy attribution key. Strategies are
created via `create_strategy(...)`, which assigns the auto-increment id.

## 1. Registration
**Yes — register `sector_follow_cap5_vol` as one row in `strategies` at boot**,
idempotently: look up by a stable natural key (`name='sector_follow_cap5_vol'`),
and only `create_strategy(...)` if absent, so restarts/re-deploys reuse the same
auto-increment `id`. Store that `id` in the strategy's `config_snapshot.json`
(`strategy_id`) so backtests, the journal, and the live evaluator agree on one
canonical id. Keep `platform='internal'` (not tradingview/chartink) and
`is_active=False` while `mode: scaffold-only`.

## 2. Order tagging at placement (proposal)
At the 15:20 evaluation, the evaluator emits recommended orders. When an order is
actually placed (operator-manual first, automated later), tag it by carrying
`strategy_id` into the order record:
- Reuse the existing order→strategy linkage rather than inventing a parallel one.
- Concretely: stamp the placing payload with the resolved `strategy_id` and persist
  it on the order/trade row at fill time (mirror how webhook strategies already
  associate orders), so every fill is attributable without heuristics.
- Until live, the backtest writes `strategy_id` into `trades.csv` so the schema is
  exercised end-to-end before any real order flows.

## 3. P&L attribution query pattern
```sql
-- realized P&L per strategy over a window
SELECT s.id AS strategy_id, s.name,
       COUNT(*) AS n_fills, SUM(t.realized_pnl) AS pnl
FROM   trades t
JOIN   strategies s ON s.id = t.strategy_id
WHERE  t.fill_time BETWEEN :start AND :end
GROUP  BY s.id, s.name;
```
Single integer FK join — cheap, indexable on `t.strategy_id`. Matches the existing
`StrategySymbolMapping` pattern; no schema change needed beyond ensuring the
order/trade row persists `strategy_id`.

## Open question for Phase 1
Does the live trade/order table already carry a `strategy_id` column, or only the
mapping table? If only the mapping, Phase 1 adds a nullable `strategy_id` FK to the
order/trade journal (additive, back-compatible). Confirm before writing code.
