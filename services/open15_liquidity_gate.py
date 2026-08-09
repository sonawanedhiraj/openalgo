"""Option-liquidity exclusion rules for ``open15_vol_breakout`` (issue #583, Gate 1).

Reads the scores written by ``services/option_liquidity_service.py`` and answers two
questions the strategy asks at two different moments:

``stage1(symbol)``
    At **09:10 arm**, before the side is known. Only excludes a symbol that fails on
    **both** sides — those can never be traded either way, so no side information is
    needed. On 2026-08-07 medians that is 25 of 208.

``stage2(symbol, side)``
    At **09:16 seed selection** and again on every **rolling addition**, once the gap
    ranking has assigned a side. Excludes a symbol whose *assigned* side fails. This is
    where the remaining 17 one-side-only names are caught — 8 thin only on calls, 9
    only on puts.

Two stages rather than one because the two obvious single-stage designs each break
something: filtering at arm alone means guessing the side, while filtering only after
ranking lets an illiquid name occupy a top-N slot first.

Rules that are load-bearing, not stylistic
------------------------------------------
**Fail OPEN on a DATA gap, fail CLOSED on a CONTRACT gap.** Missing, stale or
unreadable scores mean *our collection* broke, so the symbol is INCLUDED and a warning
is logged — darkening a universe because a job failed is the #390 shape, where 3 stale
symbols of 216 held the entire scanner all session. A symbol with **no NFO option
contracts at all** is a different fact: that is a stale ``SCANNER_SYMBOLS``, not an
illiquidity verdict, and it is excluded loudly (EXIDEIND, NUVAMA and SAMMAANCAP are in
exactly that state today).

**Insufficient history is NOT a low score.** A newly listed F&O name reports
``insufficient_history`` and is *included*; scoring it low would be indistinguishable
from scoring it on no evidence.

**Hysteresis, computed from history rather than stored state.** Out below
``min_pctile``; back only once the median has held at or above ``reentry_pctile`` for
``reentry_days`` consecutive sessions. Without a band a name sitting on the threshold
enters and leaves the universe on alternate days and no round is comparable.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from utils.logging import get_logger

logger = get_logger(__name__)

# Exclusion reasons. Kept distinct on purpose — "we judged it and it failed", "there is
# nothing to trade", and "too new to judge" call for completely different operator
# responses, and a single blended reason hides which one happened.
REASON_ILLIQUID = "illiquid_options"
REASON_NO_CONTRACTS = "no_option_contracts"
REASON_INSUFFICIENT = "insufficient_history"

SIDE_FOR = {"L": "CE", "S": "PE"}


@dataclass
class Exclusion:
    symbol: str
    reason: str
    side: str | None = None  # "long" | "short" | "both" | None
    pctile: float | None = None
    detail: dict = field(default_factory=dict)

    def as_event(self) -> dict:
        out = {"symbol": self.symbol, "reason": self.reason}
        if self.side:
            out["side"] = self.side
        if self.pctile is not None:
            out["pctile"] = round(self.pctile, 1)
        out.update(self.detail)
        return out


@dataclass
class LiquidityGate:
    """An immutable snapshot of today's exclusion verdicts.

    Built once at arm; every later question is answered from memory so the tick path
    never touches the database.
    """

    enabled: bool
    min_pctile: float
    reentry_pctile: float
    reentry_days: int
    scores: dict = field(default_factory=dict)  # (symbol, side) -> row
    excluded_sides: dict = field(default_factory=dict)  # (symbol, side) -> Exclusion
    contract_underlyings: set = field(default_factory=set)
    have_scores: bool = True

    # ---- per-side verdict -------------------------------------------------
    def side_verdict(self, symbol: str, opt_side: str) -> Exclusion | None:
        """``None`` when tradeable; an ``Exclusion`` when not."""
        if not self.enabled:
            return None
        if self.contract_underlyings and symbol not in self.contract_underlyings:
            return Exclusion(symbol, REASON_NO_CONTRACTS, side="both")
        if not self.have_scores:
            return None  # data gap -> fail OPEN
        return self.excluded_sides.get((symbol, opt_side))

    # ---- stage 1: arm, side unknown ---------------------------------------
    def stage1(self, symbol: str) -> Exclusion | None:
        """Exclude only when BOTH sides fail — the side-independent verdict."""
        if not self.enabled:
            return None
        if self.contract_underlyings and symbol not in self.contract_underlyings:
            return Exclusion(symbol, REASON_NO_CONTRACTS, side="both")
        if not self.have_scores:
            return None
        ce = self.excluded_sides.get((symbol, "CE"))
        pe = self.excluded_sides.get((symbol, "PE"))
        if ce and pe:
            return Exclusion(
                symbol,
                REASON_ILLIQUID,
                side="both",
                pctile=min(x for x in (ce.pctile, pe.pctile) if x is not None)
                if (ce.pctile is not None or pe.pctile is not None)
                else None,
                detail={"ce_pctile": ce.pctile, "pe_pctile": pe.pctile},
            )
        return None

    # ---- stage 2: side assigned -------------------------------------------
    def stage2(self, symbol: str, action_side: str) -> Exclusion | None:
        """``action_side`` is the strategy's ``"L"``/``"S"``, not the option type."""
        opt = SIDE_FOR.get(action_side)
        if opt is None:
            return None
        ex = self.side_verdict(symbol, opt)
        if ex is None:
            return None
        if ex.side == "both":
            return ex
        return Exclusion(
            ex.symbol,
            ex.reason,
            side="long" if action_side == "L" else "short",
            pctile=ex.pctile,
            detail=ex.detail,
        )


