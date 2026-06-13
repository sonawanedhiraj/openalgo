"""Market-data freshness validation for strategy pre-flight + daily monitoring.

Background — the 2026-05-29 → 06-10 incident
--------------------------------------------
``sector_follow_cap5_vol`` reads its sector-index and stock 1m bars from
``db/historify.duckdb``. The daily after-close backfill that keeps that feed
current did not exist until 2026-06-10, so the index 1m feed silently sat 12 days
stale (last bar 2026-05-29) while the strategy's E2E suite — hermetic, mocked
data — stayed green. The day before the first sandbox run we found it only by
hand-querying DuckDB. This module makes that an automated, daily, fail-loud
check.

The functions here are **pure / read-only** on DuckDB and the strategy config —
they never place orders, mutate the feed, or write the freshness verdict (the
caller persists it via ``database.data_health_db``). Staleness is **business-day
aware**: a weekend gap is not stale, only missing trading days are.

Timestamp convention: ``market_data.timestamp`` is a UTC epoch (e.g.
``1780048740`` == ``2026-05-29 15:29 IST`` == ``09:59 UTC``). We derive the IST
calendar date of the last bar via the IST tzinfo and compare trading days.

Note: market holidays are NOT modelled — only weekends. A single mid-week NSE
holiday inflates measured staleness by one business day, which the default
1-business-day threshold (yesterday's close acceptable) absorbs for the common
case; a holiday immediately followed by a missed backfill could produce a
false-positive alert. The cost of a false positive is an auto-pause the operator
overrides, which is the safe direction.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
_DEFAULT_DUCKDB_PATH = "db/historify.duckdb"

# Default acceptable staleness in business days. 1 == "yesterday's close is fine"
# (the realistic state at 15:20 IST, before today's after-close backfill runs).
_DEFAULT_MAX_STALENESS = 1


def default_max_staleness_business_days() -> int:
    """Env-configurable default threshold (``MAX_STALENESS_BUSINESS_DAYS``)."""
    try:
        return int(os.getenv("MAX_STALENESS_BUSINESS_DAYS", str(_DEFAULT_MAX_STALENESS)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_STALENESS


# --------------------------------------------------------------------------- #
# Business-day helpers (weekend-aware; holidays not modelled — see module note)
# --------------------------------------------------------------------------- #
def _ist_date_of_epoch(ts: float) -> date:
    """IST calendar date of a UTC-epoch market_data timestamp."""
    return datetime.fromtimestamp(ts, _IST).date()


def _prev_or_same_business_day(d: date) -> date:
    """Roll a weekend date back to the preceding Friday; weekdays unchanged."""
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


def business_days_between(d_from: date, d_to: date) -> int:
    """Count business days in the half-open interval ``(d_from, d_to]``.

    Returns 0 when ``d_to <= d_from`` (the last bar is at or ahead of the
    reference day — never negative).
    """
    if d_to <= d_from:
        return 0
    n = 0
    cur = d_from
    while cur < d_to:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Pure freshness queries
# --------------------------------------------------------------------------- #
def get_data_freshness(
    duckdb_path: str,
    symbols: list[str],
    interval: str = "1m",
) -> dict[str, int | None]:
    """Last stored timestamp (UTC epoch) per symbol, ``None`` if no bars exist.

    Read-only on ``historify.duckdb``. A symbol with no rows maps to ``None`` so
    the caller can treat "never ingested" distinctly from "stale".
    """
    if not symbols:
        return {}
    import duckdb

    out: dict[str, int | None] = dict.fromkeys(symbols)
    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        placeholders = ", ".join(["?"] * len(symbols))
        rows = con.execute(
            f"""
            SELECT symbol, MAX(timestamp) AS last_ts
            FROM market_data
            WHERE interval = ?
              AND symbol IN ({placeholders})
            GROUP BY symbol
            """,
            [interval, *symbols],
        ).fetchall()
    finally:
        con.close()
    for symbol, last_ts in rows:
        if last_ts is not None:
            out[symbol] = int(last_ts)
    return out


def compute_stale_symbols(
    duckdb_path: str,
    symbols: list[str],
    today: date | None = None,
    max_staleness_business_days: int = 0,
    interval: str = "1m",
) -> tuple[list[str], list[str], dict[str, dict]]:
    """Split ``symbols`` into stale vs fresh against the latest trading day.

    Pure / read-only. Reads MAX(timestamp) per symbol from ``historify.duckdb``
    and compares each symbol's most-recent 1m bar IST date to the most recent
    business day on/before ``today`` (today's expected 15:30 IST close). A symbol
    is **stale** when it is more than ``max_staleness_business_days`` business days
    behind that reference (default 0 — the boot/periodic convergence target wants
    *today's* close present) or has no bars at all.

    Returns ``(stale, fresh, details)`` where ``details`` maps each symbol to
    ``{last_ts, last_date, staleness_days, stale}``. ``stale``/``fresh`` are sorted.
    """
    ref_date = today or datetime.now(_IST).date()
    ref_business_day = _prev_or_same_business_day(ref_date)

    freshness = get_data_freshness(duckdb_path, symbols, interval=interval)

    stale: list[str] = []
    fresh: list[str] = []
    details: dict[str, dict] = {}
    for sym in symbols:
        last_ts = freshness.get(sym)
        if last_ts is None:
            details[sym] = {
                "last_ts": None,
                "last_date": None,
                "staleness_days": None,
                "stale": True,
            }
            stale.append(sym)
            continue
        last_date = _ist_date_of_epoch(last_ts)
        staleness = business_days_between(last_date, ref_business_day)
        is_stale = staleness > max_staleness_business_days
        details[sym] = {
            "last_ts": last_ts,
            "last_date": last_date.isoformat(),
            "staleness_days": staleness,
            "stale": is_stale,
        }
        (stale if is_stale else fresh).append(sym)

    return sorted(stale), sorted(fresh), details


def _resolve_strategy_symbols(strategy_name: str) -> dict[str, list[str]]:
    """Map a strategy to the ``{'stock': [...], 'index': [...]}`` it depends on.

    Derived live from the strategy's config + sector map, so it tracks the
    universe / map automatically. Raises ``ValueError`` for unknown strategies.
    """
    if strategy_name == "sector_follow_cap5_vol":
        from services.sector_follow_service import load_config, load_sector_map

        stocks = list(load_config().universe)
        indices = sorted(set(load_sector_map().values()))
        return {"stock": stocks, "index": indices}
    raise ValueError(f"unknown strategy for freshness check: {strategy_name!r}")


def check_strategy_data_ready(
    strategy_name: str,
    date: str | None = None,
    max_staleness_business_days: int | None = None,
    duckdb_path: str = _DEFAULT_DUCKDB_PATH,
    index_only: bool = False,
) -> tuple[bool, dict[str, dict]]:
    """Is ``strategy_name``'s market data fresh enough to trade on ``date``?

    Args:
        strategy_name: strategy whose dependent symbols to check.
        date: reference IST date ``YYYY-MM-DD`` (default: today IST). Staleness is
            measured against the most recent business day on/before this date.
        max_staleness_business_days: threshold; a symbol is stale when its last
            bar is more than this many business days behind the reference. Default
            from ``MAX_STALENESS_BUSINESS_DAYS`` env (1).
        duckdb_path: historify DuckDB path (overridable for tests).
        index_only: when True, check only the index feed (used by the exit gate —
            exits need current index data but tolerate intraday stock gaps).

    Returns:
        ``(overall_ok, details)`` where ``details`` maps each symbol to
        ``{last_ts, last_date, staleness_days, ok, kind}``. ``overall_ok`` is True
        iff every checked symbol is present AND within threshold. A symbol with no
        bars (``last_ts`` None) is always ``ok=False`` (staleness_days None).
    """
    if max_staleness_business_days is None:
        max_staleness_business_days = default_max_staleness_business_days()

    ref_date = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now(_IST).date()
    ref_business_day = _prev_or_same_business_day(ref_date)

    groups = _resolve_strategy_symbols(strategy_name)
    kinds = ["index"] if index_only else ["index", "stock"]
    symbols: list[str] = []
    kind_of: dict[str, str] = {}
    for k in kinds:
        for s in groups.get(k, []):
            kind_of[s] = k
            symbols.append(s)

    freshness = get_data_freshness(duckdb_path, symbols)

    details: dict[str, dict] = {}
    overall_ok = True
    for sym in symbols:
        last_ts = freshness.get(sym)
        if last_ts is None:
            details[sym] = {
                "last_ts": None,
                "last_date": None,
                "staleness_days": None,
                "ok": False,
                "kind": kind_of[sym],
            }
            overall_ok = False
            continue
        last_date = _ist_date_of_epoch(last_ts)
        staleness = business_days_between(last_date, ref_business_day)
        ok = staleness <= max_staleness_business_days
        details[sym] = {
            "last_ts": last_ts,
            "last_date": last_date.isoformat(),
            "staleness_days": staleness,
            "ok": ok,
            "kind": kind_of[sym],
        }
        if not ok:
            overall_ok = False

    return overall_ok, details


def format_freshness_report(strategy_name: str, freshness_details: dict[str, dict]) -> str:
    """Render a freshness verdict as compact markdown for humans / Telegram."""
    if not freshness_details:
        return f"*{strategy_name}* data freshness: (no symbols checked)"

    stale = sorted(s for s, d in freshness_details.items() if not d.get("ok", True))
    total = len(freshness_details)
    header = (
        f"*{strategy_name}* data freshness — "
        f"{'✅ OK' if not stale else f'🚨 {len(stale)}/{total} STALE'}"
    )
    lines = [header]
    if stale:
        lines.append("")
        lines.append("*Stale symbols:*")
        for s in stale:
            d = freshness_details[s]
            last = d.get("last_date") or "MISSING"
            n = d.get("staleness_days")
            n_txt = "no data" if n is None else f"{n} business day(s) behind"
            lines.append(f"  • `{s}` ({d.get('kind', '?')}) — last {last}, {n_txt}")
    return "\n".join(lines)
