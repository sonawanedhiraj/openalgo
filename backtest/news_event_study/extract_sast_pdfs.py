"""R46 pilot: download SAST (Substantial Acquisition of Shares and Takeovers)
disclosure PDFs from NSE and extract transaction direction + size.

Background
----------
``announcements.duckdb``'s ``announcements`` table has ~4,711 rows with
category = 'Disclosure under SEBI Takeover Regulations'. The feed's
``summary_text`` is boilerplate ("X has submitted to the Exchange a copy of
Disclosures under Regulation ..."), it never says whether the underlying
transaction is an acquisition, a disposal, a pledge/invocation, or an
inter-se transfer, nor the quantity / %-of-equity involved. That detail is
only inside the linked PDF (``attachment_url``, occasionally a .zip
containing the PDF), which is normally a standardised SEBI Reg 29(1) / 29(2)
/ 31 tabular disclosure form.

This script is the PILOT: it samples a small number of these filings, proves
the download + text-extraction + direction-classification pipeline works,
and reports pilot metrics so the parent session can decide whether it is
worth scaling to the full ~4,700-row universe. It intentionally does NOT run
--all.

PDF backend
-----------
None of pypdf / pdfplumber / PyMuPDF (fitz) are installed in the project's
uv-managed environment (verified 2026-07-07). Rather than `uv add` a new
runtime dependency for a pilot, run this script with an ephemeral dependency:

    uv run --with pypdf python backtest/news_event_study/extract_sast_pdfs.py --sample 300

The script will happily use pdfplumber or fitz instead if either is already
importable (checked in that order: pypdf, pdfplumber, fitz) — whichever
comes first wins. If none is importable, it prints an error and exits
without downloading anything.

Usage
-----
    uv run --with pypdf python backtest/news_event_study/extract_sast_pdfs.py --sample 300
    uv run --with pypdf python backtest/news_event_study/extract_sast_pdfs.py --all   # NOT for the pilot

Output: ``outputs/news_event_study/sast_extractions.duckdb`` table
``sast_extract``, plus downloaded PDFs cached (resume-safe) under
``outputs/news_event_study/sast_pdfs/<seq_id>.pdf``.

Standalone research script — no imports from this project's services/, and
read-only against announcements.duckdb.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

import duckdb
import requests

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
ANNOUNCEMENTS_DB = "outputs/news_event_study/announcements.duckdb"
DEFAULT_OUT_DB = "outputs/news_event_study/sast_extractions.duckdb"
PDF_DIR = Path("outputs/news_event_study/sast_pdfs")
CATEGORY = "Disclosure under SEBI Takeover Regulations"
STRATIFY_CUTOFF = date(2026, 1, 1)
RANDOM_SEED = 42

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.nseindia.com/",
}

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = [3, 8, 15]
INTER_DOWNLOAD_SLEEP_SECONDS = 1.0
PROGRESS_EVERY_N = 25

# --------------------------------------------------------------------------- #
# Classification rules
# --------------------------------------------------------------------------- #
# Strip the regulation's own title before matching so "(Substantial
# Acquisition of Shares and Takeovers)" never counts as a buy cue.
TITLE_STRIP_RE = re.compile(
    r"\(\s*Substantial\s+Acquisition\s+of\s+Shares\s+and\s+Takeovers\s*\)",
    re.IGNORECASE,
)

BUY_PATTERNS = [
    r"\backquisitions?\b",  # (kept for OCR typo tolerance, harmless no-op)
    r"\bacquisitions?\b",
    r"\backquir\w*\b",
    r"\bacquir\w*\b",
    r"\bpurchase\w*\b",
    r"\bbought\b",
    r"\bmarket purchase\b",
    r"\bsubscription\b",
    r"\ballotment\b",
]
SELL_PATTERNS = [
    r"\bdisposal\w*\b",
    r"\bdispos\w*\b",
    r"\bsale\w*\b",
    r"\bsold\b",
    r"\bmarket sale\b",
]
PLEDGE_PATTERNS = [
    r"\bpledge\w*\b",
    r"\binvocation\w*\b",
    r"\binvok\w*\b",
    r"\bencumbrance\w*\b",
    r"\brelease of pledge\b",
]
TRANSFER_PATTERNS = [
    r"\binter-se\b",
    r"\binter se transfer\b",
    r"\bgift\w*\b",
]

MODE_CONTEXT_RE = re.compile(r"(mode of|nature of)", re.IGNORECASE)
MODE_WINDOW_RADIUS = 120
CUE_SNIPPET_RADIUS = 150

PCT_RE = re.compile(r"(\d{1,3}(?:\.\d{1,4})?)\s*%")
PCT_CONTEXT_RADIUS = 60

SHARES_RE = re.compile(r"\b([\d,]{3,})\s*(?:Equity\s+)?Shares\b", re.IGNORECASE)


def strip_title(text: str) -> str:
    return TITLE_STRIP_RE.sub(" ", text)


def _any_match(window: str, patterns: list[str]) -> bool:
    return any(re.search(p, window, re.IGNORECASE) for p in patterns)


def classify_window(window: str) -> str | None:
    """Classify a text window into buy/sell/pledge/transfer/mixed/None."""
    has_buy = _any_match(window, BUY_PATTERNS)
    has_sell = _any_match(window, SELL_PATTERNS)
    has_pledge = _any_match(window, PLEDGE_PATTERNS)
    has_transfer = _any_match(window, TRANSFER_PATTERNS)
    if has_buy and has_sell:
        return "mixed"
    if has_pledge:
        return "pledge"
    if has_transfer:
        return "transfer"
    if has_buy:
        return "buy"
    if has_sell:
        return "sell"
    return None


def get_mode_windows(text: str, radius: int = MODE_WINDOW_RADIUS) -> list[str]:
    windows = []
    for m in MODE_CONTEXT_RE.finditer(text):
        s = max(0, m.start() - radius)
        e = min(len(text), m.end() + radius)
        windows.append(text[s:e])
    return windows


def find_first_cue_window(text: str, radius: int = CUE_SNIPPET_RADIUS) -> str:
    all_patterns = BUY_PATTERNS + SELL_PATTERNS + PLEDGE_PATTERNS + TRANSFER_PATTERNS
    for p in all_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            s = max(0, m.start() - radius)
            e = min(len(text), m.end() + radius)
            return text[s:e]
    return text[:300]


def extract_pct_before_after(text: str) -> tuple[float | None, float | None]:
    """Best-effort extraction of %-of-equity before/after the transaction.

    Heuristic: scan every "<number>%" occurrence, look at a window of chars
    immediately around it, and tag it 'before' or 'after' based on which
    keyword is nearer in that window. First 'before' and first 'after' found
    win. This is a heuristic tiebreak/validation signal, not a guaranteed
    parse — table layouts vary and PDF text extraction can interleave labels
    and values unpredictably.
    """
    before_val: float | None = None
    after_val: float | None = None
    for m in PCT_RE.finditer(text):
        val = float(m.group(1))
        ctx = (
            text[max(0, m.start() - PCT_CONTEXT_RADIUS) : m.start()]
            + " "
            + text[m.end() : m.end() + PCT_CONTEXT_RADIUS]
        ).lower()
        has_before = "before" in ctx
        has_after = "after" in ctx
        if has_before and not has_after and before_val is None:
            before_val = val
        elif has_after and not has_before and after_val is None:
            after_val = val
        if before_val is not None and after_val is not None:
            break
    return before_val, after_val


def extract_shares_qty(text: str) -> int | None:
    m = SHARES_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def pct_context_snippet(text: str, radius: int = CUE_SNIPPET_RADIUS) -> str:
    m = PCT_RE.search(text)
    if not m:
        return text[:300]
    s = max(0, m.start() - radius)
    e = min(len(text), m.end() + radius)
    return text[s:e]


def classify_document(raw_text: str) -> dict:
    """Run the layered classifier over one document's extracted text.

    Returns a dict with keys: direction, direction_source, pct_before,
    pct_after, shares_qty, snippet.
    """
    text = strip_title(raw_text)

    text_direction: str | None = None
    text_source: str | None = None
    text_snippet: str | None = None

    mode_windows = get_mode_windows(text)
    if mode_windows:
        combined = " ... ".join(mode_windows)
        d = classify_window(combined)
        if d:
            text_direction = d
            text_source = "mode_window"
            text_snippet = combined[:300]

    if text_direction is None:
        d = classify_window(text)
        if d:
            text_direction = d
            text_source = "fulltext"
            text_snippet = find_first_cue_window(text)[:300]

    pct_before, pct_after = extract_pct_before_after(text)
    shares_qty = extract_shares_qty(text)

    pct_direction: str | None = None
    if pct_before is not None and pct_after is not None:
        if pct_after > pct_before:
            pct_direction = "buy"
        elif pct_after < pct_before:
            pct_direction = "sell"
        else:
            pct_direction = "unchanged"

    if text_direction is not None:
        direction = text_direction
        direction_source = text_source
        snippet = text_snippet
    elif pct_direction in ("buy", "sell"):
        direction = pct_direction
        direction_source = "pct_delta"
        snippet = pct_context_snippet(text)
    else:
        direction = None
        direction_source = "failed"
        snippet = text[:300] if text else None

    return {
        "direction": direction,
        "direction_source": direction_source,
        "text_direction": text_direction,
        "pct_direction": pct_direction,
        "pct_before": pct_before,
        "pct_after": pct_after,
        "shares_qty": shares_qty,
        "snippet": snippet,
    }


# --------------------------------------------------------------------------- #
# PDF backend detection + text extraction
# --------------------------------------------------------------------------- #
def detect_pdf_backend() -> str | None:
    try:
        import pypdf  # noqa: F401

        return "pypdf"
    except ImportError:
        pass
    try:
        import pdfplumber  # noqa: F401

        return "pdfplumber"
    except ImportError:
        pass
    try:
        import fitz  # noqa: F401

        return "fitz"
    except ImportError:
        pass
    return None


def extract_text(pdf_path: Path, backend: str) -> str:
    if backend == "pypdf":
        import pypdf

        reader = pypdf.PdfReader(str(pdf_path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if backend == "pdfplumber":
        import pdfplumber

        parts = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(parts)
    if backend == "fitz":
        import fitz

        doc = fitz.open(str(pdf_path))
        try:
            return "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
    raise RuntimeError(f"unknown pdf backend: {backend}")


# --------------------------------------------------------------------------- #
# Download (resume-safe: skip if the final .pdf already exists)
# --------------------------------------------------------------------------- #
def ensure_pdf(seq_id: str, url: str, session: requests.Session) -> tuple[str, Path | None, str]:
    """Download `url` (PDF or ZIP-containing-PDF) to PDF_DIR/<seq_id>.pdf.

    Returns (status, pdf_path_or_None, detail). status is one of:
    'skip_exists' | 'downloaded' | 'failed'.
    """
    pdf_path = PDF_DIR / f"{seq_id}.pdf"
    if pdf_path.exists():
        return "skip_exists", pdf_path, "already downloaded"

    tail = url.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1].lower() if "." in tail else "pdf"

    content: bytes | None = None
    detail = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            content = resp.content
            break
        except requests.RequestException as exc:
            detail = str(exc)
            if attempt < MAX_RETRIES:
                backoff = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                time.sleep(backoff)

    if content is None:
        return "failed", None, f"download failed: {detail}"

    if ext == "zip":
        raw_path = PDF_DIR / f"{seq_id}_raw.zip"
        raw_path.write_bytes(content)
        try:
            with zipfile.ZipFile(raw_path) as zf:
                pdf_names = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
                if not pdf_names:
                    return "failed", None, "zip contains no pdf entry"
                data = zf.read(pdf_names[0])
                pdf_path.write_bytes(data)
        except zipfile.BadZipFile as exc:
            return "failed", None, f"bad zip: {exc}"
        finally:
            raw_path.unlink(missing_ok=True)
    else:
        pdf_path.write_bytes(content)

    return "downloaded", pdf_path, "ok"


# --------------------------------------------------------------------------- #
# Event selection
# --------------------------------------------------------------------------- #
def load_candidates(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Return (seq_id, symbol, eff_date, attachment_url) rows for all SAST
    filings that have an attachment_url."""
    rows = con.execute(
        """
        SELECT seq_id, symbol, CAST(announced_at AS DATE) AS eff_date, attachment_url
        FROM announcements
        WHERE category = ? AND attachment_url IS NOT NULL
        """,
        [CATEGORY],
    ).fetchall()
    return rows