def _hysteresis_excluded(
    median: float | None,
    recent: list[float],
    min_pctile: float,
    reentry_pctile: float,
    reentry_days: int,
) -> bool:
    """Today's exclusion verdict, derived from history alone — no stored state.

    Below the floor is always out. Between the floor and the re-entry line a name stays
    out **if it dipped below the floor at any point in the re-entry window**; only once
    it has held the whole window without dipping is it readmitted. That is the band's
    whole purpose: a name oscillating around the threshold must not flap in and out on
    alternate days.
    """
    if median is None:
        return False
    if median < min_pctile:
        return True
    if median >= reentry_pctile:
        return False
    window = recent[: max(0, reentry_days - 1)]
    return any(v < min_pctile for v in window)


def build_gate(cfg: dict, today: dt.date | None = None) -> LiquidityGate:
    """Read the scores and pre-compute every verdict. Never raises.

    Any failure degrades to a gate that excludes nothing (``have_scores=False``), which
    is the fail-open contract — a broken read must never dark the universe.
    """
    enabled = bool(cfg.get("option_liquidity_gate_enabled", True))
    min_pctile = float(cfg.get("option_liquidity_min_pctile", 20))
    reentry_pctile = float(cfg.get("option_liquidity_reentry_pctile", 25))
    reentry_days = int(cfg.get("option_liquidity_reentry_days", 3))
    max_stale = int(cfg.get("option_liquidity_max_staleness_days", 3))
    min_days = int(cfg.get("option_liquidity_min_days", 10))

    gate = LiquidityGate(
        enabled=enabled,
        min_pctile=min_pctile,
        reentry_pctile=reentry_pctile,
        reentry_days=reentry_days,
    )
    if not enabled:
        return gate

    try:
        from services.option_liquidity_service import option_underlyings

        gate.contract_underlyings = option_underlyings()
    except Exception:
        logger.exception("open15 gate: master-contract read failed — no contract check")
        gate.contract_underlyings = set()

    try:
        from database.option_liquidity_db import get_daily_pctile_history, get_latest_scores

        scores = get_latest_scores(max_age_days=max_stale, today=today)
    except Exception:
        logger.exception("open15 gate: score read failed — gate FAILS OPEN for the day")
        gate.have_scores = False
        return gate

    if not scores:
        logger.warning(
            "open15 gate: no usable option-liquidity scores (missing or older than %d "
            "days) — gate FAILS OPEN for the day",
            max_stale,
        )
        gate.have_scores = False
        return gate

    gate.scores = scores
    try:
        history = get_daily_pctile_history(reentry_days + 1, today or dt.date.today())
    except Exception:
        logger.exception("open15 gate: history read failed — hysteresis disabled today")
        history = {}

    n_insufficient = 0
    for key, row in scores.items():
        median = row.get("option_liquidity_pctile")
        if median is None:
            # too new to rank: INCLUDE, and say why. Never treated as illiquid.
            n_insufficient += 1
            continue
        if (row.get("n_days_in_median") or 0) < min_days:
            n_insufficient += 1
            continue
        if _hysteresis_excluded(
            median, history.get(key, []), min_pctile, reentry_pctile, reentry_days
        ):
            gate.excluded_sides[key] = Exclusion(
                key[0],
                REASON_ILLIQUID,
                side="long" if key[1] == "CE" else "short",
                pctile=median,
                detail={"turnover": row.get("atm_premium_turnover")},
            )

    logger.info(
        "open15 gate: %d scored sides, %d excluded, %d insufficient history, %d "
        "underlyings with contracts",
        len(scores),
        len(gate.excluded_sides),
        n_insufficient,
        len(gate.contract_underlyings),
    )
    return gate
