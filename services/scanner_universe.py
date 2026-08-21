"""The scanner universe, in two flavours: watched vs tradeable (issue #648).

``SCANNER_SYMBOLS`` is a hand-maintained env list. NSE moves stocks in and out
of the F&O segment on its own schedule, so the list drifts: on 2026-08-19 it
held 211 NSE names of which **three had no option contracts at all** —
EXIDEIND, NUVAMA and SAMMAANCAP, the last of whose contracts expired
2026-06-30 and were never re-listed.

Two answers, because the same list is asked two different questions
--------------------------------------------------------------------

``scanner_universe()`` — **what we COLLECT data for.** The raw env list. Used by
    the historify 1m/``D`` backfill and its convergence scheduler.

``tradeable_universe()`` — **what we ACT on.** The above minus anything absent
    from today's master contract. Used by the scanner's rule evaluation, the WS
    pre-subscribe, the aggregator, the smoke check and the option-liquidity
    scoring.

**The split is load-bearing, not tidiness.** If dropped names also left data
maintenance, their 1m and daily series would freeze the day NSE excluded them —
and when NSE adds one back, the scanner's gap and volume gates would be reading
a series with a months-wide hole in it. The convergence check only fetches
symbols *behind today's close* incrementally, so healing that needs the manual
``--from/--to`` CLI. History is cheap; keep collecting it. **Collect on
everything in the list; act only on the F&O names.**

Why act at all, rather than warn
--------------------------------
The scanner's rules ARE the F&O intraday screeners (``fno_intraday_buy_chartink``
/ ``..._sell_chartink``, mirroring Chartink's F&O-only lists). An in-house hit on
a non-F&O name is one Chartink structurally cannot emit, so it pollutes the daily
``scanner_comparison`` as well as costing a subscription slot and a scoring pass.
``option_liquidity_service.reconcile_universe`` used to say this must never be
automated because "the scanner, sector_follow and the aggregator all read that
variable, so rewriting it from here would silently change the universe of every
one of them". That is right about the risk and wrong about the conclusion: the
answer is to make the change explicit and observable — one loader, one log line
naming what was dropped — not to keep acting on names that cannot trade.

Fail-open and the plausibility floor are inherited from
``fno_universe.filter_to_fno`` (issue #647): any degraded master-contract read
returns the FULL list, because a real exclusion is 1-3 names while a broken
instrument dump looks like 200 (the #390 shape).

``sector_follow`` is untouched — ``LOCK_STATIC_30`` is its own list.
"""

from __future__ import annotations

import datetime as dt
import os
import threading

from utils.logging import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
# (date, frozenset(watched)) -> the resolved tradeable set. Keyed on the date so
# a fresh master contract is picked up each morning with no restart, and on the
# WHOLE watched set so an env edit is never masked.
#
# The set, not its length: keying on a count made two different universes of the
# same size collide, which is how a cached answer for one list was served for
# another (caught by the pre-existing aggregator tests, which share a process
# with everything else that touches SCANNER_SYMBOLS).
_cache: dict[tuple, set[str]] = {}
_logged_for: set[tuple] = set()


def _raw_symbols() -> set[str]:
    raw = os.getenv("SCANNER_SYMBOLS", "")
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def scanner_universe() -> set[str]:
    """Every symbol in ``SCANNER_SYMBOLS`` — the DATA-MAINTENANCE universe.

    Deliberately unfiltered. See the module docstring: dropping a name from
    backfill is what makes its eventual re-inclusion arrive with a hole in its
    history.
    """
    return _raw_symbols()


def tradeable_universe(today: dt.date | None = None) -> set[str]:
    """``scanner_universe()`` minus symbols with no NFO option contracts.

    The DECISION universe. Day-cached: the master contract is re-downloaded each
    morning, so the first call of the day rebuilds and every later one is free.
    Never raises — a degraded read returns the full list (fail open).
    """
    watched = _raw_symbols()
    day = today or dt.date.today()
    key = (day, frozenset(watched))
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return set(cached)

    try:
        from services.fno_universe import filter_to_fno

        kept, dropped, degraded = filter_to_fno(watched)
    except Exception:
        logger.exception("scanner_universe: F&O filter raised — using the raw list")
        return watched

    with _lock:
        _cache[key] = set(kept)
        # the log line is the observability this change owes the operator: it is
        # how "the universe shrank" stops being silent. Once per day per set.
        first_time = key not in _logged_for
        _logged_for.add(key)
    if first_time:
        if degraded:
            logger.critical(
                "scanner universe: %d watched, F&O filter DEGRADED (%s) — acting on "
                "all %d. Nothing is dropped until the instrument dump is readable.",
                len(watched),
                degraded,
                len(kept),
            )
        else:
            logger.info(
                "scanner universe: %d watched, %d tradeable%s",
                len(watched),
                len(kept),
                (" (dropped: " + ", ".join(dropped) + ")") if dropped else "",
            )
    return kept


def reset_cache() -> None:
    """Drop the day cache. For tests, and for a master-contract re-download."""
    with _lock:
        _cache.clear()
        _logged_for.clear()
