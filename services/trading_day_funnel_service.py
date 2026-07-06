"""Trading-day funnel: end-of-session diagnostic for the signal → order pipeline.

The motivating incident: on 2026-06-26 the simplified engine, sector_follow, and
scanner_service all booted and stayed healthy, decision-input completeness ran
100% all day, yet **zero trades** landed in ``trade_journal`` and zero rows in
``sandbox_orders``. The day looked healthy from every individual layer's view;
nothing alerted because no single layer was unhealthy. That class of silent
failure is what this funnel exists to make impossible to miss.

What it does
============

Once per trading day (mon-fri 15:35 IST, just after the EOD watchdog and the
sandbox MIS square-off but before scanner_comparison_eod's 15:45 slot) the
service walks the signal-to-execution chain and renders a single Telegram
summary covering every layer's count for the day:

* **Scanner hits** — distinct symbols flagged by ``scan_results`` rows whose
  ``run_at`` IST date matches today, broken down by ``source``
  (``inhouse`` vs ``chartink``).
* **Engine signals** — rows in ``signal_decision`` whose ``candidate_at`` IST
  date matches today, broken down by ``decision`` (taken vs skipped/vetoed).
* **Orders attempted** — rows in ``trade_journal`` whose ``placed_at`` IST
  date matches today, grouped by ``strategy_name``.
* **Orders filled** — same rows, filtered to ``entry_fill_at IS NOT NULL``.
* **Sandbox orders** — same date filter on ``sandbox_orders.order_timestamp``
  (cross-check against the engine's view).
* **Open at EOD** — ``trade_journal`` rows where ``exit_price IS NULL``
  (whether the EOD watchdog already squared them off or not).

The funnel then computes a one-line drop-off verdict — *hits→signals*,
*signals→orders*, *orders→fills* — and, for each drop where ``K < M``, names
the first symbol dropped and the reason captured in ``signal_decision.reasoning``
(falls back to ``"no reasoning recorded"`` when null). The next "zero trades"
day will produce a Telegram message at 15:35 IST that says *exactly* which
layer dropped the signal, no forensic SQL required.

Design rules (carried over from sibling services)
-------------------------------------------------

* **Read-only.** This service writes nothing to any database — it queries
  ``scan_results``, ``signal_decision``, ``trade_journal`` (openalgo.db) and
  ``sandbox_orders`` (sandbox.db) and assembles a message. It never mutates
  trading state, never re-triggers a signal, never rewrites a journal row.
* **Fail-graceful.** Every per-layer query is wrapped — one bad row or a
  transient DB error degrades the corresponding count to ``None`` and the
  formatter prints ``"?"`` for that layer rather than aborting the whole
  message. A total failure inside the scheduler job is logged via
  ``logger.exception`` and swallowed (never raised back into APScheduler).
* **Telegram via the standard notification path.** Routes through
  ``services.notification_service.notify("trading_day_funnel", message)`` so
  the Phase 6 inbound-bot fallback delivers when the legacy outbound bot is
  inactive; the operator's existing per-event ``NOTIFY_TRADING_DAY_FUNNEL``
  toggle gates it (default true).
* **Feature flag.** Master switch ``TRADING_DAY_FUNNEL_ENABLED`` (env, default
  ``true``). Fire time overridable via ``TRADING_DAY_FUNNEL_TIME`` (default
  ``15:35``). Both are read per-fire in the job body so flipping the flag
  needs only a restart.

Scheduler: a single ``trading_day_funnel`` APScheduler job, registered at app
boot next to the EOD reconciliation and scanner_comparison jobs.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = pytz.timezone("Asia/Kolkata")
_DEFAULT_TIME = "15:35"


def _today_ist() -> str:
    """Today's date in IST, ``YYYY-MM-DD``. Indirected for testability."""
    return _dt.datetime.now(_IST).strftime("%Y-%m-%d")


def _now_ist_iso() -> str:
    return _dt.datetime.now(_IST).isoformat()


# --------------------------------------------------------------------------- #
# Per-layer counters. Each returns a small dict; any DB error degrades the
# count to a sentinel (None or empty dict) rather than raising — the funnel
# is diagnostic, never trading-critical, so a partial render beats no render.
# --------------------------------------------------------------------------- #


def _parse_symbol_list(blob: Any) -> list[str]:
    """Parse a ``scan_results.symbols`` JSON list into a list of upper-cased symbols.

    Returns ``[]`` on null / malformed input — one bad row never poisons the
    union, since every counter folds via ``set().update(...)``.
    """
    if not blob:
        return []
    try:
        items = json.loads(blob)
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for s in items:
        if s is None:
            continue
        sym = str(s).strip().upper()
        if sym:
            out.append(sym)
    return out


