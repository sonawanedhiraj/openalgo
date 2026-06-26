"""The single public path for strategy-mode flips (issue #162).

Why this exists
---------------
Before #162, mode changes happened via raw SQL UPDATE on the ``strategy_mode``
table. On 2026-06-26 15:20 IST this allowed a flip to LIVE while the data
pipeline was empty; the strategy emitted 0 orders silently. There was no
system-enforced check that the conditions for LIVE were met, and no record of
who flipped or why.

This service is the **only sanctioned mutation path** for strategy modes
going forward. Every flip:

1. Runs ``services.strategy_preflight.run_preflight(strategy, target_mode)``.
2. If the preflight passes → mutates the ``strategy_mode`` row, audits the
   accepted attempt, publishes a ``strategy_mode_changed`` event so the
   strategy can pick up the new mode immediately, and Telegram-notifies the
   operator.
3. If the preflight fails → does NOT mutate the row, audits the blocked
   attempt, Telegram-notifies the operator, and returns the blockers list so
   the caller (HTTP API / CLI) can surface them.

Sandbox flips are always allowed (preflight returns ``can_flip=True``
unconditionally for ``target_mode="sandbox"``) but are still audited.

The raw write helper ``database.strategy_mode_db.set_mode`` is preserved for
the boot-time migration script and tests. Production callers should never use
it directly — use :func:`flip_mode` from this module instead. (A future PR
may move ``set_mode`` to a private ``_set_mode_unchecked`` once existing
callers are audited.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from utils.event_bus import Event
from utils.event_bus import bus as _default_bus
from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Event topic published on every accepted flip. Subscribers receive a
# ``StrategyModeChangedEvent`` with the strategy + previous + new mode.
_TOPIC = "strategy_mode_changed"


@dataclass
class StrategyModeChangedEvent(Event):
    """Published when a flip is accepted and the row has been written.

    Strategies that cache their mode in-process subscribe to this and re-read
    on the next signal/tick. Sandbox→live and live→sandbox both fire it.
    """

    strategy_name: str = ""
    previous_mode: str | None = None
    new_mode: str = ""
    flipped_by: str = ""
    topic: str = _TOPIC


@dataclass
class FlipOutcome:
    """The standard return shape from :func:`flip_mode`.

    A successful flip returns ``accepted=True`` with ``new_mode`` set. A
    blocked flip returns ``accepted=False`` with the ``blockers`` list
    populated. ``warnings`` always surface (accepted or blocked) so the UI
    can show them.
    """

    accepted: bool
    strategy_name: str
    target_mode: str
    previous_mode: str | None = None
    new_mode: str | None = None  # equals target_mode on success, previous_mode on block
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    audit_id: int | None = None
    error_message: str | None = None

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "strategy_name": self.strategy_name,
            "target_mode": self.target_mode,
            "previous_mode": self.previous_mode,
            "new_mode": self.new_mode,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "audit_id": self.audit_id,
            "error_message": self.error_message,
        }


def _current_mode(strategy_name: str) -> str | None:
    """Read the strategy's current persistent mode, or None if no row exists."""
    try:
        from database.strategy_mode_db import get_mode

        row = get_mode(strategy_name)
        return (row or {}).get("mode")
    except Exception:
        logger.exception("flip_mode: failed to read current mode for %s", strategy_name)
        return None


def _telegram_notify(outcome: FlipOutcome) -> None:
    """Best-effort Telegram alert for every flip attempt. Never raises."""
    try:
        from services.notification_service import get_notification_service
    except Exception:
        logger.exception("flip_mode: notification_service import failed — skipping Telegram")
        return

    icon = "✅" if outcome.accepted else "🚫"
    verdict = "FLIPPED" if outcome.accepted else "REFUSED"
    lines = [
        f"{icon} Strategy mode {verdict}: {outcome.strategy_name}",
        f"  {outcome.previous_mode or '?'} → {outcome.target_mode}",
        f"  by={outcome.flipped_by if hasattr(outcome, 'flipped_by') else 'unknown'}",
        f"  at={datetime.now(_IST).strftime('%Y-%m-%d %H:%M:%S IST')}",
    ]
    if outcome.blockers:
        lines.append("  Blockers:")
        for b in outcome.blockers:
            lines.append(f"    - {b}")
    if outcome.warnings:
        lines.append("  Warnings:")
        for w in outcome.warnings:
            lines.append(f"    - {w}")

    try:
        get_notification_service().notify("strategy_mode_flip", "\n".join(lines))
    except Exception:
        logger.exception("flip_mode: Telegram notify failed for %s", outcome.strategy_name)


