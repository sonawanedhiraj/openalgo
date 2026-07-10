"""Pure read-model over a futures_follow_cap50 entry-evaluation payload (issue #395).

The ``futures_follow_eval_snapshots`` row for one trading day carries the whole
per-symbol breakdown (~9 KB). The strategy page's evaluation-history table only
needs a per-day digest of it, so ``summarize_payload`` derives one (~300 B).

Deliberately pure: no DB, no service singleton, no clock. It takes the dict that
``FuturesFollowService.run_entry`` persisted and returns a plain dict. That makes
the derivation unit-testable on its own and keeps the persistence module
(``database/futures_follow_eval_db.py``) free of presentation logic.
"""

from __future__ import annotations

# Outcomes a symbol can only reach AFTER clearing all three gates — every branch
# of run_entry's signal loop, plus "cleared the gates but fell outside the K5
# selection". Mirrored in the frontend's PASSING_OUTCOMES.
PASSING_OUTCOMES = frozenset(
    {
        "in_cap_placed",
        "cap_skipped",
        "vetoed",
        "placement_failed",
        "not_selected",
    }
)

_GATES = ("sector", "stock", "vol")


def summarize_payload(snapshot: dict) -> dict:
    """Digest one ``futures_follow_eval_snapshots`` row into a history-table entry.

    ``snapshot`` is a ``database.futures_follow_eval_db`` row dict (``eval_date``,
    ``eval_at``, ``payload``). Tolerates a partial or empty payload — an old row
    written by an earlier schema yields zeros rather than raising, because a
    single malformed day must not blank the whole history table.

    ``gates_passed`` inverts the stored ``per_gate_fail_counts`` against the count
    of symbols that were actually gate-evaluated (missing-data symbols never
    reach a gate). Passed-of-N is the operator-facing framing — a large fail count
    on a day that produced a signal reads backwards — while ``gates_failed``
    carries the raw stored numbers through, so the card's tooltip still reconciles
    with the ``scanner FAIL`` log lines.
    """
    payload = snapshot.get("payload") or {}
    symbols = payload.get("symbols") or []
    gate_fails = payload.get("per_gate_fail_counts") or {}
    sources = payload.get("intraday_source_counts") or {}

    outcomes: dict[str, int] = {}
    for row in symbols:
        outcome = row.get("outcome") or "unknown"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    total = len(symbols)
    missing_data = int(gate_fails.get("missing_data", 0))
    evaluated = max(total - missing_data, 0)

    return {
        "eval_date": snapshot.get("eval_date"),
        "eval_at": snapshot.get("eval_at"),
        "mode": payload.get("mode"),
        "n_signals": int(payload.get("n_signals", 0)),
        "placed": outcomes.get("in_cap_placed", 0),
        "cap_skipped": int(payload.get("cap_skipped", 0)),
        "vetoed": int(payload.get("vetoed", 0)),
        "placement_failed": outcomes.get("placement_failed", 0),
        "missing_data": missing_data,
        "total_symbols": total,
        "evaluated_symbols": evaluated,
        "gates_passed": {g: max(evaluated - int(gate_fails.get(g, 0)), 0) for g in _GATES},
        "gates_failed": {g: int(gate_fails.get(g, 0)) for g in _GATES},
        "dominant_source": _dominant_source(sources),
        "live_source_count": int(sources.get("quotes", 0)) + int(sources.get("aggregator", 0)),
        "passed_symbols": [
            {"symbol": r.get("symbol"), "outcome": r.get("outcome")}
            for r in symbols
            if r.get("outcome") in PASSING_OUTCOMES
        ],
    }


def _dominant_source(sources: dict) -> str:
    """The intraday source that served the most symbols (mirrors the
    ``source=quotes fetched=30/30`` log line). ``none`` when nothing was served."""
    if not sources:
        return "none"
    source, count = max(sources.items(), key=lambda kv: (kv[1], kv[0] != "none"))
    return source if count > 0 else "none"
