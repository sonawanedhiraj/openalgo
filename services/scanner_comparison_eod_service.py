"""EOD in-house-scanner-vs-Chartink comparison job.

This is the OpenAlgo-resident replacement for the Cowork-side
``scanner-vs-chartink-daily-comparison`` scheduled task. That task ran in a
sandbox without repo/folder access and silently failed (no comparison was ever
persisted). Running the comparison inside OpenAlgo — where ``scan_cycle`` (the
Chartink webhook record) and ``scan_results`` (the in-house scanner output) both
live — makes the result durable: a row in ``scanner_comparison`` AND a Telegram
summary, every trading day.

What it compares, per side (BUY / SELL):

* **Chartink** = the union of the symbol lists Chartink posted via webhook,
  recorded in ``scan_cycle`` rows with ``cycle_kind='chartink'`` for the day
  (``screener_buy`` for BUY, ``screener_sell`` for SELL).
* **In-house** = the union of the symbols the live tick-driven ``ScannerService``
  flagged that day — ``scan_results`` rows with ``source='inhouse'``, grouped by
  the ``screener_type`` of the matched ``scan_definition``.

Treating Chartink as ground truth, ``inhouse_only`` names are false positives and
``chartink_only`` names are false negatives. We report counts, the Jaccard index,
the recall ratio (intersection / chartink), the top diff names each way, and a
one-line tuning suggestion.

**Caveat (load-bearing for interpretation):** the in-house side reflects what the
*live tick-driven* scanner actually produced. The scanner is downstream of the
broker tick feed and only sees ticks the engine subscribed (see the
"in-house scanner starved" learning) — so a disjoint result is usually tick
starvation, not a threshold mismatch. The tuning suggestion calls this out.

Scheduler: a single ``scanner_comparison_eod`` APScheduler job at 15:45 IST
mon-fri (matching the retired Cowork task's cron), registered at app boot next to
the sector_follow jobs. Gated by ``SCANNER_COMPARISON_EOD_ENABLED`` (default true);
fire time overridable via ``SCANNER_COMPARISON_EOD_TIME`` (default ``15:45``).
Read-only on every database except its own ``scanner_comparison`` table.
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

# The in-house scan_definition names whose screener_type maps each side. Names
# are stable in scan_definitions; we group by the joined screener_type rather
# than hard-coding names, so this is informational only.
_DEFAULT_TIME = "15:45"


def _today_ist() -> str:
    return _dt.datetime.now(_IST).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return _dt.datetime.now(_IST).isoformat()


def _symbol_set_from_json(blob: Any) -> set[str]:
    """Parse a JSON-encoded list of symbols into a clean upper-cased set.

    Tolerant of ``None``, malformed JSON, and non-list payloads — returns an
    empty set rather than raising, so a single bad audit row can't sink the
    whole comparison.
    """
    if not blob:
        return set()
    try:
        items = json.loads(blob)
    except (ValueError, TypeError):
        return set()
    if not isinstance(items, list):
        return set()
    out: set[str] = set()
    for s in items:
        if s is None:
            continue
        sym = str(s).strip().upper()
        if sym:
            out.add(sym)
    return out


def _chartink_sets(date: str) -> tuple[set[str], set[str]]:
    """Union of Chartink BUY / SELL symbols posted via webhook for ``date``.

    Reads ``scan_cycle`` rows with ``cycle_kind='chartink'`` whose ``started_at``
    IST date-prefix matches ``date``. Any other cycle_kind (test/manual) is
    excluded.
    """
    buy: set[str] = set()
    sell: set[str] = set()
    from database import scan_cycle_db as scdb

    sess = scdb.db_session
    try:
        rows = sess.query(scdb.ScanCycle).filter(scdb.ScanCycle.cycle_kind == "chartink").all()
        for row in rows:
            if (row.started_at or "")[:10] != date:
                continue
            buy |= _symbol_set_from_json(row.screener_buy)
            sell |= _symbol_set_from_json(row.screener_sell)
    finally:
        sess.remove()
    return buy, sell


def _inhouse_sets(date: str) -> tuple[set[str], set[str]]:
    """Union of in-house BUY / SELL symbols the scanner flagged for ``date``.

    Reads ``scan_results`` rows with ``source='inhouse'`` joined to their
    ``scan_definition`` (for the ``screener_type``), filtered to ``run_at`` IST
    date-prefix == ``date``.
    """
    buy: set[str] = set()
    sell: set[str] = set()
    from database import scanner_db as sdb

    sess = sdb.db_session
    try:
        rows = (
            sess.query(sdb.ScanResult, sdb.ScanDefinition.screener_type)
            .join(sdb.ScanDefinition, sdb.ScanResult.scan_definition_id == sdb.ScanDefinition.id)
            .filter(sdb.ScanResult.source == "inhouse")
            .all()
        )
        for row, screener_type in rows:
            if (row.run_at or "")[:10] != date:
                continue
            syms = _symbol_set_from_json(row.symbols)
            if screener_type == "buy":
                buy |= syms
            elif screener_type == "sell":
                sell |= syms
    finally:
        sess.remove()
    return buy, sell


def _suggest_tuning(inhouse: set[str], chartink: set[str]) -> str:
    """One-line, human-readable tuning verdict for a single side.

    Heuristics, in priority order:
      * both empty                 → parity (no hits either side)
      * disjoint with hits on both → structural mismatch (likely tick starvation)
      * jaccard >= 0.7             → parity (close agreement)
      * recall low, FP high        → too loose (firing on names Chartink ignores)
      * recall low, FP low         → too tight (missing Chartink names)
      * otherwise                  → partial overlap, monitor
    """
    n, m = len(inhouse), len(chartink)
    inter = inhouse & chartink
    k = len(inter)
    union = inhouse | chartink

    if not union:
        return "parity: no hits on either side today"
    if k == 0 and n > 0 and m > 0:
        return (
            "structural mismatch: in-house and Chartink hits are fully disjoint — "
            "most likely in-house tick starvation (scanner only sees engine-subscribed "
            "symbols), not a threshold problem. Check WS subscription coverage before tuning."
        )
    jaccard = k / len(union)
    if jaccard >= 0.7:
        return f"parity: strong agreement (Jaccard {jaccard:.2f})"
    recall = k / m if m else None
    false_pos = n - k
    if recall is not None and recall < 0.5 and false_pos > k:
        return (
            f"too loose: only {k}/{m} Chartink names matched and {false_pos} in-house-only "
            "false positives — tighten the in-house gates."
        )
    if recall is not None and recall < 0.5:
        return (
            f"too tight: only {k}/{m} Chartink names matched with few false positives — "
            "in-house gates are missing Chartink names; loosen or check data coverage."
        )
    return f"partial overlap (Jaccard {jaccard:.2f}) — monitor; no clear loose/tight bias yet"


def _metrics(inhouse: set[str], chartink: set[str], side: str) -> dict[str, Any]:
    """Compute the per-side metric bundle written to the DB / report."""
    inter = inhouse & chartink
    n, m, k = len(inhouse), len(chartink), len(inter)
    union = inhouse | chartink
    jaccard = (k / len(union)) if union else None
    ratio = (k / m) if m else None  # recall against Chartink
    return {
        "screener_side": side,
        "inhouse_count": n,
        "chartink_count": m,
        "intersection_count": k,
        "intersection": sorted(inter),
        "jaccard": jaccard,
        "ratio": ratio,
        # Top-5 diff names each way (full lists are derivable; we cap for the
        # Telegram summary and DB JSON to keep them readable).
        "false_positives": sorted(inhouse - chartink),
        "false_negatives": sorted(chartink - inhouse),
        "tuning_suggestion": _suggest_tuning(inhouse, chartink),
    }


def _parse_symbol_list(blob: Any) -> list[str]:
    """Sorted unique-upper symbol list from a JSON-encoded blob, tolerant of junk.

    Shares the same tolerance contract as ``_symbol_set_from_json`` but preserves
    a deterministic order for the UI timeline (set iteration order is not stable
    across processes).
    """
    return sorted(_symbol_set_from_json(blob))


def get_timeline_for_date(date: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Per-event timeline of every Chartink + in-house screener hit for ``date``.

    Pure read; touches no writable table. Returns::

        {
            "chartink": [ {ts, side, symbols, posted, post_status, cycle_id}, ... ],
            "inhouse":  [ {ts, side, symbols, posted, definition, result_id}, ... ],
        }

    Each event is a single audit-trail row (one ``scan_cycle`` for Chartink, one
    ``scan_results`` for in-house). The UI renders them as side-by-side
    chronologically-sorted tables. ``side`` is ``"BUY"``/``"SELL"``; for Chartink
    a single cycle can carry both sides, so it appears once per side it had
    symbols for.
    """
    if date is None:
        date = _today_ist()

    chartink: list[dict[str, Any]] = []
    from database import scan_cycle_db as scdb

    cyc_sess = scdb.db_session
    try:
        rows = (
            cyc_sess.query(scdb.ScanCycle)
            .filter(scdb.ScanCycle.cycle_kind == "chartink")
            .order_by(scdb.ScanCycle.started_at.asc())
            .all()
        )
        for row in rows:
            ts = row.started_at or ""
            if ts[:10] != date:
                continue
            post_status = (row.post_status or "").lower()
            posted = post_status == "ok"
            for side, blob in (("BUY", row.screener_buy), ("SELL", row.screener_sell)):
                syms = _parse_symbol_list(blob)
                if not syms:
                    continue
                chartink.append(
                    {
                        "ts": ts,
                        "side": side,
                        "symbols": syms,
                        "count": len(syms),
                        "posted": posted,
                        "post_status": row.post_status,
                        "cycle_id": row.id,
                    }
                )
    finally:
        cyc_sess.remove()

    inhouse: list[dict[str, Any]] = []
    from database import scanner_db as sdb

    in_sess = sdb.db_session
    try:
        rows = (
            in_sess.query(sdb.ScanResult, sdb.ScanDefinition.screener_type, sdb.ScanDefinition.name)
            .join(sdb.ScanDefinition, sdb.ScanResult.scan_definition_id == sdb.ScanDefinition.id)
            .filter(sdb.ScanResult.source == "inhouse")
            .order_by(sdb.ScanResult.run_at.asc())
            .all()
        )
        for row, screener_type, def_name in rows:
            ts = row.run_at or ""
            if ts[:10] != date:
                continue
            syms = _parse_symbol_list(row.symbols)
            side = "BUY" if (screener_type or "").lower() == "buy" else "SELL"
            inhouse.append(
                {
                    "ts": ts,
                    "side": side,
                    "symbols": syms,
                    "count": len(syms),
                    "posted": bool(row.posted_to_engine),
                    "definition": def_name,
                    "result_id": row.id,
                }
            )
    finally:
        in_sess.remove()

    return {"chartink": chartink, "inhouse": inhouse}


