"""Pure helpers for the open15 decision-log history UI (issue #444).

Parses persisted day-log event lists (``open15_day_logs.log_json``) into
per-day digests and per-symbol selection-outcome rows, so the blueprint's
``/api/decision_log/days`` and ``/api/decision_log/export.csv`` endpoints
and their tests share one implementation. No DB access, no Flask — pure
functions over the event dicts written by ``Open15BreakoutService._log_event``.
"""

from __future__ import annotations

import csv
import io
from typing import Any

CSV_COLUMNS = [
    "date",
    "symbol",
    "side",
    # seed (09:16 gap ranking) vs rolling (intraday re-rank) — issue #529
    "watch_source",
    "gap_pct",
    "entered",
    "level",
    "level_broken",
    "max_vol_ratio",
    "vol_needed",
    "trigger_price",
    "vol_ratio_at_trigger",
    "qty",
    "exit_price",
    "pnl",
    "skip_reason",
    # real fill vs broker-rejected paper simulation (issue #548) — load-bearing
    # for any downstream scoring: a paper row's P&L never actually happened
    "fill",
    "error_message",
]


def summarize_day(
    date: str,
    events: list[dict[str, Any]],
    trades_pnl: float | None = None,
    paper_pnl: float | None = None,
) -> dict:
    """One-line digest of a day's decision log for the history sidebar.

    ``entered`` counts REAL fills only. Broker-rejected entries (issue #548) are
    counted and priced separately as ``paper`` / ``paper_pnl`` — a simulated day
    must never be summed into a day's P&L, or the sidebar shows money that was
    never made.
    """
    status = "unknown"
    selected = 0
    entered = 0
    entry_syms: set[str] = set()
    paper_syms: set[str] = set()
    rolling_added = 0  # symbols appended intraday by the rolling watch list (#529)
    pnl_from_events = 0.0
    paper_from_events = 0.0
    saw_exit_pnl = False
    saw_paper_pnl = False
    for ev in events:
        kind = ev.get("event")
        if kind in ("skipped_late_boot", "skipped_no_prev_closes"):
            status = kind
        elif kind == "armed" and status == "unknown":
            status = "armed"
        elif kind == "selection":
            selected = len(ev.get("selected") or {})
        elif kind == "watchlist_add":
            rolling_added += 1
        elif kind == "entry" and ev.get("order_status") == "success":
            entry_syms.add(ev.get("symbol", ""))
        elif kind == "entry_rejected":
            paper_syms.add(ev.get("symbol", ""))
        elif kind == "exit" and ev.get("pnl") is not None:
            pnl_from_events += float(ev["pnl"])
            saw_exit_pnl = True
        elif kind == "exit_paper" and ev.get("gross") is not None:
            paper_from_events += float(ev["gross"])
            saw_paper_pnl = True
        elif kind == "summary":
            status = ev.get("day") or status
            selected = ev.get("selected", selected)
    entered = len(entry_syms)
    pnl = trades_pnl if trades_pnl is not None else (pnl_from_events if saw_exit_pnl else None)
    ppnl = paper_pnl if paper_pnl is not None else (paper_from_events if saw_paper_pnl else None)
    return {
        "date": date,
        "status": status,
        "selected": selected,
        "rolling_added": rolling_added,
        "entered": entered,
        "paper": len(paper_syms),
        "pnl": round(pnl, 2) if pnl is not None else None,
        "paper_pnl": round(ppnl, 2) if ppnl is not None else None,
        "events": len(events),
    }


