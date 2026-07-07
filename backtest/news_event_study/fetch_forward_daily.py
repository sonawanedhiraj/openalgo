"""Forward daily-bars fetcher for the news/event-momentum study round R45.

Round R43 (see docs/research/journal and strategies/STRATEGY_REGISTRY.md) studied
price action tightly around each announcement date. R45 needs a much longer
forward daily-bars window per symbol (2025-06-01 -> 2026-07-07) to study
multi-horizon post-event drift, plus NIFTY (NSE_INDEX) over the same range for
market adjustment.

This script is modeled directly on ``fetch_event_prices.py`` — same auth
pattern (``database.auth_db.get_first_available_api_key`` +
``services.history_service.get_history``, interval "D", ``source="api"``,
which enforces the broker's 3 req/sec limit internally) — but is much simpler:
one daily-bars fetch per symbol over a fixed date range, not per (symbol,
event_date) pair.

Input: distinct symbols from ``outputs/news_event_study/announcements.duckdb``
table ``events_filtered`` (read-only). Output: NEW tables
``prices_daily_fwd`` + ``fetch_fwd_log`` in ``outputs/news_event_study/prices.duckdb``
— the existing ``prices_daily`` / ``prices_1m`` / ``fetch_log`` tables from the
R43 fetcher are untouched.

Run with ``uv run python`` from the repo root so ``.env`` loads and the live
``db/openalgo.db`` auth token is readable. Do NOT import ``app``.

    uv run python backtest/news_event_study/fetch_forward_daily.py
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

import duckdb

# This script lives under backtest/news_event_study/ but imports project modules
# (database.*, services.*) that live at the repo root. Ensure the repo root is
# on sys.path regardless of the current working directory the caller invokes
# `uv run python` from.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

EVENTS_DB_PATH = "outputs/news_event_study/announcements.duckdb"
EVENTS_TABLE = "events_filtered"
DEFAULT_DB_PATH = "outputs/news_event_study/prices.duckdb"

EXCHANGE = "NSE"
INDEX_SYMBOL = "NIFTY"
INDEX_EXCHANGE = "NSE_INDEX"

START_DATE = "2025-06-01"
END_DATE = "2026-07-07"

PROGRESS_EVERY_N_SYMBOLS = 100


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS prices_daily_fwd (
            symbol VARCHAR,
            bar_date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume BIGINT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fetch_fwd_log (
            symbol VARCHAR,
            status VARCHAR,
            detail VARCHAR,
            fetched_at TIMESTAMP
        )
        """
    )


def load_ok_symbols(con: duckdb.DuckDBPyConnection) -> set[str]:
    """Return the set of symbols already marked 'ok' in fetch_fwd_log — for resume."""
    rows = con.execute("SELECT symbol FROM fetch_fwd_log WHERE status = 'ok'").fetchall()
    return {r[0] for r in rows}


def log_result(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    status: str,
    detail: str,
) -> None:
    con.execute(
        "INSERT INTO fetch_fwd_log (symbol, status, detail, fetched_at) VALUES (?, ?, ?, ?)",
        [symbol, status, detail[:500], datetime.now()],
    )