def _count_scanner_hits(date: str) -> dict[str, Any]:
    """Count distinct symbols flagged by the scanner today, per ``source``.

    Returns ``{"inhouse": N, "chartink": N, "total": N, "by_source": {…}}``.
    A query failure returns ``{"inhouse": None, "chartink": None, "total": None}``
    so the renderer can show ``"?"`` for the layer.
    """
    try:
        from database import scanner_db as srdb

        sess = srdb.db_session
        try:
            rows = sess.query(
                srdb.ScanResult.symbols, srdb.ScanResult.source, srdb.ScanResult.run_at
            ).all()
        finally:
            sess.remove()
    except Exception:
        logger.exception("trading_day_funnel: scanner-hits query failed")
        return {"inhouse": None, "chartink": None, "total": None, "by_source": {}}

    by_source: dict[str, set[str]] = {}
    for symbols_blob, source, run_at in rows:
        if not run_at or run_at[:10] != date:
            continue
        key = (source or "unknown").lower()
        bucket = by_source.setdefault(key, set())
        bucket.update(_parse_symbol_list(symbols_blob))

    total: set[str] = set()
    for s in by_source.values():
        total |= s

    return {
        "inhouse": len(by_source.get("inhouse", set())),
        "chartink": len(by_source.get("chartink", set())),
        "total": len(total),
        "by_source": {k: sorted(v) for k, v in by_source.items()},
    }


def _count_engine_signals(date: str) -> dict[str, Any]:
    """Count ``signal_decision`` rows for today, split by ``actually_taken``.

    Returns counts plus the FIRST dropped (``actually_taken=0``) row's
    ``(symbol, reasoning)`` so the formatter can name the dropped signal.
    """
    try:
        from database import signal_decision_db as sddb

        sess = sddb.db_session
        try:
            rows = sess.query(
                sddb.SignalDecision.candidate_at,
                sddb.SignalDecision.symbol,
                sddb.SignalDecision.decision,
                sddb.SignalDecision.actually_taken,
                sddb.SignalDecision.reasoning,
                sddb.SignalDecision.source,
            ).all()
        finally:
            sess.remove()
    except Exception:
        logger.exception("trading_day_funnel: engine-signals query failed")
        return {
            "total": None,
            "taken": None,
            "vetoed": None,
            "first_dropped": None,
            "by_source": {},
        }

    today_rows = [r for r in rows if r[0] and r[0][:10] == date]
    taken = [r for r in today_rows if r[3] == 1]
    vetoed = [r for r in today_rows if r[3] == 0]

    first_dropped: dict[str, str] | None = None
    if vetoed:
        # Earliest by candidate_at — list order on the dialect isn't guaranteed.
        v = min(vetoed, key=lambda r: r[0] or "")
        first_dropped = {
            "symbol": v[1] or "?",
            "reason": (v[4] or "no reasoning recorded").strip()[:140],
            "source": v[5] or "?",
        }

    by_source: dict[str, int] = {}
    for r in today_rows:
        by_source[r[5] or "unknown"] = by_source.get(r[5] or "unknown", 0) + 1

    return {
        "total": len(today_rows),
        "taken": len(taken),
        "vetoed": len(vetoed),
        "first_dropped": first_dropped,
        "by_source": by_source,
    }


def _count_strategy_orders(date: str) -> dict[str, dict[str, int | None]]:
    """Per-strategy ``trade_journal`` counts for today.

    Returns ``{strategy_name: {"attempted": K, "filled": J, "open": O,
    "closed": C}}``. ``open`` = ``exit_price IS NULL``; ``closed`` = the
    complement; ``filled`` is independent (an entry confirmed by the broker
    but maybe not yet exited).
    """
    try:
        from database import trade_journal_db as tjdb

        sess = tjdb.db_session
        try:
            rows = sess.query(
                tjdb.TradeJournal.strategy_name,
                tjdb.TradeJournal.placed_at,
                tjdb.TradeJournal.entry_fill_at,
                tjdb.TradeJournal.exit_price,
            ).all()
        finally:
            sess.remove()
    except Exception:
        logger.exception("trading_day_funnel: strategy-orders query failed")
        return {}

    out: dict[str, dict[str, int | None]] = {}
    for strategy, placed_at, entry_fill_at, exit_price in rows:
        if not placed_at or placed_at[:10] != date:
            continue
        bucket = out.setdefault(
            strategy or "unknown",
            {"attempted": 0, "filled": 0, "open": 0, "closed": 0},
        )
        bucket["attempted"] += 1
        if entry_fill_at:
            bucket["filled"] += 1
        if exit_price is None:
            bucket["open"] += 1
        else:
            bucket["closed"] += 1
    return out