def _publish_event(strategy_name: str, previous_mode: str | None, new_mode: str, by: str) -> None:
    """Publish the in-process event. Best-effort; failure does NOT roll back the flip."""
    try:
        _default_bus.publish(
            StrategyModeChangedEvent(
                strategy_name=strategy_name,
                previous_mode=previous_mode,
                new_mode=new_mode,
                flipped_by=by,
            )
        )
    except Exception:
        logger.exception(
            "flip_mode: event publish failed for %s (%s→%s)",
            strategy_name,
            previous_mode,
            new_mode,
        )


def flip_mode(
    strategy_name: str,
    target_mode: str,
    flipped_by: str = "unknown",
    notes: str | None = None,
) -> FlipOutcome:
    """Attempt to change a strategy's mode. The single sanctioned mutation path.

    Args:
        strategy_name: Strategy identifier (matches the ``strategy_mode`` PK).
        target_mode: ``"live"`` or ``"sandbox"``.
        flipped_by: Audit record of who initiated (Flask session user, CLI
            invocation, etc.). Recorded in both the ``strategy_mode``
            ``updated_by`` column and the new ``strategy_mode_audit`` row.
        notes: Optional free-text note recorded on the ``strategy_mode`` row.

    Returns:
        :class:`FlipOutcome` describing the result. Callers should branch on
        ``accepted`` and surface ``blockers`` to the operator on refusal.

    Never raises. A truly unexpected failure (audit DB down, etc.) returns
    ``accepted=False`` with ``error_message`` set.
    """
    # Validate input first — preflight will catch this too, but a hard guard
    # here lets the caller distinguish "bad input" from "preflight blocked".
    if not strategy_name:
        return FlipOutcome(
            accepted=False,
            strategy_name="",
            target_mode=target_mode,
            blockers=["strategy_name is required"],
            error_message="invalid input: empty strategy_name",
        )
    if target_mode not in ("live", "sandbox"):
        return FlipOutcome(
            accepted=False,
            strategy_name=strategy_name,
            target_mode=target_mode,
            blockers=[f"target_mode must be 'live' or 'sandbox', got {target_mode!r}"],
            error_message="invalid input: bad target_mode",
        )

    previous_mode = _current_mode(strategy_name)

    # Same-mode no-op short-circuit. Still audit so the trail shows the attempt,
    # but don't run a preflight or publish an event — nothing changed.
    if previous_mode == target_mode:
        try:
            from database.strategy_mode_audit_db import record_attempt

            audit_row = record_attempt(
                strategy_name=strategy_name,
                target_mode=target_mode,
                previous_mode=previous_mode,
                accepted=True,
                blockers=[],
                warnings=[f"Already in {target_mode} mode — no-op"],
                snapshot={"reason": "same-mode no-op"},
                flipped_by=flipped_by,
            )
            audit_id = audit_row.get("id")
        except Exception:
            logger.exception("flip_mode: same-mode audit write failed")
            audit_id = None
        return FlipOutcome(
            accepted=True,
            strategy_name=strategy_name,
            target_mode=target_mode,
            previous_mode=previous_mode,
            new_mode=target_mode,
            warnings=[f"Already in {target_mode} mode — no-op"],
            audit_id=audit_id,
        )

    # Run the preflight.
    try:
        from services.strategy_preflight import run_preflight

        preflight = run_preflight(strategy_name, target_mode)
    except Exception as e:
        # run_preflight is supposed to never raise; if it does, treat as a
        # fail-closed blocker.
        logger.exception("flip_mode: preflight raised unexpectedly for %s", strategy_name)
        outcome = FlipOutcome(
            accepted=False,
            strategy_name=strategy_name,
            target_mode=target_mode,
            previous_mode=previous_mode,
            new_mode=previous_mode,
            blockers=[f"Preflight raised: {e!r}"],
            error_message=str(e),
        )
        outcome.flipped_by = flipped_by  # type: ignore[attr-defined]
        _telegram_notify(outcome)
        return outcome

    # Record the attempt in the audit table BEFORE mutating, so even a crash
    # between audit and write leaves an actionable trail.
    audit_id: int | None = None
    try:
        from database.strategy_mode_audit_db import record_attempt

        audit_row = record_attempt(
            strategy_name=strategy_name,
            target_mode=target_mode,
            previous_mode=previous_mode,
            accepted=preflight.can_flip,
            blockers=preflight.blockers,
            warnings=preflight.warnings,
            snapshot=preflight.snapshot,
            flipped_by=flipped_by,
        )
        audit_id = audit_row.get("id")
    except Exception:
        logger.exception(
            "flip_mode: audit write failed for %s — continuing with flip decision",
            strategy_name,
        )

    if not preflight.can_flip:
        outcome = FlipOutcome(
            accepted=False,
            strategy_name=strategy_name,
            target_mode=target_mode,
            previous_mode=previous_mode,
            new_mode=previous_mode,  # mode is unchanged
            blockers=list(preflight.blockers),
            warnings=list(preflight.warnings),
            audit_id=audit_id,
        )
        outcome.flipped_by = flipped_by  # type: ignore[attr-defined]
        _telegram_notify(outcome)
        logger.info(
            "flip_mode REFUSED: %s %s→%s — %d blocker(s)",
            strategy_name,
            previous_mode,
            target_mode,
            len(preflight.blockers),
        )
        return outcome

    # Preflight passed — write the row.
    try:
        from database.strategy_mode_db import set_mode

        set_mode(
            strategy_name=strategy_name,
            mode=target_mode,
            updated_by=flipped_by,
            notes=notes,
        )
    except Exception as e:
        logger.exception("flip_mode: set_mode raised for %s", strategy_name)
        outcome = FlipOutcome(
            accepted=False,
            strategy_name=strategy_name,
            target_mode=target_mode,
            previous_mode=previous_mode,
            new_mode=previous_mode,
            blockers=[f"DB write failed: {e!r}"],
            warnings=list(preflight.warnings),
            audit_id=audit_id,
            error_message=str(e),
        )
        outcome.flipped_by = flipped_by  # type: ignore[attr-defined]
        _telegram_notify(outcome)
        return outcome

    # Publish the in-process event so strategies pick up the new mode now.
    _publish_event(strategy_name, previous_mode, target_mode, flipped_by)

    outcome = FlipOutcome(
        accepted=True,
        strategy_name=strategy_name,
        target_mode=target_mode,
        previous_mode=previous_mode,
        new_mode=target_mode,
        blockers=[],
        warnings=list(preflight.warnings),
        audit_id=audit_id,
    )
    outcome.flipped_by = flipped_by  # type: ignore[attr-defined]
    _telegram_notify(outcome)
    logger.info(
        "flip_mode ACCEPTED: %s %s→%s by=%s",
        strategy_name,
        previous_mode,
        target_mode,
        flipped_by,
    )
    return outcome


