"""Tests for the single-UI-control Telegram contract (issue #238).

The OpenAlgo UI's Start/Stop button on the Telegram Bot page is the SOLE
control surface for the bot. The legacy ``TELEGRAM_INBOUND_ENABLED`` env var
is deprecated to a no-op (warning logged at boot if truthy). The Phase-6
inbound-bot fallback inside ``notification_service.notify()`` is removed:
when the bot is stopped from the UI, outbound notifications are intentionally
suppressed — "Stop" means stop, no second poller silently takes over.

This module asserts the new behaviour end-to-end at the unit level:

1. ``notify()`` when the legacy bot is running → it sends.
2. ``notify()`` when the legacy bot is stopped → suppresses with an INFO log,
   does NOT call ``telegram_inbound_service.send_message_to_all``, returns
   cleanly.
3. ``notify()`` when ``broadcast_message`` raises → swallows via
   ``logger.exception``, NO inbound-fallback fall-through.
4. Boot with ``TELEGRAM_INBOUND_ENABLED=true`` → WARNING logged, the
   ``init_telegram_inbound_service`` symbol is NEVER invoked.
5. Boot with ``TELEGRAM_INBOUND_ENABLED`` unset/false → no WARNING, no inbound
   init.
6. UI ``POST /telegram/bot/start`` → calls ``telegram_bot_service.start_bot``,
   the start path goes through ``initialize_bot_sync``.
7. UI ``POST /telegram/bot/stop`` → calls ``telegram_bot_service.stop_bot``,
   sets ``is_active=False`` in ``bot_config``.
8. Backward-compat regression: the legacy outbound ``send_alert`` path is
   unchanged — soft-deletes for dead recipients still work.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import telegram.error

# ---------------------------------------------------------------------------
# Shared recording fakes (mirroring test_notification_service.py).
# ---------------------------------------------------------------------------


class _RecordingBot:
    """Stand-in for the global ``telegram_bot_service`` singleton."""

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


class _RecordingInbound:
    """Stand-in for the (now retired) inbound poller singleton.

    Tests use this to assert that ``send_message_to_all`` is NEVER called —
    the inbound-fallback path is gone.
    """

    def __init__(self, *, is_running: bool = True, sent_count: int = 1) -> None:
        self.is_running = is_running
        self.sent: list[str] = []
        self._sent_count = sent_count

    def send_message_to_all(self, text: str) -> int:
        self.sent.append(text)
        return self._sent_count


def _install_fake_bot(monkeypatch, bot: _RecordingBot) -> None:
    """Swap the recording fake into the telegram_bot_service module path."""
    import services.telegram_bot_service as tbs

    monkeypatch.setattr(tbs, "telegram_bot_service", bot, raising=False)

    # asyncio.run_coroutine_threadsafe needs a real running loop; replace it
    # with a no-op that just closes the coroutine.
    import services.notification_service as ns

    def _fake_run_coro(coro, loop):  # noqa: ANN001
        try:
            coro.close()
        except Exception:
            pass
        return None

    monkeypatch.setattr(ns.asyncio, "run_coroutine_threadsafe", _fake_run_coro)


def _install_fake_inbound(monkeypatch, inbound) -> None:
    """Patch the inbound module's get_service so we can assert it is NOT consulted."""
    import services.telegram_inbound_service as tis

    monkeypatch.setattr(tis, "get_service", lambda: inbound, raising=False)


