"""Single-poller guard (issue #238): the Telegram inbound service must NEVER
start a competing ``getUpdates`` poller while the UI-toggled interactive bot
(``telegram_bot_service``, controlled by ``bot_config.is_active``) owns the token.

Two pollers on one token produce a persistent
``telegram.error.Conflict: terminated by other getUpdates request`` (observed
~200/hour all day on 2026-06-30). The UI bot is the single poller and single
sender; the inbound service defers to it.

These tests drive ``TelegramInboundService.start()`` with the Telegram
Application/updater fully mocked — no network, no real event loop. They assert:

* when ``bot_config.is_active`` is true, ``start()`` returns without ever
  spawning the poller thread (``start_polling`` is never reached);
* when the UI bot is inactive AND ``TELEGRAM_INBOUND_ENABLED`` is true, the
  inbound poller is allowed to start (the thread is spawned and reaches
  ``start_polling``).
"""

from __future__ import annotations

import threading
import time

import pytest

from services.telegram_inbound_service import TelegramInboundService

AUTH = 4242


def _make_bot(*, ui_active: bool) -> TelegramInboundService:
    """Build an inbound service with the UI-bot-active predicate injected, so the
    guard decision is deterministic and touches no real DB."""
    return TelegramInboundService(
        set_intent=lambda *a, **k: {},
        get_intent=lambda *a, **k: None,
        delete_intent=lambda *a, **k: False,
        authorized_chat_ids=lambda: {AUTH},
        ui_bot_active=lambda: ui_active,
        now=None,
    )


def test_inbound_poller_does_not_start_when_ui_bot_active(monkeypatch):
    """UI bot owns the token (is_active=True) → inbound start() must NOT spawn a
    poller thread and must NOT call start_polling. It returns (False, reason)."""
    bot = _make_bot(ui_active=True)

    started = {"polling": False, "thread": False}

    def _boom_thread(*a, **k):  # the poller thread must never be created
        started["thread"] = True
        raise AssertionError("inbound poller thread spawned while UI bot active")

    monkeypatch.setattr("services.telegram_inbound_service.original_threading.Thread", _boom_thread)

    ok, msg = bot.start()

    assert ok is False
    assert started["thread"] is False
    assert started["polling"] is False
    assert bot.is_running is False
    assert "issue #238" in msg or "UI bot" in msg


def test_inbound_poller_may_start_when_ui_bot_inactive(monkeypatch):
    """UI bot is down (is_active=False) and the inbound flag is on → the poller
    IS allowed to start: the thread is spawned and reaches start_polling. We mock
    the PTB Application so no real network/loop is touched."""
    bot = _make_bot(ui_active=False)

    polling_called = threading.Event()

    # Mock get_bot_config so start() finds a token without a DB.
    monkeypatch.setattr(
        "database.telegram_db.get_bot_config",
        lambda: {"bot_token": "dummy-token", "is_active": False},
    )

    class _FakeUpdater:
        def __init__(self):
            self.running = True

        async def start_polling(self, *a, **k):
            polling_called.set()

        async def stop(self):
            self.running = False

    class _FakeApp:
        def __init__(self):
            self.updater = _FakeUpdater()
            self.bot = object()

        def add_handler(self, *a, **k):
            pass

        async def initialize(self):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

        async def shutdown(self):
            pass

    class _FakeBuilder:
        def token(self, *a, **k):
            return self

        def build(self):
            return _FakeApp()

    import telegram.ext as ptb_ext

    monkeypatch.setattr(ptb_ext.Application, "builder", staticmethod(lambda: _FakeBuilder()))

    try:
        ok, msg = bot.start()
        # start() blocks up to ~10s waiting for is_running; the fake poller flips
        # it quickly. Either way the poller thread must have been spawned and
        # start_polling reached — that is the assertion that matters.
        assert polling_called.wait(timeout=10), "inbound poller never reached start_polling"
        assert ok is True
        assert bot.is_running is True
    finally:
        bot._stop_event.set()
        if bot.bot_thread and bot.bot_thread.is_alive():
            bot.bot_thread.join(timeout=5)
        # Best-effort cleanup so the daemon thread doesn't linger.
        time.sleep(0.1)


def test_default_ui_bot_active_predicate_reads_bot_config(monkeypatch):
    """The production default predicate reads bot_config.is_active and fails
    safe (treats an unreadable config as 'active' → never a second poller)."""
    from services import telegram_inbound_service as mod

    monkeypatch.setattr("database.telegram_db.get_bot_config", lambda: {"is_active": True})
    assert mod._default_ui_bot_active() is True

    monkeypatch.setattr("database.telegram_db.get_bot_config", lambda: {"is_active": False})
    assert mod._default_ui_bot_active() is False

    def _raise():
        raise RuntimeError("db down")

    monkeypatch.setattr("database.telegram_db.get_bot_config", _raise)
    # Fail-safe: an indeterminate config must NOT permit a second poller.
    assert mod._default_ui_bot_active() is True
