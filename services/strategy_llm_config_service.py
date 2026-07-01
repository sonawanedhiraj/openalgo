"""The single public path for per-strategy LLM-mode flips (issue #266 Phase 2).

Mirrors ``services.strategy_mode_service.flip_mode`` but for the LLM axis
(``off`` / ``veto`` / ``delegate``). It is a lighter guard than the trading-mode
flip because it is **not** money-routing — flipping the LLM control never sends
an order, it only decides whether the reviewer runs and whether a ``skip``
verdict blocks. So there is no preflight gate here. Every flip still:

1. Validates the target mode.
2. Writes the ``strategy_llm_config`` row via the unchecked DB helper.
3. Publishes a ``StrategyLLMModeChangedEvent`` so any in-process cache can
   invalidate (best-effort — failure never rolls back the write).
4. Best-effort Telegram-notifies the operator.

Never raises: a truly unexpected failure returns ``accepted=False`` with
``error_message`` set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from utils.event_bus import Event
from utils.event_bus import bus as _default_bus
from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

VALID_LLM_MODES = ("off", "veto", "delegate")

# Event topic published on every accepted flip.
_TOPIC = "strategy_llm_mode_changed"


@dataclass
class StrategyLLMModeChangedEvent(Event):
    """Published when an LLM-mode flip is accepted and the row is written."""

    strategy_name: str = ""
    previous_llm_mode: str | None = None
    new_llm_mode: str = ""
    flipped_by: str = ""
    topic: str = _TOPIC


@dataclass
class LLMFlipOutcome:
    """Standard return shape from :func:`flip_llm_mode`."""

    accepted: bool
    strategy_name: str
    target_llm_mode: str
    previous_llm_mode: str | None = None
    new_llm_mode: str | None = None
    warnings: list[str] = field(default_factory=list)
    error_message: str | None = None

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "strategy_name": self.strategy_name,
            "target_llm_mode": self.target_llm_mode,
            "previous_llm_mode": self.previous_llm_mode,
            "new_llm_mode": self.new_llm_mode,
            "warnings": list(self.warnings),
            "error_message": self.error_message,
        }


def _current_llm_mode(strategy_name: str) -> str | None:
    """Read the strategy's current persistent LLM mode, or None if no row."""
    try:
        from database.strategy_llm_config_db import get_llm_mode

        row = get_llm_mode(strategy_name)
        return (row or {}).get("llm_mode")
    except Exception:
        logger.exception("flip_llm_mode: failed to read current mode for %s", strategy_name)
        return None


def _telegram_notify(outcome: LLMFlipOutcome, flipped_by: str) -> None:
    """Best-effort Telegram alert for every flip attempt. Never raises."""
    try:
        from services.notification_service import get_notification_service
    except Exception:
        logger.exception("flip_llm_mode: notification_service import failed — skipping Telegram")
        return

    icon = "🤖" if outcome.accepted else "🚫"
    verdict = "SET" if outcome.accepted else "FAILED"
    lines = [
        f"{icon} Strategy LLM mode {verdict}: {outcome.strategy_name}",
        f"  {outcome.previous_llm_mode or '?'} → {outcome.target_llm_mode}",
        f"  by={flipped_by}",
        f"  at={datetime.now(_IST).strftime('%Y-%m-%d %H:%M:%S IST')}",
    ]
    if outcome.warnings:
        lines.append("  Warnings:")
        for w in outcome.warnings:
            lines.append(f"    - {w}")

    try:
        get_notification_service().notify("strategy_llm_mode_flip", "\n".join(lines))
    except Exception:
        logger.exception("flip_llm_mode: Telegram notify failed for %s", outcome.strategy_name)


def _publish_event(strategy_name: str, previous_mode: str | None, new_mode: str, by: str) -> None:
    """Publish the in-process event. Best-effort; failure does NOT roll back."""
    try:
        _default_bus.publish(
            StrategyLLMModeChangedEvent(
                strategy_name=strategy_name,
                previous_llm_mode=previous_mode,
                new_llm_mode=new_mode,
                flipped_by=by,
            )
        )
    except Exception:
        logger.exception(
            "flip_llm_mode: event publish failed for %s (%s→%s)",
            strategy_name,
            previous_mode,
            new_mode,
        )


def flip_llm_mode(
    strategy_name: str,
    target_llm_mode: str,
    flipped_by: str = "unknown",
    notes: str | None = None,
) -> LLMFlipOutcome:
    """Set a strategy's persistent LLM mode. The single sanctioned mutation path.

    Args:
        strategy_name: Strategy identifier (matches the ``strategy_llm_config`` PK).
        target_llm_mode: ``"off"`` | ``"veto"`` | ``"delegate"``.
        flipped_by: Audit label of who initiated the change.
        notes: Optional free-text note recorded on the row.

    Returns:
        :class:`LLMFlipOutcome`. Never raises.
    """
    if not strategy_name:
        return LLMFlipOutcome(
            accepted=False,
            strategy_name="",
            target_llm_mode=target_llm_mode,
            error_message="invalid input: empty strategy_name",
        )
    if target_llm_mode not in VALID_LLM_MODES:
        return LLMFlipOutcome(
            accepted=False,
            strategy_name=strategy_name,
            target_llm_mode=target_llm_mode,
            error_message=(
                f"target_llm_mode must be one of {VALID_LLM_MODES}, got {target_llm_mode!r}"
            ),
        )

    previous_mode = _current_llm_mode(strategy_name)

    warnings: list[str] = []
    if target_llm_mode == "delegate":
        warnings.append(
            "delegate stored but treated as 'veto' — the LLM-decides engine path "
            "is not built yet (a later phase)"
        )

    if previous_mode == target_llm_mode:
        return LLMFlipOutcome(
            accepted=True,
            strategy_name=strategy_name,
            target_llm_mode=target_llm_mode,
            previous_llm_mode=previous_mode,
            new_llm_mode=target_llm_mode,
            warnings=[f"Already in {target_llm_mode} mode — no-op", *warnings],
        )

    try:
        from database.strategy_llm_config_db import _set_llm_mode_unchecked

        _set_llm_mode_unchecked(
            strategy_name=strategy_name,
            llm_mode=target_llm_mode,
            updated_by=flipped_by,
            notes=notes,
        )
    except Exception as e:
        logger.exception("flip_llm_mode: DB write failed for %s", strategy_name)
        outcome = LLMFlipOutcome(
            accepted=False,
            strategy_name=strategy_name,
            target_llm_mode=target_llm_mode,
            previous_llm_mode=previous_mode,
            new_llm_mode=previous_mode,
            warnings=warnings,
            error_message=str(e),
        )
        _telegram_notify(outcome, flipped_by)
        return outcome

    _publish_event(strategy_name, previous_mode, target_llm_mode, flipped_by)

    outcome = LLMFlipOutcome(
        accepted=True,
        strategy_name=strategy_name,
        target_llm_mode=target_llm_mode,
        previous_llm_mode=previous_mode,
        new_llm_mode=target_llm_mode,
        warnings=warnings,
    )
    _telegram_notify(outcome, flipped_by)
    logger.info(
        "flip_llm_mode ACCEPTED: %s %s→%s by=%s",
        strategy_name,
        previous_mode,
        target_llm_mode,
        flipped_by,
    )
    return outcome
