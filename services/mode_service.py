"""Resolve the effective trade mode for any strategy or the global order path.

**Mode-only architecture.** The single persistent control is ``mode`` ∈
{``live``, ``sandbox``} per strategy, stored in the ``strategy_mode`` table.
There is no ``intent`` axis and no daily date key — automated, self-expiring
safety guards live in ``strategy_runtime_override`` (read at engine job-entry),
not here.

Resolution (``resolve_mode``): persistent ``strategy_mode`` row → env mode flag
→ ``sandbox`` default. **Default is sandbox everywhere** — both per-strategy and
on the global external-order gate. ``live`` is an explicit operator opt-in via a
persistent row; nothing is ever *refused* for lack of configuration (it routes
to the virtual sandbox book instead).

``resolve_strategy_mode`` and ``resolve_effective_mode`` are retained as
DEPRECATED back-compat shims over ``resolve_mode`` (see their docstrings). New
code should call ``resolve_mode`` directly.
"""

import os
from dataclasses import dataclass
from enum import Enum

from database.daily_intent_db import (
    _today_ist_str,
    get_daily_intent,
    set_daily_intent,
)
from database.settings_db import get_analyze_mode

__all__ = [
    "EffectiveMode",
    "EffectiveDecision",
    "ResolvedMode",
    "resolve_mode",
    "get_daily_intent",
    "set_daily_intent",
    "set_daily_intent_safe",
    "resolve_effective_mode",
    "resolve_strategy_mode",
    "GLOBAL_MODE_KEY",
    "_today_ist_str",
]


def set_daily_intent_safe(
    intent: str,
    set_by: str,
    notes: str | None = None,
    date_str: str | None = None,
    locked: bool = False,
) -> dict:
    """Back-compat thin pass-through to the legacy ``daily_intent`` DB writer.

    Retained for callers/tests that still write the legacy table directly. The
    legacy table is being retired; new code should set the persistent mode via
    ``database.strategy_mode_db.set_mode``."""
    return set_daily_intent(intent, set_by, notes=notes, date_str=date_str, locked=locked)


class EffectiveMode(str, Enum):
    LIVE = "live"
    SANDBOX = "sandbox"
    # Retained for back-compat with the order services (close/cancel/basket),
    # which still branch on them defensively. The mode-only resolver never
    # returns SKIP or DISABLED — the global gate fails to SANDBOX, never refuses.
    SKIP = "skip"
    DISABLED = "disabled"


# Reserved strategy key for the GLOBAL external-order path (the /api/v1
# place/close/cancel family). An operator opts the external path into live by
# setting a strategy_mode row for this key; with no row it defaults to sandbox.
GLOBAL_MODE_KEY = "__global__"


# --------------------------------------------------------------------------- #
# Canonical resolver
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResolvedMode:
    """Resolved persistent mode for one strategy (or the global key).

    mode:   'live' | 'sandbox'
    source: 'strategy_mode' | 'env' | 'default'
    """

    mode: str
    source: str


# Per-engine env vocabularies, collapsed onto the mode-only {live, sandbox}
# axis. Any "no orders" sentinel (disabled / scaffold / skip) maps to the
# conservative ``sandbox`` — never to ``live``.
_ENV_VAR = {
    "simplified_engine": "SIMPLIFIED_ENGINE_MODE",
    "sector_follow_cap5_vol": "SECTOR_FOLLOW_CAP5_VOL_MODE",
}
_ENV_VALUE_MAP = {
    "live": "live",
    "sandbox": "sandbox",
    "disabled": "sandbox",
    "scaffold": "sandbox",
    "skip": "sandbox",
}


def _env_mode(strategy_name: str) -> str | None:
    """Map a strategy's env mode flag onto {live, sandbox}, or None if unset."""
    var = _ENV_VAR.get(strategy_name)
    if not var:
        return None
    raw = os.getenv(var)
    if raw is None:
        return None
    return _ENV_VALUE_MAP.get(raw.strip().lower(), "sandbox")


