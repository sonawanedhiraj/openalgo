"""Resume-safe price-data fetcher for the news/event-momentum study (R43).

For each (symbol, event_date) pair, fetches from the broker's historical API:
  (a) 1m bars for event_date AND the next trading day (next weekday, simplified —
      if the following weekday turns out to be a market holiday the 1m fetch will
      come back empty and is logged as no_data; the simulator handles the gap).
  (b) daily bars for [event_date - 90 calendar days, event_date + 3 days].

Results are stored in DuckDB (default outputs/news_event_study/prices.duckdb).
This script is READ-ONLY on every live OpenAlgo database (openalgo.db is only
read, via database.auth_db / database.symbol, to resolve the broker auth token
and validate symbols) — the only thing it writes is the output DuckDB file.

Input design (pick ONE, both are supported)
---------------------------------------------
1. CSV file with columns `symbol,event_date` (event_date as YYYY-MM-DD):

       uv run python backtest/news_event_study/fetch_event_prices.py \
           --events outputs/news_event_study/events.csv

2. A SQL query against an existing DuckDB file (e.g. the announcements harvest
   or a curated event-study table), returning columns named `symbol` and
   `event_date`:

       uv run python backtest/news_event_study/fetch_event_prices.py \
           --events-db outputs/news_event_study/announcements.duckdb \
           --events-sql "SELECT symbol, CAST(fetch_day AS VARCHAR) AS event_date FROM announcements WHERE category = 'Result'"

Ad-hoc smoke testing (bypasses --events entirely):

       uv run python backtest/news_event_study/fetch_event_prices.py \
           --symbols RELIANCE,TATAMOTORS --date 2026-06-24

Fetch pattern
--------------
Mirrors ``services/sector_follow_stock_backfill.py`` / the broker-API arm of
``services/history_service.get_history``: auth resolves via
``database.auth_db.get_first_available_api_key()`` (keyed off whichever user
has a non-revoked ``Auth`` row with a broker configured — NOT a hardcoded
'zerodha' string) and ``get_history(..., api_key=...)``. ``get_history``
already enforces the broker's 3 req/sec rate limit internally
(``_enforce_rate_limit`` in ``services/history_service.py``) so no extra sleep
is needed between calls here.

Run with ``uv run python`` from the repo root so ``.env`` loads and the live
``db/openalgo.db`` auth token is readable. Do NOT import ``app`` (booting the
full Flask app conflicts with the already-running dev-server instance) — this
script only imports the database/service modules it needs.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

# This script lives under backtest/news_event_study/ but imports project modules
# (database.*, services.*) that live at the repo root. Ensure the repo root is
# on sys.path regardless of the current working directory the caller invokes
# `uv run python` from.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_DB_PATH = "outputs/news_event_study/prices.duckdb"
EXCHANGE = "NSE"
DAILY_LOOKBACK_DAYS = 90
DAILY_LOOKAHEAD_DAYS = 3
PROGRESS_EVERY_N_EVENTS = 25


@dataclass(frozen=True)
class EventRow:
    symbol: str
    event_date: date


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS prices_1m (
            symbol VARCHAR,
            ts TIMESTAMP,
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
        CREATE TABLE IF NOT EXISTS prices_daily (
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
        CREATE TABLE IF NOT EXISTS fetch_log (
            symbol VARCHAR,
            event_date DATE,
            kind VARCHAR,
            status VARCHAR,
            detail VARCHAR,
            fetched_at TIMESTAMP
        )
        """
    )


def load_ok_log(con: duckdb.DuckDBPyConnection) -> set[tuple[str, date, str]]:
    """Return the set of (symbol, event_date, kind) already marked 'ok' — for resume."""
    rows = con.execute(
        "SELECT symbol, event_date, kind FROM fetch_log WHERE status = 'ok'"
    ).fetchall()
    return {(r[0], r[1], r[2]) for r in rows}


def log_result(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    event_date: date,
    kind: str,
    status: str,
    detail: str,
) -> None:
    con.execute(
        "INSERT INTO fetch_log (symbol, event_date, kind, status, detail, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [symbol, event_date, kind, status, detail[:500], datetime.now()],
    )


