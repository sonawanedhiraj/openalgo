"""Write-guard regression tests for the strategy_mode table (issue #260 / #162 Phase 1).

Pins the invariant that ``services.strategy_mode_service.flip_mode`` is the ONLY
sanctioned writer of the ``strategy_mode`` table, and that the raw writer is the
UNCHECKED, underscore-prefixed ``_set_mode_unchecked`` — not a public ``set_mode``.

Context: on 2026-06-24 ``test/harness.py`` called the then-public
``database.strategy_mode_db.set_mode(..., updated_by='harness')`` and silently
flipped ``sector_follow_cap5_vol`` to ``live`` — a real-money mode change with no
preflight, no audit, no event. These tests make that class of bypass impossible
to reintroduce without a failing test.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from services import strategy_mode_service
from services.strategy_mode_service import flip_mode
from services.strategy_preflight import PreflightResult


@pytest.fixture(autouse=True)
def _silence_telegram():
    """Best-effort Telegram notify must not hit the real bot in tests."""
    with patch.object(strategy_mode_service, "_telegram_notify"):
        yield


@pytest.fixture
def fresh_mode_and_audit(monkeypatch):
    """Rebind strategy_mode_db AND strategy_mode_audit_db to one in-memory SQLite.

    ``flip_mode`` writes both the mode row and the audit row, so both modules
    must share the engine for the accepted-path test to see real rows.
    """
    from database import strategy_mode_audit_db as audit
    from database import strategy_mode_db as sm

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))

    for mod in (sm, audit):
        monkeypatch.setattr(mod, "engine", eng)
        monkeypatch.setattr(mod, "db_session", sess)
        mod.Base.query = sess.query_property()
        mod.Base.metadata.create_all(eng)

    yield sm, audit, sess

    sess.remove()
    eng.dispose()


# --------------------------------------------------------------------------- #
# (a) No public set_mode — only the unchecked writer exists.
# --------------------------------------------------------------------------- #


def test_strategy_mode_db_exposes_no_public_set_mode():
    """The raw writer must be underscore-prefixed and unchecked; a public
    ``set_mode`` is the exact bypass that caused the 2026-06-24 incident."""
    from database import strategy_mode_db as sm

    assert not hasattr(sm, "set_mode"), (
        "database.strategy_mode_db must NOT expose a public set_mode — it is a "
        "preflight-bypassing writer. Only _set_mode_unchecked should exist."
    )
    assert hasattr(sm, "_set_mode_unchecked"), (
        "the unchecked writer _set_mode_unchecked must exist for flip_mode + migration"
    )


# --------------------------------------------------------------------------- #
# (b) flip_mode → live is REFUSED when preflight fails; no row is written.
# --------------------------------------------------------------------------- #


def test_live_flip_blocked_by_preflight_writes_no_row(fresh_mode_and_audit):
    sm, _audit, _sess = fresh_mode_and_audit

    # Start from a known sandbox row so previous_mode is well-defined.
    sm._set_mode_unchecked("guarded_strat", "sandbox", updated_by="seed")

    with patch(
        "services.strategy_preflight.run_preflight",
        return_value=PreflightResult(
            can_flip=False,
            blockers=["Index aggregator empty for 8/8 sector indices"],
            warnings=[],
            snapshot={"path": "preflight"},
        ),
    ):
        out = flip_mode("guarded_strat", "live", flipped_by="ui")

    assert out.accepted is False
    assert out.new_mode == "sandbox"  # unchanged
    assert any("aggregator" in b for b in out.blockers)

    # The row must be UNTOUCHED — still sandbox, still the seed writer.
    row = sm.get_mode("guarded_strat")
    assert row is not None
    assert row["mode"] == "sandbox", "a blocked live flip must not mutate the row"
    assert row["updated_by"] == "seed"


def test_live_flip_on_unset_strategy_blocked_creates_no_row(fresh_mode_and_audit):
    sm, _audit, _sess = fresh_mode_and_audit

    with patch(
        "services.strategy_preflight.run_preflight",
        return_value=PreflightResult(
            can_flip=False,
            blockers=["broker session not live"],
            warnings=[],
            snapshot={},
        ),
    ):
        out = flip_mode("never_seen_strat", "live", flipped_by="ui")

    assert out.accepted is False
    # No row should have been created for a refused live flip.
    assert sm.get_mode("never_seen_strat") is None


# --------------------------------------------------------------------------- #
# (c) flip_mode → sandbox SUCCEEDS: writes the row + audits the accept.
# --------------------------------------------------------------------------- #


def test_sandbox_flip_writes_row_and_audits(fresh_mode_and_audit):
    sm, audit, _sess = fresh_mode_and_audit

    out = flip_mode("sb_strat", "sandbox", flipped_by="cli")

    assert out.accepted is True
    assert out.new_mode == "sandbox"

    # The mode row was actually written via the guarded path.
    row = sm.get_mode("sb_strat")
    assert row is not None
    assert row["mode"] == "sandbox"
    assert row["updated_by"] == "cli"

    # And the accepted attempt was audited.
    attempts = audit.list_attempts(strategy_name="sb_strat", limit=10)
    assert len(attempts) >= 1
    latest = attempts[0]
    assert latest["target_mode"] == "sandbox"
    assert bool(latest["accepted"]) is True
