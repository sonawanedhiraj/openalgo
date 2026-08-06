"""Option-contract liquidity derivations for open15_vol_breakout (issue #555).

Pure functions over a journal row. No DB, no broker, no Flask — so the same
definitions serve the trades API, the CSV export and the tests, and there is one
place where "spread" means something.

**Why every metric is normalized.** Issue #488 captured raw contract counts
(``opt_entry_volume``, ``opt_entry_oi``) and then observed that "every ex-ante
metric ranked the two live trades backwards". Measured on 2026-08-06, the reason
needs no exotic explanation — lot sizes across this universe differ by ~30x:

===============  ==========  ==========  =============  ==========
contract         volume      lots        OI             OI lots
===============  ==========  ==========  =============  ==========
HAL 4750CE       689,850     4,599       83,250         555
SAIL 180CE       17,686,100  3,763       6,471,900      1,377
===============  ==========  ==========  =============  ==========

Raw counts make SAIL look 26x more liquid; in lots the two are comparable. A
contract count is not a quantity of anything until it is divided by its lot
size, so nothing here reports one.

**What actually costs money.** The strategy sends MARKET orders, so it crosses
the spread twice. At the same instant HAL's spread was 0.67% of mid and SAIL's
was 2.11% — a ~1.3% vs ~4.2% round-trip drag that no column recorded before.

**Nothing here gates anything** (the #488 rule, restated). R58 and #488 both
showed that inventing a threshold before the data supports one makes this
strategy worse. These are measurements; a rule needs its own evidence.
"""

from __future__ import annotations

import json
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)


