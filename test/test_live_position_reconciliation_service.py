"""Unit tests for services/live_position_reconciliation_service.py (issue #265).

The broker positionbook read (``openposition_service.get_open_position``) and the
divergence alert (``source_divergence_alerts.check_and_alert``) are both mocked —
no network, no live DB, no broker session.

Guard semantics under test:
    * broker_qty == 0                → SUPPRESS (phantom), alert.
    * broker < journaled             → CLAMP to broker, alert.
    * broker opposite sign vs close  → SUPPRESS (nothing to close), alert.
    * broker fetch raises / fails    → FAIL CLOSED (proceed with journaled, never
                                       more) + alert.
    * broker consistent (>= journal) → PROCEED with journaled qty, no alert.
    * flag off                       → PROCEED unchanged, no broker call.
"""

from unittest.mock import patch

import pytest

from services import live_position_reconciliation_service as recon


@pytest.fixture(autouse=True)
def _reset_dedup():
    """Reset the alert dedup table so each test sees a fresh alert."""
    from services import source_divergence_alerts

    source_divergence_alerts.reset_dedup_for_tests()
    yield
    source_divergence_alerts.reset_dedup_for_tests()


def _mock_broker(quantity):
    """Return a (success, {'quantity': q, 'status': 'success'}, 200) tuple."""
    return (True, {"quantity": quantity, "status": "success"}, 200)


def _call(journaled=2, side="SELL"):
    return recon.reconcile_exit(
        strategy="futures_follow_cap50",
        api_key="k",
        symbol="NIFTY30JUN26FUT",
        exchange="NFO",
        product="NRML",
        expected_close_side=side,
        journaled_qty=journaled,
    )


# --------------------------------------------------------------------------- #
# broker flat → suppress
# --------------------------------------------------------------------------- #
def test_broker_flat_suppresses_and_alerts():
    with (
        patch("services.openposition_service.get_open_position", return_value=_mock_broker(0)),
        patch.object(recon, "_emit_drift_alert") as alert,
    ):
        d = _call(journaled=2)
    assert d.action == recon.ACTION_SUPPRESS
    assert d.guarded_qty == 0
    assert d.should_place is False
    assert d.reason == recon.REASON_BROKER_FLAT
    alert.assert_called_once()


# --------------------------------------------------------------------------- #
# broker < journaled → clamp
# --------------------------------------------------------------------------- #
def test_partial_broker_clamps_down_and_alerts():
    # journaled 2 lots (150 qty), broker holds only 75 → clamp to 75.
    with (
        patch("services.openposition_service.get_open_position", return_value=_mock_broker(75)),
        patch.object(recon, "_emit_drift_alert") as alert,
    ):
        d = _call(journaled=150)
    assert d.action == recon.ACTION_CLAMP
    assert d.guarded_qty == 75
    assert d.should_place is True
    assert d.reason == recon.REASON_PARTIAL_MISMATCH
    alert.assert_called_once()


# --------------------------------------------------------------------------- #
# broker opposite sign vs the expected close → suppress
# --------------------------------------------------------------------------- #
def test_opposite_sign_suppresses():
    # We want to SELL (close a long) but the broker is NET SHORT (-75) → there is
    # nothing to close on the long side; a SELL would deepen the short. Suppress.
    with (
        patch("services.openposition_service.get_open_position", return_value=_mock_broker(-75)),
        patch.object(recon, "_emit_drift_alert") as alert,
    ):
        d = _call(journaled=75, side="SELL")
    assert d.action == recon.ACTION_SUPPRESS
    assert d.guarded_qty == 0
    assert d.reason == recon.REASON_OPPOSITE_SIDE
    alert.assert_called_once()


def test_buy_close_of_short_uses_negative_broker_qty():
    # Closing a SHORT is a BUY; broker net -150, journaled 150 → proceed (consistent).
    with (
        patch("services.openposition_service.get_open_position", return_value=_mock_broker(-150)),
        patch.object(recon, "_emit_drift_alert") as alert,
    ):
        d = _call(journaled=150, side="BUY")
    assert d.action == recon.ACTION_PROCEED
    assert d.guarded_qty == 150
    alert.assert_not_called()


