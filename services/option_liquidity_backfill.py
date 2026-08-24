"""One-time seed of ``option_liquidity_daily`` from the NSE UDiFF F&O bhavcopy.

**Why this exists.** The gate reads a 20-day median. Without history every score is
NULL on day one, the gate fails open on every name, and it is installed but inert. The
runtime path (``services/option_liquidity_service.py``) is a live broker sweep and can
only ever start accumulating *today*; the archive is the only way to have a populated
median on the first morning.

**One-time and operator-run.** Not wired into the runtime, not scheduled. Once the
live sweep has ~20 sessions of its own this becomes redundant.

Why SQLite and not DuckDB
-------------------------
The obvious home for per-contract history is ``fo_bhavcopy_eod`` in
``historify.duckdb``, but the running app holds that file open read-write and DuckDB
refuses a second cross-process writer. Scoring the downloaded files straight into
``option_liquidity_daily`` sidesteps that and drops a dependency.

Mixing this with broker-sourced rows in one median
--------------------------------------------------
Sound, and for one specific reason: the stored value is a **rank percentile within
that day's universe**, not an absolute. Turnover here is ``lots x lot_size x close``
while the broker path computes ``volume_units x ltp`` — both are rupees of premium, so
any systematic scale difference between the feeds cancels in the ranking. Rows are
stamped ``source='bhavcopy'`` so a mixed median is visible rather than assumed.

⚠ **Units differ from the broker path and this is the #555 trap.** Bhavcopy
``TtlTradgVol`` is in **LOTS** (verified: only 17 of 27,577 rows are divisible by lot
size, i.e. coincidences), while ``OpnIntrst`` is in **UNITS** (27,577 of 27,577
divisible). So turnover needs the lot multiply here and must NOT get one on the broker
path. ``TtlTrfVal`` is *notional*, not premium — it is deliberately unused.

⚠ **The contract key must include expiry.** The same strike exists in all three listed
contract months; a strike-only key silently mixes them.

Usage
-----
``uv run python -m services.option_liquidity_backfill --days 40``
``uv run python -m services.option_liquidity_backfill --from 2026-06-15 --to 2026-08-07``
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import time
import zipfile
from collections import defaultdict

import requests

from services.option_liquidity_service import (
    apply_median,
    assign_percentiles,
    load_equity_universe,
)
from utils.logging import get_logger

logger = get_logger(__name__)

_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ds}_F_0000.csv.zip"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_PAUSE_S = 1.2  # be a good citizen against a public archive


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": _UA,
            "Accept": "text/html,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/all-reports-derivatives",
        }
    )
    try:
        s.get("https://www.nseindia.com", timeout=15)
    except Exception:
        logger.info("bhavcopy: cookie warm-up failed; continuing")
    return s


def fetch_day(sess: requests.Session, day: dt.date) -> list[dict] | None:
    """Stock-option (``STO``) rows for one date, or ``None`` if there is no file.

    A missing file is the normal signal for a holiday or a weekend — the archive
    simply has no object — so it is not an error.
    """
    try:
        r = sess.get(_URL.format(ds=day.strftime("%Y%m%d")), timeout=45)
    except Exception:
        logger.exception("bhavcopy: fetch failed for %s", day)
        return None
    if r.status_code != 200 or len(r.content) < 1024:
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    except Exception:
        logger.exception("bhavcopy: unreadable archive for %s", day)
        return None
    return [row for row in csv.DictReader(io.StringIO(text)) if row.get("FinInstrmTp") == "STO"]


def score_day(rows: list[dict], universe: set[str], per_side: int) -> dict:
    """``{(symbol, side): metrics}`` for one bhavcopy day, same shape as the sweep."""
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        sym = r.get("TckrSymb")
        if sym and sym in universe:
            by_sym[sym].append(r)

    scored: dict[tuple[str, str], dict] = {}
    for sym, rs in by_sym.items():
        # front month — deliberately WITHOUT pick_contract's expiry-week roll
        # (issue #669): this reconstructs history from before the roll existed,
        # and rolling past days would change what those sessions actually saw
        try:
            front = min(r["XpryDt"] for r in rs)
        except (ValueError, KeyError):
            continue
        near = [r for r in rs if r["XpryDt"] == front]
        try:
            spot = float(near[0].get("UndrlygPric") or 0)
            lot = float(near[0].get("NewBrdLotQty") or 0)
        except (TypeError, ValueError):
            continue
        if spot <= 0 or lot <= 0:
            continue
        try:
            expiry = dt.date.fromisoformat(front)
        except ValueError:
            expiry = None

        for side in ("CE", "PE"):
            leg = [r for r in near if r.get("OptnTp") == side]
            leg.sort(key=lambda r: abs(float(r.get("StrkPric") or 0) - spot))
            band = leg[:per_side]
            if not band:
                continue
            turnover = 0.0
            vol_lots = 0.0
            oi_units = 0.0
            trades = 0
            dead = 0
            for r in band:
                try:
                    v = float(r.get("TtlTradgVol") or 0)  # LOTS
                    close = float(r.get("ClsPric") or 0)
                    oi = float(r.get("OpnIntrst") or 0)  # UNITS
                    n = int(r.get("TtlNbOfTxsExctd") or 0)
                except (TypeError, ValueError):
                    continue
                if v <= 0:
                    dead += 1
                turnover += v * lot * close  # lots -> units, then rupees of premium
                vol_lots += v
                oi_units += oi
                trades += n
            scored[(sym, side)] = {
                "symbol": sym,
                "side": side,
                "atm_premium_turnover": round(turnover, 2),
                "atm_zero_vol_strikes": dead,
                "band_strikes": len(band),
                # the archive carries no quotes, so there is no spread to report
                "atm_spread_pct": None,
                "atm_volume_lots": round(vol_lots, 2),
                "atm_oi_lots": round(oi_units / lot, 2) if lot else None,
                "atm_trades": trades or None,
                "avg_ticket_inr": round(turnover / trades, 2) if trades else None,
                "expiry_used": expiry,
                "source": "bhavcopy",
            }
    return scored


def backfill(start: dt.date, end: dt.date, per_side: int = 6, dry_run: bool = False) -> dict:
    """Score every archived day in ``[start, end]`` and write them chronologically.

    Chronological order is required, not cosmetic: each day's median is built from the
    days already written, so processing out of order would compute medians over a
    partial past.
    """
    from database.option_liquidity_db import (
        get_daily_pctile_history,
        init_db,
        upsert_scores,
    )
    from services.option_liquidity_service import _median_days, _min_days

    init_db()
    universe = load_equity_universe()
    if not universe:
        logger.error("bhavcopy backfill: SCANNER_SYMBOLS is empty — nothing to score")
        return {"status": "no_universe"}

    sess = _session()
    day = start
    written = days_ok = days_missing = 0
    per_day: list[tuple[str, int, int]] = []
    while day <= end:
        if day.weekday() >= 5:  # cheap skip; holidays fall out via a missing file
            day += dt.timedelta(days=1)
            continue
        rows = fetch_day(sess, day)
        if not rows:
            days_missing += 1
            logger.info("bhavcopy backfill: no file for %s (holiday?)", day)
            day += dt.timedelta(days=1)
            time.sleep(_PAUSE_S)
            continue

        scored = score_day(rows, universe, per_side)
        assign_percentiles(scored)
        history = get_daily_pctile_history(_median_days() - 1, day)
        apply_median(scored, history, min_days=_min_days(), median_days=_median_days())
        out = list(scored.values())
        ranked = sum(1 for r in out if r.get("option_liquidity_pctile") is not None)
        if out and not dry_run:
            written += upsert_scores(day, out)
        days_ok += 1
        per_day.append((str(day), len(out), ranked))
        logger.info(
            "bhavcopy backfill: %s -> %d rows (%d with a full median)", day, len(out), ranked
        )
        day += dt.timedelta(days=1)
        time.sleep(_PAUSE_S)

    return {
        "status": "ok",
        "days_scored": days_ok,
        "days_missing": days_missing,
        "rows_written": written,
        "dry_run": dry_run,
        "per_day": per_day,
    }


def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Seed option_liquidity_daily from NSE bhavcopy")
    ap.add_argument("--from", dest="start", help="YYYY-MM-DD")
    ap.add_argument("--to", dest="end", help="YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--days", type=int, help="calendar days back from --to instead of --from")
    ap.add_argument("--per-side", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today() - dt.timedelta(days=1)
    if args.start:
        start = dt.date.fromisoformat(args.start)
    elif args.days:
        start = end - dt.timedelta(days=args.days)
    else:
        ap.error("give --from or --days")
    print(f"backfilling {start} .. {end} (per_side={args.per_side}, dry_run={args.dry_run})")
    out = backfill(start, end, per_side=args.per_side, dry_run=args.dry_run)
    print(
        f"\ndays scored {out.get('days_scored')} · missing {out.get('days_missing')} · rows written {out.get('rows_written')}"
    )
    for d, n, ranked in out.get("per_day", [])[-10:]:
        print(f"  {d}  rows={n:>4}  with-median={ranked:>4}")


if __name__ == "__main__":
    _main()
