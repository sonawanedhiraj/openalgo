"""Tests for ``strategies.sector_follow_cap5_vol.preflight`` (issue #162 — S4).

The strategy-specific preflight that would have refused today's
2026-06-26 15:20 IST LIVE flip. Verifies:
  - Sandbox bypass
  - Each individual gate's pass/fail behaviour
  - The today's-failure scenario produces a blocker with the right message
  - Default gates compose correctly
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from services.strategy_preflight import PreflightResult
from strategies.sector_follow_cap5_vol import preflight as sf_preflight

_IST = timezone(timedelta(hours=5, minutes=30))

INDICES = [
    "NIFTY",
    "BANKNIFTY",
    "NIFTYAUTO",
    "NIFTYFMCG",
    "NIFTYIT",
    "NIFTYMETAL",
    "NIFTYPSUBANK",
    "NIFTYPVTBANK",
]
STOCKS = [f"STK{i:02d}" for i in range(30)]


def _passing_provider(symbol, as_of):
    """Mock provider — every symbol has today's bar."""
    return (100.0, 1000)


def _empty_provider(symbol, as_of):
    """Mock provider — no symbol has today's bar (today's actual failure)."""
    return (None, None)


def _index_only_empty_provider(symbol, as_of):
    """Stocks covered, indices empty — exactly today's failure mode."""
    if symbol in INDICES:
        return (None, None)
    return (100.0, 1000)


# --------------------------------------------------------------------------- #
# Sandbox bypass
# --------------------------------------------------------------------------- #


def test_sandbox_target_always_allowed():
    """Sandbox flips bypass every gate — no default, no custom checks run."""
    with patch.object(sf_preflight, "run_default_checks") as defaults:
        result = sf_preflight.check_can_go_live("sandbox")
    assert result.can_flip is True
    defaults.assert_not_called()


def test_invalid_target_mode_is_blocked():
    result = sf_preflight.check_can_go_live("yolo")
    assert result.can_flip is False
    assert any("target_mode" in b for b in result.blockers)


# --------------------------------------------------------------------------- #
# Individual gates — index aggregator coverage (load-bearing)
# --------------------------------------------------------------------------- #


def test_index_coverage_blocks_when_all_indices_empty():
    """Today's exact failure: all 8 sector indices have today_close=None."""
    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        return_value=INDICES,
    ):
        check = sf_preflight.check_index_aggregator_coverage(_empty_provider, datetime.now(_IST))
    assert check.passed is False
    blocker = check.blocker_message or ""
    assert "8/8" in blocker
    assert "NIFTYAUTO" in blocker  # missing list named
    assert "issue #161" in blocker or "scanner aggregator" in blocker


def test_index_coverage_passes_when_all_indices_covered():
    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        return_value=INDICES,
    ):
        check = sf_preflight.check_index_aggregator_coverage(_passing_provider, datetime.now(_IST))
    assert check.passed is True


def test_index_coverage_blocks_when_partial_missing():
    """Even one missing index blocks — the strategy needs ALL mapped indices
    to compute sector_ret."""

    def partial_provider(sym, as_of):
        if sym == "NIFTYIT":
            return (None, None)
        return (100.0, 1000)

    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        return_value=INDICES,
    ):
        check = sf_preflight.check_index_aggregator_coverage(partial_provider, datetime.now(_IST))
    assert check.passed is False
    assert "NIFTYIT" in (check.blocker_message or "")


def test_index_coverage_blocks_when_no_indices_mapped():
    """Empty sector_map.json → no indices → block (won't trade anyway)."""
    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        return_value=[],
    ):
        check = sf_preflight.check_index_aggregator_coverage(_passing_provider, datetime.now(_IST))
    assert check.passed is False
    assert "sector_map" in (check.blocker_message or "").lower()


def test_index_coverage_fails_closed_when_symbols_import_fails():
    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        side_effect=RuntimeError("module broken"),
    ):
        check = sf_preflight.check_index_aggregator_coverage(_passing_provider, datetime.now(_IST))
    assert check.passed is False
    assert (
        "module broken" in (check.blocker_message or "")
        or "import" in (check.blocker_message or "").lower()
    )


# --------------------------------------------------------------------------- #
# Stock aggregator coverage
# --------------------------------------------------------------------------- #


def test_stock_coverage_passes_at_full_coverage():
    check = sf_preflight.check_stock_aggregator_coverage(
        _passing_provider, datetime.now(_IST), STOCKS
    )
    assert check.passed is True


def test_stock_coverage_passes_above_threshold(monkeypatch):
    monkeypatch.setenv("SECTOR_FOLLOW_PREFLIGHT_MIN_STOCK_COVERAGE", "0.9")

    def provider(sym, as_of):
        # 28/30 = 93% > 90% threshold
        if sym in (STOCKS[0], STOCKS[1]):
            return (None, None)
        return (100.0, 1000)

    check = sf_preflight.check_stock_aggregator_coverage(provider, datetime.now(_IST), STOCKS)
    assert check.passed is True
    # Surfaces missing symbols as a warning, not a blocker
    assert check.warning_message is not None
    assert STOCKS[0] in check.warning_message


def test_stock_coverage_blocks_below_threshold(monkeypatch):
    monkeypatch.setenv("SECTOR_FOLLOW_PREFLIGHT_MIN_STOCK_COVERAGE", "0.9")

    def provider(sym, as_of):
        # 25/30 = 83% < 90% threshold
        if sym in STOCKS[:5]:
            return (None, None)
        return (100.0, 1000)

    check = sf_preflight.check_stock_aggregator_coverage(provider, datetime.now(_IST), STOCKS)
    assert check.passed is False
    assert "25/30" in (check.blocker_message or "")