# --------------------------------------------------------------------------- #
# Bar insertion (dedup via anti-join)
# --------------------------------------------------------------------------- #
def insert_1m_bars(con: duckdb.DuckDBPyConnection, symbol: str, records: list[dict]) -> int:
    """Insert 1m bars for `symbol`, deduping on (symbol, ts). Returns rows written."""
    if not records:
        return 0
    rows = []
    for r in records:
        ts = r.get("timestamp")
        if ts is None:
            continue
        # broker df 'timestamp' column may be int epoch or datetime-like; normalize.
        if isinstance(ts, (int, float)):
            ts_dt = datetime.fromtimestamp(ts)
        else:
            ts_dt = ts
        rows.append(
            (
                symbol,
                ts_dt,
                r.get("open"),
                r.get("high"),
                r.get("low"),
                r.get("close"),
                int(r.get("volume") or 0),
            )
        )
    if not rows:
        return 0

    con.execute("CREATE TEMP TABLE IF NOT EXISTS _stage_1m AS SELECT * FROM prices_1m WHERE 1=0")
    con.execute("DELETE FROM _stage_1m")
    con.executemany(
        "INSERT INTO _stage_1m (symbol, ts, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    before = con.execute("SELECT COUNT(*) FROM prices_1m").fetchone()[0]
    con.execute(
        """
        INSERT INTO prices_1m
        SELECT s.* FROM _stage_1m s
        ANTI JOIN prices_1m p ON s.symbol = p.symbol AND s.ts = p.ts
        """
    )
    after = con.execute("SELECT COUNT(*) FROM prices_1m").fetchone()[0]
    return after - before


def insert_daily_bars(con: duckdb.DuckDBPyConnection, symbol: str, records: list[dict]) -> int:
    """Insert daily bars for `symbol`, deduping on (symbol, bar_date). Returns rows written."""
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
        "CREATE TEMP TABLE IF NOT EXISTS _stage_daily AS SELECT * FROM prices_daily WHERE 1=0"
    )
    con.execute("DELETE FROM _stage_daily")
    con.executemany(
        "INSERT INTO _stage_daily (symbol, bar_date, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    before = con.execute("SELECT COUNT(*) FROM prices_daily").fetchone()[0]
    con.execute(
        """
        INSERT INTO prices_daily
        SELECT s.* FROM _stage_daily s
        ANTI JOIN prices_daily p ON s.symbol = p.symbol AND s.bar_date = p.bar_date
        """
    )
    after = con.execute("SELECT COUNT(*) FROM prices_daily").fetchone()[0]
    return after - before


# --------------------------------------------------------------------------- #
# Fetch helpers
# --------------------------------------------------------------------------- #
def next_weekday(d: date) -> date:
    """Next calendar weekday after `d` (Mon-Fri). Simplified — does not consult
    the NSE holiday calendar; a holiday next-day will simply come back empty
    from the broker and is logged as no_data (the simulator handles gaps)."""
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def fetch_one(
    symbol: str,
    exchange: str,
    interval: str,
    start_date: str,
    end_date: str,
    api_key: str,
) -> tuple[str, str, list[dict]]:
    """Call get_history and normalize into (status, detail, records).

    status is one of 'ok' | 'no_data' | 'error'.
    """
    from services.history_service import get_history

    try:
        success, response, _status_code = get_history(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            start_date=start_date,
            end_date=end_date,
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


# --------------------------------------------------------------------------- #
# Event source loading
# --------------------------------------------------------------------------- #
def load_events_from_csv(path: str) -> list[EventRow]:
    events: list[EventRow] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = (row.get("symbol") or "").strip().upper()
            ev = (row.get("event_date") or "").strip()
            if not sym or not ev:
                continue
            events.append(EventRow(symbol=sym, event_date=datetime.strptime(ev, "%Y-%m-%d").date()))
    return events


def load_events_from_sql(db_path: str, sql: str) -> list[EventRow]:
    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute(sql).fetchall()
        cols = [d[0] for d in con.description]
    finally:
        con.close()
    sym_idx = cols.index("symbol")
    date_idx = cols.index("event_date")
    events: list[EventRow] = []
    for r in rows:
        sym = str(r[sym_idx]).strip().upper()
        ev_raw = r[date_idx]
        if isinstance(ev_raw, date):
            ev = ev_raw if not isinstance(ev_raw, datetime) else ev_raw.date()
        else:
            ev = datetime.strptime(str(ev_raw)[:10], "%Y-%m-%d").date()
        if not sym:
            continue
        events.append(EventRow(symbol=sym, event_date=ev))
    return events


# --------------------------------------------------------------------------- #
# Main per-event processing
# --------------------------------------------------------------------------- #
def process_event(
    con: duckdb.DuckDBPyConnection,
    ev: EventRow,
    api_key: str,
    ok_log: set[tuple[str, date, str]],
    counters: dict[str, int],
) -> None:
    symbol, event_date = ev.symbol, ev.event_date

    # --- 1m bars: event_date and next trading (week)day ---
    if (symbol, event_date, "1m") in ok_log:
        counters["skipped"] += 1
    else:
        nxt = next_weekday(event_date)
        start_1m = event_date.strftime("%Y-%m-%d")
        end_1m = nxt.strftime("%Y-%m-%d")
        status, detail, records = fetch_one(symbol, EXCHANGE, "1m", start_1m, end_1m, api_key)
        n_written = 0
        if status == "ok":
            n_written = insert_1m_bars(con, symbol, records)
        log_result(con, symbol, event_date, "1m", status, detail)
        counters[status] += 1
        counters["bars_1m"] += n_written

    # --- daily bars: [event_date - 90d, event_date + 3d] ---
    if (symbol, event_date, "daily") in ok_log:
        counters["skipped"] += 1
    else:
        start_d = (event_date - timedelta(days=DAILY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        end_d = (event_date + timedelta(days=DAILY_LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
        status, detail, records = fetch_one(symbol, EXCHANGE, "D", start_d, end_d, api_key)
        n_written = 0
        if status == "ok":
            n_written = insert_daily_bars(con, symbol, records)
        log_result(con, symbol, event_date, "daily", status, detail)
        counters[status] += 1
        counters["bars_daily"] += n_written


def run(events: list[EventRow], db_path: str) -> int:
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
    ok_log = load_ok_log(con)
    print(f"Resume state: {len(ok_log)} (symbol, event_date, kind) already 'ok'")

    counters = {
        "ok": 0,
        "no_data": 0,
        "error": 0,
        "skipped": 0,
        "bars_1m": 0,
        "bars_daily": 0,
    }

    t0 = time.monotonic()
    for i, ev in enumerate(events, start=1):
        process_event(con, ev, api_key, ok_log, counters)
        if i % PROGRESS_EVERY_N_EVENTS == 0:
            elapsed = time.monotonic() - t0
            print(
                f"  ... {i}/{len(events)} events processed "
                f"(ok={counters['ok']} no_data={counters['no_data']} "
                f"error={counters['error']} skipped={counters['skipped']}, "
                f"{elapsed:.1f}s elapsed, {elapsed / i:.2f}s/event avg)"
            )

    elapsed = time.monotonic() - t0
    print()
    print("=== Fetch complete ===")
    print(f"Events processed     : {len(events)}")
    print(f"ok / no_data / error : {counters['ok']} / {counters['no_data']} / {counters['error']}")
    print(f"skipped (resumed)    : {counters['skipped']}")
    print(f"1m bars written      : {counters['bars_1m']}")
    print(f"daily bars written   : {counters['bars_daily']}")
    print(f"elapsed              : {elapsed:.1f}s ({elapsed / max(len(events), 1):.2f}s/event)")

    con.close()
    return 0 if counters["error"] == 0 else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch 1m + daily price bars around news/event dates into DuckDB."
    )
    parser.add_argument(
        "--events", dest="events_csv", help="CSV file with columns symbol,event_date"
    )
    parser.add_argument(
        "--events-sql", dest="events_sql", help="SQL query returning columns symbol, event_date"
    )
    parser.add_argument(
        "--events-db", dest="events_db", help="DuckDB file path to run --events-sql against"
    )
    parser.add_argument(
        "--symbols", dest="symbols", help="Comma-separated symbols for ad-hoc smoke testing"
    )
    parser.add_argument("--date", dest="ad_hoc_date", help="Event date YYYY-MM-DD for --symbols")
    parser.add_argument("--db", dest="db_path", default=DEFAULT_DB_PATH, help="Output DuckDB path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    events: list[EventRow]
    if args.symbols and args.ad_hoc_date:
        ev_date = datetime.strptime(args.ad_hoc_date, "%Y-%m-%d").date()
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        events = [EventRow(symbol=s, event_date=ev_date) for s in syms]
    elif args.events_csv:
        events = load_events_from_csv(args.events_csv)
    elif args.events_sql and args.events_db:
        events = load_events_from_sql(args.events_db, args.events_sql)
    else:
        print(
            "error: provide one of --symbols/--date, --events <csv>, or --events-sql/--events-db",
            file=sys.stderr,
        )
        return 2

    if not events:
        print("error: no events resolved from input", file=sys.stderr)
        return 2

    print(f"Loaded {len(events)} event row(s); output -> {args.db_path}")
    return run(events, args.db_path)


if __name__ == "__main__":
    raise SystemExit(main())
