"""Round-trip test for the ``bot_config.broadcast_enabled`` field.

DB isolation is inherited from ``test/conftest.py``, which redirects
``DATABASE_URL`` to a per-process temp dir before any ``database.*`` import
and inits the ``telegram_db`` schema there. This file used to carry its own
pre-conftest isolation hack — a module-level
``os.environ["DATABASE_URL"] = "sqlite:///:memory:"`` plus a rebind of
``telegram_db`` module globals onto a private in-memory engine — and ran its
whole body (two ``update_bot_config`` writes) at *import* time. Both leaked
(issue #666, the #662 pollution class): the env write poisoned any
``database.*`` module first imported afterwards, and the import-time writes
fired during collection. Do not reintroduce either; the row cleanup below
keeps this file hermetic in the shared temp DB instead.

Run: uv run pytest test/test_broadcast_save.py -v
"""

import pytest

import database.telegram_db as telegram_db
from database.telegram_db import get_bot_config, update_bot_config


@pytest.fixture(autouse=True)
def _clean_bot_config():
    """The conftest temp DB is shared by the whole pytest run; drop the
    singleton ``bot_config`` row after each test so the flags written here
    never leak into later tests reading the bot config."""
    yield
    try:
        telegram_db.db_session.query(telegram_db.BotConfig).delete(synchronize_session=False)
        telegram_db.db_session.commit()
    finally:
        telegram_db.db_session.remove()


def test_broadcast_enabled_roundtrips_as_bool():
    """broadcast_enabled must save and load as a real bool, both ways."""
    assert update_bot_config({"broadcast_enabled": False}) is True
    config = get_bot_config()
    assert config.get("broadcast_enabled") is False

    assert update_bot_config({"broadcast_enabled": True}) is True
    config = get_bot_config()
    assert config.get("broadcast_enabled") is True
