"""Resume-safe harvester for historical NSE corporate announcements.

Downloads day-by-day from the NSE corporate-announcements API into a DuckDB
file, tracking progress in a `harvest_log` table so a killed/interrupted run
can be resumed without re-fetching days already marked 'ok', and without
duplicating rows already inserted.

Standalone research script — no imports from this project's services/.

Usage:
    uv run python backtest/news_event_study/harvest_announcements.py \
        --from 2025-04-01 --to 2026-04-01 \
        --db outputs/news_event_study/announcements.duckdb
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import requests

NSE_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"
NSE_REFERER_URL = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_REFERER_URL,
}

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = [5, 15, 30]
INTER_DAY_SLEEP_SECONDS = 1.5
PROGRESS_SUMMARY_EVERY_N_DAYS = 20

DEFAULT_DB_PATH = "outputs/news_event_study/announcements.duckdb"


def _fmt_nse_date(d: date) -> str:
    """NSE API wants DD-MM-YYYY."""
    return d.strftime("%d-%m-%Y")


def prime_session(session: requests.Session) -> None:
    """GET the referer page once to pick up NSE's anti-bot cookies.

    Best-effort: NSE occasionally 4xx's this priming request too; we don't
    care about the body or status, only that cookies land in the session jar.
    """
    try:
        session.get(NSE_REFERER_URL, headers=HEADERS, timeout=20)
    except requests.RequestException as exc:
        print(f"  [warn] cookie priming request failed ({exc}); continuing anyway")


def fetch_day(session: requests.Session, day: date) -> list[dict] | None:
    """Fetch announcements for a single calendar day.

    Retries up to MAX_RETRIES times on 401/403/timeout/JSON-decode failure,
    re-priming cookies before each retry. Returns None if all retries fail.
    """
    date_str = _fmt_nse_date(day)
    params = {"index": "equities", "from_date": date_str, "to_date": date_str}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(NSE_ANNOUNCEMENTS_URL, params=params, headers=HEADERS, timeout=20)
            if resp.status_code in (401, 403):
                raise requests.HTTPError(f"status={resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError(f"unexpected JSON shape: {type(data)}")
            return data
        except (requests.RequestException, ValueError) as exc:
            print(f"  [retry {attempt}/{MAX_RETRIES}] {date_str} failed: {exc}")
            if attempt < MAX_RETRIES:
                backoff = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                print(f"    re-priming cookies and retrying in {backoff}s")
                time.sleep(backoff)
                prime_session(session)
            else:
                print(f"  [fail] {date_str} exhausted retries, giving up for today")
                return None
    return None


def _parse_sort_date(value: str | None) -> datetime | None:
    """Parse NSE's `sort_date` field ("2026-07-06 21:40:06") into a datetime."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS announcements (
            seq_id VARCHAR,
            symbol VARCHAR,
            isin VARCHAR,
            company_name VARCHAR,
            category VARCHAR,
            summary_text VARCHAR,
            attachment_url VARCHAR,
            announced_at TIMESTAMP,
            exch_diss_time VARCHAR,
            industry VARCHAR,
            fetch_day DATE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS harvest_log (
            fetch_day DATE,
            n_rows INTEGER,
            status VARCHAR,
            fetched_at TIMESTAMP
        )
        """
    )


def load_done_days(con: duckdb.DuckDBPyConnection) -> set[date]:
    rows = con.execute("SELECT fetch_day FROM harvest_log WHERE status = 'ok'").fetchall()
    return {r[0] for r in rows}


def load_existing_seq_ids(con: duckdb.DuckDBPyConnection) -> set[str]:
    rows = con.execute("SELECT DISTINCT seq_id FROM announcements").fetchall()
    return {r[0] for r in rows if r[0] is not None}


def insert_day_rows(
    con: duckdb.DuckDBPyConnection,
    day: date,
    raw_rows: list[dict],
    existing_seq_ids: set[str],
) -> int:
    """Insert rows for a day, deduping on seq_id. Returns count of rows inserted."""
    to_insert = []
    for r in raw_rows:
        seq_id = r.get("seq_id")
        if seq_id is None:
            # No stable id to dedup on; still record it, but it can't be
            # deduped on a re-run. Fall back to None (accepted trade-off for
            # a research script covering the NSE feed's normal id field).
            pass
        if seq_id is not None and seq_id in existing_seq_ids:
            continue
        to_insert.append(
            (
                seq_id,
                r.get("symbol"),
                r.get("sm_isin"),
                r.get("sm_name"),
                r.get("desc"),
                r.get("attchmntText"),
                r.get("attchmntFile"),
                _parse_sort_date(r.get("sort_date")),
                r.get("exchdisstime"),
                r.get("smIndustry"),
                day,
            )
        )
        if seq_id is not None:
            existing_seq_ids.add(seq_id)

    if not to_insert:
        return 0

    con.executemany(
        """
        INSERT INTO announcements (
            seq_id, symbol, isin, company_name, category, summary_text,
            attachment_url, announced_at, exch_diss_time, industry, fetch_day
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        to_insert,
    )
    return len(to_insert)


