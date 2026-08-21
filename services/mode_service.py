"""Resolve the effective trade mode for any strategy or the global order path.

**Mode-only, UI-driven architecture (issue #440).** The single persistent
control is ``mode`` ∈ {``live``, ``sandbox``} per strategy, stored in the
``strategy_mode`` table and written ONLY through the preflight-gated
``strategy_mode_service.flip_mode`` — i.e. the strategies-page toggle. There
is no ``intent`` axis, no daily date key, and **no hidden global DB flag** —
the retired ``strategy_mode['__global__']`` row and the legacy ``daily_intent``
fall-through no longer participate in order dispatch. Automated, self-expiring
safety guards live in ``strategy_runtime_override`` (read at engine job-entry),
not here.

Two visible operator controls, nothing else:

1. The navbar **Analyze/Live toggle** (``settings.analyze_mode``) — the
   platform-wide kill switch. Analyze ON forces SANDBOX for everything.
2. The per-strategy **Live/Sandbox toggle** on /strategies (a ``strategy_mode``
   row). With Analyze OFF, an order routes LIVE **only** when its strategy's
   row says ``live``. No row / unknown label → SANDBOX (default deny).

Order dispatch for exposure-creating paths (place/basket/split/smart) MUST use
:func:`resolve_order_mode`. :func:`resolve_effective_mode` is retained as the
platform-level analyze overlay for read decorations and order-management
routing only — it must NEVER gate a new order onto the live broker.

``resolve_strategy_mode`` is retained as a DEPRECATED back-compat shim over
``resolve_mode`` (see its docstring). New code should call ``resolve_mode``
directly.
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
    "resolve_order_mode",
    "get_daily_intent",
    "set_daily_intent",
    "set_daily_intent_safe",
    "resolve_effective_mode",
    "resolve_strategy_mode",
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
    ``services.strategy_mode_service.flip_mode`` (the only sanctioned writer)."""
    return set_daily_intent(intent, set_by, notes=notes, date_str=date_str, locked=locked)


class EffectiveMode(str, Enum):
    LIVE = "live"
    SANDBOX = "sandbox"
    # Retained only so old imports don't break. NO resolver returns these —
    # orders are never refused for lack of configuration (they route sandbox).
    SKIP = "skip"
    DISABLED = "disabled"


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
# axis. EVERY env value maps to ``sandbox`` — an env var is a hidden non-UI
# flag and can never escalate to live (issue #440: only the strategies-page
# toggle, i.e. a ``strategy_mode`` row written via ``flip_mode``, can).
_ENV_VAR = {
    "simplified_engine": "SIMPLIFIED_ENGINE_MODE",
    "sector_follow_cap5_vol": "SECTOR_FOLLOW_CAP5_VOL_MODE",
}
_ENV_VALUE_MAP = {
    "live": "sandbox",  # capped — env cannot opt into live (#440)
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


def resolve_order_mode(mode_key: str | None) -> EffectiveMode:
    """Per-strategy order dispatch: the ONLY gate for exposure-creating orders.

    ``mode_key`` is the strategy identity of the order — normally the payload's
    ``strategy`` label; in-repo engines pass their canonical ``strategy_mode``
    key explicitly when the two differ (simplified engine).

    UI-driven resolution (issue #440), two visible controls and nothing else:

    1. Analyze/Live navbar toggle: Analyze ON → ``SANDBOX`` for everything
       (platform kill switch; also the fail-safe if the flag can't be read).
    2. Strategies-page toggle: with Analyze OFF, ``LIVE`` **only** when a
       ``strategy_mode`` row for ``mode_key`` says ``live``. No key, no row,
       or an env-only resolution → ``SANDBOX`` (default deny — no hidden
       ``__global__``/``daily_intent``/env escalation paths exist anymore).

    Never raises and never returns SKIP/DISABLED — an order is never refused
    for lack of configuration; it routes to the virtual sandbox book.
    """
    try:
        if get_analyze_mode():
            return EffectiveMode.SANDBOX
    except Exception:
        # If the analyzer flag can't be read, fail to the safer paper mode.
        return EffectiveMode.SANDBOX

    if not mode_key:
        return EffectiveMode.SANDBOX

    rm = resolve_mode(mode_key)
    if rm.mode == "live" and rm.source == "strategy_mode":
        return EffectiveMode.LIVE
    return EffectiveMode.SANDBOX


def resolve_effective_mode(date_str: str | None = None) -> EffectiveMode:
    """Platform-level analyze overlay — read decorations + order management.

    Returns ``SANDBOX`` when the Analyze/Live navbar toggle is on Analyze (or
    unreadable), else ``LIVE``. That is the whole resolution: the retired
    hidden ``strategy_mode['__global__']`` row and legacy ``daily_intent``
    fall-through are gone (issue #440 — no non-UI DB flags).

    Use ONLY for (a) read paths deciding which book to display and (b)
    order-management routing (cancel/modify/close) where the sandbox-book
    lookup handles per-order targeting. It must NEVER gate an
    exposure-creating order onto the live broker — that is
    :func:`resolve_order_mode`'s job (per-strategy, default deny).
    ``date_str`` is accepted for back-compat and ignored.
    """
    try:
        if get_analyze_mode():
            return EffectiveMode.SANDBOX
    except Exception:
        return EffectiveMode.SANDBOX
    return EffectiveMode.LIVE