# --------------------------------------------------------------------------- #
# Bar insertion (dedup via anti-join)
# --------------------------------------------------------------------------- #
def insert_daily_bars(con: duckdb.DuckDBPyConnection, symbol: str, records: list[dict]) -> int:
    """Insert daily bars for `symbol` into prices_daily_fwd, deduping on
    (symbol, bar_date). Returns rows written."""
    if not records:
        return 0
    rows = []
    for r in records:
        ts = r.get("timestamp")
        if ts is None:
            continue
        if isinstance(ts, (int, float)):
            bar_date = datetime.fromtimestamp(ts).date()
        elif isinstance(ts, datetime):
            bar_date = ts.date()
        elif isinstance(ts, date):
            bar_date = ts
        else:
            bar_date = datetime.strptime(str(ts)[:10], "%Y-%m-%d").date()
        rows.append(
            (
                symbol,
                bar_date,
                r.get("open"),
                r.get("high"),
                r.get("low"),
                r.get("close"),
                int(r.get("volume") or 0),
            )
        )
    if not rows:
        return 0

    con.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _stage_daily_fwd AS SELECT * FROM prices_daily_fwd WHERE 1=0"
    )
    con.execute("DELETE FROM _stage_daily_fwd")
    con.executemany(
        "INSERT INTO _stage_daily_fwd (symbol, bar_date, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    before = con.execute("SELECT COUNT(*) FROM prices_daily_fwd").fetchone()[0]
    con.execute(
        """
        INSERT INTO prices_daily_fwd
        SELECT s.* FROM _stage_daily_fwd s
        ANTI JOIN prices_daily_fwd p ON s.symbol = p.symbol AND s.bar_date = p.bar_date
        """
    )
    after = con.execute("SELECT COUNT(*) FROM prices_daily_fwd").fetchone()[0]
    return after - before


# --------------------------------------------------------------------------- #
# Fetch helper
# --------------------------------------------------------------------------- #
def fetch_one(
    symbol: str,
    exchange: str,
    api_key: str,
) -> tuple[str, str, list[dict]]:
    """Call get_history (interval D) and normalize into (status, detail, records).

    status is one of 'ok' | 'no_data' | 'error'.
    """
    from services.history_service import get_history

    try:
        success, response, _status_code = get_history(
            symbol=symbol,
            exchange=exchange,
            interval="D",
            start_date=START_DATE,
            end_date=END_DATE,
            api_key=api_key,
            source="api",
        )
    except Exception as e:  # never let a per-symbol hiccup crash the run
        return "error", str(e), []

    if not success:
        msg = (response or {}).get("message", "unknown error")
        return "error", msg, []

    records = (response or {}).get("data") or []
    if not records:
        return "no_data", "empty response", []
    return "ok", f"{len(records)} bars", records


def load_symbols_from_events_db(events_db_path: str) -> list[str]:
    con = duckdb.connect(events_db_path, read_only=True)
    try:
        rows = con.execute(f"SELECT DISTINCT symbol FROM {EVENTS_TABLE}").fetchall()
    finally:
        con.close()
    symbols = sorted({str(r[0]).strip().upper() for r in rows if r[0]})
    return symbols


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def process_symbol(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    exchange: str,
    api_key: str,
    ok_symbols: set[str],
    counters: dict[str, int],
) -> None:
    if symbol in ok_symbols:
        counters["skipped"] += 1
        return

    status, detail, records = fetch_one(symbol, exchange, api_key)
    n_written = 0
    if status == "ok":
        n_written = insert_daily_bars(con, symbol, records)
    log_result(con, symbol, status, detail)
    counters[status] += 1
    counters["bars_written"] += n_written


def run(symbols: list[str], db_path: str) -> int:
    from database.auth_db import get_first_available_api_key

    api_key = get_first_available_api_key()
    if not api_key:
        print(
            "ERROR: no API key / active broker session available "
            "(database.auth_db.get_first_available_api_key() returned None). "
            "The broker session may be dead — log in and retry.",
            file=sys.stderr,
        )
        return 2

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(db_path)
    init_schema(con)
    ok_symbols = load_ok_symbols(con)
    print(f"Resume state: {len(ok_symbols)} symbol(s) already 'ok'")

    counters = {
        "ok": 0,
        "no_data": 0,
        "error": 0,
        "skipped": 0,
        "bars_written": 0,
    }

    # NIFTY index first (needed for market adjustment), then the stock universe.
    all_targets = [(INDEX_SYMBOL, INDEX_EXCHANGE)] + [(s, EXCHANGE) for s in symbols]

    t0 = time.monotonic()
    for i, (symbol, exchange) in enumerate(all_targets, start=1):
        process_symbol(con, symbol, exchange, api_key, ok_symbols, counters)
        if i % PROGRESS_EVERY_N_SYMBOLS == 0:
            elapsed = time.monotonic() - t0
            print(
                f"  ... {i}/{len(all_targets)} symbols processed "
                f"(ok={counters['ok']} no_data={counters['no_data']} "
                f"error={counters['error']} skipped={counters['skipped']}, "
                f"{elapsed:.1f}s elapsed, {elapsed / i:.2f}s/symbol avg)",
                flush=True,
            )

    elapsed = time.monotonic() - t0
    print()
    print("=== Fetch complete ===")
    print(f"Symbols processed     : {len(all_targets)}")
    print(f"ok / no_data / error  : {counters['ok']} / {counters['no_data']} / {counters['error']}")
    print(f"skipped (resumed)     : {counters['skipped']}")
    print(f"daily bars written    : {counters['bars_written']}")
    print(
        f"elapsed               : {elapsed:.1f}s ({elapsed / max(len(all_targets), 1):.2f}s/symbol)"
    )

    con.close()
    return 0 if counters["error"] == 0 else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch forward daily bars (2025-06-01 -> 2026-07-07) for the "
        "R45 news-event multi-horizon study, into a NEW prices_daily_fwd table."
    )
    parser.add_argument(
        "--events-db",
        dest="events_db",
        default=EVENTS_DB_PATH,
        help="DuckDB file to read distinct symbols from (table events_filtered)",
    )
    parser.add_argument("--db", dest="db_path", default=DEFAULT_DB_PATH, help="Output DuckDB path")
    parser.add_argument(
        "--symbols",
        dest="symbols",
        help="Comma-separated symbols for ad-hoc smoke testing (bypasses --events-db)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = load_symbols_from_events_db(args.events_db)

    if not symbols:
        print("error: no symbols resolved from input", file=sys.stderr)
        return 2

    print(
        f"Loaded {len(symbols)} distinct symbol(s) from {args.events_db}; output -> {args.db_path}"
    )
    return run(symbols, args.db_path)


if __name__ == "__main__":
    raise SystemExit(main())