def stratified_sample(rows: list[tuple], n: int, seed: int = RANDOM_SEED) -> list[tuple]:
    """Half sampled from eff_date < STRATIFY_CUTOFF, half from >=. Caps to
    what's available in each stratum."""
    before = [r for r in rows if r[2] < STRATIFY_CUTOFF]
    after = [r for r in rows if r[2] >= STRATIFY_CUTOFF]

    n_before = min(n // 2, len(before))
    n_after = min(n - n_before, len(after))
    # If one stratum is short, top up from the other where possible.
    if n_before + n_after < n:
        n_before = min(n - n_after, len(before))

    rng = random.Random(seed)  # nosec B311
    picked_before = rng.sample(before, n_before) if n_before else []
    picked_after = rng.sample(after, n_after) if n_after else []
    combined = picked_before + picked_after
    rng.shuffle(combined)
    return combined


# --------------------------------------------------------------------------- #
# Output schema
# --------------------------------------------------------------------------- #
def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sast_extract (
            seq_id VARCHAR,
            symbol VARCHAR,
            eff_date DATE,
            pdf_status VARCHAR,
            text_chars INTEGER,
            direction VARCHAR,
            direction_source VARCHAR,
            pct_before DOUBLE,
            pct_after DOUBLE,
            shares_qty BIGINT,
            snippet VARCHAR,
            error_detail VARCHAR
        )
        """
    )


def load_existing_seq_ids(con: duckdb.DuckDBPyConnection) -> set[str]:
    try:
        rows = con.execute("SELECT DISTINCT seq_id FROM sast_extract").fetchall()
    except duckdb.CatalogException:
        return set()
    return {r[0] for r in rows if r[0] is not None}


def insert_row(con: duckdb.DuckDBPyConnection, row: dict) -> None:
    con.execute(
        """
        INSERT INTO sast_extract (
            seq_id, symbol, eff_date, pdf_status, text_chars, direction,
            direction_source, pct_before, pct_after, shares_qty, snippet, error_detail
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row["seq_id"],
            row["symbol"],
            row["eff_date"],
            row["pdf_status"],
            row["text_chars"],
            row["direction"],
            row["direction_source"],
            row["pct_before"],
            row["pct_after"],
            row["shares_qty"],
            row["snippet"],
            row["error_detail"],
        ],
    )


