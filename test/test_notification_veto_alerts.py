"""Tests for ``publish_veto_decision_alert`` — LLM veto-layer Telegram alerts.

The helper is a module-level shim that:
* No-ops for any ``decision != 'skip'`` (including 'take', 'review_failed',
  'circuit_open').
* No-ops when ``NOTIFY_VETO_ALERTS`` env is 'false'.
* No-ops when ``NOTIFY_TELEGRAM_ENABLED`` master switch is false.
* Formats shadow vs active mode with distinct prefixes (🔬 [SHADOW] vs 🚫).
* Never raises — Telegram errors are caught and logged.

Tests mirror the recording-bot pattern in ``test_notification_service.py``:
swap a fake ``telegram_bot_service`` into the module, capture broadcast calls,
and reset the singleton so each test reads its own env snapshot.
"""

from __future__ import annotations

import logging

import pytest

# ---------------------------------------------------------------------------
# Recording fake (mirrors test_notification_service._RecordingBot).
# ---------------------------------------------------------------------------


class _RecordingBot:
    def __init__(self, *, is_running: bool = True, raise_on_send: bool = False) -> None:
        self.is_running = is_running
        self.bot_loop = object() if is_running else None
        self.sent: list[tuple[str, dict | None]] = []
        self._raise = raise_on_send

    def broadcast_message(self, message: str, filters: dict | None = None):
        self.sent.append((message, filters))
        if self._raise:
            raise RuntimeError("simulated telegram failure")

        async def _noop():
            return None

        return _noop()


def _install_fake_bot(monkeypatch, bot: _RecordingBot) -> None:
    import services.telegram_bot_service as tbs

    monkeypatch.setattr(tbs, "telegram_bot_service", bot, raising=False)

    import services.notification_service as ns

    def _fake_run_coro(coro, loop):  # noqa: ANN001
        try:
            coro.close()
        except Exception:
            pass
        return None

    monkeypatch.setattr(ns.asyncio, "run_coroutine_threadsafe", _fake_run_coro)


@pytest.fixture
def reset_singleton(monkeypatch):
    """Force the NotificationService singleton to reconstruct after env changes."""
    from services.notification_service import reset_notification_service_for_tests

    reset_notification_service_for_tests()
    yield
    reset_notification_service_for_tests()


def _enable(monkeypatch, **env):
    """Set NOTIFY_TELEGRAM_ENABLED=true plus any extra env, then reset singleton."""
    monkeypatch.setenv("NOTIFY_TELEGRAM_ENABLED", "true")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from services.notification_service import reset_notification_service_for_tests

    reset_notification_service_for_tests()


# ---------------------------------------------------------------------------
# Decision gating
# ---------------------------------------------------------------------------


def test_alert_no_op_when_decision_is_take(monkeypatch, reset_singleton):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _enable(monkeypatch)

    from services.notification_service import publish_veto_decision_alert

    publish_veto_decision_alert(
        symbol="RELIANCE",
        decision="take",
        reasoning="regime aligned",
        confidence=0.82,
        enforcement_mode="shadow",
        source="chartink_buy",
    )

    assert bot.sent == []


def test_alert_no_op_when_decision_is_review_failed(monkeypatch, reset_singleton):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _enable(monkeypatch)

    from services.notification_service import publish_veto_decision_alert

    publish_veto_decision_alert(
        symbol="RELIANCE",
        decision="review_failed",
        reasoning="bridge_timeout",
        confidence=0.0,
        enforcement_mode="shadow",
        source="chartink_buy",
    )

    assert bot.sent == []


def test_alert_no_op_when_decision_is_circuit_open(monkeypatch, reset_singleton):
    """Future-proofing — if a circuit breaker ever lands, the alert must NOT fire."""
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _enable(monkeypatch)

    from services.notification_service import publish_veto_decision_alert

    publish_veto_decision_alert(
        symbol="RELIANCE",
        decision="circuit_open",
        reasoning="breaker tripped",
        confidence=0.0,
        enforcement_mode="shadow",
        source="chartink_buy",
    )

    assert bot.sent == []


# ---------------------------------------------------------------------------
# Mode formatting
# ---------------------------------------------------------------------------


def test_alert_shadow_mode_formats_message(monkeypatch, reset_singleton):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _enable(monkeypatch)

    from services.notification_service import publish_veto_decision_alert

    publish_veto_decision_alert(
        symbol="INFY",
        decision="skip",
        reasoning="vix elevated, breadth negative",
        confidence=0.74,
        enforcement_mode="shadow",
        source="chartink_buy",
    )

    assert len(bot.sent) == 1
    body = bot.sent[0][0]
    assert "SHADOW" in body
    assert "INFY" in body
    assert "vix elevated, breadth negative" in body
    assert "74%" in body
    assert "chartink_buy" in body
    assert "would block" in body