def test_stock_coverage_blocks_on_empty_universe():
    check = sf_preflight.check_stock_aggregator_coverage(_passing_provider, datetime.now(_IST), [])
    assert check.passed is False
    assert "universe" in (check.blocker_message or "").lower()


# --------------------------------------------------------------------------- #
# Master contract gate
# --------------------------------------------------------------------------- #


def test_master_contract_passes_when_ready():
    with patch(
        "database.master_contract_status_db.get_status",
        return_value={"is_ready": True, "status": "success"},
    ):
        check = sf_preflight.check_master_contract_ready("zerodha")
    assert check.passed is True


def test_master_contract_blocks_when_downloading():
    with patch(
        "database.master_contract_status_db.get_status",
        return_value={"is_ready": False, "status": "downloading"},
    ):
        check = sf_preflight.check_master_contract_ready("zerodha")
    assert check.passed is False
    assert "downloading" in (check.blocker_message or "").lower()


def test_master_contract_blocks_when_status_missing():
    with patch(
        "database.master_contract_status_db.get_status",
        return_value=None,
    ):
        check = sf_preflight.check_master_contract_ready("zerodha")
    assert check.passed is False


def test_master_contract_fails_closed_on_exception():
    with patch(
        "database.master_contract_status_db.get_status",
        side_effect=RuntimeError("db down"),
    ):
        check = sf_preflight.check_master_contract_ready("zerodha")
    assert check.passed is False


# --------------------------------------------------------------------------- #
# Composition with defaults — full check_can_go_live
# --------------------------------------------------------------------------- #


@pytest.fixture
def _all_defaults_passing():
    with patch(
        "strategies.sector_follow_cap5_vol.preflight.run_default_checks",
        return_value=PreflightResult(
            can_flip=True, blockers=[], warnings=[], snapshot={"defaults_ok": True}
        ),
    ):
        yield


@pytest.fixture
def _service_with_stocks_no_indices(monkeypatch):
    """Sets up the today's-actual-failure scenario via the provider resolver."""

    def fake_resolver():
        return _index_only_empty_provider, STOCKS

    monkeypatch.setattr(sf_preflight, "_resolve_provider_and_universe", fake_resolver)
    monkeypatch.setattr(
        "services.sector_follow_index_backfill.sector_index_symbols",
        lambda: INDICES,
    )


def test_todays_failure_scenario_blocks_flip(
    _all_defaults_passing, _service_with_stocks_no_indices
):
    """The integration test that would have refused today's 15:20 flip:
    stocks covered, all indices empty, defaults all passing."""
    with patch(
        "database.master_contract_status_db.get_status",
        return_value={"is_ready": True, "status": "success"},
    ):
        result = sf_preflight.check_can_go_live("live")

    assert result.can_flip is False
    assert any(
        "index" in b.lower() and ("8/8" in b or "8 sector" in b.lower()) for b in result.blockers
    ), f"expected index-coverage blocker, got: {result.blockers}"


def test_all_passing_allows_flip(_all_defaults_passing, monkeypatch):
    """Happy path — all gates pass, flip allowed."""
    monkeypatch.setattr(
        sf_preflight,
        "_resolve_provider_and_universe",
        lambda: (_passing_provider, STOCKS),
    )
    monkeypatch.setattr(
        "services.sector_follow_index_backfill.sector_index_symbols",
        lambda: INDICES,
    )
    with patch(
        "database.master_contract_status_db.get_status",
        return_value={"is_ready": True, "status": "success"},
    ):
        result = sf_preflight.check_can_go_live("live")
    assert result.can_flip is True
    assert result.blockers == []


def test_default_gate_failure_propagates(_service_with_stocks_no_indices):
    """If the system-wide default gate (e.g. broker session) fails, flip is
    blocked even if all custom gates pass."""
    with (
        patch(
            "strategies.sector_follow_cap5_vol.preflight.run_default_checks",
            return_value=PreflightResult(
                can_flip=False,
                blockers=["Broker session is not live"],
                warnings=[],
                snapshot={"defaults_ok": False},
            ),
        ),
        patch(
            "database.master_contract_status_db.get_status",
            return_value={"is_ready": True, "status": "success"},
        ),
        # Use a passing provider so the only failure is the default
        patch.object(
            sf_preflight,
            "_resolve_provider_and_universe",
            return_value=(_passing_provider, STOCKS),
        ),
    ):
        result = sf_preflight.check_can_go_live("live")
    assert result.can_flip is False
    assert "Broker session is not live" in result.blockers


def test_snapshot_records_preflight_path_and_strategy(_all_defaults_passing, monkeypatch):
    monkeypatch.setattr(
        sf_preflight,
        "_resolve_provider_and_universe",
        lambda: (_passing_provider, STOCKS),
    )
    monkeypatch.setattr(
        "services.sector_follow_index_backfill.sector_index_symbols",
        lambda: INDICES,
    )
    with patch(
        "database.master_contract_status_db.get_status",
        return_value={"is_ready": True, "status": "success"},
    ):
        result = sf_preflight.check_can_go_live("live")
    assert result.snapshot["preflight_path"] == "strategies.sector_follow_cap5_vol.preflight"
    assert result.snapshot["strategy_name"] == "sector_follow_cap5_vol"
    assert result.snapshot["target_mode"] == "live"
    assert "custom_checks" in result.snapshot
    # All custom checks should be present in the snapshot.
    names = {c["name"] for c in result.snapshot["custom_checks"]}
    assert names == {
        "sf_index_aggregator_coverage",
        "sf_stock_aggregator_coverage",
        "sf_master_contract_ready",
    }
