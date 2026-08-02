"""Builds the compact, read-only picture of one trading day.

This is stage 1 of the post-market review: a deterministic aggregation over
every journal, decision log, health row and scheduler fire the day produced,
compressed into a single dict small enough to persist daily and (from Phase 3)
hand to an LLM.

Design rules
------------

* **Read-only.** This module writes nothing except the log-digest snapshot that
  :mod:`services.postmarket_log_digest` owns.
* **Every section is independently wrapped.** A dead source degrades that one
  section to ``None`` and is named in ``sources_failed`` — it never kills the
  run. The same discipline as :mod:`services.trading_day_funnel_service`, and
  the reason a partial digest is still worth persisting.
* **Counts, not payloads.** Per-symbol detail is kept only where a later
  expectation contract needs it (e.g. open positions carried overnight). The
  9 KB futures eval snapshot is reduced via ``futures_follow_eval_view``.
* **No verdicts.** This module reports what happened. Deciding whether that is
  *wrong* is Phase 2's expectation contracts; explaining it is Phase 3's LLM.
  Keeping those separate is what stops the report from inventing problems.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime
from typing import Any

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Strategies whose journals are folded into the digest, with the module and
# columns needed to summarise each. Kept as data so adding a strategy is a
# one-line change rather than another bespoke query function.
_STRATEGY_JOURNALS: tuple[dict[str, Any], ...] = (
    {
        "name": "futures_follow_cap50",
        "module": "database.futures_follow_db",
        "model": "FuturesFollowTrade",
        "date_col": "entry_date",
        "pnl_col": "net_pnl",
    },
    {
        "name": "sector_follow_cap5_vol",
        "module": "database.sector_follow_db",
        "model": "SectorFollowTrade",
        "date_col": "entry_date",
        "pnl_col": None,
    },
    {
        "name": "open15_vol_breakout",
        "module": "database.open15_breakout_db",
        "model": "Open15Trade",
        "date_col": "trade_date",
        "pnl_col": "pnl",
    },
    {
        "name": "intraday_pullback_top2",
        "module": "database.intraday_pullback_db",
        "model": "IntradayPullbackTrade",
        "date_col": "trade_date",
        "pnl_col": "net_pnl",
    },
)


def _today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def _normalise_date(target_date: str | date_cls | None) -> str:
    if target_date is None:
        return _today_ist()
    if isinstance(target_date, date_cls):
        return target_date.strftime("%Y-%m-%d")
    return str(target_date)


class _Collector:
    """Accumulates sections and records which ones failed."""

    def __init__(self, date: str) -> None:
        self.date = date
        self.sections: dict[str, Any] = {}
        self.failed: list[str] = []

    def add(self, name: str, fn: Any) -> None:
        """Run ``fn()`` and store its result under ``name``.

        A raising section is recorded as ``None`` and named in ``sources_failed``
        rather than aborting the digest — a day with one dead table is still
        worth reporting on.
        """
        try:
            self.sections[name] = fn()
        except Exception:
            logger.exception("postmarket_day_digest: section %r failed for %s", name, self.date)
            self.sections[name] = None
            self.failed.append(name)


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def _collect_jobs(date: str) -> dict[str, Any]:
    """Per-job fire summary from the ``job_run`` audit table."""
    from database import job_run_db

    summary = job_run_db.summarize_date(date)
    errored = sorted(k for k, v in summary.items() if v.get("error"))
    missed = sorted(k for k, v in summary.items() if v.get("missed"))
    return {
        "recorded": len(summary),
        "jobs": summary,
        "jobs_with_errors": errored,
        "jobs_missed": missed,
    }


def _collect_trade_journal(date: str) -> dict[str, Any]:
    """Simplified-engine trade journal, bucketed by strategy and direction."""
    from database import trade_journal_db as tjdb

    sess = tjdb.db_session
    try:
        rows = (
            sess.query(
                tjdb.TradeJournal.strategy_name,
                tjdb.TradeJournal.direction,
                tjdb.TradeJournal.symbol,
                tjdb.TradeJournal.entry_fill_at,
                tjdb.TradeJournal.exit_price,
                tjdb.TradeJournal.exit_reason,
                tjdb.TradeJournal.pnl,
            )
            .filter(tjdb.TradeJournal.placed_at.like(f"{date}%"))
            .all()
        )
    finally:
        sess.remove()

    by_strategy: dict[str, dict[str, Any]] = {}
    for strategy, direction, symbol, entry_fill_at, exit_price, exit_reason, pnl in rows:
        key = strategy or "unknown"
        entry = by_strategy.setdefault(
            key,
            {
                "placed": 0,
                "filled": 0,
                "closed": 0,
                "open_at_eod": 0,
                "unpriced_exits": 0,
                "net_pnl": 0.0,
                "by_direction": {},
                "exit_reasons": {},
                "open_symbols": [],
            },
        )
        entry["placed"] += 1
        if entry_fill_at:
            entry["filled"] += 1
        if exit_price is not None:
            entry["closed"] += 1
        else:
            entry["open_at_eod"] += 1
            if symbol:
                entry["open_symbols"].append(symbol)
        # A stamped exit with no price is the #350 class: the watchdog closed the
        # row without waiting for the fill, so P&L reads zero on the dashboard.
        if exit_reason and exit_price is None:
            entry["unpriced_exits"] += 1
        if pnl is not None:
            entry["net_pnl"] += float(pnl)
        side = (direction or "?").upper()
        entry["by_direction"][side] = entry["by_direction"].get(side, 0) + 1
        if exit_reason:
            entry["exit_reasons"][exit_reason] = entry["exit_reasons"].get(exit_reason, 0) + 1

    for entry in by_strategy.values():
        entry["net_pnl"] = round(entry["net_pnl"], 2)
        entry["open_symbols"] = sorted(set(entry["open_symbols"]))[:20]

    return {
        "total_rows": len(rows),
        "by_strategy": by_strategy,
    }


def _collect_signals(date: str) -> dict[str, Any]:
    """``signal_decision`` rows: taken vs vetoed, and why."""
    from database import signal_decision_db as sddb

    sess = sddb.db_session
    try:
        rows = (
            sess.query(
                sddb.SignalDecision.symbol,
                sddb.SignalDecision.source,
                sddb.SignalDecision.decision,
                sddb.SignalDecision.actually_taken,
                sddb.SignalDecision.enforcement_mode,
                sddb.SignalDecision.reasoning,
            )
            .filter(sddb.SignalDecision.candidate_at.like(f"{date}%"))
            .all()
        )
    finally:
        sess.remove()

    by_decision: dict[str, int] = {}
    by_source: dict[str, int] = {}
    review_failures = 0
    vetoed: list[dict[str, str]] = []
    for symbol, source, decision, actually_taken, enforcement, reasoning in rows:
        label = decision or "unknown"
        by_decision[label] = by_decision.get(label, 0) + 1
        by_source[source or "unknown"] = by_source.get(source or "unknown", 0) + 1
        if label == "review_failed":
            review_failures += 1
        if actually_taken == 0 and len(vetoed) < 10:
            vetoed.append(
                {
                    "symbol": symbol or "?",
                    "decision": label,
                    "enforcement": enforcement or "?",
                    "reason": (reasoning or "no reasoning recorded").strip()[:160],
                }
            )

    return {
        "total": len(rows),
        "taken": sum(1 for r in rows if r[3] == 1),
        "not_taken": sum(1 for r in rows if r[3] == 0),
        "by_decision": by_decision,
        "by_source": by_source,
        "llm_review_failures": review_failures,
        "sample_not_taken": vetoed,
    }


def _collect_scanner(date: str) -> dict[str, Any]:
    """Scanner hits by source, plus the day's scan-cycle post outcomes."""
    from database import scan_cycle_db as scdb
    from database import scanner_db as srdb

    sess = srdb.db_session
    try:
        hit_rows = (
            sess.query(srdb.ScanResult.symbols, srdb.ScanResult.source)
            .filter(srdb.ScanResult.run_at.like(f"{date}%"))
            .all()
        )
    finally:
        sess.remove()

    import json as _json

    by_source: dict[str, set[str]] = {}
    for symbols_blob, source in hit_rows:
        bucket = by_source.setdefault((source or "unknown").lower(), set())
        try:
            items = _json.loads(symbols_blob) if symbols_blob else []
        except (ValueError, TypeError):
            items = []
        if isinstance(items, list):
            bucket.update(str(s).strip().upper() for s in items if s)

    sess = scdb.db_session
    try:
        cycle_rows = (
            sess.query(scdb.ScanCycle.cycle_kind, scdb.ScanCycle.post_status)
            .filter(scdb.ScanCycle.started_at.like(f"{date}%"))
            .all()
        )
    finally:
        sess.remove()

    cycles: dict[str, dict[str, int]] = {}
    for kind, status in cycle_rows:
        bucket = cycles.setdefault(kind or "unknown", {})
        key = status or "unknown"
        bucket[key] = bucket.get(key, 0) + 1

    return {
        "hit_rows": len(hit_rows),
        "distinct_symbols_by_source": {k: len(v) for k, v in by_source.items()},
        "cycles_by_kind": cycles,
    }