# --------------------------------------------------------------------------- #
# broker fetch fails / raises → fail closed
# --------------------------------------------------------------------------- #
def test_broker_fetch_failure_fails_closed_no_over_exit():
    failed = (False, {"status": "error", "message": "positionbook down"}, 500)
    with (
        patch("services.openposition_service.get_open_position", return_value=failed),
        patch.object(recon, "_emit_drift_alert") as alert,
    ):
        d = _call(journaled=2)
    assert d.action == recon.ACTION_PROCEED
    # Fail closed: never exit MORE than journaled.
    assert d.guarded_qty == 2
    assert d.reason == recon.REASON_BROKER_FETCH_FAILED
    alert.assert_called_once()


def test_broker_fetch_raises_fails_closed():
    with (
        patch(
            "services.openposition_service.get_open_position",
            side_effect=RuntimeError("boom"),
        ),
        patch.object(recon, "_emit_drift_alert") as alert,
    ):
        d = _call(journaled=2)
    assert d.action == recon.ACTION_PROCEED
    assert d.guarded_qty == 2
    assert d.reason == recon.REASON_BROKER_FETCH_FAILED
    alert.assert_called_once()


def test_no_api_key_fails_closed():
    with patch.object(recon, "_emit_drift_alert") as alert:
        d = recon.reconcile_exit(
            strategy="s",
            api_key=None,
            symbol="X",
            exchange="NFO",
            product="NRML",
            expected_close_side="SELL",
            journaled_qty=3,
        )
    assert d.action == recon.ACTION_PROCEED
    assert d.guarded_qty == 3
    assert d.reason == recon.REASON_BROKER_FETCH_FAILED
    alert.assert_called_once()


# --------------------------------------------------------------------------- #
# match → proceed, no alert
# --------------------------------------------------------------------------- #
def test_consistent_broker_proceeds_no_alert():
    with (
        patch("services.openposition_service.get_open_position", return_value=_mock_broker(150)),
        patch.object(recon, "_emit_drift_alert") as alert,
    ):
        d = _call(journaled=150)
    assert d.action == recon.ACTION_PROCEED
    assert d.guarded_qty == 150
    assert d.reason == recon.REASON_MATCH
    alert.assert_not_called()


def test_broker_larger_than_journaled_closes_only_journaled():
    # Broker shows 300 (an unrelated bigger position), journaled 150 → never close
    # MORE than we opened.
    with patch("services.openposition_service.get_open_position", return_value=_mock_broker(300)):
        d = _call(journaled=150)
    assert d.action == recon.ACTION_PROCEED
    assert d.guarded_qty == 150


# --------------------------------------------------------------------------- #
# flag off → no broker call, unchanged
# --------------------------------------------------------------------------- #
def test_flag_off_is_noop(monkeypatch):
    monkeypatch.setenv("POSITION_RECONCILE_ENABLED", "false")
    with patch("services.openposition_service.get_open_position") as broker:
        d = _call(journaled=2)
    broker.assert_not_called()
    assert d.action == recon.ACTION_PROCEED
    assert d.guarded_qty == 2
    assert d.reason == recon.REASON_DISABLED


def test_real_drift_alert_dispatches_once():
    """End-to-end through the real _emit_drift_alert (source_divergence stubbed)."""
    with (
        patch("services.openposition_service.get_open_position", return_value=_mock_broker(0)),
        patch("services.source_divergence_alerts.check_and_alert") as chk,
    ):
        d = _call(journaled=2)
    assert d.action == recon.ACTION_SUPPRESS
    chk.assert_called_once()
    kwargs = chk.call_args.kwargs
    assert kwargs["source_a_label"] == "journal_qty"
    assert kwargs["source_b_label"] == "broker_qty"
    assert kwargs["source_a_value"] == 2.0
    assert kwargs["source_b_value"] == 0.0