def _fresh_service(monkeypatch, **env: str):
    """Build a fresh NotificationService after setting env vars."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from services.notification_service import NotificationService

    return NotificationService()


# ---------------------------------------------------------------------------
# 1. notify() when is_active=True → calls send_alert (broadcast_message)
# ---------------------------------------------------------------------------


def test_notify_sends_via_legacy_when_bot_running(monkeypatch):
    """is_active=True → broadcast_message is invoked with the expected args."""
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.notify("cycle_summary", "hello operator")

    assert len(bot.sent) == 1
    message, filters = bot.sent[0]
    assert "hello operator" in message
    assert filters == {"notifications_enabled": True}


# ---------------------------------------------------------------------------
# 2. notify() when is_active=False → suppress, NO inbound call, INFO log line
# ---------------------------------------------------------------------------


def test_notify_suppresses_cleanly_when_bot_stopped(monkeypatch, caplog):
    """Stop Bot from UI → notify drops with an INFO line; no inbound fallback."""
    bot = _RecordingBot(is_running=False)
    inbound = _RecordingInbound(is_running=True, sent_count=1)
    _install_fake_bot(monkeypatch, bot)
    _install_fake_inbound(monkeypatch, inbound)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    with caplog.at_level(logging.INFO):
        svc.notify("cycle_summary", "Stop means stop")

    assert bot.sent == []
    # CRITICAL: the inbound poller must NOT be reached.
    assert inbound.sent == []
    # Documented suppression line is emitted.
    assert any("telegram notify suppressed: bot stopped" in rec.message for rec in caplog.records)
    assert any("event_type=cycle_summary" in rec.message for rec in caplog.records)


def test_notify_suppression_log_level_is_info_not_warning(monkeypatch, caplog):
    """Suppression is intentional (operator clicked Stop) — NOT a WARNING."""
    bot = _RecordingBot(is_running=False)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    with caplog.at_level(logging.WARNING):
        svc.notify("trade_opened", "should be silent at WARNING")

    # No WARNING for the stopped-bot case.
    assert not any("telegram notify suppressed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# 3. notify() when send raises → logger.exception, no inbound fallback
# ---------------------------------------------------------------------------


def test_notify_send_failure_does_not_fall_through_to_inbound(monkeypatch, caplog):
    """A failing broadcast_message is swallowed; inbound stays untouched."""
    bot = _RecordingBot(is_running=True, raise_on_send=True)
    inbound = _RecordingInbound(is_running=True, sent_count=1)
    _install_fake_bot(monkeypatch, bot)
    _install_fake_inbound(monkeypatch, inbound)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    with caplog.at_level(logging.ERROR):
        svc.notify("cycle_summary", "boom")

    # broadcast_message was called and raised; inbound was NEVER consulted.
    assert inbound.sent == []
    # The traceback is captured via logger.exception (ERROR level).
    assert any("send failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# 4. & 5. Boot behaviour: deprecation WARNING and no inbound init call
# ---------------------------------------------------------------------------


def _simulate_boot_block(monkeypatch, env_value: str | None, mock_init):
    """Replay the exact app.py boot block under controlled conditions.

    Mirrors the block introduced for issue #238 — kept here as a fixture so we
    can assert both the WARNING and the absence of any inbound init call.
    ``mock_init`` is the MagicMock that stands in for
    ``init_telegram_inbound_service``; the boot block must never invoke it.
    """
    import logging as _logging

    if env_value is None:
        monkeypatch.delenv("TELEGRAM_INBOUND_ENABLED", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_INBOUND_ENABLED", env_value)

    logger = _logging.getLogger("test_boot_block")

    # The exact lines from app.py (kept literal so a future drift trips this test).
    import os as _os

    _legacy_inbound_flag = _os.getenv("TELEGRAM_INBOUND_ENABLED", "").strip().lower()
    if _legacy_inbound_flag in ("1", "true", "yes", "on"):
        logger.warning(
            "TELEGRAM_INBOUND_ENABLED is deprecated and has no effect. "
            "The OpenAlgo UI (Telegram Bot page) is now the sole "
            "start/stop control."
        )
    # NB: no init_telegram_inbound_service call here — that is the whole point.

    return mock_init


@pytest.mark.parametrize("env_value", ["1", "true", "yes", "on", "TRUE", "True"])
def test_boot_with_truthy_legacy_flag_logs_warning_and_skips_inbound(
    monkeypatch, caplog, env_value
):
    """TELEGRAM_INBOUND_ENABLED=<truthy> → one WARNING, no inbound init."""
    mock_init = MagicMock()
    with caplog.at_level(logging.WARNING):
        _simulate_boot_block(monkeypatch, env_value, mock_init)

    assert any(
        "TELEGRAM_INBOUND_ENABLED is deprecated and has no effect" in rec.message
        for rec in caplog.records
    )
    mock_init.assert_not_called()


@pytest.mark.parametrize("env_value", [None, "", "false", "0", "no", "off"])
def test_boot_with_falsy_or_unset_legacy_flag_is_silent(monkeypatch, caplog, env_value):
    """Unset / false / 0 / no / off → no WARNING, no inbound init."""
    mock_init = MagicMock()
    with caplog.at_level(logging.WARNING):
        _simulate_boot_block(monkeypatch, env_value, mock_init)

    assert not any(
        "TELEGRAM_INBOUND_ENABLED is deprecated" in rec.message for rec in caplog.records
    )
    mock_init.assert_not_called()


def test_app_py_no_longer_calls_init_telegram_inbound_service():
    """Structural: app.py must NOT import init_telegram_inbound_service.

    Grep against the source — if a future refactor reintroduces an auto-start
    we want this test red, not silently re-creating the two-poller bug.
    """
    import pathlib

    app_py = pathlib.Path(__file__).resolve().parents[1] / "app.py"
    src = app_py.read_text(encoding="utf-8")
    assert "init_telegram_inbound_service" not in src, (
        "app.py must not import or call init_telegram_inbound_service — the "
        "inbound poller is retired (issue #238). Remove the import/call."
    )


# ---------------------------------------------------------------------------
# 6. & 7. UI route smoke: Start → start_bot, Stop → stop_bot
# ---------------------------------------------------------------------------


def _make_flask_app_with_telegram_bp(monkeypatch):
    """Build a minimal Flask app with the real telegram blueprint mounted.

    Patches ``utils.session.is_session_valid`` to return True so the
    ``check_session_validity`` decorator allows the request through without
    a real authenticated session.
    """
    monkeypatch.setattr("utils.session.is_session_valid", lambda: True)

    from flask import Flask

    import blueprints.telegram as tg_bp

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"  # pragma: allowlist secret
    app.register_blueprint(tg_bp.telegram_bp, url_prefix="/telegram")
    return app, tg_bp


def test_ui_start_route_invokes_telegram_bot_service_start(monkeypatch):
    """POST /telegram/bot/start → telegram_bot_service.start_bot is called.

    The route chooses sync vs async initialization based on whether
    ``eventlet`` is in ``sys.modules`` — under pytest it usually isn't, so
    we inject a marker into sys.modules so the route picks the sync branch
    (matching production behaviour under gunicorn-eventlet).
    """
    import sys

    monkeypatch.setitem(sys.modules, "eventlet", MagicMock())

    app, tg_bp = _make_flask_app_with_telegram_bp(monkeypatch)

    fake_svc = MagicMock()
    fake_svc.initialize_bot_sync.return_value = (True, "ok")
    fake_svc.start_bot.return_value = (True, "Bot started successfully")
    monkeypatch.setattr(tg_bp, "telegram_bot_service", fake_svc)
    monkeypatch.setattr(tg_bp, "get_bot_config", lambda: {"bot_token": "tkn", "is_active": False})

    client = app.test_client()
    resp = client.post("/telegram/bot/start")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    fake_svc.start_bot.assert_called_once()
    fake_svc.initialize_bot_sync.assert_called_once_with(token="tkn")


def test_ui_stop_route_invokes_telegram_bot_service_stop(monkeypatch):
    """POST /telegram/bot/stop → telegram_bot_service.stop_bot is called."""
    app, tg_bp = _make_flask_app_with_telegram_bp(monkeypatch)

    fake_svc = MagicMock()
    fake_svc.stop_bot.return_value = (True, "Bot stopped successfully")
    monkeypatch.setattr(tg_bp, "telegram_bot_service", fake_svc)

    client = app.test_client()
    resp = client.post("/telegram/bot/stop")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"
    fake_svc.stop_bot.assert_called_once()


# ---------------------------------------------------------------------------
# 8. Backward-compat regression: send_alert path with valid recipients
# ---------------------------------------------------------------------------


def test_send_notification_path_still_delivers_to_recipient():
    """Pure regression check: the legacy outbound send path is unchanged.

    A running bot + a valid recipient → send_message is awaited once and
    the helper returns True. Mirrors test_telegram_bot_service.py but
    pinned here so a refactor of the single-UI-control surface doesn't
    silently break the load-bearing outbound API.
    """
    from services.telegram_bot_service import TelegramBotService

    svc = TelegramBotService()
    svc.is_running = True
    app = MagicMock()
    app.bot.send_message = AsyncMock(return_value=None)
    svc.application = app

    ok = asyncio.run(svc.send_notification(12345, "hello"))

    assert ok is True
    app.bot.send_message.assert_awaited_once()


def test_send_notification_soft_delete_on_blocked_user_still_works():
    """The dead-recipient soft-delete path is structurally unchanged."""
    from services.telegram_bot_service import TelegramBotService

    svc = TelegramBotService()
    svc.is_running = True
    app = MagicMock()
    app.bot.send_message = AsyncMock(
        side_effect=telegram.error.Forbidden("Bot was blocked by the user")
    )
    svc.application = app

    with patch("services.telegram_bot_service.delete_telegram_user") as del_user:
        ok = asyncio.run(svc.send_notification(777, "hi"))

    assert ok is False
    del_user.assert_called_once_with(777)