def _summarize_strategy_journal(date: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Generic per-strategy journal summary driven by ``_STRATEGY_JOURNALS``."""
    import importlib

    module = importlib.import_module(spec["module"])
    model = getattr(module, spec["model"])
    date_col = getattr(model, spec["date_col"])

    sess = module.db_session
    try:
        rows = sess.query(model).filter(date_col == date).all()
        summary: dict[str, Any] = {
            "rows": len(rows),
            "by_status": {},
            "by_mode": {},
            "symbols": [],
        }
        pnl_total = 0.0
        has_pnl = False
        for row in rows:
            status = str(getattr(row, "status", None) or "unknown")
            summary["by_status"][status] = summary["by_status"].get(status, 0) + 1
            mode = str(getattr(row, "mode", None) or "unknown")
            summary["by_mode"][mode] = summary["by_mode"].get(mode, 0) + 1
            symbol = getattr(row, "symbol", None) or getattr(row, "nifty_symbol", None)
            if symbol:
                summary["symbols"].append(str(symbol))
            if spec["pnl_col"]:
                value = getattr(row, spec["pnl_col"], None)
                if value is not None:
                    pnl_total += float(value)
                    has_pnl = True
        summary["symbols"] = sorted(set(summary["symbols"]))[:20]
        summary["net_pnl"] = round(pnl_total, 2) if has_pnl else None
        return summary
    finally:
        sess.remove()


def _collect_strategy_journals(date: str) -> dict[str, Any]:
    """Every per-strategy journal, each failing independently."""
    out: dict[str, Any] = {}
    for spec in _STRATEGY_JOURNALS:
        try:
            out[spec["name"]] = _summarize_strategy_journal(date, spec)
        except Exception:
            logger.exception("postmarket_day_digest: journal summary failed for %s", spec["name"])
            out[spec["name"]] = None
    return out


def _collect_futures_carry(date: str) -> dict[str, Any]:
    """Open NIFTY futures carry and today's exits.

    This is the raw signal behind the #497 class of failure — ``futures_follow``
    T+1 exits were silently dead for four trading days while open NRML lots
    accumulated to 110% of book. The contract that asserts on it lands in
    Phase 2; here we only report the two numbers it needs.
    """
    from database import futures_follow_db as ffdb

    sess = ffdb.db_session
    try:
        rows = (
            sess.query(
                ffdb.FuturesFollowTrade.entry_date,
                ffdb.FuturesFollowTrade.side,
                ffdb.FuturesFollowTrade.lots,
                ffdb.FuturesFollowTrade.nifty_symbol,
            )
            .filter(ffdb.FuturesFollowTrade.status == "placed")
            .filter(ffdb.FuturesFollowTrade.entry_date <= date)
            .order_by(ffdb.FuturesFollowTrade.entry_date.asc(), ffdb.FuturesFollowTrade.id.asc())
            .all()
        )
    finally:
        sess.remove()

    # An exit is a SEPARATE `SELL` row — `exit_price` is only ever set on the
    # SELL leg, never back-filled onto the BUY it closes. So carry is the running
    # lot balance, and the oldest still-uncovered BUY is found by consuming BUY
    # legs FIFO against the SELL lots seen so far.
    buy_queue: list[tuple[str, int, str]] = []  # (entry_date, lots_remaining, symbol)
    entries_today = exits_today = 0
    lots_bought_today = lots_sold_today = 0

    for entry_date, side, lots, symbol in rows:
        qty = int(lots or 0)
        day = entry_date or ""
        if (side or "").upper() == "BUY":
            buy_queue.append((day, qty, symbol or "?"))
            if day == date:
                entries_today += 1
                lots_bought_today += qty
        elif (side or "").upper() == "SELL":
            if day == date:
                exits_today += 1
                lots_sold_today += qty
            remaining = qty
            while remaining > 0 and buy_queue:
                head_date, head_lots, head_symbol = buy_queue[0]
                consumed = min(remaining, head_lots)
                remaining -= consumed
                if head_lots - consumed <= 0:
                    buy_queue.pop(0)
                else:
                    buy_queue[0] = (head_date, head_lots - consumed, head_symbol)

    open_lots = sum(lots for _, lots, _ in buy_queue)
    oldest_open = buy_queue[0][0] if buy_queue else None
    carry_age_days = None
    if oldest_open:
        try:
            carry_age_days = (
                datetime.strptime(date, "%Y-%m-%d") - datetime.strptime(oldest_open, "%Y-%m-%d")
            ).days
        except ValueError:
            carry_age_days = None

    return {
        "entries_today": entries_today,
        "exits_today": exits_today,
        "lots_bought_today": lots_bought_today,
        "lots_sold_today": lots_sold_today,
        "open_lots_carried": open_lots,
        "oldest_open_entry_date": oldest_open,
        # Calendar days the oldest uncovered lot has been held. A T+1 strategy
        # should never show more than a weekend's worth here.
        "carry_age_days": carry_age_days,
        "open_entries": [f"{sym}@{day} x{lots}" for day, lots, sym in buy_queue[:20]],
    }


def _collect_futures_eval(date: str) -> dict[str, Any] | None:
    """The 15:20 evaluation snapshot, compressed to its ~300 B digest."""
    from database import futures_follow_eval_db as ffedb
    from services import futures_follow_eval_view

    snapshot = ffedb.get_snapshot("futures_follow_cap50", date)
    if not snapshot:
        return None
    try:
        return futures_follow_eval_view.summarize_payload(snapshot)
    except Exception:
        logger.exception("postmarket_day_digest: eval snapshot summarise failed")
        return {"summarise_failed": True}


def _collect_data_health(date: str) -> dict[str, Any]:
    """Latest freshness verdict per tracked universe."""
    from database import data_health_db as dhdb

    out: dict[str, Any] = {}
    for strategy in ("sector_follow_cap5_vol", "scanner_universe_1m", "scanner_universe_D"):
        row = dhdb.get_latest_check(strategy)
        if not row:
            out[strategy] = None
            continue
        out[strategy] = {
            "check_at": row.get("check_at"),
            "overall_ok": row.get("overall_ok"),
            "n_stale": len(row.get("stale_symbols") or []),
            "stale_sample": (row.get("stale_symbols") or [])[:10],
        }
    return out


def _collect_scanner_comparison(date: str) -> list[dict[str, Any]]:
    """In-house vs Chartink parity rows for the date."""
    from database import scanner_comparison_db as scmdb

    rows = scmdb.get_comparisons_for_date(date) or []
    return [
        {
            "side": r.get("screener_side"),
            "inhouse": r.get("inhouse_count"),
            "chartink": r.get("chartink_count"),
            "intersection": r.get("intersection_count"),
            "jaccard": r.get("jaccard"),
            "verdict": (r.get("tuning_suggestion") or "")[:200],
        }
        for r in rows
    ]


def _collect_mirror(date: str) -> dict[str, Any]:
    """Multi-account mirror fan-out outcomes for the date."""
    from database import account_orders_db as aodb

    sess = aodb.db_session
    try:
        # created_at is a DateTime, so the date filter runs in Python rather than
        # a dialect-specific DATE() cast.
        rows = sess.query(
            aodb.AccountOrder.created_at,
            aodb.AccountOrder.status,
            aodb.AccountOrder.account_id,
        ).all()
    finally:
        sess.remove()

    by_status: dict[str, int] = {}
    accounts: set[int] = set()
    for created_at, status, account_id in rows:
        if not created_at or created_at.strftime("%Y-%m-%d") != date:
            continue
        key = status or "unknown"
        by_status[key] = by_status.get(key, 0) + 1
        if account_id is not None:
            accounts.add(account_id)

    return {
        "orders_today": sum(by_status.values()),
        "by_status": by_status,
        "accounts_seen": len(accounts),
    }


def _collect_sandbox_orders(date: str) -> int:
    """Cross-check count of sandbox orders (separate DB)."""
    from database import sandbox_db as sbdb

    sess = sbdb.db_session
    try:
        rows = sess.query(sbdb.SandboxOrders.order_timestamp).all()
    finally:
        sess.remove()

    count = 0
    for (stamp,) in rows:
        if stamp is None:
            continue
        text = stamp if isinstance(stamp, str) else stamp.strftime("%Y-%m-%d")
        if str(text)[:10] == date:
            count += 1
    return count


def _collect_logs(date: str) -> dict[str, Any]:
    from services.postmarket_log_digest import build_log_digest

    return build_log_digest(date)


def _is_trading_day(date: str) -> bool | None:
    """Weekday-and-not-an-NSE-holiday, or ``None`` if the calendar is unavailable."""
    try:
        from services.data_freshness_service import is_trading_day

        return bool(is_trading_day(datetime.strptime(date, "%Y-%m-%d").date()))
    except Exception:
        logger.exception("postmarket_day_digest: trading-day check failed for %s", date)
        return None


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def build_day_digest(target_date: str | date_cls | None = None) -> dict[str, Any]:
    """Assemble the full read-only picture of ``target_date``.

    Args:
        target_date: IST calendar date (``YYYY-MM-DD`` or ``date``). Defaults to
            today in IST.

    Returns:
        A dict with one key per section, ``sources_failed`` naming any section
        that degraded to ``None``, and ``generated_at``. Never raises.
    """
    date = _normalise_date(target_date)
    started = datetime.utcnow()
    collector = _Collector(date)

    collector.add("jobs", lambda: _collect_jobs(date))
    collector.add("trade_journal", lambda: _collect_trade_journal(date))
    collector.add("signals", lambda: _collect_signals(date))
    collector.add("scanner", lambda: _collect_scanner(date))
    collector.add("strategy_journals", lambda: _collect_strategy_journals(date))
    collector.add("futures_carry", lambda: _collect_futures_carry(date))
    collector.add("futures_eval", lambda: _collect_futures_eval(date))
    collector.add("data_health", lambda: _collect_data_health(date))
    collector.add("scanner_comparison", lambda: _collect_scanner_comparison(date))
    collector.add("mirror", lambda: _collect_mirror(date))
    collector.add("sandbox_orders", lambda: _collect_sandbox_orders(date))
    collector.add("logs", lambda: _collect_logs(date))

    elapsed_ms = int((datetime.utcnow() - started).total_seconds() * 1000)
    digest: dict[str, Any] = {
        "date": date,
        "generated_at": datetime.utcnow().isoformat(),
        "elapsed_ms": elapsed_ms,
        "is_trading_day": _is_trading_day(date),
        "sources_failed": collector.failed,
    }
    digest.update(collector.sections)
    return digest