def selection_outcomes(date: str, events: list[dict[str, Any]]) -> list[dict]:
    """Per-selected-symbol outcome rows — the backtest-facing flattening.

    One row per symbol in the day's ``selection`` event, enriched from the
    matching ``entry`` / ``exit`` / ``no_entry`` / ``entry_skipped`` events.
    Days with no selection (skipped / dead feed) yield no rows.
    """
    rows: dict[str, dict] = {}
    for ev in events:
        kind = ev.get("event")
        if kind == "selection":
            gaps = ev.get("gaps_pct") or {}
            for sym, side in (ev.get("selected") or {}).items():
                # a row already tagged `rolling` was proven so by its own
                # `watchlist_add`, which pre-#545 was logged EARLIER in the same
                # tick than this event — never downgrade it back to `seed`
                if rows.get(sym, {}).get("watch_source") == "rolling":
                    continue
                rows[sym] = dict.fromkeys(CSV_COLUMNS)
                rows[sym].update(
                    date=date,
                    symbol=sym,
                    side=side,
                    watch_source="seed",
                    gap_pct=gaps.get(sym),
                    entered=False,
                )
        elif kind == "watchlist_add":
            # a rolling add is a watched symbol too (issue #529) — it gets its
            # own outcome row, tagged so the two cohorts can be scored apart.
            # `gap_pct` carries its % change AT ADD (there is no 09:15 gap for
            # a symbol the 09:16 ranking never picked).
            #
            # A `watchlist_add` is PROOF of a rolling add and therefore wins
            # over the `selection` event (issue #545): `maybe_rerank` skips any
            # symbol already in `selected`, so a genuine seed pick can never
            # emit one. Day logs written before #545 recorded the first
            # re-rank pass's adds inside `selection` too — overriding here
            # repairs those historical rows instead of leaving them as `seed`
            # carrying an open gap in the %-at-add column.
            sym = ev.get("symbol")
            if not sym:
                continue
            if sym not in rows:
                rows[sym] = dict.fromkeys(CSV_COLUMNS)
                rows[sym]["entered"] = False
            rows[sym].update(
                date=date,
                symbol=sym,
                side=ev.get("side"),
                watch_source="rolling",
                gap_pct=ev.get("pct_change"),
            )
        elif kind == "entry":
            sym = ev.get("symbol")
            if sym not in rows:
                continue
            rows[sym].update(
                entered=ev.get("order_status") == "success",
                level=ev.get("level"),
                trigger_price=ev.get("trigger_price"),
                vol_ratio_at_trigger=ev.get("vol_ratio"),
                qty=ev.get("qty"),
            )
        elif kind == "watch_stats":
            # every selected symbol, entered ones included (issue #524). Only
            # fills what is still unset, so the per-symbol `no_entry` event
            # below keeps precedence and pre-#524 days parse identically.
            for sym, st in (ev.get("stats") or {}).items():
                if sym not in rows:
                    continue
                if rows[sym].get("max_vol_ratio") is None:
                    rows[sym]["max_vol_ratio"] = st.get("max_vol_ratio")
                if rows[sym].get("level_broken") is None:
                    rows[sym]["level_broken"] = st.get("level_broken")
                if rows[sym].get("vol_needed") is None:
                    rows[sym]["vol_needed"] = ev.get("needed")
                if rows[sym].get("watch_source") is None and st.get("watch_source"):
                    rows[sym]["watch_source"] = st["watch_source"]
        elif kind == "no_entry":
            sym = ev.get("symbol")
            if sym not in rows:
                continue
            rows[sym].update(
                level_broken=ev.get("level_broken"),
                max_vol_ratio=ev.get("max_vol_ratio"),
                vol_needed=ev.get("needed"),
            )
        elif kind == "entry_rejected":
            # a rejected entry is a real measurement, just not a real fill — it
            # keeps its outcome row so the CSV stays complete (issue #548)
            sym = ev.get("symbol")
            if sym not in rows:
                continue
            rows[sym].update(
                entered=False,
                fill="paper",
                qty=ev.get("qty"),
                trigger_price=ev.get("entry_price"),
                skip_reason="entry_rejected",
                error_message=ev.get("error"),
            )
        elif kind in ("exit", "exit_paper"):
            sym = ev.get("symbol")
            if sym not in rows:
                continue
            rows[sym].update(exit_price=ev.get("exit_price"), pnl=ev.get("pnl"))
            if kind == "exit_paper":
                rows[sym]["fill"] = "paper"
        elif kind == "entry_skipped":
            sym = ev.get("symbol")
            if sym in rows:
                rows[sym]["skip_reason"] = ev.get("reason")
    return list(rows.values())


def render_csv(outcome_rows: list[dict]) -> str:
    """Serialize outcome rows (all days) to a CSV string, header included."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in outcome_rows:
        writer.writerow(row)
    return buf.getvalue()
