"""Tests for futures_follow_cap50 + simplified_engine preflights (issue #162 S5).

These two strategies share the preflight infrastructure but plug in different
custom gates:

* futures_follow_cap50 reuses sector_follow's preflight (shared signal source)
  + adds a NIFTY-future-contract resolvability gate.
* simplified_engine has different operational shape (webhook-driven) so its
  custom gates focus on webhook config + journal strategy presence.

Both must:
  - Pass sandbox unconditionally
  - Compose default gates with their own
  - Fail closed on any probe exception
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.strategy_preflight import PreflightResult

# --------------------------------------------------------------------------- #
# futures_follow_cap50
# --------------------------------------------------------------------------- #


@pytest.fixture
def ff_preflight():
    from strategies.futures_follow_cap50 import preflight as ff

    return ff


def test_ff_sandbox_target_always_allowed(ff_preflight):
    with patch("strategies.sector_follow_cap5_vol.preflight.check_can_go_live") as sf_check:
        result = ff_preflight.check_can_go_live("sandbox")
    assert result.can_flip is True
    sf_check.assert_not_called()


def test_ff_invalid_target_mode_is_blocked(ff_preflight):
    result = ff_preflight.check_can_go_live("yolo")
    assert result.can_flip is False
    assert any("target_mode" in b for b in result.blockers)


def test_ff_check_nifty_future_resolvable_passes_when_resolver_returns_contract(ff_preflight):
    mock_service = MagicMock()
    mock_service._resolve_nifty_future_contract.return_value = {
        "tradingsymbol": "NIFTY26JUNFUT",
        "instrument_token": 12345,
    }
    with patch(
        "services.futures_follow_service.get_service",
        return_value=mock_service,
    ):
        check = ff_preflight.check_nifty_future_resolvable()
    assert check.passed is True


def test_ff_check_nifty_future_blocks_when_resolver_returns_none(ff_preflight):
    """No tradable contract → flip refused with a clear message."""
    mock_service = MagicMock()
    mock_service._resolve_nifty_future_contract.return_value = None
    with patch(
        "services.futures_follow_service.get_service",
        return_value=mock_service,
    ):
        check = ff_preflight.check_nifty_future_resolvable()
    assert check.passed is False
    assert "future" in (check.blocker_message or "").lower()


def test_ff_check_nifty_future_blocks_when_service_uninitialised(ff_preflight):
    with patch("services.futures_follow_service.get_service", return_value=None):
        check = ff_preflight.check_nifty_future_resolvable()
    assert check.passed is False
    assert "not initialised" in (check.blocker_message or "").lower()


def test_ff_check_nifty_future_fails_closed_on_resolver_exception(ff_preflight):
    mock_service = MagicMock()
    mock_service._resolve_nifty_future_contract.side_effect = RuntimeError("boom")
    with patch(
        "services.futures_follow_service.get_service",
        return_value=mock_service,
    ):
        check = ff_preflight.check_nifty_future_resolvable()
    assert check.passed is False
    assert "raised" in (check.blocker_message or "").lower()


def test_ff_composes_shared_sector_follow_preflight_with_futures_gate(ff_preflight):
    """The happy path: shared sector_follow preflight passes + futures contract
    resolvable → flip allowed."""
    shared = PreflightResult(
        can_flip=True,
        blockers=[],
        warnings=["sector_follow warn"],
        snapshot={"shared": True},
    )
    mock_service = MagicMock()
    mock_service._resolve_nifty_future_contract.return_value = {"x": 1}
    with (
        patch(
            "strategies.sector_follow_cap5_vol.preflight.check_can_go_live",
            return_value=shared,
        ),
        patch(
            "services.futures_follow_service.get_service",
            return_value=mock_service,
        ),
    ):
        result = ff_preflight.check_can_go_live("live")

    assert result.can_flip is True
    assert "sector_follow warn" in result.warnings
    assert result.snapshot["preflight_path"] == "strategies.futures_follow_cap50.preflight"


def test_ff_blocks_when_shared_preflight_blocks(ff_preflight):
    """A sector_follow blocker (today's bug — index aggregator empty) must
    propagate through and block the futures_follow LIVE flip."""
    shared = PreflightResult(
        can_flip=False,
        blockers=["Index intraday aggregator empty for 8/8"],
        warnings=[],
        snapshot={"shared": True},
    )
    mock_service = MagicMock()
    mock_service._resolve_nifty_future_contract.return_value = {"x": 1}
    with (
        patch(
            "strategies.sector_follow_cap5_vol.preflight.check_can_go_live",
            return_value=shared,
        ),
        patch(
            "services.futures_follow_service.get_service",
            return_value=mock_service,
        ),
    ):
        result = ff_preflight.check_can_go_live("live")

    assert result.can_flip is False
    assert "Index intraday aggregator empty for 8/8" in result.blockers


def test_ff_blocks_when_only_futures_gate_fails(ff_preflight):
    """Shared preflight passes but no NIFTY future resolvable → flip refused."""
    shared = PreflightResult(can_flip=True, blockers=[], warnings=[], snapshot={})
    mock_service = MagicMock()
    mock_service._resolve_nifty_future_contract.return_value = None
    with (
        patch(
            "strategies.sector_follow_cap5_vol.preflight.check_can_go_live",
            return_value=shared,
        ),
        patch(
            "services.futures_follow_service.get_service",
            return_value=mock_service,
        ),
    ):
        result = ff_preflight.check_can_go_live("live")
    assert result.can_flip is False
    assert any("future" in b.lower() for b in result.blockers)


def test_ff_fails_closed_when_shared_preflight_raises(ff_preflight):
    with patch(
        "strategies.sector_follow_cap5_vol.preflight.check_can_go_live",
        side_effect=RuntimeError("sf module broken"),
    ):
        result = ff_preflight.check_can_go_live("live")
    assert result.can_flip is False
    assert any("sector_follow shared" in b.lower() for b in result.blockers)


# --------------------------------------------------------------------------- #
# simplified_engine
# --------------------------------------------------------------------------- #


@pytest.fixture
def se_preflight():
    from strategies.simplified_engine import preflight as se

    return se


def test_se_sandbox_target_always_allowed(se_preflight):
    with patch.object(se_preflight, "run_default_checks") as defaults:
        result = se_preflight.check_can_go_live("sandbox")
    assert result.can_flip is True
    defaults.assert_not_called()


def test_se_invalid_target_mode_is_blocked(se_preflight):
    result = se_preflight.check_can_go_live("yolo")
    assert result.can_flip is False


def test_se_passes_when_active_strategy_row_exists(se_preflight):
    mock_query = MagicMock()
    mock_query.filter_by.return_value.count.return_value = 2  # 2 active rows

    mock_session = MagicMock()
    mock_session.query.return_value = mock_query

    # Build a fake strategy_db module shape that the preflight will import.
    fake_module = MagicMock()
    fake_module.db_session = mock_session
    fake_module.strategy_model = MagicMock()

    with patch.dict(
        "sys.modules",
        {"database.strategy_db": fake_module},
    ):
        check = se_preflight.check_webhook_strategy_configured()
    assert check.passed is True


def test_se_blocks_when_no_active_strategy_rows(se_preflight):
    mock_query = MagicMock()
    mock_query.filter_by.return_value.count.return_value = 0  # none active

    mock_session = MagicMock()
    mock_session.query.return_value = mock_query

    fake_module = MagicMock()
    fake_module.db_session = mock_session
    fake_module.strategy_model = MagicMock()

    with patch.dict(
        "sys.modules",
        {"database.strategy_db": fake_module},
    ):
        check = se_preflight.check_webhook_strategy_configured()
    assert check.passed is False
    assert "active strategy" in (check.blocker_message or "").lower()


def test_se_probe_uses_real_strategy_db_import(se_preflight, monkeypatch):
    """Regression (#162): the probe must import the REAL database.strategy_db
    symbols (Strategy + db_session). The original code imported a non-existent
    name (`from database.strategy_db import strategy_model as strategy_mod`),
    which raised ImportError and failed the check closed with
    "Could not probe strategy_db ..." — blocking EVERY live flip. The sibling
    tests missed it because they inject a MagicMock module (auto-creating any
    attribute name). This exercises the real import against an in-memory DB.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker

    import database.strategy_db as sdb

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(sdb, "engine", eng)
    monkeypatch.setattr(sdb, "db_session", sess)
    sdb.Base.query = sess.query_property()
    sdb.Base.metadata.create_all(eng)

    # No active rows → coarse presence check blocks (NOT the probe-error path).
    check = se_preflight.check_webhook_strategy_configured()
    assert check.passed is False
    assert "no active strategy" in (check.blocker_message or "").lower()
    assert "could not probe" not in (check.blocker_message or "").lower()

    # An active strategy row → probe passes (proves the real import resolves).
    sess.add(
        sdb.Strategy(
            name="chartink_FnO_intraday_buy",
            webhook_id="wh-regression-1",
            user_id="op",
            is_active=True,
        )
    )
    sess.commit()
    check2 = se_preflight.check_webhook_strategy_configured()
    assert check2.passed is True


def test_se_composes_defaults_with_webhook_gate(se_preflight):
    """Happy path: defaults pass + active strategy rows → flip allowed."""
    defaults = PreflightResult(can_flip=True, blockers=[], warnings=[], snapshot={"defaults": True})
    mock_query = MagicMock()
    mock_query.filter_by.return_value.count.return_value = 1

    mock_session = MagicMock()
    mock_session.query.return_value = mock_query

    fake_module = MagicMock()
    fake_module.db_session = mock_session
    fake_module.strategy_model = MagicMock()

    with (
        patch.object(se_preflight, "run_default_checks", return_value=defaults),
        patch.dict(
            "sys.modules",
            {"database.strategy_db": fake_module},
        ),
    ):
        result = se_preflight.check_can_go_live("live")
    assert result.can_flip is True
    assert result.snapshot["preflight_path"] == "strategies.simplified_engine.preflight"


def test_se_default_gate_blocker_propagates(se_preflight):
    """Default gate failure (e.g. orphan trades — which is real on dev today)
    must block the LIVE flip even if the webhook gate passes."""
    defaults = PreflightResult(
        can_flip=False,
        blockers=["7 orphan trade(s) in journal"],
        warnings=[],
        snapshot={"defaults_failed": True},
    )
    mock_query = MagicMock()
    mock_query.filter_by.return_value.count.return_value = 1
    mock_session = MagicMock()
    mock_session.query.return_value = mock_query

    fake_module = MagicMock()
    fake_module.db_session = mock_session
    fake_module.strategy_model = MagicMock()

    with (
        patch.object(se_preflight, "run_default_checks", return_value=defaults),
        patch.dict(
            "sys.modules",
            {"database.strategy_db": fake_module},
        ),
    ):
        result = se_preflight.check_can_go_live("live")
    assert result.can_flip is False
    assert "7 orphan trade(s) in journal" in result.blockers
