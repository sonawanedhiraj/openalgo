"""open15 trade rating — the R63 three-check A/B/C grade (issue #726).

Pure module: no DB, no broker, no clock. The service computes the inputs and
calls :func:`grade_trigger` on the ZMQ tick thread (microseconds, no I/O); the
R63 research harness and the unit tests call the same function, so the grade
the page shows and the grade research reads can never drift apart.

The three checks (report: ``docs/research/strategy/open15_vol_breakout/
2026-09-14_r63_winner_patterns_and_trade_rating.md`` §5) are the ONLY ones
that held on the 44-row holdout; the intuitive extras (freshness of the
break, level extension, gap band, option spread) fit the 48 real fills at
86 % and reversed on the holdout, so they are deliberately absent:

- ``early``      trigger at or before 09:22:00 IST
- ``market_ok``  equal-weight universe median return, 09:15 open -> now,
                 above ``MARKET_MIN_PCT`` (-0.30 %)
- ``clean_vol``  volume ratio at the trigger below ``CLEAN_VOL_MAX`` (1.55x):
                 the 1.5x gate was crossed, not blown through

**C** = ``market_ok`` false OR the trigger is later than 09:24:00.
**A** = not C, ``early`` and ``clean_vol``. **B** = everything else.

Thresholds are code constants on purpose (pre-registered): a UI knob would
invite exactly the monthly re-cut R63 warns against. The pre-registered
re-decision at ~40 NEW real fills is recorded on issue #726.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from typing import Any

GRADES = ("A", "B", "C")
ALL_GRADES = "ABC"

#: seconds after midnight IST
EARLY_CUTOFF_S = 9 * 3600 + 22 * 60  # 09:22:00 — at or before → ``early``
CLOSED_AFTER_S = 9 * 3600 + 24 * 60  # 09:24:00 — later than this → C
MARKET_MIN_PCT = -0.30  # universe median must be ABOVE this (strict)
CLEAN_VOL_MAX = 1.55  # volume ratio must be BELOW this (strict)

#: the effective rules, stamped on the ``armed`` event so a past day is
#: replayable against the thresholds it actually ran with
RULES = {
    "early_cutoff": "09:22:00",
    "closed_after": "09:24:00",
    "market_min_pct": MARKET_MIN_PCT,
    "clean_vol_max": CLEAN_VOL_MAX,
}


#: ``open15_trades.rating_source`` (issue #748): a grade frozen at the trigger
#: by the running service, or rebuilt afterwards from the #528 tick capture by
#: ``services.open15_rating_backfill``. Backfilled grades cover the rows R63 was
#: FITTED on, so every consumer must be able to tell the two apart.
RATING_SOURCE_LIVE = "live"
RATING_SOURCE_BACKFILL = "backfill"


def _hhmm(sec: int) -> str:
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"


def describe_grades() -> dict[str, dict[str, str]]:
    """Plain-language A/B/C descriptions, built from the SAME constants the
    grader reads (issue #748) so the page can never describe a rule that is
    not the one running. ``title`` is a 2-4 word name, ``text`` one sentence.
    """
    early, closed = _hhmm(EARLY_CUTOFF_S), _hhmm(CLOSED_AFTER_S)
    mkt = f"{MARKET_MIN_PCT:+.2f}%"
    return {
        "A": {
            "title": "clean early break",
            "text": (
                f"Triggered by {early}, volume ratio under {CLEAN_VOL_MAX:g}x (crossed the "
                f"gate, did not blow through it), and the F&O median move since 09:15 above {mkt}."
            ),
        },
        "B": {
            "title": "playable",
            "text": (
                f"F&O median above {mkt} and triggered by {closed}, but after {early} "
                f"or with the volume ratio at {CLEAN_VOL_MAX:g}x or more."
            ),
        },
        "C": {
            "title": "weak tape or late",
            "text": (
                f"F&O median move since 09:15 at or below {mkt} (a falling tape), "
                f"or triggered after {closed}."
            ),
        },
    }


def normalize_trade_grades(raw: Any) -> str:
    """Canonical ``trade_grades`` string: a sorted subset of ``"ABC"``.

    Accepts ``"AB"``, ``"a,b"``, ``["A","B"]``, ``None``. Anything that leaves
    no legal grade — ``None``, ``""``, ``"xyz"`` — resolves to ``"ABC"`` (trade
    everything, byte-identical to the pre-#726 behaviour). Clamp-don't-reject,
    like every other ``open15_config`` knob: a typo must not silently turn the
    strategy off.
    """
    if raw is None:
        return ALL_GRADES
    if isinstance(raw, (list, tuple, set)):
        chars = "".join(str(x) for x in raw)
    else:
        chars = str(raw)
    kept = {c for c in chars.upper() if c in GRADES}
    return "".join(g for g in GRADES if g in kept) or ALL_GRADES


def sec_of_day(hhmm: str | None, second: int | None) -> int | None:
    """``"09:21"`` + ``33`` -> seconds after midnight; ``None`` on bad input."""
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 3600 + int(m) * 60 + int(second or 0)
    except (TypeError, ValueError, AttributeError):
        return None


def universe_median_pct(
    opens: Mapping[str, float], ltps: Mapping[str, float]
) -> tuple[float | None, int]:
    """Equal-weight median return (%) from each symbol's 09:15 open to its
    last LTP, over the symbols that have BOTH. Returns ``(median, n)``;
    ``(None, 0)`` when nothing is measurable. A free NIFTY proxy built from
    data the service already holds — no broker call, ~200 floats.
    """
    rets = []
    for sym, ltp in ltps.items():
        o = opens.get(sym)
        try:
            if o and o > 0 and ltp and ltp > 0:
                rets.append((float(ltp) / float(o) - 1.0) * 100.0)
        except (TypeError, ValueError):
            continue
    if not rets:
        return None, 0
    return round(statistics.median(rets), 3), len(rets)


def grade_trigger(
    trigger_sec: int | None,
    univ_median_pct: float | None,
    vol_ratio: float | None,
) -> dict[str, Any]:
    """Grade one trigger (or, with ``vol_ratio=None``, a provisional watch row).

    Returns ``{"grade", "fails", "early", "market_ok", "clean_vol", "closed",
    "inputs"}``. Every unknown input FAILS OPEN for its own check (an unknown
    tape is not a falling tape; an unknown ratio is not a blown gate) and is
    named in ``fails`` as ``*_unknown`` so the page can say what it did not
    know — a grade must never be a silent guess.
    """
    fails: list[str] = []
    if trigger_sec is None:
        early, closed = True, False
        fails.append("time_unknown")
    else:
        early = trigger_sec <= EARLY_CUTOFF_S
        closed = trigger_sec > CLOSED_AFTER_S
    if univ_median_pct is None:
        market_ok = True
        fails.append("market_unknown")
    else:
        market_ok = float(univ_median_pct) > MARKET_MIN_PCT
    if vol_ratio is None:
        clean_vol = True
    else:
        clean_vol = float(vol_ratio) < CLEAN_VOL_MAX
    if not market_ok:
        fails.append("market_ok")
    if closed:
        fails.append("closed")
    if not early:
        fails.append("early")
    if not clean_vol:
        fails.append("clean_vol")
    if closed or not market_ok:
        grade = "C"
    elif early and clean_vol:
        grade = "A"
    else:
        grade = "B"
    return {
        "grade": grade,
        "fails": fails,
        "early": early,
        "market_ok": market_ok,
        "clean_vol": clean_vol,
        "closed": closed,
        "inputs": {
            "trigger_sec": trigger_sec,
            "univ_median_pct": univ_median_pct,
            "vol_ratio": None if vol_ratio is None else round(float(vol_ratio), 3),
        },
    }


def phase_label(now_sec: int, univ_median_pct: float | None) -> str:
    """One line for the page's grade-phase chip."""
    if univ_median_pct is not None and univ_median_pct <= MARKET_MIN_PCT:
        return f"C — tape below {MARKET_MIN_PCT:+.2f}%"
    if now_sec <= EARLY_CUTOFF_S:
        return "A-eligible until 09:22"
    if now_sec <= CLOSED_AFTER_S:
        return "B at best until 09:24"
    return "C — closed after 09:24"


def rating_kw(action: Mapping[str, Any]) -> dict[str, Any]:
    """The journal columns for an action the service has already graded.

    Reads what :meth:`Open15BreakoutService._rate_action` attached; an
    ungraded action (grader raised, or a pre-#726 caller) yields nothing,
    so ``insert_trade`` writes NULLs rather than a fabricated grade.
    """
    r = action.get("rating")
    if not r:
        return {}
    inputs = action.get("rating_inputs") or {}
    return {
        "rating": r,
        "rating_univ_median_pct": inputs.get("univ_median_pct"),
        "rating_vol_ratio": inputs.get("vol_ratio"),
        "rating_provisional_at_add": action.get("rating_provisional_at_add"),
        "rating_source": RATING_SOURCE_LIVE,
    }
