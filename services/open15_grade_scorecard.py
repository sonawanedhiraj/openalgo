"""open15 grade scorecard — how each R63 grade has actually done (issue #748).

Pure aggregation over :func:`database.open15_breakout_db.grade_scorecard_rows`;
no broker call, no clock. Reports NUMBERS only — no verdict, no decision rule
(the #726 pre-registered C review stays where it is).

Three switches, each answering a different question:

- ``apply_sl`` / ``apply_trail`` — with either OFF, that risk exit is undone and
  the row is valued at its held-to-scheduled-exit counterfactual (#704/#713),
  through :func:`net_of_row_under` (one convention, #552). A row whose
  counterfactual is not priced yet is counted ``pending`` and left out — never
  Rs0. Paper/shadow rows were never stopped or trailed, so only real rows move.
- cohort — ``real`` (money) and ``full_slot`` (real + full-slot paper/shadow)
  are BOTH returned and never merged; 1-lot ``sim`` rows are in neither.
- ``window`` — ``all`` / ``r63`` (rows up to :data:`LIVE_GRADING_FROM`, graded
  after the fact from tick capture: the sample R63 was FITTED on, so it flatters
  the rating by construction) / ``live`` (graded at the trigger).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

#: first trading day the service froze grades at the trigger (#726 shipped)
LIVE_GRADING_FROM = "2026-09-15"
WINDOWS = ("all", "r63", "live")
GRADE_KEYS = ("A", "B", "C", "U")  # U = ungraded
#: tape buckets on ``rating_univ_median_pct``; the edges mirror the grader's
#: ``market_ok`` threshold (<= -0.30 fails) and its mirror image
TAPE_EDGE = 0.30
TAPE_BUCKETS = ("down", "flat", "up", "unknown")


def tape_bucket(univ_median_pct: float | None) -> str:
    if univ_median_pct is None:
        return "unknown"
    x = float(univ_median_pct)
    if x <= -TAPE_EDGE:
        return "down"
    if x >= TAPE_EDGE:
        return "up"
    return "flat"


def max_drawdown(points: Iterable[tuple[str, float]]) -> dict[str, Any]:
    """Largest peak-to-trough fall of cumulative net, in the order given.

    Equity starts at 0, so a losing first trade is a drawdown from 0.
    Returns ``{"dd": <= 0, "peak_date", "trough_date"}``.
    """
    cum = peak = 0.0
    peak_date = None
    best = {"dd": 0.0, "peak_date": None, "trough_date": None}
    for date, net in points:
        cum += net
        if cum > peak:
            peak, peak_date = cum, date
        dd = cum - peak
        if dd < best["dd"]:
            best = {"dd": round(dd, 2), "peak_date": peak_date, "trough_date": date}
    return best


def _stats(items: list[dict]) -> dict[str, Any]:
    """``items``: dicts with ``date``, ``side``, ``net`` — in trigger order."""
    n = len(items)
    net = round(sum(i["net"] for i in items), 2)
    out: dict[str, Any] = {
        "n": n,
        "wins": sum(1 for i in items if i["net"] > 0),
        "net": net,
        "avg": round(net / n, 2) if n else None,
    }
    out["win_rate"] = round(out["wins"] / n, 3) if n else None
    for side, key in (("L", "long"), ("S", "short")):
        sub = [i for i in items if i["side"] == side]
        w = sum(1 for i in sub if i["net"] > 0)
        out[key] = {
            "n": len(sub),
            "wins": w,
            "win_rate": round(w / len(sub), 3) if sub else None,
            "net": round(sum(i["net"] for i in sub), 2),
        }
    out["max_dd"] = max_drawdown((i["date"], i["net"]) for i in items)
    return out


def _in_window(row: dict, window: str) -> bool:
    if window == "r63":
        return (row.get("trade_date") or "") < LIVE_GRADING_FROM
    if window == "live":
        return (row.get("trade_date") or "") >= LIVE_GRADING_FROM
    return True


def _cohort_block(items: list[dict]) -> dict[str, Any]:
    by_grade = {g: _stats([i for i in items if i["grade"] == g]) for g in GRADE_KEYS}
    tape: dict[str, dict] = {}
    for b in TAPE_BUCKETS:
        sub = [i for i in items if i["tape"] == b]
        tape[b] = {
            side_key: _stats([i for i in sub if i["side"] == side])
            for side, side_key in (("L", "long"), ("S", "short"))
        }
    return {"grades": by_grade, "all": _stats(items), "tape_side": tape}


def build_scorecard(
    rows: list[dict],
    apply_sl: bool = True,
    apply_trail: bool = True,
    window: str = "all",
) -> dict[str, Any]:
    """Pure core: the rows in, the card payload out (testable without a DB)."""
    from database.open15_breakout_db import net_of_row_under
    from services.open15_rating import RATING_SOURCE_BACKFILL, RULES, describe_grades

    window = window if window in WINDOWS else "all"
    real: list[dict] = []
    full: list[dict] = []
    pending = {"real": 0, "full_slot": 0}
    n_backfill = 0
    since = None
    for r in rows:
        if not _in_window(r, window):
            continue
        net = net_of_row_under(r, apply_sl, apply_trail)
        is_real = r.get("cohort") == "real"
        if net is None:
            pending["full_slot"] += 1
            if is_real:
                pending["real"] += 1
            continue
        since = since or r.get("trade_date")
        n_backfill += int(r.get("rating_source") == RATING_SOURCE_BACKFILL)
        item = {
            "date": r.get("trade_date"),
            "side": r.get("side"),
            "net": net,
            "grade": r.get("rating") if r.get("rating") in ("A", "B", "C") else "U",
            "tape": tape_bucket(r.get("rating_univ_median_pct")),
        }
        full.append(item)
        if is_real:
            real.append(item)
    return {
        "status": "ok",
        "apply_sl": apply_sl,
        "apply_trail": apply_trail,
        "window": window,
        "live_grading_from": LIVE_GRADING_FROM,
        "since": since,
        "n_backfilled": n_backfill,
        "pending": pending,
        "descriptions": describe_grades(),
        "rules": dict(RULES),
        "tape_edge_pct": TAPE_EDGE,
        "cohorts": {"real": _cohort_block(real), "full_slot": _cohort_block(full)},
    }


def scorecard(
    apply_sl: bool = True,
    apply_trail: bool = True,
    window: str = "all",
    mode: str | None = None,
) -> dict[str, Any]:
    from database.open15_breakout_db import grade_scorecard_rows

    out = build_scorecard(grade_scorecard_rows(mode), apply_sl, apply_trail, window)
    out["mode"] = mode or "all"
    return out
