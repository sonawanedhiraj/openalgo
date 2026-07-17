"""Resume-safe harvester for historical NSE insider-trading (PIT) disclosures.

Downloads day-by-day from the NSE corporates-pit API (SEBI PIT/insider-trading
disclosures: Regulation 7(2) filings by promoters/directors/employees/other
"designated persons") into the SAME DuckDB file the announcements harvester
uses, adding new tables (`insider_pit`, `harvest_log_pit`) so both datasets can
be joined by symbol/date without juggling two files.

Modeled directly on `harvest_announcements.py`: same session/UA/cookie-prime
pattern (only the Referer path differs), same retry/backoff, same resume-safe
day-by-day design. The API has no stable per-record id, so dedup keys off
`record_hash` = md5 of the record's canonical (sorted-key) JSON string instead
of a `seq_id`.

Standalone research script — no imports from this project's services/.

Usage:
    uv run python backtest/news_event_study/harvest_insider_pit.py \
        --from 2025-07-01 --to 2026-07-07 \
        --db outputs/news_event_study/announcements.duckdb
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import requests

NSE_PIT_URL = "https://www.nseindia.com/api/corporates-pit"
NSE_REFERER_URL = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"

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

# All fields observed in a live May-2026 response (verified 2026-07-07). Kept
# in full so nothing is lost; raw_json carries the record verbatim as a
# backstop against any field NSE adds/removes later.
RAW_FIELD_ORDER = [
    "symbol",
    "company",
    "acqName",
    "personCategory",
    "secType",
    "securitiesTypePost",
    "secAcq",
    "secVal",
    "tdpTransactionType",
    "acqMode",
    "befAcqSharesNo",
    "befAcqSharesPer",
    "afterAcqSharesNo",
    "afterAcqSharesPer",
    "buyQuantity",
    "buyValue",
    "sellquantity",
    "sellValue",
    "acqfromDt",
    "acqtoDt",
    "intimDt",
    "date",
    "exchange",
    "derivativeType",
    "tdpDerivativeContractType",
    "anex",
    "did",
    "pid",
    "remarks",
    "tkdAcqm",
    "xbrl",
    "xbrlFileSize",
]


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
    """Fetch insider-PIT disclosures for a single calendar day.

    Retries up to MAX_RETRIES times on 401/403/timeout/JSON-decode failure,
    re-priming cookies before each retry. Returns None if all retries fail.
    An empty `data` list is a legitimate result (weekend/no filings) and is
    returned as `[]`, not None.
    """
    date_str = _fmt_nse_date(day)
    params = {"index": "equities", "from_date": date_str, "to_date": date_str}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(NSE_PIT_URL, params=params, headers=HEADERS, timeout=20)
            if resp.status_code in (401, 403):
                raise requests.HTTPError(f"status={resp.status_code}")
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict) or "data" not in payload:
                raise ValueError(f"unexpected JSON shape: {type(payload)}")
            data = payload["data"]
            if not isinstance(data, list):
                raise ValueError(f"unexpected 'data' shape: {type(data)}")
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


def _parse_nse_date(value: str | None, fmt: str = "%d-%b-%Y") -> date | None:
    if not value or value == "-":
        return None
    try:
        return datetime.strptime(value.strip(), fmt).date()
    except ValueError:
        return None


def _parse_nse_datetime(value: str | None) -> datetime | None:
    """Parse the `date` field ("02-May-2026 16:46") -> datetime."""
    if not value or value == "-":
        return None
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _to_int(value) -> int | None:
    if value is None or value == "-" or value == "":
        return None
    try:
        return int(str(value).replace(",", ""))
    except ValueError:
        return None


def _to_float(value) -> float | None:
    if value is None or value == "-" or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def record_hash(record: dict) -> str:
    """md5 of the record's canonical (sorted-key) JSON string.

    This is the only stable dedup key available: the API exposes no
    consistent per-record sequence id across the fields we've observed.
    """
    canonical = json.dumps(record, sort_keys=True, default=str)
    return hashlib.md5(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS insider_pit (
            record_hash VARCHAR,
            symbol VARCHAR,
            company VARCHAR,
            acq_name VARCHAR,
            person_category VARCHAR,
            security_type VARCHAR,
            security_type_post VARCHAR,
            sec_acq_qty BIGINT,
            sec_val DOUBLE,
            transaction_type VARCHAR,
            acq_mode VARCHAR,
            bef_acq_shares_no BIGINT,
            bef_acq_shares_pct DOUBLE,
            after_acq_shares_no BIGINT,
            after_acq_shares_pct DOUBLE,
            buy_quantity BIGINT,
            buy_value DOUBLE,
            sell_quantity BIGINT,
            sell_value DOUBLE,
            acq_from_date DATE,
            acq_to_date DATE,
            intimation_date DATE,
            broadcast_at TIMESTAMP,
            exchange VARCHAR,
            derivative_type VARCHAR,
            derivative_contract_type VARCHAR,
            annexure VARCHAR,
            did VARCHAR,
            pid VARCHAR,
            remarks VARCHAR,
            tkd_acqm VARCHAR,
            xbrl_url VARCHAR,
            xbrl_file_size VARCHAR,
            fetch_day DATE,
            raw_json VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS harvest_log_pit (
            fetch_day DATE,
            n_rows INTEGER,
            status VARCHAR,
            fetched_at TIMESTAMP
        )
        """
    )


