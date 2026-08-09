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
    # issue #555. Appended at the END so every existing column keeps its
    # position: an analysis script reading this CSV by index must not break.
    "instrument",
    "opt_symbol",
    "opt_entry_premium",
    "opt_exit_premium",
    "entry_fill_price",
    "exit_fill_price",
    "pnl_source",
]


def summarize_day(
    date: str,
    events: list[dict[str, Any]],
    trades_pnl: float | None = None,
    paper_pnl: float | None = None,
    sim_pnl: float | None = None,
    shadow_pnl: float | None = None,
) -> dict:
    """One-line digest of a day's decision log for the history sidebar.

    ``entered`` counts REAL fills only. There are FOUR P&L buckets and they are
    never summed into one another:

    * ``pnl`` — real fills. The only one that is money.
    * ``paper_pnl`` — the broker REJECTED the entry (issue #548). An order was
      attempted and refused.
    * ``sim_pnl`` — no order was ever attempted (unaffordable / past the daily
      cap, issue #555), priced at 1 lot to answer "would it have paid?".
    * ``shadow_pnl`` — the side is switched off by ``trade_side`` (issue #581),
      priced at full slot size to answer "does the signal work on that side?".

    Keeping them apart is the point: "the broker blocked us", "we could not
    afford it" and "we do not trade that side" are different facts about a day,
    and a blended figure states none of them while looking authoritative.

    All four are **NET** of modelled charges (issue #552), matching the
    per-symbol rows and ``trades_pnl_by_date`` / ``paper_pnl_by_date`` /
    ``sim_pnl_by_date`` / ``shadow_pnl_by_date``. There is one P&L convention
    here; do not reintroduce a gross one.
    """
    status = "unknown"
    selected = 0
    entered = 0
    entry_syms: set[str] = set()
    paper_syms: set[str] = set()
    sim_syms: set[str] = set()
    shadow_syms: set[str] = set()
    rolling_added = 0  # symbols appended intraday by the rolling watch list (#529)
    pnl_from_events = 0.0
    paper_from_events = 0.0
    sim_from_events = 0.0
    shadow_from_events = 0.0
    saw_exit_pnl = False
    saw_paper_pnl = False
    saw_sim_pnl = False
    saw_shadow_pnl = False
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
        elif kind == "entry_skipped" and ev.get("fill") == "sim":
            sim_syms.add(ev.get("symbol", ""))
        elif kind == "entry_shadow":
            shadow_syms.add(ev.get("symbol", ""))
        elif kind == "exit" and ev.get("pnl") is not None:
            pnl_from_events += float(ev["pnl"])
            saw_exit_pnl = True
        elif kind == "exit_paper" and ev.get("pnl") is not None:
            # `pnl`, NOT `gross` (issue #552) — the digest must agree with the
            # per-symbol rows the page renders from the same events, and those
            # read `pnl`. Both events carry gross/charges/pnl(net) alike.
            paper_from_events += float(ev["pnl"])
            saw_paper_pnl = True
        elif kind == "exit_sim" and ev.get("pnl") is not None:
            sim_from_events += float(ev["pnl"])
            saw_sim_pnl = True
        elif kind == "exit_shadow" and ev.get("pnl") is not None:
            shadow_from_events += float(ev["pnl"])
            saw_shadow_pnl = True
        elif kind == "summary":
            status = ev.get("day") or status
            selected = ev.get("selected", selected)
    entered = len(entry_syms)
    pnl = trades_pnl if trades_pnl is not None else (pnl_from_events if saw_exit_pnl else None)
    ppnl = paper_pnl if paper_pnl is not None else (paper_from_events if saw_paper_pnl else None)
    spnl = sim_pnl if sim_pnl is not None else (sim_from_events if saw_sim_pnl else None)
    shpnl = (
        shadow_pnl if shadow_pnl is not None else (shadow_from_events if saw_shadow_pnl else None)
    )
    return {
        "date": date,
        "status": status,
        "selected": selected,
        "rolling_added": rolling_added,
        "entered": entered,
        "paper": len(paper_syms),
        "sim": len(sim_syms),
        "shadow": len(shadow_syms),
        "pnl": round(pnl, 2) if pnl is not None else None,
        "paper_pnl": round(ppnl, 2) if ppnl is not None else None,
        "sim_pnl": round(spnl, 2) if spnl is not None else None,
        "shadow_pnl": round(shpnl, 2) if shpnl is not None else None,
        "events": len(events),
    }


# Fields the JOURNAL owns (issue #557). The journal is corrected in place by the
# reconcile passes; the decision log is a record of what was believed AT THE
# TIME. So anything the journal stores must be read from the journal, and the
# events keep only what they alone know — the decision timeline.
#
# The JS in ``blueprints/open15_breakout.py`` applies the same overlay; the two
# are kept honest by ``test_logs_page_js_and_python_agree_*``.
_JOURNAL_OWNED = {
    "trigger_price": "trigger_price",
    "exit_price": "exit_price",
    "instrument": "instrument",
    "opt_symbol": "opt_symbol",
    "opt_entry_premium": "opt_entry_premium",
    "opt_exit_premium": "opt_exit_premium",
    "entry_fill_price": "entry_fill_price",
    "exit_fill_price": "exit_fill_price",
    "pnl_source": "pnl_source",
    "fill": "fill",
    "error_message": "error_message",
    "skip_reason": "reason",
    "level": "level",
}


