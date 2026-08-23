"""Test to verify Telegram bot configuration saving and loading.

DB isolation is inherited from ``test/conftest.py``, which redirects
``DATABASE_URL`` to a per-process temp dir before any ``database.*`` import
and inits the ``telegram_db`` schema there. This file used to carry its own
pre-conftest isolation hack — a module-level
``os.environ["DATABASE_URL"] = "sqlite:///:memory:"`` plus a rebind of
``telegram_db`` module globals onto a private in-memory engine. Both ran at
collection time and never reverted (issue #666, the #662 pollution class):
the env write poisoned any ``database.*`` module first imported afterwards
(NullPool + ``:memory:`` = a fresh empty DB per connection → "no such
table"), and the rebind detached ``telegram_db`` from the conftest temp DB
for every later test. Do not reintroduce either; the row cleanup below keeps
this file hermetic in the shared temp DB instead.

Run: uv run pytest test/test_telegram_config.py -v
"""

import pytest

import database.telegram_db as telegram_db
from database.telegram_db import get_bot_config, update_bot_config


@pytest.fixture(autouse=True)
def _clean_bot_config():
    """The conftest temp DB is shared by the whole pytest run; drop the
    singleton ``bot_config`` row after each test so the test token written
    here never leaks into later tests reading the bot config."""
    yield
    try:
        telegram_db.db_session.query(telegram_db.BotConfig).delete(synchronize_session=False)
        telegram_db.db_session.commit()
    finally:
        telegram_db.db_session.remove()


def test_config():
    """Configuration saves and loads back, token round-tripping through
    encryption at rest."""
    payload = {
        "bot_token": "test_token_123456789",
        "broadcast_enabled": True,
        "rate_limit_per_minute": 60,
    }

    assert update_bot_config(payload) is True

    config = get_bot_config()
    assert config.get("bot_token", "").startswith("test_token")
    assert config.get("token") == config.get("bot_token")  # backward-compat alias
    assert config.get("broadcast_enabled") is True
    assert config.get("rate_limit_per_minute") == 60