def load_done_days(con: duckdb.DuckDBPyConnection) -> set[date]:
    rows = con.execute("SELECT fetch_day FROM harvest_log_pit WHERE status = 'ok'").fetchall()
    return {r[0] for r in rows}


def load_existing_hashes(con: duckdb.DuckDBPyConnection) -> set[str]:
    rows = con.execute("SELECT DISTINCT record_hash FROM insider_pit").fetchall()
    return {r[0] for r in rows if r[0] is not None}


def insert_day_rows(
    con: duckdb.DuckDBPyConnection,
    day: date,
    raw_rows: list[dict],
    existing_hashes: set[str],
) -> int:
    """Insert rows for a day, deduping on record_hash. Returns count inserted."""
    to_insert = []
    for r in raw_rows:
        h = record_hash(r)
        if h in existing_hashes:
            continue

        to_insert.append(
            (
                h,
                r.get("symbol"),
                r.get("company"),
                r.get("acqName"),
                r.get("personCategory"),
                r.get("secType"),
                r.get("securitiesTypePost"),
                _to_int(r.get("secAcq")),
                _to_float(r.get("secVal")),
                r.get("tdpTransactionType"),
                r.get("acqMode"),
                _to_int(r.get("befAcqSharesNo")),
                _to_float(r.get("befAcqSharesPer")),
                _to_int(r.get("afterAcqSharesNo")),
                _to_float(r.get("afterAcqSharesPer")),
                _to_int(r.get("buyQuantity")),
                _to_float(r.get("buyValue")),
                _to_int(r.get("sellquantity")),
                _to_float(r.get("sellValue")),
                _parse_nse_date(r.get("acqfromDt")),
                _parse_nse_date(r.get("acqtoDt")),
                _parse_nse_date(r.get("intimDt")),
                _parse_nse_datetime(r.get("date")),
                r.get("exchange"),
                r.get("derivativeType"),
                r.get("tdpDerivativeContractType"),
                r.get("anex"),
                r.get("did"),
                r.get("pid"),
                r.get("remarks"),
                r.get("tkdAcqm"),
                r.get("xbrl"),
                r.get("xbrlFileSize"),
                day,
                json.dumps(r, sort_keys=True, default=str),
            )
        )
        existing_hashes.add(h)

    if not to_insert:
        return 0

    con.executemany(
        """
        INSERT INTO insider_pit (
            record_hash, symbol, company, acq_name, person_category,
            security_type, security_type_post, sec_acq_qty, sec_val,
            transaction_type, acq_mode, bef_acq_shares_no, bef_acq_shares_pct,
            after_acq_shares_no, after_acq_shares_pct, buy_quantity, buy_value,
            sell_quantity, sell_value, acq_from_date, acq_to_date,
            intimation_date, broadcast_at, exchange, derivative_type,
            derivative_contract_type, annexure, did, pid, remarks, tkd_acqm,
            xbrl_url, xbrl_file_size, fetch_day, raw_json
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        to_insert,
    )
    return len(to_insert)


def log_day(con: duckdb.DuckDBPyConnection, day: date, n_rows: int, status: str) -> None:
    con.execute(
        "INSERT INTO harvest_log_pit (fetch_day, n_rows, status, fetched_at) VALUES (?, ?, ?, ?)",
        [day, n_rows, status, datetime.now()],
    )


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harvest historical NSE insider-trading (PIT) disclosures into DuckDB."
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

    print(f"Harvesting NSE insider-PIT disclosures {start} .. {end} -> {db_path}")

    con = duckdb.connect(str(db_path))
    init_schema(con)

    done_days = load_done_days(con)
    existing_hashes = load_existing_hashes(con)
    print(
        f"Resume state: {len(done_days)} day(s) already 'ok', "
        f"{len(existing_hashes)} record_hash(es) already stored"
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

        n_new = insert_day_rows(con, day, raw_rows, existing_hashes)
        log_day(con, day, len(raw_rows), "ok")
        total_new_rows += n_new
        days_processed_this_run += 1

        print(f"{day.isoformat()}: {len(raw_rows)} rows ({n_new} new)")

        if days_processed_this_run % PROGRESS_SUMMARY_EVERY_N_DAYS == 0:
            cum = con.execute("SELECT COUNT(*) FROM insider_pit").fetchone()[0]
            print(
                f"  --- progress: {days_processed_this_run} day(s) fetched this run, "
                f"{cum} total rows in DB, {len(failed_days)} failed so far ---"
            )

        time.sleep(INTER_DAY_SLEEP_SECONDS)

    total_rows = con.execute("SELECT COUNT(*) FROM insider_pit").fetchone()[0]
    distinct_symbols = con.execute("SELECT COUNT(DISTINCT symbol) FROM insider_pit").fetchone()[0]
    distinct_categories = con.execute(
        "SELECT COUNT(DISTINCT person_category) FROM insider_pit"
    ).fetchone()[0]

    print()
    print("=== Harvest complete ===")
    print(f"Date range requested : {start} .. {end} ({total_days} calendar days)")
    print(f"Days fetched this run: {days_processed_this_run}")
    print(f"Days skipped (resume): {total_days - days_processed_this_run - len(failed_days)}")
    print(f"New rows this run    : {total_new_rows}")
    print(f"Total rows in DB     : {total_rows}")
    print(f"Distinct symbols     : {distinct_symbols}")
    print(f"Distinct person categories: {distinct_categories}")
    print(f"Failed days ({len(failed_days)}): {failed_days}")

    con.close()
    return 0 if not failed_days else 1


if __name__ == "__main__":
    raise SystemExit(main())
