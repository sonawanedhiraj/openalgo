"""Tests for ``database.strategy_mode_audit_db`` (issue #162 — S2).

The audit table is the forensic trail of every flip attempt — accepted OR
blocked — so an operator can always answer "what did the system know when
this flip happened?".
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from database import strategy_mode_audit_db
from database.strategy_mode_audit_db import (
    StrategyModeAudit,
    list_attempts,
    record_attempt,
)


@pytest.fixture(autouse=True)
def _isolate_audit_db(monkeypatch):
    """Rebind ``database.strategy_mode_audit_db`` to a fresh in-memory engine
    per test.

    The conftest's session-scope ``_INIT_TARGETS`` list creates the table
    in the redirected temp DB at session start, but that engine is shared
    across all tests in the worker and is vulnerable to pollution from any
    test that swaps the global engine. Mirrors the per-test fixture pattern
    in test_sector_follow_service.py / test_sector_follow_full_cycle.py to
    keep this file hermetic regardless of xdist test ordering.
    """
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(strategy_mode_audit_db, "engine", eng)
    monkeypatch.setattr(strategy_mode_audit_db, "db_session", sess)
    strategy_mode_audit_db.Base.query = sess.query_property()
    strategy_mode_audit_db.Base.metadata.create_all(eng)
    yield
    sess.remove()
    eng.dispose()


def test_record_attempt_accepted_writes_row():
    out = record_attempt(
        strategy_name="strat_accepted",
        target_mode="live",
        previous_mode="sandbox",
        accepted=True,
        blockers=[],
        warnings=["fishy"],
        snapshot={"preflight_path": "default"},
        flipped_by="tester",
    )
    assert out["id"] is not None
    assert out["accepted"] is True
    assert out["target_mode"] == "live"
    assert out["previous_mode"] == "sandbox"
    assert out["warnings"] == ["fishy"]
    assert out["snapshot"] == {"preflight_path": "default"}
    assert out["flipped_by"] == "tester"


def test_record_attempt_blocked_writes_row_with_blockers():
    out = record_attempt(
        strategy_name="strat_blocked",
        target_mode="live",
        previous_mode="sandbox",
        accepted=False,
        blockers=["broker down", "orphan trade"],
        warnings=[],
        snapshot={"preflight_path": "strategies.x.preflight"},
        flipped_by="ui",
    )
    assert out["accepted"] is False
    assert out["blockers"] == ["broker down", "orphan trade"]


def test_record_attempt_handles_none_lists_and_snapshot():
    """Defaults serialise to empty list/object — never NULL."""
    out = record_attempt(
        strategy_name="strat_defaults",
        target_mode="sandbox",
        previous_mode=None,
        accepted=True,
        blockers=None,
        warnings=None,
        snapshot=None,
        flipped_by="cli",
    )
    assert out["blockers"] == []
    assert out["warnings"] == []
    assert out["snapshot"] == {}


def test_record_attempt_empty_strategy_name_is_logged_skipped(caplog):
    """Bad input is logged and skipped (returns empty dict) — never raises."""
    out = record_attempt(
        strategy_name="",
        target_mode="live",
        previous_mode="sandbox",
        accepted=False,
        flipped_by="ui",
    )
    assert out == {}


def test_record_attempt_db_failure_returns_empty_does_not_raise():
    """An audit insert failure must not break the flip path."""
    with patch.object(
        strategy_mode_audit_db.db_session,
        "add",
        side_effect=RuntimeError("db gone"),
    ):
        out = record_attempt(
            strategy_name="strat_dbfail",
            target_mode="live",
            previous_mode="sandbox",
            accepted=False,
            blockers=["x"],
            flipped_by="ui",
        )
    assert out == {}


def test_list_attempts_returns_recent_first():
    record_attempt(
        strategy_name="strat_listing",
        target_mode="sandbox",
        previous_mode=None,
        accepted=True,
        flipped_by="op",
    )
    record_attempt(
        strategy_name="strat_listing",
        target_mode="live",
        previous_mode="sandbox",
        accepted=False,
        blockers=["blocked"],
        flipped_by="op",
    )
    rows = list_attempts(strategy_name="strat_listing", limit=10)
    assert len(rows) >= 2
    # Newest first
    assert rows[0]["target_mode"] == "live"
    assert rows[1]["target_mode"] == "sandbox"


def test_list_attempts_filter_accepted_only():
    record_attempt(
        strategy_name="strat_accfilter",
        target_mode="live",
        previous_mode="sandbox",
        accepted=True,
        flipped_by="op",
    )
    record_attempt(
        strategy_name="strat_accfilter",
        target_mode="live",
        previous_mode="sandbox",
        accepted=False,
        blockers=["x"],
        flipped_by="op",
    )
    accepted = list_attempts(strategy_name="strat_accfilter", accepted_only=True)
    assert all(r["accepted"] is True for r in accepted)


def test_record_attempt_serialises_complex_snapshot():
    snapshot = {
        "default_checks": [
            {"name": "broker", "passed": True, "blocker": None, "warning": None},
            {"name": "orphan", "passed": False, "blocker": "1 orphan", "warning": None},
        ],
        "extra": {"nested": True},
    }
    out = record_attempt(
        strategy_name="strat_complex",
        target_mode="live",
        previous_mode="sandbox",
        accepted=False,
        blockers=["1 orphan"],
        snapshot=snapshot,
        flipped_by="ui",
    )
    assert out["snapshot"]["default_checks"][1]["passed"] is False
    assert out["snapshot"]["extra"]["nested"] is True


def test_record_attempt_handles_non_json_snapshot_gracefully():
    """A snapshot with a non-JSON-serialisable value (e.g. datetime) must not
    crash the audit insert — falls back to empty-object marker."""
    snapshot = {"ts": datetime(2026, 6, 26, 15, 20)}  # datetime isn't JSON serialisable
    out = record_attempt(
        strategy_name="strat_badsnap",
        target_mode="sandbox",
        previous_mode=None,
        accepted=True,
        snapshot=snapshot,
        flipped_by="op",
    )
    # Row written; snapshot replaced with fallback empty object.
    assert out["id"] is not None
    assert out["snapshot"] == {}