def log_day(con: duckdb.DuckDBPyConnection, day: date, n_rows: int, status: str) -> None:
    con.execute(
        "INSERT INTO harvest_log (fetch_day, n_rows, status, fetched_at) VALUES (?, ?, ?, ?)",
        [day, n_rows, status, datetime.now()],
    )


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harvest historical NSE corporate announcements into DuckDB."
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        required=True,
        help="Start date (inclusive), YYYY-MM-DD",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        required=True,
        help="End date (inclusive), YYYY-MM-DD",
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=DEFAULT_DB_PATH,
        help=f"Output DuckDB file path (default: {DEFAULT_DB_PATH})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    start = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    end = datetime.strptime(args.to_date, "%Y-%m-%d").date()
    if start > end:
        print(f"error: --from ({start}) is after --to ({end})")
        return 2

    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Harvesting NSE announcements {start} .. {end} -> {db_path}")

    con = duckdb.connect(str(db_path))
    init_schema(con)

    done_days = load_done_days(con)
    existing_seq_ids = load_existing_seq_ids(con)
    print(
        f"Resume state: {len(done_days)} day(s) already 'ok', "
        f"{len(existing_seq_ids)} seq_id(s) already stored"
    )

    session = requests.Session()
    prime_session(session)

    failed_days: list[str] = []
    total_new_rows = 0
    days_processed_this_run = 0

    all_days = list(daterange(start, end))
    total_days = len(all_days)

    for day in all_days:
        if day in done_days:
            continue

        raw_rows = fetch_day(session, day)

        if raw_rows is None:
            log_day(con, day, 0, "failed")
            failed_days.append(day.isoformat())
            continue

        n_new = insert_day_rows(con, day, raw_rows, existing_seq_ids)
        log_day(con, day, len(raw_rows), "ok")
        total_new_rows += n_new
        days_processed_this_run += 1

        print(f"{day.isoformat()}: {len(raw_rows)} rows ({n_new} new)")

        if days_processed_this_run % PROGRESS_SUMMARY_EVERY_N_DAYS == 0:
            cum = con.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
            print(
                f"  --- progress: {days_processed_this_run} day(s) fetched this run, "
                f"{cum} total rows in DB, {len(failed_days)} failed so far ---"
            )

        time.sleep(INTER_DAY_SLEEP_SECONDS)

    total_rows = con.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
    distinct_symbols = con.execute("SELECT COUNT(DISTINCT symbol) FROM announcements").fetchone()[0]
    distinct_categories = con.execute(
        "SELECT COUNT(DISTINCT category) FROM announcements"
    ).fetchone()[0]

    print()
    print("=== Harvest complete ===")
    print(f"Date range requested : {start} .. {end} ({total_days} calendar days)")
    print(f"Days fetched this run: {days_processed_this_run}")
    print(f"Days skipped (resume): {total_days - days_processed_this_run - len(failed_days)}")
    print(f"New rows this run    : {total_new_rows}")
    print(f"Total rows in DB     : {total_rows}")
    print(f"Distinct symbols     : {distinct_symbols}")
    print(f"Distinct categories  : {distinct_categories}")
    print(f"Failed days ({len(failed_days)}): {failed_days}")

    con.close()
    return 0 if not failed_days else 1


if __name__ == "__main__":
    raise SystemExit(main())