def compute_comparison(date: str | None = None) -> dict[str, Any]:
    """Compute the BUY + SELL comparison for ``date`` (default today IST).

    Pure read — touches no writable table. Returns a dict::

        {"date": ..., "BUY": {<metrics>}, "SELL": {<metrics>}}
    """
    if date is None:
        date = _today_ist()
    ch_buy, ch_sell = _chartink_sets(date)
    ih_buy, ih_sell = _inhouse_sets(date)
    return {
        "date": date,
        "BUY": _metrics(ih_buy, ch_buy, "BUY"),
        "SELL": _metrics(ih_sell, ch_sell, "SELL"),
    }


def _fmt_side_line(m: dict[str, Any]) -> str:
    """One Telegram line per side: counts + intersection + Jaccard."""
    jac = "—" if m["jaccard"] is None else f"{m['jaccard']:.2f}"
    return (
        f"*{m['screener_side']}*: inhouse={m['inhouse_count']} "
        f"chartink={m['chartink_count']} ∩={m['intersection_count']} "
        f"Jaccard={jac}"
    )


def _format_telegram(result: dict[str, Any]) -> str:
    """Build the concise Telegram summary message (markdown)."""
    lines = [f"📊 *Scanner vs Chartink* — {result['date']}"]
    for side in ("BUY", "SELL"):
        m = result[side]
        lines.append(_fmt_side_line(m))
        fp = m["false_positives"][:5]
        fn = m["false_negatives"][:5]
        if fp:
            lines.append(f"  FP (inhouse-only): {', '.join(fp)}")
        if fn:
            lines.append(f"  FN (chartink-only): {', '.join(fn)}")
        lines.append(f"  ↳ {m['tuning_suggestion']}")
    return "\n".join(lines)