def _count_sandbox_orders(date: str) -> dict[str, Any]:
    """Cross-check count of ``sandbox_orders`` rows for today, per strategy.

    Independent of the engine's ``trade_journal`` view — if these two ever
    disagree it usually means an order made it to sandbox but the journal
    write hook failed (the C1/C1b half-update pattern from #167).
    """
    try:
        from database import sandbox_db as sbdb

        sess = sbdb.db_session
        try:
            rows = sess.query(
                sbdb.SandboxOrders.strategy,
                sbdb.SandboxOrders.order_timestamp,
                sbdb.SandboxOrders.order_status,
            ).all()
        finally:
            sess.remove()
    except Exception:
        logger.exception("trading_day_funnel: sandbox-orders query failed")
        return {"total": None, "by_strategy": {}}

    by_strategy: dict[str, int] = {}
    total = 0
    for strategy, ts, _status in rows:
        if ts is None:
            continue
        # sandbox_orders.order_timestamp is a DATETIME; coerce to ISO via str()
        # and slice — handles both string and datetime adapters.
        ts_str = str(ts)
        if not ts_str.startswith(date):
            continue
        key = strategy or "unknown"
        by_strategy[key] = by_strategy.get(key, 0) + 1
        total += 1
    return {"total": total, "by_strategy": by_strategy}


# --------------------------------------------------------------------------- #
# Composition + formatting.
# --------------------------------------------------------------------------- #


def compute_funnel(date: str | None = None) -> dict[str, Any]:
    """Walk every layer and return a single dict for the formatter / tests."""
    if date is None:
        date = _today_ist()
    return {
        "date": date,
        "hits": _count_scanner_hits(date),
        "signals": _count_engine_signals(date),
        "strategies": _count_strategy_orders(date),
        "sandbox": _count_sandbox_orders(date),
    }


def _fmt_count(n: int | None) -> str:
    return "?" if n is None else str(n)


def _fmt_drop_arrow(numerator: int | None, denominator: int | None) -> str:
    """Render ``"K/M (XX%)"`` with sane fallbacks.

    Zero-denominator yields ``"K/0"`` rather than dividing — a day with zero
    hits and zero signals is a valid (quiet-market) outcome, not an error.
    """
    n = _fmt_count(numerator)
    d = _fmt_count(denominator)
    if numerator is None or denominator is None:
        return f"{n}/{d}"
    if denominator == 0:
        return f"{n}/{d}"
    pct = (numerator / denominator) * 100.0
    return f"{n}/{d} ({pct:.0f}%)"


def _format_telegram(result: dict[str, Any]) -> str:
    """Render the funnel dict as a concise Telegram-flavoured markdown summary."""
    date = result["date"]
    hits = result["hits"]
    signals = result["signals"]
    strategies = result["strategies"]
    sandbox = result["sandbox"]

    lines: list[str] = [f"📊 *Trading day funnel* — {date}"]

    # Layer 1 — scanner
    hits_total = _fmt_count(hits.get("total"))
    inhouse = _fmt_count(hits.get("inhouse"))
    chartink = _fmt_count(hits.get("chartink"))
    lines.append(f"├ Scanner hits: {hits_total} (inhouse={inhouse}, chartink={chartink})")

    # Layer 2 — engine signals
    sig_total = _fmt_count(signals.get("total"))
    taken = _fmt_count(signals.get("taken"))
    vetoed = _fmt_count(signals.get("vetoed"))
    lines.append(f"├ Engine signals: {sig_total} (taken={taken}, vetoed={vetoed})")

    # Layer 3 — orders per strategy
    if not strategies:
        lines.append("├ Orders: — (no strategy activity)")
    else:
        lines.append("├ Orders (per strategy):")
        for name in sorted(strategies.keys()):
            s = strategies[name]
            lines.append(
                f"│   • {name}: attempted={s['attempted']}, "
                f"filled={s['filled']}, open_eod={s['open']}, closed={s['closed']}"
            )

    # Layer 4 — sandbox cross-check
    sb_total = _fmt_count(sandbox.get("total"))
    lines.append(f"├ Sandbox orders today: {sb_total}")

    # Drop-off verdict — name the first layer where K < M.
    lines.append("└ Drop-off:")
    lines.append(f"    hits→signals: {_fmt_drop_arrow(signals.get('total'), hits.get('total'))}")

    orders_attempted_total = sum(
        s["attempted"] for s in strategies.values() if isinstance(s.get("attempted"), int)
    )
    lines.append(
        f"    signals_taken→orders: {_fmt_drop_arrow(orders_attempted_total, signals.get('taken'))}"
    )

    orders_filled_total = sum(
        s["filled"] for s in strategies.values() if isinstance(s.get("filled"), int)
    )
    lines.append(
        f"    orders→fills: {_fmt_drop_arrow(orders_filled_total, orders_attempted_total)}"
    )

    # Name the first dropped signal so the operator knows where to start.
    dropped = signals.get("first_dropped")
    if dropped:
        lines.append(
            f"    first vetoed: {dropped['symbol']} ({dropped['source']}) — {dropped['reason']}"
        )

    return "\n".join(lines)