# --------------------------------------------------------------------------- #
# Main per-event processing
# --------------------------------------------------------------------------- #
def process_one(
    seq_id: str,
    symbol: str,
    eff_date: date,
    url: str,
    session: requests.Session,
    backend: str,
) -> dict:
    status, pdf_path, detail = ensure_pdf(seq_id, url, session)

    if status == "downloaded":
        time.sleep(INTER_DOWNLOAD_SLEEP_SECONDS)

    if status == "failed" or pdf_path is None:
        return {
            "seq_id": seq_id,
            "symbol": symbol,
            "eff_date": eff_date,
            "pdf_status": "download_failed",
            "text_chars": 0,
            "direction": None,
            "direction_source": "failed",
            "pct_before": None,
            "pct_after": None,
            "shares_qty": None,
            "snippet": None,
            "error_detail": detail,
        }

    try:
        text = extract_text(pdf_path, backend)
    except Exception as exc:  # noqa: BLE001 - any pdf-lib failure, keep pilot moving
        return {
            "seq_id": seq_id,
            "symbol": symbol,
            "eff_date": eff_date,
            "pdf_status": "extract_failed",
            "text_chars": 0,
            "direction": None,
            "direction_source": "failed",
            "pct_before": None,
            "pct_after": None,
            "shares_qty": None,
            "snippet": None,
            "error_detail": f"extract failed: {exc}",
        }

    if not text or not text.strip():
        return {
            "seq_id": seq_id,
            "symbol": symbol,
            "eff_date": eff_date,
            "pdf_status": "extract_failed",
            "text_chars": 0,
            "direction": None,
            "direction_source": "failed",
            "pct_before": None,
            "pct_after": None,
            "shares_qty": None,
            "snippet": None,
            "error_detail": "extracted text was empty (likely a scanned/image PDF)",
        }

    result = classify_document(text)
    return {
        "seq_id": seq_id,
        "symbol": symbol,
        "eff_date": eff_date,
        "pdf_status": "ok",
        "text_chars": len(text),
        "direction": result["direction"],
        "direction_source": result["direction_source"],
        "pct_before": result["pct_before"],
        "pct_after": result["pct_after"],
        "shares_qty": result["shares_qty"],
        "snippet": result["snippet"],
        "error_detail": None,
        # kept only for the run-level agreement-rate report, not persisted:
        "_text_direction": result["text_direction"],
        "_pct_direction": result["pct_direction"],
    }


