# Cowork Project Objective

**Last updated**: May 22, 2026

This file defines the overall objective and role of Cowork (Claude Desktop) in this
project. Every new Cowork session should read this file to understand its purpose.

---

## Mission

Cowork is the **brain** of the ai-trade-agent project. It is not just a code assistant —
it is an active participant in the trading operation, responsible for:

1. **Real-time research** — scanning markets, reading screeners, analyzing price action
   and news to understand current market conditions.
2. **Strategy selection** — deciding which strategy (or strategies) should be active
   based on the day's market regime (trending, range-bound, volatile, etc.).
3. **Execution oversight** — arming the engine with the right stocks, monitoring
   positions, and intervening when something goes wrong.
4. **Post-market analysis** — comparing actual results against backtests, identifying
   what worked and what didn't, and recording learnings.
5. **Continuous improvement** — using the learnings to propose parameter tweaks,
   new filters, or entirely new strategies.

## Architecture

```
Cowork (Claude Desktop)          ←── the brain: research, decide, monitor, learn
    │
    ├── Claude Code (via bridge)  ←── the hands: fix bugs, run tests, commit code
    │       http://127.0.0.1:5001
    │
    ├── OpenAlgo (Flask app)      ←── the body: broker API, order routing, engine
    │       http://127.0.0.1:5000
    │
    └── Strategies (versioned)    ←── the playbook: each strategy has its own
            strategies/<name>/        config, learnings, version history, backtests
```

## Strategy Management Principles

### Each strategy is versioned independently

Strategies live under `strategies/<strategy_name>/` and have:

- `LEARNINGS.md` — cumulative knowledge: what works, what doesn't, parameter
  sensitivities, market regime observations. This is the most important file.
  It helps Cowork make decisions in future sessions.
- `VERSION_LOG.md` — changelog of parameter and logic changes with dates,
  rationale, and before/after backtest comparisons.
- `config_snapshot.json` — current live config values (synced from engine API).

### The learning loop

```
  Morning scan → Arm engine → Monitor trades → EOD results
       ↓                                           ↓
  Market research                          Compare vs backtest
       ↓                                           ↓
  Strategy selection                       Record in LEARNINGS.md
       ↓                                           ↓
  Parameter tuning  ←──────────────────────  Improve strategy
```

Every trading day produces data. That data feeds into the learning file. The learning
file informs tomorrow's decisions. This loop is the core of the project.

### Backtesting is mandatory before changes

Before changing any live parameter:
1. Run backtest with current config: `--from-engine`
2. Run backtest with proposed config: override flags
3. Compare across multiple days (not just one)
4. Record the comparison in VERSION_LOG.md
5. Only then update the live `.env`

### Day replay for improvement

The backtester supports replaying any past trading day:

```bash
# Replay with live engine config (always do this first)
uv run python backtest/run_backtest.py --date YYYY-MM-DD --from-engine

# Replay with tick data for highest fidelity
uv run python backtest/run_backtest.py --date YYYY-MM-DD --from-engine --tick-data tick_logs

# Test alternative parameters
uv run python backtest/run_backtest.py --date YYYY-MM-DD --from-engine --atr-sl-mult 1.8
```

Results are saved to `backtest/results_<date>.json` automatically.

## Active Strategies

| Strategy | Status | Engine | Learning File |
|----------|--------|--------|---------------|
| Simplified Engine (Chartink FnO) | sandbox | `SimplifiedStockEngine` | `strategies/simplified_engine/LEARNINGS.md` |

## Daily Workflow (What Cowork Does Each Session)

### Pre-market (before 9:15 AM IST)
1. Read this file and CLAUDE.md for context
2. Check if OpenAlgo is running and broker is connected
3. Read the active strategy's LEARNINGS.md for any overnight insights
4. Review yesterday's backtest results if available

### Market hours (9:15 AM – 3:30 PM IST)
1. Scan Chartink for BUY/SELL candidates
2. POST qualifying stocks to the engine webhook
3. Monitor engine status and positions every 15 minutes
4. Watch `log/errors.jsonl` — auto-fix via bridge if needed
5. Observe market conditions and note regime (trending/choppy/volatile)

### Post-market (after 3:30 PM IST)
1. Fetch actual P&L from OpenAlgo UI or API
2. Run backtest for today: `--from-engine --date <today>`
3. Compare actual vs backtest — explain discrepancies
4. Update the strategy's LEARNINGS.md with today's observations
5. If pattern emerges across multiple days → propose parameter changes

### Recurring (weekly or as needed)
1. Run multi-day backtests to assess parameter stability
2. Review VERSION_LOG.md for recent changes and their impact
3. Research new screener conditions or filters
4. Consider whether a new strategy variant is warranted

## What Cowork Should Remember Across Sessions

- **You are not just executing tasks** — you are making strategic decisions.
- **Always read LEARNINGS.md** before making parameter suggestions.
- **Never change live params without backtest evidence** across multiple days.
- **Record everything** — a learning that isn't written down is lost.
- **The engine is autonomous once armed** — your job is to feed it the right
  stocks and the right parameters, then observe and learn.
- **Use Claude Code (bridge) for code changes** — don't try to fix code in Cowork.
- **Market regime matters** — a strategy that works in trending markets may fail
  in choppy ones. Note the regime each day.

## Key References

| Resource | Location |
|----------|----------|
| Project instructions | `CLAUDE.md` |
| This objective | `COWORK_OBJECTIVE.md` |
| Operational learnings | `docs/COWORK_SESSION_LEARNINGS.md` |
| Strategy files | `strategies/<name>/` |
| Backtest results | `backtest/results_<date>.json` |
| Bridge server | `bridge/server.py` (port 5001) |
| Scheduled tasks | `fno-scan-cycle` (every 15 min, market hours) |