def _dispatch_telegram(message: str) -> bool:
    """Send ``message`` via the notification service (Phase 6 inbound fallback aware).

    Returns True when the send was dispatched (the notification layer never
    raises and no-ops when disabled; we treat a clean call as "sent"). The
    notification service is resolved lazily so tests can monkeypatch it.
    """
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify("scanner_comparison", message)
        return True
    except Exception:
        logger.exception("scanner_comparison: telegram dispatch failed")
        return False


def run_comparison_for_date(
    date: str | None = None, dispatch_telegram: bool = True
) -> dict[str, Any]:
    """Compute, persist (idempotent), and optionally Telegram the comparison.

    This is the callable the APScheduler job and the one-shot backfill both use.
    Writes one ``scanner_comparison`` row per side (delete-then-insert per
    ``(date, side)``), so re-running for the same day overwrites rather than
    duplicates. Returns the computed result dict augmented with ``telegram_sent``
    and the written ``row_ids``.
    """
    if date is None:
        date = _today_ist()

    result = compute_comparison(date)

    message = _format_telegram(result)
    telegram_sent = _dispatch_telegram(message) if dispatch_telegram else False

    from database import scanner_comparison_db as scdb

    run_at = _now_iso()
    row_ids: dict[str, int] = {}
    for side in ("BUY", "SELL"):
        m = result[side]
        row_id = scdb.upsert_comparison(
            date=date,
            screener_side=side,
            inhouse_count=m["inhouse_count"],
            chartink_count=m["chartink_count"],
            intersection_count=m["intersection_count"],
            jaccard=m["jaccard"],
            ratio=m["ratio"],
            false_positives=m["false_positives"],
            false_negatives=m["false_negatives"],
            tuning_suggestion=m["tuning_suggestion"],
            telegram_sent=telegram_sent,
            run_at=run_at,
        )
        row_ids[side] = row_id

    result["telegram_sent"] = telegram_sent
    result["row_ids"] = row_ids
    result["telegram_message"] = message
    logger.info(
        "scanner_comparison EOD: date=%s BUY(ih=%d,ch=%d,∩=%d) SELL(ih=%d,ch=%d,∩=%d) telegram=%s",
        date,
        result["BUY"]["inhouse_count"],
        result["BUY"]["chartink_count"],
        result["BUY"]["intersection_count"],
        result["SELL"]["inhouse_count"],
        result["SELL"]["chartink_count"],
        result["SELL"]["intersection_count"],
        telegram_sent,
    )
    return result