# --------------------------------------------------------------------------- #
# Summary reporting
# --------------------------------------------------------------------------- #
def print_summary(con: duckdb.DuckDBPyConnection, run_results: list[dict]) -> None:
    n = len(run_results)
    if n == 0:
        print("No rows processed this run.")
        return

    n_download_ok = sum(1 for r in run_results if r["pdf_status"] in ("ok", "extract_failed"))
    n_download_failed = sum(1 for r in run_results if r["pdf_status"] == "download_failed")
    n_extract_ok = sum(1 for r in run_results if r["pdf_status"] == "ok")
    n_extract_failed = sum(1 for r in run_results if r["pdf_status"] == "extract_failed")

    print()
    print("=== R46 SAST pilot summary (this run) ===")
    print(f"Events processed         : {n}")
    print(
        f"PDF download success     : {n_download_ok}/{n} "
        f"({100 * n_download_ok / n:.1f}%)  [download_failed={n_download_failed}]"
    )
    denom_extract = n_download_ok
    if denom_extract:
        print(
            f"Text extraction success  : {n_extract_ok}/{denom_extract} "
            f"({100 * n_extract_ok / denom_extract:.1f}%)  [extract_failed={n_extract_failed}]"
        )
    else:
        print("Text extraction success  : n/a (no PDFs downloaded)")

    print()
    print("Direction distribution (final, post fallback):")
    dist: dict[str, int] = {}
    for r in run_results:
        key = r["direction"] or "failed"
        dist[key] = dist.get(key, 0) + 1
    for k, v in sorted(dist.items(), key=lambda kv: -kv[1]):
        print(f"  {k:10s}: {v:4d} ({100 * v / n:.1f}%)")

    confident = sum(
        1 for r in run_results if r["direction"] in ("buy", "sell", "pledge", "transfer")
    )
    print(
        f"\nConfident-direction coverage (buy/sell/pledge/transfer, excl. mixed/failed): "
        f"{confident}/{n} ({100 * confident / n:.1f}%)"
    )

    print()
    both = [
        r
        for r in run_results
        if r.get("_text_direction") in ("buy", "sell")
        and r.get("_pct_direction") in ("buy", "sell")
    ]
    if both:
        agree = sum(1 for r in both if r["_text_direction"] == r["_pct_direction"])
        print(
            f"Text-vs-pct-delta agreement (both present, buy/sell only): "
            f"{agree}/{len(both)} ({100 * agree / len(both):.1f}%)"
        )
    else:
        print("Text-vs-pct-delta agreement: n/a (no rows with both signals present)")

    print()
    print("Sample rows (up to 10):")
    shown = [r for r in run_results if r["pdf_status"] == "ok"][:10]
    if not shown:
        shown = run_results[:10]
    for r in shown:
        snip = (r["snippet"] or "").replace("\n", " ").strip()[:100]
        print(
            f"  {r['symbol']:12s} dir={str(r['direction']):9s} src={str(r['direction_source']):12s} {snip!r}"
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="R46 pilot: download SAST disclosure PDFs and extract transaction direction/size."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sample", type=int, help="Random stratified sample size (seed=42)")
    group.add_argument("--all", action="store_true", help="Process the full ~4,700-row universe")
    parser.add_argument(
        "--out",
        dest="out_db",
        default=DEFAULT_OUT_DB,
        help=f"Output DuckDB path (default: {DEFAULT_OUT_DB})",
    )
    parser.add_argument(
        "--announcements-db",
        dest="announcements_db",
        default=ANNOUNCEMENTS_DB,
        help=f"Source announcements DuckDB path (default: {ANNOUNCEMENTS_DB})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    backend = detect_pdf_backend()
    if backend is None:
        print(
            "ERROR: no PDF text-extraction library is importable (checked pypdf, "
            "pdfplumber, fitz/PyMuPDF). None is a declared project dependency as of "
            "2026-07-07. Re-run with an ephemeral dependency instead of `uv add`, e.g.:\n"
            "    uv run --with pypdf python backtest/news_event_study/extract_sast_pdfs.py --sample 300\n"
            "Stopping without downloading anything.",
            file=sys.stderr,
        )
        return 3
    print(f"Using PDF backend: {backend}")

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    Path(args.out_db).parent.mkdir(parents=True, exist_ok=True)

    src_con = duckdb.connect(args.announcements_db, read_only=True)
    candidates = load_candidates(src_con)
    src_con.close()
    print(f"Candidates (category={CATEGORY!r}, attachment_url IS NOT NULL): {len(candidates)}")

    if args.all:
        events = candidates
        print("--all requested: processing the FULL universe.")
    else:
        events = stratified_sample(candidates, args.sample)
        n_before = sum(1 for e in events if e[2] < STRATIFY_CUTOFF)
        n_after = len(events) - n_before
        print(
            f"Stratified sample: requested={args.sample}, got={len(events)} "
            f"(before {STRATIFY_CUTOFF}: {n_before}, on/after: {n_after})"
        )

    out_con = duckdb.connect(args.out_db)
    init_schema(out_con)
    existing = load_existing_seq_ids(out_con)
    if existing:
        before_filter = len(events)
        events = [e for e in events if e[0] not in existing]
        print(
            f"Resume: {len(existing)} seq_id(s) already in {args.out_db}; "
            f"skipping {before_filter - len(events)} already-processed row(s)"
        )

    session = requests.Session()

    run_results: list[dict] = []
    t0 = time.monotonic()
    for i, (seq_id, symbol, eff_date, url) in enumerate(events, start=1):
        result = process_one(seq_id, symbol, eff_date, url, session, backend)
        insert_row(out_con, result)
        run_results.append(result)

        if i % PROGRESS_EVERY_N == 0:
            elapsed = time.monotonic() - t0
            n_ok = sum(1 for r in run_results if r["pdf_status"] == "ok")
            print(f"  ... {i}/{len(events)} processed (ok={n_ok}), {elapsed:.1f}s elapsed")

    elapsed = time.monotonic() - t0
    print(
        f"\nDone: {len(events)} event(s) in {elapsed:.1f}s ({elapsed / max(len(events), 1):.2f}s/event)"
    )

    print_summary(out_con, run_results)

    out_con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