def _dispatch_telegram(message: str) -> bool:
    """Route through the standard notification path. Returns True on dispatch."""
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("trading_day_funnel", message)
        return True
    except Exception:
        logger.exception("trading_day_funnel: telegram dispatch failed")
        return False


def run_funnel_for_date(date: str | None = None, dispatch_telegram: bool = True) -> dict[str, Any]:
    """Compute the funnel and optionally Telegram it.

    Returns the computed dict augmented with ``telegram_sent`` and the rendered
    ``telegram_message`` so a one-shot CLI / backfill caller can inspect what
    would have been sent without re-rendering.
    """
    if date is None:
        date = _today_ist()
    result = compute_funnel(date)
    message = _format_telegram(result)
    telegram_sent = _dispatch_telegram(message) if dispatch_telegram else False
    result["telegram_sent"] = telegram_sent
    result["telegram_message"] = message

    # Single-line structured log so the funnel is observable even without
    # Telegram delivery (e.g. flag off, bot down). Mirrors the
    # scanner_comparison_eod info line.
    strategies = result["strategies"]
    attempted_total = sum(
        s["attempted"] for s in strategies.values() if isinstance(s.get("attempted"), int)
    )
    filled_total = sum(s["filled"] for s in strategies.values() if isinstance(s.get("filled"), int))
    logger.info(
        "trading_day_funnel: date=%s hits=%s signals=%s/%s orders=%d fills=%d sandbox=%s "
        "strategies=%s telegram=%s",
        date,
        result["hits"].get("total"),
        result["signals"].get("taken"),
        result["signals"].get("total"),
        attempted_total,
        filled_total,
        result["sandbox"].get("total"),
        sorted(strategies.keys()),
        telegram_sent,
    )
    return result


# --------------------------------------------------------------------------- #
# APScheduler wiring — mirrors scanner_comparison_eod_service.
# --------------------------------------------------------------------------- #


def _funnel_job() -> None:
    """Job body — never raises. Honours the per-fire enable flag."""
    if os.environ.get("TRADING_DAY_FUNNEL_ENABLED", "true").lower() != "true":
        logger.info("trading_day_funnel job disabled (TRADING_DAY_FUNNEL_ENABLED!=true)")
        return
    try:
        run_funnel_for_date(date=None, dispatch_telegram=True)
    except Exception:
        logger.exception("trading_day_funnel job failed")


def _parse_hh_mm(raw: str, default: str = _DEFAULT_TIME) -> tuple[int, int]:
    try:
        hh, mm = raw.strip().split(":")
        return int(hh), int(mm)
    except Exception:
        dh, dm = default.split(":")
        return int(dh), int(dm)


def register_jobs(scheduler=None) -> None:
    """Register the 15:35 IST mon-fri funnel job on the shared scheduler."""
    sched = scheduler
    if sched is None:
        from services.historify_scheduler_service import get_historify_scheduler

        sched = get_historify_scheduler().scheduler

    from apscheduler.triggers.cron import CronTrigger

    hour, minute = _parse_hh_mm(os.environ.get("TRADING_DAY_FUNNEL_TIME", _DEFAULT_TIME))
    sched.add_job(
        _funnel_job,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=hour, minute=minute, timezone="Asia/Kolkata"
        ),
        id="trading_day_funnel",
        replace_existing=True,
        name=f"Trading day funnel summary ({hour:02d}:{minute:02d} IST)",
    )
    logger.info("trading_day_funnel job registered (%02d:%02d IST mon-fri)", hour, minute)


def init_trading_day_funnel_service(scheduler=None) -> None:
    """Boot entry point — register the funnel job. No-op-safe."""
    register_jobs(scheduler)
