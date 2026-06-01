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
    "get_daily_intent",
    "set_daily_intent",
    "resolve_effective_mode",
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
