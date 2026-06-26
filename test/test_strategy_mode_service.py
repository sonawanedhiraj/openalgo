"""Tests for ``services.strategy_mode_service.flip_mode`` (issue #162 — S3).

The single public mutation path for strategy modes. Every flip — accepted
OR blocked — must:
  1. Run the preflight (sandbox bypassed).
  2. Audit the attempt.
  3. On accept: mutate the row + publish event + Telegram.
  4. On block: NOT mutate + Telegram with blockers.
  5. Never raise.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services import strategy_mode_service
from services.strategy_mode_service import (
    StrategyModeChangedEvent,
    flip_mode,
)
from services.strategy_preflight import PreflightResult


@pytest.fixture(autouse=True)
def _silence_telegram():
    """The notify call is best-effort; we don't want it to hit the real bot in tests."""
    with patch.object(strategy_mode_service, "_telegram_notify"):
        yield


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


def test_empty_strategy_name_is_blocked():
    out = flip_mode("", "live", flipped_by="op")
    assert out.accepted is False
    assert any("strategy_name" in b for b in out.blockers)


def test_invalid_target_mode_is_blocked():
    out = flip_mode("any_strategy", "yolo", flipped_by="op")
    assert out.accepted is False
    assert any("target_mode" in b for b in out.blockers)


# --------------------------------------------------------------------------- #
# Sandbox → always allowed (preflight bypassed)
# --------------------------------------------------------------------------- #


def test_sandbox_flip_accepted_writes_mode_and_publishes_event():
    with (
        patch.object(strategy_mode_service, "_current_mode", return_value="live"),
        patch("database.strategy_mode_db.set_mode") as set_mode,
        patch.object(strategy_mode_service._default_bus, "publish") as publish,
        patch(
            "database.strategy_mode_audit_db.record_attempt",
            return_value={"id": 42},
        ) as audit,
    ):
        out = flip_mode("strat_sb", "sandbox", flipped_by="cli")

    assert out.accepted is True
    assert out.target_mode == "sandbox"
    assert out.new_mode == "sandbox"
    assert out.previous_mode == "live"
    assert out.audit_id == 42
    set_mode.assert_called_once_with(
        strategy_name="strat_sb", mode="sandbox", updated_by="cli", notes=None
    )
    publish.assert_called_once()
    event = publish.call_args.args[0]
    assert isinstance(event, StrategyModeChangedEvent)
    assert event.strategy_name == "strat_sb"
    assert event.previous_mode == "live"
    assert event.new_mode == "sandbox"
    audit.assert_called_once()
    assert audit.call_args.kwargs["accepted"] is True


# --------------------------------------------------------------------------- #
# LIVE → preflight gated
# --------------------------------------------------------------------------- #


def test_live_flip_blocked_does_not_mutate():
    """Today's exact scenario: preflight refuses → row stays sandbox."""
    with (
        patch.object(strategy_mode_service, "_current_mode", return_value="sandbox"),
        patch(
            "services.strategy_preflight.run_preflight",
            return_value=PreflightResult(
                can_flip=False,
                blockers=["Index aggregator empty for 8/8 sector indices"],
                warnings=[],
                snapshot={"path": "strategies.sector_follow_cap5_vol.preflight"},
            ),
        ),
        patch("database.strategy_mode_db.set_mode") as set_mode,
        patch.object(strategy_mode_service._default_bus, "publish") as publish,
        patch(
            "database.strategy_mode_audit_db.record_attempt",
            return_value={"id": 99},
        ) as audit,
    ):
        out = flip_mode("sector_follow_cap5_vol", "live", flipped_by="ui")

    assert out.accepted is False
    assert out.target_mode == "live"
    assert out.new_mode == "sandbox"  # unchanged
    assert out.previous_mode == "sandbox"
    assert any("aggregator" in b for b in out.blockers)
    assert out.audit_id == 99
    set_mode.assert_not_called()
    publish.assert_not_called()
    audit.assert_called_once()
    assert audit.call_args.kwargs["accepted"] is False
    assert audit.call_args.kwargs["blockers"] == ["Index aggregator empty for 8/8 sector indices"]


def test_live_flip_accepted_mutates_audits_and_publishes():
    with (
        patch.object(strategy_mode_service, "_current_mode", return_value="sandbox"),
        patch(
            "services.strategy_preflight.run_preflight",
            return_value=PreflightResult(
                can_flip=True,
                blockers=[],
                warnings=["minor concern"],
                snapshot={"all_checks_passed": True},
            ),
        ),
        patch("database.strategy_mode_db.set_mode") as set_mode,
        patch.object(strategy_mode_service._default_bus, "publish") as publish,
        patch(
            "database.strategy_mode_audit_db.record_attempt",
            return_value={"id": 7},
        ),
    ):
        out = flip_mode("strat_accept", "live", flipped_by="ui", notes="going live")

    assert out.accepted is True
    assert out.new_mode == "live"
    assert out.warnings == ["minor concern"]
    set_mode.assert_called_once_with(
        strategy_name="strat_accept", mode="live", updated_by="ui", notes="going live"
    )
    publish.assert_called_once()


