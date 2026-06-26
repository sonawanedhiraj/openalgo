"""Tests for the master-contract readiness gate in ws_recovery_service.

Issue #141: ``recover()`` used to iterate symbols immediately on
``broker_session_refreshed`` even though the master-contract download (kicked
off in the same ``handle_auth_success`` path) was still in progress. The
symbol→token map being incomplete caused the post-login
"Could not find instrument token for NSE:XYZ" cascade.

This gate polls ``master_contract_status_db.get_status(broker)`` until
``is_ready`` is True or a bounded timeout fires; on timeout the run is skipped
(non-fatal — the next refresh event re-fires recovery).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.ws_recovery_service import WSRecoveryService


@pytest.fixture
def svc(monkeypatch):
    """A recovery service with all heavy side-effects stubbed out."""
    monkeypatch.setenv("WS_RECOVERY_MASTER_CONTRACT_WAIT_SEC", "2")
    service = WSRecoveryService.__new__(WSRecoveryService)
    # Inject the minimum attributes recover() reads. The aggregator is
    # mocked so the symbol loop is a no-op if we ever reach it.
    service._resolve_aggregator = lambda: MagicMock()  # type: ignore[method-assign]
    service._universe_provider = lambda: [("RELIANCE", "NSE"), ("INFY", "NSE")]
    service._api_key_provider = lambda: "api-key-x"
    service._history_fetcher = lambda *a, **kw: []
    service._notifier = MagicMock()  # type: ignore[method-assign]
    service.lookback_min = 20
    return service


def test_wait_returns_true_when_already_ready(monkeypatch, svc):
    """When master-contract is already ready, the wait is a near-instant pass."""
    with patch(
        "database.master_contract_status_db.get_status",
        return_value={"is_ready": True, "broker": "zerodha"},
    ) as gs:
        ok = svc._wait_for_master_contract_ready("zerodha")
    assert ok is True
    gs.assert_called_once_with("zerodha")


def test_wait_returns_false_on_timeout(monkeypatch, svc):
    """Permanently-downloading status drives the wait to bounded timeout."""
    with patch(
        "database.master_contract_status_db.get_status",
        return_value={"status": "downloading", "is_ready": False, "broker": "zerodha"},
    ):
        ok = svc._wait_for_master_contract_ready("zerodha")
    assert ok is False


def test_wait_tolerates_status_exception_proceeds(monkeypatch, svc):
    """A raising get_status logs + falls through to the no-row fast-pass.

    We don't want a transient master-contract-DB blip to block the recovery
    run; if the status query is failing, treat as "no info" and proceed.
    """
    with patch(
        "database.master_contract_status_db.get_status",
        side_effect=RuntimeError("db blip"),
    ) as gs:
        ok = svc._wait_for_master_contract_ready("zerodha")
    assert ok is True
    assert gs.call_count >= 1


def test_wait_blocks_only_when_status_is_pending(monkeypatch, svc):
    """The actual gate behaviour: pending/downloading status with
    is_ready=False is what causes the wait to time out."""
    with patch(
        "database.master_contract_status_db.get_status",
        return_value={"status": "downloading", "is_ready": False, "broker": "zerodha"},
    ):
        ok = svc._wait_for_master_contract_ready("zerodha")
    assert ok is False


def test_recover_skips_symbol_loop_when_master_contract_not_ready(monkeypatch, svc):
    """The integration-shaped assertion: recover() short-circuits to a skipped
    summary when the master contract never becomes ready, without ever calling
    the history fetcher."""
    fetcher = MagicMock(return_value=[])
    svc._history_fetcher = fetcher

    with patch(
        "database.master_contract_status_db.get_status",
        return_value={"status": "downloading", "is_ready": False, "broker": "zerodha"},
    ):
        result = svc.recover(username="dheeraj", broker="zerodha")

    assert result["status"] == "skipped"
    assert result["reason"] == "master_contract_not_ready"
    fetcher.assert_not_called()


def test_recover_proceeds_when_master_contract_ready(monkeypatch, svc):
    """Sanity: the gate doesn't break the happy path."""
    fetcher = MagicMock(return_value=[])
    svc._history_fetcher = fetcher

    with patch(
        "database.master_contract_status_db.get_status",
        return_value={"is_ready": True, "broker": "zerodha"},
    ):
        result = svc.recover(username="dheeraj", broker="zerodha")

    # 2 symbols × empty history → not skipped on the gate; proceeds to symbol loop.
    assert result["status"] != "skipped" or result.get("reason") != "master_contract_not_ready"
    assert fetcher.call_count == 2


def test_recover_skips_gate_when_broker_is_empty(monkeypatch, svc):
    """Backwards compat: an empty broker arg (legacy callers) bypasses the
    gate so we don't regress existing behaviour for tests / callers that don't
    pass it."""
    fetcher = MagicMock(return_value=[])
    svc._history_fetcher = fetcher

    with patch(
        "database.master_contract_status_db.get_status",
        return_value={"status": "downloading", "is_ready": False, "broker": "zerodha"},
    ) as gs:
        result = svc.recover(username="dheeraj", broker="")

    gs.assert_not_called()
    assert result["status"] != "skipped" or result.get("reason") != "master_contract_not_ready"