# --------------------------------------------------------------------------- #
# APScheduler wiring — module-level job body + singleton flag (serializable for
# the SQLAlchemy jobstore), mirroring services/sector_follow_service.py.
# --------------------------------------------------------------------------- #


def _eod_comparison_job() -> None:
    """Job body: run the comparison for today, persist + Telegram. Never raises."""
    if os.environ.get("SCANNER_COMPARISON_EOD_ENABLED", "true").lower() != "true":
        logger.info("scanner_comparison EOD job disabled (SCANNER_COMPARISON_EOD_ENABLED!=true)")
        return
    try:
        run_comparison_for_date(date=None, dispatch_telegram=True)
    except Exception:
        logger.exception("scanner_comparison EOD job failed")


def _parse_hh_mm(raw: str, default: str = _DEFAULT_TIME) -> tuple[int, int]:
    """Parse ``HH:MM`` into (hour, minute), falling back to ``default`` on junk."""
    try:
        hh, mm = raw.strip().split(":")
        return int(hh), int(mm)
    except Exception:
        dh, dm = default.split(":")
        return int(dh), int(dm)


def register_jobs(scheduler=None) -> None:
    """Register the 15:45 IST mon-fri EOD comparison job on the shared scheduler.

    Idempotent (``replace_existing=True``). The fire time is read from
    ``SCANNER_COMPARISON_EOD_TIME`` (default 15:45). Registration always happens;
    the per-fire ``SCANNER_COMPARISON_EOD_ENABLED`` gate (checked in the job body)
    is what turns the work on/off, so flipping the flag needs only a restart, not
    a re-registration.
    """
    sched = scheduler
    if sched is None:
        from services.historify_scheduler_service import get_historify_scheduler

        sched = get_historify_scheduler().scheduler

    from apscheduler.triggers.cron import CronTrigger

    hour, minute = _parse_hh_mm(os.environ.get("SCANNER_COMPARISON_EOD_TIME", _DEFAULT_TIME))
    sched.add_job(
        _eod_comparison_job,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=hour, minute=minute, timezone="Asia/Kolkata"
        ),
        id="scanner_comparison_eod",
        replace_existing=True,
        name=f"Scanner vs Chartink EOD comparison ({hour:02d}:{minute:02d} IST)",
    )
    logger.info("scanner_comparison EOD job registered (%02d:%02d IST mon-fri)", hour, minute)


def init_scanner_comparison_eod_service(scheduler=None) -> None:
    """Boot entry point: register the EOD comparison job. No-op-safe."""
    register_jobs(scheduler)