# --------------------------------------------------------------------------- #
# Same-mode no-op (no preflight, no event, but still audited)
# --------------------------------------------------------------------------- #


def test_same_mode_is_a_noop_no_event_no_preflight():
    with (
        patch.object(strategy_mode_service, "_current_mode", return_value="live"),
        patch("services.strategy_preflight.run_preflight") as preflight,
        patch("database.strategy_mode_db.set_mode") as set_mode,
        patch.object(strategy_mode_service._default_bus, "publish") as publish,
        patch(
            "database.strategy_mode_audit_db.record_attempt",
            return_value={"id": 11},
        ) as audit,
    ):
        out = flip_mode("strat_noop", "live", flipped_by="op")

    assert out.accepted is True
    assert out.new_mode == "live"
    assert out.previous_mode == "live"
    assert any("no-op" in w for w in out.warnings)
    preflight.assert_not_called()
    set_mode.assert_not_called()
    publish.assert_not_called()
    audit.assert_called_once()
    # Audit row records the no-op attempt.
    assert audit.call_args.kwargs["accepted"] is True


# --------------------------------------------------------------------------- #
# Failure modes — never raise
# --------------------------------------------------------------------------- #


def test_preflight_unexpected_exception_is_treated_as_blocker():
    with (
        patch.object(strategy_mode_service, "_current_mode", return_value="sandbox"),
        patch(
            "services.strategy_preflight.run_preflight",
            side_effect=RuntimeError("preflight totally broken"),
        ),
        patch("database.strategy_mode_db.set_mode") as set_mode,
    ):
        out = flip_mode("strat_pfraises", "live", flipped_by="ui")

    assert out.accepted is False
    assert any("raised" in b.lower() for b in out.blockers)
    set_mode.assert_not_called()


def test_db_set_mode_failure_audits_block_does_not_raise():
    """Preflight passes but set_mode raises → outcome rejected, no crash."""
    with (
        patch.object(strategy_mode_service, "_current_mode", return_value="sandbox"),
        patch(
            "services.strategy_preflight.run_preflight",
            return_value=PreflightResult(can_flip=True, blockers=[], warnings=[], snapshot={}),
        ),
        patch(
            "database.strategy_mode_db.set_mode",
            side_effect=RuntimeError("DB write failed"),
        ),
        patch.object(strategy_mode_service._default_bus, "publish") as publish,
        patch(
            "database.strategy_mode_audit_db.record_attempt",
            return_value={"id": 1},
        ),
    ):
        out = flip_mode("strat_dbfail", "live", flipped_by="op")

    assert out.accepted is False
    assert "DB write failed" in (out.error_message or "")
    assert any("DB write" in b for b in out.blockers)
    publish.assert_not_called()


def test_event_publish_failure_does_not_roll_back_flip():
    """If publish fails after a successful flip, the flip stands (best-effort event)."""
    with (
        patch.object(strategy_mode_service, "_current_mode", return_value="sandbox"),
        patch(
            "services.strategy_preflight.run_preflight",
            return_value=PreflightResult(can_flip=True, blockers=[], warnings=[], snapshot={}),
        ),
        patch("database.strategy_mode_db.set_mode") as set_mode,
        patch.object(
            strategy_mode_service._default_bus,
            "publish",
            side_effect=RuntimeError("bus down"),
        ),
        patch(
            "database.strategy_mode_audit_db.record_attempt",
            return_value={"id": 2},
        ),
    ):
        out = flip_mode("strat_busfail", "live", flipped_by="op")

    assert out.accepted is True  # flip succeeded; event publish is best-effort
    set_mode.assert_called_once()


def test_audit_failure_does_not_block_flip():
    """Audit insert failure must not prevent an otherwise-valid flip — the
    audit is best-effort, the flip proceeds, and the failure is logged."""
    with (
        patch.object(strategy_mode_service, "_current_mode", return_value="sandbox"),
        patch(
            "services.strategy_preflight.run_preflight",
            return_value=PreflightResult(can_flip=True, blockers=[], warnings=[], snapshot={}),
        ),
        patch(
            "database.strategy_mode_audit_db.record_attempt",
            side_effect=RuntimeError("audit DB down"),
        ),
        patch("database.strategy_mode_db.set_mode") as set_mode,
        patch.object(strategy_mode_service._default_bus, "publish") as publish,
    ):
        out = flip_mode("strat_auditfail", "live", flipped_by="op")

    # The flip succeeds; audit_id is None (the insert failed).
    assert out.accepted is True
    assert out.audit_id is None
    set_mode.assert_called_once()
    publish.assert_called_once()