def test_alert_active_mode_formats_message(monkeypatch, reset_singleton):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _enable(monkeypatch)

    from services.notification_service import publish_veto_decision_alert

    publish_veto_decision_alert(
        symbol="TCS",
        decision="skip",
        reasoning="post-15:00 fade pattern",
        confidence=0.88,
        enforcement_mode="active",
        source="chartink_sell",
    )

    assert len(bot.sent) == 1
    body = bot.sent[0][0]
    assert "BLOCKED" in body
    assert "TCS" in body
    assert "post-15:00 fade pattern" in body
    assert "88%" in body
    assert "chartink_sell" in body
    assert "🚫" in body


def test_alert_unknown_mode_falls_back_to_generic_format(monkeypatch, reset_singleton):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _enable(monkeypatch)

    from services.notification_service import publish_veto_decision_alert

    publish_veto_decision_alert(
        symbol="HDFC",
        decision="skip",
        reasoning="some reason",
        confidence=0.5,
        enforcement_mode="off",
        source="chartink_buy",
    )

    assert len(bot.sent) == 1
    body = bot.sent[0][0]
    assert "HDFC" in body
    assert "off" in body
    assert "some reason" in body


def test_alert_handles_missing_confidence(monkeypatch, reset_singleton):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _enable(monkeypatch)

    from services.notification_service import publish_veto_decision_alert

    publish_veto_decision_alert(
        symbol="WIPRO",
        decision="skip",
        reasoning="no confidence given",
        confidence=None,
        enforcement_mode="shadow",
        source="chartink_buy",
    )

    assert len(bot.sent) == 1
    body = bot.sent[0][0]
    assert "—" in body  # placeholder for unknown confidence


def test_alert_does_not_truncate_long_reasoning(monkeypatch, reset_singleton):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _enable(monkeypatch)

    from services.notification_service import publish_veto_decision_alert

    long_reason = "the LLM produced a long-form rationale " * 20  # ~720 chars
    publish_veto_decision_alert(
        symbol="SBIN",
        decision="skip",
        reasoning=long_reason,
        confidence=0.6,
        enforcement_mode="shadow",
        source="chartink_buy",
    )

    assert len(bot.sent) == 1
    assert long_reason in bot.sent[0][0]


# ---------------------------------------------------------------------------
# Toggles
# ---------------------------------------------------------------------------


def test_alert_no_op_when_notify_veto_alerts_false(monkeypatch, reset_singleton):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _enable(monkeypatch, NOTIFY_VETO_ALERTS="false")

    from services.notification_service import publish_veto_decision_alert

    publish_veto_decision_alert(
        symbol="ITC",
        decision="skip",
        reasoning="anything",
        confidence=0.7,
        enforcement_mode="shadow",
        source="chartink_buy",
    )

    assert bot.sent == []


def test_alert_no_op_when_telegram_master_disabled(monkeypatch, reset_singleton):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    # Master switch off — per-event toggle defaults true but should be gated by master.
    monkeypatch.setenv("NOTIFY_TELEGRAM_ENABLED", "false")
    from services.notification_service import reset_notification_service_for_tests

    reset_notification_service_for_tests()

    from services.notification_service import publish_veto_decision_alert

    publish_veto_decision_alert(
        symbol="AXIS",
        decision="skip",
        reasoning="anything",
        confidence=0.7,
        enforcement_mode="shadow",
        source="chartink_buy",
    )

    assert bot.sent == []


# ---------------------------------------------------------------------------
# Fail-safety
# ---------------------------------------------------------------------------


def test_alert_swallows_broadcast_exception(monkeypatch, reset_singleton, caplog):
    bot = _RecordingBot(is_running=True, raise_on_send=True)
    _install_fake_bot(monkeypatch, bot)
    _enable(monkeypatch)

    from services.notification_service import publish_veto_decision_alert

    with caplog.at_level(logging.WARNING):
        # MUST NOT raise.
        publish_veto_decision_alert(
            symbol="HDFCBANK",
            decision="skip",
            reasoning="this should not blow up",
            confidence=0.65,
            enforcement_mode="shadow",
            source="chartink_buy",
        )

    # The fake records the message before raising — broadcast was attempted.
    assert len(bot.sent) == 1
    # The exception was caught and logged inside notify().
    assert any("send failed" in rec.message for rec in caplog.records)


def test_alert_handles_missing_source(monkeypatch, reset_singleton):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _enable(monkeypatch)

    from services.notification_service import publish_veto_decision_alert

    publish_veto_decision_alert(
        symbol="NOSRC",
        decision="skip",
        reasoning="no source provided",
        confidence=0.5,
        enforcement_mode="shadow",
        source=None,
    )

    assert len(bot.sent) == 1
    body = bot.sent[0][0]
    assert "NOSRC" in body
    assert "unknown" in body  # source fallback placeholder