def apply_journal(rows: dict[str, dict], journal: list[dict] | None) -> None:
    """Overlay authoritative journal values onto event-derived outcome rows.

    Mutates ``rows`` in place. A symbol with no journal row is left untouched —
    it never triggered, so there is nothing to be authoritative about.

    **``pnl`` is NET here** (issue #552): the journal stores gross in ``pnl``
    with charges separate in ``charges_inr``, while these outcome rows — and the
    day digest they must agree with — are net throughout. Copying the journal's
    ``pnl`` straight across would silently reintroduce the gross/net split that
    #552 removed, in the exact place #557 was opened to fix.
    """
    if not journal:
        return
    from database.open15_breakout_db import net_pnl_of_row

    for jr in journal:
        row = rows.get(jr.get("symbol"))
        if row is None:
            continue
        for col, key in _JOURNAL_OWNED.items():
            value = jr.get(key)
            if value is not None:
                row[col] = value
        # size: `quantity` is what was ORDERED, `sim_quantity` what a non-traded
        # row is priced on — exactly one of them is meaningful per row
        row["qty"] = jr.get("quantity") or jr.get("sim_quantity")
        if jr.get("pnl") is not None:
            row["pnl"] = round(net_pnl_of_row(jr), 2)


def selection_outcomes(
    date: str, events: list[dict[str, Any]], journal: list[dict] | None = None
) -> list[dict]:
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
                # both legs (issue #555): in option mode `trigger_price` is the
                # STOCK price while the P&L is on the premium, so a row carrying
                # only one of them cannot be reconciled to its own P&L
                instrument=ev.get("instrument") or "stock",
                opt_symbol=ev.get("contract"),
                opt_entry_premium=ev.get("premium"),
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
                instrument=ev.get("instrument") or "stock",
                opt_symbol=ev.get("contract"),
            )
        elif kind == "entry_shadow":
            # the switched-off side (issue #581): a full measurement row, but no
            # order was placed for it — `entered` stays False so it can never be
            # counted as a fill anywhere downstream
            sym = ev.get("symbol")
            if sym not in rows:
                continue
            rows[sym].update(
                entered=False,
                fill="shadow",
                qty=ev.get("qty"),
                trigger_price=ev.get("trigger_price"),
                skip_reason=ev.get("reason"),
                vol_ratio_at_trigger=ev.get("vol_ratio"),
                instrument=ev.get("instrument") or "stock",
                opt_symbol=ev.get("contract"),
                opt_entry_premium=(
                    ev.get("entry_price") if ev.get("instrument") == "option" else None
                ),
            )
        elif kind in ("exit", "exit_paper", "exit_sim", "exit_shadow"):
            sym = ev.get("symbol")
            if sym not in rows:
                continue
            rows[sym].update(exit_price=ev.get("exit_price"), pnl=ev.get("pnl"))
            if ev.get("instrument") == "option":
                rows[sym]["opt_exit_premium"] = ev.get("exit_price")
                if rows[sym].get("opt_entry_premium") is None:
                    rows[sym]["opt_entry_premium"] = ev.get("entry_price")
            if kind == "exit_paper":
                rows[sym]["fill"] = "paper"
            elif kind in ("exit_sim", "exit_shadow"):
                # these carry a qty that is a PRICING size, not an order size
                rows[sym].update(fill=ev.get("fill") or "sim", qty=ev.get("qty"))
        elif kind == "entry_skipped":
            sym = ev.get("symbol")
            if sym in rows:
                rows[sym]["skip_reason"] = ev.get("reason")
                if ev.get("fill") == "sim":
                    rows[sym]["fill"] = "sim"
                if ev.get("opt_symbol"):
                    rows[sym].update(
                        instrument="option",
                        opt_symbol=ev.get("opt_symbol"),
                        opt_entry_premium=ev.get("opt_entry_premium"),
                    )
    # The journal wins, last (issue #557). `fill_reconcile_row` / `liquidity_row`
    # events used to populate these fields, which left them stale whenever a
    # reconcile landed after the day log was sealed — including the ARM-TIME
    # CATCH-UP, whose whole purpose is to run on a later day. Those events stay
    # in the timeline as an audit of when reconciliation happened; they no
    # longer feed a row.
    apply_journal(rows, journal)
    return list(rows.values())


def render_csv(outcome_rows: list[dict]) -> str:
    """Serialize outcome rows (all days) to a CSV string, header included."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in outcome_rows:
        writer.writerow(row)
    return buf.getvalue()