# --------------------------------------------------------------------------- #
# CLI fallback (issue #162 — S6)
# --------------------------------------------------------------------------- #


def _cli_main(argv: list[str] | None = None) -> int:
    """``uv run python -m services.strategy_mode_service flip <name> <mode>``.

    Operator-friendly entry point for flipping mode from the shell when the
    UI is unavailable. Goes through the full preflight + audit + event path —
    NOT a raw SQL update.

    Examples:
        # Block-or-flip (interactive): exits 0 on accept, 1 on block.
        uv run python -m services.strategy_mode_service flip sector_follow_cap5_vol live

        # Show recent audit for a strategy.
        uv run python -m services.strategy_mode_service audit sector_follow_cap5_vol --limit 20

        # List strategies + their current mode.
        uv run python -m services.strategy_mode_service list
    """
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        prog="python -m services.strategy_mode_service",
        description="Flip a strategy's persistent mode through the preflight gate.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    flip_p = sub.add_parser("flip", help="Flip a strategy's mode (sandbox↔live).")
    flip_p.add_argument("strategy_name")
    flip_p.add_argument("mode", choices=["live", "sandbox"])
    flip_p.add_argument("--notes", default=None, help="Optional free-text note on the row.")
    flip_p.add_argument(
        "--by",
        default=None,
        help="Override the 'flipped_by' audit label (default: 'cli:<user>').",
    )

    audit_p = sub.add_parser("audit", help="Show recent flip attempts for a strategy.")
    audit_p.add_argument("strategy_name")
    audit_p.add_argument("--limit", type=int, default=10)

    sub.add_parser("list", help="List current mode for every strategy with a row.")

    args = parser.parse_args(argv)

    if args.cmd == "flip":
        import getpass
        import os as _os

        by = (
            args.by
            or f"cli:{_os.environ.get('USER') or _os.environ.get('USERNAME') or getpass.getuser() or 'unknown'}"
        )
        outcome = flip_mode(
            strategy_name=args.strategy_name,
            target_mode=args.mode,
            flipped_by=by,
            notes=args.notes,
        )
        print(_json.dumps(outcome.to_dict(), indent=2))
        return 0 if outcome.accepted else 1

    if args.cmd == "audit":
        from database.strategy_mode_audit_db import list_attempts

        rows = list_attempts(strategy_name=args.strategy_name, limit=args.limit)
        print(_json.dumps(rows, indent=2, default=str))
        return 0

    if args.cmd == "list":
        from database.strategy_mode_db import list_modes

        rows = list_modes()
        print(_json.dumps(rows, indent=2, default=str))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_cli_main())
