"""Resolve the effective trade mode for any given moment.

Stage-0 floor: the operator declares a ``daily_intent`` (``live`` / ``sandbox``
/ ``skip``) at the start of each trading day. That intent is combined with the
legacy global ``settings.analyze_mode`` flag using a most-conservative-wins
rule. A missing daily_intent row resolves to ``DISABLED`` — we refuse to trade
with no declared intent, which closes the historical gap where the engine
could fire orders on a day the operator never armed it.

The helper here is read-only against the legacy flag. Wiring it into
``place_order_service`` is a deliberate follow-up step; for now it is exposed
via the read-only ``/mode/status`` endpoint so the operator and Cowork can
inspect what the resolver would return.
"""

import os
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from database.daily_intent_db import (
    _today_ist_str,
    get_daily_intent,
    set_daily_intent,
)
from database.settings_db import get_analyze_mode

__all__ = [
    "EffectiveMode",
    "EffectiveDecision",
    "get_daily_intent",
    "set_daily_intent",
    "resolve_effective_mode",
    "resolve_strategy_mode",
]


class EffectiveMode(str, Enum):
    LIVE = "live"
    SANDBOX = "sandbox"
    # Operator explicitly told us to sit the day out.
    SKIP = "skip"
    # No intent on record — refuse to trade.
    DISABLED = "disabled"


def resolve_effective_mode(date_str: str | None = None) -> EffectiveMode:
    """Combine ``daily_intent`` and ``analyze_mode`` into a single decision.

    Resolution rules (most-conservative-wins):

    * No daily_intent row for ``date_str`` (defaults to today IST) → DISABLED.
    * ``intent='skip'`` → SKIP, regardless of analyze_mode.
    * ``intent='sandbox'`` → SANDBOX.
    * ``intent='live'`` AND ``analyze_mode is True`` → SANDBOX (analyze on
      means the operator wants paper-trade across the platform; honour that
      even if today's intent is live).
    * ``intent='live'`` AND ``analyze_mode is False`` → LIVE.
    """
    if date_str is None:
        date_str = _today_ist_str()

    intent_row = get_daily_intent(date_str)
    if intent_row is None:
        return EffectiveMode.DISABLED

    intent = intent_row["intent"]
    if intent == "skip":
        return EffectiveMode.SKIP
    if intent == "sandbox":
        return EffectiveMode.SANDBOX
    if intent == "live":
        return EffectiveMode.SANDBOX if get_analyze_mode() else EffectiveMode.LIVE

    # Unknown intent value — refuse to trade. Validation at the write side
    # should already prevent this, but the resolver fails closed.
    return EffectiveMode.DISABLED


def set_daily_intent_safe(
    intent: Literal["live", "sandbox", "skip"],
    set_by: str,
    notes: str | None = None,
    date_str: str | None = None,
    locked: bool = False,
) -> dict:
    """Thin pass-through wrapper kept for symmetry with the DB layer."""
    return set_daily_intent(intent, set_by, notes=notes, date_str=date_str, locked=locked)


# --------------------------------------------------------------------------- #
# Unified per-strategy {mode, intent} resolver
# --------------------------------------------------------------------------- #
# This is the single per-strategy read path. It is intentionally a SEPARATE
# function from the legacy global ``resolve_effective_mode`` (which returns the
# ``EffectiveMode`` enum and is load-bearing for place_order_service and
# /mode/status). See docs/design/strategy_daily_intent.md for the naming
# rationale and the full fall-through contract.


@dataclass(frozen=True)
class EffectiveDecision:
    """Resolved {mode, intent} for one strategy on one IST day.

    mode:   'live' | 'sandbox' | 'skip'   — HOW orders route.
    intent: 'run'  | 'pause'   | 'halt'   — WHETHER to act.
    daily_capital_cap: optional override of the strategy's default daily capital.
    source: 'unified' | 'legacy' | 'env' | 'default' — where the decision came
            from, for attribution/logging.
    """

    mode: str
    intent: str
    daily_capital_cap: float | None
    source: str


def _flag_enabled() -> bool:
    """STRATEGY_DAILY_INTENT_ENABLED, default true (ships hot)."""
    raw = os.getenv("STRATEGY_DAILY_INTENT_ENABLED", "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _env_decision(strategy_name: str) -> EffectiveDecision:
    """Map a strategy's env mode flag onto the unified {mode, intent} axes.

    The env vocabularies differ per engine; both map onto the unified ``mode``:
      * simplified_engine: SIMPLIFIED_ENGINE_MODE disabled→skip / sandbox / live
        (unset → sandbox, matching the engine's own fail-safe default).
      * sector_follow_cap5_vol: SECTOR_FOLLOW_CAP5_VOL_MODE scaffold→skip /
        sandbox / live (unset → skip, since scaffold places no orders).
    Any unrecognized value fails safe to 'skip'. intent is always 'run' from env.
    """
    if strategy_name == "simplified_engine":
        raw = (os.getenv("SIMPLIFIED_ENGINE_MODE") or "sandbox").strip().lower()
        mode = {"disabled": "skip", "sandbox": "sandbox", "live": "live"}.get(raw, "sandbox")
    elif strategy_name == "sector_follow_cap5_vol":
        raw = (os.getenv("SECTOR_FOLLOW_CAP5_VOL_MODE") or "scaffold").strip().lower()
        mode = {"scaffold": "skip", "sandbox": "sandbox", "live": "live"}.get(raw, "skip")
    else:
        mode = "sandbox"
    return EffectiveDecision(mode=mode, intent="run", daily_capital_cap=None, source="env")


def resolve_strategy_mode(strategy_name: str, date: str | None = None) -> EffectiveDecision:
    """Resolve the effective {mode, intent} for one strategy.

    Fall-through (flag on): unified row → legacy daily_intent (simplified only) →
    env mode flag → default(sandbox, run). With the flag off, the unified-row
    step is skipped — i.e. exactly today's behavior. Never raises: any DB error
    falls through to the env/default decision so a job is never killed by the
    resolver.
    """
    if date is None:
        date = _today_ist_str()

    if _flag_enabled():
        try:
            from database.strategy_daily_intent_db import get_intent

            row = get_intent(strategy_name, date)
            if row is not None:
                return EffectiveDecision(
                    mode=row["mode"],
                    intent=row["intent"],
                    daily_capital_cap=row["daily_capital_cap"],
                    source="unified",
                )
        except Exception:
            # Fail open to the legacy/env path; never block a job on a DB read.
            pass

    # Legacy daily_intent table only describes the simplified engine.
    if strategy_name == "simplified_engine":
        try:
            legacy = get_daily_intent(date)
            if legacy is not None and legacy["intent"] in ("live", "sandbox", "skip"):
                return EffectiveDecision(
                    mode=legacy["intent"],
                    intent="run",
                    daily_capital_cap=None,
                    source="legacy",
                )
        except Exception:
            pass

    # Env mode flag (per-engine vocabulary).
    if strategy_name in ("simplified_engine", "sector_follow_cap5_vol"):
        return _env_decision(strategy_name)

    return EffectiveDecision(mode="sandbox", intent="run", daily_capital_cap=None, source="default")
