"""Mode-only (2026-06-12): the Telegram inbound bot's per-day intent control
(run/pause/halt + capital cap) and the 08:45 IST morning-prompt job are retired.
These tests assert the retired commands now return the deprecation notice, write
nothing, and that register_jobs no longer schedules the morning prompt — while
/status still works.

Constructs the service with injected deps (mirrors test/e2e/test_critical_flows
_inbound), so no token, poller, or DB write is involved.
"""

import pytest

from services.telegram_inbound_service import _DEPRECATED_MSG, TelegramInboundService

AUTH = 4242
UNAUTH = 9999
SF = "sector_follow_cap5_vol"


class _RecordingIntentDB:
    """Tracks whether any intent write was attempted (it must NOT be)."""

    def __init__(self):
        self.writes = []
        self.deletes = []

    def set_intent(self, *a, **k):
        self.writes.append((a, k))
        return {}

    def get_intent(self, *a, **k):
        return None

    def delete_intent(self, *a, **k):
        self.deletes.append((a, k))
        return False


@pytest.fixture
def bot_and_db():
    db = _RecordingIntentDB()
    bot = TelegramInboundService(
        set_intent=db.set_intent,
        get_intent=db.get_intent,
        delete_intent=db.delete_intent,
        authorized_chat_ids=lambda: {AUTH},
        now=None,
    )
    return bot, db


@pytest.mark.parametrize(
    "text",
    [
        "/intent sector_follow_cap5_vol pause",
        "/intent sector_follow_cap5_vol halt",
        "/intent sector_follow_cap5_vol live",  # mode flip — also retired
        "/intent sector_follow_cap5_vol cap 100000",
        "/intent sector_follow_cap5_vol clear",
        "/pause sector_follow_cap5_vol",
        "/resume sector_follow_cap5_vol",
        "/halt sector_follow_cap5_vol",
        "/morning",
        "pause sector_follow_cap5_vol",  # free-text form
        "halt simplified",
    ],
)
def test_retired_commands_return_deprecation_and_write_nothing(bot_and_db, text):
    bot, db = bot_and_db
    reply = bot.handle_text(AUTH, 101, text)
    assert reply == _DEPRECATED_MSG
    assert db.writes == []  # no intent row written
    assert db.deletes == []


def test_inline_button_callback_is_deprecated(bot_and_db):
    bot, db = bot_and_db
    reply = bot.handle_callback(AUTH, 55, f"intent:{SF}:pause")
    assert reply == _DEPRECATED_MSG
    assert db.writes == []


def test_status_still_works(bot_and_db):
    bot, _ = bot_and_db
    reply = bot.handle_text(AUTH, 1, "/status")
    assert reply is not None
    assert reply != _DEPRECATED_MSG
    # Mode-only status reports each strategy's mode, not an intent.
    assert "simplified_engine" in reply
    assert "sector_follow_cap5_vol" in reply


def test_unauthorized_still_silently_ignored(bot_and_db):
    bot, db = bot_and_db
    assert bot.handle_text(UNAUTH, 1, "/intent sector_follow_cap5_vol pause") is None
    assert db.writes == []


def test_register_jobs_does_not_schedule_morning_prompt(bot_and_db):
    """The 08:45 IST morning-prompt job must NOT be registered (it is retired)."""
    bot, _ = bot_and_db

    class _FakeScheduler:
        def __init__(self):
            self.added = []
            self.removed = []

        def add_job(self, *a, **k):
            self.added.append((a, k))

        def remove_job(self, job_id):
            self.removed.append(job_id)

    sched = _FakeScheduler()
    bot.register_jobs(scheduler=sched)
    # No job added at all (the only job this service ever added was the prompt).
    assert sched.added == []
    added_ids = [k.get("id") for _a, k in sched.added]
    assert "telegram_inbound_morning_prompt" not in added_ids
