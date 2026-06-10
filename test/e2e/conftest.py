"""E2E fixtures for test/e2e/test_critical_flows.py.

These tests exercise real cross-component seams (the unified ``strategy_daily_intent``
table → ``resolve_strategy_mode`` → engine intent gates, and the Telegram inbound
bot → that same table) without ever touching a production DB, a broker, or the
network.

Isolation strategy
------------------
The DB modules (``strategy_daily_intent_db``, ``telegram_db``) bind their engine +
``scoped_session`` to ``DATABASE_URL`` at import time. Rather than fight that with
env ordering, each fixture *rebinds* the module's ``engine`` / ``db_session`` to a
fresh temp-file SQLite via ``monkeypatch`` and creates the tables there. Module
functions reference ``db_session`` / ``engine`` by global name at call time, so the
rebind is transparent and fully reverted after the test. No live DB is read or
written — see memory "pytest pollutes live DB + preflight".
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

_IST = timezone(timedelta(hours=5, minutes=30))


def _rebind(module, base, monkeypatch, tmp_path, fname):
    """Point a DB module's engine/session at a fresh temp SQLite + create tables."""
    db_file = os.path.join(tmp_path, fname)
    eng = create_engine(
        f"sqlite:///{db_file}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(module, "engine", eng, raising=False)
    monkeypatch.setattr(module, "db_session", sess, raising=False)
    base.query = sess.query_property()
    base.metadata.create_all(bind=eng)
    return eng, sess


@pytest.fixture
def tmp_db_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def intent_db(monkeypatch, tmp_db_dir):
    """Real ``strategy_daily_intent_db`` bound to a temp SQLite. Yields the module."""
    import database.strategy_daily_intent_db as sdi

    _rebind(sdi, sdi.Base, monkeypatch, tmp_db_dir, "intent.db")
    return sdi


@pytest.fixture
def telegram_db_temp(monkeypatch, tmp_db_dir):
    """Real ``telegram_db`` bound to a temp SQLite (bot_config + users). Yields it."""
    import database.telegram_db as tdb

    _rebind(tdb, tdb.Base, monkeypatch, tmp_db_dir, "telegram.db")
    return tdb


@pytest.fixture
def clean_env(monkeypatch):
    """Strip strategy mode env vars so resolver fall-through is deterministic."""
    for k in (
        "SIMPLIFIED_ENGINE_MODE",
        "SECTOR_FOLLOW_CAP5_VOL_MODE",
        "STRATEGY_DAILY_INTENT_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STRATEGY_DAILY_INTENT_ENABLED", "true")


@pytest.fixture
def mock_telegram_client():
    """A recording stub standing in for ``telegram.Bot`` — captures sends so a
    test can assert what the inbound bot would have replied/pushed."""

    class _RecordingBot:
        def __init__(self):
            self.sent: list[dict] = []
            self.answered: list = []

        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
            return {"message_id": len(self.sent)}

    return _RecordingBot()


@pytest.fixture
def freeze_ist():
    """Return a callable producing a fixed IST datetime; pass a different
    ``hour``/``day`` to advance. Avoids a hard freezegun dependency — the
    services under test all accept an injected ``now``."""

    def _make(year=2026, month=6, day=11, hour=15, minute=20):
        return datetime(year, month, day, hour, minute, tzinfo=_IST)

    return _make