def _get(row: Any, key: str):
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def _f(value) -> float | None:
    """Coerce to float; None for missing/unusable AND for 0.

    A zero bid, ask, lot size or tick size is never a real market value — it is
    the broker's "not available" (its mappers default absent fields to 0). Left
    as 0.0 it would divide into infinities or report a 100% spread.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out else None


def spread(bid, ask, tick_size=None) -> dict:
    """``{abs, pct, ticks}`` for one side of the trade.

    ``pct`` is of the MID, not of the LTP: the LTP is whichever side last traded,
    so quoting the spread against it makes the same book look wider or narrower
    depending on who traded last.
    """
    b, a = _f(bid), _f(ask)
    if b is None or a is None or a < b:
        return {"abs": None, "pct": None, "ticks": None}
    width = a - b
    mid = (a + b) / 2
    tick = _f(tick_size)
    return {
        "abs": round(width, 4),
        "pct": round(width / mid * 100, 4) if mid else None,
        "ticks": round(width / tick, 1) if tick else None,
    }


def lots(contracts, lot_size) -> float | None:
    """Contract count expressed in LOTS — the only cross-contract comparable."""
    c, ls = _f(contracts), _f(lot_size)
    return round(c / ls, 2) if (c is not None and ls) else None


def derive(row: Any) -> dict:
    """Every liquidity metric for a journal row, as a flat dict.

    Missing inputs yield ``None``, never 0 — "not captured" and "zero liquidity"
    are different facts and the research rows are useless if they blur.
    """
    lot_size = _get(row, "opt_lot_size")
    tick = _get(row, "opt_tick_size")

    entry_spread = spread(_get(row, "opt_entry_bid"), _get(row, "opt_entry_ask"), tick)
    exit_spread = spread(_get(row, "opt_exit_bid"), _get(row, "opt_exit_ask"), tick)

    # the round-trip drag a MARKET order pays: cross the ask in, the bid out
    rt = None
    if entry_spread["pct"] is not None and exit_spread["pct"] is not None:
        rt = round(entry_spread["pct"] + exit_spread["pct"], 4)

    entry_vol, entry_oi = _get(row, "opt_entry_volume"), _get(row, "opt_entry_oi")
    exit_vol, exit_oi = _get(row, "opt_exit_volume"), _get(row, "opt_exit_oi")
    entry_prem = _f(_get(row, "opt_entry_premium"))

    # rupee turnover — the honest cross-contract size comparator, because a lot
    # of a Rs5 option and a lot of a Rs160 option are not the same risk
    turnover = None
    if entry_vol is not None and entry_prem:
        turnover = round(float(entry_vol) * entry_prem, 2)

    # >1 means the day's traded volume exceeded the standing position book:
    # active two-way flow rather than a stale book of held positions
    vol_to_oi = None
    ev, eo = _f(entry_vol), _f(entry_oi)
    if ev is not None and eo:
        vol_to_oi = round(ev / eo, 3)

    return {
        "entry_spread_abs": entry_spread["abs"],
        "entry_spread_pct": entry_spread["pct"],
        "entry_spread_ticks": entry_spread["ticks"],
        "exit_spread_abs": exit_spread["abs"],
        "exit_spread_pct": exit_spread["pct"],
        "exit_spread_ticks": exit_spread["ticks"],
        "round_trip_spread_pct": rt,
        "entry_volume_lots": lots(entry_vol, lot_size),
        "entry_oi_lots": lots(entry_oi, lot_size),
        "exit_volume_lots": lots(exit_vol, lot_size),
        "exit_oi_lots": lots(exit_oi, lot_size),
        "entry_turnover_inr": turnover,
        "volume_to_oi": vol_to_oi,
        "oi_change": (
            int(exit_oi) - int(entry_oi) if exit_oi is not None and entry_oi is not None else None
        ),
        "oi_change_lots": (
            lots(int(exit_oi) - int(entry_oi), lot_size)
            if exit_oi is not None and entry_oi is not None
            else None
        ),
    }


def spread_cost_inr(row: Any) -> float | None:
    """What crossing the spread twice would have cost this position, in rupees.

    **Reporting only — never deducted from ``pnl``** (operator decision,
    2026-08-06). Sim and paper rows price both legs at the quote LTP, so their
    P&L is optimistic by roughly this amount; real rows use broker fills, where
    the spread is already inside the fill price and this figure is the
    counterfactual. Keeping it out of ``pnl`` preserves the #552 single
    convention and keeps every previously-written row comparable.
    """
    qty = _f(_get(row, "quantity")) or _f(_get(row, "sim_quantity"))
    if qty is None:
        return None
    d = derive(row)
    legs = [d["entry_spread_abs"], d["exit_spread_abs"]]
    if any(x is None for x in legs):
        return None
    # half the spread per leg: a mid-priced fill is the fair reference, and a
    # market order gives up half the width on each side of it
    return round(sum(legs) / 2 * qty, 2)


def parse_path(raw) -> list[dict]:
    """Decode ``opt_liquidity_path``; ``[]`` on anything unreadable."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        out = json.loads(raw)
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        logger.exception("open15 liquidity: unparseable path payload")
        return []


def summarize_path(raw, lot_size=None) -> dict:
    """``{minutes, oi_open, oi_close, oi_change, oi_change_lots, volume_traded}``.

    The point of the path is direction: whether open interest was BUILDING or
    UNWINDING while the position was held, which two endpoint snapshots
    structurally cannot show.
    """
    path = parse_path(raw)
    if not path:
        return {
            "minutes": 0,
            "oi_open": None,
            "oi_close": None,
            "oi_change": None,
            "oi_change_lots": None,
            "volume_traded": None,
        }
    ois = [p.get("oi") for p in path if p.get("oi") is not None]
    vols = [p.get("v") for p in path if p.get("v") is not None]
    change = (ois[-1] - ois[0]) if len(ois) >= 2 else None
    return {
        "minutes": len(path),
        "oi_open": ois[0] if ois else None,
        "oi_close": ois[-1] if ois else None,
        "oi_change": change,
        "oi_change_lots": lots(change, lot_size) if change is not None else None,
        # per-minute volume from the bars is INCREMENTAL (each bar's own traded
        # quantity), unlike the quote's cumulative day figure — summing is
        # correct here and would be wrong there
        "volume_traded": sum(vols) if vols else None,
    }
