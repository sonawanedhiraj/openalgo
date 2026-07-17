"""Pure read-model over an intraday_pullback_top2 entry-evaluation payload (issue #422).

The ``intraday_pullback_eval_snapshots`` row for one trading day carries the whole per-pick
breakdown (every pick's selection numbers, running trigger diagnostics and reason). The strategy
page's evaluation-history table only needs a per-day digest of it, so ``summarize_payload``
derives one.

Deliberately pure: no DB, no service singleton, no clock. It takes the row dict that
``database.intraday_pullback_eval_db`` returns and gives back a plain dict — unit-testable on its
own, and it keeps presentation logic out of the persistence module. Mirrors
``services/futures_follow_eval_view.py``.

Note the row shape differs from futures_follow's: ``intraday_pullback_eval_db._row_to_dict``
spreads the payload at the TOP level (``{eval_date, eval_at, **payload}``) rather than nesting it
under a ``payload`` key, so this module reads the payload fields directly off the row.
"""

from __future__ import annotations

# Per-pick diag counters worth aggregating across the day's picks. ``candles`` is deliberately
# excluded — it is a per-symbol bar count, and summing it across picks reads as a meaningless
# total rather than a signal about why the day did (not) trade.
_DIAG_KEYS = ("ref_formed", "breakouts", "gate_blocked", "no_slot", "entries", "exits")


def summarize_payload(snapshot: dict) -> dict:
    """Digest one ``intraday_pullback_eval_snapshots`` row into a history-table entry.

    ``snapshot`` is a ``database.intraday_pullback_eval_db`` row dict. Tolerates a partial or
    empty payload — a row written by an earlier schema yields zeros/nulls rather than raising,
    because one malformed day must not blank the whole history table.

    The strategy trades ~0.7 times/day, so most days are legitimately zero-trade. The digest is
    built to make that explainable at a glance: ``diag`` totals say how far each day got
    (references formed -> breakouts seen -> gate/slot blocks -> entries), which distinguishes
    "no breakout ever came" from "breakouts came but the gate blocked them" from "no picks were
    selected at all".
    """
    evaluation = snapshot.get("evaluation") or []

    diag_totals = dict.fromkeys(_DIAG_KEYS, 0)
    for row in evaluation:
        diag = row.get("diag") or {}
        for key in _DIAG_KEYS:
            try:
                diag_totals[key] += int(diag.get(key) or 0)
            except (TypeError, ValueError):
                continue

    positions = [row.get("position") for row in evaluation]

    return {
        "eval_date": snapshot.get("eval_date"),
        "eval_at": snapshot.get("eval_at"),
        "mode": snapshot.get("mode"),
        "side_today": snapshot.get("side_today"),
        "nifty_930_pct": snapshot.get("nifty_930_pct"),
        "selected": bool(snapshot.get("selected")),
        "n_picks": len(evaluation),
        "n_trades": int(snapshot.get("n_trades_today") or 0),
        "n_open": sum(1 for p in positions if p == "open"),
        "diag": diag_totals,
        "picks": [{"symbol": r.get("symbol"), "position": r.get("position")} for r in evaluation],
    }
