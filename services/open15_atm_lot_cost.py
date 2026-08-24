"""ATM lot-cost coverage ladder for ``open15_vol_breakout`` (issue #591).

Answers the operator's sizing question directly: **how much capital per trade
(1 ATM lot) covers how many of the F&O stock-option universe?** The 09:10 arm
emits one ``atm_lot_cost`` decision-log event per day; the logs page renders it
as the coverage-ladder card.

Where the prices come from
--------------------------
The EOD option-liquidity sweep (``services/option_liquidity_service.py``)
already quotes every universe name's ATM contract at ~15:40 and now persists
``atm_lot_cost_inr`` (= ATM LTP x lot size) per (symbol, side). This module
only *reads* those rows — zero broker calls. At 09:10 a fresh option quote
would return pre-open/stale LTPs anyway, so yesterday's close premium is the
honest number; the event says which sweep date priced it.

Conventions that are load-bearing
---------------------------------
- **Worst side per name** (max of CE/PE cost): at arm time the side is unknown
  until the 09:16 seed ranking, so affordability must hold for either side.
- **Affordability matches the strategy's own rule**: a name is affordable at
  capital C when ``C // lot_cost >= 1`` — the same comparison ``_enter_option``
  makes against ``margin_effective`` (an unaffordable one becomes a ``sim``
  row). Comparing against anything else would make the "affordable" count
  disagree with the day's actual skips.
- **The denominator is priced names only.** A name the sweep could not price
  is reported in ``unresolved`` — it is never counted as "covered".
- **Observational.** Nothing gates or trades on this. A failure to compute is
  a skipped event, never a skipped trading day.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter

from utils.logging import get_logger

logger = get_logger(__name__)

#: Fixed ladder steps (%). The current-slot row and the configurable target row
#: are added dynamically.
LADDER_STEPS = (50, 75, 90, 95, 100)

#: Cap on the symbol lists inside ``unresolved`` — bounds the day-log row size
#: without ever hiding the counts.
_UNRESOLVED_LIST_CAP = 50

#: How stale (in trading sessions) the newest sweep may be before we refuse to
#: price the ladder from it. Generous on purpose: this is a planning aid, and a
#: long weekend must not dark it (the #589 sessions-not-days rule).
DEFAULT_MAX_STALENESS_SESSIONS = 5


def worst_side_costs(
    scores: dict[tuple[str, str], dict], universe: set[str]
) -> tuple[list[dict], dict]:
    """``(costs, unresolved)`` over ``universe`` from the sweep's score rows.

    ``costs`` rows are ``{"s", "k", "lot", "ce", "pe", "w"}`` sorted ascending
    by ``w`` (the worst-side lot cost). Strike/lot are quoted from the side that
    set ``w``. ``unresolved`` = ``{"n", "no_quote": [...], "not_scored": [...]}``
    — a name the sweep has rows for but no ATM cost on either side is
    ``no_quote``; a name with no rows at all is ``not_scored`` (no NFO
    contracts, or the spot/quote sweep missed it). Kept distinct because they
    call for different operator responses (#583's reason discipline).
    """
    costs: list[dict] = []
    no_quote: list[str] = []
    not_scored: list[str] = []
    for sym in sorted(universe):
        ce_row = scores.get((sym, "CE"))
        pe_row = scores.get((sym, "PE"))
        if ce_row is None and pe_row is None:
            not_scored.append(sym)
            continue
        ce = (ce_row or {}).get("atm_lot_cost_inr")
        pe = (pe_row or {}).get("atm_lot_cost_inr")
        if not ce and not pe:
            no_quote.append(sym)
            continue
        if (ce or 0) >= (pe or 0):
            worst, src = float(ce), ce_row
        else:
            worst, src = float(pe), pe_row
        costs.append(
            {
                "s": sym,
                "k": src.get("atm_strike"),
                "lot": src.get("atm_lot_size"),
                "ce": ce,
                "pe": pe,
                "w": round(worst, 2),
            }
        )
    costs.sort(key=lambda c: c["w"])
    unresolved = {
        "n": len(no_quote) + len(not_scored),
        "no_quote": no_quote[:_UNRESOLVED_LIST_CAP],
        "not_scored": not_scored[:_UNRESOLVED_LIST_CAP],
    }
    return costs, unresolved


def build_ladder(costs: list[dict], capital_per_slot: float, target_pct: int) -> dict:
    """Ladder rows + drop-top hints from an ascending-sorted cost list.

    Each row: ``{"pct", "names", "capital"}`` (+ ``marker`` for the
    current-slot and target rows, ``costliest`` on the 100% row). ``capital``
    at p% is the cost of the ceil(p% x N)-th cheapest name — the minimum
    capital/slot at which at least p% of priced names are affordable.
    """
    n = len(costs)
    rows: list[dict] = []
    for pct in sorted(set(LADDER_STEPS) | {int(target_pct)}):
        idx = max(0, math.ceil(pct / 100 * n) - 1)
        row = {"pct": pct, "names": idx + 1, "capital": costs[idx]["w"]}
        if pct == int(target_pct):
            row["marker"] = "target"
        if pct == 100:
            row["costliest"] = costs[-1]["s"]
        rows.append(row)
    n_aff = sum(1 for c in costs if c["w"] <= capital_per_slot)
    rows.append(
        {
            "pct": round(100 * n_aff / n, 1),
            "names": n_aff,
            "capital": round(float(capital_per_slot), 2),
            "marker": "current_slot",
        }
    )
    rows.sort(key=lambda r: (r["pct"], r.get("marker") is not None))
    drop_top = {}
    for k in (5, 10):
        if n > k:
            drop_top[str(k)] = costs[n - k - 1]["w"]
    return {"ladder": rows, "drop_top": drop_top}


def _front_expiry(
    scores: dict[tuple[str, str], dict], today: dt.date
) -> tuple[str | None, int | None]:
    """Modal ``expiry_used`` across the sweep + its days-to-expiry."""
    exps = Counter(r["expiry_used"] for r in scores.values() if r.get("expiry_used"))
    if not exps:
        return None, None
    expiry = exps.most_common(1)[0][0]
    try:
        dte = (dt.date.fromisoformat(expiry) - today).days
    except (TypeError, ValueError):
        dte = None
    return expiry, dte


def compute_event(
    universe: set[str],
    capital_per_slot: float,
    target_pct: int,
    today: dt.date | None = None,
    max_staleness_sessions: int = DEFAULT_MAX_STALENESS_SESSIONS,
) -> dict | None:
    """Full ``atm_lot_cost`` event payload, or ``None`` when there is nothing
    honest to price it from (no sweep, sweep too stale, or no priced name).

    ``None`` means "skip the event", never "the universe is unaffordable" —
    the same fail-open discipline as ``get_latest_scores`` itself.
    """
    from database.option_liquidity_db import get_latest_scores

    today = today or dt.datetime.now().date()
    scores = get_latest_scores(max_age_days=max_staleness_sessions, today=today)
    if not scores:
        return None
    costs, unresolved = worst_side_costs(scores, universe)
    if not costs:
        return None
    as_of = next(iter(scores.values())).get("as_of_date")
    expiry, dte = _front_expiry(scores, today)
    # Honesty guard (issue #669): the sweep normally already rolls past the
    # broker's physical-delivery block window for its consumption day, but this
    # ladder tolerates sweeps up to ``max_staleness_sessions`` old — a pre-roll
    # sweep (data gap, pre-#669 rows) can still land here on a blocked day. Its
    # lot costs would then describe front-month contracts the strategy cannot
    # buy today (next-month ATM premiums are materially higher), so the card
    # must say the coverage is overstated rather than present it as clean.
    expiry_blocked_today = False
    if expiry:
        try:
            from services.open15_option_shadow import is_expiry_blocked

            expiry_blocked_today = is_expiry_blocked(dt.date.fromisoformat(expiry), today)
        except Exception:
            logger.exception("atm_lot_cost: expiry block-window check failed — flag omitted")
    out = {
        "as_of": as_of,
        "capital_per_slot": round(float(capital_per_slot), 2),
        "target_pct": int(target_pct),
        "priced": len(costs),
        "universe_n": len(universe),
        "unresolved": unresolved,
        "expiry": expiry,
        "dte": dte,
        **({"expiry_blocked_today": True} if expiry_blocked_today else {}),
        # the full sorted distribution rides along so the page can answer
        # "who is excluded at THIS capital?" for any ladder row without a
        # second endpoint. ~200 rows x ~70 B ≈ 15 KB once per day.
        "costs": costs,
    }
    out.update(build_ladder(costs, capital_per_slot, target_pct))
    return out


def _main() -> None:  # pragma: no cover - operator CLI
    """Dry-run: print today's ladder from the latest sweep. Writes nothing."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capital", type=float, default=None, help="capital/slot override (Rs)")
    parser.add_argument("--target", type=int, default=None, help="coverage target %% override")
    parser.add_argument("--dry-run", action="store_true", help="accepted for symmetry; always dry")
    args = parser.parse_args()

    # idempotent column migrations — the CLI may run before the app has booted
    # on a build that added the atm_* / coverage_target_pct columns (same
    # pattern as run_for_date)
    try:
        from database.open15_breakout_db import init_db as _open15_init
        from database.option_liquidity_db import init_db

        init_db()
        _open15_init()
    except Exception:
        logger.exception("atm_lot_cost CLI: table init failed")

    from services.open15_breakout_service import (
        Open15BreakoutService,
        _coverage_target_default,
        resolve_day_config,
    )

    try:
        from database.open15_breakout_db import get_config

        cfg_row = get_config()
    except Exception:
        cfg_row = None
    day_cfg = resolve_day_config(cfg_row, 0.0)
    capital = args.capital if args.capital is not None else day_cfg["margin_effective"]
    target = (
        args.target
        if args.target is not None
        else day_cfg.get("coverage_target_pct", _coverage_target_default())
    )
    # the runtime path passes ``svc.universe``, already F&O-filtered at arm
    # (issue #647). The CLI must price the SAME denominator or its ladder
    # disagrees with the card the page renders.
    from services.scanner_universe import tradeable_universe

    universe = tradeable_universe() or Open15BreakoutService._load_universe()
    if not universe:
        print("SCANNER_SYMBOLS empty — nothing to price")
        return
    ev = compute_event(universe, capital, target)
    if not ev:
        print("no usable option-liquidity sweep (run option_liquidity first / check staleness)")
        return
    print(
        f"ATM lot-cost coverage ladder — sweep {ev['as_of']}, expiry {ev['expiry']} "
        f"({ev['dte']} DTE), {ev['priced']}/{ev['universe_n']} priced, "
        f"capital/slot Rs {capital:,.0f}, target {target}%"
    )
    print(f"{'coverage':>10} {'names':>12} {'capital/slot':>15}")
    for r in ev["ladder"]:
        marker = {"current_slot": "  <- current slot", "target": "  <- target"}.get(
            r.get("marker"), ""
        )
        tail = f"  costliest: {r['costliest']}" if r.get("costliest") else marker
        print(f"{r['pct']:>9}% {r['names']:>6}/{ev['priced']:<5} Rs {r['capital']:>12,.0f}{tail}")
    if ev["drop_top"]:
        hints = " · ".join(f"drop top {k} -> Rs {v:,.0f}" for k, v in ev["drop_top"].items())
        print(f"cover-all alternatives: {hints}")
    u = ev["unresolved"]
    if u["n"]:
        print(
            f"unresolved {u['n']}: not_scored={','.join(u['not_scored']) or '-'} "
            f"no_quote={','.join(u['no_quote']) or '-'}"
        )


if __name__ == "__main__":
    _main()
