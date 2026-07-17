"""R48 price fetcher — scanner-universe 1m + daily bars via the broker
historical API (mirrors backtest/news_event_study/fetch_event_prices.py).

READ-ONLY on every live OpenAlgo database. Writes only the output DuckDB.
Run from the MAIN repo root: uv run python <this file> so .env loads and the
live auth token is readable. Resume-safe via fetch_log.
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DB_PATH = REPO_ROOT / "outputs" / "tod_volume_gate" / "prices.duckdb"

DAILY_START = "2024-06-01"
M1_START = "2025-06-20"
END = "2026-07-06"  # last completed trading day; exclude today's partial session

INDEX_SYMBOLS = {
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
    "INDIAVIX",
    "SENSEX",
    "BANKEX",
}


def universe() -> list[str]:
    importlib.import_module("utils.config")  # forces load_dotenv from repo root
    raw = os.environ.get("SCANNER_SYMBOLS", "")
    syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return [s for s in syms if s not in INDEX_SYMBOLS and not s.startswith("NIFTY")]


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS prices_1m ("
        "symbol VARCHAR, ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, "
        "close DOUBLE, volume BIGINT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS prices_daily ("
        "symbol VARCHAR, bar_date DATE, open DOUBLE, high DOUBLE, low DOUBLE, "
        "close DOUBLE, volume BIGINT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS fetch_log ("
        "symbol VARCHAR, kind VARCHAR, status VARCHAR, detail VARCHAR, fetched_at TIMESTAMP)"
    )


def fetch_one(symbol: str, interval: str, start_date: str, end_date: str, api_key: str):
    from services.history_service import get_history

    try:
        success, response, _ = get_history(
            symbol=symbol,
            exchange="NSE",
            interval=interval,
            start_date=start_date,
            end_date=end_date,
            api_key=api_key,
            source="api",
        )
    except Exception as e:  # noqa: BLE001 — per-symbol hiccup must not crash the run
        return "error", str(e), []
    if not success:
        return "error", (response or {}).get("message", "unknown error"), []
    records = (response or {}).get("data") or []
    if not records:
        return "no_data", "empty response", []
    return "ok", f"{len(records)} bars", records


def to_dt(ts):
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts)
    return ts


def _records_df(symbol, records):
    import pandas as pd

    df = pd.DataFrame(records)
    if df.empty or "timestamp" not in df.columns:
        return None
    ts = df["timestamp"]
    if pd.api.types.is_numeric_dtype(ts):
        dt = pd.to_datetime(ts, unit="s")
        # broker epochs are UTC seconds; fromtimestamp() on this IST box gave
        # IST-naive — replicate via tz shift
        dt = dt + pd.Timedelta(hours=5, minutes=30)
    else:
        dt = pd.to_datetime(ts)
        if getattr(dt.dt, "tz", None) is not None:
            dt = dt.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    out = pd.DataFrame(
        {
            "symbol": symbol,
            "ts": dt,
            "open": df.get("open"),
            "high": df.get("high"),
            "low": df.get("low"),
            "close": df.get("close"),
            "volume": df.get("volume", 0).fillna(0).astype("int64"),
        }
    )
    return out


def insert_1m(con, symbol, records) -> int:
    out = _records_df(symbol, records)
    if out is None:
        return 0
    con.execute("DELETE FROM prices_1m WHERE symbol = ?", [symbol])
    con.register("_out_1m", out)
    con.execute(
        "INSERT INTO prices_1m SELECT symbol, ts, open, high, low, close, volume FROM _out_1m"
    )
    con.unregister("_out_1m")
    return len(out)


def insert_daily(con, symbol, records) -> int:
    out = _records_df(symbol, records)
    if out is None:
        return 0
    out = out.assign(bar_date=out["ts"].dt.date).drop(columns=["ts"])
    con.execute("DELETE FROM prices_daily WHERE symbol = ?", [symbol])
    con.register("_out_d", out)
    con.execute(
        "INSERT INTO prices_daily SELECT symbol, bar_date, open, high, low, close, volume FROM _out_d"
    )
    con.unregister("_out_d")
    return len(out)


def main() -> int:
    syms = universe()
    print(f"universe: {len(syms)} NSE equities")

    from database.auth_db import get_first_available_api_key

    api_key = get_first_available_api_key()
    if not api_key:
        print("ERROR: no API key / active broker session", file=sys.stderr)
        return 2

    con = duckdb.connect(str(DB_PATH))
    init_schema(con)
    done = {
        (r[0], r[1])
        for r in con.execute("SELECT symbol, kind FROM fetch_log WHERE status='ok'").fetchall()
    }
    print(f"resume: {len(done)} (symbol, kind) already ok")

    t0 = time.monotonic()
    counters = {"ok": 0, "no_data": 0, "error": 0, "skipped": 0}
    for i, sym in enumerate(syms, 1):
        for kind, interval, start in (("daily", "D", DAILY_START), ("1m", "1m", M1_START)):
            if (sym, kind) in done:
                counters["skipped"] += 1
                continue
            status, detail, records = fetch_one(sym, interval, start, END, api_key)
            if status == "ok":
                if kind == "daily":
                    insert_daily(con, sym, records)
                else:
                    insert_1m(con, sym, records)
            con.execute(
                "INSERT INTO fetch_log VALUES (?,?,?,?,?)",
                [sym, kind, status, detail[:400], datetime.now()],
            )
            counters[status] += 1
            if status != "ok":
                print(f"  {sym} {kind}: {status} — {detail[:120]}")
        if i % 10 == 0:
            el = time.monotonic() - t0
            print(
                f"  ... {i}/{len(syms)} symbols ({el:.0f}s, ok={counters['ok']} "
                f"err={counters['error']} nd={counters['no_data']} skip={counters['skipped']})",
                flush=True,
            )

    print("=== done ===")
    print(counters)
    print(con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM prices_1m").fetchone())
    print(con.execute("SELECT COUNT(*), MIN(bar_date), MAX(bar_date) FROM prices_daily").fetchone())
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