def resolve_mode(strategy_name: str) -> ResolvedMode:
    """Resolve the persistent mode for ``strategy_name``.

    Fall-through: persistent ``strategy_mode`` row → env mode flag → ``sandbox``.
    Never raises — any DB error falls through to env/default so a trading job is
    never killed by the resolver.
    """
    try:
        from database.strategy_mode_db import get_mode

        row = get_mode(strategy_name)
        if row is not None and row["mode"] in ("live", "sandbox"):
            return ResolvedMode(mode=row["mode"], source="strategy_mode")
    except Exception:
        # Fail open to env/default; never block a job on a DB read.
        pass

    env = _env_mode(strategy_name)
    if env is not None:
        return ResolvedMode(mode=env, source="env")

    return ResolvedMode(mode="sandbox", source="default")


# --------------------------------------------------------------------------- #
# DEPRECATED back-compat shims
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EffectiveDecision:
    """DEPRECATED legacy shape. ``intent`` is always ``'run'`` in mode-only —
    the run/pause/halt axis was retired (safety guards moved to
    ``strategy_runtime_override``). ``daily_capital_cap`` is always None.
    """

    mode: str
    intent: str
    daily_capital_cap: float | None
    source: str


def resolve_strategy_mode(strategy_name: str, date: str | None = None) -> EffectiveDecision:
    """DEPRECATED shim over :func:`resolve_mode`.

    Returns the legacy ``EffectiveDecision`` shape so existing callers keep
    working, but ``intent`` is hard-wired to ``'run'`` and ``daily_capital_cap``
    to ``None`` — those axes no longer exist. ``date`` is ignored (mode is not
    date-keyed). New code should call :func:`resolve_mode` and read
    ``strategy_runtime_override`` for any pause/kill state.
    """
    rm = resolve_mode(strategy_name)
    return EffectiveDecision(mode=rm.mode, intent="run", daily_capital_cap=None, source=rm.source)


def _legacy_global_mode(date_str: str | None = None) -> str | None:
    """Back-compat fall-through for the global gate: the legacy date-keyed
    ``daily_intent`` table, collapsed onto {live, sandbox}.

    Returns 'live' / 'sandbox' for a present legacy row (``skip`` → ``sandbox``,
    since 'skip' is retired), or None when no legacy row exists. This mirrors the
    documented phased retirement of ``daily_intent`` — ``strategy_mode`` is the
    primary control; legacy is consulted only while external-API workflows
    migrate. Never raises.
    """
    try:
        row = get_daily_intent(date_str if date_str is not None else _today_ist_str())
    except Exception:
        return None
    if not row:
        return None
    intent = row.get("intent")
    if intent == "live":
        return "live"
    if intent in ("sandbox", "skip"):
        return "sandbox"
    return None


def resolve_effective_mode(date_str: str | None = None) -> EffectiveMode:
    """DEPRECATED shim — the GLOBAL external-order gate, now mode-only.

    Resolution order:

    1. ``strategy_mode['__global__']`` row — the operator's persistent global
       knob (primary control). Set it to enable live external-API orders.
    2. Legacy ``daily_intent`` table — documented back-compat fall-through while
       the daily_intent retirement completes (``skip`` collapses to sandbox).
    3. ``SANDBOX`` default (was ``DISABLED``). External callers are never
       refused for lack of configuration — orders route to the virtual ₹1Cr
       sandbox book. This is the authorized "default sandbox globally" policy;
       the change only ever makes the path *more* sandboxy, never live.

    A resolved ``live`` is downgraded to ``SANDBOX`` whenever the platform-wide
    ``analyze_mode`` is ON (legacy conservative overlay). Never returns ``SKIP``
    or ``DISABLED``.
    """
    rm = resolve_mode(GLOBAL_MODE_KEY)
    mode = rm.mode
    if rm.source != "strategy_mode":
        # No explicit global strategy_mode row — consult legacy daily_intent
        # before falling to the sandbox default.
        legacy = _legacy_global_mode(date_str)
        if legacy is not None:
            mode = legacy

    if mode == "live":
        try:
            if get_analyze_mode():
                return EffectiveMode.SANDBOX
        except Exception:
            # If the analyzer flag can't be read, fail to the safer paper mode.
            return EffectiveMode.SANDBOX
        return EffectiveMode.LIVE
    return EffectiveMode.SANDBOX
