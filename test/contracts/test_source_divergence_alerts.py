"""Unit contract for :mod:`services.source_divergence_alerts` (issue #231).

These tests pin the runtime behaviour the three production seams depend on:

* threshold gating (no alert below ``SOURCE_DIVERGENCE_THRESHOLD_PCT``)
* flag gating (``SOURCE_DIVERGENCE_ALERTS_ENABLED=false`` silences everything)
* once-per-(service, symbol, day) dedup so the Telegram channel does not flood
* IST-date rollover clears the dedup table
* fail-safe semantics — neither a broken ``notify`` nor garbage args may raise

The Telegram path itself is mocked — every test patches
``services.notification_service.get_notification_service`` so a real bot never
fires from a test run.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

import services.source_divergence_alerts as sda
from services.source_divergence_alerts import check_and_alert


@pytest.fixture(autouse=True)
def _reset_dedup_around_each_test(monkeypatch):
    """Clear the in-process dedup table before AND after each test so the
    ordering of the suite never affects assertions.

    Also defaults the flag ON and threshold to 0.5 (the production defaults)
    so individual tests opt into overriding either one explicitly.
    """
    monkeypatch.setenv("SOURCE_DIVERGENCE_ALERTS_ENABLED", "true")
    monkeypatch.setenv("SOURCE_DIVERGENCE_THRESHOLD_PCT", "0.5")
    sda.reset_dedup_for_tests()
    yield
    sda.reset_dedup_for_tests()


@pytest.fixture
def mock_notify():
    """Capture every ``notify`` call across the test — no real Telegram fires."""
    mock_service = MagicMock()
    with patch(
        "services.notification_service.get_notification_service",
        return_value=mock_service,
    ):
        yield mock_service.notify


# ----------------------------------------------------------------------- #
# Contract A — divergence above threshold → exactly one alert.
# ----------------------------------------------------------------------- #
def test_above_threshold_emits_one_alert(mock_notify):
    """100 vs 102 is a 2.0% relative divergence — above the 0.5% default —
    so a single ``notify("source_divergence", ...)`` MUST fire and the helper
    MUST return True.
    """
    fired = check_and_alert(
        service="aggregator_seeder",
        symbol="INFY",
        source_a_label="historify_close",
        source_a_value=100.0,
        source_b_label="broker_close",
        source_b_value=102.0,
    )
    assert fired is True
    assert mock_notify.call_count == 1
    args, kwargs = mock_notify.call_args
    assert args[0] == "source_divergence"
    assert "INFY" in args[1]
    assert kwargs.get("service") == "aggregator_seeder"


# ----------------------------------------------------------------------- #
# Contract B — divergence below threshold → silent, no alert.
# ----------------------------------------------------------------------- #
def test_below_threshold_is_silent(mock_notify):
    """100 vs 100.2 is 0.2% — below 0.5% — so no alert."""
    fired = check_and_alert(
        service="aggregator_seeder",
        symbol="INFY",
        source_a_label="historify_close",
        source_a_value=100.0,
        source_b_label="broker_close",
        source_b_value=100.2,
    )
    assert fired is False
    assert mock_notify.call_count == 0


# ----------------------------------------------------------------------- #
# Contract C — flag OFF silences alerts even on a large divergence.
# Operator-killable: a single env flag must stop the Telegram channel.
# ----------------------------------------------------------------------- #
def test_flag_off_silences_alerts(monkeypatch, mock_notify):
    monkeypatch.setenv("SOURCE_DIVERGENCE_ALERTS_ENABLED", "false")
    fired = check_and_alert(
        service="aggregator_seeder",
        symbol="INFY",
        source_a_label="historify_close",
        source_a_value=100.0,
        source_b_label="broker_close",
        source_b_value=200.0,  # 100% divergence — would alert if flag were on.
    )
    assert fired is False
    assert mock_notify.call_count == 0


# ----------------------------------------------------------------------- #
# Contract D — per-(service, symbol, day) dedup. Load-bearing.
# Calling twice with the same args fires exactly one alert.
# ----------------------------------------------------------------------- #
def test_dedup_same_args_fires_exactly_once(mock_notify):
    """The same divergence reported twice on the same IST day must produce
    exactly one Telegram alert. The second call returns False (suppressed by
    dedup). This is what stops the rule re-firing every 5m bar close from
    becoming a Telegram flood.
    """
    a = check_and_alert(
        service="scanner_rule_buy",
        symbol="INFY",
        source_a_label="daily_close",
        source_a_value=100.0,
        source_b_label="live_5m_close",
        source_b_value=103.0,
    )
    b = check_and_alert(
        service="scanner_rule_buy",
        symbol="INFY",
        source_a_label="daily_close",
        source_a_value=100.0,
        source_b_label="live_5m_close",
        source_b_value=103.0,
    )
    assert a is True
    assert b is False
    assert mock_notify.call_count == 1


# ----------------------------------------------------------------------- #
# Contract E — dedup is keyed on (service, symbol, day). Distinct service
# OR distinct symbol = distinct alert.
# ----------------------------------------------------------------------- #
def test_dedup_does_not_block_distinct_services(mock_notify):
    a = check_and_alert(
        service="aggregator_seeder",
        symbol="INFY",
        source_a_label="x",
        source_a_value=100.0,
        source_b_label="y",
        source_b_value=110.0,
    )
    b = check_and_alert(
        service="scanner_rule_buy",
        symbol="INFY",
        source_a_label="x",
        source_a_value=100.0,
        source_b_label="y",
        source_b_value=110.0,
    )
    assert a is True
    assert b is True
    assert mock_notify.call_count == 2


def test_dedup_does_not_block_distinct_symbols(mock_notify):
    a = check_and_alert(
        service="aggregator_seeder",
        symbol="INFY",
        source_a_label="x",
        source_a_value=100.0,
        source_b_label="y",
        source_b_value=110.0,
    )
    b = check_and_alert(
        service="aggregator_seeder",
        symbol="TCS",
        source_a_label="x",
        source_a_value=100.0,
        source_b_label="y",
        source_b_value=110.0,
    )
    assert a is True
    assert b is True
    assert mock_notify.call_count == 2


# ----------------------------------------------------------------------- #
# Contract F — IST date rollover clears the dedup table.
# A genuine cross-midnight regression alerts the next morning.
# ----------------------------------------------------------------------- #
def test_date_rollover_resets_dedup(mock_notify):
    today = date(2026, 6, 29)
    tomorrow = today + timedelta(days=1)

    a = check_and_alert(
        service="aggregator_seeder",
        symbol="INFY",
        source_a_label="x",
        source_a_value=100.0,
        source_b_label="y",
        source_b_value=110.0,
        day_ist=today,
    )
    # Same args, same key — dedup would block this on the same day.
    b = check_and_alert(
        service="aggregator_seeder",
        symbol="INFY",
        source_a_label="x",
        source_a_value=100.0,
        source_b_label="y",
        source_b_value=110.0,
        day_ist=today,
    )
    # Rollover — fresh alert.
    c = check_and_alert(
        service="aggregator_seeder",
        symbol="INFY",
        source_a_label="x",
        source_a_value=100.0,
        source_b_label="y",
        source_b_value=110.0,
        day_ist=tomorrow,
    )
    assert (a, b, c) == (True, False, True)
    assert mock_notify.call_count == 2


# ----------------------------------------------------------------------- #
# Contract G — threshold is env-tunable.
# ----------------------------------------------------------------------- #
def test_threshold_env_var_respected(monkeypatch, mock_notify):
    monkeypatch.setenv("SOURCE_DIVERGENCE_THRESHOLD_PCT", "5.0")
    # 100 vs 102 is 2% — under the new 5% threshold.
    fired = check_and_alert(
        service="aggregator_seeder",
        symbol="INFY",
        source_a_label="x",
        source_a_value=100.0,
        source_b_label="y",
        source_b_value=102.0,
    )
    assert fired is False
    assert mock_notify.call_count == 0


# ----------------------------------------------------------------------- #
# Contract H — never raises. Garbage inputs short-circuit cleanly.
# ----------------------------------------------------------------------- #
def test_garbage_inputs_do_not_raise(mock_notify):
    # NaN / None / weird types — must not propagate an exception out.
    for bad_a, bad_b in [(None, 100.0), (100.0, None), ("nope", 100.0), (100.0, "nope")]:
        fired = check_and_alert(
            service="aggregator_seeder",
            symbol="INFY",
            source_a_label="x",
            source_a_value=bad_a,  # type: ignore[arg-type]
            source_b_label="y",
            source_b_value=bad_b,  # type: ignore[arg-type]
        )
        # We don't strictly require a specific return — only "never raises".
        assert fired in (True, False)


# ----------------------------------------------------------------------- #
# Contract I — a broken notify does not propagate.
# ----------------------------------------------------------------------- #
def test_broken_notify_does_not_propagate():
    """If ``notify`` raises, the helper must swallow and return True (the
    alert was *attempted*) rather than letting the broker observability
    layer crash the read site.
    """
    mock_service = MagicMock()
    mock_service.notify.side_effect = RuntimeError("telegram is down")
    with patch(
        "services.notification_service.get_notification_service",
        return_value=mock_service,
    ):
        fired = check_and_alert(
            service="aggregator_seeder",
            symbol="INFY",
            source_a_label="x",
            source_a_value=100.0,
            source_b_label="y",
            source_b_value=110.0,
        )
    # The alert was emitted (the side-effect fired) — return is True. The
    # important part is the test got here without an exception.
    assert fired is True


# ----------------------------------------------------------------------- #
# Contract J — invalid threshold env value falls back to the default.
# ----------------------------------------------------------------------- #
def test_invalid_threshold_env_falls_back_to_default(monkeypatch, mock_notify):
    monkeypatch.setenv("SOURCE_DIVERGENCE_THRESHOLD_PCT", "not-a-number")
    # 0.2% divergence — below the 0.5% default — must NOT alert.
    fired = check_and_alert(
        service="aggregator_seeder",
        symbol="INFY",
        source_a_label="x",
        source_a_value=100.0,
        source_b_label="y",
        source_b_value=100.2,
    )
    assert fired is False
    assert mock_notify.call_count == 0
