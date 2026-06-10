"""Phase 2 Deliverable 1 — sector-index 1m coverage audit.

The sector_follow_cap5_vol signal evaluator (services/sector_follow_service.py
``duckdb_metrics_provider``) needs intraday 1m bars for each *mapped* sector
index. If an index has no 1m bars, ``sector_ret`` is None and every stock mapped
to that index fails the gate (fail-closed) — so the entry can never fire. Phase 0's
data_coverage.md audited *stock* 1m only; this script audits the *index* side.

Read-only on db/historify.duckdb. Emits
strategies/sector_follow_cap5_vol/index_data_coverage.md.

Usage:
    uv run python outputs/sector_follow_cap5_vol_phase2_2026-06-10/index_1m_audit.py

The live DuckDB is exclusively write-locked by the running OpenAlgo app during
market hours. We connect read_only=True; if that still raises a lock error, the
script falls back to the r33 daily parquet snapshot for the daily columns and
marks the 1m columns UNKNOWN_LOCKED so the operator can re-run post-close.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[2]
_STRAT_DIR = _REPO / "strategies" / "sector_follow_cap5_vol"
_SECTOR_MAP = _STRAT_DIR / "sector_map.json"
_OUT = _STRAT_DIR / "index_data_coverage.md"

# The live db/historify.duckdb is write-locked by the running app during market
# hours. Override with --db <path> or HISTORIFY_DB_PATH=<path> to point at an
# unlocked snapshot copy (the doc committed here was generated from such a copy;
# operator can re-run against the live file post-close to refresh).
_DEFAULT_DB = _REPO / "db" / "historify.duckdb"


def _db_path() -> str:
    for i, a in enumerate(sys.argv):
        if a == "--db" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return os.getenv("HISTORIFY_DB_PATH", str(_DEFAULT_DB))


def _unique_indices() -> list[str]:
    raw = json.loads(_SECTOR_MAP.read_text(encoding="utf-8"))
    idx = {entry["index"] for entry in raw.get("map", {}).values()}
    return sorted(idx)


def _stocks_for_index(index_sym: str) -> list[str]:
    raw = json.loads(_SECTOR_MAP.read_text(encoding="utf-8"))
    return sorted(s for s, e in raw.get("map", {}).items() if e["index"] == index_sym)


def _to_date(v) -> date | None:
    # historify stores epoch seconds; tolerate datetime too.
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), tz=timezone.utc).date()
    return datetime.fromisoformat(str(v)[:19]).date()


def _query_one(con, symbol: str, interval: str) -> tuple[int, str | None, str | None, date | None]:
    """(bar_count, min_ts_iso, max_ts_iso, max_date) for a symbol+interval."""
    row = con.execute(
        """
        SELECT COUNT(*), MIN(timestamp), MAX(timestamp)
        FROM market_data
        WHERE symbol = ? AND interval = ?
        """,
        [symbol, interval],
    ).fetchone()
    cnt = int(row[0] or 0)
    if cnt == 0:
        return 0, None, None, None
    mind, maxd = _to_date(row[1]), _to_date(row[2])
    return cnt, (mind.isoformat() if mind else None), (maxd.isoformat() if maxd else None), maxd


def _status(c1: int, cd: int) -> str:
    if c1 > 0:
        return "READY"
    if cd > 0:
        return "1M_MISSING_USE_DAILY"
    return "NO_DATA"


def main() -> int:
    indices = _unique_indices()
    db = _db_path()
    # Freshness yardstick: stock 1m reaches 2026-06-08 (Phase 0). An index 1m feed
    # more than ~2 trading days behind that can't serve a same-day 15:20 signal.
    today = date.today()
    rows: list[dict] = []
    lock_error: str | None = None

    try:
        import duckdb

        con = duckdb.connect(db, read_only=True)
        try:
            for idx in indices:
                c1, min1, max1, max1d = _query_one(con, idx, "1m")
                cd, mind, maxd, _ = _query_one(con, idx, "D")
                stale_days = (today - max1d).days if max1d else None
                rows.append(
                    dict(
                        index=idx, c1=c1, min1=min1, max1=max1, stale_days=stale_days,
                        cd=cd, mind=mind, maxd=maxd, status=_status(c1, cd),
                    )
                )
        finally:
            con.close()
    except Exception as e:  # lock or missing-file
        lock_error = str(e)
        for idx in indices:
            rows.append(
                dict(index=idx, c1=-1, min1=None, max1=None, stale_days=None, cd=-1,
                     mind=None, maxd=None, status="UNKNOWN_LOCKED")
            )

    _write_report(rows, lock_error, db)
    print(f"DB: {db}")
    print(f"Wrote {_OUT}")
    for r in rows:
        print(f"  {r['index']:18s} 1m={r['c1']:>10} D={r['cd']:>8}  {r['status']}")
    return 0


_STALE_TRADING_DAYS = 4  # >~4 calendar days behind ≈ can't serve a same-day signal


def _write_report(rows: list[dict], lock_error: str | None, db: str = "") -> None:
    def cell(v):
        return "—" if v in (None, "") else str(v)

    lines: list[str] = []
    lines.append("# sector_follow_cap5_vol — Sector Index 1m Coverage Audit")
    lines.append("")
    lines.append("**Phase 2 Deliverable 1** · generated by "
                 "`outputs/sector_follow_cap5_vol_phase2_2026-06-10/index_1m_audit.py`")
    if db:
        snap = " (snapshot copy — live file write-locked by the running app)" \
            if "Temp" in db or "tmp" in db else ""
        lines.append(f"· source: `{db}`{snap}")
    lines.append("")
    lines.append("The signal evaluator needs **1m** bars for each mapped sector index "
                 "(`duckdb_metrics_provider` derives the index intraday return from 1m; "
                 "no 1m → `sector_ret=None` → fail-closed → those stocks never fire).")
    lines.append("")
    if lock_error:
        lines.append(f"> ⚠️ DuckDB could not be opened read-only (`{lock_error}`). "
                     "The live file is write-locked by the running app during market "
                     "hours. Columns marked `UNKNOWN_LOCKED` — re-run this script "
                     "post-close, or pass `--db <snapshot.duckdb>`.")
        lines.append("")
    lines.append("| index_symbol | 1m bars | 1m range | 1m stale (days) | daily bars | daily range | status |")
    lines.append("| --- | ---: | --- | ---: | ---: | --- | --- |")
    for r in rows:
        c1 = "?" if r["c1"] == -1 else f"{r['c1']:,}"
        cd = "?" if r["cd"] == -1 else f"{r['cd']:,}"
        rng1 = "?" if r["c1"] == -1 else (f"{cell(r['min1'])} → {cell(r['max1'])}" if r["c1"] else "—")
        rngd = "?" if r["cd"] == -1 else (f"{cell(r['mind'])} → {cell(r['maxd'])}" if r["cd"] else "—")
        sd = r.get("stale_days")
        sd_cell = "—" if sd is None else (f"{sd} ⚠️" if sd > _STALE_TRADING_DAYS else str(sd))
        lines.append(f"| {r['index']} | {c1} | {rng1} | {sd_cell} | {cd} | {rngd} | {r['status']} |")
    lines.append("")

    stale = [r for r in rows if (r.get("stale_days") or 0) > _STALE_TRADING_DAYS and r["c1"] > 0]
    if stale:
        lines.append("## ⚠️ Freshness gap — index 1m is NOT kept current")
        lines.append("")
        lines.append("Stock 1m reaches 2026-06-08 (Phase 0), but every index 1m series "
                     f"above is >{_STALE_TRADING_DAYS} days behind. The live 15:20 IST "
                     "signal queries *today's* index 1m vs the prior close — if the index "
                     "1m feed isn't backfilled/subscribed daily like the stocks, **every** "
                     "stock fails-closed (no `sector_ret`), not just the daily-only two. "
                     "This is an operational gap, not a one-time backfill: index 1m must be "
                     "added to the daily historify backfill/subscription. **Phase 3/4 work "
                     "item** — flagged here so the operator wires it before sandbox go-live.")
        lines.append("")

    ready = [r for r in rows if r["status"] == "READY"]
    daily_only = [r for r in rows if r["status"] == "1M_MISSING_USE_DAILY"]
    nodata = [r for r in rows if r["status"] == "NO_DATA"]
    unknown = [r for r in rows if r["status"] == "UNKNOWN_LOCKED"]

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **{len(ready)}** index/indices READY (1m present)")
    lines.append(f"- **{len(daily_only)}** 1M_MISSING_USE_DAILY (daily only)")
    lines.append(f"- **{len(nodata)}** NO_DATA")
    if unknown:
        lines.append(f"- **{len(unknown)}** UNKNOWN_LOCKED (DB locked — re-run post-close)")
    lines.append("")

    # Affected-stock remediation guidance.
    if nodata:
        lines.append("## NO_DATA indices — affected stocks + remediation")
        lines.append("")
        for r in nodata:
            stocks = _stocks_for_index(r["index"])
            lines.append(f"- **{r['index']}** → affects: {', '.join(stocks)}")
        lines.append("")
        lines.append("**Decision:** re-map affected stocks to `NIFTY` (broad-market "
                     "fallback) so their signal can still fire, rather than leaving them "
                     "silently dead. Apply via sector_map.json and document the diff here. "
                     "(Not auto-applied — operator confirms; NIFTY itself must be READY first.)")
        lines.append("")
    if daily_only:
        lines.append("## 1M_MISSING_USE_DAILY — known limitation (Phase 4 work item)")
        lines.append("")
        lines.append("Daily bars exist but the live 15:20 IST signal needs the *intraday* "
                     "index change. Deriving an intraday running index OHLC from the "
                     "underlying constituents is **Phase 4** (shadow-replay verification) — "
                     "NOT fixed here. Until then these indices fail-closed intraday.")
        lines.append("")
        for r in daily_only:
            stocks = _stocks_for_index(r["index"])
            lines.append(f"- **{r['index']}** (daily {r['cd']:,} bars) → {', '.join(stocks)}")
        lines.append("")

    _OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
