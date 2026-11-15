"""R80 step 2a: replay the REAL in-house scanner rules over 2024-01-01..2025-06-19.

Reuses backtest/inhouse_scanner/replay_signals.process_symbol (R56 harness, frozen clock, PIT frames)
on data/bars_r80.duckdb. Loads .env first so the rule reads the live gap % (1.5) exactly as the scanner.

  uv run python backtest/r80_scanner/signals_r80.py
"""
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
os.chdir(REPO)


def _env():
    for line in open(REPO / ".env", encoding="utf-8"):
        if line.startswith(("CHARTINK_RULE_", "SCANNER_")) and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip("'\"")


_env()
import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

DB = str(HERE / "data" / "bars_r80.duckdb")


def work(sym):
    import backtest.inhouse_scanner.replay_signals as rs

    con = duckdb.connect(DB, read_only=True)
    try:
        return rs.process_symbol(con, sym, pd.Timestamp("2024-01-01"), pd.Timestamp("2025-06-19"))
    except Exception as e:  # noqa: BLE001
        print("ERR", sym, repr(e), flush=True)
        return []
    finally:
        con.close()


if __name__ == "__main__":
    con = duckdb.connect(DB, read_only=True)
    syms = [r[0] for r in con.execute("select distinct symbol from prices_1m where symbol not like '\\_\\_%' escape '\\' order by 1").fetchall()]
    con.close()
    print("gap", os.environ.get("CHARTINK_RULE_BUY_GAP_PCT"), os.environ.get("CHARTINK_RULE_SELL_GAP_PCT"),
          "symbols", len(syms), flush=True)
    t0 = time.time()
    rows = []
    with Pool(int(os.environ.get("R80_PROCS", "6"))) as p:
        for i, r in enumerate(p.imap_unordered(work, syms), 1):
            rows += r
            if i % 10 == 0:
                print(f"{i}/{len(syms)} {time.time() - t0:.0f}s signals={len(rows)}", flush=True)
    df = pd.DataFrame(rows).sort_values(["day", "fire_ts", "symbol"])
    df.to_parquet(HERE / "data" / "signals_r80.parquet", index=False)
    print("DONE", len(df), df.direction.value_counts().to_dict(), flush=True)
