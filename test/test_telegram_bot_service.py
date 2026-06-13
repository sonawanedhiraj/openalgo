"""Tests for TelegramBotService soft-delete handling of dead recipients.

Covers the two error paths that must NOT crash a notification/broadcast:
- telegram.error.BadRequest "chat not found" (user deleted their account / never
  started the bot)
- telegram.error.Forbidden (user blocked the bot)

Both must soft-delete the registration via delete_telegram_user() and continue.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import telegram.error

from services.telegram_bot_service import TelegramBotService


def _make_running_service(send_side_effect):
    """Build a TelegramBotService wired with a mock bot ready to send."""
    svc = TelegramBotService()
    svc.is_running = True
    app = MagicMock()
    app.bot.send_message = AsyncMock(side_effect=send_side_effect)
    svc.application = app
    return svc


def test_send_notification_chat_not_found_soft_deletes():
    svc = _make_running_service(telegram.error.BadRequest("Chat not found"))
    with patch("services.telegram_bot_service.delete_telegram_user") as del_user:
        result = asyncio.run(svc.send_notification(123456789, "hi"))
    assert result is False
    del_user.assert_called_once_with(123456789)


def test_send_notification_forbidden_soft_deletes():
    svc = _make_running_service(telegram.error.Forbidden("Bot was blocked by the user"))
    with patch("services.telegram_bot_service.delete_telegram_user") as del_user:
        result = asyncio.run(svc.send_notification(555, "hi"))
    assert result is False
    del_user.assert_called_once_with(555)


def test_send_notification_parse_entities_still_retries_plain():
    """Regression guard: markdown-parse fallback must NOT soft-delete."""
    svc = _make_running_service([telegram.error.BadRequest("Can't parse entities"), None])
    with patch("services.telegram_bot_service.delete_telegram_user") as del_user:
        result = asyncio.run(svc.send_notification(999, "*bad"))
    assert result is True
    del_user.assert_not_called()
    assert svc.application.bot.send_message.call_count == 2


def test_broadcast_continues_past_forbidden_user():
    """3 users; the 2nd has blocked the bot. 1st + 3rd succeed, 2nd soft-deleted."""
    users = [
        {"telegram_id": 111, "notifications_enabled": True},
        {"telegram_id": 222, "notifications_enabled": True},
        {"telegram_id": 333, "notifications_enabled": True},
    ]
    send = AsyncMock(
        side_effect=[
            None,  # user 111 ok
            telegram.error.Forbidden("Bot was blocked by the user"),  # user 222
            None,  # user 333 ok
        ]
    )
    svc = TelegramBotService()
    svc.is_running = True
    app = MagicMock()
    app.bot.send_message = send
    svc.application = app

    with (
        patch("services.telegram_bot_service.get_all_telegram_users", return_value=users),
        patch("services.telegram_bot_service.delete_telegram_user") as del_user,
    ):
        success, fail = asyncio.run(svc.broadcast_message("hello"))

    assert success == 2
    assert fail == 1
    del_user.assert_called_once_with(222)


def test_broadcast_continues_past_chat_not_found_user():
    users = [
        {"telegram_id": 111, "notifications_enabled": True},
        {"telegram_id": 222, "notifications_enabled": True},
        {"telegram_id": 333, "notifications_enabled": True},
    ]
    send = AsyncMock(
        side_effect=[
            None,
            telegram.error.BadRequest("Chat not found"),
            None,
        ]
    )
    svc = TelegramBotService()
    svc.is_running = True
    app = MagicMock()
    app.bot.send_message = send
    svc.application = app

    with (
        patch("services.telegram_bot_service.get_all_telegram_users", return_value=users),
        patch("services.telegram_bot_service.delete_telegram_user") as del_user,
    ):
        success, fail = asyncio.run(svc.broadcast_message("hello"))

    assert success == 2
    assert fail == 1
    del_user.assert_called_once_with(222)
