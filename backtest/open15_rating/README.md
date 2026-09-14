# open15_vol_breakout — Round 63 winner-pattern / trade-rating harness

Run from the repo root (read-only on the live DB via `mode=ro`; tick logs in `tick_logs/open15/`):

    uv run python backtest/open15_rating/build_features.py      # -> features.json / features.csv (next to the script)
    uv run python backtest/open15_rating/analyze_winners.py     # winner/loser medians, binned win rates, news matches
    uv run python backtest/open15_rating/rating_v1_7check.py    # 7-check rating (in-sample fit; fails holdout)
    uv run python backtest/open15_rating/rating_v2_3check.py    # 3-check rating (holdout + time-split checks)

Report: docs/research/strategy/open15_vol_breakout/2026-09-14_r63_winner_patterns_and_trade_rating.md (issue #725).
